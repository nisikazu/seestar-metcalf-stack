"""Compare retained pre-optimization algorithms with the production stacker."""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

SCRIPT_ROOT = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPT_ROOT))

from moving_target_stack import (  # noqa: E402
    add_to_average,
    apply_background_model,
    background_grid,
    fit_background_surface,
    read_fits,
    registered_valid_mask,
    shift_image,
)


def timed(callable_, repeats: int = 1):
    samples = []
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = callable_()
        samples.append(time.perf_counter() - started)
    return result, statistics.mean(samples), samples


def legacy_shift_plane(data, dx, dy, source_valid=None):
    """Production implementation retained from before slice translation."""
    height, width = data.shape
    if abs(dx) < 1.0e-9 and abs(dy) < 1.0e-9:
        valid = np.ones((height, width), dtype=bool) if source_valid is None else source_valid.copy()
        return data.astype(np.float64, copy=True), valid
    yy, xx = np.indices((height, width), dtype=np.float32)
    src_x = xx - np.float32(dx)
    src_y = yy - np.float32(dy)
    x0 = np.floor(src_x).astype(np.int32)
    y0 = np.floor(src_y).astype(np.int32)
    x1 = x0 + 1
    y1 = y0 + 1
    valid = (x0 >= 0) & (y0 >= 0) & (x1 < width) & (y1 < height)
    if source_valid is not None and np.any(valid):
        positions = np.flatnonzero(valid)
        py = positions // width
        px = positions % width
        valid[py, px] = (
            source_valid[y0[py, px], x0[py, px]]
            & source_valid[y0[py, px], x1[py, px]]
            & source_valid[y1[py, px], x0[py, px]]
            & source_valid[y1[py, px], x1[py, px]]
        )
    output = np.zeros((height, width), dtype=np.float32)
    if np.any(valid):
        wx = src_x[valid] - x0[valid]
        wy = src_y[valid] - y0[valid]
        output[valid] = (
            (1.0 - wx) * (1.0 - wy) * data[y0[valid], x0[valid]]
            + wx * (1.0 - wy) * data[y0[valid], x1[valid]]
            + (1.0 - wx) * wy * data[y1[valid], x0[valid]]
            + wx * wy * data[y1[valid], x1[valid]]
        )
    return output, valid


def legacy_shift_image(data, dx, dy, source_valid=None):
    if data.ndim == 2:
        return legacy_shift_plane(data, dx, dy, source_valid)
    planes = []
    common = None
    for plane in data:
        shifted, mask = legacy_shift_plane(plane, dx, dy, source_valid)
        planes.append(shifted)
        common = mask if common is None else common & mask
    return np.stack(planes), common


def legacy_apply_background(data, valid, coefficients, mode):
    planes = data[np.newaxis] if data.ndim == 2 else data
    correction = np.tensordot(coefficients, background_grid(*valid.shape, mode), axes=(1, 0))
    result = planes.astype(np.float64, copy=True)
    result[:, valid] += correction[:, valid]
    return result[0] if data.ndim == 2 else result


def legacy_add_to_average(sum_image, count_image, image, mask):
    if sum_image is None:
        sum_image = np.zeros_like(image, dtype=np.float64)
        count_image = np.zeros(mask.shape, dtype=np.uint32)
    if image.ndim == 3:
        sum_image += image * mask[np.newaxis]
    else:
        sum_image += image * mask
    count_image += mask.astype(np.uint16)
    return sum_image, count_image


def aperture_flux(data: np.ndarray, valid: np.ndarray, x: float, y: float, radius: float) -> np.ndarray:
    yy, xx = np.indices(valid.shape, dtype=np.float64)
    aperture = valid & ((xx - x) ** 2 + (yy - y) ** 2 <= radius * radius)
    if not np.any(aperture):
        raise ValueError("The benchmark aperture did not contain any valid pixels")
    planes = data[np.newaxis, :, :] if data.ndim == 2 else data
    return np.sum(planes[:, aperture], axis=1, dtype=np.float64)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("registration_dir", type=Path)
    parser.add_argument("--frames", type=int, default=8)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--dx", type=float, default=7.35)
    parser.add_argument("--dy", type=float, default=-4.60)
    args = parser.parse_args()
    paths = sorted(args.registration_dir.glob("r_*.fit"))[: args.frames]
    if not paths:
        raise SystemExit("No r_*.fit files found")

    measurements = {
        "FITS read": [],
        "quadratic fit": [],
        "legacy background apply": [],
        "production background apply": [],
        "legacy Metcalf shift": [],
        "production Metcalf shift": [],
        "legacy star zero shift": [],
        "production star direct": [],
        "legacy mean accumulation": [],
        "production mean accumulation": [],
    }
    maximum_error = 0.0
    maximum_aperture_error = {"target-centre": 0.0, "reference-star": 0.0}
    maximum_aperture_relative_error = {"target-centre": 0.0, "reference-star": 0.0}
    matching_valid_masks = 0
    frame_inputs = []
    for index, path in enumerate(paths, start=1):
        image, elapsed, _ = timed(lambda: read_fits(path), args.repeats)
        measurements["FITS read"].append(elapsed)
        valid = registered_valid_mask(image.data)
        frame_inputs.append((image.data, valid))
        model, elapsed, _ = timed(
            lambda: fit_background_surface(image.data, valid, "quadratic"), args.repeats
        )
        measurements["quadratic fit"].append(elapsed)
        correction = -model.coefficients
        legacy_background, elapsed, _ = timed(
            lambda: legacy_apply_background(image.data, valid, correction, "quadratic"), args.repeats
        )
        measurements["legacy background apply"].append(elapsed)
        production_background, elapsed, _ = timed(
            lambda: apply_background_model(image.data, valid, correction, "quadratic"), args.repeats
        )
        measurements["production background apply"].append(elapsed)
        np.testing.assert_allclose(production_background, legacy_background, rtol=0.0, atol=1.0e-9)

        legacy_shifted, elapsed, _ = timed(
            lambda: legacy_shift_image(production_background, args.dx, args.dy, valid), args.repeats
        )
        measurements["legacy Metcalf shift"].append(elapsed)
        production_shifted, elapsed, _ = timed(
            lambda: shift_image(production_background, args.dx, args.dy, valid), args.repeats
        )
        measurements["production Metcalf shift"].append(elapsed)
        legacy_data, legacy_mask = legacy_shifted
        production_data, production_mask = production_shifted
        np.testing.assert_array_equal(production_mask, legacy_mask)
        matching_valid_masks += 1
        maximum_error = max(maximum_error, float(np.max(np.abs(production_data - legacy_data))))

        height, width = production_mask.shape
        target_x = (width - 1.0) / 2.0
        target_y = (height - 1.0) / 2.0
        candidate = production_mask.copy()
        yy, xx = np.indices(candidate.shape)
        candidate &= (xx - target_x) ** 2 + (yy - target_y) ** 2 >= 30.0**2
        candidate[:8, :] = False
        candidate[-8:, :] = False
        candidate[:, :8] = False
        candidate[:, -8:] = False
        brightness = np.mean(production_data, axis=0) if production_data.ndim == 3 else production_data
        reference_flat = int(np.argmax(np.where(candidate, brightness, -np.inf)))
        reference_y, reference_x = np.unravel_index(reference_flat, candidate.shape)
        for label, aperture_x, aperture_y, radius in (
            ("target-centre", target_x, target_y, 8.0),
            ("reference-star", float(reference_x), float(reference_y), 5.0),
        ):
            old_flux = aperture_flux(legacy_data, legacy_mask, aperture_x, aperture_y, radius)
            new_flux = aperture_flux(production_data, production_mask, aperture_x, aperture_y, radius)
            absolute_error = float(np.max(np.abs(new_flux - old_flux)))
            relative_error = float(np.max(np.abs(new_flux - old_flux) / np.maximum(np.abs(old_flux), 1.0)))
            maximum_aperture_error[label] = max(maximum_aperture_error[label], absolute_error)
            maximum_aperture_relative_error[label] = max(
                maximum_aperture_relative_error[label], relative_error
            )

        _legacy_star, elapsed, _ = timed(
            lambda: legacy_shift_image(production_background, 0.0, 0.0, valid), args.repeats
        )
        measurements["legacy star zero shift"].append(elapsed)
        _direct_star, elapsed, _ = timed(lambda: (production_background, valid), args.repeats)
        measurements["production star direct"].append(elapsed)

        legacy_sum = np.zeros_like(production_data, dtype=np.float64)
        legacy_count = np.zeros(production_mask.shape, dtype=np.uint32)
        _, elapsed, _ = timed(
            lambda: legacy_add_to_average(legacy_sum, legacy_count, production_data, production_mask),
            args.repeats,
        )
        measurements["legacy mean accumulation"].append(elapsed)
        production_sum = np.zeros_like(production_data, dtype=np.float64)
        production_count = np.zeros(production_mask.shape, dtype=np.uint32)
        _, elapsed, _ = timed(
            lambda: add_to_average(production_sum, production_count, production_data, production_mask),
            args.repeats,
        )
        measurements["production mean accumulation"].append(elapsed)
        print(f"frame {index}/{len(paths)} complete", flush=True)

    print("\nMean timings")
    for name, values in measurements.items():
        print(f"{name:31s} {statistics.mean(values):8.4f} s/frame")
    shift_ratio = statistics.mean(measurements["production Metcalf shift"]) / statistics.mean(
        measurements["legacy Metcalf shift"]
    )
    print(f"production/legacy shift ratio {shift_ratio:.3f}")
    print(f"maximum full-frame numerical difference {maximum_error:.6g} ADU")
    print(f"full valid masks matched {matching_valid_masks}/{len(paths)} frames")
    for label in ("target-centre", "reference-star"):
        print(
            f"maximum {label} aperture difference "
            f"{maximum_aperture_error[label]:.6g} ADU "
            f"({maximum_aperture_relative_error[label]:.3e} relative)"
        )

    def legacy_frame(pair):
        data, valid = pair
        model = fit_background_surface(data, valid, "quadratic")
        corrected = legacy_apply_background(data, valid, -model.coefficients, "quadratic")
        return legacy_shift_image(corrected, args.dx, args.dy, valid)

    def production_frame(pair):
        data, valid = pair
        model = fit_background_surface(data, valid, "quadratic")
        corrected = apply_background_model(data, valid, -model.coefficients, "quadratic")
        return shift_image(corrected, args.dx, args.dy, valid)

    def parallel_trial(worker, workers: int) -> float:
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(worker, frame_inputs))
        return time.perf_counter() - started

    print(f"\n{len(frame_inputs)}-frame fit + background apply + shift wall time")
    for label, worker in (("legacy", legacy_frame), ("production", production_frame)):
        for workers in (1, 2, 4):
            print(f"{label:10s} workers={workers}: {parallel_trial(worker, workers):.4f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
