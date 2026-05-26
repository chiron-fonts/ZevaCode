import argparse
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path
from time import monotonic

from fontTools.misc.transform import Transform
from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.filterPen import DecomposingFilterPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.removeOverlaps import removeOverlaps
from fontTools.varLib.instancer import instantiateVariableFont

from extract_font import (
    parse_axis_settings,
    parse_normalize_width_json,
    parse_transformation,
    plan_width_normalization,
    preferred_normalized_width,
    serialize_normalize_width_rules,
)
from merge_vf_cjk import (
    codepoint_string,
    ensure_directory_for,
    ensure_file_exists,
    format_elapsed,
    log_status,
    refresh_unicode_coverage_metadata,
    save_report,
    sync_font_counters,
    unicode_cmap,
    update_unicode_cmaps,
    validate_output_font,
    parse_unicode_blocks,
)

CJK_CACHE_DIR_ENV = "ZEVCODE_CJK_CACHE_DIR"
CJK_CACHE_SCHEMA_VERSION = 1
STATIC_NAME_REWRITE_IDS = (1, 3, 4, 6, 16, 18, 21, 25)


def require_static_target_tables(font: TTFont, path: Path, label: str) -> None:
    missing = [table for table in ("hmtx", "cmap", "name") if table not in font]
    if missing:
        raise ValueError(f"{label} font at {path} is missing required tables: {', '.join(missing)}")
    if "fvar" in font:
        raise ValueError(f"{label} font at {path} must be static and must not contain an fvar table.")
    if "glyf" not in font and "CFF " not in font:
        raise ValueError(f"{label} font at {path} must contain either glyf or CFF outlines.")


def require_variable_cjk_tables(font: TTFont, path: Path) -> None:
    missing = [table for table in ("fvar", "hmtx", "cmap") if table not in font]
    if missing:
        raise ValueError(f"CJK font at {path} is missing required tables: {', '.join(missing)}")
    if "glyf" not in font and "CFF2" not in font and "CFF " not in font:
        raise ValueError(f"CJK font at {path} must contain glyf, CFF2, or CFF outlines.")


def instantiate_cjk_static_font(path: Path, axis_settings: dict[str, float]) -> TTFont:
    font = TTFont(path)
    require_variable_cjk_tables(font, path)
    instantiated = instantiateVariableFont(font, axis_settings, inplace=False)
    return instantiated


def format_axis_value(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return format(value, "g")


def normalized_axis_string(axis_settings: dict[str, float]) -> str:
    return ",".join(
        f"{axis_tag}={format_axis_value(value)}"
        for axis_tag, value in sorted(axis_settings.items())
    )


def file_fingerprint(path: Path, *, use_content_hash: bool = False) -> str:
    if use_content_hash:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    stat = path.stat()
    return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"


def outline_flavor(font: TTFont) -> str:
    if "glyf" in font:
        return "ttf"
    if "CFF2" in font or "CFF " in font:
        return "otf"
    raise ValueError("Expected glyf, CFF2, or CFF outlines.")


def outline_flavor_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".ttf":
        return "ttf"
    if suffix == ".otf":
        return "otf"
    raise ValueError(f"Unsupported CJK font suffix {suffix!r} for cache keying.")


def collect_block_glyph_names(intervals: list[tuple[int, int]], cjk_font: TTFont) -> list[str]:
    source_cmap = unicode_cmap(cjk_font)
    glyph_names: set[str] = set()
    for start, end in intervals:
        for codepoint in range(start, end + 1):
            glyph_name = source_cmap.get(codepoint)
            if glyph_name is not None:
                glyph_names.add(glyph_name)
    return sorted(glyph_names)


def decompose_selected_composites(font: TTFont, glyph_names: list[str]) -> int:
    if "glyf" not in font:
        return 0

    glyf_table = font["glyf"]
    glyph_set = font.getGlyphSet()
    hmtx = font["hmtx"] if "hmtx" in font else None
    count = 0
    for glyph_name in glyph_names:
        glyph = glyf_table[glyph_name]
        if not hasattr(glyph, "isComposite") or not glyph.isComposite():
            continue

        metrics = None
        if hmtx and glyph_name in hmtx.metrics:
            metrics = hmtx[glyph_name]
        pen = TTGlyphPen(glyph_set)
        filter_pen = DecomposingFilterPen(pen, glyph_set)
        glyph_set[glyph_name].draw(filter_pen)
        new_glyph = pen.glyph()
        if hasattr(new_glyph, "recalcBounds"):
            try:
                new_glyph.recalcBounds(glyf_table)
            except Exception:
                pass
        glyf_table[glyph_name] = new_glyph
        if metrics:
            hmtx[glyph_name] = metrics
        count += 1
    return count


def cache_key_payload(
    cjk_path: Path,
    blocks_path: Path,
    axis_settings: dict[str, float],
    flavor: str,
) -> dict[str, object]:
    return {
        "version": CJK_CACHE_SCHEMA_VERSION,
        "source": file_fingerprint(cjk_path),
        "blocks": file_fingerprint(blocks_path, use_content_hash=True),
        "axis": normalized_axis_string(axis_settings),
        "flavor": flavor,
    }


def cached_cjk_path(cache_root: Path, key_payload: dict[str, object], suffix: str) -> Path:
    digest = hashlib.sha256(json.dumps(key_payload, sort_keys=True).encode("utf-8")).hexdigest()
    return cache_root / "frozen_cjk" / f"{digest}{suffix}"


def save_font_atomic(font: TTFont, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f"{destination.stem}.",
        suffix=destination.suffix,
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temp_path = Path(temp_name)
    try:
        font.save(temp_path)
        temp_path.replace(destination)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise


def prepare_cjk_static_font(
    cjk_path: Path,
    axis_settings: dict[str, float],
    intervals: list[tuple[int, int]],
    blocks_path: Path,
) -> tuple[TTFont, dict[str, object]]:
    preparation_start = monotonic()
    log_status("Start loading and flattening CJK source...")
    cache_root_text = os.environ.get(CJK_CACHE_DIR_ENV, "").strip()
    flavor = outline_flavor_from_path(cjk_path)
    key_payload = cache_key_payload(cjk_path, blocks_path, axis_settings, flavor)
    cache_info: dict[str, object] = {
        "enabled": bool(cache_root_text),
        "hit": False,
        "path": None,
        "axis": normalized_axis_string(axis_settings),
    }

    cache_path = None
    if cache_root_text:
        cache_root = Path(cache_root_text)
        cache_path = cached_cjk_path(cache_root, key_payload, cjk_path.suffix.lower())
        cache_info["path"] = str(cache_path)
        if cache_path.exists():
            log_status(f"Using cached CJK resource: {cache_path}")
            log_status(f"Finished loading and flattening CJK source ({format_elapsed(monotonic() - preparation_start)} elapsed)")
            return TTFont(cache_path), cache_info | {"hit": True}

    cjk_font = instantiate_cjk_static_font(cjk_path, axis_settings)
    block_glyph_names = collect_block_glyph_names(intervals, cjk_font)
    if block_glyph_names:
        log_status(f"Flattening {len(block_glyph_names)} CJK glyphs for cache preparation...")
        removeOverlaps(cjk_font, glyphNames=block_glyph_names)
        if "glyf" in cjk_font:
            log_status("Decomposing selected CJK source composites for cache preparation...")
            decomposed = decompose_selected_composites(cjk_font, block_glyph_names)
            log_status(f"decomposed {decomposed} composite glyphs")
        if cache_path is not None:
            log_status(f"Saving cached CJK resource: {cache_path}")
            save_font_atomic(cjk_font, cache_path)

    log_status(f"Finished loading and flattening CJK source ({format_elapsed(monotonic() - preparation_start)} elapsed)")
    return cjk_font, cache_info


def collect_static_candidates(
    intervals: list[tuple[int, int]],
    target_font: TTFont,
    cjk_font: TTFont,
) -> tuple[list[dict[str, int | str]], dict[str, int]]:
    target_cmap = unicode_cmap(target_font)
    target_glyph_names = set(target_font.getGlyphOrder())
    source_cmap = unicode_cmap(cjk_font)

    counts = {
        "total_block_codepoints": 0,
        "inserted": 0,
        "existing_unicode": 0,
        "existing_glyph_name": 0,
        "missing_in_cjk": 0,
    }
    candidates = []

    for start, end in intervals:
        for codepoint in range(start, end + 1):
            counts["total_block_codepoints"] += 1
            glyph_name = source_cmap.get(codepoint)
            if glyph_name is None:
                counts["missing_in_cjk"] += 1
                continue
            if codepoint in target_cmap:
                counts["existing_unicode"] += 1
                continue
            if glyph_name in target_glyph_names:
                counts["existing_glyph_name"] += 1
                continue
            candidates.append({"codepoint": codepoint, "glyph_name": glyph_name})

    counts["inserted"] = len(candidates)
    return candidates, counts


def transform_metrics(metrics: tuple[int, int], transform) -> tuple[int, int]:
    if transform is None:
        return copy.deepcopy(metrics)
    advance_width, lsb = metrics
    transformed_lsb = int(round(lsb * transform.xx + transform.dx))
    transformed_advance_width = max(0, int(round(advance_width * transform.xx)))
    return transformed_advance_width, transformed_lsb


def build_inserted_ttf_glyph(
    source_font: TTFont,
    glyph_name: str,
    transform,
    metrics: tuple[int, int] | None = None,
    normalize_shift_x: int = 0,
):
    glyph_set = source_font.getGlyphSet()
    pen = TTGlyphPen(glyph_set)
    draw_pen = pen
    if transform is not None:
        draw_pen = TransformPen(draw_pen, transform)
    if normalize_shift_x:
        draw_pen = TransformPen(draw_pen, Transform(1, 0, 0, 1, normalize_shift_x, 0))
    glyph_set[glyph_name].draw(draw_pen)
    glyph = pen.glyph()
    if hasattr(glyph, "recalcBounds"):
        glyph.recalcBounds(source_font["glyf"])
    metrics = transform_metrics(metrics or source_font["hmtx"][glyph_name], transform)
    return glyph, metrics


def append_ttf_glyph(font: TTFont, glyph_name: str, glyph, metrics: tuple[int, int]) -> None:
    glyph_order = list(font.getGlyphOrder())
    glyph_order.append(glyph_name)
    font.setGlyphOrder(glyph_order)
    font["glyf"][glyph_name] = glyph
    font["hmtx"][glyph_name] = metrics
    sync_font_counters(font)


def build_inserted_cff_charstring(
    source_font: TTFont,
    glyph_name: str,
    transform,
    metrics: tuple[int, int] | None = None,
    normalize_shift_x: int = 0,
):
    glyph_set = source_font.getGlyphSet()
    pen = T2CharStringPen(None, glyph_set)
    draw_pen = pen
    if transform is not None:
        draw_pen = TransformPen(draw_pen, transform)
    if normalize_shift_x:
        draw_pen = TransformPen(draw_pen, Transform(1, 0, 0, 1, normalize_shift_x, 0))
    glyph_set[glyph_name].draw(draw_pen)
    return pen, transform_metrics(metrics or source_font["hmtx"][glyph_name], transform)


def append_cff_glyph(font: TTFont, glyph_name: str, pen: T2CharStringPen, metrics: tuple[int, int]) -> None:
    top_dict = font["CFF "].cff.topDictIndex[0]
    char_strings = top_dict.CharStrings
    char_string = pen.getCharString(
        private=getattr(char_strings, "private", getattr(top_dict, "Private", None)),
        globalSubrs=getattr(char_strings, "globalSubrs", font["CFF "].cff.GlobalSubrs),
    )
    glyph_order = list(font.getGlyphOrder())
    glyph_order.append(glyph_name)
    font.setGlyphOrder(glyph_order)
    top_dict.charset = font.getGlyphOrder()
    if char_strings.charStringsAreIndexed:
        char_strings.charStrings[glyph_name] = len(char_strings.charStringsIndex)
        char_strings.charStringsIndex.append(char_string)
        if hasattr(char_strings, "fdSelect"):
            char_strings.fdSelect.append(0)
    else:
        char_strings[glyph_name] = char_string
    font["hmtx"][glyph_name] = metrics
    sync_font_counters(font)


def build_normalization_action_entry(codepoint: int, glyph_name: str, normalization) -> dict[str, object]:
    return {
        "codepoint": codepoint_string(codepoint),
        "glyph_name": glyph_name,
        "status": normalization["status"],
        "reason": normalization["reason"],
        "preferred_width": normalization["preferred_width"],
        "original_advance_width": normalization["original_advance_width"],
        "final_advance_width": normalization["final_advance_width"],
        "shift_x": normalization["shift_x"],
    }


def merge_candidates_into_target(
    target_font: TTFont,
    cjk_font: TTFont,
    candidates: list[dict[str, int | str]],
    transform,
    normalize_width_rules: list[dict[str, object]],
) -> tuple[dict[int, str], dict[str, int], list[dict[str, object]]]:
    codepoint_to_glyph = {}
    appended_glyphs: set[str] = set()
    glyph_normalization: dict[str, dict[str, object] | None] = {}
    glyph_preferred_widths: dict[str, int | None] = {}
    normalization_counts = {"matched": 0, "processed": 0, "skipped": 0}
    normalization_actions: list[dict[str, object]] = []
    for candidate in candidates:
        codepoint = int(candidate["codepoint"])
        glyph_name = str(candidate["glyph_name"])
        preferred_width = preferred_normalized_width(codepoint, normalize_width_rules)
        if glyph_name in glyph_preferred_widths and glyph_preferred_widths[glyph_name] != preferred_width:
            raise ValueError(
                f"Glyph {glyph_name!r} is referenced by multiple codepoints with conflicting normalize_width rules."
            )
        if glyph_name not in glyph_preferred_widths:
            glyph_preferred_widths[glyph_name] = preferred_width
        normalization = glyph_normalization.get(glyph_name)
        if glyph_name not in glyph_normalization:
            normalization = plan_width_normalization(cjk_font["hmtx"][glyph_name], preferred_width)
            glyph_normalization[glyph_name] = normalization
        normalized_metrics = cjk_font["hmtx"][glyph_name]
        normalize_shift_x = 0
        if normalization is not None:
            normalization_counts["matched"] += 1
            normalization_counts[normalization["status"]] += 1
            normalization_actions.append(build_normalization_action_entry(codepoint, glyph_name, normalization))
            normalized_metrics = (normalization["final_advance_width"], normalization["final_lsb"])
            normalize_shift_x = int(normalization["shift_x"])
        if glyph_name not in appended_glyphs:
            if "glyf" in target_font:
                glyph, metrics = build_inserted_ttf_glyph(
                    cjk_font,
                    glyph_name,
                    transform,
                    metrics=normalized_metrics,
                    normalize_shift_x=normalize_shift_x,
                )
                append_ttf_glyph(target_font, glyph_name, glyph, metrics)
            else:
                pen, metrics = build_inserted_cff_charstring(
                    cjk_font,
                    glyph_name,
                    transform,
                    metrics=normalized_metrics,
                    normalize_shift_x=normalize_shift_x,
                )
                append_cff_glyph(target_font, glyph_name, pen, metrics)
            appended_glyphs.add(glyph_name)
        codepoint_to_glyph[codepoint] = glyph_name
    update_unicode_cmaps(target_font, codepoint_to_glyph)
    return codepoint_to_glyph, normalization_counts, normalization_actions


def build_static_report(
    target_path: Path,
    cjk_path: Path,
    blocks_path: Path,
    output_path: Path,
    report_path: Path,
    target_family_name: str | None,
    target_postscript_name: str | None,
    cjk_axis: dict[str, float],
    transform,
    normalize_width_rules: list[dict[str, object]],
    intervals: list[tuple[int, int]],
    counts: dict[str, int],
    codepoint_to_glyph: dict[int, str],
    normalization_counts: dict[str, int],
    normalization_actions: list[dict[str, object]],
    cache_info: dict[str, object],
    companion_ttf_output: Path | None,
) -> dict:
    inserted = [
        {"codepoint": codepoint_string(codepoint), "glyph_name": glyph_name}
        for codepoint, glyph_name in sorted(codepoint_to_glyph.items())
    ]
    return {
        "inputs": {
            "target": str(target_path),
            "cjk": str(cjk_path),
            "blocks": str(blocks_path),
            "output": str(output_path),
            "companion_ttf_output": str(companion_ttf_output) if companion_ttf_output is not None else None,
            "report": str(report_path),
            "target_family_name": target_family_name,
            "target_postscript_name": target_postscript_name,
            "cjk_axis": cjk_axis,
            "cjk_transform": tuple(transform) if transform is not None else None,
            "normalize_width": serialize_normalize_width_rules(normalize_width_rules) or None,
            "cjk_cache": cache_info,
        },
        "interval_count": len(intervals),
        "counts": counts,
        "inserted": inserted,
        "normalize_width": {
            "counts": normalization_counts,
            "actions": normalization_actions,
        },
        "notes": {
            "layout_policy": "Target layout tables are preserved and are not augmented for inserted glyphs.",
            "outline_policy": "Selected CJK glyphs are overlap-flattened before insertion into the static target.",
        },
    }


def replace_name_fragment(value: str, source: str | None, target: str) -> str:
    if not source or source not in value:
        return value
    return value.replace(source, target)


def rename_static_font_by_replacement(
    font: TTFont,
    source_family_name: str | None,
    target_family_name: str | None,
    source_postscript_name: str | None,
    target_postscript_name: str | None,
) -> None:
    if "name" not in font:
        raise ValueError("Output font has no name table to update.")

    for record in font["name"].names:
        if record.nameID not in STATIC_NAME_REWRITE_IDS:
            continue
        try:
            original_value = record.toUnicode()
        except UnicodeDecodeError:
            continue
        new_value = original_value
        if record.nameID in (1, 4, 16, 18, 21):
            new_value = replace_name_fragment(new_value, source_family_name, target_family_name)
        if record.nameID in (3, 6, 25):
            new_value = replace_name_fragment(new_value, source_postscript_name, target_postscript_name)
        if new_value != original_value:
            font["name"].setName(
                new_value,
                nameID=record.nameID,
                platformID=record.platformID,
                platEncID=record.platEncID,
                langID=record.langID,
            )

    if "CFF " in font:
        cff = font["CFF "].cff
        top_dict = cff.topDictIndex[0]
        cff.fontNames = [
            replace_name_fragment(name, source_postscript_name, target_postscript_name)
            for name in cff.fontNames
        ]
        if hasattr(top_dict, "FamilyName"):
            top_dict.FamilyName = replace_name_fragment(top_dict.FamilyName, source_family_name, target_family_name)
        if hasattr(top_dict, "FullName"):
            top_dict.FullName = replace_name_fragment(top_dict.FullName, source_family_name, target_family_name)
        if hasattr(top_dict, "FontName"):
            top_dict.FontName = replace_name_fragment(top_dict.FontName, source_postscript_name, target_postscript_name)


def validate_replacement_args(args: argparse.Namespace) -> bool:
    family_pair = (args.source_family_name, args.target_family_name)
    postscript_pair = (args.source_postscript_name, args.target_postscript_name)
    if bool(family_pair[0]) != bool(family_pair[1]):
        raise ValueError("Static replacement requires both --source-family-name and --target-family-name together.")
    if bool(postscript_pair[0]) != bool(postscript_pair[1]):
        raise ValueError("Static replacement requires both --source-postscript-name and --target-postscript-name together.")
    return any(value is not None for value in (*family_pair, *postscript_pair))


def convert_cff_font_to_ttf(font: TTFont) -> TTFont:
    if "CFF " not in font:
        raise ValueError("TTF companion conversion requires a CFF-backed source font.")

    converted = copy.deepcopy(font)
    glyph_order = converted.getGlyphOrder()
    glyph_set = converted.getGlyphSet()

    glyf_table = newTable("glyf")
    glyf_table.glyphs = {}
    glyf_table.glyphOrder = glyph_order
    hmtx_table = converted["hmtx"]
    for glyph_name in glyph_order:
        pen = TTGlyphPen(glyph_set)
        cu2qu_pen = Cu2QuPen(pen, max_err=1.0, reverse_direction=True)
        glyph_set[glyph_name].draw(cu2qu_pen)
        glyph = pen.glyph()
        if hasattr(glyph, "recalcBounds"):
            try:
                glyph.recalcBounds(glyf_table)
            except Exception:
                pass
        glyf_table.glyphs[glyph_name] = glyph
        if hasattr(glyph, "xMin") and glyph_name in hmtx_table.metrics:
            advance_width, _ = hmtx_table[glyph_name]
            hmtx_table[glyph_name] = (advance_width, glyph.xMin)

    converted["glyf"] = glyf_table
    converted["loca"] = newTable("loca")
    converted["head"].glyphDataFormat = 0
    converted["head"].indexToLocFormat = 0

    maxp_table = converted["maxp"] = newTable("maxp")
    maxp_table.tableVersion = 0x00010000
    maxp_table.numGlyphs = len(glyph_order)
    maxp_table.maxPoints = 0
    maxp_table.maxContours = 0
    maxp_table.maxCompositePoints = 0
    maxp_table.maxCompositeContours = 0
    maxp_table.maxZones = 2
    maxp_table.maxTwilightPoints = 0
    maxp_table.maxStorage = 0
    maxp_table.maxFunctionDefs = 0
    maxp_table.maxInstructionDefs = 0
    maxp_table.maxStackElements = 0
    maxp_table.maxSizeOfInstructions = 0
    maxp_table.maxComponentElements = 0
    maxp_table.maxComponentDepth = 0

    if "post" in converted:
        converted["post"].formatType = 2.0
        converted["post"].extraNames = []
        converted["post"].mapping = {}

    converted.sfntVersion = "\x00\x01\x00\x00"
    for tag in ("CFF ", "VORG"):
        if tag in converted:
            del converted[tag]
    sync_font_counters(converted)
    return converted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="merge_static_cjk.py",
        description="Merge selected glyphs from a variable CJK font into a static target font.",
    )
    parser.add_argument("--target", required=True, help="Target static font path.")
    parser.add_argument("--cjk", required=True, help="Source CJK variable font path.")
    parser.add_argument("--blocks", required=True, help="Unicode block list path.")
    parser.add_argument("--cjk-axis", required=True, help='CJK instancing axis settings, e.g. "wght=300,wdth=100,IDSP=0".')
    parser.add_argument("--out", required=True, help="Output merged static font path.")
    parser.add_argument("--report", required=True, help="Output JSON report path.")
    parser.add_argument("--ttf-out", default=None, help="Optional companion TTF output path for CFF-based static builds.")
    parser.add_argument("--source-family-name", default=None, help="Static source family root to replace in relevant name records.")
    parser.add_argument("--target-family-name", default=None, help="Replacement family root used for matched name records.")
    parser.add_argument(
        "--source-postscript-name",
        default=None,
        help="Static source PostScript root to replace in relevant PostScript/CFF name fields.",
    )
    parser.add_argument(
        "--target-postscript-name",
        default=None,
        help="Replacement PostScript root used for matched PostScript/CFF name fields.",
    )
    parser.add_argument(
        "--cjk-transform",
        default=None,
        help='Optional affine transform for inserted CJK glyphs as "a,b,c,d,e,f".',
    )
    parser.add_argument(
        "--normalize-width",
        default=None,
        help="Optional JSON normalize_width configuration forwarded from the profile wrapper.",
    )
    return parser.parse_args()


def main() -> None:
    build_start = monotonic()
    args = parse_args()

    target_path = Path(args.target)
    cjk_path = Path(args.cjk)
    blocks_path = Path(args.blocks)
    output_path = Path(args.out)
    report_path = Path(args.report)
    companion_ttf_output_path = Path(args.ttf_out) if args.ttf_out else None

    ensure_file_exists(target_path, "Target")
    ensure_file_exists(cjk_path, "CJK")
    if not blocks_path.is_file():
        raise FileNotFoundError(f"Unicode block file not found: {blocks_path}")

    intervals = parse_unicode_blocks(blocks_path)
    cjk_axis = parse_axis_settings(args.cjk_axis)
    transform = parse_transformation(args.cjk_transform)
    normalize_width_rules = parse_normalize_width_json(args.normalize_width)
    use_replacement_naming = validate_replacement_args(args)

    target_font = TTFont(target_path)
    require_static_target_tables(target_font, target_path, "Target")
    preserved_x_avg_char_width = None
    if "OS/2" in target_font:
        preserved_x_avg_char_width = target_font["OS/2"].xAvgCharWidth

    log_status("Start merging static font...")
    cjk_font, cache_info = prepare_cjk_static_font(cjk_path, cjk_axis, intervals, blocks_path)

    candidate_start = monotonic()
    log_status("Start selecting candidate glyphs...")
    candidates, counts = collect_static_candidates(intervals, target_font, cjk_font)
    log_status(f"considered: {counts['total_block_codepoints']}")
    log_status(f"insertable: {counts['inserted']}")
    log_status(f"skipped existing target cmap: {counts['existing_unicode']}")
    log_status(f"skipped existing target glyph name: {counts['existing_glyph_name']}")
    log_status(f"skipped missing in CJK: {counts['missing_in_cjk']}")
    log_status(f"Finished selecting candidate glyphs ({format_elapsed(monotonic() - candidate_start)} elapsed)")

    merge_start = monotonic()
    log_status("Start merging glyphs into static target...")
    codepoint_to_glyph, normalization_counts, normalization_actions = merge_candidates_into_target(
        target_font,
        cjk_font,
        candidates,
        transform,
        normalize_width_rules,
    )
    if normalize_width_rules:
        log_status(f"normalize_width matched: {normalization_counts['matched']}")
        log_status(f"normalize_width processed: {normalization_counts['processed']}")
        log_status(f"normalize_width skipped: {normalization_counts['skipped']}")
    log_status(f"Finished merging glyphs into static target ({format_elapsed(monotonic() - merge_start)} elapsed)")

    save_start = monotonic()
    log_status("Start renaming and saving output font...")
    if use_replacement_naming:
        rename_static_font_by_replacement(
            target_font,
            source_family_name=args.source_family_name,
            target_family_name=args.target_family_name,
            source_postscript_name=args.source_postscript_name,
            target_postscript_name=args.target_postscript_name,
        )
    refresh_unicode_coverage_metadata(target_font, x_avg_char_width=preserved_x_avg_char_width)
    ensure_directory_for(output_path)
    target_font.save(output_path)
    if companion_ttf_output_path is not None:
        if output_path.suffix.lower() != ".otf":
            raise ValueError("Companion TTF output is only supported when the primary static output is OTF.")
        log_status("Converting merged OTF to companion TTF...")
        companion_ttf_font = convert_cff_font_to_ttf(target_font)
        ensure_directory_for(companion_ttf_output_path)
        companion_ttf_font.save(companion_ttf_output_path)
    log_status(f"Finished renaming and saving output font ({format_elapsed(monotonic() - save_start)} elapsed)")

    validate_start = monotonic()
    log_status("Start validating output font...")
    validate_output_font(output_path, codepoint_to_glyph, target_path)
    if companion_ttf_output_path is not None:
        log_status("Validating companion TTF font...")
        validate_output_font(companion_ttf_output_path, codepoint_to_glyph, target_path)
    log_status(f"Finished validating output font ({format_elapsed(monotonic() - validate_start)} elapsed)")

    report = build_static_report(
        target_path=target_path,
        cjk_path=cjk_path,
        blocks_path=blocks_path,
        output_path=output_path,
        report_path=report_path,
        target_family_name=args.target_family_name,
        target_postscript_name=args.target_postscript_name,
        cjk_axis=cjk_axis,
        transform=transform,
        normalize_width_rules=normalize_width_rules,
        intervals=intervals,
        counts=counts,
        codepoint_to_glyph=codepoint_to_glyph,
        normalization_counts=normalization_counts,
        normalization_actions=normalization_actions,
        cache_info=cache_info,
        companion_ttf_output=companion_ttf_output_path,
    )
    save_report(report, report_path)
    log_status(f"Wrote report to {report_path}")
    log_status(f"Finished merging static font ({format_elapsed(monotonic() - build_start)} elapsed)")


if __name__ == "__main__":
    main()
