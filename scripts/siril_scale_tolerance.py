#!/usr/bin/env python3
"""Measure how much image-scale error Siril's internal plate solver tolerates."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from moving_target_pipeline import sanitize_fits_for_upload
from plate_solve_benchmark import (
    REPO_ROOT,
    Trial,
    infer_center,
    infer_effective_pixel_size,
    infer_true_scale,
    read_fits_header,
    resolve_siril,
    run_trial,
)


DEFAULT_FACTORS = (
    0.50,
    0.60,
    0.70,
    0.80,
    0.85,
    0.90,
    0.95,
    1.00,
    1.05,
    1.10,
    1.15,
    1.20,
    1.25,
    1.30,
    1.40,
    1.50,
    1.75,
    2.00,
)


def parse_factors(text: str | None) -> tuple[float, ...]:
    if text is None:
        return DEFAULT_FACTORS
    values: list[float] = []
    for item in text.split(","):
        try:
            factor = float(item.strip())
        except ValueError as error:
            raise argparse.ArgumentTypeError(f"invalid scale factor: {item}") from error
        if not math.isfinite(factor) or factor <= 0:
            raise argparse.ArgumentTypeError(f"scale factors must be positive: {item}")
        values.append(factor)
    if not values:
        raise argparse.ArgumentTypeError("provide at least one scale factor")
    return tuple(sorted(set(values)))


def contiguous_success_range(rows: list[dict[str, object]]) -> tuple[float | None, float | None]:
    by_factor: dict[float, list[bool]] = {}
    for row in rows:
        by_factor.setdefault(float(row["scale_factor"]), []).append(row["status"] == "success")
    factors = sorted(by_factor)
    if not factors:
        return None, None
    anchor = min(range(len(factors)), key=lambda index: abs(factors[index] - 1.0))
    successful = [all(by_factor[factor]) for factor in factors]
    if not successful[anchor]:
        return None, None
    lower = upper = anchor
    while lower > 0 and successful[lower - 1]:
        lower -= 1
    while upper + 1 < len(factors) and successful[upper + 1]:
        upper += 1
    return factors[lower], factors[upper]


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sweep supplied image-scale factors through Siril plate solving")
    parser.add_argument("fits", type=Path)
    parser.add_argument("--factors", help="Comma-separated factors; default is a coarse 0.50x to 2.00x sweep")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--pixel-scale-arcsec", type=float)
    parser.add_argument("--effective-pixel-size-um", type=float)
    parser.add_argument("--ra-deg", type=float)
    parser.add_argument("--dec-deg", type=float)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--siril", type=Path)
    parser.add_argument("--siril-catalog", choices=("tycho2", "nomad", "localgaia", "gaia", "ppmxl", "brightstars", "apass"))
    parser.add_argument("--siril-cache-mode", choices=("reuse", "cold-each"), default="reuse")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    args.factor_values = parse_factors(args.factors)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
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
    siril = resolve_siril(args.siril)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = (args.output_dir or REPO_ROOT / "plate_solve_benchmark" / f"{source.stem}-siril-scale-{stamp}").resolve()
    output.mkdir(parents=True, exist_ok=True)
    benchmark_input = sanitize_fits_for_upload(source, output / "benchmark_input_sanitized.fit")
    config = {
        "fits": str(source),
        "true_scale_arcsec": true_scale,
        "scale_source": scale_source,
        "effective_pixel_size_um": pixel_size,
        "pixel_size_source": pixel_source,
        "factors": list(args.factor_values),
        "repeats": args.repeats,
        "siril": str(siril),
        "siril_cache_mode": args.siril_cache_mode,
    }
    (output / "tolerance_config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # run_trial only reads these attributes for Siril trials.
    args.astrometry_scale_range_factor = 2.2
    rows: list[dict[str, object]] = []
    total = len(args.factor_values) * args.repeats
    order = 0
    for factor in args.factor_values:
        for repeat in range(1, args.repeats + 1):
            order += 1
            print(f"[{order}/{total}] Siril supplied scale={factor:.4f}x ({true_scale * factor:.6f} arcsec/pixel)", flush=True)
            result = run_trial(
                Trial(order, "siril", f"factor-{factor:.4f}", factor, repeat),
                args,
                benchmark_input,
                true_scale,
                pixel_size,
                center,
                output,
                None,
                siril,
            )
            row = asdict(result)
            rows.append(row)
            write_csv(output / "siril_scale_tolerance.csv", rows)
            print(f"  {result.status}: {result.elapsed_seconds:.2f}s {result.error}", flush=True)

    lower, upper = contiguous_success_range(rows)
    summary = [
        "# Siril image-scale tolerance",
        "",
        f"- Correct scale: {true_scale:.9f} arcsec/pixel",
        f"- Attempts: {len(rows)}",
        f"- Successes: {sum(row['status'] == 'success' for row in rows)}",
    ]
    if lower is None:
        summary.append("- Contiguous success range around 1.0x: not found")
    else:
        summary.append(f"- Tested contiguous success range around 1.0x: {lower:.4f}x to {upper:.4f}x")
        summary.append(f"- Supplied scale range: {true_scale * lower:.9f} to {true_scale * upper:.9f} arcsec/pixel")
    (output / "siril_scale_tolerance.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(f"Wrote {output / 'siril_scale_tolerance.csv'}")
    print(f"Wrote {output / 'siril_scale_tolerance.md'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
