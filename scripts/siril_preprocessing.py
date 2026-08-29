#!/usr/bin/env python3
"""Resolve SharpCap calibration settings and build Siril preprocessing scripts."""

from __future__ import annotations

import math
import re
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path


DISABLED_VALUES = {"", "none", "off", "disabled", "disable"}
HOT_ONLY_VALUES = {"hot pixel removal", "hot pixel removal only"}
HOT_COLD_VALUES = {
    "hot and cold pixel removal",
    "hot and cold pixel removal only",
    "hot/cold pixel removal",
    "hot/cold pixel removal only",
}

# Siril 1.4.1 requires both cosmetic sigma arguments to be positive. Zero
# therefore means a zero-sigma threshold rather than "disabled" and can mark
# millions of ordinary pixels. A practically unreachable positive threshold
# disables the unwanted side while retaining the requested correction.
DISABLED_COSMETIC_SIGMA = 1_000_000.0


@dataclass(frozen=True)
class PreprocessingPlan:
    enabled: bool
    settings_file: str | None
    dark_enabled: bool
    dark_file: str | None
    dark_source: str
    flat_enabled: bool
    flat_file: str | None
    flat_source: str
    hot_pixel_enabled: bool
    hot_pixel_source: str
    cold_pixel_enabled: bool
    cold_pixel_source: str
    hot_pixel_sigma: float
    cold_pixel_sigma: float
    sharpcap_hot_pixel_sensitivity: float | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, object] | None) -> "PreprocessingPlan":
        if not value:
            return disabled_plan()
        return cls(
            enabled=bool(value.get("enabled", True)),
            settings_file=str(value["settings_file"]) if value.get("settings_file") else None,
            dark_enabled=bool(value.get("dark_enabled", False)),
            dark_file=str(value["dark_file"]) if value.get("dark_file") else None,
            dark_source=str(value.get("dark_source", "default")),
            flat_enabled=bool(value.get("flat_enabled", False)),
            flat_file=str(value["flat_file"]) if value.get("flat_file") else None,
            flat_source=str(value.get("flat_source", "default")),
            hot_pixel_enabled=bool(value.get("hot_pixel_enabled", False)),
            hot_pixel_source=str(value.get("hot_pixel_source", "default")),
            cold_pixel_enabled=bool(value.get("cold_pixel_enabled", False)),
            cold_pixel_source=str(value.get("cold_pixel_source", "default")),
            hot_pixel_sigma=float(value.get("hot_pixel_sigma", 3.0)),
            cold_pixel_sigma=float(value.get("cold_pixel_sigma", 3.0)),
            sharpcap_hot_pixel_sensitivity=(
                float(value["sharpcap_hot_pixel_sensitivity"])
                if value.get("sharpcap_hot_pixel_sensitivity") is not None
                else None
            ),
        )


def disabled_plan(settings_file: Path | None = None, hot_sigma: float = 3.0, cold_sigma: float = 3.0) -> PreprocessingPlan:
    return PreprocessingPlan(
        enabled=False,
        settings_file=str(settings_file) if settings_file else None,
        dark_enabled=False,
        dark_file=None,
        dark_source="disabled",
        flat_enabled=False,
        flat_file=None,
        flat_source="disabled",
        hot_pixel_enabled=False,
        hot_pixel_source="disabled",
        cold_pixel_enabled=False,
        cold_pixel_source="disabled",
        hot_pixel_sigma=hot_sigma,
        cold_pixel_sigma=cold_sigma,
        sharpcap_hot_pixel_sensitivity=None,
    )


def setting(settings: dict[str, str], name: str) -> str:
    expected = name.casefold()
    for key, value in settings.items():
        if key.casefold() == expected:
            return value.strip()
    return ""


def parse_optional_float(value: str) -> float | None:
    try:
        number = float(value.strip())
    except (AttributeError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def recorded_basename(value: str) -> str:
    return re.split(r"[\\/]", value.strip().strip('"'))[-1]


def resolve_recorded_file(value: str, settings_file: Path | None, session_root: Path) -> Path | None:
    text = value.strip().strip('"')
    if not text or text.casefold() in DISABLED_VALUES | HOT_ONLY_VALUES | HOT_COLD_VALUES:
        return None
    recorded = Path(text)
    if recorded.is_file():
        return recorded.resolve()
    basename = recorded_basename(text)
    roots: list[Path] = []
    if settings_file:
        roots.append(settings_file.parent)
    roots.extend([session_root, session_root.parent, session_root.parent.parent])
    candidates: set[Path] = set()
    for root in roots:
        for candidate in (root / basename, root / "darks" / basename, root / "flats" / basename):
            if candidate.is_file():
                candidates.add(candidate.resolve())
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        locations = ", ".join(str(path) for path in sorted(candidates, key=str))
        raise ValueError(f"Calibration filename is ambiguous after relocation: {basename}: {locations}")
    return None


def resolve_explicit_file(path: Path | None, option: str) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{option} file not found: {resolved}")
    return resolved


def resolve_switch(explicit: str, automatic: bool) -> tuple[bool, str]:
    if explicit == "enable":
        return True, "command-line"
    if explicit == "disable":
        return False, "command-line"
    return automatic, "CameraSettings" if automatic else "default"


def resolve_preprocessing_plan(
    *,
    settings: dict[str, str],
    settings_file: Path | None,
    session_root: Path,
    preprocessing: str = "auto",
    dark_correction: str = "auto",
    dark_file: Path | None = None,
    flat_correction: str = "auto",
    flat_file: Path | None = None,
    hot_pixel_correction: str = "auto",
    cold_pixel_correction: str = "auto",
    hot_pixel_sigma: float = 3.0,
    cold_pixel_sigma: float = 3.0,
) -> PreprocessingPlan:
    if preprocessing == "disable":
        return disabled_plan(settings_file, hot_pixel_sigma, cold_pixel_sigma)

    explicit_dark = resolve_explicit_file(dark_file, "--dark-file")
    explicit_flat = resolve_explicit_file(flat_file, "--flat-file")
    if explicit_dark and dark_correction == "disable":
        raise ValueError("--dark-file cannot be combined with --dark-correction disable")
    if explicit_flat and flat_correction == "disable":
        raise ValueError("--flat-file cannot be combined with --flat-correction disable")

    dark_value = setting(settings, "Subtract Dark")
    flat_value = setting(settings, "Apply Flat")
    dark_token = dark_value.casefold()
    flat_token = flat_value.casefold()
    sensitivity = parse_optional_float(setting(settings, "Hot Pixel Sensitivity"))

    recorded_dark_requested = bool(dark_value and dark_token not in DISABLED_VALUES | HOT_ONLY_VALUES | HOT_COLD_VALUES)
    recorded_flat_requested = bool(flat_value and flat_token not in DISABLED_VALUES)
    recorded_dark = resolve_recorded_file(dark_value, settings_file, session_root) if recorded_dark_requested else None
    recorded_flat = resolve_recorded_file(flat_value, settings_file, session_root) if recorded_flat_requested else None

    automatic_dark = recorded_dark_requested
    automatic_flat = recorded_flat_requested
    dark_enabled, dark_source = resolve_switch(dark_correction, automatic_dark)
    flat_enabled, flat_source = resolve_switch(flat_correction, automatic_flat)
    if explicit_dark:
        dark_enabled, dark_source = True, "command-line file"
    if explicit_flat:
        flat_enabled, flat_source = True, "command-line file"
    resolved_dark = explicit_dark or recorded_dark
    resolved_flat = explicit_flat or recorded_flat
    if dark_enabled and resolved_dark is None:
        raise FileNotFoundError(
            f"CameraSettings requests dark subtraction but the master dark was not found: {dark_value or '(not recorded)'}. "
            "Provide --dark-file or use --dark-correction disable."
        )
    if flat_enabled and resolved_flat is None:
        raise FileNotFoundError(
            f"CameraSettings requests flat correction but the master flat was not found: {flat_value or '(not recorded)'}. "
            "Provide --flat-file or use --flat-correction disable."
        )

    # SharpCap's numeric sensitivity is an on/off signal here. Its scale is
    # not compatible with Siril sigma thresholds, so the numeric value is not
    # converted.
    automatic_hot = sensitivity is not None and sensitivity != 0.0
    automatic_cold = dark_token in HOT_COLD_VALUES and automatic_hot
    hot_enabled, hot_source = resolve_switch(hot_pixel_correction, automatic_hot)
    cold_enabled, cold_source = resolve_switch(cold_pixel_correction, automatic_cold)

    return PreprocessingPlan(
        enabled=dark_enabled or flat_enabled or hot_enabled or cold_enabled,
        settings_file=str(settings_file) if settings_file else None,
        dark_enabled=dark_enabled,
        dark_file=str(resolved_dark) if dark_enabled and resolved_dark else None,
        dark_source=dark_source,
        flat_enabled=flat_enabled,
        flat_file=str(resolved_flat) if flat_enabled and resolved_flat else None,
        flat_source=flat_source,
        hot_pixel_enabled=hot_enabled,
        hot_pixel_source=hot_source,
        cold_pixel_enabled=cold_enabled,
        cold_pixel_source=cold_source,
        hot_pixel_sigma=hot_pixel_sigma,
        cold_pixel_sigma=cold_pixel_sigma,
        sharpcap_hot_pixel_sensitivity=sensitivity,
    )


def stage_preprocessing_files(plan: PreprocessingPlan, work_dir: Path) -> PreprocessingPlan:
    updates: dict[str, object] = {}
    calibration_dir = work_dir / "calibration"
    for kind, value in (("dark", plan.dark_file), ("flat", plan.flat_file)):
        if not value:
            continue
        source = Path(value).resolve()
        suffix = source.suffix.lower() or ".fit"
        calibration_dir.mkdir(parents=True, exist_ok=True)
        destination = calibration_dir / f"master_{kind}{suffix}"
        if source != destination.resolve():
            shutil.copy2(source, destination)
        updates[f"{kind}_file"] = str(destination)
    return replace(plan, **updates) if updates else plan


def cosmetic_sigmas(plan: PreprocessingPlan) -> tuple[float, float]:
    cold = plan.cold_pixel_sigma if plan.cold_pixel_enabled else DISABLED_COSMETIC_SIGMA
    hot = plan.hot_pixel_sigma if plan.hot_pixel_enabled else DISABLED_COSMETIC_SIGMA
    return cold, hot


def quote_siril_argument(value: str | Path) -> str:
    """Quote one Siril script argument while retaining portable path separators."""
    escaped = str(value).replace("\\", "/").replace('"', '\\"')
    return f'"{escaped}"'


def siril_master_name(value: str | None, lines: list[str]) -> str | None:
    if not value:
        return None
    source = Path(value)
    script_source = f"calibration/{source.name}" if source.parent.name == "calibration" else source.name
    if source.suffix.casefold() in {".fit", ".fits", ".fts"}:
        return script_source
    converted = source.with_suffix(".fit")
    script_converted = (
        f"calibration/{converted.name}" if converted.parent.name == "calibration" else converted.name
    )
    lines.extend(
        [
            f"load {quote_siril_argument(script_source)}",
            f"save {quote_siril_argument(script_converted)}",
            "close",
        ]
    )
    return script_converted


def build_sequence_preprocess_script(
    basename: str,
    plan: PreprocessingPlan,
    *,
    cfa: bool,
) -> tuple[str, str]:
    lines = ["requires 1.4.0", f"convert {basename}"]
    dark_name = siril_master_name(plan.dark_file, lines)
    flat_name = siril_master_name(plan.flat_file, lines)
    input_sequence = f"{basename}_"
    if (plan.hot_pixel_enabled or plan.cold_pixel_enabled) and not plan.dark_enabled:
        cold, hot = cosmetic_sigmas(plan)
        command = "seqfind_cosme_cfa" if cfa else "seqfind_cosme"
        lines.append(f"{command} {input_sequence} {cold:.8g} {hot:.8g} -prefix=cc_")
        input_sequence = f"cc_{input_sequence}"

    needs_calibrate = plan.dark_enabled or plan.flat_enabled or cfa
    if needs_calibrate:
        options: list[str] = []
        if plan.dark_enabled and dark_name:
            options.append(f"-dark={dark_name}")
        if plan.flat_enabled and flat_name:
            options.append(f"-flat={flat_name}")
        if (plan.hot_pixel_enabled or plan.cold_pixel_enabled) and plan.dark_enabled:
            cold, hot = cosmetic_sigmas(plan)
            options.extend(["-cc=dark", f"{cold:.8g}", f"{hot:.8g}"])
        if cfa:
            options.extend(["-cfa", "-debayer"])
        options.append("-prefix=pp_")
        lines.append(f"calibrate {input_sequence} " + " ".join(options))
        output_sequence = f"pp_{input_sequence}"
    else:
        output_sequence = input_sequence
    return "\n".join([*lines, ""]), output_sequence


def build_single_preprocess_script(
    source: Path,
    plan: PreprocessingPlan,
    *,
    cfa: bool,
    corrected_intermediate: Path,
) -> tuple[str, str]:
    lines = ["requires 1.4.0"]
    dark_name = siril_master_name(plan.dark_file, lines)
    flat_name = siril_master_name(plan.flat_file, lines)
    input_path = source
    if (plan.hot_pixel_enabled or plan.cold_pixel_enabled) and not plan.dark_enabled:
        cold, hot = cosmetic_sigmas(plan)
        command = "find_cosme_cfa" if cfa else "find_cosme"
        lines.extend(
            [
                f"load {quote_siril_argument(source.name)}",
                f"{command} {cold:.8g} {hot:.8g}",
                f"save {quote_siril_argument(corrected_intermediate.name)}",
                "close",
            ]
        )
        input_path = corrected_intermediate

    options: list[str] = []
    if plan.dark_enabled and dark_name:
        options.append(f"-dark={dark_name}")
    if plan.flat_enabled and flat_name:
        options.append(f"-flat={flat_name}")
    if (plan.hot_pixel_enabled or plan.cold_pixel_enabled) and plan.dark_enabled:
        cold, hot = cosmetic_sigmas(plan)
        options.extend(["-cc=dark", f"{cold:.8g}", f"{hot:.8g}"])
    if cfa:
        options.extend(["-cfa", "-debayer"])
    options.append("-prefix=pp_")
    lines.append(f"calibrate_single {quote_siril_argument(input_path.name)} " + " ".join(options))
    return "\n".join([*lines, ""]), f"pp_{input_path.name}"
