import argparse
import json
from collections import Counter
from pathlib import Path

from fontTools.ttLib import TTFont


def format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def invert_avar(value: float, segments: dict[float, float] | None) -> float:
    if not segments:
        return value
    items = sorted(segments.items())
    if value <= items[0][1]:
        return items[0][0]
    if value >= items[-1][1]:
        return items[-1][0]

    for (from_a, to_a), (from_b, to_b) in zip(items, items[1:]):
        low_to, high_to = sorted((to_a, to_b))
        if low_to <= value <= high_to:
            if to_a == to_b:
                return from_a
            ratio = (value - to_a) / (to_b - to_a)
            return from_a + ratio * (from_b - from_a)
    return value


def normalized_to_user(normalized_value: float, minimum: float, default: float, maximum: float) -> float:
    if normalized_value >= 0:
        return default + normalized_value * (maximum - default)
    return default + normalized_value * (default - minimum)


def axis_user_value(axis, normalized_value: float, avar_segments: dict[str, dict[float, float]]) -> float:
    pre_avar = invert_avar(normalized_value, avar_segments.get(axis.axisTag))
    return normalized_to_user(pre_avar, axis.minValue, axis.defaultValue, axis.maxValue)


def collect_peak_masters(font: TTFont) -> tuple[list[dict], list[dict]]:
    axes = list(font["fvar"].axes)
    axis_tags = [axis.axisTag for axis in axes]
    avar_segments = font["avar"].segments if "avar" in font else {}

    peak_counter = Counter()
    region_counter = Counter()

    for variations in font["gvar"].variations.values():
        for variation in variations:
            peak = []
            region = []
            for axis_tag in axis_tags:
                support = variation.axes.get(axis_tag, (0.0, 0.0, 0.0))
                region.append((axis_tag, tuple(float(v) for v in support)))
                if support[1] != 0:
                    peak.append((axis_tag, float(support[1])))
            if peak:
                peak_counter[tuple(peak)] += 1
            region_counter[tuple(region)] += 1

    masters = []
    default_master = {
        "name": "default",
        "normalized": {axis.axisTag: 0.0 for axis in axes},
        "user": {axis.axisTag: axis.defaultValue for axis in axes},
        "region_count": 0,
    }
    masters.append(default_master)

    for index, (peak, count) in enumerate(sorted(peak_counter.items(), key=lambda item: (len(item[0]), item[0]))):
        normalized = {axis.axisTag: 0.0 for axis in axes}
        for axis_tag, value in peak:
            normalized[axis_tag] = value
        user = {
            axis.axisTag: axis_user_value(axis, normalized[axis.axisTag], avar_segments)
            for axis in axes
        }
        masters.append(
            {
                "name": f"master-{index + 1}",
                "normalized": normalized,
                "user": user,
                "region_count": count,
            }
        )

    regions = []
    for region, count in sorted(region_counter.items(), key=lambda item: (-item[1], item[0])):
        regions.append(
            {
                "support": {axis_tag: list(values) for axis_tag, values in region},
                "count": count,
            }
        )
    return masters, regions


def print_axes(font: TTFont) -> None:
    print("Axes:")
    for axis in font["fvar"].axes:
        print(
            f"  {axis.axisTag}: min={format_number(axis.minValue)} "
            f"default={format_number(axis.defaultValue)} max={format_number(axis.maxValue)}"
        )


def print_masters(masters: list[dict], axis_tags: list[str]) -> None:
    print("\nInferred masters:")
    for master in masters:
        user_text = ", ".join(f"{tag}={format_number(master['user'][tag])}" for tag in axis_tags)
        normalized_text = ", ".join(f"{tag}={format_number(master['normalized'][tag])}" for tag in axis_tags)
        suffix = ""
        if master["region_count"]:
            suffix = f"  (peaks seen in {master['region_count']} tuples)"
        print(f"  {master['name']}: {user_text}    [{normalized_text}]{suffix}")


def print_regions(regions: list[dict], axis_tags: list[str]) -> None:
    print("\nUnique gvar support regions:")
    for region in regions:
        support_text = ", ".join(
            f"{tag}=({format_number(region['support'][tag][0])},"
            f"{format_number(region['support'][tag][1])},"
            f"{format_number(region['support'][tag][2])})"
            for tag in axis_tags
        )
        print(f"  {region['count']}: {support_text}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="inspect_var_masters.py",
        description="Infer effective variable-font master locations from gvar support peaks.",
    )
    parser.add_argument("font", help="Variable font file to inspect.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text output.")
    parser.add_argument("--show-regions", action="store_true", help="Include unique gvar support regions in the output.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    font_path = Path(args.font)
    if not font_path.is_file():
        raise FileNotFoundError(f"Font not found: {font_path}")

    font = TTFont(font_path)
    if "fvar" not in font or "gvar" not in font:
        raise ValueError(f"{font_path} is not a TrueType variable font with both fvar and gvar tables.")

    masters, regions = collect_peak_masters(font)
    axis_tags = [axis.axisTag for axis in font["fvar"].axes]

    if args.json:
        payload = {
            "font": str(font_path),
            "axes": [
                {
                    "tag": axis.axisTag,
                    "minimum": axis.minValue,
                    "default": axis.defaultValue,
                    "maximum": axis.maxValue,
                }
                for axis in font["fvar"].axes
            ],
            "masters": masters,
        }
        if args.show_regions:
            payload["regions"] = regions
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    print(f"Font: {font_path}")
    print_axes(font)
    print_masters(masters, axis_tags)
    if args.show_regions:
        print_regions(regions, axis_tags)


if __name__ == "__main__":
    main()
