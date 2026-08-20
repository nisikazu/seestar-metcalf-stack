"""Developer-only experiment for Siril stack capabilities.

This is intentionally not part of the release manifest.  It creates a small
synthetic FITS sequence, asks Siril to register and stack it, and records the
resulting dimensions and timings.  The custom-offset case is deliberately
explicit: Siril's stack command consumes registration metadata; the test also
tries a Python-prealigned sequence to show the available workaround.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

SCRIPT_ROOT = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from moving_target_stack import read_fits_header, read_fits, write_fits_float32  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--siril", type=Path, help="Path to siril-cli.exe or siril-cli")
    parser.add_argument("--output", type=Path, default=Path("siril_stack_benchmark_result"))
    parser.add_argument("--frames", type=int, default=8, help="Synthetic frame count (default: 8)")
    return parser.parse_args()


def resolve_siril(explicit: Path | None) -> Path:
    if explicit:
        return explicit.resolve()
    candidates = [
        Path(__file__).resolve().parents[2] / "tools" / "siril-1.4.1" / "siril" / "bin" / "siril-cli.exe",
        Path(os.environ["SIRIL_CLI"]).expanduser() if os.environ.get("SIRIL_CLI") else None,
    ]
    found = shutil.which("siril-cli")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError("Siril CLI not found; pass --siril or set SIRIL_CLI")


def shift_integer(image: np.ndarray, dx: int, dy: int) -> np.ndarray:
    result = np.zeros_like(image)
    height, width = image.shape[-2:]
    source_x0 = max(0, -dx)
    source_x1 = min(width, width - dx) if dx >= 0 else width
    source_y0 = max(0, -dy)
    source_y1 = min(height, height - dy) if dy >= 0 else height
    dest_x0 = max(0, dx)
    dest_x1 = dest_x0 + (source_x1 - source_x0)
    dest_y0 = max(0, dy)
    dest_y1 = dest_y0 + (source_y1 - source_y0)
    result[dest_y0:dest_y1, dest_x0:dest_x1] = image[source_y0:source_y1, source_x0:source_x1]
    return result


def make_frame(width: int, height: int, dx: int, dy: int) -> np.ndarray:
    image = np.full((height, width), 100.0, dtype=np.float32)
    stars = [
        (15, 14, 5000),
        (38, 19, 3000),
        (66, 15, 4200),
        (96, 23, 2500),
        (116, 39, 3500),
        (21, 49, 2700),
        (52, 42, 4600),
        (78, 53, 3200),
        (108, 67, 3900),
        (14, 82, 2200),
        (43, 74, 4100),
        (73, 84, 2800),
        (101, 88, 3300),
    ]
    kernel = np.asarray([1.0, 2.0, 1.0], dtype=np.float32)
    kernel = np.outer(kernel, kernel) / 4.0
    for x, y, value in stars:
        image[y - 1 : y + 2, x - 1 : x + 2] += value * kernel
    return shift_integer(image, dx, dy)


def write_sequence(directory: Path, name: str, offsets: list[tuple[int, int]]) -> None:
    header = {
        "OBJECT": "SirilStackBenchmark",
        "DATE-OBS": "2026-01-01T00:00:00Z",
        "EXPOSURE": 1.0,
    }
    for index, (dx, dy) in enumerate(offsets, start=1):
        path = directory / f"{name}_{index:05d}.fit"
        write_fits_float32(path, make_frame(128, 96, dx, dy), header, {})


def run_siril(siril: Path, work: Path, script_name: str, script: str) -> tuple[float, str]:
    script_path = work / script_name
    script_path.write_text("requires 1.4.0\n" + script.strip() + "\n", encoding="ascii")
    command = [str(siril), "-d", str(work), "-s", str(script_path)]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=work,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.perf_counter() - started
    output = completed.stdout
    if completed.returncode != 0:
        raise RuntimeError(f"Siril failed with exit code {completed.returncode}\n{output}")
    return elapsed, output


def output_shape(path: Path) -> list[int] | None:
    if not path.exists():
        return None
    header, _cards, _offset = read_fits_header(path)
    axes = [int(header[key]) for key in ("NAXIS1", "NAXIS2") if key in header]
    if "NAXIS3" in header:
        axes.append(int(header["NAXIS3"]))
    return axes


def python_stack_timing(registered: Path, frames: int) -> tuple[float, int]:
    paths = [registered / f"r_frame_{index:05d}.fit" for index in range(1, frames + 1)]
    arrays = [read_fits(path).data.astype(np.float32) for path in paths if path.exists()]
    if not arrays:
        raise RuntimeError("Python timing found no registered frames")
    started = time.perf_counter()
    cube = np.stack(arrays, axis=0)
    # Measure both operations so the comparison covers the requested methods.
    np.mean(cube, axis=0)
    np.median(cube, axis=0)
    return time.perf_counter() - started, len(arrays)


def run_experiment(root: Path, siril: Path, frames: int) -> dict[str, object]:
    offsets = [((index % 3) - 1, index % 2) for index in range(frames)]
    raw = root / "raw"
    raw.mkdir(parents=True)
    write_sequence(raw, "frame", offsets)

    registered = root / "registered"
    registered.mkdir()
    shutil.copytree(raw, registered, dirs_exist_ok=True)
    registration_script = """
convert frame
setref frame_ 1
register frame_ -prefix=r_ -transf=shift -minpairs=10
stack frame_ mean none -nonorm -out=siril_mean.fit -32b
stack frame_ median -nonorm -out=siril_median.fit -32b
stack frame_ mean none -nonorm -maximize -out=siril_mean_maximize.fit -32b
"""
    registration_seconds, registration_log = run_siril(siril, registered, "register_and_stack.ssf", registration_script)
    stack_only_seconds, stack_only_log = run_siril(
        siril,
        registered,
        "stack_only.ssf",
        """
stack frame_ mean none -nonorm -out=siril_mean_stack_only.fit -32b
stack frame_ median -nonorm -out=siril_median_stack_only.fit -32b
stack frame_ mean none -nonorm -maximize -out=siril_mean_maximize_stack_only.fit -32b
""",
    )
    python_seconds, registered_count = python_stack_timing(registered, frames)

    prealigned = root / "prealigned_custom_offset"
    prealigned.mkdir()
    base_header = {"OBJECT": "SirilStackBenchmark", "DATE-OBS": "2026-01-01T00:00:00Z"}
    for index, (dx, dy) in enumerate(offsets, start=1):
        # Undo the known motion before Siril sees the files. This is a workaround,
        # not a direct custom transform supplied to the Siril stack command.
        image = make_frame(128, 96, dx, dy)
        aligned = shift_integer(image, -dx, -dy)
        write_fits_float32(prealigned / f"aligned_{index:05d}.fit", aligned, base_header, {})
    prealigned_seconds, prealigned_log = run_siril(
        siril,
        prealigned,
        "prealigned_stack.ssf",
        """
convert aligned
stack aligned_ mean none -nonorm -out=prealigned_mean.fit -32b
stack aligned_ median -nonorm -out=prealigned_median.fit -32b
""",
    )

    return {
        "siril": str(siril),
        "frame_count": frames,
        "registered_frame_count": registered_count,
        "known_offsets_px": offsets,
        "registration_and_stack_seconds": registration_seconds,
        "siril_stack_only_seconds": stack_only_seconds,
        "python_mean_and_median_stack_only_seconds": python_seconds,
        "prealigned_custom_offset_seconds": prealigned_seconds,
        "outputs": {
            "mean": output_shape(registered / "siril_mean.fit"),
            "median": output_shape(registered / "siril_median.fit"),
            "mean_maximize": output_shape(registered / "siril_mean_maximize.fit"),
            "mean_stack_only": output_shape(registered / "siril_mean_stack_only.fit"),
            "median_stack_only": output_shape(registered / "siril_median_stack_only.fit"),
            "mean_maximize_stack_only": output_shape(registered / "siril_mean_maximize_stack_only.fit"),
            "prealigned_mean": output_shape(prealigned / "prealigned_mean.fit"),
            "prealigned_median": output_shape(prealigned / "prealigned_median.fit"),
        },
        "direct_custom_offsets": {
            "supported": False,
            "reason": "Siril stack consumes registration metadata; stack has no per-frame X/Y offset arguments.",
            "workaround_tested": "Python pre-aligned each frame, then Siril stacked without registration.",
        },
        "common_area": {
            "comparison": "mean vs mean_maximize dimensions",
            "note": "-maximize includes the registered framing footprint; default mean is the non-maximized result.",
        },
        "logs": {
            "registration_and_stack": registration_log,
            "stack_only": stack_only_log,
            "prealigned_custom_offset": prealigned_log,
        },
    }


def main() -> int:
    args = parse_args()
    if args.frames < 3:
        raise SystemExit("--frames must be at least 3")
    root = args.output.resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    siril = resolve_siril(args.siril)
    result = run_experiment(root, siril, args.frames)
    report = root / "report.json"
    report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {report}")
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "registration_and_stack_seconds",
                    "siril_stack_only_seconds",
                    "python_mean_and_median_stack_only_seconds",
                    "prealigned_custom_offset_seconds",
                    "outputs",
                    "direct_custom_offsets",
                )
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
