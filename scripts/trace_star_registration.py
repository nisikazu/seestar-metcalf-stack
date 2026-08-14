#!/usr/bin/env python
"""Trace pairwise Seestar star registrations through a short FITS sequence.

This experimental helper is for reference-frame selection.  It registers each
frame to the preceding successfully matched frame, composes those transforms
into the first-frame coordinate system, and writes a CSV plus a diagnostic PNG.
It does not alter or stack the source FITS files.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from moving_target_stack import parse_time, read_fits, read_fits_header
from pair_star_registration import (
    DetectedStar,
    SimilarityTransform,
    detect_stars,
    estimate_similarity_transform,
    points_from_stars,
    registration_plane,
)


def compose_similarity(outer: SimilarityTransform, inner: SimilarityTransform) -> SimilarityTransform:
    """Return outer(inner(point)); both transforms use native Cartesian pixels."""
    angle = math.radians(outer.rotation_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    inner_translation = np.array([inner.tx, inner.ty], dtype=np.float64)
    translation = outer.scale * np.array(
        [
            cosine * inner_translation[0] - sine * inner_translation[1],
            sine * inner_translation[0] + cosine * inner_translation[1],
        ],
        dtype=np.float64,
    ) + np.array([outer.tx, outer.ty], dtype=np.float64)
    return SimilarityTransform(
        scale=outer.scale * inner.scale,
        rotation_deg=outer.rotation_deg + inner.rotation_deg,
        tx=float(translation[0]),
        ty=float(translation[1]),
    )


def image_center_offset(transform: SimilarityTransform, width: int, height: int) -> tuple[float, float]:
    center = np.array([[(width - 1) / 2.0, (height - 1) / 2.0]], dtype=np.float64)
    mapped = transform.apply(center)[0]
    return float(mapped[0] - center[0, 0]), float(mapped[1] - center[0, 1])


def polygon_area(vertices: list[tuple[float, float]]) -> float:
    if len(vertices) < 3:
        return 0.0
    return abs(
        sum(
            vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
            - vertices[(index + 1) % len(vertices)][0] * vertices[index][1]
            for index in range(len(vertices))
        )
    ) / 2.0


def clip_polygon(
    vertices: list[tuple[float, float]],
    inside,
    intersect,
) -> list[tuple[float, float]]:
    if not vertices:
        return []
    output: list[tuple[float, float]] = []
    previous = vertices[-1]
    previous_inside = inside(previous)
    for current in vertices:
        current_inside = inside(current)
        if current_inside != previous_inside:
            output.append(intersect(previous, current))
        if current_inside:
            output.append(current)
        previous = current
        previous_inside = current_inside
    return output


def clipped_overlap_fraction(transform: SimilarityTransform, width: int, height: int) -> float:
    """Return the fraction of a transformed frame falling within frame 1."""
    corners = np.array(
        [[0.0, 0.0], [width - 1.0, 0.0], [width - 1.0, height - 1.0], [0.0, height - 1.0]],
        dtype=np.float64,
    )
    vertices = [tuple(point) for point in transform.apply(corners)]

    def clip_axis(
        polygon: list[tuple[float, float]], axis: int, bound: float, keep_greater: bool
    ) -> list[tuple[float, float]]:
        def inside(point: tuple[float, float]) -> bool:
            return point[axis] >= bound if keep_greater else point[axis] <= bound

        def intersect(start: tuple[float, float], end: tuple[float, float]) -> tuple[float, float]:
            delta = end[axis] - start[axis]
            if abs(delta) < 1e-12:
                return start
            ratio = (bound - start[axis]) / delta
            return (start[0] + ratio * (end[0] - start[0]), start[1] + ratio * (end[1] - start[1]))

        return clip_polygon(polygon, inside, intersect)

    vertices = clip_axis(vertices, 0, 0.0, True)
    vertices = clip_axis(vertices, 0, width - 1.0, False)
    vertices = clip_axis(vertices, 1, 0.0, True)
    vertices = clip_axis(vertices, 1, height - 1.0, False)
    # Corners are expressed at pixel centres (0 .. width - 1), so use the
    # matching continuous rectangle area rather than the number of samples.
    return polygon_area(vertices) / max(float((width - 1) * (height - 1)), 1.0)


def dated_files(source_dir: Path, pattern: str) -> list[tuple[datetime, Path]]:
    files: list[tuple[datetime, Path]] = []
    for path in source_dir.glob(pattern):
        if not path.is_file():
            continue
        header, _cards, _offset = read_fits_header(path)
        date_obs = header.get("DATE-OBS")
        if date_obs is None:
            continue
        files.append((parse_time(date_obs), path))
    files.sort(key=lambda item: item[0])
    if not files:
        raise FileNotFoundError(f"No dated FITS files matching {pattern} in {source_dir}")
    return files


def split_sessions(
    dated: list[tuple[datetime, Path]], session_gap_min: float
) -> list[list[tuple[datetime, Path]]]:
    if session_gap_min <= 0.0:
        raise ValueError("session_gap_min must be positive")
    sessions: list[list[tuple[datetime, Path]]] = []
    for item in dated:
        if not sessions or (item[0] - sessions[-1][-1][0]).total_seconds() > session_gap_min * 60.0:
            sessions.append([item])
        else:
            sessions[-1].append(item)
    return sessions


def print_session_table(sessions: list[list[tuple[datetime, Path]]], selected_index: int) -> None:
    print("Index  Frames  UTC start                 UTC end", flush=True)
    for index, session in enumerate(sessions, start=1):
        marker = " <- selected" if index == selected_index else ""
        print(
            f"{index:>5}  {len(session):>6}  {session[0][0].isoformat():<25} {session[-1][0].isoformat()}{marker}",
            flush=True,
        )


def nice_range(values: list[float]) -> tuple[float, float]:
    if not values:
        return -1.0, 1.0
    low = min(values)
    high = max(values)
    if math.isclose(low, high, rel_tol=0.0, abs_tol=1e-12):
        padding = max(abs(low) * 0.1, 1.0)
    else:
        padding = (high - low) * 0.12
    return low - padding, high + padding


def draw_axes(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], title: str, ymin: float, ymax: float) -> None:
    left, top, right, bottom = box
    draw.rectangle(box, outline=(120, 120, 120))
    draw.text((left + 4, top + 3), title, fill="white")
    draw.text((left + 4, bottom - 14), f"{ymin:.3f}", fill=(180, 180, 180))
    draw.text((left + 4, top + 16), f"{ymax:.3f}", fill=(180, 180, 180))


def graph_point(value: float, low: float, high: float, start: int, end: int) -> int:
    return int(round(end - (value - low) * (end - start) / (high - low)))


def write_trace_png(path: Path, rows: list[dict[str, object]]) -> None:
    width, height = 1400, 900
    canvas = Image.new("RGB", (width, height), (24, 24, 24))
    draw = ImageDraw.Draw(canvas)
    draw.text((16, 10), "Experimental pairwise registration trace (all offsets are relative to frame 1)", fill="white")
    valid = [row for row in rows if row["status"] == "ok"]
    indices = [int(row["index"]) for row in valid]
    x_offsets = [float(row["center_dx_px"]) for row in valid]
    y_offsets = [float(row["center_dy_px"]) for row in valid]
    rotations = [float(row["rotation_deg"]) for row in valid]
    scale_ppm = [(float(row["scale"]) - 1.0) * 1_000_000.0 for row in valid]
    panels = {
        "offset": (30, 45, 680, 420),
        "path": (720, 45, 1370, 420),
        "rotation": (30, 470, 680, 845),
        "scale": (720, 470, 1370, 845),
    }
    offset_low, offset_high = nice_range(x_offsets + y_offsets)
    draw_axes(draw, panels["offset"], "Image-center offsets: X red, Y cyan [px]", offset_low, offset_high)
    if indices:
        index_min, index_max = min(indices), max(indices)
        span = max(index_max - index_min, 1)
        left, top, right, bottom = panels["offset"]
        for values, color in ((x_offsets, (255, 90, 90)), (y_offsets, (60, 220, 255))):
            points = [
                (
                    int(left + (index - index_min) * (right - left) / span),
                    graph_point(value, offset_low, offset_high, top + 24, bottom - 18),
                )
                for index, value in zip(indices, values)
            ]
            if len(points) > 1:
                draw.line(points, fill=color, width=2)
            for point in points:
                draw.ellipse((point[0] - 2, point[1] - 2, point[0] + 2, point[1] + 2), fill=color)

    path_x_low, path_x_high = nice_range(x_offsets)
    path_y_low, path_y_high = nice_range(y_offsets)
    draw_axes(draw, panels["path"], "Image-center path: X / Y [px]", path_y_low, path_y_high)
    left, top, right, bottom = panels["path"]
    path_points = [
        (
            int(left + (x - path_x_low) * (right - left) / (path_x_high - path_x_low)),
            graph_point(y, path_y_low, path_y_high, top + 24, bottom - 18),
        )
        for x, y in zip(x_offsets, y_offsets)
    ]
    if len(path_points) > 1:
        draw.line(path_points, fill=(255, 220, 80), width=2)
    for index, point in zip(indices, path_points):
        draw.ellipse((point[0] - 3, point[1] - 3, point[0] + 3, point[1] + 3), fill=(255, 220, 80))
        draw.text((point[0] + 4, point[1] + 2), str(index), fill=(210, 210, 210))

    for key, title, values, color in (
        ("rotation", "Cumulative rotation [deg]", rotations, (170, 130, 255)),
        ("scale", "Cumulative scale offset [ppm]", scale_ppm, (100, 255, 140)),
    ):
        low, high = nice_range(values)
        draw_axes(draw, panels[key], title, low, high)
        left, top, right, bottom = panels[key]
        if indices:
            index_min, index_max = min(indices), max(indices)
            span = max(index_max - index_min, 1)
            points = [
                (
                    int(left + (index - index_min) * (right - left) / span),
                    graph_point(value, low, high, top + 24, bottom - 18),
                )
                for index, value in zip(indices, values)
            ]
            if len(points) > 1:
                draw.line(points, fill=color, width=2)
            for point in points:
                draw.ellipse((point[0] - 2, point[1] - 2, point[0] + 2, point[1] + 2), fill=color)
    failed = [str(row["index"]) for row in rows if row["status"] != "ok"]
    if failed:
        draw.text((16, 870), f"Unmatched frames: {', '.join(failed)}", fill=(255, 120, 120))
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--count", type=int, default=20, help="Number of chronological FITS frames to inspect")
    parser.add_argument("--pattern", default="*.fit*")
    parser.add_argument("--session-gap-min", type=float, default=60.0)
    parser.add_argument(
        "--session-index",
        type=int,
        help="1-based session to inspect. Defaults to the latest session after gap splitting.",
    )
    parser.add_argument("--list-sessions", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("registration_trace"))
    parser.add_argument("--bin-factor", type=int, default=2)
    parser.add_argument("--threshold-sigma", type=float, default=6.0)
    parser.add_argument("--max-stars", type=int, default=80)
    parser.add_argument("--match-stars", type=int, default=25)
    parser.add_argument("--min-inliers", type=int, default=6)
    parser.add_argument("--match-radius", type=float, default=2.0)
    parser.add_argument("--scale-min", type=float, default=0.90)
    parser.add_argument("--scale-max", type=float, default=1.10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    sessions = split_sessions(dated_files(args.source_dir.resolve(), args.pattern), args.session_gap_min)
    session_index = args.session_index if args.session_index is not None else len(sessions)
    if session_index < 1 or session_index > len(sessions):
        raise SystemExit(f"--session-index {session_index} is out of range; found {len(sessions)} session(s)")
    print_session_table(sessions, session_index)
    if args.list_sessions:
        return 0
    dated = sessions[session_index - 1][: args.count]
    if not dated:
        raise SystemExit("No frames remain after session and count selection")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    first_time, first_path = dated[0]
    first_image = read_fits(first_path)
    first_plane = registration_plane(first_image.data, args.bin_factor)
    first_stars = detect_stars(first_plane, args.threshold_sigma, args.max_stars)
    native_height, native_width = first_image.data.shape[-2:]
    identity = SimilarityTransform(1.0, 0.0, 0.0, 0.0)
    rows.append(
        {
            "index": 1,
            "date_obs": first_time.isoformat(),
            "elapsed_sec": 0.0,
            "file": first_path.name,
            "status": "ok",
            "previous_success_index": "",
            "reference_star_count": len(first_stars),
            "target_star_count": len(first_stars),
            "pair_inliers": "",
            "pair_rms_native_px": "",
            "origin_tx_px": 0.0,
            "origin_ty_px": 0.0,
            "center_dx_px": 0.0,
            "center_dy_px": 0.0,
            "rotation_deg": 0.0,
            "scale": 1.0,
            "overlap_with_first_fraction": 1.0,
            "error": "",
        }
    )
    previous_stars: list[DetectedStar] = first_stars
    previous_transform = identity
    previous_index = 1
    for index, (when, path) in enumerate(dated[1:], start=2):
        image = read_fits(path)
        plane = registration_plane(image.data, args.bin_factor)
        stars = detect_stars(plane, args.threshold_sigma, args.max_stars)
        base = {
            "index": index,
            "date_obs": when.isoformat(),
            "elapsed_sec": (when - first_time).total_seconds(),
            "file": path.name,
            "previous_success_index": previous_index,
            "reference_star_count": len(previous_stars),
            "target_star_count": len(stars),
        }
        try:
            pair = estimate_similarity_transform(
                points_from_stars(stars),
                points_from_stars(previous_stars),
                match_radius=args.match_radius,
                min_inliers=args.min_inliers,
                scale_min=args.scale_min,
                scale_max=args.scale_max,
                maximum_points=args.match_stars,
            )
            cumulative = compose_similarity(previous_transform, pair.transform)
            center_dx, center_dy = image_center_offset(cumulative, native_width // args.bin_factor, native_height // args.bin_factor)
            rows.append(
                {
                    **base,
                    "status": "ok",
                    "pair_inliers": len(pair.inlier_pairs),
                    "pair_rms_native_px": pair.rms_px * args.bin_factor,
                    "origin_tx_px": cumulative.tx * args.bin_factor,
                    "origin_ty_px": cumulative.ty * args.bin_factor,
                    "center_dx_px": center_dx * args.bin_factor,
                    "center_dy_px": center_dy * args.bin_factor,
                    "rotation_deg": cumulative.rotation_deg,
                    "scale": cumulative.scale,
                    "overlap_with_first_fraction": clipped_overlap_fraction(
                        cumulative, native_width // args.bin_factor, native_height // args.bin_factor
                    ),
                    "error": "",
                }
            )
            previous_stars = stars
            previous_transform = cumulative
            previous_index = index
            print(
                f"frame {index}/{len(dated)}: inliers={len(pair.inlier_pairs)} "
                f"rms={pair.rms_px * args.bin_factor:.3f}px center=({center_dx * args.bin_factor:+.1f}, "
                f"{center_dy * args.bin_factor:+.1f})px overlap={rows[-1]['overlap_with_first_fraction']:.3f}",
                flush=True,
            )
        except RuntimeError as error:
            rows.append(
                {
                    **base,
                    "status": "unmatched",
                    "pair_inliers": "",
                    "pair_rms_native_px": "",
                    "origin_tx_px": "",
                    "origin_ty_px": "",
                    "center_dx_px": "",
                    "center_dy_px": "",
                    "rotation_deg": "",
                    "scale": "",
                    "overlap_with_first_fraction": "",
                    "error": str(error),
                }
            )
            print(f"frame {index}/{len(dated)}: unmatched: {error}", flush=True)
    csv_path = output_dir / "registration_trace.csv"
    fields = list(rows[0])
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    png_path = output_dir / "registration_trace.png"
    write_trace_png(png_path, rows)
    summary_path = output_dir / "registration_trace.json"
    summary_path.write_text(
        json.dumps(
            {
                "source_dir": str(args.source_dir.resolve()),
                "session_gap_min": args.session_gap_min,
                "session_index": session_index,
                "session_count": len(sessions),
                "frame_count": len(rows),
                "matched_count": sum(row["status"] == "ok" for row in rows),
                "first_frame": first_path.name,
                "csv": str(csv_path),
                "graph": str(png_path),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {csv_path}", flush=True)
    print(f"Wrote {png_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
