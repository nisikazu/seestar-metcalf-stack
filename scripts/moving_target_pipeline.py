#!/usr/bin/env python
"""End-to-end moving-target stack pipeline.

This wrapper connects:
1. first-frame plate solve with astrometry_solve.py,
2. Siril similarity registration on background stars,
3. target-motion compensated stacking with moving_target_stack.py.
"""

from __future__ import annotations

import argparse
import calendar
import json
import shutil
import subprocess
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

from moving_target_stack import parse_time, processing_method_token, read_fits_header, select_reference_index


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
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Solve first frame and stack Seestar subframes on a moving target")
    parser.add_argument(
        "source_dir_arg",
        nargs="?",
        type=Path,
        metavar="SOURCE_DIR",
        help="Directory containing Seestar subframe FITS files",
    )
    parser.add_argument("--source-dir", dest="source_dir_option", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--ephemeris-csv",
        type=Path,
        help="Existing or desired Horizons CSV. If omitted, one is generated automatically.",
    )
    parser.add_argument("--pattern", default="*.fit")
    parser.add_argument("--count", type=int)
    parser.add_argument("--after", help="Keep frames at or after this UTC ISO timestamp")
    parser.add_argument("--before", help="Keep frames at or before this UTC ISO timestamp")
    parser.add_argument(
        "--include-failed-frames",
        action="store_true",
        help="Include Seestar files whose names contain '_failed_'. They are skipped by default.",
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
    parser.add_argument("--horizons-object", help="Override Horizons object/designation for auto ephemeris")
    parser.add_argument("--horizons-command", help="Raw Horizons COMMAND value for auto ephemeris")
    parser.add_argument("--horizons-chunk-size", type=int, default=25)
    parser.add_argument("--horizons-retries", type=int, default=5)
    parser.add_argument(
        "--solve-dir",
        type=Path,
        help="Astrometry cache directory. Defaults to the source FITS directory.",
    )
    parser.add_argument(
        "--solve-name",
        help="Astrometry cache filename prefix. Defaults to the reference FITS stem.",
    )
    parser.add_argument("--wcs-fits", type=Path, help="Reuse an existing WCS FITS instead of solving")
    parser.add_argument("--astrometry-json", type=Path, help="Optional existing astrometry JSON, recorded in summary")
    parser.add_argument(
        "--skip-solve",
        action="store_true",
        help="Do not upload; require a valid explicit or cached Astrometry.net solution.",
    )
    parser.add_argument("--work-dir", type=Path, help="Use this exact run work directory instead of creating one under --work-root")
    parser.add_argument("--work-root", type=Path, default=REPO_ROOT / "metcalf_output")
    parser.add_argument(
        "--work-name",
        help="Work directory stem. Defaults to '<FITS OBJECT>_<method>'; a timestamp is appended.",
    )
    parser.add_argument("--registration-transform", default="similarity")
    parser.add_argument("--registration-minpairs", type=int, default=6)
    parser.add_argument(
        "--stack-method",
        choices=("mean", "median", "rankfit"),
        default="mean",
        help="Per-pixel combination method. median and rankfit exclude exact-zero samples. Defaults to mean.",
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
    parser.add_argument("--preview-flip-vertical", action="store_true")
    parser.add_argument("--output-bitpix", choices=("float32", "uint16"), default="uint16")
    parser.add_argument("--uint16-scale", choices=("none", "global", "per-channel"), default="none")
    parser.add_argument("--scale-low-percentile", type=float, default=0.0)
    parser.add_argument("--scale-high-percentile", type=float, default=100.0)
    parser.add_argument("--preview-low-percentile", type=float, default=5.0)
    parser.add_argument("--preview-high-percentile", type=float, default=99.95)
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep intermediate image FITS files generated during Siril registration.",
    )
    parser.add_argument(
        "--output-prefix",
        help="Output filename stem. Defaults to '<OBJECT>_<start>-<end>_<N>frames'.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show pipeline and per-frame progress.")
    args = parser.parse_args()
    if args.source_dir_arg and args.source_dir_option:
        parser.error("specify the source folder either as the first argument or with --source-dir, not both")
    args.source_dir = args.source_dir_arg or args.source_dir_option
    if args.source_dir is None:
        parser.error("source folder is required")
    delattr(args, "source_dir_arg")
    delattr(args, "source_dir_option")
    if not 1 <= args.rankfit_fraction <= 100:
        parser.error("--rankfit-fraction must be an integer from 1 to 100")
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
    fits_files = [path for path in files if path.suffix.lower() in {".fit", ".fits"}]
    return bool(fits_files) and all(path.name.lower().startswith("stacked_") for path in fits_files)


def resolve_source_dir(source_dir: Path, pattern: str) -> Path:
    source_dir = repair_windows_cmd_path(source_dir)
    files = sorted(source_dir.glob(pattern), key=lambda p: p.name) if source_dir.exists() else []
    sub_candidate = source_dir.with_name(f"{source_dir.name}_sub")
    sub_files = sorted(sub_candidate.glob(pattern), key=lambda p: p.name) if sub_candidate.exists() else []
    if sub_files and (not files or looks_like_stacked_outputs(files)):
        print(f"Using subframe directory: {sub_candidate}", file=sys.stderr)
        return sub_candidate
    return source_dir


def is_failed_frame(path: Path) -> bool:
    return "_failed_" in path.name.lower()


def load_dated_files(source_dir: Path, pattern: str, include_failed_frames: bool = False) -> list[tuple[datetime, Path]]:
    files = sorted(source_dir.glob(pattern), key=lambda p: p.name)
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
    args.source_dir = resolve_source_dir(args.source_dir, args.pattern)
    dated = load_dated_files(args.source_dir, args.pattern, args.include_failed_frames)
    if args.after:
        after_time = parse_time(args.after)
        dated = [item for item in dated if item[0] >= after_time]
    if args.before:
        before_time = parse_time(args.before)
        dated = [item for item in dated if item[0] <= before_time]
    return split_sessions_by_gap(dated, args.session_gap_min)


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
    print("\nSelect by number:")
    print(f"  seestar-metcalf-stack.cmd {quoted_source} --session-index N")
    print("Select the first session starting at or after a local date/time:")
    print(f"  seestar-metcalf-stack.cmd {quoted_source} --session-at YYYYMMDD-hhmmss")


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


def read_object_name(frame: Path) -> str:
    header, _cards, _offset = read_fits_header(frame)
    value = header.get("OBJECT")
    return str(value).strip() if value else frame.parent.name


def default_ephemeris_path(args: argparse.Namespace, first_frame: Path, session_index: int) -> Path:
    object_name = read_object_name(first_frame)
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
        args.work_name = default_work_name(first_frame, args.stack_method, args.rankfit_fraction)
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
    if args.verbose:
        cmd.append("--verbose")
    if args.horizons_object:
        cmd.extend(["--object", args.horizons_object])
    if args.horizons_command:
        cmd.extend(["--command", args.horizons_command])
    if args.include_failed_frames:
        cmd.append("--include-failed-frames")
    if args.after:
        cmd.extend(["--after", args.after])
    if args.before:
        cmd.extend(["--before", args.before])
    if args.session_gap_min is not None:
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
    subprocess.run(cmd, cwd=cwd, check=True)


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
    required = {"CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2", "CD1_1", "CD1_2", "CD2_1", "CD2_2"}
    return required.issubset(header)


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

    json_path, wcs_path = solve_cache_paths(args, first_frame)
    valid_json = is_valid_astrometry_json(json_path)
    if is_valid_wcs_fits(wcs_path):
        print(f"Reusing cached Astrometry.net WCS: {wcs_path}", flush=True)
        return wcs_path, json_path if valid_json else None
    if valid_json:
        print(f"Reusing cached Astrometry.net calibration: {json_path}", flush=True)
        return None, json_path
    if args.skip_solve:
        raise SystemExit("--skip-solve requested, but no valid explicit or cached Astrometry.net solution was found")

    json_path.parent.mkdir(parents=True, exist_ok=True)
    upload_frame = sanitize_fits_for_upload(
        first_frame,
        args.work_dir / f"{first_frame.stem}_upload_sanitized.fit",
    )
    solve_command = child_command(
        "astrometry_solve.py",
        [str(upload_frame), str(json_path), str(wcs_path)],
    )
    resume_subid = cached_submission_id(json_path)
    if resume_subid:
        print(f"Resuming cached Astrometry.net submission {resume_subid} for {first_frame.name}.", flush=True)
        solve_command.append(resume_subid)
    else:
        print(f"No valid cached plate solve for {first_frame.name}; uploading to Astrometry.net.", flush=True)
    run(solve_command, REPO_ROOT)
    if is_valid_wcs_fits(wcs_path):
        return wcs_path, json_path

    if is_valid_astrometry_json(json_path):
        print(f"WCS FITS was not usable; falling back to Astrometry.net JSON calibration: {json_path}", file=sys.stderr)
        return None, json_path
    # A completed but unusable job should not trap future runs into resuming the
    # same failed submission forever. Interrupted/network-failed runs never
    # reach here, so their checkpoint remains available for resume.
    submission_path = json_path.with_name(f"{json_path.stem}_submission.json")
    if resume_subid and submission_path.exists():
        submission_path.unlink()
    raise RuntimeError(f"Astrometry.net completed without a usable WCS or calibration cache for {first_frame}")


def run_stack(
    args: argparse.Namespace,
    ephemeris_csv: Path,
    wcs_fits: Path | None,
    astrometry_json: Path | None,
) -> dict[str, object]:
    cmd = child_command(
        "moving_target_stack.py",
        [
            "--source-dir",
            str(args.source_dir),
            "--ephemeris-csv",
            str(ephemeris_csv),
            "--work-dir",
            str(args.work_dir),
            "--registration-transform",
            args.registration_transform,
            "--registration-minpairs",
            str(args.registration_minpairs),
            "--stack-method",
            args.stack_method,
            "--rankfit-fraction",
            str(args.rankfit_fraction),
            "--reference-frame",
            args.reference_frame,
            "--output-bitpix",
            args.output_bitpix,
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
        ],
    )
    if args.pattern:
        cmd.extend(["--pattern", args.pattern])
    if args.output_prefix:
        cmd.extend(["--output-prefix", args.output_prefix])
    if wcs_fits:
        cmd.extend(["--wcs-fits", str(wcs_fits)])
    if astrometry_json:
        cmd.extend(["--astrometry-json", str(astrometry_json)])
    if args.count is not None:
        cmd.extend(["--count", str(args.count)])
    if args.after:
        cmd.extend(["--after", args.after])
    if args.before:
        cmd.extend(["--before", args.before])
    if args.session_gap_min is not None:
        cmd.extend(["--session-gap-min", str(args.session_gap_min), "--session-index", str(args.session_index)])
    if args.preview_flip_vertical:
        cmd.append("--preview-flip-vertical")
    if args.no_cleanup:
        cmd.append("--no-cleanup")
    if args.include_failed_frames:
        cmd.append("--include-failed-frames")
    if args.verbose:
        cmd.append("--verbose")
    verbose(args, f"Starting {args.stack_method} stack worker")
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
        write_console_safe(line)
    process.stdout.close()
    returncode = process.wait()
    output = "".join(output_lines)
    if returncode != 0:
        raise subprocess.CalledProcessError(
            returncode,
            cmd,
            output=output,
        )
    return parse_stack_summary(output)


def main() -> int:
    if args.list_sessions:
        print_sessions(args)
        return 0
    session_index, files, session_info = resolve_session(args)
    args.session_index = session_index
    reference_index = select_reference_index(files, args.reference_frame)
    reference_frame = files[reference_index - 1]
    verbose(
        args,
        f"Selected session {session_index}: {len(files)} frames; reference {reference_index}/{len(files)} "
        f"({reference_frame.name})",
    )
    args.work_dir = prepare_work_dir(args, reference_frame)
    verbose(args, f"Work directory: {args.work_dir}")
    verbose(args, "Stage 1/3: obtaining target ephemeris")
    ephemeris_csv = ensure_ephemeris(args, reference_frame, session_index)
    verbose(args, "Stage 2/3: resolving reference-frame sky coordinates")
    wcs_fits, astrometry_json = solve_first_frame(args, reference_frame)
    verbose(args, f"Stage 3/3: registering and stacking with method={args.stack_method}")
    stack_summary = run_stack(args, ephemeris_csv, wcs_fits, astrometry_json)

    pipeline_summary = {
        "source_dir": str(args.source_dir),
        "ephemeris_csv": str(ephemeris_csv),
        "session": session_info,
        "reference_frame_mode": args.reference_frame,
        "reference_frame_index": reference_index,
        "reference_frame": str(reference_frame),
        "stack_method": args.stack_method,
        "stack_method_token": processing_method_token(args.stack_method, args.rankfit_fraction),
        "rankfit_fraction_percent": args.rankfit_fraction if args.stack_method == "rankfit" else None,
        "wcs_fits": str(wcs_fits) if wcs_fits else None,
        "astrometry_json": str(astrometry_json) if astrometry_json else None,
        "stack": stack_summary,
    }
    work_dir = Path(str(stack_summary["work_dir"]))
    summary_path = work_dir / "moving_target_pipeline_summary.json"
    summary_path.write_text(json.dumps(pipeline_summary, indent=2), encoding="utf-8")
    outputs = stack_summary.get("outputs", {})
    print(
        "Pipeline complete: "
        f"used {stack_summary.get('used_frames')}/{stack_summary.get('input_frames')} frames; "
        f"metcalf={outputs.get('metcalf_fits') or outputs.get('fits')}; "
        f"star={outputs.get('star_fits')}; "
        f"comparison={outputs.get('comparison_fits')}",
        flush=True,
    )
    print(f"Wrote pipeline summary: {summary_path}")
    return 0


def run_internal_script() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--internal-script":
        global args
        args = parse_args()
        return main()
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
    return script_main()


if __name__ == "__main__":
    raise SystemExit(run_internal_script())
