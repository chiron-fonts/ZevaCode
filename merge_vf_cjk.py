import argparse
import copy
import json
import tempfile
from datetime import datetime
from pathlib import Path
from time import monotonic

from fontTools import varLib
from fontTools.designspaceLib import AxisDescriptor, DesignSpaceDocument, SourceDescriptor
from fontTools.misc.transform import Transform
from fontTools.ttLib import TTFont
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable
from fontTools.varLib.errors import VarLibValidationError
from fontTools.varLib.instancer import instantiateVariableFont

from extract_font import (
    decompose_composites,
    parse_axis_settings,
    parse_normalize_width_json,
    parse_transformation,
    plan_width_normalization,
    preferred_normalized_width,
    serialize_normalize_width_rules,
)

TEMPORARY_MASTER_STRIP_TABLES = ("GSUB", "GPOS", "GDEF")
RESTORED_TARGET_TABLES = ("GDEF", "GPOS", "GSUB", "avar", "STAT", "name", "fvar")


def format_elapsed(elapsed_seconds: float) -> str:
    total_seconds = max(0, int(round(elapsed_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h{minutes:02}m{seconds:02}s"


def log_status(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {message}")


def parse_unicode_blocks(path: Path) -> list[tuple[int, int]]:
    intervals: list[tuple[int, int]] = []
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        body = raw_line.split(";", 1)[0].strip()
        if not body:
            continue
        if ".." not in body:
            raise ValueError(f"{path}:{line_number}: expected START..END, got {raw_line!r}")
        start_text, end_text = (part.strip() for part in body.split("..", 1))
        try:
            start = int(start_text, 16)
            end = int(end_text, 16)
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: invalid hex range {body!r}") from exc
        if start > end:
            raise ValueError(f"{path}:{line_number}: range start exceeds end in {body!r}")
        intervals.append((start, end))
    return merge_intervals(intervals)


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not intervals:
        return []
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def ensure_file_exists(path: Path, label: str) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"{label} font not found: {path}")


def ensure_directory_for(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def require_tables(font: TTFont, path: Path, label: str, tables: tuple[str, ...]) -> None:
    missing = [table for table in tables if table not in font]
    if missing:
        missing_text = ", ".join(missing)
        raise ValueError(f"{label} font at {path} is missing required tables: {missing_text}")


def get_font_axes(font: TTFont):
    if "fvar" not in font:
        raise ValueError("Expected variable font with an fvar table.")
    return list(font["fvar"].axes)


def get_font_axis_tags(font: TTFont) -> list[str]:
    return [axis.axisTag for axis in get_font_axes(font)]


def get_default_location(font: TTFont) -> dict[str, float]:
    return {axis.axisTag: axis.defaultValue for axis in get_font_axes(font)}


def location_key(location: dict[str, float], axis_tags: list[str]) -> tuple[tuple[str, float], ...]:
    return tuple((axis_tag, float(location[axis_tag])) for axis_tag in axis_tags)


def format_location(location: dict[str, float], axis_tags: list[str]) -> str:
    return ",".join(f"{axis_tag}={location[axis_tag]}" for axis_tag in axis_tags)


def parse_transform_value(value, label: str) -> Transform | None:
    if value is None:
        return None
    if isinstance(value, str):
        return parse_transformation(value)
    if isinstance(value, (list, tuple)) and len(value) == 6:
        try:
            return Transform(*[float(item) for item in value])
        except ValueError as exc:
            raise ValueError(f"{label} contains a non-numeric transform value: {value!r}") from exc
    raise ValueError(f"{label} must be null, a string, or a 6-item array; got {value!r}")


def validate_location(location: dict, axis_tags: list[str], label: str) -> dict[str, float]:
    if not isinstance(location, dict):
        raise ValueError(f"{label} must be an object mapping axis tags to values.")
    missing = [axis_tag for axis_tag in axis_tags if axis_tag not in location]
    extra = [axis_tag for axis_tag in location if axis_tag not in axis_tags]
    if missing or extra:
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected {', '.join(extra)}")
        raise ValueError(f"{label} axis mismatch: {'; '.join(detail)}")
    try:
        return {axis_tag: float(location[axis_tag]) for axis_tag in axis_tags}
    except ValueError as exc:
        raise ValueError(f"{label} contains non-numeric axis values: {location!r}") from exc


def ensure_default_master_present(master_specs: list[dict], default_location: dict[str, float], axis_tags: list[str]) -> None:
    default_key = location_key(default_location, axis_tags)
    for master_spec in master_specs:
        if location_key(master_spec["target"], axis_tags) == default_key:
            return
    raise ValueError(
        "Master configuration must include a target master at the default location "
        f"{format_location(default_location, axis_tags)}."
    )


def build_legacy_master_specs(
    target_font: TTFont,
    cjk_font: TTFont,
    low_location: dict[str, float],
    high_location: dict[str, float],
    default_location: dict[str, float] | None,
    default_transform: Transform | None,
) -> tuple[list[dict], str]:
    target_axis_tags = get_font_axis_tags(target_font)
    if target_axis_tags != ["wght"]:
        raise ValueError(
            "Targets with more than one axis require --master-config. "
            f"Found axes: {', '.join(target_axis_tags)}"
        )

    axis_tag = "wght"
    axis_range = get_axis_range(target_font, axis_tag)
    _, target_default_weight, _ = axis_range
    if default_location is None:
        default_location = derive_cjk_default_location(cjk_font, low_location, high_location, target_default_weight)
        rebuild_master_note = "Derived an internal default CJK master because fontTools.varLib.build requires a base master at the target axis default."
    else:
        rebuild_master_note = "Used explicit --cjk-default location for the rebuild base master."

    master_specs = [
        {
            "name": "low",
            "target": {axis_tag: axis_range[0]},
            "cjk": low_location,
            "transform": default_transform,
        },
        {
            "name": "default",
            "target": {axis_tag: axis_range[1]},
            "cjk": default_location,
            "transform": default_transform,
        },
        {
            "name": "high",
            "target": {axis_tag: axis_range[2]},
            "cjk": high_location,
            "transform": default_transform,
        },
    ]
    return master_specs, rebuild_master_note


def load_master_config(
    config_path: Path,
    target_font: TTFont,
    cjk_font: TTFont,
    default_transform: Transform | None,
) -> tuple[list[dict], str]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Master config at {config_path} must contain a top-level object.")

    target_axis_tags = get_font_axis_tags(target_font)
    cjk_axis_tags = get_font_axis_tags(cjk_font)
    configured_target_axes = payload.get("target_axes", target_axis_tags)
    if configured_target_axes != target_axis_tags:
        raise ValueError(
            f"Master config target_axes {configured_target_axes!r} do not match target font axes {target_axis_tags!r}."
        )

    masters = payload.get("masters")
    if not isinstance(masters, list) or not masters:
        raise ValueError(f"Master config at {config_path} must contain a non-empty masters array.")

    master_specs = []
    for index, master in enumerate(masters, start=1):
        if not isinstance(master, dict):
            raise ValueError(f"masters[{index - 1}] must be an object.")
        target_location = validate_location(master.get("target"), target_axis_tags, f"masters[{index - 1}].target")
        cjk_location = validate_location(master.get("cjk"), cjk_axis_tags, f"masters[{index - 1}].cjk")
        transform = parse_transform_value(master.get("transform"), f"masters[{index - 1}].transform")
        if transform is None:
            transform = default_transform
        master_name = master.get("name") or f"master-{index}"
        master_specs.append(
            {
                "name": master_name,
                "target": target_location,
                "cjk": cjk_location,
                "transform": transform,
            }
        )

    ensure_default_master_present(master_specs, get_default_location(target_font), target_axis_tags)
    return master_specs, f"Loaded target/CJK masters from {config_path}."

def unicode_cmap(font: TTFont) -> dict[int, str]:
    cmap: dict[int, str] = {}
    for table in font["cmap"].tables:
        if table.isUnicode():
            cmap.update(table.cmap)
    return cmap


def build_unicode_subtable_index(font: TTFont) -> tuple[list, list]:
    bmp_tables = []
    all_unicode_tables = []
    for table in font["cmap"].tables:
        if not table.isUnicode():
            continue
        if table.format == 4:
            bmp_tables.append(table)
        if table.format == 12:
            all_unicode_tables.append(table)
        elif table.format == 4:
            all_unicode_tables.append(table)
    return bmp_tables, all_unicode_tables


def create_unicode_format12_subtable(platform_id: int, plat_enc_id: int):
    table = CmapSubtable.newSubtable(12)
    table.platformID = platform_id
    table.platEncID = plat_enc_id
    table.language = 0
    table.cmap = {}
    return table


def ensure_unicode_format12_subtables(font: TTFont) -> list:
    cmap_table = font["cmap"]
    format12_tables = [table for table in cmap_table.tables if table.isUnicode() and table.format == 12]
    if format12_tables:
        return format12_tables

    existing_unicode_mappings = unicode_cmap(font)
    format12_tables = [
        create_unicode_format12_subtable(platform_id=0, plat_enc_id=4),
        create_unicode_format12_subtable(platform_id=3, plat_enc_id=10),
    ]
    for table in format12_tables:
        table.cmap.update(existing_unicode_mappings)
    cmap_table.tables.extend(format12_tables)
    return format12_tables


def update_unicode_cmaps(font: TTFont, mappings: dict[int, str]) -> None:
    bmp_tables, all_unicode_tables = build_unicode_subtable_index(font)
    if any(codepoint > 0xFFFF for codepoint in mappings):
        format12_tables = [table for table in all_unicode_tables if table.format == 12]
        if not format12_tables:
            format12_tables = ensure_unicode_format12_subtables(font)
            all_unicode_tables.extend(format12_tables)
    for codepoint, glyph_name in mappings.items():
        target_tables = all_unicode_tables
        if codepoint <= 0xFFFF:
            target_tables = bmp_tables + [table for table in all_unicode_tables if table.format == 12]
        for table in target_tables:
            if table.format == 4 and codepoint > 0xFFFF:
                continue
            table.cmap[codepoint] = glyph_name


def sync_font_counters(font: TTFont) -> None:
    glyph_order = list(font.getGlyphOrder())
    if "glyf" in font:
        font["glyf"].glyphOrder = glyph_order
    glyph_count = len(glyph_order)
    if "maxp" in font:
        font["maxp"].numGlyphs = glyph_count
    if "hhea" in font and "hmtx" in font:
        font["hhea"].numberOfHMetrics = len(font["hmtx"].metrics)


def get_axis_range(font: TTFont, axis_tag: str) -> tuple[float, float, float]:
    if "fvar" not in font:
        raise ValueError("Target font must be variable and contain an fvar table.")
    for axis in font["fvar"].axes:
        if axis.axisTag == axis_tag:
            return axis.minValue, axis.defaultValue, axis.maxValue
    raise ValueError(f"Axis {axis_tag!r} not found in target font.")


def instantiate_static_font(path: Path, axis_settings: dict[str, float] | None, label: str) -> TTFont:
    font = TTFont(path)
    require_tables(font, path, label, ("glyf", "hmtx", "cmap"))
    if "fvar" in font:
        if not axis_settings:
            raise ValueError(f"{label} font at {path} is variable but no axis settings were supplied.")
        font = instantiateVariableFont(font, axis_settings, inplace=False)
    elif axis_settings:
        raise ValueError(f"{label} font at {path} is static but axis settings were supplied: {axis_settings}")
    require_tables(font, path, label, ("glyf", "hmtx", "cmap"))
    return font


def derive_cjk_default_location(
    cjk_font: TTFont,
    low_location: dict[str, float],
    high_location: dict[str, float],
    target_default_weight: float,
) -> dict[str, float]:
    axis_map = {axis.axisTag: axis for axis in cjk_font["fvar"].axes}
    derived = dict(low_location)
    for axis_tag, axis in axis_map.items():
        if axis_tag == "wght":
            derived[axis_tag] = min(max(target_default_weight, axis.minValue), axis.maxValue)
            continue
        if axis_tag not in derived and axis_tag in high_location:
            derived[axis_tag] = high_location[axis_tag]
        if axis_tag not in derived:
            derived[axis_tag] = axis.defaultValue
    return derived


def append_new_glyph(
    font: TTFont,
    glyph_name: str,
    glyph,
    metrics: tuple[int, int],
) -> None:
    glyph_order = list(font.getGlyphOrder())
    glyph_order.append(glyph_name)
    font.setGlyphOrder(glyph_order)
    font["glyf"][glyph_name] = glyph
    font["hmtx"][glyph_name] = metrics
    sync_font_counters(font)


def glyph_width(glyph) -> int:
    if getattr(glyph, "numberOfContours", 0) <= 0:
        return 0
    if not hasattr(glyph, "xMin") or not hasattr(glyph, "xMax"):
        return 0
    return glyph.xMax - glyph.xMin


def transform_inserted_glyph(
    source_font: TTFont,
    glyph_name: str,
    transform: Transform | None,
    metrics: tuple[int, int] | None = None,
    normalize_shift_x: int = 0,
) -> tuple[object, tuple[int, int]]:
    source_glyph = source_font["glyf"][glyph_name]
    metric_source = copy.deepcopy(metrics or source_font["hmtx"][glyph_name])
    advance_width, lsb = metric_source
    if transform is None and not normalize_shift_x:
        return copy.deepcopy(source_glyph), metric_source

    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    glyph_set = source_font.getGlyphSet()
    pen = TTGlyphPen(glyph_set)
    draw_pen = pen
    if transform is not None:
        draw_pen = TransformPen(draw_pen, transform)
    if normalize_shift_x:
        draw_pen = TransformPen(draw_pen, Transform(1, 0, 0, 1, normalize_shift_x, 0))
    glyph_set[glyph_name].draw(draw_pen)
    transformed_glyph = pen.glyph()
    if hasattr(transformed_glyph, "recalcBounds"):
        transformed_glyph.recalcBounds(source_font["glyf"])

    if transform is None:
        return transformed_glyph, metric_source
    transformed_lsb = int(round(lsb * transform.xx + transform.dx))
    transformed_advance_width = max(0, int(round(advance_width * transform.xx)))
    return transformed_glyph, (transformed_advance_width, transformed_lsb)


def strip_tables(font: TTFont, table_tags: tuple[str, ...]) -> TTFont:
    stripped_font = copy.deepcopy(font)
    for table_tag in table_tags:
        if table_tag in stripped_font:
            del stripped_font[table_tag]
    return stripped_font


def make_source_descriptor(path: Path, name: str, family_name: str, style_name: str, location: dict[str, float]) -> SourceDescriptor:
    source = SourceDescriptor()
    source.path = str(path)
    source.filename = path.name
    source.name = name
    source.familyName = family_name
    source.styleName = style_name
    source.location = location
    return source


def build_designspace(
    axes,
    family_name: str,
    sources: list[tuple[str, Path, str, dict[str, float]]],
) -> DesignSpaceDocument:
    document = DesignSpaceDocument()
    for source_axis in axes:
        axis = AxisDescriptor()
        axis.name = source_axis.axisTag
        axis.tag = source_axis.axisTag
        axis.minimum = source_axis.minValue
        axis.default = source_axis.defaultValue
        axis.maximum = source_axis.maxValue
        document.addAxis(axis)

    for source_name, path, style_name, location in sources:
        document.addSource(make_source_descriptor(path, source_name, family_name, style_name, location))
    return document


def get_family_name(font: TTFont) -> str:
    if "name" not in font:
        return "MergedFont"
    for record in font["name"].names:
        if record.nameID == 1:
            try:
                return record.toUnicode()
            except UnicodeDecodeError:
                continue
    return "MergedFont"


def get_name_value(font: TTFont, name_ids: tuple[int, ...], fallback: str = "") -> str:
    if "name" not in font:
        return fallback
    for name_id in name_ids:
        for record in font["name"].names:
            if record.nameID != name_id:
                continue
            try:
                value = record.toUnicode()
            except UnicodeDecodeError:
                continue
            if value:
                return value
    return fallback


def sanitize_postscript_name(value: str) -> str:
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-")
    sanitized = "".join(ch for ch in value.replace(" ", "") if ch in allowed)
    if not sanitized:
        raise ValueError(f"Font name {value!r} cannot be converted to a valid PostScript name.")
    return sanitized


def rename_output_font(font: TTFont, family_name: str, style_name: str | None = None) -> None:
    if "name" not in font:
        raise ValueError("Output font has no name table to update.")

    if style_name is None:
        style_name = get_name_value(font, (17, 2), "Regular")
    version_name = get_name_value(font, (5,), f"Version {font['head'].fontRevision:.3f}")
    unique_prefix = get_name_value(font, (3,), version_name).split(";", 1)[0]
    postscript_family = sanitize_postscript_name(family_name)
    postscript_style = sanitize_postscript_name(style_name)

    replacements = {
        1: family_name,
        2: style_name,
        3: f"{unique_prefix};ZEV;{postscript_family}-{postscript_style}",
        4: f"{family_name} {style_name}".strip(),
        6: f"{postscript_family}-{postscript_style}",
        16: family_name,
        17: style_name,
        21: family_name,
        22: style_name,
        25: postscript_family,
    }

    existing_keys = {(record.platformID, record.platEncID, record.langID) for record in font["name"].names}
    for platform_id, plat_enc_id, lang_id in existing_keys:
        for name_id, value in replacements.items():
            font["name"].setName(value, nameID=name_id, platformID=platform_id, platEncID=plat_enc_id, langID=lang_id)

    if "CFF " in font:
        cff = font["CFF "].cff
        postscript_name = f"{postscript_family}-{postscript_style}"
        cff.fontNames = [postscript_name]
        top_dict = cff.topDictIndex[0]
        top_dict.FamilyName = family_name
        top_dict.FullName = f"{family_name} {style_name}".strip()
        top_dict.FontName = postscript_name


def refresh_unicode_coverage_metadata(font: TTFont, x_avg_char_width: int | None = None) -> None:
    if "OS/2" not in font:
        return
    font["OS/2"].recalcUnicodeRanges(font)
    if getattr(font["OS/2"], "version", 0) >= 1:
        font["OS/2"].recalcCodePageRanges(font)
    if x_avg_char_width is None:
        font["OS/2"].recalcAvgCharWidth(font)
    else:
        font["OS/2"].xAvgCharWidth = x_avg_char_width


def codepoint_string(codepoint: int) -> str:
    return f"U+{codepoint:04X}"


def collect_candidate_data(
    intervals: list[tuple[int, int]],
    target_font: TTFont,
    cjk_fonts: dict[str, TTFont],
) -> tuple[list[dict[str, str | int]], dict[str, int]]:
    target_cmap = unicode_cmap(target_font)
    target_glyph_names = set(target_font.getGlyphOrder())
    master_names = list(cjk_fonts.keys())
    cjk_maps = {name: unicode_cmap(font) for name, font in cjk_fonts.items()}

    counts = {
        "total_block_codepoints": 0,
        "present_in_all_cjk_masters": 0,
        "missing_in_cjk": 0,
        "existing_unicode": 0,
        "existing_glyph_name": 0,
        "source_name_mismatch": 0,
        "inserted": 0,
    }
    candidates: list[dict[str, str | int]] = []

    for start, end in intervals:
        for codepoint in range(start, end + 1):
            counts["total_block_codepoints"] += 1
            glyph_names = [cjk_maps[name].get(codepoint) for name in master_names]
            if not all(glyph_names):
                counts["missing_in_cjk"] += 1
                continue
            counts["present_in_all_cjk_masters"] += 1
            if codepoint in target_cmap:
                counts["existing_unicode"] += 1
                continue
            canonical_name = glyph_names[0]
            if any(glyph_name != canonical_name for glyph_name in glyph_names[1:]):
                counts["source_name_mismatch"] += 1
                continue
            if canonical_name in target_glyph_names:
                counts["existing_glyph_name"] += 1
                continue
            candidates.append({"codepoint": codepoint, "glyph_name": canonical_name})
            counts["inserted"] += 1
    counts["inserted_glyphs"] = len({str(candidate["glyph_name"]) for candidate in candidates})
    return candidates, counts


def build_master_normalization_action(master_name: str, normalization: dict[str, object]) -> dict[str, object]:
    return {
        "name": master_name,
        "status": normalization["status"],
        "reason": normalization["reason"],
        "original_advance_width": normalization["original_advance_width"],
        "final_advance_width": normalization["final_advance_width"],
        "shift_x": normalization["shift_x"],
    }


def merge_candidates_into_targets(
    candidates: list[dict[str, str | int]],
    target_fonts: dict[str, TTFont],
    master_specs: list[dict],
    cjk_fonts: dict[str, TTFont],
    normalize_width_rules: list[dict[str, object]],
) -> tuple[dict[int, str], dict[str, int], list[dict[str, object]]]:
    codepoint_to_glyph: dict[int, str] = {}
    appended_glyphs: set[str] = set()
    glyph_preferred_widths: dict[str, int | None] = {}
    glyph_normalization_actions: dict[str, dict[str, dict[str, object] | None]] = {}
    normalization_counts = {
        "matched_codepoints": 0,
        "processed_master_actions": 0,
        "skipped_master_actions": 0,
    }
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
        if glyph_name not in appended_glyphs:
            per_master_normalization: dict[str, dict[str, object] | None] = {}
            for master_spec in master_specs:
                master_name = master_spec["name"]
                target_font = target_fonts[master_name]
                source_font = cjk_fonts[master_name]
                normalization = plan_width_normalization(source_font["hmtx"][glyph_name], preferred_width)
                per_master_normalization[master_name] = normalization
                metrics = source_font["hmtx"][glyph_name]
                normalize_shift_x = 0
                if normalization is not None:
                    metrics = (normalization["final_advance_width"], normalization["final_lsb"])
                    normalize_shift_x = int(normalization["shift_x"])
                transformed_glyph, transformed_metrics = transform_inserted_glyph(
                    source_font,
                    glyph_name,
                    master_spec["transform"],
                    metrics=metrics,
                    normalize_shift_x=normalize_shift_x,
                )
                append_new_glyph(
                    target_font,
                    glyph_name,
                    transformed_glyph,
                    transformed_metrics,
                )
            glyph_normalization_actions[glyph_name] = per_master_normalization
            appended_glyphs.add(glyph_name)
        if preferred_width is not None:
            normalization_counts["matched_codepoints"] += 1
            action_entry = {
                "codepoint": codepoint_string(codepoint),
                "glyph_name": glyph_name,
                "preferred_width": preferred_width,
                "masters": [],
            }
            for master_spec in master_specs:
                master_name = master_spec["name"]
                normalization = glyph_normalization_actions[glyph_name][master_name]
                if normalization is None:
                    continue
                if normalization["status"] == "processed":
                    normalization_counts["processed_master_actions"] += 1
                else:
                    normalization_counts["skipped_master_actions"] += 1
                action_entry["masters"].append(build_master_normalization_action(master_name, normalization))
            normalization_actions.append(action_entry)
        codepoint_to_glyph[codepoint] = glyph_name

    for target_font in target_fonts.values():
        update_unicode_cmaps(target_font, codepoint_to_glyph)
        sync_font_counters(target_font)
    return codepoint_to_glyph, normalization_counts, normalization_actions


def rebuild_variable_font(
    target_font: TTFont,
    master_specs: list[dict],
    target_fonts: dict[str, TTFont],
    output_path: Path,
    output_family_name: str,
) -> None:
    preserved_x_avg_char_width = None
    if "OS/2" in target_font:
        preserved_x_avg_char_width = target_font["OS/2"].xAvgCharWidth
    with tempfile.TemporaryDirectory(prefix="merge-vf-cjk-") as temp_dir:
        temp_root = Path(temp_dir)
        designspace_path = temp_root / "merge.designspace"

        stripped_target_fonts = {name: strip_tables(font, TEMPORARY_MASTER_STRIP_TABLES) for name, font in target_fonts.items()}
        sources = []
        for index, master_spec in enumerate(master_specs, start=1):
            master_name = master_spec["name"]
            master_path = temp_root / f"{index:02d}-{master_name}.ttf"
            stripped_target_fonts[master_name].save(master_path)
            sources.append((master_name, master_path, master_name, master_spec["target"]))

        designspace = build_designspace(
            axes=get_font_axes(target_font),
            family_name=output_family_name,
            sources=sources,
        )
        designspace.write(designspace_path)

        try:
            variable_font, _, _ = varLib.build(str(designspace_path))
        except VarLibValidationError as exc:
            raise RuntimeError(f"Variable font rebuild failed: {exc}") from exc

        for table_tag in RESTORED_TARGET_TABLES:
            if table_tag in target_font:
                variable_font[table_tag] = copy.deepcopy(target_font[table_tag])
        rename_output_font(variable_font, output_family_name)
        refresh_unicode_coverage_metadata(variable_font, x_avg_char_width=preserved_x_avg_char_width)

        ensure_directory_for(output_path)
        variable_font.save(output_path)


def build_report(
    target_path: Path,
    cjk_path: Path,
    blocks_path: Path,
    output_path: Path,
    report_path: Path,
    output_family_name: str,
    master_specs: list[dict],
    normalize_width_rules: list[dict[str, object]],
    intervals: list[tuple[int, int]],
    counts: dict[str, int],
    codepoint_to_glyph: dict[int, str],
    normalization_counts: dict[str, int],
    normalization_actions: list[dict[str, object]],
    rebuild_master_note: str,
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
            "report": str(report_path),
            "font_name": output_family_name,
            "normalize_width": serialize_normalize_width_rules(normalize_width_rules) or None,
        },
        "interval_count": len(intervals),
        "masters": [
            {
                "name": master_spec["name"],
                "target": master_spec["target"],
                "cjk": master_spec["cjk"],
                "transform": tuple(master_spec["transform"]) if master_spec["transform"] is not None else None,
            }
            for master_spec in master_specs
        ],
        "counts": counts,
        "inserted": inserted,
        "normalize_width": {
            "counts": normalization_counts,
            "actions": normalization_actions,
        },
        "notes": {
            "rebuild": rebuild_master_note,
            "gsub_gpos_policy": "Target GSUB and GPOS tables are preserved from the target master workflow and are not augmented for inserted glyphs.",
        },
    }


def save_report(report: dict, report_path: Path) -> None:
    ensure_directory_for(report_path)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")


def validate_output_font(output_path: Path, expected_mappings: dict[int, str], original_target_path: Path) -> None:
    output_font = TTFont(output_path)
    output_cmap = unicode_cmap(output_font)
    target_cmap = unicode_cmap(TTFont(original_target_path))

    missing = [codepoint_string(codepoint) for codepoint in expected_mappings if output_cmap.get(codepoint) != expected_mappings[codepoint]]
    if missing:
        preview = ", ".join(missing[:10])
        raise RuntimeError(f"Output font is missing expected cmap mappings: {preview}")

    overwritten = []
    for codepoint, glyph_name in target_cmap.items():
        if output_cmap.get(codepoint) != glyph_name:
            overwritten.append(codepoint_string(codepoint))
            if len(overwritten) >= 10:
                break
    if overwritten:
        preview = ", ".join(overwritten)
        raise RuntimeError(f"Output font overwrote existing target cmap mappings: {preview}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="merge_vf_cjk.py",
        description="Merge selected glyphs from a CJK source variable font into a target variable font.",
    )
    parser.add_argument("--target", required=True, help="Target variable font path.")
    parser.add_argument("--cjk", required=True, help="Source CJK variable font path.")
    parser.add_argument("--blocks", required=True, help="Unicode block list path.")
    parser.add_argument("--master-config", default=None, help="JSON file describing target masters, CJK masters, and optional per-master transforms.")
    parser.add_argument("--cjk-low", default=None, help='Legacy low CJK master location, e.g. "wght=200,wdth=110,IDSP=100".')
    parser.add_argument("--cjk-high", default=None, help='Legacy high CJK master location, e.g. "wght=800,wdth=110,IDSP=100".')
    parser.add_argument(
        "--cjk-default",
        default=None,
        help='Legacy default CJK master location. If omitted, a default master is derived to match the target axis default.',
    )
    parser.add_argument("--out", required=True, help="Output merged variable font path.")
    parser.add_argument("--report", required=True, help="Output JSON report path.")
    parser.add_argument(
        "--font-name",
        default=None,
        help='Override the output font family name. Defaults to "ZevCode-JBM" for JetBrains Mono targets.',
    )
    parser.add_argument(
        "--cjk-transform",
        default=None,
        help='Optional affine transform for inserted CJK glyphs as "a,b,c,d,e,f", for example "2,0,0,2,0,5".',
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
    master_config_path = Path(args.master_config) if args.master_config else None

    ensure_file_exists(target_path, "Target")
    ensure_file_exists(cjk_path, "CJK")
    if not blocks_path.is_file():
        raise FileNotFoundError(f"Unicode block file not found: {blocks_path}")
    if master_config_path is not None and not master_config_path.is_file():
        raise FileNotFoundError(f"Master config file not found: {master_config_path}")

    intervals = parse_unicode_blocks(blocks_path)
    default_transform = parse_transformation(args.cjk_transform)
    normalize_width_rules = parse_normalize_width_json(args.normalize_width)

    target_font = TTFont(target_path)
    require_tables(target_font, target_path, "Target", ("glyf", "hmtx", "cmap", "fvar"))
    target_family_name = get_family_name(target_font)
    output_family_name = args.font_name or ("ZevCode-JBM" if target_family_name == "JetBrains Mono" else target_family_name)

    cjk_source_font = TTFont(cjk_path)
    require_tables(cjk_source_font, cjk_path, "CJK", ("glyf", "hmtx", "cmap", "fvar"))
    if master_config_path is not None:
        master_specs, rebuild_master_note = load_master_config(master_config_path, target_font, cjk_source_font, default_transform)
    else:
        if not args.cjk_low or not args.cjk_high:
            raise ValueError(
                "Either --master-config or both --cjk-low and --cjk-high are required."
            )
        master_specs, rebuild_master_note = build_legacy_master_specs(
            target_font=target_font,
            cjk_font=cjk_source_font,
            low_location=parse_axis_settings(args.cjk_low),
            high_location=parse_axis_settings(args.cjk_high),
            default_location=parse_axis_settings(args.cjk_default) if args.cjk_default else None,
            default_transform=default_transform,
        )

    log_status("Start merging variable font...")
    target_load_start = monotonic()
    log_status("Start loading and instantiating target masters...")
    target_fonts = {}
    for master_spec in master_specs:
        master_name = master_spec["name"]
        target_fonts[master_name] = instantiate_static_font(
            target_path,
            master_spec["target"],
            f"Target {master_name}",
        )
    log_status(f"Finished loading and instantiating target masters ({format_elapsed(monotonic() - target_load_start)} elapsed)")

    cjk_prepare_start = monotonic()
    log_status("Start loading and preparing CJK masters...")
    cjk_fonts = {}
    for master_spec in master_specs:
        master_name = master_spec["name"]
        cjk_fonts[master_name] = instantiate_static_font(
            cjk_path,
            master_spec["cjk"],
            f"CJK {master_name}",
        )

    log_status("Decomposing CJK composite glyphs...")
    for name, font in cjk_fonts.items():
        decomposed = decompose_composites(font, verbose=False)
        log_status(f"{name}: decomposed {decomposed} composite glyphs")
    log_status(f"Finished loading and preparing CJK masters ({format_elapsed(monotonic() - cjk_prepare_start)} elapsed)")

    candidate_start = monotonic()
    log_status("Start selecting candidate glyphs...")
    candidates, counts = collect_candidate_data(intervals, target_font, cjk_fonts)
    log_status(f"considered: {counts['total_block_codepoints']}")
    log_status(f"present in all CJK masters: {counts['present_in_all_cjk_masters']}")
    log_status(f"insertable: {counts['inserted']}")
    log_status(f"skipped existing target cmap: {counts['existing_unicode']}")
    log_status(f"skipped existing target glyph name: {counts['existing_glyph_name']}")
    log_status(f"skipped missing in CJK: {counts['missing_in_cjk']}")
    log_status(f"skipped source name mismatch: {counts['source_name_mismatch']}")
    log_status(f"Finished selecting candidate glyphs ({format_elapsed(monotonic() - candidate_start)} elapsed)")

    merge_start = monotonic()
    log_status("Start merging glyphs into target masters...")
    codepoint_to_glyph, normalization_counts, normalization_actions = merge_candidates_into_targets(
        candidates,
        target_fonts,
        master_specs,
        cjk_fonts,
        normalize_width_rules,
    )
    if normalize_width_rules:
        log_status(f"normalize_width matched codepoints: {normalization_counts['matched_codepoints']}")
        log_status(f"normalize_width processed master actions: {normalization_counts['processed_master_actions']}")
        log_status(f"normalize_width skipped master actions: {normalization_counts['skipped_master_actions']}")
    log_status(f"Finished merging glyphs into target masters ({format_elapsed(monotonic() - merge_start)} elapsed)")

    rebuild_start = monotonic()
    log_status("Start rebuilding output variable font...")
    rebuild_variable_font(target_font, master_specs, target_fonts, output_path, output_family_name)
    log_status(f"Finished rebuilding output variable font ({format_elapsed(monotonic() - rebuild_start)} elapsed)")

    validate_start = monotonic()
    log_status("Start validating output font...")
    validate_output_font(output_path, codepoint_to_glyph, target_path)
    log_status(f"Finished validating output font ({format_elapsed(monotonic() - validate_start)} elapsed)")

    report = build_report(
        target_path=target_path,
        cjk_path=cjk_path,
        blocks_path=blocks_path,
        output_path=output_path,
        report_path=report_path,
        output_family_name=output_family_name,
        master_specs=master_specs,
        normalize_width_rules=normalize_width_rules,
        intervals=intervals,
        counts=counts,
        codepoint_to_glyph=codepoint_to_glyph,
        normalization_counts=normalization_counts,
        normalization_actions=normalization_actions,
        rebuild_master_note=rebuild_master_note,
    )
    save_report(report, report_path)

    log_status(f"Saved merged VF to {output_path}")
    log_status(f"Saved merge report to {report_path}")
    log_status(f"Finished merging variable font ({format_elapsed(monotonic() - build_start)} elapsed)")


if __name__ == "__main__":
    main()
