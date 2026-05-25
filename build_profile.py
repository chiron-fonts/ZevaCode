import argparse
import copy
import os
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from time import monotonic

import yaml

from extract_font import parse_axis_settings


REPO_ROOT = Path(__file__).resolve().parent
VARIABLE_MERGE_SCRIPT = REPO_ROOT / "merge_vf_cjk.py"
STATIC_MERGE_SCRIPT = REPO_ROOT / "merge_static_cjk.py"

PROFILE_TYPE_VARIABLE = "variable"
PROFILE_TYPE_STATIC = "static"
CJK_CACHE_DIR_ENV = "ZEVCODE_CJK_CACHE_DIR"

DISPLAY_WEIGHT_NAMES = {
    "thin": "Thin",
    "extralight": "ExtraLight",
    "light": "Light",
    "regular": "Regular",
    "medium": "Medium",
    "semilight": "SemiLight",
    "semibold": "SemiBold",
    "bold": "Bold",
    "extrabold": "ExtraBold",
    "black": "Black",
}

KNOWN_WEIGHT_SUFFIXES = sorted(DISPLAY_WEIGHT_NAMES.values(), key=len, reverse=True)


def format_elapsed(elapsed_seconds: float) -> str:
    total_seconds = max(0, int(round(elapsed_seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}h{minutes:02}m{seconds:02}s"


def log_status(message: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] {message}")


def is_skipped(mapping: dict | None) -> bool:
    return bool(mapping and mapping.get("skip"))


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Profile {path} must contain a top-level mapping.")
    return data


def require_keys(mapping: dict, keys: tuple[str, ...], label: str) -> None:
    missing = [key for key in keys if key not in mapping]
    if missing:
        raise ValueError(f"{label} is missing required keys: {', '.join(missing)}")


def repo_path(value: str | None, label: str, *, must_exist: bool = True) -> Path | None:
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if must_exist and not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    return path


def repo_output_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def command_output_label(command: list[str]) -> str:
    if "--out" in command:
        return command[command.index("--out") + 1]
    return shlex.join(command)


def merge_variant_config(file_spec: dict, variant_name: str, variant_spec: dict) -> dict:
    merged = copy.deepcopy(variant_spec)
    italic_override = merged.pop("italic", None)
    if file_spec.get("italic") and italic_override:
        if not isinstance(italic_override, dict):
            raise ValueError(f"variants.{variant_name}.italic must be a mapping.")
        merged.update(italic_override)
    return merged


def build_output_family_name(family_code: str, variant_name: str) -> str:
    return f"ZevCodeTC-{family_code}-{variant_name}"


def build_output_file_name(family_name: str, italic: bool) -> str:
    return f"{family_name}{'-Italic' if italic else ''}.ttf"


def build_report_file_name(family_name: str, italic: bool) -> str:
    return f"{family_name}{'-Italic' if italic else ''}-merge-report.json"


def build_variable_merge_command(
    profile_path: Path,
    profile: dict,
    file_spec: dict,
    variant_name: str,
    variant_spec: dict,
    blocks_override: Path | None,
    output_dir: Path,
    report_dir: Path,
) -> list[str]:
    require_keys(profile, ("cjk_font", "blocks", "files", "variants"), profile_path.name)
    require_keys(file_spec, ("name", "target", "family_code"), f"file {file_spec!r}")

    family_code = file_spec["family_code"]
    merged_variant = merge_variant_config(file_spec, variant_name, variant_spec)
    output_family_name = build_output_family_name(family_code, variant_name)
    target_path = repo_path(file_spec["target"], f"Target for file {file_spec['name']}")
    cjk_path = repo_path(profile["cjk_font"], "CJK source font")
    blocks_path = blocks_override or repo_path(profile["blocks"], "Unicode block list")

    if target_path is None or cjk_path is None or blocks_path is None:
        raise ValueError("Target, CJK font, and blocks path must be defined.")

    output_path = output_dir / build_output_file_name(output_family_name, bool(file_spec.get("italic")))
    report_path = report_dir / build_report_file_name(output_family_name, bool(file_spec.get("italic")))

    command = [
        sys.executable,
        repo_output_path(VARIABLE_MERGE_SCRIPT),
        "--target",
        repo_output_path(target_path),
        "--cjk",
        repo_output_path(cjk_path),
        "--blocks",
        repo_output_path(blocks_path),
        "--font-name",
        output_family_name,
        "--out",
        repo_output_path(output_path),
        "--report",
        repo_output_path(report_path),
    ]

    if "master_config" in merged_variant:
        master_config_path = repo_path(merged_variant["master_config"], f"master_config for variant {variant_name}")
        if master_config_path is None:
            raise ValueError(f"Variant {variant_name} has an empty master_config.")
        command.extend(["--master-config", repo_output_path(master_config_path)])
    else:
        for required_key in ("cjk_low", "cjk_high"):
            if required_key not in merged_variant:
                raise ValueError(f"Variant {variant_name} must define {required_key} or master_config.")
        command.extend(["--cjk-low", merged_variant["cjk_low"]])
        command.extend(["--cjk-high", merged_variant["cjk_high"]])
        if merged_variant.get("cjk_default"):
            command.extend(["--cjk-default", merged_variant["cjk_default"]])
        if merged_variant.get("cjk_transform"):
            command.extend(["--cjk-transform", merged_variant["cjk_transform"]])

    return command


def normalize_static_weight_key(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def format_axis_value(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return format(value, "g")


def merge_axis_settings_strings(*settings: str | None) -> str:
    merged: dict[str, float] = {}
    for setting in settings:
        if not setting:
            continue
        merged.update(parse_axis_settings(setting))
    if not merged:
        raise ValueError("Expected at least one axis setting.")
    return ",".join(f"{axis_tag}={format_axis_value(value)}" for axis_tag, value in merged.items())


def parse_static_style_suffix(suffix: str) -> tuple[str, bool]:
    italic = suffix.endswith("Italic")
    weight_part = suffix[:-6] if italic else suffix
    if not weight_part:
        return "regular", italic

    normalized = normalize_static_weight_key(weight_part)
    if normalized not in DISPLAY_WEIGHT_NAMES:
        raise ValueError(f"Unsupported static weight token {weight_part!r}.")
    return normalized, italic


def infer_static_file_metadata(target_path: Path, source_filename_prefix: str | None) -> dict:
    stem = target_path.stem
    suffix = stem
    if source_filename_prefix and stem.startswith(f"{source_filename_prefix}-"):
        suffix = stem[len(source_filename_prefix) + 1 :]
    elif "-" in stem:
        suffix = stem.split("-", 1)[1]

    weight_key, italic = parse_static_style_suffix(suffix)
    weight_display = DISPLAY_WEIGHT_NAMES[weight_key]
    if italic and weight_key == "regular":
        output_suffix = "Italic"
    elif italic:
        output_suffix = f"{weight_display}Italic"
    else:
        output_suffix = weight_display

    return {
        "weight_key": weight_key,
        "italic": italic,
        "output_suffix": output_suffix,
        "output_format": target_path.suffix.lower().lstrip("."),
    }


def normalize_static_file_spec(entry, family_spec: dict) -> dict:
    if isinstance(entry, str):
        return {"target": entry}
    if not isinstance(entry, dict):
        raise ValueError(f"Static file entry must be a string path or mapping, got {entry!r}")
    return dict(entry)


def static_family_label(family_spec: dict) -> str:
    return str(family_spec.get("directory_name") or "<unnamed static family>")


def static_file_label(file_spec: dict, family_label: str) -> str:
    return str(file_spec.get("name") or file_spec.get("glob") or file_spec.get("target") or f"<unnamed static file in {family_label}>")


def variable_file_label(file_spec: dict) -> str:
    return str(file_spec.get("name") or file_spec.get("target") or "<unnamed variable file>")


def render_static_template(template: str, *, label: str, variant_name: str, weight_name: str) -> str:
    if not isinstance(template, str) or not template:
        raise ValueError(f"{label} must be a non-empty string.")
    try:
        rendered = template.format(variant=variant_name, weight=weight_name)
    except KeyError as exc:
        missing = exc.args[0]
        raise ValueError(
            f"{label} uses unsupported placeholder {{{missing}}}; allowed placeholders are {{variant}} and {{weight}}."
        ) from exc
    if not rendered:
        raise ValueError(f"{label} must not resolve to an empty string.")
    return rendered


def skip_matches_selected_static_files(
    profile_path: Path,
    family_label: str,
    file_spec: dict,
    selected_files: set[str],
) -> bool:
    if not selected_files:
        return True

    selectable_names: set[str] = set()
    if file_spec.get("name"):
        selectable_names.add(str(file_spec["name"]))

    if "glob" in file_spec:
        glob_path = repo_path(file_spec["glob"], f"glob in family {family_label}", must_exist=False)
        if glob_path is not None:
            selectable_names.update(path.stem for path in glob_path.parent.glob(glob_path.name) if path.is_file())
    else:
        target_path = repo_path(file_spec.get("target"), f"target in family {family_label}", must_exist=False)
        if target_path is not None:
            selectable_names.add(target_path.stem)

    return bool(selectable_names & selected_files)


def expand_static_family_files(
    profile_path: Path,
    family_spec: dict,
    selected_files: set[str],
) -> tuple[list[dict], int]:
    require_keys(family_spec, ("directory_name", "target_filename_prefix", "files"), f"family in {profile_path.name}")
    family_label = static_family_label(family_spec)
    files = family_spec["files"]
    if not isinstance(files, list) or not files:
        raise ValueError(f"family {family_label} in {profile_path.name} must define a non-empty files list.")

    expanded_specs = []
    skipped_selected_entries = 0
    family_source_filename_prefix = family_spec.get("source_filename_prefix")
    for entry in files:
        file_spec = normalize_static_file_spec(entry, family_spec)
        if is_skipped(file_spec):
            if skip_matches_selected_static_files(profile_path, family_label, file_spec, selected_files):
                skipped_selected_entries += 1
                log_status(f"Skipping static file entry {static_file_label(file_spec, family_label)} due to skip: true")
            continue
        targets: list[Path] = []
        if "glob" in file_spec:
            glob_path = repo_path(file_spec["glob"], f"glob in family {family_label}", must_exist=False)
            if glob_path is None:
                raise ValueError(f"family {family_label} defines an empty glob.")
            targets = sorted(path for path in glob_path.parent.glob(glob_path.name) if path.is_file())
            if not targets:
                raise FileNotFoundError(f"No files matched glob {file_spec['glob']!r} in family {family_label}.")
        else:
            target_path = repo_path(file_spec.get("target"), f"target in family {family_label}")
            if target_path is None:
                raise ValueError(f"family {family_label} includes a file entry without target/glob.")
            targets = [target_path]

        for target_path in targets:
            metadata = infer_static_file_metadata(
                target_path,
                file_spec.get("source_filename_prefix") or family_source_filename_prefix,
            )
            if file_spec.get("weight"):
                metadata["weight_key"] = normalize_static_weight_key(file_spec["weight"])
            if "italic" in file_spec:
                metadata["italic"] = bool(file_spec["italic"])
            if "output_suffix" in file_spec:
                metadata["output_suffix"] = file_spec["output_suffix"]
            if "output_format" in file_spec:
                metadata["output_format"] = str(file_spec["output_format"]).lstrip(".")

            if metadata["weight_key"] not in DISPLAY_WEIGHT_NAMES:
                raise ValueError(
                    f"Static file {target_path} resolved unknown weight key {metadata['weight_key']!r}."
                )

            selector_name = file_spec.get("name") or target_path.stem
            expanded_specs.append(
                {
                    "directory_name": family_spec["directory_name"],
                    "target_filename_prefix_template": family_spec["target_filename_prefix"],
                    "selector_name": selector_name,
                    "target": target_path,
                    "weight_key": metadata["weight_key"],
                    "italic": metadata["italic"],
                    "output_suffix": metadata["output_suffix"],
                    "output_format": metadata["output_format"],
                    "cjk_axis_override": file_spec.get("cjk_axis_override"),
                    "source_family_name": file_spec.get("source_family_name") or family_spec.get("source_family_name"),
                    "target_family_name": file_spec.get("target_family_name") or family_spec.get("target_family_name"),
                    "source_postscript_name": file_spec.get("source_postscript_name")
                    or family_spec.get("source_postscript_name"),
                    "target_postscript_name": file_spec.get("target_postscript_name")
                    or family_spec.get("target_postscript_name"),
                    "ttf_companion": bool(file_spec.get("ttf_companion", family_spec.get("ttf_companion", False))),
                }
            )
    return expanded_specs, skipped_selected_entries


def static_variant_axis_string(profile: dict, file_spec: dict, variant_name: str, variant_spec: dict) -> str:
    merged_variant = merge_variant_config(file_spec, variant_name, variant_spec)
    variant_axis = merged_variant.get("cjk_axis")
    if variant_axis is None and "cjk_low" in merged_variant and "cjk_high" not in merged_variant:
        variant_axis = merged_variant["cjk_low"]
    if variant_axis is None:
        raise ValueError(f"Static variant {variant_name} must define cjk_axis.")

    weight_specs = profile.get("weights", {})
    weight_override = None
    if weight_specs:
        if not isinstance(weight_specs, dict):
            raise ValueError("weights must be a mapping.")
        weight_spec = weight_specs.get(file_spec["weight_key"], {})
        if weight_spec and not isinstance(weight_spec, dict):
            raise ValueError(f"weights.{file_spec['weight_key']} must be a mapping.")
        weight_override = weight_spec.get("cjk_axis_override")

    return merge_axis_settings_strings(variant_axis, weight_override, file_spec.get("cjk_axis_override"))


def build_static_merge_command(
    profile_path: Path,
    profile: dict,
    file_spec: dict,
    variant_name: str,
    variant_spec: dict,
    blocks_override: Path | None,
    output_dir: Path,
    report_dir: Path,
) -> list[str]:
    require_keys(profile, ("cjk_font", "blocks", "families", "variants"), profile_path.name)

    merged_variant = merge_variant_config(file_spec, variant_name, variant_spec)
    cjk_axis = static_variant_axis_string(profile, file_spec, variant_name, variant_spec)
    weight_name = DISPLAY_WEIGHT_NAMES[file_spec["weight_key"]]
    target_family_name = None
    if file_spec.get("target_family_name") is not None:
        target_family_name = render_static_template(
            file_spec["target_family_name"],
            label=f"target_family_name for {file_spec['target']}",
            variant_name=variant_name,
            weight_name=weight_name,
        )
    target_postscript_name = None
    if file_spec.get("target_postscript_name") is not None:
        target_postscript_name = render_static_template(
            file_spec["target_postscript_name"],
            label=f"target_postscript_name for {file_spec['target']}",
            variant_name=variant_name,
            weight_name=weight_name,
        )
    target_filename_prefix = render_static_template(
        file_spec["target_filename_prefix_template"],
        label=f"target_filename_prefix for {file_spec['target']}",
        variant_name=variant_name,
        weight_name=weight_name,
    )
    source_family_name = file_spec.get("source_family_name")
    source_postscript_name = file_spec.get("source_postscript_name")
    if bool(source_family_name) != bool(target_family_name):
        raise ValueError(f"Static build for {file_spec['target']} must define source_family_name and target_family_name together.")
    if bool(source_postscript_name) != bool(target_postscript_name):
        raise ValueError(
            f"Static build for {file_spec['target']} must define source_postscript_name and target_postscript_name together."
        )

    target_path = file_spec["target"]
    cjk_path = repo_path(profile["cjk_font"], "CJK source font")
    blocks_path = blocks_override or repo_path(profile["blocks"], "Unicode block list")
    if cjk_path is None or blocks_path is None:
        raise ValueError("Static builds require CJK font and blocks path.")

    family_dir = output_dir / "static" / file_spec["directory_name"]
    report_family_dir = report_dir / "static" / file_spec["directory_name"]
    output_path = family_dir / f"{target_filename_prefix}-{file_spec['output_suffix']}.{file_spec['output_format']}"
    report_path = report_family_dir / f"{target_filename_prefix}-{file_spec['output_suffix']}-merge-report.json"
    companion_ttf_path = None
    if file_spec.get("ttf_companion") and file_spec["output_format"] == "otf":
        companion_ttf_path = family_dir / f"{target_filename_prefix}-{file_spec['output_suffix']}.ttf"

    command = [
        sys.executable,
        repo_output_path(STATIC_MERGE_SCRIPT),
        "--target",
        repo_output_path(target_path),
        "--cjk",
        repo_output_path(cjk_path),
        "--blocks",
        repo_output_path(blocks_path),
        "--cjk-axis",
        cjk_axis,
        "--out",
        repo_output_path(output_path),
        "--report",
        repo_output_path(report_path),
    ]
    if companion_ttf_path is not None:
        command.extend(["--ttf-out", repo_output_path(companion_ttf_path)])
    if source_family_name:
        command.extend(["--source-family-name", source_family_name, "--target-family-name", target_family_name])
    if source_postscript_name:
        command.extend(
            ["--source-postscript-name", source_postscript_name, "--target-postscript-name", target_postscript_name]
        )
    if merged_variant.get("cjk_transform"):
        command.extend(["--cjk-transform", merged_variant["cjk_transform"]])
    return command


def build_variable_commands(
    profile_path: Path,
    profile: dict,
    args: argparse.Namespace,
    blocks_override: Path | None,
    output_dir: Path,
    report_dir: Path,
) -> tuple[list[list[str]], bool]:
    require_keys(profile, ("cjk_font", "blocks", "files", "variants"), profile_path.name)
    files = profile["files"]
    variants = profile["variants"]
    if not isinstance(files, list) or not files:
        raise ValueError(f"{profile_path.name} must define a non-empty files list.")
    if not isinstance(variants, dict) or not variants:
        raise ValueError(f"{profile_path.name} must define a non-empty variants mapping.")

    selected_files = set(args.file or [])
    selected_codes = set(args.code or [])
    selected_variants = set(args.variant or [])

    commands = []
    skipped_selected_entries = 0
    selected_active_entries = 0
    for file_spec in files:
        if is_skipped(file_spec):
            if (
                (not selected_files or file_spec.get("name") in selected_files)
                and (not selected_codes or file_spec.get("family_code") in selected_codes)
            ):
                skipped_selected_entries += 1
                log_status(f"Skipping variable file entry {variable_file_label(file_spec)} due to skip: true")
            continue
        file_name = file_spec.get("name")
        if not file_name:
            raise ValueError(f"Each file entry in {profile_path.name} must define a name.")
        family_code = file_spec.get("family_code")
        if not family_code:
            raise ValueError(f"Each file entry in {profile_path.name} must define a family_code.")
        if selected_files and file_name not in selected_files:
            continue
        if selected_codes and family_code not in selected_codes:
            continue
        selected_active_entries += 1
        for variant_name, variant_spec in variants.items():
            if selected_variants and variant_name not in selected_variants:
                continue
            if not isinstance(variant_spec, dict):
                raise ValueError(f"variants.{variant_name} must be a mapping.")
            commands.append(
                build_variable_merge_command(
                    profile_path=profile_path,
                    profile=profile,
                    file_spec=file_spec,
                    variant_name=variant_name,
                    variant_spec=variant_spec,
                    blocks_override=blocks_override,
                    output_dir=output_dir,
                    report_dir=report_dir,
                )
            )
    return commands, selected_active_entries == 0 and skipped_selected_entries > 0


def build_static_commands(
    profile_path: Path,
    profile: dict,
    args: argparse.Namespace,
    blocks_override: Path | None,
    output_dir: Path,
    report_dir: Path,
) -> tuple[list[list[str]], bool]:
    require_keys(profile, ("cjk_font", "blocks", "families", "variants"), profile_path.name)
    families = profile["families"]
    variants = profile["variants"]
    if not isinstance(families, list) or not families:
        raise ValueError(f"{profile_path.name} must define a non-empty families list.")
    if not isinstance(variants, dict) or not variants:
        raise ValueError(f"{profile_path.name} must define a non-empty variants mapping.")

    selected_files = set(args.file or [])
    selected_families = set(args.family or [])
    selected_variants = set(args.variant or [])

    commands = []
    skipped_selected_entries = 0
    selected_active_entries = 0
    for family_spec in families:
        family_label = static_family_label(family_spec)
        family_name = family_spec.get("directory_name")
        family_selected = not selected_families or family_name in selected_families
        if is_skipped(family_spec):
            if family_selected:
                skipped_selected_entries += 1
                log_status(f"Skipping static family {family_label} due to skip: true")
            continue
        directory_name = family_spec.get("directory_name")
        if not directory_name:
            raise ValueError(f"Each static family in {profile_path.name} must define a directory_name.")
        if selected_families and directory_name not in selected_families:
            continue
        expanded_files, skipped_file_entries = expand_static_family_files(profile_path, family_spec, selected_files)
        skipped_selected_entries += skipped_file_entries
        for file_spec in expanded_files:
            if selected_files and file_spec["selector_name"] not in selected_files and file_spec["target"].stem not in selected_files:
                continue
            selected_active_entries += 1
            for variant_name, variant_spec in variants.items():
                if selected_variants and variant_name not in selected_variants:
                    continue
                if not isinstance(variant_spec, dict):
                    raise ValueError(f"variants.{variant_name} must be a mapping.")
                commands.append(
                    build_static_merge_command(
                        profile_path=profile_path,
                        profile=profile,
                        file_spec=file_spec,
                        variant_name=variant_name,
                        variant_spec=variant_spec,
                        blocks_override=blocks_override,
                        output_dir=output_dir,
                        report_dir=report_dir,
                    )
                )
    return commands, selected_active_entries == 0 and skipped_selected_entries > 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="build_profile.py",
        description="Build one or more font variants from a family profile.",
    )
    parser.add_argument("profile", help="YAML family profile path.")
    parser.add_argument("--variant", action="append", help="Variant name to build. Repeat to select multiple variants.")
    parser.add_argument("--file", action="append", help="File entry name to build. Repeat to select multiple files.")
    parser.add_argument(
        "--family",
        action="append",
        help="Static directory_name to build. Repeat to select multiple families.",
    )
    parser.add_argument("--code", action="append", help="Variable family code to build. Repeat to select multiple codes.")
    parser.add_argument("--blocks-override", help="Override the profile's blocks file for smoke tests or focused runs.")
    parser.add_argument("--output-dir", default="out", help="Directory for built font files. Default: out")
    parser.add_argument("--report-dir", default="out", help="Directory for JSON reports. Default: out")
    parser.add_argument("--dry-run", action="store_true", help="Print resolved merge commands without executing them.")
    return parser.parse_args()


def main() -> None:
    build_start = monotonic()
    args = parse_args()

    profile_path = repo_path(args.profile, "Profile file")
    if profile_path is None:
        raise ValueError("Profile path is required.")
    profile = load_yaml(profile_path)
    if is_skipped(profile):
        log_status(f"Skipping profile {profile_path.name} due to skip: true")
        return
    profile_type = profile.get("type", PROFILE_TYPE_VARIABLE)

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = REPO_ROOT / output_dir
    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = REPO_ROOT / report_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    blocks_override = repo_path(args.blocks_override, "blocks override") if args.blocks_override else None

    log_status(f"Start building profile {profile_path.name}...")
    if profile_type == PROFILE_TYPE_VARIABLE:
        commands, skip_only = build_variable_commands(profile_path, profile, args, blocks_override, output_dir, report_dir)
    elif profile_type == PROFILE_TYPE_STATIC:
        commands, skip_only = build_static_commands(profile_path, profile, args, blocks_override, output_dir, report_dir)
    else:
        raise ValueError(f"Unsupported profile type {profile_type!r} in {profile_path.name}.")

    if not commands:
        if skip_only:
            log_status(
                f"No builds generated for {profile_path.name} because all selected entries are marked skip: true"
            )
            log_status(f"Finished building profile {profile_path.name} ({format_elapsed(monotonic() - build_start)} elapsed)")
            return
        raise ValueError("No builds selected. Check --variant/--file/--family/--code filters.")

    subprocess_env = os.environ.copy()
    cache_root = subprocess_env.get(CJK_CACHE_DIR_ENV)
    temp_cache_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        if profile_type == PROFILE_TYPE_STATIC and not args.dry_run and not cache_root:
            temp_cache_dir = tempfile.TemporaryDirectory(prefix="zevcode-cjk-cache-")
            subprocess_env[CJK_CACHE_DIR_ENV] = temp_cache_dir.name
            log_status(f"Using temporary static CJK cache: {temp_cache_dir.name}")

        for index, command in enumerate(commands, start=1):
            command_start = monotonic()
            command_label = command_output_label(command)
            log_status(f"Start building font {index}/{len(commands)}: {command_label}")
            print(shlex.join(command))
            if not args.dry_run:
                subprocess.run(command, cwd=REPO_ROOT, check=True, env=subprocess_env)
            log_status(f"Finished building font {index}/{len(commands)}: {command_label} ({format_elapsed(monotonic() - command_start)} elapsed)")
    finally:
        if temp_cache_dir is not None:
            temp_cache_dir.cleanup()
    log_status(f"Finished building profile {profile_path.name} ({format_elapsed(monotonic() - build_start)} elapsed)")


if __name__ == "__main__":
    main()
