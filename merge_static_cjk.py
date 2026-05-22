import argparse
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path

from fontTools.pens.cu2quPen import Cu2QuPen
from fontTools.pens.filterPen import DecomposingFilterPen
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.removeOverlaps import removeOverlaps
from fontTools.varLib.instancer import instantiateVariableFont

from extract_font import parse_axis_settings, parse_transformation
from merge_vf_cjk import (
    codepoint_string,
    ensure_directory_for,
    ensure_file_exists,
    get_family_name,
    refresh_unicode_coverage_metadata,
    rename_output_font,
    save_report,
    sanitize_postscript_name,
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
            print(f"Using cached CJK resource: {cache_path}")
            return TTFont(cache_path), cache_info | {"hit": True}

    cjk_font = instantiate_cjk_static_font(cjk_path, axis_settings)
    block_glyph_names = collect_block_glyph_names(intervals, cjk_font)
    if block_glyph_names:
        print(f"Flattening {len(block_glyph_names)} CJK glyphs for cache preparation...")
        removeOverlaps(cjk_font, glyphNames=block_glyph_names)
        if "glyf" in cjk_font:
            print("Decomposing selected CJK source composites for cache preparation...")
            decomposed = decompose_selected_composites(cjk_font, block_glyph_names)
            print(f"  decomposed {decomposed} composite glyphs")
        if cache_path is not None:
            print(f"Saving cached CJK resource: {cache_path}")
            save_font_atomic(cjk_font, cache_path)

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


def build_inserted_ttf_glyph(source_font: TTFont, glyph_name: str, transform):
    glyph_set = source_font.getGlyphSet()
    pen = TTGlyphPen(glyph_set)
    draw_pen = TransformPen(pen, transform) if transform is not None else pen
    glyph_set[glyph_name].draw(draw_pen)
    glyph = pen.glyph()
    if hasattr(glyph, "recalcBounds"):
        glyph.recalcBounds(source_font["glyf"])
    metrics = transform_metrics(source_font["hmtx"][glyph_name], transform)
    return glyph, metrics


def append_ttf_glyph(font: TTFont, glyph_name: str, glyph, metrics: tuple[int, int]) -> None:
    glyph_order = list(font.getGlyphOrder())
    glyph_order.append(glyph_name)
    font.setGlyphOrder(glyph_order)
    font["glyf"][glyph_name] = glyph
    font["hmtx"][glyph_name] = metrics
    sync_font_counters(font)


def build_inserted_cff_charstring(source_font: TTFont, glyph_name: str, transform):
    glyph_set = source_font.getGlyphSet()
    metrics = transform_metrics(source_font["hmtx"][glyph_name], transform)
    pen = T2CharStringPen(None, glyph_set)
    draw_pen = TransformPen(pen, transform) if transform is not None else pen
    glyph_set[glyph_name].draw(draw_pen)
    return pen, metrics


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


def merge_candidates_into_target(
    target_font: TTFont,
    cjk_font: TTFont,
    candidates: list[dict[str, int | str]],
    transform,
) -> dict[int, str]:
    codepoint_to_glyph = {}
    appended_glyphs: set[str] = set()
    for candidate in candidates:
        codepoint = int(candidate["codepoint"])
        glyph_name = str(candidate["glyph_name"])
        if glyph_name not in appended_glyphs:
            if "glyf" in target_font:
                glyph, metrics = build_inserted_ttf_glyph(cjk_font, glyph_name, transform)
                append_ttf_glyph(target_font, glyph_name, glyph, metrics)
            else:
                pen, metrics = build_inserted_cff_charstring(cjk_font, glyph_name, transform)
                append_cff_glyph(target_font, glyph_name, pen, metrics)
            appended_glyphs.add(glyph_name)
        codepoint_to_glyph[codepoint] = glyph_name
    update_unicode_cmaps(target_font, codepoint_to_glyph)
    return codepoint_to_glyph


def build_static_report(
    target_path: Path,
    cjk_path: Path,
    blocks_path: Path,
    output_path: Path,
    report_path: Path,
    output_family_name: str,
    cjk_axis: dict[str, float],
    transform,
    intervals: list[tuple[int, int]],
    counts: dict[str, int],
    codepoint_to_glyph: dict[int, str],
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
            "font_name": output_family_name,
            "cjk_axis": cjk_axis,
            "cjk_transform": tuple(transform) if transform is not None else None,
            "cjk_cache": cache_info,
        },
        "interval_count": len(intervals),
        "counts": counts,
        "inserted": inserted,
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
    target_family_root: str,
    source_family_name: str | None,
    source_postscript_name: str | None,
) -> None:
    if "name" not in font:
        raise ValueError("Output font has no name table to update.")

    target_postscript_root = sanitize_postscript_name(target_family_root)
    for record in font["name"].names:
        if record.nameID not in STATIC_NAME_REWRITE_IDS:
            continue
        try:
            original_value = record.toUnicode()
        except UnicodeDecodeError:
            continue
        new_value = original_value
        if record.nameID in (1, 4, 16, 18, 21):
            new_value = replace_name_fragment(new_value, source_family_name, target_family_root)
        if record.nameID in (3, 6, 25):
            new_value = replace_name_fragment(new_value, source_postscript_name, target_postscript_root)
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
            replace_name_fragment(name, source_postscript_name, target_postscript_root)
            for name in cff.fontNames
        ]
        if hasattr(top_dict, "FamilyName"):
            top_dict.FamilyName = replace_name_fragment(top_dict.FamilyName, source_family_name, target_family_root)
        if hasattr(top_dict, "FullName"):
            top_dict.FullName = replace_name_fragment(top_dict.FullName, source_family_name, target_family_root)
        if hasattr(top_dict, "FontName"):
            top_dict.FontName = replace_name_fragment(top_dict.FontName, source_postscript_name, target_postscript_root)


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
    parser.add_argument("--font-name", default=None, help="Override the output font family name.")
    parser.add_argument("--weight-name", default=None, help="Optional static weight name to append to the family name.")
    parser.add_argument("--style-name", default=None, help="Optional style name override, typically Regular or Italic.")
    parser.add_argument("--source-family-name", default=None, help="Static source family root to replace in relevant name records.")
    parser.add_argument(
        "--source-postscript-name",
        default=None,
        help="Static source PostScript root to replace in relevant PostScript/CFF name fields.",
    )
    parser.add_argument(
        "--cjk-transform",
        default=None,
        help='Optional affine transform for inserted CJK glyphs as "a,b,c,d,e,f".',
    )
    return parser.parse_args()


def main() -> None:
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

    target_font = TTFont(target_path)
    require_static_target_tables(target_font, target_path, "Target")
    preserved_x_avg_char_width = None
    if "OS/2" in target_font:
        preserved_x_avg_char_width = target_font["OS/2"].xAvgCharWidth
    output_family_name = args.font_name or get_family_name(target_font)
    use_replacement_naming = bool(args.source_family_name or args.source_postscript_name)
    if args.weight_name and not use_replacement_naming:
        output_family_name = f"{output_family_name} {args.weight_name}".strip()
    output_style_name = args.style_name or ("Italic" if "Italic" in target_path.stem else "Regular")

    print("Loading and instantiating CJK source...")
    cjk_font, cache_info = prepare_cjk_static_font(cjk_path, cjk_axis, intervals, blocks_path)

    print("Selecting candidate glyphs...")
    candidates, counts = collect_static_candidates(intervals, target_font, cjk_font)
    print(f"  considered: {counts['total_block_codepoints']}")
    print(f"  insertable: {counts['inserted']}")
    print(f"  skipped existing target cmap: {counts['existing_unicode']}")
    print(f"  skipped existing target glyph name: {counts['existing_glyph_name']}")
    print(f"  skipped missing in CJK: {counts['missing_in_cjk']}")

    print("Merging glyphs into static target...")
    codepoint_to_glyph = merge_candidates_into_target(target_font, cjk_font, candidates, transform)

    print("Renaming and saving output font...")
    if use_replacement_naming:
        rename_static_font_by_replacement(
            target_font,
            target_family_root=output_family_name,
            source_family_name=args.source_family_name,
            source_postscript_name=args.source_postscript_name,
        )
    else:
        rename_output_font(target_font, output_family_name, output_style_name)
    refresh_unicode_coverage_metadata(target_font, x_avg_char_width=preserved_x_avg_char_width)
    ensure_directory_for(output_path)
    target_font.save(output_path)
    if companion_ttf_output_path is not None:
        if output_path.suffix.lower() != ".otf":
            raise ValueError("Companion TTF output is only supported when the primary static output is OTF.")
        print("Converting merged OTF to companion TTF...")
        companion_ttf_font = convert_cff_font_to_ttf(target_font)
        ensure_directory_for(companion_ttf_output_path)
        companion_ttf_font.save(companion_ttf_output_path)

    print("Validating output font...")
    validate_output_font(output_path, codepoint_to_glyph, target_path)
    if companion_ttf_output_path is not None:
        print("Validating companion TTF font...")
        validate_output_font(companion_ttf_output_path, codepoint_to_glyph, target_path)

    report = build_static_report(
        target_path=target_path,
        cjk_path=cjk_path,
        blocks_path=blocks_path,
        output_path=output_path,
        report_path=report_path,
        output_family_name=output_family_name,
        cjk_axis=cjk_axis,
        transform=transform,
        intervals=intervals,
        counts=counts,
        codepoint_to_glyph=codepoint_to_glyph,
        cache_info=cache_info,
        companion_ttf_output=companion_ttf_output_path,
    )
    save_report(report, report_path)
    print(f"Wrote report to {report_path}")


if __name__ == "__main__":
    main()
