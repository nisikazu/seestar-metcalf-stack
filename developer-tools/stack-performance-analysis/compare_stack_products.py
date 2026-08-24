#!/usr/bin/env python
"""Compare two stack FITS products, including masks and aperture photometry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from moving_target_stack import read_fits  # noqa: E402


def planes(data: np.ndarray) -> np.ndarray:
    return data[np.newaxis, :, :] if data.ndim == 2 else data


def valid_footprint(data: np.ndarray) -> np.ndarray:
    source = planes(data)
    return np.all(np.isfinite(source), axis=0) & np.any(source != 0.0, axis=0)


def aperture_flux(
    data: np.ndarray,
    valid: np.ndarray,
    x: float,
    y: float,
    radius: float = 6.0,
    annulus_inner: float = 10.0,
    annulus_outer: float = 16.0,
) -> list[float] | None:
    source = planes(data).astype(np.float64, copy=False)
    yy, xx = np.indices(valid.shape, dtype=np.float64)
    radius_squared = (xx - x) ** 2 + (yy - y) ** 2
    aperture = valid & (radius_squared <= radius * radius)
    annulus = valid & (radius_squared >= annulus_inner * annulus_inner) & (
        radius_squared <= annulus_outer * annulus_outer
    )
    if np.count_nonzero(aperture) < 5 or np.count_nonzero(annulus) < 20:
        return None
    area = float(np.count_nonzero(aperture))
    return [
        float(np.sum(channel[aperture], dtype=np.float64) - area * np.median(channel[annulus]))
        for channel in source
    ]


def local_centroid(
    data: np.ndarray,
    valid: np.ndarray,
    x: float,
    y: float,
    radius: float,
    annulus_inner: float,
    annulus_outer: float,
) -> tuple[float, float] | None:
    """Estimate one source centroid after subtracting a local annulus background."""
    luminance = np.mean(planes(data).astype(np.float64, copy=False), axis=0)
    yy, xx = np.indices(valid.shape, dtype=np.float64)
    radius_squared = (xx - x) ** 2 + (yy - y) ** 2
    aperture = valid & (radius_squared <= radius * radius)
    annulus = valid & (radius_squared >= annulus_inner * annulus_inner) & (
        radius_squared <= annulus_outer * annulus_outer
    )
    if np.count_nonzero(aperture) < 5 or np.count_nonzero(annulus) < 20:
        return None
    background = float(np.median(luminance[annulus]))
    weights = np.maximum(luminance[aperture] - background, 0.0)
    total = float(np.sum(weights, dtype=np.float64))
    if not np.isfinite(total) or total <= 0.0:
        return None
    return (
        float(np.sum(xx[aperture] * weights, dtype=np.float64) / total),
        float(np.sum(yy[aperture] * weights, dtype=np.float64) / total),
    )


def select_reference_stars(
    data: np.ndarray,
    valid: np.ndarray,
    count: int,
    excluded: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    luminance = np.mean(planes(data).astype(np.float64, copy=False), axis=0)
    height, width = valid.shape
    candidates = valid.copy()
    margin = 20
    candidates[:margin] = False
    candidates[-margin:] = False
    candidates[:, :margin] = False
    candidates[:, -margin:] = False
    values = np.where(candidates, luminance, -np.inf).ravel()
    candidate_count = min(values.size, max(2000, count * 500))
    indices = np.argpartition(values, -candidate_count)[-candidate_count:]
    indices = indices[np.argsort(values[indices])[::-1]]
    selected: list[tuple[float, float]] = []
    for flat_index in indices:
        if not np.isfinite(values[flat_index]):
            continue
        y, x = divmod(int(flat_index), width)
        if any((x - px) ** 2 + (y - py) ** 2 < 30.0**2 for px, py in [*excluded, *selected]):
            continue
        selected.append((float(x), float(y)))
        if len(selected) == count:
            break
    return selected


def relative_channel_difference(first: list[float], second: list[float]) -> list[float | None]:
    return [
        (new - old) / old if abs(old) > 1.0e-12 else None
        for old, new in zip(first, second)
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_fits", type=Path)
    parser.add_argument("new_fits", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--target-x", type=float, help="Target x in one-based FITS coordinates")
    parser.add_argument("--target-y", type=float, help="Target y in one-based FITS coordinates")
    parser.add_argument("--reference-stars", type=int, default=8)
    parser.add_argument("--aperture-radius", type=float, default=6.0)
    parser.add_argument("--annulus-inner", type=float, default=10.0)
    parser.add_argument("--annulus-outer", type=float, default=16.0)
    parser.add_argument(
        "--recenter-radius",
        type=float,
        default=8.0,
        help="Recenter each reference star independently in each image; 0 disables recentering.",
    )
    args = parser.parse_args()

    old = read_fits(args.old_fits)
    new = read_fits(args.new_fits)
    old_data = planes(old.data).astype(np.float64, copy=False)
    new_data = planes(new.data).astype(np.float64, copy=False)
    if old_data.shape != new_data.shape:
        raise ValueError(f"FITS shapes differ: {old_data.shape} != {new_data.shape}")
    old_valid = valid_footprint(old_data)
    new_valid = valid_footprint(new_data)
    common = old_valid & new_valid
    difference = new_data[:, common] - old_data[:, common]
    absolute = np.abs(difference)

    target = None
    excluded: list[tuple[float, float]] = []
    if args.target_x is not None and args.target_y is not None:
        target_x = args.target_x - 1.0
        target_y = args.target_y - 1.0
        excluded.append((target_x, target_y))
        old_flux = aperture_flux(
            old_data,
            old_valid,
            target_x,
            target_y,
            args.aperture_radius,
            args.annulus_inner,
            args.annulus_outer,
        )
        new_flux = aperture_flux(
            new_data,
            new_valid,
            target_x,
            target_y,
            args.aperture_radius,
            args.annulus_inner,
            args.annulus_outer,
        )
        target = {
            "x_1based": args.target_x,
            "y_1based": args.target_y,
            "old_background_subtracted_flux": old_flux,
            "new_background_subtracted_flux": new_flux,
            "relative_difference": (
                relative_channel_difference(old_flux, new_flux)
                if old_flux is not None and new_flux is not None
                else None
            ),
        }

    reference_positions = select_reference_stars(old_data, old_valid, args.reference_stars, excluded)
    reference_apertures = []
    for x, y in reference_positions:
        old_center = (x, y)
        new_center = (x, y)
        if args.recenter_radius > 0.0:
            old_center = local_centroid(
                old_data,
                old_valid,
                x,
                y,
                args.recenter_radius,
                args.annulus_inner,
                args.annulus_outer,
            ) or old_center
            new_center = local_centroid(
                new_data,
                new_valid,
                x,
                y,
                args.recenter_radius,
                args.annulus_inner,
                args.annulus_outer,
            ) or new_center
        old_flux = aperture_flux(
            old_data,
            old_valid,
            old_center[0],
            old_center[1],
            args.aperture_radius,
            args.annulus_inner,
            args.annulus_outer,
        )
        new_flux = aperture_flux(
            new_data,
            new_valid,
            new_center[0],
            new_center[1],
            args.aperture_radius,
            args.annulus_inner,
            args.annulus_outer,
        )
        reference_apertures.append(
            {
                "x_1based": x + 1.0,
                "y_1based": y + 1.0,
                "old_centroid_1based": [old_center[0] + 1.0, old_center[1] + 1.0],
                "new_centroid_1based": [new_center[0] + 1.0, new_center[1] + 1.0],
                "centroid_shift_pixels": [
                    new_center[0] - old_center[0],
                    new_center[1] - old_center[1],
                ],
                "old_background_subtracted_flux": old_flux,
                "new_background_subtracted_flux": new_flux,
                "relative_difference": (
                    relative_channel_difference(old_flux, new_flux)
                    if old_flux is not None and new_flux is not None
                    else None
                ),
            }
        )

    result = {
        "old_fits": str(args.old_fits.resolve()),
        "new_fits": str(args.new_fits.resolve()),
        "shape": list(old_data.shape),
        "photometry": {
            "aperture_radius": args.aperture_radius,
            "annulus_inner": args.annulus_inner,
            "annulus_outer": args.annulus_outer,
            "recenter_radius": args.recenter_radius,
        },
        "valid_footprint": {
            "old_pixels": int(np.count_nonzero(old_valid)),
            "new_pixels": int(np.count_nonzero(new_valid)),
            "common_pixels": int(np.count_nonzero(common)),
            "different_pixels": int(np.count_nonzero(old_valid ^ new_valid)),
        },
        "common_pixel_difference_adu": {
            "maximum_absolute": float(np.max(absolute)),
            "mean_absolute": float(np.mean(absolute)),
            "root_mean_square": float(np.sqrt(np.mean(difference * difference))),
            "median_absolute": float(np.median(absolute)),
            "p99_absolute": float(np.percentile(absolute, 99.0)),
        },
        "target_aperture": target,
        "reference_star_apertures": reference_apertures,
    }
    rendered = json.dumps(result, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
