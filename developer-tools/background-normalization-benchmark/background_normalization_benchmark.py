#!/usr/bin/env python
"""Benchmark background-normalization modes on the latest session of a source folder.

This developer tool keeps the ephemeris and plate solution fixed, then runs
the normal pipeline once for each requested mode. It therefore measures the
practical extra work of background fitting and stacking, not network latency
or plate-solving time.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE = REPO_ROOT / "scripts" / "moving_target_pipeline.py"
DEFAULT_SOURCE = Path(r"D:\downloads\220PMcNaught_sub")
MODES = ("none", "offset", "plane", "quadratic")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--ephemeris-csv", type=Path)
    parser.add_argument("--wcs-fits", type=Path)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--count", type=int, help="Limit frames from the selected latest session for a quick trial")
    parser.add_argument("--session-gap-min", type=float, default=60.0)
    parser.add_argument(
        "--stack-workers",
        choices=("auto", "1", "2", "4"),
        default="auto",
        help="Forwarded stacking worker setting. Defaults to auto.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        # Pipeline stages the long Horizons filename into each work directory.
        # Keep this root short enough for ordinary Windows path limits.
        default=REPO_ROOT.parents[1] / "background_benchmark",
    )
    return parser.parse_args()


def latest_path(paths: list[Path], description: str) -> Path:
    if not paths:
        raise FileNotFoundError(f"No {description} was found; pass the corresponding option explicitly")
    return max(paths, key=lambda path: path.stat().st_mtime)


def resolve_ephemeris(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"Ephemeris CSV was not found: {explicit}")
        return explicit.resolve()
    return latest_path(
        list((REPO_ROOT / "metcalf_output").glob("**/220PMcNaught_*horizons*.csv")),
        "cached 220P Horizons ephemeris CSV",
    )


def resolve_wcs(source_dir: Path, explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.is_file():
            raise FileNotFoundError(f"WCS FITS was not found: {explicit}")
        return explicit.resolve()
    return latest_path(list(source_dir.glob("*_siril_wcs.fits")), "cached Siril WCS FITS in the source folder")


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fields = [
        "mode",
        "repeat",
        "elapsed_seconds",
        "pipeline_wall_seconds",
        "registration_seconds",
        "stacking_seconds",
        "output_seconds",
        "stack_workers",
        "input_frames",
        "used_frames",
        "process_peak_rss_bytes",
        "temporary_peak_bytes",
        "registered_fits_after_registration",
        "success",
        "work_dir",
        "summary_json",
        "error",
        "log",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(path: Path, config: dict[str, object], rows: list[dict[str, object]]) -> None:
    lines = [
        "# Background Normalization Benchmark",
        "",
        "The pipeline reused the listed ephemeris and WCS. Timings include normal registration,",
        "background estimation, Metcalf/star stacking, and output writing. They exclude network",
        "ephemeris and plate-solving time.",
        "",
        "```json",
        json.dumps(config, indent=2),
        "```",
        "",
        "| Mode | Run | Total s | Registration s | Stacking s | Workers | Frames | Peak RSS MiB | Temp peak GiB | Registered FITS | Result |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in rows:
        result = "success" if row["success"] else f"failed: {row['error']}"
        rss_mib = (
            f"{int(row['process_peak_rss_bytes']) / (1024 ** 2):.1f}"
            if row.get("process_peak_rss_bytes") is not None
            else "n/a"
        )
        temp_gib = (
            f"{int(row['temporary_peak_bytes']) / (1024 ** 3):.2f}"
            if row.get("temporary_peak_bytes") is not None
            else "n/a"
        )
        frames = (
            f"{row['used_frames']}/{row['input_frames']}"
            if row.get("used_frames") is not None
            else "n/a"
        )
        lines.append(
            f"| {row['mode']} | {row['repeat']} | {row['elapsed_seconds']:.2f} | "
            f"{float(row.get('registration_seconds') or 0.0):.2f} | "
            f"{float(row.get('stacking_seconds') or 0.0):.2f} | "
            f"{row.get('stack_workers') or 'n/a'} | {frames} | {rss_mib} | {temp_gib} | "
            f"{row.get('registered_fits_after_registration', 'n/a')} | {result} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.repeat < 1:
        raise SystemExit("--repeat must be at least 1")
    source_dir = args.source_dir.resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"Source directory was not found: {source_dir}")
    ephemeris_csv = resolve_ephemeris(args.ephemeris_csv)
    wcs_fits = resolve_wcs(source_dir, args.wcs_fits)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_dir = (args.output_root / f"220P-latest-session-{stamp}").resolve()
    result_dir.mkdir(parents=True, exist_ok=False)
    config = {
        "source_dir": str(source_dir),
        "ephemeris_csv": str(ephemeris_csv),
        "wcs_fits": str(wcs_fits),
        "modes": args.modes,
        "repeat": args.repeat,
        "session": "latest after --session-gap-min splitting",
        "session_gap_min": args.session_gap_min,
        "frame_count_limit": args.count,
        "stack_workers": args.stack_workers,
        "timing_scope": "registration, background processing, stacking, and output writing",
    }
    (result_dir / "benchmark_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    rows: list[dict[str, object]] = []
    print(f"Source: {source_dir}")
    print(f"Ephemeris: {ephemeris_csv}")
    print(f"WCS: {wcs_fits}")
    print("Session: latest")
    for mode in args.modes:
        for repeat in range(1, args.repeat + 1):
            work_name = f"background-benchmark-{mode}-run{repeat}"
            command = [
                sys.executable,
                str(PIPELINE),
                "--source-dir",
                str(source_dir),
                "--ephemeris-csv",
                str(ephemeris_csv),
                "--wcs-fits",
                str(wcs_fits),
                "--session-gap-min",
                str(args.session_gap_min),
                "--background-normalization",
                mode,
                "--work-root",
                str(result_dir),
                "--work-name",
                work_name,
                "--no-open-output",
                "--no-verbose",
                "--sun-pa",
                "off",
                "--preview-at",
                "none",
                "--stack-workers",
                args.stack_workers,
            ]
            if args.count is not None:
                command.extend(["--count", str(args.count)])
            print(f"[{mode} {repeat}/{args.repeat}] running", flush=True)
            started = time.perf_counter()
            completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            elapsed = time.perf_counter() - started
            log_path = result_dir / f"{work_name}.log"
            log_path.write_text(completed.stdout, encoding="utf-8", errors="replace")
            match = re.search(r"Wrote pipeline summary: (.+)$", completed.stdout, re.MULTILINE)
            work_dir = str(Path(match.group(1)).parent) if match else ""
            summary_path = Path(match.group(1).strip()) if match else None
            summary = {}
            if summary_path is not None and summary_path.is_file():
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            stack_summary = summary.get("stack", summary)
            timing = stack_summary.get("pipeline_timing_seconds", {})
            temporary_storage = stack_summary.get("temporary_storage", {})
            row = {
                "mode": mode,
                "repeat": repeat,
                "elapsed_seconds": round(elapsed, 3),
                "pipeline_wall_seconds": timing.get("total_pipeline_wall"),
                "registration_seconds": timing.get("registration"),
                "stacking_seconds": timing.get("stacking"),
                "output_seconds": timing.get("output"),
                "stack_workers": stack_summary.get("stack_workers"),
                "input_frames": stack_summary.get("input_frames"),
                "used_frames": stack_summary.get("used_frames"),
                "process_peak_rss_bytes": stack_summary.get("process_peak_rss_bytes"),
                "temporary_peak_bytes": temporary_storage.get("measured_peak_bytes"),
                "registered_fits_after_registration": stack_summary.get("registered_fits_after_registration"),
                "success": completed.returncode == 0,
                "work_dir": work_dir,
                "summary_json": str(summary_path) if summary_path is not None else "",
                "error": "" if completed.returncode == 0 else f"pipeline exit {completed.returncode}",
                "log": str(log_path),
            }
            rows.append(row)
            write_csv(result_dir / "benchmark_runs.csv", rows)
            write_summary(result_dir / "benchmark_summary.md", config, rows)
            print(f"[{mode} {repeat}/{args.repeat}] {elapsed:.2f}s; {'success' if completed.returncode == 0 else row['error']}")
    print(f"Wrote {result_dir / 'benchmark_runs.csv'}")
    print(f"Wrote {result_dir / 'benchmark_summary.md'}")
    return 0 if all(bool(row["success"]) for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
