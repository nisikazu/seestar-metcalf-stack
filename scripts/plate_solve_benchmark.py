#!/usr/bin/env python3
"""Benchmark remote Astrometry.net and Siril plate solving.

The benchmark repeats three supplied image scales (0.5x, 1x, and 2x) for
each solver. Every run starts from the same FITS pixels and approximate center.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from astrometry_solve import estimate_scale_hint, read_fits_header
from moving_target_pipeline import sanitize_fits_for_upload


REPO_ROOT = Path(__file__).resolve().parents[1]
SCALE_CASES = (
    ("half", 0.5),
    ("correct", 1.0),
    ("double", 2.0),
)
WCS_KEYS = {"CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2"}


@dataclass(frozen=True)
class Trial:
    order: int
    solver: str
    scale_label: str
    scale_factor: float
    repeat: int


@dataclass
class Result:
    order: int
    solver: str
    scale_label: str
    scale_factor: float
    repeat: int
    supplied_pixel_scale_arcsec: float
    supplied_focal_length_mm: float
    effective_pixel_size_um: float
    status: str
    elapsed_seconds: float
    return_code: int | None
    solved_ra_deg: float | None
    solved_dec_deg: float | None
    solved_pixel_scale_arcsec: float | None
    error: str
    log_path: str
    result_path: str


def positive_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def parse_sexagesimal(value: str, *, is_ra: bool) -> float:
    text = value.strip()
    if not text:
        raise ValueError("empty coordinate")
    if ":" not in text and not any(character.isspace() for character in text):
        return float(text)
    sign = -1.0 if text.startswith("-") else 1.0
    cleaned = text.lstrip("+-").replace(":", " ")
    parts = [float(part) for part in cleaned.split()]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"unsupported coordinate: {value}")
    absolute = parts[0]
    if len(parts) > 1:
        absolute += parts[1] / 60.0
    if len(parts) > 2:
        absolute += parts[2] / 3600.0
    degrees = absolute * (15.0 if is_ra else 1.0)
    return sign * degrees


def infer_center(header: dict[str, object], ra_override: float | None, dec_override: float | None) -> tuple[float, float]:
    ra_value = ra_override if ra_override is not None else header.get("RA", header.get("OBJCTRA", header.get("CRVAL1")))
    dec_value = dec_override if dec_override is not None else header.get("DEC", header.get("OBJCTDEC", header.get("CRVAL2")))
    if ra_value is None or dec_value is None:
        raise ValueError("FITS has no usable RA/DEC; provide --ra-deg and --dec-deg")
    ra = float(ra_value) if isinstance(ra_value, (int, float)) else parse_sexagesimal(str(ra_value), is_ra=True)
    dec = float(dec_value) if isinstance(dec_value, (int, float)) else parse_sexagesimal(str(dec_value), is_ra=False)
    if not math.isfinite(ra) or not math.isfinite(dec) or not -90 <= dec <= 90:
        raise ValueError(f"invalid center coordinates: RA={ra}, Dec={dec}")
    return ra % 360.0, dec


def wcs_pixel_scale(header: dict[str, object]) -> float | None:
    try:
        cd11 = float(header["CD1_1"])
        cd12 = float(header["CD1_2"])
        cd21 = float(header["CD2_1"])
        cd22 = float(header["CD2_2"])
        x_scale = math.hypot(cd11, cd21) * 3600.0
        y_scale = math.hypot(cd12, cd22) * 3600.0
        return (x_scale + y_scale) / 2.0
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return (abs(float(header["CDELT1"])) + abs(float(header["CDELT2"]))) * 1800.0
    except (KeyError, TypeError, ValueError):
        return None


def infer_true_scale(header: dict[str, object], explicit: float | None) -> tuple[float, str]:
    if explicit is not None:
        if not math.isfinite(explicit) or explicit <= 0:
            raise ValueError("--pixel-scale-arcsec must be a positive finite number")
        return explicit, "command-line"
    hint = estimate_scale_hint(header)
    if hint and positive_float(hint.get("arcsecPerPix")):
        return float(hint["arcsecPerPix"]), "FITS equipment metadata"
    wcs_scale = wcs_pixel_scale(header)
    if wcs_scale:
        return wcs_scale, "FITS WCS"
    raise ValueError(
        "Could not infer the correct pixel scale. Provide --pixel-scale-arcsec in arcseconds/pixel."
    )


def infer_effective_pixel_size(header: dict[str, object], explicit: float | None) -> tuple[float, str]:
    if explicit is not None:
        if not math.isfinite(explicit) or explicit <= 0:
            raise ValueError("--effective-pixel-size-um must be a positive finite number")
        return explicit, "command-line"
    x_size = positive_float(header.get("XPIXSZ"))
    y_size = positive_float(header.get("YPIXSZ"))
    x_bin = positive_float(header.get("XBINNING")) or positive_float(header.get("CCDXBIN")) or 1.0
    y_bin = positive_float(header.get("YBINNING")) or positive_float(header.get("CCDYBIN")) or 1.0
    values = []
    if x_size:
        values.append(x_size * x_bin)
    if y_size:
        values.append(y_size * y_bin)
    if values:
        return statistics.mean(values), "FITS pixel metadata"
    # Siril only uses the ratio of pixel size to focal length to derive image
    # sampling. A synthetic 1 um pixel therefore preserves the requested scale.
    return 1.0, "synthetic ratio"


def focal_length_for_scale(pixel_size_um: float, scale_arcsec: float) -> float:
    return 206.265 * pixel_size_um / scale_arcsec


def make_trials(solvers: Iterable[str], repeats: int, seed: int) -> list[Trial]:
    randomizer = random.Random(seed)
    trials: list[Trial] = []
    for repeat in range(1, repeats + 1):
        block = [(solver, label, factor) for solver in solvers for label, factor in SCALE_CASES]
        randomizer.shuffle(block)
        for solver, label, factor in block:
            trials.append(Trial(len(trials) + 1, solver, label, factor, repeat))
    return trials


def select_scale_trials(trials: list[Trial], scale_case: str) -> list[Trial]:
    selected = trials if scale_case == "all" else [trial for trial in trials if trial.scale_label == scale_case]
    return [
        Trial(order=index, solver=trial.solver, scale_label=trial.scale_label, scale_factor=trial.scale_factor, repeat=trial.repeat)
        for index, trial in enumerate(selected, start=1)
    ]


def resolve_siril(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("SIRIL_CLI"):
        candidates.append(Path(os.environ["SIRIL_CLI"]))
    candidates.extend(
        [
            REPO_ROOT / "tools" / "siril-1.4.1" / "siril" / "bin" / "siril-cli.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Siril" / "bin" / "siril-cli.exe",
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Siril" / "siril-cli.exe",
        ]
    )
    discovered = shutil.which("siril-cli") or shutil.which("siril-cli.exe")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("Siril CLI was not found; provide --siril")


def read_api_key(key_file: Path | None) -> str:
    environment = os.environ.get("ASTROMETRY_NET_API_KEY", "").strip()
    if environment:
        return environment
    candidates = [key_file] if key_file else []
    candidates.extend([REPO_ROOT / ".astrometry_api_key", Path.cwd() / ".astrometry_api_key"])
    for candidate in candidates:
        if candidate and candidate.is_file():
            value = candidate.read_text(encoding="utf-8").strip()
            if value:
                return value
    raise RuntimeError("Astrometry.net API key was not found; provide --astrometry-key-file")


def quoted_siril_path(path: Path) -> str:
    return '"' + str(path.resolve()).replace("\\", "/").replace('"', '\\"') + '"'


def build_siril_script(
    input_fits: Path,
    output_fits: Path,
    focal_mm: float,
    pixel_size_um: float,
    catalog: str | None,
) -> str:
    catalog_option = f" -catalog={catalog}" if catalog else ""
    return "\n".join(
        [
            "requires 1.4.0",
            f"load {quoted_siril_path(input_fits)}",
            (
                # Siril 1.4.1 rejects explicit coordinates in scripts despite
                # documenting them; the loaded benchmark FITS already carries
                # the same approximate center in its header.
                "platesolve -force "
                f"-focal={focal_mm:.10f} -pixelsize={pixel_size_um:.10f} -noflip{catalog_option}"
            ),
            f"save {quoted_siril_path(output_fits)}",
            "close",
            "",
        ]
    )


def parse_astrometry_result(path: Path) -> tuple[float | None, float | None, float | None, str]:
    if not path.is_file():
        return None, None, None, "Astrometry.net did not write a result JSON"
    payload = json.loads(path.read_text(encoding="utf-8"))
    status = payload.get("status") or {}
    if status.get("status") != "success":
        return None, None, None, f"Astrometry.net status: {status.get('status', 'unknown')}"
    calibration = payload.get("calibration") or payload.get("results", {}).get("calibration") or {}
    ra = positive_or_signed(calibration.get("ra"))
    dec = positive_or_signed(calibration.get("dec"))
    scale = positive_float(calibration.get("pixscale"))
    if ra is None or dec is None or scale is None:
        return ra, dec, scale, "Astrometry.net result has incomplete calibration"
    return ra, dec, scale, ""


def positive_or_signed(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def find_siril_output(expected: Path) -> Path | None:
    candidates = [expected, expected.with_suffix(".fit"), expected.with_suffix(".fits")]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def parse_siril_result(path: Path | None) -> tuple[float | None, float | None, float | None, str]:
    if path is None:
        return None, None, None, "Siril did not write the solved FITS"
    header = read_fits_header(path)
    if not WCS_KEYS.issubset(header):
        return None, None, None, "Siril output has no complete WCS center"
    ra = positive_or_signed(header.get("CRVAL1"))
    dec = positive_or_signed(header.get("CRVAL2"))
    scale = wcs_pixel_scale(header)
    if ra is None or dec is None or scale is None:
        return ra, dec, scale, "Siril output has incomplete WCS"
    return ra, dec, scale, ""


def run_command_with_heartbeat(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    log_path: Path,
    heartbeat_seconds: float = 15.0,
) -> tuple[int | None, float, bool, str]:
    started = time.perf_counter()
    timed_out = False
    next_heartbeat = heartbeat_seconds
    environment = {**environment, "PYTHONUNBUFFERED": "1"}
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("Command: " + subprocess.list2cmdline(command) + "\n\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            text=True,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        while process.poll() is None:
            elapsed = time.perf_counter() - started
            if elapsed >= timeout_seconds:
                timed_out = True
                process.kill()
                break
            if elapsed >= next_heartbeat:
                print(
                    f"  waiting: {elapsed:.0f}s elapsed; live log: {log_path}",
                    flush=True,
                )
                next_heartbeat += heartbeat_seconds
            time.sleep(min(0.05, max(0.005, timeout_seconds - elapsed)))
        return_code = process.wait()
    elapsed = time.perf_counter() - started
    output = log_path.read_text(encoding="utf-8", errors="replace")
    return return_code, elapsed, timed_out, output


def run_trial(
    trial: Trial,
    args: argparse.Namespace,
    input_fits: Path,
    true_scale: float,
    pixel_size_um: float,
    center: tuple[float, float],
    output_root: Path,
    astrometry_key: str | None,
    siril_path: Path | None,
) -> Result:
    supplied_scale = true_scale * trial.scale_factor
    supplied_focal = focal_length_for_scale(pixel_size_um, supplied_scale)
    run_dir = output_root / f"{trial.order:03d}_{trial.solver}_{trial.scale_label}_r{trial.repeat:02d}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "solver.log"
    result_path = run_dir / ("astrometry.json" if trial.solver == "astrometry" else "siril_solved.fit")
    environment = os.environ.copy()
    command: list[str]
    if trial.solver == "astrometry":
        environment["ASTROMETRY_NET_API_KEY"] = astrometry_key or ""
        command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "astrometry_solve.py"),
            str(input_fits),
            str(result_path),
            str(run_dir / "astrometry_wcs.fits"),
            "--pixel-scale-arcsec",
            f"{supplied_scale:.12g}",
            "--center-ra-deg",
            f"{center[0]:.12g}",
            "--center-dec-deg",
            f"{center[1]:.12g}",
            "--scale-range-factor",
            f"{args.astrometry_scale_range_factor:.12g}",
        ]
    else:
        if args.siril_cache_mode == "cold-each":
            isolated_root = run_dir / "siril-user-data"
            isolated_root.mkdir(parents=True, exist_ok=True)
            if os.name == "nt":
                environment["LOCALAPPDATA"] = str(isolated_root)
                environment["APPDATA"] = str(isolated_root / "roaming")
                environment["USERPROFILE"] = str(isolated_root / "profile")
                environment["HOME"] = str(isolated_root / "profile")
            else:
                environment["XDG_CACHE_HOME"] = str(isolated_root / "cache")
                environment["XDG_CONFIG_HOME"] = str(isolated_root / "config")
                environment["XDG_DATA_HOME"] = str(isolated_root / "data")
        script_path = run_dir / "platesolve.ssf"
        script_path.write_text(
            build_siril_script(
                input_fits,
                result_path,
                supplied_focal,
                pixel_size_um,
                args.siril_catalog,
            ),
            encoding="utf-8",
        )
        command = [str(siril_path), "-s", str(script_path)]

    status = "failure"
    return_code: int | None = None
    error = ""
    output = ""
    started = time.perf_counter()
    try:
        return_code, elapsed, timed_out, output = run_command_with_heartbeat(
            command,
            cwd=run_dir,
            environment=environment,
            timeout_seconds=args.timeout_seconds,
            log_path=log_path,
        )
        if timed_out:
            status = "timeout"
            error = f"timed out after {args.timeout_seconds:g} seconds"
    except OSError as run_error:
        elapsed = time.perf_counter() - started
        error = str(run_error)

    solved_ra = solved_dec = solved_scale = None
    if status != "timeout" and return_code == 0:
        try:
            if trial.solver == "astrometry":
                solved_ra, solved_dec, solved_scale, error = parse_astrometry_result(result_path)
            else:
                actual_output = find_siril_output(result_path)
                solved_ra, solved_dec, solved_scale, error = parse_siril_result(actual_output)
                if actual_output is not None:
                    result_path = actual_output
            status = "success" if not error else "failure"
        except (OSError, ValueError, json.JSONDecodeError) as parse_error:
            error = f"could not validate solver output: {parse_error}"
    elif status != "timeout":
        error = error or f"solver exited with code {return_code}"

    return Result(
        order=trial.order,
        solver=trial.solver,
        scale_label=trial.scale_label,
        scale_factor=trial.scale_factor,
        repeat=trial.repeat,
        supplied_pixel_scale_arcsec=supplied_scale,
        supplied_focal_length_mm=supplied_focal,
        effective_pixel_size_um=pixel_size_um,
        status=status,
        elapsed_seconds=elapsed,
        return_code=return_code,
        solved_ra_deg=solved_ra,
        solved_dec_deg=solved_dec,
        solved_pixel_scale_arcsec=solved_scale,
        error=error,
        log_path=str(log_path),
        result_path=str(result_path) if result_path.exists() else "",
    )


def optional_stat(values: list[float], operation: str) -> float | None:
    if not values:
        return None
    if operation == "mean":
        return statistics.mean(values)
    if operation == "stdev":
        return statistics.stdev(values) if len(values) >= 2 else 0.0
    if operation == "median":
        return statistics.median(values)
    if operation == "min":
        return min(values)
    if operation == "max":
        return max(values)
    raise ValueError(operation)


def summarize(results: list[Result]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for solver in sorted({result.solver for result in results}):
        for label, factor in SCALE_CASES:
            selected = [result for result in results if result.solver == solver and result.scale_label == label]
            success_times = [result.elapsed_seconds for result in selected if result.status == "success"]
            all_times = [result.elapsed_seconds for result in selected]
            rows.append(
                {
                    "solver": solver,
                    "scale_label": label,
                    "scale_factor": factor,
                    "attempts": len(selected),
                    "successes": sum(result.status == "success" for result in selected),
                    "failures": sum(result.status == "failure" for result in selected),
                    "timeouts": sum(result.status == "timeout" for result in selected),
                    "mean_success_seconds": optional_stat(success_times, "mean"),
                    "stdev_success_seconds": optional_stat(success_times, "stdev"),
                    "median_success_seconds": optional_stat(success_times, "median"),
                    "min_success_seconds": optional_stat(success_times, "min"),
                    "max_success_seconds": optional_stat(success_times, "max"),
                    "mean_all_seconds": optional_stat(all_times, "mean"),
                    "stdev_all_seconds": optional_stat(all_times, "stdev"),
                }
            )
    return rows


def compare_solvers(summary: list[dict[str, object]]) -> list[dict[str, object]]:
    by_condition = {(row["solver"], row["scale_label"]): row for row in summary}
    comparisons: list[dict[str, object]] = []
    for label, factor in SCALE_CASES:
        astrometry = by_condition.get(("astrometry", label))
        siril = by_condition.get(("siril", label))
        astrometry_mean = astrometry.get("mean_success_seconds") if astrometry else None
        siril_mean = siril.get("mean_success_seconds") if siril else None
        ratio = None
        if astrometry_mean is not None and siril_mean not in {None, 0}:
            ratio = float(astrometry_mean) / float(siril_mean)
        comparisons.append(
            {
                "scale_label": label,
                "scale_factor": factor,
                "astrometry_mean_success_seconds": astrometry_mean,
                "siril_mean_success_seconds": siril_mean,
                "astrometry_over_siril_ratio": ratio,
            }
        )
    return comparisons


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def display_number(value: object) -> str:
    return "-" if value is None else f"{float(value):.2f}"


def write_markdown(
    path: Path,
    config: dict[str, object],
    summary: list[dict[str, object]],
    comparisons: list[dict[str, object]],
) -> None:
    lines = [
        "# Plate solve benchmark",
        "",
        f"- FITS: `{config['fits']}`",
        f"- Correct scale: {config['true_scale_arcsec']:.6f} arcsec/pixel ({config['scale_source']})",
        f"- Center: RA={config['ra_deg']:.8f} deg, Dec={config['dec_deg']:.8f} deg",
        f"- Repeats: {config['repeats']} per solver/scale condition",
        f"- Timeout: {config['timeout_seconds']} seconds per trial",
        f"- Seed: {config['seed']}",
        f"- Astrometry.net scale range: supplied scale / {config['astrometry_scale_range_factor']} through supplied scale * {config['astrometry_scale_range_factor']}",
        "",
        "Successful-run statistics exclude failures and timeouts. The all-run mean includes their elapsed time, so timeout duration affects it.",
        "",
        "| Solver | Scale | Success | Failure | Timeout | Mean success (s) | SD success (s) | Median (s) | Min (s) | Max (s) | Mean all (s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary:
        lines.append(
            "| {solver} | {scale_label} ({scale_factor:g}x) | {successes}/{attempts} | {failures} | {timeouts} | {mean} | {stdev} | {median} | {minimum} | {maximum} | {mean_all} |".format(
                **row,
                mean=display_number(row["mean_success_seconds"]),
                stdev=display_number(row["stdev_success_seconds"]),
                median=display_number(row["median_success_seconds"]),
                minimum=display_number(row["min_success_seconds"]),
                maximum=display_number(row["max_success_seconds"]),
                mean_all=display_number(row["mean_all_seconds"]),
            )
        )
    if any(row["astrometry_over_siril_ratio"] is not None for row in comparisons):
        lines.extend(
            [
                "",
                "## Solver comparison",
                "",
                "The ratio is Astrometry.net mean success time divided by Siril mean success time. Values above 1 mean Siril was faster.",
                "",
                "| Scale | Astrometry.net mean (s) | Siril mean (s) | Astrometry / Siril |",
                "|---|---:|---:|---:|",
            ]
        )
        for row in comparisons:
            lines.append(
                "| {scale_label} ({scale_factor:g}x) | {astrometry} | {siril} | {ratio} |".format(
                    **row,
                    astrometry=display_number(row["astrometry_mean_success_seconds"]),
                    siril=display_number(row["siril_mean_success_seconds"]),
                    ratio=display_number(row["astrometry_over_siril_ratio"]),
                )
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare Astrometry.net and Siril plate-solve time at 0.5x, 1x, and 2x supplied pixel scales."
    )
    parser.add_argument("fits", type=Path, help="One FITS image to solve repeatedly")
    parser.add_argument("--pixel-scale-arcsec", type=float, help="Correct image scale in arcseconds/pixel")
    parser.add_argument("--effective-pixel-size-um", type=float, help="Effective pixel pitch used to express Siril focal length")
    parser.add_argument("--ra-deg", type=float, help="Approximate J2000 center RA in degrees")
    parser.add_argument("--dec-deg", type=float, help="Approximate J2000 center Dec in degrees")
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--solver", choices=("both", "astrometry", "siril"), default="both")
    parser.add_argument(
        "--scale-case",
        choices=("all", "half", "correct", "double"),
        default="all",
        help="Run all scale cases or only one (default: all)",
    )
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--seed", type=int, default=20260815)
    parser.add_argument("--astrometry-delay-seconds", type=float, default=3.0)
    parser.add_argument(
        "--astrometry-scale-range-factor",
        type=float,
        default=2.2,
        help="Search from supplied scale/factor to supplied scale*factor (default: 2.2)",
    )
    parser.add_argument("--confirm-astrometry-uploads", action="store_true")
    parser.add_argument("--astrometry-key-file", type=Path)
    parser.add_argument("--siril", type=Path)
    parser.add_argument("--siril-catalog", choices=("tycho2", "nomad", "localgaia", "gaia", "ppmxl", "brightstars", "apass"))
    parser.add_argument(
        "--siril-cache-mode",
        choices=("reuse", "cold-each"),
        default="reuse",
        help="Reuse Siril's normal catalogue cache, or give every trial an empty isolated cache (default: reuse)",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Print inferred parameters and trial order without contacting either solver")
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.astrometry_delay_seconds < 0:
        parser.error("--astrometry-delay-seconds cannot be negative")
    if not math.isfinite(args.astrometry_scale_range_factor) or args.astrometry_scale_range_factor <= 1:
        parser.error("--astrometry-scale-range-factor must be greater than 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.fits.expanduser().resolve()
    if not source.is_file() or source.suffix.casefold() not in {".fit", ".fits"}:
        raise FileNotFoundError(f"FITS file not found: {source}")
    header = read_fits_header(source)
    true_scale, scale_source = infer_true_scale(header, args.pixel_scale_arcsec)
    pixel_size, pixel_source = infer_effective_pixel_size(header, args.effective_pixel_size_um)
    center = infer_center(header, args.ra_deg, args.dec_deg)
    solvers = ("astrometry", "siril") if args.solver == "both" else (args.solver,)
    trials = select_scale_trials(make_trials(solvers, args.repeats, args.seed), args.scale_case)
    astrometry_trials = sum(trial.solver == "astrometry" for trial in trials)
    if astrometry_trials and not args.dry_run and not args.confirm_astrometry_uploads:
        raise RuntimeError(
            f"This run will upload the sanitized FITS {astrometry_trials} times to Astrometry.net. "
            "Review the count, then add --confirm-astrometry-uploads."
        )

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root = (args.output_dir or REPO_ROOT / "plate_solve_benchmark" / f"{source.stem}-{stamp}").resolve()
    config = {
        "fits": str(source),
        "true_scale_arcsec": true_scale,
        "scale_source": scale_source,
        "effective_pixel_size_um": pixel_size,
        "pixel_size_source": pixel_source,
        "ra_deg": center[0],
        "dec_deg": center[1],
        "repeats": args.repeats,
        "solvers": list(solvers),
        "scale_case": args.scale_case,
        "siril_cache_mode": args.siril_cache_mode,
        "timeout_seconds": args.timeout_seconds,
        "seed": args.seed,
        "astrometry_uploads": astrometry_trials,
        "astrometry_scale_range_factor": args.astrometry_scale_range_factor,
        "astrometry_scale_ranges_arcsec_per_pixel": {
            label: {
                "supplied": true_scale * factor,
                "lower": true_scale * factor / args.astrometry_scale_range_factor,
                "upper": true_scale * factor * args.astrometry_scale_range_factor,
            }
            for label, factor in SCALE_CASES
        },
        "order": [asdict(trial) for trial in trials],
    }
    print(json.dumps(config, ensure_ascii=False, indent=2))
    if args.dry_run:
        return 0

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "benchmark_config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    benchmark_input = sanitize_fits_for_upload(source, output_root / "benchmark_input_sanitized.fit")
    astrometry_key = read_api_key(args.astrometry_key_file) if astrometry_trials else None
    siril_path = resolve_siril(args.siril) if "siril" in solvers else None
    results: list[Result] = []
    for trial in trials:
        print(
            f"[{trial.order}/{len(trials)}] {trial.solver} {trial.scale_label} "
            f"repeat={trial.repeat} scale={true_scale * trial.scale_factor:.6f} arcsec/pixel",
            flush=True,
        )
        result = run_trial(
            trial,
            args,
            benchmark_input,
            true_scale,
            pixel_size,
            center,
            output_root,
            astrometry_key,
            siril_path,
        )
        results.append(result)
        print(f"  {result.status}: {result.elapsed_seconds:.2f}s {result.error}", flush=True)
        write_csv(output_root / "benchmark_runs.csv", [asdict(item) for item in results])
        if trial.solver == "astrometry" and args.astrometry_delay_seconds:
            time.sleep(args.astrometry_delay_seconds)

    summary = summarize(results)
    comparisons = compare_solvers(summary)
    write_csv(output_root / "benchmark_summary.csv", summary)
    write_csv(output_root / "benchmark_comparison.csv", comparisons)
    write_markdown(output_root / "benchmark_summary.md", config, summary, comparisons)
    print(f"Wrote {output_root / 'benchmark_runs.csv'}")
    print(f"Wrote {output_root / 'benchmark_summary.csv'}")
    print(f"Wrote {output_root / 'benchmark_comparison.csv'}")
    print(f"Wrote {output_root / 'benchmark_summary.md'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
