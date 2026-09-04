"""Developer experiment for measuring residual background-star tracks.

This tool reuses the production registration transforms and the retained
Siril-prepared RGB frames. It is intentionally not part of the user-facing
stacking path. Every candidate is computed from the same row-tiled samples.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import moving_target_stack as stacker  # noqa: E402
from fits_preview import export_preview_png  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--prepared-dir", type=Path, required=True)
    parser.add_argument("--registration-seq", type=Path, required=True)
    parser.add_argument("--shifts-csv", type=Path, required=True)
    parser.add_argument("--reference-star-stack", type=Path, required=True)
    parser.add_argument("--header-source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tile-rows", type=int, default=240)
    parser.add_argument("--background-normalization", choices=("none", "offset", "plane", "quadratic"), default="quadratic")
    parser.add_argument("--track-stars", type=int, default=48)
    parser.add_argument("--track-radius", type=int, default=8)
    parser.add_argument("--mode-window-percent", type=float, default=10.0)
    parser.add_argument("--sigma", type=float, default=3.0)
    parser.add_argument("--trim-percent", type=float, nargs="+", default=(10.0, 20.0))
    return parser.parse_args()


def is_true(value: object) -> bool:
    return str(value).strip().casefold() in {"1", "true", "yes", "y"}


def finite_float(row: dict[str, str], key: str) -> float:
    value = row.get(key, "")
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Missing numeric shifts column {key!r}") from error
    if not math.isfinite(result):
        raise ValueError(f"Non-finite shifts column {key!r}")
    return result


def load_shift_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows = [row for row in rows if is_true(row.get("used", ""))]
    if not rows:
        raise ValueError(f"No used frames were found in {path}")
    rows.sort(key=lambda row: int(row["index"]))
    return rows


def load_tasks(args: argparse.Namespace, rows: list[dict[str, str]]) -> tuple[list[stacker.StackFrameTask], tuple[int, int], dict[int, stacker.StackFrameAnalysis]]:
    registrations = stacker.parse_siril_registration(args.registration_seq)
    if not registrations:
        raise ValueError(f"No Siril registration matrices were found in {args.registration_seq}")
    first_prepared = args.prepared_dir / rows[0]["stack_input"]
    first_image = stacker.read_fits(first_prepared)
    registration_shape = tuple(int(value) for value in first_image.data.shape[-2:])
    source_header = stacker.read_fits_header(args.header_source)[0]
    source_shape = (int(source_header["NAXIS2"]), int(source_header["NAXIS1"]))
    registrations = stacker.rebase_siril_registrations(registrations, int(rows[0]["index"]))
    tasks: list[stacker.StackFrameTask] = []
    for row in rows:
        index = int(row["index"])
        registration = registrations.get(index)
        if registration is None or registration.matrix is None:
            raise ValueError(f"Registration matrix is missing for frame {index}")
        source_name = row["source"]
        source_path = args.source_dir / source_name
        header = stacker.read_fits_header(source_path)[0]
        frame_time = stacker.parse_time(header["DATE-OBS"])
        target = stacker.TargetPoint(
            frame_time,
            finite_float(row, "ra_deg"),
            finite_float(row, "dec_deg"),
        )
        source_to_registration = stacker.siril_matrix_to_array_coordinates(
            registration.matrix,
            source_shape,
            registration_shape,
        )
        tasks.append(
            stacker.StackFrameTask(
                index=index,
                source_name=source_name,
                prepared=args.prepared_dir / row["stack_input"],
                source_header=header,
                source_to_registration=tuple(float(value) for value in source_to_registration.ravel()),
                frame_time=frame_time,
                target=target,
                target_x=finite_float(row, "target_x_1based"),
                target_y=finite_float(row, "target_y_1based"),
                dx=finite_float(row, "extra_dx_px"),
                dy=finite_float(row, "extra_dy_px"),
            )
        )
    analyses: dict[int, stacker.StackFrameAnalysis] = {}
    for number, task in enumerate(tasks, start=1):
        analyses[task.index] = stacker.analyze_order_statistic_frame(
            task,
            "valid",
            args.background_normalization,
            False,
            90.0,
        )
        if number == 1 or number % 25 == 0 or number == len(tasks):
            print(f"[analysis] background model {number}/{len(tasks)}", flush=True)
    return tasks, registration_shape, analyses


def box_blur(plane: np.ndarray, radius: int = 12) -> np.ndarray:
    radius = max(1, int(radius))
    source = np.nan_to_num(np.asarray(plane, dtype=np.float64), nan=0.0)
    padded = np.pad(source, radius, mode="edge")
    integral = np.pad(np.cumsum(np.cumsum(padded, axis=0), axis=1), ((1, 0), (1, 0)))
    width = 2 * radius + 1
    result = (
        integral[width:, width:]
        - integral[:-width, width:]
        - integral[width:, :-width]
        + integral[:-width, :-width]
    ) / float(width * width)
    return result


def static_star_mask(
    star_stack: np.ndarray,
    target_x: float,
    target_y: float,
    count: int,
    radius: int,
) -> tuple[np.ndarray, list[tuple[int, int, float]]]:
    gray = np.nanmean(star_stack, axis=0) if star_stack.ndim == 3 else np.asarray(star_stack, dtype=np.float64)
    high_pass = gray.astype(np.float64) - box_blur(gray, 8)
    finite = np.isfinite(high_pass)
    threshold = float(np.percentile(high_pass[finite], 99.7))
    candidates = np.argwhere(finite & (high_pass >= threshold))
    values = high_pass[candidates[:, 0], candidates[:, 1]]
    order = np.argsort(values)[::-1]
    height, width = gray.shape
    selected: list[tuple[int, int, float]] = []
    separation = max(2 * radius + 2, 12)
    for candidate_index in order:
        y, x = (int(value) for value in candidates[candidate_index])
        if min(x, y, width - 1 - x, height - 1 - y) < radius + 2:
            continue
        if math.hypot(x - target_x, y - target_y) < 70.0:
            continue
        if any(math.hypot(x - old_x, y - old_y) < separation for old_y, old_x, _ in selected):
            continue
        selected.append((y, x, float(high_pass[y, x])))
        if len(selected) >= count:
            break
    if len(selected) < 4:
        raise ValueError(f"Only {len(selected)} static star candidates were found")
    mask = np.zeros((height, width), dtype=bool)
    yy, xx = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    disk = (xx * xx + yy * yy) <= radius * radius
    for y, x, _value in selected:
        y0, y1 = max(0, y - radius), min(height, y + radius + 1)
        x0, x1 = max(0, x - radius), min(width, x + radius + 1)
        mask[y0:y1, x0:x1] |= disk[: y1 - y0, : x1 - x0]
    return mask, selected


def shifted_star_track_mask(
    star_positions: list[tuple[int, int, float]],
    dx: float,
    dy: float,
    row_start: int,
    row_end: int,
    width: int,
    radius: int,
) -> np.ndarray:
    mask = np.zeros((row_end - row_start, width), dtype=bool)
    height = row_end - row_start
    yy, xx = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    disk = (xx * xx + yy * yy) <= radius * radius
    for source_y, source_x, _value in star_positions:
        center_x = int(round(source_x + dx))
        center_y = int(round(source_y + dy)) - row_start
        y0, y1 = max(0, center_y - radius), min(height, center_y + radius + 1)
        x0, x1 = max(0, center_x - radius), min(width, center_x + radius + 1)
        if y0 < y1 and x0 < x1:
            mask[y0:y1, x0:x1] |= disk[y0 - center_y + radius : y1 - center_y + radius, x0 - center_x + radius : x1 - center_x + radius]
    return mask


def shifted_mask_without_wrap(mask: np.ndarray, dy: int, dx: int) -> np.ndarray:
    result = np.zeros_like(mask)
    source_y0 = max(0, -dy)
    source_y1 = min(mask.shape[0], mask.shape[0] - dy) if dy >= 0 else mask.shape[0]
    source_x0 = max(0, -dx)
    source_x1 = min(mask.shape[1], mask.shape[1] - dx) if dx >= 0 else mask.shape[1]
    target_y0 = max(0, dy)
    target_y1 = target_y0 + max(0, source_y1 - source_y0)
    target_x0 = max(0, dx)
    target_x1 = target_x0 + max(0, source_x1 - source_x0)
    if target_y1 > target_y0 and target_x1 > target_x0:
        result[target_y0:target_y1, target_x0:target_x1] = mask[source_y0:source_y1, source_x0:source_x1]
    return result


def median_from_sorted(ordered: np.ndarray, counts: np.ndarray) -> np.ndarray:
    flat = ordered.reshape(ordered.shape[0], -1)
    count_flat = counts.reshape(-1)
    result = np.zeros(count_flat.size, dtype=np.float64)
    for count_value in np.unique(count_flat):
        count = int(count_value)
        if count < 1:
            continue
        pixels = count_flat == count
        middle = count // 2
        if count % 2:
            result[pixels] = flat[middle, pixels]
        else:
            result[pixels] = (flat[middle - 1, pixels] + flat[middle, pixels]) / 2.0
    return result.reshape(ordered.shape[1:])


def upper_trim_from_sorted(ordered: np.ndarray, counts: np.ndarray, percent: float) -> np.ndarray:
    flat = ordered.reshape(ordered.shape[0], -1)
    count_flat = counts.reshape(-1)
    keep = np.maximum(1, count_flat - np.ceil(count_flat * percent / 100.0).astype(np.int64))
    total = np.zeros(count_flat.size, dtype=np.float64)
    for index in range(flat.shape[0]):
        selected = index < keep
        total[selected] += flat[index, selected]
    return (total / keep).reshape(ordered.shape[1:])


def iqr_clip_mean(cube: np.ndarray, ordered: np.ndarray, counts: np.ndarray, sigma: float) -> np.ndarray:
    flat_ordered = ordered.reshape(ordered.shape[0], -1)
    count_flat = counts.reshape(-1)
    safe_count = np.maximum(count_flat, 1)
    q25 = flat_ordered[np.minimum(safe_count - 1, np.maximum(0, (safe_count - 1) // 4)), np.arange(count_flat.size)]
    q75 = flat_ordered[np.minimum(safe_count - 1, np.maximum(0, (safe_count - 1) * 3 // 4)), np.arange(count_flat.size)]
    limit = q75 + sigma * 1.5 * (q75 - q25)
    total = np.zeros(count_flat.size, dtype=np.float64)
    kept = np.zeros(count_flat.size, dtype=np.int64)
    flat_cube = cube.reshape(cube.shape[0], -1)
    for index in range(flat_cube.shape[0]):
        values = flat_cube[index]
        selected = np.isfinite(values) & (values <= limit)
        total[selected] += values[selected]
        kept[selected] += 1
    result = np.zeros(count_flat.size, dtype=np.float64)
    np.divide(total, kept, out=result, where=kept > 0)
    return result.reshape(cube.shape[1:])


def density_mode_from_sorted(ordered: np.ndarray, counts: np.ndarray, percent: float) -> np.ndarray:
    flat = ordered.reshape(ordered.shape[0], -1)
    count_flat = counts.reshape(-1)
    window = max(3, int(round(flat.shape[0] * percent / 100.0)))
    window = min(window, flat.shape[0])
    best_width = np.full(count_flat.size, np.inf, dtype=np.float64)
    best_start = np.zeros(count_flat.size, dtype=np.int64)
    for start in range(0, flat.shape[0] - window + 1):
        width = flat[start + window - 1] - flat[start]
        width[(start + window) > count_flat] = np.inf
        update = width < best_width
        best_width[update] = width[update]
        best_start[update] = start
    middle = best_start + window // 2
    result = flat[middle, np.arange(count_flat.size)]
    fallback = count_flat < window
    if np.any(fallback):
        result[fallback] = median_from_sorted(ordered, counts).reshape(-1)[fallback]
    return result.reshape(ordered.shape[1:])


def candidate_metrics(data: np.ndarray, track_mask: np.ndarray, control_mask: np.ndarray, coverage: np.ndarray) -> dict[str, object]:
    rows: dict[str, object] = {"coverage_pixels": int(np.count_nonzero(coverage))}
    for channel, plane in enumerate(data):
        residual = plane.astype(np.float64) - box_blur(plane, 12)
        track = residual[track_mask & (coverage > 0)]
        control = residual[control_mask & (coverage > 0)]
        if track.size == 0 or control.size == 0:
            continue
        prefix = f"ch{channel + 1}_"
        rows[prefix + "track_median_residual"] = float(np.median(track))
        rows[prefix + "control_median_residual"] = float(np.median(control))
        rows[prefix + "track_p95_residual"] = float(np.percentile(track, 95.0))
        rows[prefix + "control_p95_residual"] = float(np.percentile(control, 95.0))
        rows[prefix + "track_positive_excess"] = float(np.mean(np.maximum(track, 0.0)))
        rows[prefix + "control_positive_excess"] = float(np.mean(np.maximum(control, 0.0)))
        rows[prefix + "track_above_control_p95_fraction"] = float(np.mean(track > np.percentile(control, 95.0)))
    rows["mean_track_positive_excess"] = float(np.mean([value for key, value in rows.items() if key.endswith("track_positive_excess")]))
    rows["mean_control_positive_excess"] = float(np.mean([value for key, value in rows.items() if key.endswith("control_positive_excess")]))
    return rows


def add_output_offset(data: np.ndarray, levels: np.ndarray, coverage: np.ndarray) -> np.ndarray:
    result = data.astype(np.float64, copy=True)
    result[:, coverage > 0] += levels[:, np.newaxis]
    return result


def main() -> int:
    args = parse_args()
    if args.tile_rows < 1 or args.track_stars < 4 or args.track_radius < 1:
        raise ValueError("tile-rows, track-stars, and track-radius must be positive")
    if not 0.0 < args.mode_window_percent <= 100.0:
        raise ValueError("mode-window-percent must be between 0 and 100")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = load_shift_rows(args.shifts_csv)
    tasks, registration_shape, analyses = load_tasks(args, rows)
    reference_star = stacker.read_fits(args.reference_star_stack).data
    target_x = float(rows[0]["target_x_1based"]) - 1.0
    target_y = float(rows[0]["target_y_1based"]) - 1.0
    star_mask, star_positions = static_star_mask(
        reference_star,
        target_x,
        target_y,
        args.track_stars,
        args.track_radius,
    )
    print(f"[tracks] selected {len(star_positions)} static stars; radius={args.track_radius}px", flush=True)
    height, width = registration_shape
    output_shape = (reference_star.shape[0], height, width)
    candidate_names = ["mean", "median", "temporal-iqr-clip", "histogram-mode", "track-exclude-mean"]
    candidate_names.extend(f"upper-trim-{int(percent) if float(percent).is_integer() else percent:g}" for percent in args.trim_percent)
    products = {name: np.zeros(output_shape, dtype=np.float64) for name in candidate_names}
    coverage = np.zeros((height, width), dtype=np.uint32)
    track_union = np.zeros((height, width), dtype=bool)
    timings = {name: 0.0 for name in candidate_names}
    total_started = time.perf_counter()
    levels = stacker.mean_background_dc_levels({index: analysis.background_model for index, analysis in analyses.items() if analysis.background_model is not None}) if args.background_normalization != "none" else np.zeros(reference_star.shape[0], dtype=np.float64)

    for tile_number, row_start in enumerate(range(0, height, args.tile_rows), start=1):
        row_end = min(height, row_start + args.tile_rows)
        tile_height = row_end - row_start
        cube = np.full((len(tasks), reference_star.shape[0], tile_height, width), np.nan, dtype=np.float32)
        track_masks = np.zeros((len(tasks), tile_height, width), dtype=bool)
        for frame_number, task in enumerate(tasks, start=1):
            result = stacker.process_stack_tile_frame(
                task,
                analyses[task.index],
                stacker.StackCanvas(
                    shape=(tile_height, width),
                    origin_x=0.0,
                    origin_y=float(row_start),
                ),
                "valid",
                args.background_normalization,
                False,
                "moving",
            )
            values = result.metcalf_data
            valid = result.metcalf_mask
            cube[frame_number - 1] = np.where(valid[np.newaxis, :, :], values, np.nan)
            track_masks[frame_number - 1] = shifted_star_track_mask(
                star_positions,
                task.dx,
                task.dy,
                row_start,
                row_end,
                width,
                args.track_radius,
            ) & valid
            if frame_number == 1 or frame_number % 50 == 0 or frame_number == len(tasks):
                print(f"[tile {tile_number}] frame {frame_number}/{len(tasks)}", flush=True)
        finite = np.isfinite(cube)
        tile_counts = np.count_nonzero(finite, axis=0).astype(np.uint32)
        tile_coverage = np.any(finite, axis=0)[0]
        coverage[row_start:row_end] = tile_counts[0]
        track_union[row_start:row_end] |= np.any(track_masks, axis=0)
        finite_cube = np.where(finite, cube, np.nan)
        ordered = np.sort(finite_cube, axis=0)
        tile_products: dict[str, np.ndarray] = {}
        tile_products["mean"] = np.divide(
            np.nansum(finite_cube, axis=0),
            np.maximum(tile_counts, 1),
            out=np.zeros_like(finite_cube[0], dtype=np.float64),
            where=tile_counts > 0,
        )
        tile_products["median"] = median_from_sorted(ordered, tile_counts)
        tile_products["temporal-iqr-clip"] = iqr_clip_mean(finite_cube, ordered, tile_counts, args.sigma)
        tile_products["histogram-mode"] = density_mode_from_sorted(ordered, tile_counts, args.mode_window_percent)
        excluded_total = np.zeros((reference_star.shape[0], tile_height, width), dtype=np.float64)
        excluded_count = np.zeros((tile_height, width), dtype=np.uint32)
        for frame_number in range(len(tasks)):
            include = finite[frame_number] & ~track_masks[frame_number][np.newaxis, :, :]
            safe_values = np.nan_to_num(cube[frame_number], nan=0.0)
            np.add(excluded_total, safe_values, out=excluded_total, where=include)
            np.add(excluded_count, include[0], out=excluded_count, casting="unsafe")
        tile_products["track-exclude-mean"] = np.divide(
            excluded_total,
            np.maximum(excluded_count, 1),
            out=np.zeros_like(excluded_total),
            where=excluded_count[np.newaxis, :, :] > 0,
        )
        for percent in args.trim_percent:
            name = f"upper-trim-{int(percent) if float(percent).is_integer() else percent:g}"
            tile_products[name] = upper_trim_from_sorted(ordered, tile_counts, percent)
        for name, tile_product in tile_products.items():
            products[name][:, row_start:row_end] = tile_product
        del cube, finite, finite_cube, ordered, track_masks, tile_products
        print(f"[tile {tile_number}] rows {row_start + 1}-{row_end}/{height} complete", flush=True)

    control_candidates = []
    for dy, dx in ((150, 150), (-150, 150), (150, -150), (-150, -150), (250, 0), (0, 250)):
        candidate = shifted_mask_without_wrap(track_union, dy, dx)
        control_candidates.append((int(np.count_nonzero(candidate & ~track_union)), candidate))
    _score, control_mask = max(control_candidates, key=lambda item: item[0])
    metric_rows: list[dict[str, object]] = []
    source_header = stacker.read_fits_header(args.header_source)[0]
    for name, product in products.items():
        product += 0.0
        product = add_output_offset(product, levels, coverage)
        products[name] = product
        output_fit = args.output_dir / f"candidate_{name}.fit"
        stacker.write_fits_float32(output_fit, product.astype(np.float32), source_header, {"STKMODE": name, "HISTORY": "developer star-background candidate"})
        export_preview_png(args.output_dir / f"candidate_{name}.png", product, low_percentile=5.0, high_percentile=99.95, stretch="sigma", sigma_low=-1.0, sigma_high=3.0)
        metric = {"method": name, **candidate_metrics(product, track_union, control_mask, coverage)}
        metric_rows.append(metric)
    metrics_path = args.output_dir / "star_background_metrics.json"
    metrics_path.write_text(json.dumps({"source": str(args.source_dir), "frames": len(tasks), "selected_stars": len(star_positions), "track_radius_px": args.track_radius, "tile_rows": args.tile_rows, "methods": metric_rows, "elapsed_seconds": time.perf_counter() - total_started}, indent=2), encoding="utf-8")
    with (args.output_dir / "star_background_metrics.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = sorted({key for row in metric_rows for key in row})
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(metric_rows)
    print(f"[result] wrote {metrics_path}", flush=True)
    print(f"[result] elapsed={time.perf_counter() - total_started:.2f}s", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
