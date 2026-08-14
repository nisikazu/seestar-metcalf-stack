#!/usr/bin/env python
"""Exactly maximize a common Seestar field after trimming frame outliers.

The input is registration_trace.csv from trace_star_registration.py.  Every
retained-subset combination is evaluated by intersecting its transformed image
footprints.  This exact search is intended for small experiments such as 20
frames with 15 percent (3 frames) trimmed.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from moving_target_stack import read_fits
from pair_star_registration import SimilarityTransform


Point = tuple[float, float]
Polygon = list[Point]


@dataclass(frozen=True)
class FrameFootprint:
    index: int
    file: str
    date_obs: str
    transform: SimilarityTransform
    polygon: Polygon


@dataclass(frozen=True)
class ExactFootprintResult:
    selected_positions: tuple[int, ...]
    excluded_positions: tuple[int, ...]
    polygon: Polygon
    area: float
    combinations_evaluated: int
    elapsed_sec: float


def signed_polygon_area(vertices: Polygon) -> float:
    if len(vertices) < 3:
        return 0.0
    return sum(
        vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
        - vertices[(index + 1) % len(vertices)][0] * vertices[index][1]
        for index in range(len(vertices))
    ) / 2.0


def polygon_area(vertices: Polygon) -> float:
    return abs(signed_polygon_area(vertices))


def line_intersection(segment_start: Point, segment_end: Point, edge_start: Point, edge_end: Point) -> Point:
    segment = np.asarray(segment_end, dtype=np.float64) - np.asarray(segment_start, dtype=np.float64)
    edge = np.asarray(edge_end, dtype=np.float64) - np.asarray(edge_start, dtype=np.float64)
    denominator = segment[0] * edge[1] - segment[1] * edge[0]
    if abs(float(denominator)) < 1e-12:
        return segment_end
    offset = np.asarray(edge_start, dtype=np.float64) - np.asarray(segment_start, dtype=np.float64)
    ratio = (offset[0] * edge[1] - offset[1] * edge[0]) / denominator
    point = np.asarray(segment_start, dtype=np.float64) + ratio * segment
    return float(point[0]), float(point[1])


def intersect_convex_polygons(subject: Polygon, clip: Polygon) -> Polygon:
    """Intersect two convex polygons using Sutherland-Hodgman clipping."""
    output = list(subject)
    if len(output) < 3 or len(clip) < 3:
        return []
    orientation = 1.0 if signed_polygon_area(clip) >= 0.0 else -1.0
    for edge_index in range(len(clip)):
        edge_start = clip[edge_index]
        edge_end = clip[(edge_index + 1) % len(clip)]

        def inside(point: Point) -> bool:
            cross = (edge_end[0] - edge_start[0]) * (point[1] - edge_start[1]) - (
                edge_end[1] - edge_start[1]
            ) * (point[0] - edge_start[0])
            return orientation * cross >= -1e-8

        source = output
        output = []
        if not source:
            break
        previous = source[-1]
        previous_inside = inside(previous)
        for current in source:
            current_inside = inside(current)
            if current_inside:
                if not previous_inside:
                    output.append(line_intersection(previous, current, edge_start, edge_end))
                output.append(current)
            elif previous_inside:
                output.append(line_intersection(previous, current, edge_start, edge_end))
            previous = current
            previous_inside = current_inside
    return output


def intersect_many(polygons: list[Polygon]) -> Polygon:
    if not polygons:
        return []
    intersection = list(polygons[0])
    for polygon in polygons[1:]:
        intersection = intersect_convex_polygons(intersection, polygon)
        if len(intersection) < 3:
            return []
    return intersection


def transformed_frame_polygon(transform: SimilarityTransform, width: int, height: int) -> Polygon:
    corners = np.array(
        [[0.0, 0.0], [width - 1.0, 0.0], [width - 1.0, height - 1.0], [0.0, height - 1.0]],
        dtype=np.float64,
    )
    return [tuple(point) for point in transform.apply(corners)]


def exact_trimmed_footprint(
    footprints: list[FrameFootprint], trim_count: int, max_combinations: int = 1_000_000
) -> ExactFootprintResult:
    if trim_count < 0 or trim_count >= len(footprints):
        raise ValueError("trim_count must leave at least one frame")
    combination_count = math.comb(len(footprints), trim_count)
    if combination_count > max_combinations:
        raise RuntimeError(
            f"Exact search requires {combination_count:,} combinations, exceeding --max-combinations "
            f"{max_combinations:,}"
        )
    started = time.perf_counter()
    all_positions = tuple(range(len(footprints)))
    best_area = -1.0
    best_polygon: Polygon = []
    best_selected: tuple[int, ...] = ()
    best_excluded: tuple[int, ...] = ()
    evaluated = 0
    for excluded in itertools.combinations(all_positions, trim_count):
        excluded_set = set(excluded)
        selected = tuple(position for position in all_positions if position not in excluded_set)
        polygon = intersect_many([footprints[position].polygon for position in selected])
        area = polygon_area(polygon)
        evaluated += 1
        if area > best_area:
            best_area = area
            best_polygon = polygon
            best_selected = selected
            best_excluded = excluded
    return ExactFootprintResult(
        selected_positions=best_selected,
        excluded_positions=best_excluded,
        polygon=best_polygon,
        area=best_area,
        combinations_evaluated=evaluated,
        elapsed_sec=time.perf_counter() - started,
    )


def load_footprints(trace_csv: Path) -> tuple[list[FrameFootprint], int, int, dict[str, object]]:
    summary_path = trace_csv.with_name("registration_trace.json")
    if not summary_path.exists():
        raise FileNotFoundError(f"Companion trace summary not found: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    first_path = Path(str(summary["source_dir"])) / str(summary["first_frame"])
    first_image = read_fits(first_path)
    height, width = first_image.data.shape[-2:]
    footprints: list[FrameFootprint] = []
    with trace_csv.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["status"] != "ok":
                continue
            transform = SimilarityTransform(
                scale=float(row["scale"]),
                rotation_deg=float(row["rotation_deg"]),
                tx=float(row["origin_tx_px"]),
                ty=float(row["origin_ty_px"]),
            )
            footprints.append(
                FrameFootprint(
                    index=int(row["index"]),
                    file=row["file"],
                    date_obs=row["date_obs"],
                    transform=transform,
                    polygon=transformed_frame_polygon(transform, width, height),
                )
            )
    if not footprints:
        raise RuntimeError("Trace CSV contains no successfully matched frames")
    return footprints, width, height, summary


def map_polygon(vertices: Polygon, bounds: tuple[float, float, float, float], box: tuple[int, int, int, int]) -> Polygon:
    min_x, min_y, max_x, max_y = bounds
    left, top, right, bottom = box
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    scale = min((right - left) / span_x, (bottom - top) / span_y)
    offset_x = left + ((right - left) - span_x * scale) / 2.0
    offset_y = top + ((bottom - top) - span_y * scale) / 2.0
    return [
        (offset_x + (x - min_x) * scale, offset_y + (y - min_y) * scale)
        for x, y in vertices
    ]


def write_comparison_png(
    path: Path,
    footprints: list[FrameFootprint],
    all_intersection: Polygon,
    result: ExactFootprintResult,
    frame_area: float,
) -> None:
    canvas = Image.new("RGB", (1500, 850), (20, 20, 20))
    draw = ImageDraw.Draw(canvas)
    all_points = [point for footprint in footprints for point in footprint.polygon]
    bounds = (
        min(point[0] for point in all_points),
        min(point[1] for point in all_points),
        max(point[0] for point in all_points),
        max(point[1] for point in all_points),
    )
    panels = ((30, 60, 730, 810), (770, 60, 1470, 810))
    all_fraction = polygon_area(all_intersection) / frame_area
    best_fraction = result.area / frame_area
    draw.text((30, 15), "Strict common field: all frames versus exact trimmed optimum", fill="white")
    draw.text((35, 38), f"All {len(footprints)} frames: {all_fraction:.3%}", fill=(255, 150, 150))
    draw.text(
        (775, 38),
        f"Best {len(result.selected_positions)} frames: {best_fraction:.3%}; excluded "
        + ", ".join(str(footprints[position].index) for position in result.excluded_positions),
        fill=(120, 255, 150),
    )
    for panel_index, box in enumerate(panels):
        draw.rectangle(box, outline=(100, 100, 100))
        for position, footprint in enumerate(footprints):
            if panel_index == 1 and position in result.excluded_positions:
                color = (255, 90, 70)
                width = 2
            else:
                color = (80, 130, 190)
                width = 1
            draw.line(map_polygon(footprint.polygon + [footprint.polygon[0]], bounds, box), fill=color, width=width)
        intersection = all_intersection if panel_index == 0 else result.polygon
        if intersection:
            color = (180, 55, 55) if panel_index == 0 else (40, 170, 80)
            draw.polygon(map_polygon(intersection, bounds, box), fill=color, outline=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace_csv", type=Path)
    parser.add_argument("--trim-percent", type=float, default=15.0)
    parser.add_argument("--max-combinations", type=int, default=1_000_000)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    total_started = time.perf_counter()
    trace_csv = args.trace_csv.resolve()
    output_dir = args.output_dir.resolve() if args.output_dir else trace_csv.parent
    footprints, width, height, trace_summary = load_footprints(trace_csv)
    trim_count = int(math.floor(len(footprints) * args.trim_percent / 100.0 + 1e-12))
    if trim_count < 1:
        raise SystemExit("--trim-percent does not remove any frame from this sequence")
    all_intersection = intersect_many([footprint.polygon for footprint in footprints])
    all_area = polygon_area(all_intersection)
    result = exact_trimmed_footprint(footprints, trim_count, args.max_combinations)
    frame_area = float((width - 1) * (height - 1))
    selected_set = set(result.selected_positions)
    csv_path = output_dir / "common_footprint_exact.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["index", "file", "date_obs", "selected", "excluded", "center_dx_px", "center_dy_px", "rotation_deg", "scale"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        center = np.array([[(width - 1) / 2.0, (height - 1) / 2.0]], dtype=np.float64)
        for position, footprint in enumerate(footprints):
            mapped_center = footprint.transform.apply(center)[0]
            writer.writerow(
                {
                    "index": footprint.index,
                    "file": footprint.file,
                    "date_obs": footprint.date_obs,
                    "selected": position in selected_set,
                    "excluded": position not in selected_set,
                    "center_dx_px": float(mapped_center[0] - center[0, 0]),
                    "center_dy_px": float(mapped_center[1] - center[0, 1]),
                    "rotation_deg": footprint.transform.rotation_deg,
                    "scale": footprint.transform.scale,
                }
            )
    png_path = output_dir / "common_footprint_exact.png"
    write_comparison_png(png_path, footprints, all_intersection, result, frame_area)
    summary_path = output_dir / "common_footprint_exact.json"
    improvement_area = result.area - all_area
    summary = {
        "trace_csv": str(trace_csv),
        "source_dir": trace_summary.get("source_dir"),
        "input_frames": len(footprints),
        "trim_percent": args.trim_percent,
        "trim_count": trim_count,
        "retained_count": len(result.selected_positions),
        "combinations_evaluated": result.combinations_evaluated,
        "exact_search_elapsed_sec": result.elapsed_sec,
        "total_elapsed_sec": time.perf_counter() - total_started,
        "frame_area_px2": frame_area,
        "all_frames_common_area_px2": all_area,
        "all_frames_common_fraction": all_area / frame_area,
        "optimized_common_area_px2": result.area,
        "optimized_common_fraction": result.area / frame_area,
        "absolute_fraction_gain": improvement_area / frame_area,
        "relative_area_gain": (result.area / all_area - 1.0) if all_area > 0.0 else None,
        "selected_indices": [footprints[position].index for position in result.selected_positions],
        "excluded_indices": [footprints[position].index for position in result.excluded_positions],
        "excluded_files": [footprints[position].file for position in result.excluded_positions],
        "optimized_polygon": [{"x": x, "y": y} for x, y in result.polygon],
        "csv": str(csv_path),
        "comparison_png": str(png_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Exact search evaluated {result.combinations_evaluated:,} combinations in {result.elapsed_sec:.4f}s",
        flush=True,
    )
    print(
        f"Common field: all={all_area / frame_area:.3%}; optimized={result.area / frame_area:.3%}; "
        f"excluded={summary['excluded_indices']}",
        flush=True,
    )
    print(f"Wrote {summary_path}", flush=True)
    print(f"Wrote {csv_path}", flush=True)
    print(f"Wrote {png_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
