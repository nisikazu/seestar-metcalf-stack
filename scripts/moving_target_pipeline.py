#!/usr/bin/env python
"""End-to-end moving-target stack pipeline.

This wrapper connects:
1. Siril-first plate solving with Astrometry.net fallback,
2. Siril preprocessing and background-star registration, or SharpCap StackLog registration,
3. target-motion compensated stacking with moving_target_stack.py.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from moving_target_stack import (
    SirilRegistrationError,
    normalize_saturation_color,
    parse_output_region_ratio,
    parse_stack_workers,
    parse_median_tile_rows,
    parse_time,
    processing_method_token,
    read_source_image,
    read_fits_header,
    run_siril,
    select_reference_index,
    validate_output_region_options,
    wcs_cd_matrix,
    write_registered_float,
)
from sharpcap_stacklog import load_sharpcap_session, read_settings, write_manifest
from siril_preprocessing import (
    PreprocessingPlan,
    build_single_preprocess_script,
    quote_siril_argument,
    resolve_preprocessing_plan,
    stage_preprocessing_files,
)
from astrometry_solve import estimate_scale_hint, read_api_key


REPO_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[1]
)
PRIVACY_FITS_KEYS = {
    "SITELONG",
    "SITELAT",
    "SITEELEV",
    "ELEVATIO",
    "ELEVATION",
    "OBSLONG",
    "OBSLAT",
}
SIRIL_CATALOG_RETRIES = 3
SIRIL_CATALOG_RETRY_DELAY_SEC = 2.0


class TeeTextIO:
    def __init__(self, *streams: TextIO):
        self.streams = streams

    @property
    def encoding(self) -> str:
        return self.streams[0].encoding or "utf-8"

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def isatty(self) -> bool:
        return any(getattr(stream, "isatty", lambda: False)() for stream in self.streams)


def parse_args() -> argparse.Namespace:
    target_mode_default = os.environ.get("SEESTAR_STACK_TARGET_MODE", "moving").strip().lower()
    if target_mode_default not in {"moving", "fixed"}:
        target_mode_default = "moving"
    parser = argparse.ArgumentParser(description="Plate-solve, preprocess, and stack astronomical subframes")
    parser.add_argument(
        "source_dir_arg",
        nargs="?",
        type=Path,
        metavar="SOURCE",
        help="Subframe directory, or a SharpCap stacklog.csv file",
    )
    parser.add_argument("--source-dir", dest="source_dir_option", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--target-mode",
        choices=("moving", "fixed"),
        default=target_mode_default,
        help="Stack on a moving target (default) or on fixed background stars.",
    )
    parser.add_argument(
        "--ephemeris-csv",
        type=Path,
        help="Existing or desired Horizons CSV. If omitted, one is generated automatically.",
    )
    parser.add_argument("--pattern", default="*.fit*")
    parser.add_argument("--count", type=int)
    parser.add_argument("--after", help="Keep frames at or after this UTC ISO timestamp")
    parser.add_argument("--before", help="Keep frames at or before this UTC ISO timestamp")
    parser.add_argument(
        "--include-failed-frames",
        action="store_true",
        help="Include Seestar files whose names contain '_failed_'. They are skipped by default.",
    )
    parser.add_argument(
        "--include-sharpcap-rejected",
        action="store_true",
        help="Include SharpCap StackLog rows whose Frame Stacked? value is false. Defaults to successful frames only.",
    )
    parser.add_argument("--frame-manifest", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--bayer-pattern",
        choices=("RGGB", "BGGR", "GRBG", "GBRG"),
        help="Bayer pattern for SharpCap RAW PNG/TIFF when the image itself does not record one.",
    )
    parser.add_argument(
        "--session-gap-min",
        type=float,
        default=60.0,
        help="Split frames into sessions at gaps larger than this many minutes. Defaults to 60.",
    )
    parser.add_argument(
        "--session-index",
        type=int,
        help="1-based session to use. Defaults to the latest session after gap splitting.",
    )
    parser.add_argument(
        "--session-at",
        help=(
            "Select the first session whose first DATE-OBS is at or after this local time. "
            "Format: YYYYMMDD or YYYYMMDD-hhmmss; hh, mm, ss must be two digits when present."
        ),
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="List detected sessions and exit without calling Horizons, Astrometry.net, or Siril.",
    )
    parser.add_argument("--no-auto-ephemeris", action="store_true", help="Fail instead of generating a missing ephemeris CSV")
    parser.add_argument(
        "--horizons-center",
        choices=("fits-site", "geocenter"),
        default="fits-site",
        help="Observer center for auto-generated Horizons CSV. fits-site sends FITS SITELONG/SITELAT to JPL.",
    )
    parser.add_argument(
        "--site-longitude",
        type=float,
        help="Observer longitude in degrees east. Overrides FITS SITELONG when both site coordinates are supplied.",
    )
    parser.add_argument(
        "--site-latitude",
        type=float,
        help="Observer latitude in degrees north. Overrides FITS SITELAT when both site coordinates are supplied.",
    )
    parser.add_argument(
        "--pixel-scale-arcsec",
        type=float,
        help="Approximate image scale in arcseconds per pixel for plate solving when FITS camera metadata is missing.",
    )
    parser.add_argument("--horizons-object", help="Override Horizons object/designation for auto ephemeris")
    parser.add_argument("--horizons-command", help="Raw Horizons COMMAND value for auto ephemeris")
    parser.add_argument("--horizons-chunk-size", type=int, default=25)
    parser.add_argument("--horizons-retries", type=int, default=5)
    parser.add_argument(
        "--solve-dir",
        type=Path,
        help="Plate-solve cache directory. Defaults to the source FITS directory.",
    )
    parser.add_argument(
        "--solve-name",
        help="Plate-solve cache filename prefix. Defaults to the reference FITS stem.",
    )
    parser.add_argument("--wcs-fits", type=Path, help="Reuse an existing WCS FITS instead of solving")
    parser.add_argument("--astrometry-json", type=Path, help="Optional existing astrometry JSON, recorded in summary")
    parser.add_argument(
        "--plate-solver",
        choices=("auto", "siril", "astrometry"),
        default="auto",
        help="Plate solver order. auto tries Siril first and Astrometry.net only as fallback.",
    )
    parser.add_argument(
        "--siril-catalog",
        choices=("tycho2", "nomad", "localgaia", "gaia", "ppmxl", "brightstars", "apass"),
        help="Force a Siril plate-solve catalog instead of automatic selection.",
    )
    parser.add_argument("--solve-center-ra-deg", type=float, help="Approximate J2000 center RA for plate solving.")
    parser.add_argument("--solve-center-dec-deg", type=float, help="Approximate J2000 center Dec for plate solving.")
    parser.add_argument(
        "--skip-solve",
        action="store_true",
        help="Do not solve or upload; require a valid explicit or cached Siril/Astrometry.net solution.",
    )
    parser.add_argument("--work-dir", type=Path, help="Use this exact run work directory instead of creating one under --work-root")
    parser.add_argument("--work-root", type=Path, default=REPO_ROOT / "metcalf_output")
    parser.add_argument(
        "--work-name",
        help="Work directory stem. Defaults to '<FITS OBJECT>_<method>'; a timestamp is appended.",
    )
    parser.add_argument("--registration-transform", default="similarity")
    parser.add_argument("--registration-minpairs", type=int, default=6)
    parser.add_argument("--siril", type=Path, help="Siril CLI path. Defaults to SIRIL_CLI, PATH, or an OS-standard location.")
    parser.add_argument(
        "--preprocessing",
        choices=("auto", "disable"),
        default="auto",
        help="Use SharpCap CameraSettings preprocessing automatically, or disable all calibration/cosmetic correction.",
    )
    parser.add_argument("--dark-correction", choices=("auto", "enable", "disable"), default="auto")
    parser.add_argument("--dark-file", type=Path, help="Master dark override. Implies dark correction unless explicitly disabled.")
    parser.add_argument("--flat-correction", choices=("auto", "enable", "disable"), default="auto")
    parser.add_argument("--flat-file", type=Path, help="Master flat override. Implies flat correction unless explicitly disabled.")
    parser.add_argument("--hot-pixel-correction", choices=("auto", "enable", "disable"), default="auto")
    parser.add_argument("--cold-pixel-correction", choices=("auto", "enable", "disable"), default="auto")
    parser.add_argument("--hot-pixel-sigma", type=float, default=3.0, help="Siril hot-pixel sigma threshold. Defaults to 3.")
    parser.add_argument("--cold-pixel-sigma", type=float, default=3.0, help="Siril cold-pixel sigma threshold. Defaults to 3.")
    parser.add_argument(
        "--stack-method",
        choices=("mean", "median", "rankfit"),
        default="mean",
        help="Per-pixel combination method. median and rankfit exclude exact-zero samples. Defaults to mean.",
    )
    parser.add_argument(
        "--stack-workers",
        type=parse_stack_workers,
        default="auto",
        metavar="auto|1|2|4",
        help=(
            "Parallel workers for per-frame FITS/background/shift processing. "
            "auto selects 1, 2, or 4 from available RAM and frame size (default)."
        ),
    )
    parser.add_argument(
        "--median-tile-rows",
        type=parse_median_tile_rows,
        default="auto",
        metavar="auto|N",
        help=(
            "Full-width output rows held in each median/rank-fit working cube. "
            "auto uses at most about half of currently available RAM (default)."
        ),
    )
    parser.add_argument(
        "--output-region-mode",
        choices=("reference", "union", "cover-count", "cover-ratio"),
        default="reference",
        help=(
            "Output footprint: reference frame (default), all accepted frame footprints, "
            "or the bounding rectangle covered by a minimum frame count/ratio."
        ),
    )
    parser.add_argument(
        "--output-region-cover-count",
        "--cover-count",
        dest="output_region_cover_count",
        type=int,
        metavar="N",
        help="Minimum accepted-frame coverage for --output-region-mode cover-count.",
    )
    parser.add_argument(
        "--output-region-cover-ratio",
        "--cover-ratio",
        dest="output_region_cover_ratio",
        type=parse_output_region_ratio,
        metavar="M[%]",
        help="Minimum accepted-frame coverage percentage for --output-region-mode cover-ratio.",
    )
    parser.add_argument(
        "--rankfit-fraction",
        type=int,
        default=50,
        help="Central ranked-sample percentage used by rankfit (1-100). Defaults to 50.",
    )
    parser.add_argument(
        "--reference-frame",
        choices=("first", "middle"),
        default="first",
        help="Use the first frame or the frame nearest the session midpoint as registration/WCS reference.",
    )
    parser.add_argument(
        "--padding-policy",
        choices=("valid", "legacy"),
        default="valid",
        help=(
            "Treat all-zero Siril registration padding as missing and use per-pixel contribution "
            "counts (valid, default), or reproduce the previous padding behavior (legacy)."
        ),
    )
    parser.add_argument(
        "--background-normalization",
        choices=("none", "offset", "plane", "quadratic"),
        default=None,
        help=(
            "Subtract each usable frame's quadratic fitted background by default, or select none, offset, "
            "or plane to change the correction. Legacy padding defaults to none."
        ),
    )
    parser.add_argument(
        "--zero-sample-policy",
        choices=("exclude", "include"),
        default="exclude",
        help="Exclude or include exact-zero samples in median and rank-fit stacks. Defaults to exclude.",
    )
    parser.add_argument(
        "--reference-frame-file",
        help="Use this FITS filename as the registration/WCS reference; overrides --reference-frame.",
    )
    parser.add_argument(
        "--preview-north-up",
        action="store_true",
        help="Add preview PNGs rotated using the solved WCS so celestial north is up.",
    )
    parser.add_argument(
        "--preview-sun-pa-left",
        action="store_true",
        help="Add a Metcalf preview PNG rotated using WCS so the SUN_PA direction is left.",
    )
    parser.add_argument(
        "--preview-at",
        choices=("none", "UL", "UR", "LL", "LR"),
        default=None,
        help="Draw N/E and Sun annotations at this preview corner. Moving mode defaults to UL; fixed mode to none.",
    )
    parser.add_argument(
        "--annotate-size",
        type=float,
        default=60.0,
        help="Annotation radius in pixels for --preview-at. Defaults to 60.",
    )
    parser.add_argument("--output-bitpix", choices=("float32", "uint16"), default=None)
    parser.add_argument(
        "--sun-pa",
        choices=("auto", "off"),
        default="auto",
        help="Write SUN_PA/ASUN_PA into the moving-target FITS when its Horizons observer metadata is available.",
    )
    parser.add_argument("--uint16-scale", choices=("none", "global", "per-channel"), default="none")
    parser.add_argument("--scale-low-percentile", type=float, default=0.0)
    parser.add_argument("--scale-high-percentile", type=float, default=100.0)
    parser.add_argument("--preview-low-percentile", type=float, default=5.0)
    parser.add_argument("--preview-high-percentile", type=float, default=99.95)
    parser.add_argument("--preview-stretch", choices=("percentile", "sigma"), default="sigma")
    parser.add_argument("--preview-sigma-low", type=float, default=-1.0)
    parser.add_argument("--preview-sigma-high", type=float, default=3.0)
    parser.add_argument(
        "--saturation-warning",
        type=str.lower,
        choices=("enable", "disable"),
        default=None,
        help="Enable or disable separate saturation-warning preview PNGs. Fixed mode defaults to enable.",
    )
    parser.add_argument(
        "--saturation-threshold-percent",
        type=float,
        default=90.0,
        help="Warn above this percentage of the FITS saturation level. Defaults to 90.",
    )
    parser.add_argument(
        "--saturation-color",
        default="FF0000",
        help="Warning overlay color as six-digit RGB hex. Defaults to FF0000.",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep intermediate image FITS files generated during Siril registration.",
    )
    parser.add_argument(
        "--output-prefix",
        help="Output filename stem. Defaults to '<OBJECT>_<start>-<end>_<N>frames'.",
    )
    parser.add_argument("--log-file", type=Path, help="Write the complete console log to this file.")
    parser.set_defaults(verbose=True, open_output=True)
    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", help="Show pipeline and per-frame progress (default).")
    parser.add_argument("--no-verbose", dest="verbose", action="store_false", help="Hide detailed pipeline and per-frame progress.")
    parser.add_argument("--open-output", dest="open_output", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-open-output", dest="open_output", action="store_false", help="Do not open the output directory after success.")
    args = parser.parse_args()
    if args.source_dir_arg and args.source_dir_option:
        parser.error("specify the source path either as the first argument or with --source-dir, not both")
    args.source_dir = args.source_dir_arg or args.source_dir_option
    if args.source_dir is None:
        parser.error("source directory or SharpCap stacklog.csv is required")
    delattr(args, "source_dir_arg")
    delattr(args, "source_dir_option")
    if not 1 <= args.rankfit_fraction <= 100:
        parser.error("--rankfit-fraction must be an integer from 1 to 100")
    if args.background_normalization is None:
        args.background_normalization = "none" if args.padding_policy == "legacy" else "quadratic"
    if args.output_bitpix is None:
        args.output_bitpix = "float32" if args.target_mode == "fixed" else "uint16"
    if args.saturation_warning is None:
        args.saturation_warning = "enable" if args.target_mode == "fixed" else "disable"
    if args.preview_at is None:
        args.preview_at = "none" if args.target_mode == "fixed" else "UL"
    if args.target_mode == "fixed" and args.preview_sun_pa_left:
        parser.error("--preview-sun-pa-left is available only in moving-target mode")
    if args.background_normalization != "none" and args.padding_policy != "valid":
        parser.error("--background-normalization offset, plane, and quadratic require --padding-policy valid")
    try:
        validate_output_region_options(
            args.output_region_mode,
            args.output_region_cover_count,
            args.output_region_cover_ratio,
            args.padding_policy,
        )
    except ValueError as error:
        parser.error(str(error))
    if not 0.0 < args.saturation_threshold_percent <= 100.0:
        parser.error("--saturation-threshold-percent must be greater than 0 and at most 100")
    if not args.hot_pixel_sigma > 0.0 or not args.cold_pixel_sigma > 0.0:
        parser.error("--hot-pixel-sigma and --cold-pixel-sigma must be greater than 0")
    try:
        args.saturation_color = normalize_saturation_color(args.saturation_color)
    except ValueError as error:
        parser.error(str(error))
    return args


def child_command(script_name: str, arguments: list[str]) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--internal-script", script_name, *arguments]
    return [sys.executable, str(Path(__file__).resolve().parent / script_name), *arguments]


def safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "target"


def iso_compact(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def split_sessions_by_gap(dated: list[tuple[datetime, Path]], gap_min: float | None) -> list[list[tuple[datetime, Path]]]:
    if not dated:
        return []
    if gap_min is None:
        return [dated]
    sessions: list[list[tuple[datetime, Path]]] = []
    current: list[tuple[datetime, Path]] = []
    previous_time: datetime | None = None
    gap_seconds = gap_min * 60.0
    for item in dated:
        if previous_time is not None and (item[0] - previous_time).total_seconds() > gap_seconds:
            if current:
                sessions.append(current)
            current = []
        current.append(item)
        previous_time = item[0]
    if current:
        sessions.append(current)
    return sessions


def parse_bounded_pair(text: str, offset: int, minimum: int, maximum: int, default: int) -> int:
    if len(text) < offset + 2:
        return default
    token = text[offset : offset + 2]
    if not token.isdigit():
        return default
    value = int(token)
    if value < minimum or value > maximum:
        return default
    return value


def parse_session_at(value: str) -> datetime:
    text = value.strip()
    date_part, separator, time_part = text.partition("-")
    if len(date_part) < 4 or not date_part[:4].isdigit():
        raise ValueError("--session-at requires at least a four-digit year")
    year = int(date_part[:4])
    month = parse_bounded_pair(date_part, 4, 1, 12, 1)
    max_day = calendar.monthrange(year, month)[1]
    day = parse_bounded_pair(date_part, 6, 1, max_day, 1)
    if not separator:
        time_part = ""
    hour = parse_bounded_pair(time_part, 0, 0, 23, 0)
    minute = parse_bounded_pair(time_part, 2, 0, 59, 0)
    second = parse_bounded_pair(time_part, 4, 0, 59, 0)
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    return datetime(year, month, day, hour, minute, second, tzinfo=local_tz).astimezone(timezone.utc)


def choose_session_index(
    sessions: list[list[tuple[datetime, Path]]],
    session_index: int | None,
    session_at: str | None,
) -> tuple[int, datetime | None]:
    if session_index is not None and session_at:
        raise SystemExit("--session-index and --session-at cannot be used together")
    if session_at:
        threshold = parse_session_at(session_at)
        for index, session in enumerate(sessions, start=1):
            if session[0][0] >= threshold:
                return index, threshold
        raise SystemExit(
            f"--session-at {session_at} did not match any session; "
            f"latest session starts at {sessions[-1][0][0].isoformat()}"
        )
    return (session_index if session_index is not None else len(sessions)), None


def repair_windows_cmd_path(path: Path) -> Path:
    text = str(path)
    for quote in ('"', "'"):
        if quote in text:
            prefix = text.split(quote, 1)[0]
            repaired = Path(prefix)
            if repaired.exists():
                print(f"Repaired source path: {path} -> {repaired}", file=sys.stderr)
                return repaired
    if text.endswith('"') or text.endswith("'"):
        repaired = Path(text.rstrip("\"'"))
        if repaired.exists():
            print(f"Repaired source path: {path} -> {repaired}", file=sys.stderr)
            return repaired
    return path


def looks_like_stacked_outputs(files: list[Path]) -> bool:
    fits_files = [path for path in files if is_fits_frame(path)]
    return bool(fits_files) and all(path.name.lower().startswith("stacked_") for path in fits_files)


def is_fits_frame(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() not in {".fit", ".fits"}:
        return False
    stem = path.stem.casefold()
    return not stem.endswith(("_siril_wcs", "_wcs", "_upload_sanitized"))


def resolve_source_dir(source_dir: Path, pattern: str) -> Path:
    source_dir = repair_windows_cmd_path(source_dir)
    files = sorted((path for path in source_dir.glob(pattern) if is_fits_frame(path)), key=lambda p: p.name) if source_dir.exists() else []
    sub_candidate = source_dir.with_name(f"{source_dir.name}_sub")
    sub_files = sorted((path for path in sub_candidate.glob(pattern) if is_fits_frame(path)), key=lambda p: p.name) if sub_candidate.exists() else []
    if sub_files and (not files or looks_like_stacked_outputs(files)):
        print(f"Using subframe directory: {sub_candidate}", file=sys.stderr)
        return sub_candidate
    return source_dir


def is_failed_frame(path: Path) -> bool:
    return "_failed_" in path.name.lower()


def load_dated_files(source_dir: Path, pattern: str, include_failed_frames: bool = False) -> list[tuple[datetime, Path]]:
    files = sorted((path for path in source_dir.glob(pattern) if is_fits_frame(path)), key=lambda p: p.name)
    if not files:
        raise FileNotFoundError(f"No files matching {pattern} in {source_dir}")
    if not include_failed_frames:
        original_count = len(files)
        files = [path for path in files if not is_failed_frame(path)]
        skipped = original_count - len(files)
        if skipped:
            print(f"Skipped {skipped} failed frame(s); use --include-failed-frames to keep them.", file=sys.stderr)
    if not files:
        raise FileNotFoundError(f"No non-failed files matching {pattern} in {source_dir}")
    dated: list[tuple[datetime, Path]] = []
    for path in files:
        header, _cards, _offset = read_fits_header(path)
        if "DATE-OBS" not in header:
            continue
        dated.append((parse_time(header["DATE-OBS"]), path))
    dated.sort(key=lambda item: item[0])
    if not dated:
        raise FileNotFoundError(f"No files with DATE-OBS matching {pattern} in {source_dir}")
    return dated


def load_sessions(args: argparse.Namespace) -> list[list[tuple[datetime, Path]]]:
    sharpcap = load_sharpcap_session(
        args.source_dir,
        include_rejected=getattr(args, "include_sharpcap_rejected", False),
    )
    if sharpcap is not None:
        args.sharpcap_session = sharpcap
        args.source_dir = sharpcap.root
        dated = [(frame.time, frame.path) for frame in sharpcap.frames]
        if args.after:
            after_time = parse_time(args.after)
            dated = [item for item in dated if item[0] >= after_time]
        if args.before:
            before_time = parse_time(args.before)
            dated = [item for item in dated if item[0] <= before_time]
        return split_sessions_by_gap(dated, args.session_gap_min)
    args.source_dir = resolve_source_dir(args.source_dir, args.pattern)
    dated = load_dated_files(args.source_dir, args.pattern, args.include_failed_frames)
    if args.after:
        after_time = parse_time(args.after)
        dated = [item for item in dated if item[0] >= after_time]
    if args.before:
        before_time = parse_time(args.before)
        dated = [item for item in dated if item[0] <= before_time]
    return split_sessions_by_gap(dated, args.session_gap_min)


def discover_preprocessing_settings(
    source_dir: Path,
    sharpcap_session: object | None,
) -> tuple[Path | None, dict[str, str], Path]:
    """Find CameraSettings independently of SharpCap StackLog metadata."""
    if sharpcap_session is not None:
        return (
            sharpcap_session.settings_file,
            sharpcap_session.settings,
            sharpcap_session.root,
        )
    settings_file, settings = read_settings(source_dir, source_dir.parent)
    return settings_file, settings, source_dir


def print_session_table(
    args: argparse.Namespace,
    sessions: list[list[tuple[datetime, Path]]],
    selected_index: int | None = None,
    include_guidance: bool = True,
) -> None:
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    print(f"Source: {args.source_dir}")
    print(f"Session gap: {args.session_gap_min:g} minutes")
    print("Index  Frames  Local start           Local end             UTC start")
    for index, session in enumerate(sessions, start=1):
        start, end = session[0][0], session[-1][0]
        if selected_index is not None:
            marker = "  <- selected" if index == selected_index else ""
        else:
            marker = "  <- default (latest)" if index == len(sessions) else ""
        print(
            f"{index:>5}  {len(session):>6}  "
            f"{start.astimezone(local_tz):%Y-%m-%d %H:%M:%S}  "
            f"{end.astimezone(local_tz):%Y-%m-%d %H:%M:%S}  "
            f"{start:%Y-%m-%d %H:%M:%S}Z{marker}"
        )
    if not include_guidance:
        print(flush=True)
        return
    quoted_source = f'"{args.source_dir}"'
    if os.name == "nt":
        launcher = "seestar-fixed-stack.cmd" if args.target_mode == "fixed" else "seestar-metcalf-stack.cmd"
        mode_argument = ""
    elif sys.platform == "darwin":
        launcher = "./seestar-metcalf-stack.sh"
        mode_argument = " --target-mode fixed" if args.target_mode == "fixed" else ""
    else:
        launcher = "./seestar-metcalf-stack.sh"
        mode_argument = " --target-mode fixed" if args.target_mode == "fixed" else ""
    print("\nSelect by number:")
    print(f"  {launcher} {quoted_source}{mode_argument} --session-index N")
    print("Select the first session starting at or after a local date/time:")
    print(f"  {launcher} {quoted_source}{mode_argument} --session-at YYYYMMDD-hhmmss")


def print_sessions(args: argparse.Namespace) -> None:
    sessions = load_sessions(args)
    if not sessions:
        raise FileNotFoundError("No files remain after time filtering")
    print_session_table(args, sessions)


def resolve_session(args: argparse.Namespace) -> tuple[int, list[Path], dict[str, object]]:
    sessions = load_sessions(args)
    if not sessions:
        raise FileNotFoundError("No files remain after time/session filtering")
    session_index, session_at_time = choose_session_index(sessions, args.session_index, args.session_at)
    if session_index < 1 or session_index > len(sessions):
        raise SystemExit(f"--session-index {session_index} is out of range; found {len(sessions)} session(s)")
    if args.verbose:
        print_session_table(args, sessions, selected_index=session_index, include_guidance=False)
    selected = sessions[session_index - 1]
    files = [path for _when, path in selected]
    if args.count is not None:
        files = files[: args.count]
    if not files:
        raise FileNotFoundError("No files remain after time/session filtering")
    session_info = {
        "session_gap_min": args.session_gap_min,
        "session_index": session_index,
        "session_count": len(sessions),
        "selected_frame_count": len(files),
        "selected_first_time": selected[0][0].isoformat(),
        "selected_last_time": selected[-1][0].isoformat(),
        "include_failed_frames": args.include_failed_frames,
        "session_at": args.session_at,
        "session_at_utc": session_at_time.isoformat() if session_at_time else None,
    }
    return session_index, files, session_info


def select_pipeline_reference_index(args: argparse.Namespace, files: list[Path]) -> int:
    sharpcap = getattr(args, "sharpcap_session", None)
    if sharpcap is None:
        return select_reference_index(files, args.reference_frame, args.reference_frame_file)
    if args.reference_frame_file:
        requested = Path(args.reference_frame_file).name.casefold()
        matches = [index for index, path in enumerate(files, start=1) if path.name.casefold() == requested]
        if len(matches) != 1:
            raise ValueError(f"--reference-frame-file matched {len(matches)} selected SharpCap frame(s): {requested}")
        return matches[0]
    if args.reference_frame == "first":
        return 1
    frame_times = {
        frame.path.resolve(): frame.time for frame in sharpcap.frames
    }
    dated = [(frame_times[path.resolve()], index) for index, path in enumerate(files, start=1)]
    midpoint = dated[0][0] + (dated[-1][0] - dated[0][0]) / 2
    return min(dated, key=lambda item: (abs((item[0] - midpoint).total_seconds()), item[0]))[1]


def read_object_name(frame: Path) -> str:
    if not is_fits_frame(frame):
        return frame.parent.parent.parent.name or frame.parent.name
    header, _cards, _offset = read_fits_header(frame)
    value = header.get("OBJECT")
    return str(value).strip() if value else frame.parent.name


def validate_sharpcap_inputs(args: argparse.Namespace, reference_frame: Path) -> None:
    if getattr(args, "sharpcap_session", None) is None or is_fits_frame(reference_frame):
        return
    has_existing_ephemeris = args.ephemeris_csv is not None and args.ephemeris_csv.is_file()
    if args.target_mode == "moving" and not (has_existing_ephemeris or args.horizons_object or args.horizons_command):
        raise ValueError(
            "SharpCap PNG/TIFF input requires the moving target to be specified with "
            "--horizons-object, --horizons-command, or --ephemeris-csv."
        )
    if args.pixel_scale_arcsec is None:
        raise ValueError(
            "SharpCap PNG/TIFF input requires --pixel-scale-arcsec because the image does not "
            "provide a reliable plate-solve scale."
        )


def default_ephemeris_path(args: argparse.Namespace, first_frame: Path, session_index: int) -> Path:
    sharpcap = getattr(args, "sharpcap_session", None)
    object_name = sharpcap.object_name if sharpcap is not None else read_object_name(first_frame)
    if sharpcap is not None:
        frame = next(item for item in sharpcap.frames if item.path.resolve() == first_frame.resolve())
        when = frame.time
    else:
        header, _cards, _offset = read_fits_header(first_frame)
        when = parse_time(header["DATE-OBS"]) if "DATE-OBS" in header else datetime.now(timezone.utc)
    stem = f"{safe_name(object_name)}_{iso_compact(when)}_session{session_index}_horizons_{args.horizons_center}.csv"
    return args.work_dir / stem


def default_work_name(first_frame: Path, stack_method: str, rankfit_fraction: int) -> str:
    method = processing_method_token(stack_method, rankfit_fraction)
    return f"{safe_name(read_object_name(first_frame))}_{method}"


def stage_file(path: Path, work_dir: Path) -> Path:
    source = path.resolve()
    destination = work_dir / path.name
    if destination.exists() and destination.resolve() == source:
        return destination
    if source.parent == work_dir.resolve():
        return path
    shutil.copy2(path, destination)
    return destination


def make_work_dir(base: Path, name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work_dir = base / f"{name}-{stamp}"
    work_dir.mkdir(parents=True, exist_ok=False)
    return work_dir


def prepare_work_dir(args: argparse.Namespace, first_frame: Path) -> Path:
    if not args.work_name:
        sharpcap = getattr(args, "sharpcap_session", None)
        object_name = sharpcap.object_name if sharpcap is not None else read_object_name(first_frame)
        method = processing_method_token(args.stack_method, args.rankfit_fraction)
        mode = "fixed_" if args.target_mode == "fixed" else ""
        args.work_name = f"{safe_name(object_name)}_{mode}{method}"
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        return args.work_dir
    return make_work_dir(args.work_root, args.work_name)


def ensure_ephemeris(args: argparse.Namespace, first_frame: Path, session_index: int) -> Path:
    if args.ephemeris_csv and args.ephemeris_csv.exists():
        return stage_file(args.ephemeris_csv, args.work_dir)
    ephemeris_csv = args.work_dir / args.ephemeris_csv.name if args.ephemeris_csv else default_ephemeris_path(args, first_frame, session_index)
    if ephemeris_csv.exists():
        return ephemeris_csv
    if args.no_auto_ephemeris:
        raise FileNotFoundError(f"Ephemeris CSV not found: {ephemeris_csv}")

    cmd = child_command(
        "horizons_ephemeris.py",
        [
            "--source-dir",
            str(args.source_dir),
            "--output",
            str(ephemeris_csv),
            "--center",
            args.horizons_center,
            "--chunk-size",
            str(args.horizons_chunk_size),
            "--retries",
            str(args.horizons_retries),
        ],
    )
    if args.horizons_center == "fits-site":
        cmd.append("--allow-site-upload")
    if args.site_longitude is not None:
        cmd.extend(["--site-longitude", str(args.site_longitude)])
    if args.site_latitude is not None:
        cmd.extend(["--site-latitude", str(args.site_latitude)])
    if args.verbose:
        cmd.append("--verbose")
    if args.horizons_object:
        cmd.extend(["--object", args.horizons_object])
    if args.horizons_command:
        cmd.extend(["--command", args.horizons_command])
    if args.include_failed_frames:
        cmd.append("--include-failed-frames")
    if args.frame_manifest:
        cmd.extend(["--frame-manifest", str(args.frame_manifest)])
    if args.after:
        cmd.extend(["--after", args.after])
    if args.before:
        cmd.extend(["--before", args.before])
    if args.session_gap_min is not None and not args.frame_manifest:
        cmd.extend(["--session-gap-min", str(args.session_gap_min), "--session-index", str(session_index)])
    print(
        "Auto-generating Horizons ephemeris CSV. "
        f"center={args.horizons_center}; output={ephemeris_csv}",
        flush=True,
    )
    run(cmd, REPO_ROOT)
    return ephemeris_csv


def run(cmd: list[str], cwd: Path) -> None:
    print("+ " + " ".join(f'"{item}"' if " " in item else item for item in cmd), flush=True)
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout is None:
        raise RuntimeError("Child process output pipe was not created")
    output: list[str] = []
    with process.stdout:
        for line in process.stdout:
            output.append(line)
            write_console_safe(line)
    return_code = process.wait()
    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, cmd, output="".join(output))


def write_console_safe(text: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, end="", flush=True)


def verbose(args: argparse.Namespace, message: str) -> None:
    if args.verbose:
        print(f"[pipeline] {message}", flush=True)


def parse_stack_summary(output: str) -> dict[str, object]:
    for match in reversed(list(re.finditer(r"(?m)^\{", output))):
        try:
            parsed = json.loads(output[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise RuntimeError("moving_target_stack.py did not print JSON summary")


def child_error_message(output: str) -> str:
    explicit = re.findall(r"(?m)^ERROR:\s*(.+)$", output)
    if explicit:
        return explicit[-1].strip()
    if "No image was registered to the reference" in output:
        return (
            "Background-star registration failed: Siril could not align any frame to the selected reference. "
            "Choose a sharper frame with more detected stars using --reference-frame-file; "
            "see registration_diagnostics.csv in the run output directory."
        )
    if "Not enough free disk space" in output:
        return "Siril ran out of disk space while registering frames. Free space or select another --work-root."
    return "The stacking worker failed. See the run log and registration_diagnostics.csv for details."


def friendly_exception_message(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        output = str(error.output or "")
        return child_error_message(output)
    text = str(error).strip()
    if not text:
        return error.__class__.__name__
    return " ".join(text.splitlines())


def sanitize_fits_for_upload(source: Path, destination: Path) -> Path:
    """Copy FITS while blanking observing-site cards before external upload."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(source.read_bytes())
    for offset in range(0, min(len(data), 2880 * 32), 80):
        card = bytes(data[offset : offset + 80]).decode("ascii", errors="ignore")
        key = card[:8].strip()
        if key == "END":
            break
        if key in PRIVACY_FITS_KEYS:
            data[offset : offset + 80] = b" " * 80
    destination.write_bytes(data)
    return destination


def is_valid_fits(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 80:
        return False
    with path.open("rb") as handle:
        return handle.read(8) == b"SIMPLE  "


def is_valid_wcs_fits(path: Path) -> bool:
    if not is_valid_fits(path):
        return False
    try:
        header, _cards, _offset = read_fits_header(path)
    except (OSError, ValueError):
        return False
    required = {"CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2"}
    if not required.issubset(header):
        return False
    try:
        wcs_cd_matrix(header)
    except ValueError:
        return False
    return True


def is_valid_astrometry_json(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    calibration = payload.get("calibration") or payload.get("results", {}).get("calibration")
    if not isinstance(calibration, dict):
        return False
    return {"ra", "dec", "pixscale", "orientation"}.issubset(calibration)


def solve_cache_paths(args: argparse.Namespace, reference_frame: Path) -> tuple[Path, Path]:
    cache_dir = args.solve_dir or reference_frame.parent
    prefix = args.solve_name or reference_frame.stem
    return cache_dir / f"{prefix}_astrometry.json", cache_dir / f"{prefix}_wcs.fits"


def cached_submission_id(json_path: Path) -> str | None:
    submission_path = json_path.with_name(f"{json_path.stem}_submission.json")
    if not submission_path.exists():
        return None
    try:
        payload = json.loads(submission_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    subid = str(payload.get("subid", "")).strip()
    return subid if subid.isdigit() else None


SIRIL_NEAR_SCALE_FACTORS = (1.0, 0.70, 1.40, 0.50, 2.00)
SIRIL_WIDE_SCALE_FACTORS = (0.35, 2.80, 0.25, 4.00)


def positive_float(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


def parse_sexagesimal(value: str, *, is_ra: bool) -> float:
    text = value.strip()
    if ":" not in text and not any(character.isspace() for character in text):
        return float(text)
    sign = -1.0 if text.startswith("-") else 1.0
    parts = [float(part) for part in text.lstrip("+-").replace(":", " ").split()]
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Unsupported coordinate: {value}")
    absolute = parts[0] + (parts[1] / 60.0 if len(parts) > 1 else 0.0) + (parts[2] / 3600.0 if len(parts) > 2 else 0.0)
    return sign * absolute * (15.0 if is_ra else 1.0)


def infer_solve_center(header: dict[str, object], args: argparse.Namespace) -> tuple[float, float]:
    ra_value = args.solve_center_ra_deg
    dec_value = args.solve_center_dec_deg
    if ra_value is None:
        ra_value = header.get("RA", header.get("OBJCTRA", header.get("CRVAL1")))
    if dec_value is None:
        dec_value = header.get("DEC", header.get("OBJCTDEC", header.get("CRVAL2")))
    if ra_value is None or dec_value is None:
        raise ValueError(
            "The reference frame has no approximate RA/Dec for Siril plate solving; "
            "provide --solve-center-ra-deg and --solve-center-dec-deg."
        )
    ra = float(ra_value) if isinstance(ra_value, (int, float)) else parse_sexagesimal(str(ra_value), is_ra=True)
    dec = float(dec_value) if isinstance(dec_value, (int, float)) else parse_sexagesimal(str(dec_value), is_ra=False)
    if not math.isfinite(ra) or not math.isfinite(dec) or not -90.0 <= dec <= 90.0:
        raise ValueError(f"Invalid plate-solve center: RA={ra}, Dec={dec}")
    return ra % 360.0, dec


def embed_explicit_solve_center(header: dict[str, object], args: argparse.Namespace) -> None:
    """Persist CLI center hints when a raster image is converted to temporary FITS."""
    if args.solve_center_ra_deg is None and args.solve_center_dec_deg is None:
        return
    ra, dec = infer_solve_center({}, args)
    header["RA"] = ra
    header["DEC"] = dec


def infer_solve_scale(header: dict[str, object], explicit: float | None) -> float:
    if explicit is not None:
        if not math.isfinite(explicit) or explicit <= 0.0:
            raise ValueError("--pixel-scale-arcsec must be a positive finite number")
        return explicit
    hint = estimate_scale_hint(header)
    if hint and positive_float(hint.get("arcsecPerPix")):
        return float(hint["arcsecPerPix"])
    try:
        cd11, cd12, cd21, cd22 = wcs_cd_matrix(header)
        return (math.hypot(cd11, cd21) + math.hypot(cd12, cd22)) * 1800.0
    except ValueError as error:
        raise ValueError(
            "The reference frame has no reliable image scale for Siril plate solving; "
            "provide --pixel-scale-arcsec."
        ) from error


def infer_effective_pixel_size(header: dict[str, object]) -> float:
    values: list[float] = []
    for size_key, bin_keys in (("XPIXSZ", ("XBINNING", "CCDXBIN")), ("YPIXSZ", ("YBINNING", "CCDYBIN"))):
        size = positive_float(header.get(size_key))
        if size is None:
            continue
        binning = next((positive_float(header.get(key)) for key in bin_keys if positive_float(header.get(key))), 1.0)
        values.append(size * float(binning or 1.0))
    return sum(values) / len(values) if values else 1.0


def prepare_reference_with_siril(args: argparse.Namespace, reference_frame: Path) -> Path:
    image = read_source_image(reference_frame, args.bayer_pattern, debayer=False)
    pattern = str(image.header.get("BAYERPAT") or image.header.get("COLORTYP") or "").strip()
    cfa = image.data.ndim == 2 and bool(pattern)
    plan: PreprocessingPlan = args.preprocessing_plan
    if not cfa and not plan.enabled:
        return stage_file(reference_frame, args.work_dir)

    if is_fits_frame(reference_frame):
        staged = args.work_dir / f"{reference_frame.stem}_solve_input.fit"
        shutil.copy2(reference_frame, staged)
    else:
        staged = args.work_dir / f"{reference_frame.stem}_solve_input.fit"
        sharpcap = getattr(args, "sharpcap_session", None)
        frame = next(item for item in sharpcap.frames if item.path.resolve() == reference_frame.resolve())
        embed_explicit_solve_center(image.header, args)
        write_registered_float(
            staged,
            image,
            frame.time,
            sharpcap.object_name,
            sharpcap.exposure_seconds,
        )
    corrected = args.work_dir / f"cc_{staged.name}"
    staged_plan = stage_preprocessing_files(plan, args.work_dir)
    script_text, output_name = build_single_preprocess_script(
        staged,
        staged_plan,
        cfa=cfa,
        corrected_intermediate=corrected,
    )
    script = args.work_dir / "prepare_plate_solve_reference.ssf"
    script.write_text(script_text, encoding="ascii")
    verbose(args, f"Siril preprocessing reference frame: cfa={'yes' if cfa else 'no'}")
    run_siril(args.siril, args.work_dir, script, args.verbose)
    output = args.work_dir / output_name
    if not output.is_file():
        raise RuntimeError(f"Siril did not write the preprocessed reference frame: {output}")
    return output


def siril_wcs_cache_path(args: argparse.Namespace, reference_frame: Path) -> Path:
    cache_dir = args.solve_dir or reference_frame.parent
    prefix = args.solve_name or reference_frame.stem
    return cache_dir / f"{prefix}_siril_wcs.fits"


def plate_solution_source(wcs_fits: Path | None, astrometry_json: Path | None) -> str | None:
    if astrometry_json is not None:
        return "astrometry.net"
    if wcs_fits is None:
        return None
    name = Path(wcs_fits).name.casefold()
    if name.endswith("_siril_wcs.fits") or name.endswith("_siril_wcs.fit"):
        return "siril"
    if name.endswith("_wcs.fits") or name.endswith("_wcs.fit"):
        return "astrometry.net"
    return "explicit-wcs"


def build_siril_plate_solve_script(
    input_fits: Path,
    output_fits: Path,
    center: tuple[float, float],
    focal_mm: float,
    pixel_size_um: float,
    catalog: str | None,
) -> str:
    catalog_option = f" -catalog={catalog}" if catalog else ""
    return "\n".join(
        [
            "requires 1.4.0",
            f"load {quote_siril_argument(input_fits.name)}",
            (
                # Siril 1.4.1 rejects explicit coordinates in batch scripts.
                # The validated approximate center is retained in the loaded
                # FITS header, so only sampling overrides are passed here.
                f"platesolve -force -focal={focal_mm:.12g} "
                f"-pixelsize={pixel_size_um:.12g} -noflip{catalog_option}"
            ),
            f"save {quote_siril_argument(output_fits.name)}",
            "close",
            "",
        ]
    )


def siril_detected_star_count(output: str) -> int | None:
    counts = [
        int(value)
        for value in re.findall(
            r"(?:Found|Using)\s+(\d+)\s+(?:(?:Gaussian profile|detected)\s+)?stars?",
            output,
            re.IGNORECASE,
        )
    ]
    return max(counts) if counts else None


def siril_catalog_service_unavailable(output: str) -> bool:
    """Return true only for a retryable VizieR HTTP 503 response."""
    return re.search(r"HTTP(?:\s+code)?\s+503\b", output, re.IGNORECASE) is not None


def astrometry_api_key_is_configured() -> bool:
    try:
        read_api_key()
    except (OSError, RuntimeError):
        return False
    return True


def astrometry_api_key_setup_message() -> str:
    if os.name == "nt":
        command = r".\set-astrometry-api-key.cmd YOUR_API_KEY"
    else:
        command = "./set-astrometry-api-key.sh YOUR_API_KEY"
    return (
        "Astrometry.net API key is not configured. Obtain an API key from "
        "https://nova.astrometry.net/api_help, then run " + command + "."
    )


def try_siril_plate_solve(
    args: argparse.Namespace,
    input_fits: Path,
    reference_frame: Path,
    factors: tuple[float, ...],
) -> tuple[Path | None, int | None, list[str], bool]:
    header, _cards, _offset = read_fits_header(input_fits)
    center = infer_solve_center(header, args)
    scale = infer_solve_scale(header, args.pixel_scale_arcsec)
    pixel_size = infer_effective_pixel_size(header)
    errors: list[str] = []
    detected_stars: int | None = None
    unavailable_factors = 0
    for attempt, factor in enumerate(factors, start=1):
        supplied_scale = scale * factor
        focal = 206.265 * pixel_size / supplied_scale
        candidate = args.work_dir / f"siril_solve_{attempt:02d}_{factor:g}x.fit"
        script = args.work_dir / f"siril_plate_solve_{attempt:02d}.ssf"
        script.write_text(
            build_siril_plate_solve_script(input_fits, candidate, center, focal, pixel_size, args.siril_catalog),
            encoding="ascii",
        )
        verbose(
            args,
            f"Siril plate solve attempt {attempt}/{len(factors)}: scale={supplied_scale:.6g} arcsec/pixel ({factor:g}x)",
        )
        output = ""
        last_error: SirilRegistrationError | None = None
        for catalog_attempt in range(1, SIRIL_CATALOG_RETRIES + 1):
            try:
                output = run_siril(args.siril, args.work_dir, script, args.verbose)
                last_error = None
                break
            except SirilRegistrationError as error:
                output = error.output
                last_error = error
                if not siril_catalog_service_unavailable(output) or catalog_attempt >= SIRIL_CATALOG_RETRIES:
                    break
                delay = SIRIL_CATALOG_RETRY_DELAY_SEC * (2 ** (catalog_attempt - 1))
                print(
                    f"Siril catalog server returned HTTP 503; retrying the same {factor:g}x scale "
                    f"in {delay:g}s ({catalog_attempt + 1}/{SIRIL_CATALOG_RETRIES}).",
                    file=sys.stderr,
                    flush=True,
                )
                for partial in (candidate, candidate.with_suffix(".fits")):
                    partial.unlink(missing_ok=True)
                time.sleep(delay)
        if last_error is not None:
            if siril_catalog_service_unavailable(output):
                unavailable_factors += 1
                errors.append(
                    f"{factor:g}x: VizieR catalog server remained unavailable (HTTP 503) "
                    f"after {SIRIL_CATALOG_RETRIES} attempts"
                )
            else:
                errors.append(f"{factor:g}x: {str(last_error)}")
        count = siril_detected_star_count(output)
        if count is not None:
            detected_stars = max(detected_stars or 0, count)
        actual = candidate if candidate.is_file() else candidate.with_suffix(".fits")
        if is_valid_wcs_fits(actual):
            cache = siril_wcs_cache_path(args, reference_frame)
            cache.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(actual, cache)
            print(f"Siril plate solve succeeded at {factor:g}x scale: {cache}", flush=True)
            return cache, detected_stars, errors, False
        errors.append(f"{factor:g}x: no usable WCS output")
    return None, detected_stars, errors, unavailable_factors == len(factors)


def solve_first_frame(args: argparse.Namespace, first_frame: Path) -> tuple[Path | None, Path | None]:
    if args.wcs_fits:
        if not args.wcs_fits.exists():
            raise FileNotFoundError(f"WCS FITS not found: {args.wcs_fits}")
        staged_wcs = stage_file(args.wcs_fits, args.work_dir)
        staged_json = stage_file(args.astrometry_json, args.work_dir) if args.astrometry_json else None
        return staged_wcs, staged_json
    if args.astrometry_json:
        if not args.astrometry_json.exists():
            raise FileNotFoundError(f"Astrometry JSON not found: {args.astrometry_json}")
        return None, stage_file(args.astrometry_json, args.work_dir)

    siril_cache = siril_wcs_cache_path(args, first_frame)
    if is_valid_wcs_fits(siril_cache):
        print(f"Reusing cached Siril WCS: {siril_cache}", flush=True)
        return siril_cache, None

    json_path, wcs_path = solve_cache_paths(args, first_frame)
    valid_json = is_valid_astrometry_json(json_path)
    if is_valid_wcs_fits(wcs_path):
        print(f"Reusing cached Astrometry.net WCS: {wcs_path}", flush=True)
        return wcs_path, json_path if valid_json else None
    if valid_json:
        print(f"Reusing cached Astrometry.net calibration: {json_path}", flush=True)
        return None, json_path
    if args.skip_solve:
        raise SystemExit("--skip-solve requested, but no valid explicit or cached Siril/Astrometry.net solution was found")

    prepared_reference: Path | None = None
    siril_errors: list[str] = []
    siril_catalog_unavailable = False
    detected_stars: int | None = None
    if args.plate_solver in {"auto", "siril"}:
        try:
            prepared_reference = prepare_reference_with_siril(args, first_frame)
            factors = SIRIL_NEAR_SCALE_FACTORS + (SIRIL_WIDE_SCALE_FACTORS if args.plate_solver == "siril" else ())
            solved, detected_stars, siril_errors, siril_catalog_unavailable = try_siril_plate_solve(
                args,
                prepared_reference,
                first_frame,
                factors,
            )
            if solved:
                return solved, None
            if detected_stars is not None and detected_stars < args.registration_minpairs:
                raise RuntimeError(
                    f"Siril detected only {detected_stars} star(s) in the reference frame; "
                    f"background registration requires {args.registration_minpairs}. "
                    "Choose a clearer reference frame instead of widening the plate-solve search."
                )
        except (OSError, ValueError, SirilRegistrationError) as error:
            siril_errors.append(str(error))
        if args.plate_solver == "siril":
            details = "; ".join(siril_errors[-5:]) or "no usable WCS output"
            raise RuntimeError(f"Siril plate solving failed after scale search: {details}")

    if args.plate_solver in {"auto", "astrometry"} and not astrometry_api_key_is_configured():
        if siril_catalog_unavailable:
            raise RuntimeError(
                "Siril could not obtain a star catalog because VizieR kept returning HTTP 503. "
                "The configured retries were exhausted, and the Astrometry.net fallback cannot run. "
                + astrometry_api_key_setup_message()
            )
        if args.plate_solver == "astrometry":
            raise RuntimeError(astrometry_api_key_setup_message())

    json_path.parent.mkdir(parents=True, exist_ok=True)
    astrometry_input = prepared_reference or first_frame
    if is_fits_frame(astrometry_input):
        upload_frame = sanitize_fits_for_upload(
            astrometry_input,
            args.work_dir / f"{first_frame.stem}_upload_sanitized.fit",
        )
    else:
        upload_frame = stage_file(astrometry_input, args.work_dir)
    solve_command = child_command(
        "astrometry_solve.py",
        [str(upload_frame), str(json_path), str(wcs_path)],
    )
    if args.pixel_scale_arcsec is not None:
        solve_command.extend(["--pixel-scale-arcsec", str(args.pixel_scale_arcsec)])
    if args.solve_center_ra_deg is not None:
        solve_command.extend(["--center-ra-deg", str(args.solve_center_ra_deg)])
    if args.solve_center_dec_deg is not None:
        solve_command.extend(["--center-dec-deg", str(args.solve_center_dec_deg)])
    resume_subid = cached_submission_id(json_path)
    if resume_subid:
        print(f"Resuming cached Astrometry.net submission {resume_subid} for {first_frame.name}.", flush=True)
        solve_command.append(resume_subid)
    else:
        print(f"No valid cached plate solve for {first_frame.name}; uploading to Astrometry.net.", flush=True)
    astrometry_error: Exception | None = None
    try:
        run(solve_command, REPO_ROOT)
    except (OSError, subprocess.CalledProcessError) as error:
        astrometry_error = error
    if is_valid_wcs_fits(wcs_path):
        return wcs_path, json_path

    if is_valid_astrometry_json(json_path):
        print(f"WCS FITS was not usable; falling back to Astrometry.net JSON calibration: {json_path}", file=sys.stderr)
        return None, json_path

    if args.plate_solver == "auto" and prepared_reference is not None:
        print("Astrometry.net was unavailable or did not solve; extending the Siril scale search.", file=sys.stderr, flush=True)
        solved, wide_stars, wide_errors, _wide_catalog_unavailable = try_siril_plate_solve(
            args,
            prepared_reference,
            first_frame,
            SIRIL_WIDE_SCALE_FACTORS,
        )
        if solved:
            return solved, None
        detected_stars = max(value for value in (detected_stars, wide_stars) if value is not None) if any(
            value is not None for value in (detected_stars, wide_stars)
        ) else None
        siril_errors.extend(wide_errors)
    # A completed but unusable job should not trap future runs into resuming the
    # same failed submission forever. Interrupted/network-failed runs never
    # reach here, so their checkpoint remains available for resume.
    submission_path = json_path.with_name(f"{json_path.stem}_submission.json")
    if resume_subid and submission_path.exists():
        submission_path.unlink()
    details = []
    if astrometry_error:
        details.append(f"Astrometry.net: {friendly_exception_message(astrometry_error)}")
    if siril_errors:
        details.append("Siril: " + "; ".join(siril_errors[-5:]))
    suffix = " " + " | ".join(details) if details else ""
    raise RuntimeError(f"Plate solving completed without a usable WCS for {first_frame}.{suffix}")


def run_stack(
    args: argparse.Namespace,
    ephemeris_csv: Path | None,
    wcs_fits: Path | None,
    astrometry_json: Path | None,
) -> dict[str, object]:
    cmd = child_command(
        "moving_target_stack.py",
        [
            "--source-dir",
            str(args.source_dir),
            "--target-mode",
            args.target_mode,
            "--work-dir",
            str(args.work_dir),
            "--registration-transform",
            args.registration_transform,
            "--registration-minpairs",
            str(args.registration_minpairs),
            "--stack-method",
            args.stack_method,
            "--stack-workers",
            str(args.stack_workers),
            "--median-tile-rows",
            str(args.median_tile_rows),
            "--output-region-mode",
            args.output_region_mode,
            "--padding-policy",
            args.padding_policy,
            "--background-normalization",
            args.background_normalization,
            "--zero-sample-policy",
            args.zero_sample_policy,
            "--rankfit-fraction",
            str(args.rankfit_fraction),
            "--reference-frame",
            args.reference_frame,
            "--output-bitpix",
            args.output_bitpix,
            "--sun-pa",
            args.sun_pa,
            "--uint16-scale",
            args.uint16_scale,
            "--scale-low-percentile",
            str(args.scale_low_percentile),
            "--scale-high-percentile",
            str(args.scale_high_percentile),
            "--preview-low-percentile",
            str(args.preview_low_percentile),
            "--preview-high-percentile",
            str(args.preview_high_percentile),
            "--preview-stretch",
            args.preview_stretch,
            "--preview-sigma-low",
            str(args.preview_sigma_low),
            "--preview-sigma-high",
            str(args.preview_sigma_high),
            "--saturation-warning",
            args.saturation_warning,
            "--saturation-threshold-percent",
            str(args.saturation_threshold_percent),
            "--saturation-color",
            args.saturation_color,
        ],
    )
    if args.output_region_cover_count is not None:
        cmd.extend(["--output-region-cover-count", str(args.output_region_cover_count)])
    if args.output_region_cover_ratio is not None:
        cmd.extend(["--output-region-cover-ratio", str(args.output_region_cover_ratio)])
    if ephemeris_csv is not None:
        cmd.extend(["--ephemeris-csv", str(ephemeris_csv)])
    if args.pattern:
        cmd.extend(["--pattern", args.pattern])
    if args.frame_manifest:
        cmd.extend(["--frame-manifest", str(args.frame_manifest)])
    if args.preprocessing_plan_file:
        cmd.extend(["--preprocessing-plan", str(args.preprocessing_plan_file)])
    if args.bayer_pattern:
        cmd.extend(["--bayer-pattern", args.bayer_pattern])
    if args.output_prefix:
        cmd.extend(["--output-prefix", args.output_prefix])
    if wcs_fits:
        cmd.extend(["--wcs-fits", str(wcs_fits)])
    if astrometry_json:
        cmd.extend(["--astrometry-json", str(astrometry_json)])
    solution_source = plate_solution_source(wcs_fits, astrometry_json)
    if solution_source:
        cmd.extend(["--plate-solver-name", solution_source])
    if args.count is not None:
        cmd.extend(["--count", str(args.count)])
    if args.reference_frame_file:
        cmd.extend(["--reference-frame-file", args.reference_frame_file])
    if args.after:
        cmd.extend(["--after", args.after])
    if args.before:
        cmd.extend(["--before", args.before])
    if args.session_gap_min is not None and not args.frame_manifest:
        cmd.extend(["--session-gap-min", str(args.session_gap_min), "--session-index", str(args.session_index)])
    if args.preview_north_up:
        cmd.append("--preview-north-up")
    if args.preview_sun_pa_left:
        cmd.append("--preview-sun-pa-left")
    cmd.extend(["--preview-at", args.preview_at, "--annotate-size", str(args.annotate_size)])
    if args.no_cleanup:
        cmd.append("--no-cleanup")
    if args.include_failed_frames:
        cmd.append("--include-failed-frames")
    if args.siril:
        cmd.extend(["--siril", str(args.siril)])
    cmd.append("--verbose" if args.verbose else "--no-verbose")
    verbose(args, f"Starting {args.target_mode} {args.stack_method} stack worker")
    process = subprocess.Popen(
        cmd,
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    output_lines: list[str] = []
    if process.stdout is None:
        raise RuntimeError("moving_target_stack.py stdout pipe was not created")
    for line in iter(process.stdout.readline, ""):
        output_lines.append(line)
        if not line.startswith("ERROR:"):
            write_console_safe(line)
    process.stdout.close()
    returncode = process.wait()
    output = "".join(output_lines)
    if returncode != 0:
        raise RuntimeError(child_error_message(output))
    return parse_stack_summary(output)


def main(args: argparse.Namespace) -> Path | None:
    if args.list_sessions:
        print_sessions(args)
        return None
    session_index, files, session_info = resolve_session(args)
    args.session_index = session_index
    reference_index = select_pipeline_reference_index(args, files)
    reference_mode = "file" if args.reference_frame_file else args.reference_frame
    reference_frame = files[reference_index - 1]
    validate_sharpcap_inputs(args, reference_frame)
    verbose(
        args,
        f"Selected session {session_index}: {len(files)} frames; reference {reference_index}/{len(files)} "
        f"({reference_frame.name})",
    )
    args.work_dir = prepare_work_dir(args, reference_frame)
    verbose(args, f"Work directory: {args.work_dir}")
    sharpcap = getattr(args, "sharpcap_session", None)
    settings_file, settings, preprocessing_root = discover_preprocessing_settings(args.source_dir, sharpcap)
    preprocessing_plan = resolve_preprocessing_plan(
        settings=settings,
        settings_file=settings_file,
        session_root=preprocessing_root,
        preprocessing=args.preprocessing,
        dark_correction=args.dark_correction,
        dark_file=args.dark_file,
        flat_correction=args.flat_correction,
        flat_file=args.flat_file,
        hot_pixel_correction=args.hot_pixel_correction,
        cold_pixel_correction=args.cold_pixel_correction,
        hot_pixel_sigma=args.hot_pixel_sigma,
        cold_pixel_sigma=args.cold_pixel_sigma,
    )
    args.preprocessing_plan = preprocessing_plan
    args.preprocessing_plan_file = args.work_dir / "preprocessing_plan.json"
    args.preprocessing_plan_file.write_text(
        json.dumps(preprocessing_plan.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if sharpcap is not None:
        args.frame_manifest = write_manifest(
            args.work_dir / "sharpcap_frame_manifest.json",
            sharpcap,
            files,
            preprocessing_plan.to_dict(),
        )
        alignment_mode = "SharpCap offsets" if sharpcap.alignment_complete else "incomplete offsets; Siril fallback"
        verbose(
            args,
            f"SharpCap Live Stack detected: version={sharpcap.version_text or 'unknown'}; "
            f"selected={len(files)}; rejected={sharpcap.rejected_rows}; missing raw={sharpcap.missing_raw_rows}; "
            f"registration={alignment_mode}",
        )
        verbose(
            args,
            f"SharpCap metadata: stacklog={sharpcap.stacklog}; "
            f"CameraSettings={sharpcap.settings_file or 'not found'}",
        )
        verbose(
            args,
            "SharpCap preprocessing: "
            f"dark={'on' if preprocessing_plan.dark_enabled else 'off'}; "
            f"flat={'on' if preprocessing_plan.flat_enabled else 'off'}; "
            f"hot={'on' if preprocessing_plan.hot_pixel_enabled else 'off'}; "
            f"cold={'on' if preprocessing_plan.cold_pixel_enabled else 'off'}",
        )
    else:
        verbose(
            args,
            f"CameraSettings without StackLog: {settings_file or 'not found'}; "
            f"dark={'on' if preprocessing_plan.dark_enabled else 'off'}; "
            f"flat={'on' if preprocessing_plan.flat_enabled else 'off'}; "
            f"hot={'on' if preprocessing_plan.hot_pixel_enabled else 'off'}; "
            f"cold={'on' if preprocessing_plan.cold_pixel_enabled else 'off'}",
        )
    if args.target_mode == "moving":
        verbose(args, "Stage 1/3: obtaining target ephemeris")
        ephemeris_csv = ensure_ephemeris(args, reference_frame, session_index)
        verbose(args, "Stage 2/3: resolving reference-frame sky coordinates")
        wcs_fits, astrometry_json = solve_first_frame(args, reference_frame)
        verbose(args, f"Stage 3/3: registering and stacking with method={args.stack_method}")
    else:
        ephemeris_csv = None
        verbose(args, "Stage 1/2: resolving reference-frame sky coordinates")
        wcs_fits, astrometry_json = solve_first_frame(args, reference_frame)
        verbose(args, f"Stage 2/2: registering and fixed stacking with method={args.stack_method}")
    stack_summary = run_stack(args, ephemeris_csv, wcs_fits, astrometry_json)

    pipeline_summary = {
        "source_dir": str(args.source_dir),
        "target_mode": args.target_mode,
        "ephemeris_csv": str(ephemeris_csv) if ephemeris_csv else None,
        "session": session_info,
        "reference_frame_mode": reference_mode,
        "reference_frame_index": reference_index,
        "reference_frame": str(reference_frame),
        "stack_method": args.stack_method,
        "stack_method_token": processing_method_token(args.stack_method, args.rankfit_fraction),
        "input_mode": "sharpcap-live-stack" if getattr(args, "sharpcap_session", None) is not None else "fits-subframes",
        "frame_manifest": str(args.frame_manifest) if args.frame_manifest else None,
        "preprocessing": args.preprocessing_plan.to_dict(),
        "plate_solver_requested": args.plate_solver,
        "plate_solution_source": plate_solution_source(wcs_fits, astrometry_json),
        "padding_policy": args.padding_policy,
        "output_region_mode": args.output_region_mode,
        "output_region_cover_count": args.output_region_cover_count,
        "output_region_cover_ratio_percent": args.output_region_cover_ratio,
        "zero_sample_policy": args.zero_sample_policy if args.stack_method != "mean" else None,
        "rankfit_fraction_percent": args.rankfit_fraction if args.stack_method == "rankfit" else None,
        "saturation_warning": args.saturation_warning,
        "saturation_threshold_percent": args.saturation_threshold_percent,
        "saturation_color": args.saturation_color,
        "wcs_fits": str(wcs_fits) if wcs_fits else None,
        "astrometry_json": str(astrometry_json) if astrometry_json else None,
        "stack": stack_summary,
    }
    work_dir = Path(str(stack_summary["work_dir"]))
    summary_name = "fixed_target_pipeline_summary.json" if args.target_mode == "fixed" else "moving_target_pipeline_summary.json"
    summary_path = work_dir / summary_name
    summary_path.write_text(json.dumps(pipeline_summary, indent=2), encoding="utf-8")
    outputs = stack_summary.get("outputs", {})
    if args.target_mode == "fixed":
        print(
            "Fixed-target pipeline complete: "
            f"used {stack_summary.get('used_frames')}/{stack_summary.get('input_frames')} frames; "
            f"fixed={outputs.get('fixed_fits') or outputs.get('fits')}",
            flush=True,
        )
    else:
        print(
            "Pipeline complete: "
            f"used {stack_summary.get('used_frames')}/{stack_summary.get('input_frames')} frames; "
            f"metcalf={outputs.get('metcalf_fits') or outputs.get('fits')}; "
            f"star={outputs.get('star_fits')}; "
            f"comparison={outputs.get('comparison_fits')}",
            flush=True,
        )
    print(f"Wrote pipeline summary: {summary_path}")
    return summary_path


def open_output_directory(path: Path) -> None:
    resolved = path.resolve()
    if os.name == "nt":
        os.startfile(resolved)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(resolved)])
    else:
        opener = shutil.which("xdg-open")
        if not opener:
            raise FileNotFoundError("xdg-open was not found")
        subprocess.Popen([opener, str(resolved)])


def default_log_path(args: argparse.Namespace) -> Path:
    if args.log_file:
        return args.log_file.expanduser().resolve()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = "fixed" if args.target_mode == "fixed" else "metcalf"
    return (args.work_root / f"{prefix}-{stamp}.log").expanduser().resolve()


def run_cli(args: argparse.Namespace) -> int:
    log_path = default_log_path(args)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8", buffering=1) as log_handle:
        stdout = TeeTextIO(sys.stdout, log_handle)
        stderr = TeeTextIO(sys.stderr, log_handle)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            runtime = f"EXE: {sys.executable}" if getattr(sys, "frozen", False) else f"Python: {sys.executable}"
            print("Seestar Fixed Stack" if args.target_mode == "fixed" else "Seestar Metcalf Stack")
            print(f"Runtime: {runtime}")
            print(f"Verbose: {'enabled' if args.verbose else 'disabled'}")
            print(f"Command:  {shlex.join(sys.argv)}")
            print(f"Log:      {log_path}")
            print("")
            try:
                summary_path = main(args)
                if summary_path and args.open_output:
                    output_dir = summary_path.parent
                    print(f"Opening output folder: {output_dir}")
                    try:
                        open_output_directory(output_dir)
                    except Exception as exc:
                        print(f"Warning: could not open output folder: {exc}", file=sys.stderr)
                print("Processing complete.")
                return 0
            except KeyboardInterrupt:
                print("\nProcessing cancelled.", file=sys.stderr)
                return 130
            except Exception as error:
                traceback.print_exc(file=log_handle)
                print(f"\nError: {friendly_exception_message(error)}", file=sys.stderr)
                print(f"Details were written to: {log_path}", file=sys.stderr)
                return 1


def run_internal_script() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--internal-script":
        args = parse_args()
        return run_cli(args)
    script_name = sys.argv[2]
    sys.argv = [script_name, *sys.argv[3:]]
    if script_name == "astrometry_solve.py":
        from astrometry_solve import main as script_main
    elif script_name == "horizons_ephemeris.py":
        from horizons_ephemeris import main as script_main
    elif script_name == "moving_target_stack.py":
        from moving_target_stack import main as script_main
    else:
        raise SystemExit(f"Unknown internal script: {script_name}")
    try:
        return script_main()
    except KeyboardInterrupt:
        print("ERROR: Processing cancelled.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"ERROR: {friendly_exception_message(error)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_internal_script())
