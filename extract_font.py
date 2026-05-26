import argparse
import json
import sys

from fontTools.misc.transform import Transform
from fontTools.pens.filterPen import DecomposingFilterPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
import os


def parse_axis_settings(s: str):
    """Parse axis settings string like 'wght=400,wdth=100' into a dict.
    Empty/None returns empty dict.
    """
    if not s:
        return {}
    out = {}
    for part in s.split(','):
        part = part.strip()
        if not part:
            continue
        if '=' not in part:
            raise ValueError(f"Axis setting '{part}' missing '='")
        k, v = part.split('=', 1)
        try:
            # Allow float values
            out[k.strip()] = float(v)
        except ValueError:
            raise ValueError(f"Invalid axis value for {k}: {v}")
    return out


def parse_transformation(s: str):
    """Parse a transformation string like "1,0,0,1,0,0" into a 6-tuple of floats.
    Empty/None returns identity (1,0,0,1,0,0).
    """
    if not s:
        return None
    parts = s.split(',')
    if len(parts) != 6:
        raise ValueError(f"Transformation must have 6 comma-separated values; got {len(parts)}")
    try:
        return Transform(*[float(p) for p in parts])
    except ValueError:
        raise ValueError(f"Invalid float value in transformation: {s}")


def merge_codepoint_ranges(ranges):
    if not ranges:
        return []
    merged = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def parse_codepoint_value(value, label: str) -> int:
    if isinstance(value, int):
        codepoint = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{label} must not be empty.")
        if text.upper().startswith("U+"):
            text = text[2:]
        try:
            codepoint = int(text, 16)
        except ValueError as exc:
            raise ValueError(f"{label} must be a hexadecimal codepoint; got {value!r}") from exc
    else:
        raise ValueError(f"{label} must be a hexadecimal string or integer; got {value!r}")
    if not 0 <= codepoint <= 0x10FFFF:
        raise ValueError(f"{label} must be within the Unicode range; got {codepoint!r}")
    return codepoint


def parse_normalize_width_payload(payload, label: str = "normalize_width"):
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise ValueError(f"{label} must be a list of normalization rules.")

    rules = []
    occupied_ranges = []
    for index, entry in enumerate(payload):
        entry_label = f"{label}[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{entry_label} must be a mapping.")
        if "codepoints" not in entry or "width" not in entry:
            raise ValueError(f"{entry_label} must define both codepoints and width.")

        raw_ranges = entry["codepoints"]
        if not isinstance(raw_ranges, list) or not raw_ranges:
            raise ValueError(f"{entry_label}.codepoints must be a non-empty list of [start, end] ranges.")
        ranges = []
        for range_index, raw_range in enumerate(raw_ranges):
            range_label = f"{entry_label}.codepoints[{range_index}]"
            if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
                raise ValueError(f"{range_label} must be a two-item [start, end] range.")
            start = parse_codepoint_value(raw_range[0], f"{range_label}[0]")
            end = parse_codepoint_value(raw_range[1], f"{range_label}[1]")
            if start > end:
                raise ValueError(f"{range_label} start must not exceed end.")
            ranges.append((start, end))
        ranges = merge_codepoint_ranges(ranges)

        width = entry["width"]
        if isinstance(width, float):
            if not width.is_integer():
                raise ValueError(f"{entry_label}.width must be an integer.")
            width = int(width)
        elif not isinstance(width, int):
            raise ValueError(f"{entry_label}.width must be an integer.")
        if width <= 0:
            raise ValueError(f"{entry_label}.width must be greater than zero.")

        for start, end in ranges:
            for existing_start, existing_end in occupied_ranges:
                if start <= existing_end and existing_start <= end:
                    raise ValueError(
                        f"{entry_label}.codepoints overlaps an earlier normalize_width range: "
                        f"{start:04X}..{end:04X} overlaps {existing_start:04X}..{existing_end:04X}"
                    )
            occupied_ranges.append((start, end))

        rules.append({"codepoints": ranges, "width": width})
    return rules


def parse_normalize_width_json(value: str | None, label: str = "--normalize-width"):
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON.") from exc
    return parse_normalize_width_payload(payload, label)


def serialize_normalize_width_rules(rules):
    return [
        {
            "codepoints": [[start, end] for start, end in rule["codepoints"]],
            "width": rule["width"],
        }
        for rule in rules
    ]


def preferred_normalized_width(codepoint: int, rules) -> int | None:
    for rule in rules:
        for start, end in rule["codepoints"]:
            if start <= codepoint <= end:
                return rule["width"]
    return None


def plan_width_normalization(metrics: tuple[int, int], preferred_width: int | None):
    if preferred_width is None:
        return None

    advance_width, lsb = metrics
    if advance_width >= preferred_width:
        return {
            "status": "skipped",
            "reason": "width-already-sufficient",
            "preferred_width": preferred_width,
            "original_advance_width": advance_width,
            "final_advance_width": advance_width,
            "original_lsb": lsb,
            "final_lsb": lsb,
            "shift_x": 0,
        }

    shift_x = (preferred_width - advance_width) // 2
    return {
        "status": "processed",
        "reason": None,
        "preferred_width": preferred_width,
        "original_advance_width": advance_width,
        "final_advance_width": preferred_width,
        "original_lsb": lsb,
        "final_lsb": lsb + shift_x,
        "shift_x": shift_x,
    }


def decompose_composites(font: TTFont, verbose: bool = True) -> int:
    """Decompose all composite glyphs in a TrueType 'glyf' table.

    This replaces composite glyphs with simple glyphs by drawing them into a pen
    and building a new TT glyph containing all contours. Metrics are preserved.

    Returns number of glyphs decomposed.
    """
    if 'glyf' not in font:  # CFF / other outlines
        if verbose:
            print("No 'glyf' table present (CFF or other outline); nothing to decompose.")
        return 0

    glyf_table = font['glyf']
    glyph_set = font.getGlyphSet()
    hmtx = font['hmtx'] if 'hmtx' in font else None

    from fontTools.pens.ttGlyphPen import TTGlyphPen

    count = 0
    for glyph_name in font.getGlyphOrder():
        g = glyf_table[glyph_name]
        if not hasattr(g, 'isComposite'):
            continue
        if g.isComposite():
            # Preserve metrics (composites can use USE_MY_METRICS flag)
            metrics = None
            if hmtx and glyph_name in hmtx.metrics:
                metrics = hmtx[glyph_name]
            pen = TTGlyphPen(glyph_set)
            filter_pen = DecomposingFilterPen(pen, glyph_set)
            # Using the high-level glyph set ensures recursive expansion
            glyph_set[glyph_name].draw(filter_pen)
            new_glyph = pen.glyph()
            # Recalculate bounds for safety
            if hasattr(new_glyph, 'recalcBounds'):
                try:
                    new_glyph.recalcBounds(glyf_table)
                except Exception:
                    pass
            glyf_table[glyph_name] = new_glyph
            if metrics:
                hmtx[glyph_name] = metrics
            count += 1
            if verbose and count % 200 == 0:
                print(f"Decomposed {count} composites so far...")
    if verbose:
        print(f"Total composite glyphs decomposed: {count}")
    return count


def apply_glyph_transform(font: TTFont, transform: Transform, cjk_mode_transform: bool, verbose: bool = True) -> int:
    """Apply a 2x3 affine transformation to all glyphs in a TrueType 'glyf' table.

    The transformation is given as a fontTools Transform object.

    If cjk_mode_transform is True, check if the glyph's advance width is 1000upm.
    In this case, only the glyph outlines are transformed, GPOS table is left unchanged.
    This is useful for CJK fonts where the advance widths should remain the same.

    Returns number of glyphs transformed.
    """
    if 'glyf' not in font:  # CFF / other outlines
        if verbose:
            print("No 'glyf' table present (CFF or other outline); nothing to transform.")
        return 0

    glyf_table = font['glyf']
    glyph_set = font.getGlyphSet()

    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.pens.transformPen import TransformPen

    count = 0
    for glyph_name in font.getGlyphOrder():
        g = glyf_table[glyph_name]
        if g.isComposite():
            raise ValueError(f"Glyph '{glyph_name}' is still composite; decompose before applying transform.")

        if cjk_mode_transform:
            # Check if advance width is 1000 upm; if not, skip transforming this glyph.
            if 'hmtx' in font and glyph_name in font['hmtx'].metrics:
                advanceWidth, lsb = font['hmtx'][glyph_name]
                if advanceWidth != 1000:
                    if verbose:
                        print(
                            f"Skipping glyph '{glyph_name}' with advance width {advanceWidth} (not 1000) in CJK mode.")
                    continue
            else:
                if verbose:
                    print(f"Skipping glyph '{glyph_name}' with no hmtx entry in CJK mode.")
                continue

        if g.numberOfContours > 0:
            pen = TTGlyphPen(glyph_set)
            transform_pen = TransformPen(pen, transform)
            glyph_set[glyph_name].draw(transform_pen)
            new_glyph = pen.glyph()
            # Recalculate bounds for safety
            if not cjk_mode_transform and hasattr(new_glyph, 'recalcBounds'):
                try:
                    new_glyph.recalcBounds(glyf_table)
                except Exception:
                    pass
            glyf_table[glyph_name] = new_glyph

        # Also adjust metrics if present
        if not cjk_mode_transform:
            if 'hmtx' in font and glyph_name in font['hmtx'].metrics:
                advanceWidth, lsb = font['hmtx'][glyph_name]
                xScale = transform.xx
                ySkew = transform.xy
                new_advanceWidth = int(round(advanceWidth * xScale))
                new_lsb = int(round(lsb * xScale + 0 * ySkew))  # y component of lsb is always 0
                font['hmtx'][glyph_name] = (new_advanceWidth, new_lsb)
        count += 1
        if verbose and count % 200 == 0:
            print(f"Transformed {count} glyphs so far...")
    if verbose:
        print(f"Total glyphs transformed: {count}")
    return count


def apply_gpos_transform(font: TTFont, transform: Transform, verbose: bool = True) -> int:
    """ Apply a transformation to all GPOS positioning values in the font.
    This modifies the GPOS table in place. In particular, the scaleX, scaleY and translateX, translateY values
    in the transform matrix are applied to the GPOS values.
    Returns number of GPOS lookups transformed.
    """
    if 'GPOS' not in font:
        if verbose:
            print("No GPOS table present; nothing to transform.")
        return 0

    scaleX = transform.xx
    scaleY = transform.yy
    translateX = transform.dx
    translateY = transform.dy
    if (transform.xy or transform.yx) and verbose:
        print("Warning: GPOS transform only applies scaleX/scaleY/translateX/translateY; skew/rotation ignored.")

    gpos = font['GPOS'].table

    # Helper to adjust a ValueRecord (may be None)
    def adjust_value_record(vr):
        if vr is None:
            return False
        changed = False
        # Attributes may or may not exist; use hasattr to avoid AttributeError.
        if hasattr(vr, 'XPlacement') and vr.XPlacement is not None:
            new_val = int(round(vr.XPlacement * scaleX + translateX))
            if new_val != vr.XPlacement:
                vr.XPlacement = new_val
                changed = True
        if hasattr(vr, 'YPlacement') and vr.YPlacement is not None:
            new_val = int(round(vr.YPlacement * scaleY + translateY))
            if new_val != vr.YPlacement:
                vr.YPlacement = new_val
                changed = True
        if hasattr(vr, 'XAdvance') and vr.XAdvance is not None:
            new_val = int(round(vr.XAdvance * scaleX))
            if new_val != vr.XAdvance:
                vr.XAdvance = new_val
                changed = True
        if hasattr(vr, 'YAdvance') and vr.YAdvance is not None:
            new_val = int(round(vr.YAdvance * scaleY))
            if new_val != vr.YAdvance:
                vr.YAdvance = new_val
                changed = True
        return changed

    # Helper to adjust an Anchor table (may be None)
    def adjust_anchor(anchor):
        if anchor is None:
            return False
        changed = False
        if hasattr(anchor, 'XCoordinate') and anchor.XCoordinate is not None:
            new_x = int(round(anchor.XCoordinate * scaleX + translateX))
            if new_x != anchor.XCoordinate:
                anchor.XCoordinate = new_x
                changed = True
        if hasattr(anchor, 'YCoordinate') and anchor.YCoordinate is not None:
            new_y = int(round(anchor.YCoordinate * scaleY + translateY))
            if new_y != anchor.YCoordinate:
                anchor.YCoordinate = new_y
                changed = True
        return changed

    lookups = gpos.LookupList.Lookup if gpos.LookupList else []
    transformed_lookups = 0

    for lookup in lookups:
        lookup_changed = False
        ltype = lookup.LookupType
        # Iterate over subtables
        for sub in lookup.SubTable:
            # Type 1: Single Adjustment
            if ltype == 1:
                if sub.Format == 1:
                    if adjust_value_record(sub.Value):
                        lookup_changed = True
                elif sub.Format == 2:
                    for vr in sub.Value:  # list aligned with Coverage order
                        if adjust_value_record(vr):
                            lookup_changed = True
            # Type 2: Pair Adjustment
            elif ltype == 2:
                if sub.Format == 1:
                    for pairSet in sub.PairSet:
                        for pairValueRecord in pairSet.PairValueRecord:
                            if adjust_value_record(pairValueRecord.Value1):
                                lookup_changed = True
                            if adjust_value_record(pairValueRecord.Value2):
                                lookup_changed = True
                elif sub.Format == 2:
                    class1Recs = sub.Class1Record
                    for class1 in class1Recs:
                        for class2 in class1.Class2Record:
                            if adjust_value_record(class2.Value1):
                                lookup_changed = True
                            if adjust_value_record(class2.Value2):
                                lookup_changed = True
            # Type 3: Cursive Attachment
            elif ltype == 3:
                for rec in getattr(sub, 'EntryExitRecord', []) or []:
                    if adjust_anchor(rec.EntryAnchor):
                        lookup_changed = True
                    if adjust_anchor(rec.ExitAnchor):
                        lookup_changed = True
            # Type 4: MarkToBase Attachment
            elif ltype == 4:
                # MarkArray
                if hasattr(sub, 'MarkArray') and sub.MarkArray:
                    for markRec in sub.MarkArray.MarkRecord:
                        if adjust_anchor(markRec.MarkAnchor):
                            lookup_changed = True
                # BaseArray
                if hasattr(sub, 'BaseArray') and sub.BaseArray:
                    for baseRec in sub.BaseArray.BaseRecord:
                        for anchor in baseRec.BaseAnchor:
                            if adjust_anchor(anchor):
                                lookup_changed = True
            # Type 5: MarkToLigature
            elif ltype == 5:
                if hasattr(sub, 'MarkArray') and sub.MarkArray:
                    for markRec in sub.MarkArray.MarkRecord:
                        if adjust_anchor(markRec.MarkAnchor):
                            lookup_changed = True
                if hasattr(sub, 'LigatureArray') and sub.LigatureArray:
                    for ligaAttach in sub.LigatureArray.LigatureAttach:
                        for comp in ligaAttach.ComponentRecord:
                            for anchor in comp.LigatureAnchor:
                                if adjust_anchor(anchor):
                                    lookup_changed = True
            # Type 6: MarkToMark
            elif ltype == 6:
                if hasattr(sub, 'Mark1Array') and sub.Mark1Array:
                    for markRec in sub.Mark1Array.MarkRecord:
                        if adjust_anchor(markRec.MarkAnchor):
                            lookup_changed = True
                if hasattr(sub, 'Mark2Array') and sub.Mark2Array:
                    for mark2Rec in sub.Mark2Array.Mark2Record:
                        for anchor in mark2Rec.Mark2Anchor:
                            if adjust_anchor(anchor):
                                lookup_changed = True
            # Types 7 & 8 (Context / Chained Context) contain nested lookups referenced by LookupRecords.
            # We don't transform here directly; their referenced lookups are already processed in this loop.
            else:
                continue
        if lookup_changed:
            transformed_lookups += 1
    if verbose:
        print(f"Transformed {transformed_lookups} GPOS lookups (types 1-6).")
    return transformed_lookups


def extract_font(input_path, output_path, axis_settings=None, transform=None, cjk_mode_transform: bool = False,
                 skip_decomposition=False):
    """Extract a static instance from a variable font, decompose composite glyphs,
    and apply an optional transformation matrix.
    """
    print(f"Loading font: {input_path}...")
    font = TTFont(input_path)
    print("Font loaded.")

    # If variable font and axis settings provided, instantiate first.
    if 'fvar' in font:
        if not axis_settings:
            raise ValueError("Font is variable but no axis settings provided.")

        print(f"Instantiating variable font with axes: {axis_settings}...")
        font = instantiateVariableFont(font, axis_settings, inplace=False)
        print("Removing overlaps...")
        from fontTools.ttLib.removeOverlaps import removeOverlaps
        removeOverlaps(font)
    else:
        if axis_settings:
            print("Warning: Axis settings provided but font is not variable; ignoring axis settings.")

    if skip_decomposition:
        print("Skipping decomposition of composite glyphs.")
    else:
        print("Decomposing composite glyphs...")
        decomposed = decompose_composites(font, verbose=True)
        if decomposed == 0:
            print("Note: No composite glyphs were found or decomposed.")

    if transform is not None:
        print(f"Applying transformation {transform}...")
        apply_glyph_transform(font, transform, cjk_mode_transform, verbose=True)
        if not cjk_mode_transform:
            apply_gpos_transform(font, transform, verbose=True)

    print(f"Saving font to {output_path}")
    font.save(output_path)
    print("Done.")


def main():
    parser = argparse.ArgumentParser(
        prog='Extract, decompose and transform a variable font',
        description='Extract a static instance from a variable font, decompose composite glyphs, and apply an optional transformation matrix.',
        epilog='Example: python extract_font.py InterVariable.ttf -a "wght=320,opsz=20" -t "0.47,0,0,0.47,0,16" Inter-[[wght]]-[[opsz]].ttf')

    parser.add_argument('input', help='Input font file path (variable or static).')
    parser.add_argument('-a', '--axis', type=str, default=None,
                        help='Axis settings for variable font, e.g. "wght=400,wdth=100".')
    parser.add_argument('-x', '--skip-decomposition', action='store_true',
                        help='When specified, skip the decomposition step (default: false).'),
    parser.add_argument('-t', '--transform', type=str, default=None,
                        help='Transformation matrix as "a,b,c,d,e,f" for 2x3 affine transform.')
    parser.add_argument('-c', '--cjk-mode-transform', action='store_true',
                        help='When specified, only transform glyphs with 1000upm advance width and do not adjust GPOS (useful for CJK fonts).'
                        )
    parser.add_argument('output', help='Output font file path. Defaults to input-decomposed.ttf if not provided.')

    args = parser.parse_args()
    input_path = args.input
    axis_settings = parse_axis_settings(args.axis)
    transformation = parse_transformation(args.transform)
    out_path = args.output

    # Replace placeholder in out_path with value in axis settings if present
    if out_path and '[[' in out_path and ']]' in out_path:
        try:
            out_path = out_path.replace('[[', '{').replace(']]', '}')
            out_path = out_path.format(**{k: int(v) if v.is_integer() else v for k, v in axis_settings.items()})
        except KeyError as e:
            print(f"Error: Output path placeholder {e} not found in axis settings.")
            sys.exit(1)

    if not input_path:
        print("No input font supplied.")
        sys.exit(1)

    if not out_path:
        print("No output font supplied.")
        sys.exit(1)

    if not os.path.isfile(input_path):
        print(f"Input font not found: {input_path}")
        sys.exit(1)

    extract_font(input_path, out_path, axis_settings, transformation, args.cjk_mode_transform, args.skip_decomposition)


if __name__ == "__main__":
    main()
