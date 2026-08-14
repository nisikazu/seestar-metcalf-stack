#!/usr/bin/env python
"""Diagnose a reference-free pairwise star registration for Seestar FITS.

This is an experimental tool.  It estimates the similarity transform that maps
one FITS frame (target) into another (reference), without Siril, Astrometry.net
or any dependencies beyond numpy and Pillow.  It deliberately stops at two
frames so its star detection and matching can be validated before it is used by
the stacking pipeline.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from moving_target_stack import read_fits


@dataclass(frozen=True)
class DetectedStar:
    x: float
    y: float
    flux: float


@dataclass(frozen=True)
class SimilarityTransform:
    """Maps source pixel coordinates into destination pixel coordinates."""

    scale: float
    rotation_deg: float
    tx: float
    ty: float

    def apply(self, points: np.ndarray) -> np.ndarray:
        angle = math.radians(self.rotation_deg)
        cosine = math.cos(angle)
        sine = math.sin(angle)
        matrix = self.scale * np.array([[cosine, -sine], [sine, cosine]], dtype=np.float64)
        return np.asarray(points, dtype=np.float64) @ matrix.T + np.array([self.tx, self.ty])


@dataclass(frozen=True)
class PairRegistration:
    transform: SimilarityTransform
    inlier_pairs: list[tuple[int, int, float]]
    rms_px: float


def registration_plane(data: np.ndarray, bin_factor: int) -> np.ndarray:
    """Return a binned luminance plane while keeping native FITS orientation."""
    plane = np.asarray(data, dtype=np.float64)
    if plane.ndim == 3:
        plane = np.mean(plane, axis=0)
    if plane.ndim != 2:
        raise ValueError(f"Expected a 2D or channel-first FITS image, got shape {plane.shape}")
    if bin_factor < 1:
        raise ValueError("bin_factor must be at least 1")
    height = plane.shape[0] // bin_factor * bin_factor
    width = plane.shape[1] // bin_factor * bin_factor
    if height < bin_factor or width < bin_factor:
        raise ValueError("Image is smaller than the requested bin factor")
    plane = plane[:height, :width]
    if bin_factor > 1:
        plane = plane.reshape(height // bin_factor, bin_factor, width // bin_factor, bin_factor).mean(axis=(1, 3))
    return plane.astype(np.float32, copy=False)


def robust_sigma(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return 1.0
    median = float(np.median(finite))
    sigma = float(np.median(np.abs(finite - median)) * 1.4826)
    return max(sigma, 1e-6)


def local_background(plane: np.ndarray, tile_size: int) -> tuple[np.ndarray, np.ndarray]:
    """Estimate a piecewise-constant local background and noise map."""
    height, width = plane.shape
    background = np.empty_like(plane, dtype=np.float32)
    sigma = np.empty_like(plane, dtype=np.float32)
    for y0 in range(0, height, tile_size):
        y1 = min(y0 + tile_size, height)
        for x0 in range(0, width, tile_size):
            x1 = min(x0 + tile_size, width)
            tile = plane[y0:y1, x0:x1]
            finite = tile[np.isfinite(tile)]
            level = float(np.median(finite)) if finite.size else 0.0
            background[y0:y1, x0:x1] = level
            sigma[y0:y1, x0:x1] = robust_sigma(tile)
    return background, sigma


def local_maximum_3x3(plane: np.ndarray) -> np.ndarray:
    padded = np.pad(plane, 1, mode="constant", constant_values=-np.inf)
    maximum = np.full_like(plane, -np.inf)
    height, width = plane.shape
    for y in range(3):
        for x in range(3):
            maximum = np.maximum(maximum, padded[y : y + height, x : x + width])
    return maximum


def detect_stars(
    plane: np.ndarray,
    threshold_sigma: float = 6.0,
    max_stars: int = 80,
    minimum_separation: float = 5.0,
    centroid_radius: int = 3,
) -> list[DetectedStar]:
    """Detect isolated stellar peaks with local background estimates.

    The detector is intentionally conservative.  Broad comet heads, clouds and
    hot pixels can still appear in the candidate list, but the geometric matcher
    rejects them unless they form a consistent asterism.
    """
    if plane.ndim != 2:
        raise ValueError("Star detection requires a 2D plane")
    if max_stars < 1:
        raise ValueError("max_stars must be positive")
    background, sigma = local_background(plane, tile_size=64)
    peaks = local_maximum_3x3(plane)
    edge = max(centroid_radius + 1, int(math.ceil(minimum_separation)))
    candidates = (plane == peaks) & (plane > background + threshold_sigma * sigma)
    candidates[:edge, :] = False
    candidates[-edge:, :] = False
    candidates[:, :edge] = False
    candidates[:, -edge:] = False
    ys, xs = np.nonzero(candidates)
    ranked = sorted(
        zip(xs.tolist(), ys.tolist()),
        key=lambda point: float(plane[point[1], point[0]] - background[point[1], point[0]]),
        reverse=True,
    )
    selected: list[DetectedStar] = []
    min_distance_squared = minimum_separation * minimum_separation
    for x, y in ranked:
        if any((star.x - x) ** 2 + (star.y - y) ** 2 < min_distance_squared for star in selected):
            continue
        patch = plane[y - centroid_radius : y + centroid_radius + 1, x - centroid_radius : x + centroid_radius + 1]
        level = float(background[y, x])
        weights = np.clip(patch - level, 0.0, None)
        total = float(np.sum(weights))
        if total <= 0.0:
            continue
        yy, xx = np.mgrid[y - centroid_radius : y + centroid_radius + 1, x - centroid_radius : x + centroid_radius + 1]
        centroid_x = float(np.sum(xx * weights) / total)
        centroid_y = float(np.sum(yy * weights) / total)
        selected.append(DetectedStar(centroid_x, centroid_y, total))
        if len(selected) >= max_stars:
            break
    return selected


def points_from_stars(stars: list[DetectedStar], limit: int | None = None) -> np.ndarray:
    if limit is not None:
        stars = stars[:limit]
    return np.asarray([(star.x, star.y) for star in stars], dtype=np.float64)


def fit_similarity(source: np.ndarray, destination: np.ndarray) -> SimilarityTransform:
    """Fit a rotation, uniform scale and translation from paired coordinates."""
    source = np.asarray(source, dtype=np.float64)
    destination = np.asarray(destination, dtype=np.float64)
    if source.shape != destination.shape or source.ndim != 2 or source.shape[1] != 2 or len(source) < 2:
        raise ValueError("Similarity fitting requires at least two paired Nx2 point arrays")
    source_center = np.mean(source, axis=0)
    destination_center = np.mean(destination, axis=0)
    centered_source = source - source_center
    centered_destination = destination - destination_center
    dot = float(np.sum(centered_source[:, 0] * centered_destination[:, 0] + centered_source[:, 1] * centered_destination[:, 1]))
    cross = float(np.sum(centered_source[:, 0] * centered_destination[:, 1] - centered_source[:, 1] * centered_destination[:, 0]))
    angle = math.atan2(cross, dot)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    rotated_source = centered_source @ np.array([[cosine, sine], [-sine, cosine]], dtype=np.float64)
    denominator = float(np.sum(centered_source * centered_source))
    if denominator <= 0.0:
        raise ValueError("Source points are degenerate")
    scale = float(np.sum(rotated_source * centered_destination) / denominator)
    translation = destination_center - scale * (source_center @ np.array([[cosine, sine], [-sine, cosine]], dtype=np.float64))
    return SimilarityTransform(scale, math.degrees(angle), float(translation[0]), float(translation[1]))


def unique_nearest_matches(
    transform: SimilarityTransform,
    source: np.ndarray,
    destination: np.ndarray,
    maximum_distance: float,
) -> list[tuple[int, int, float]]:
    transformed = transform.apply(source)
    distances_squared = np.sum((transformed[:, np.newaxis, :] - destination[np.newaxis, :, :]) ** 2, axis=2)
    destination_indices = np.argmin(distances_squared, axis=1)
    candidates = sorted(
        (
            (float(math.sqrt(distances_squared[source_index, destination_index])), source_index, int(destination_index))
            for source_index, destination_index in enumerate(destination_indices)
            if distances_squared[source_index, destination_index] <= maximum_distance * maximum_distance
        ),
        key=lambda item: item[0],
    )
    used_destination: set[int] = set()
    matches: list[tuple[int, int, float]] = []
    for distance, source_index, destination_index in candidates:
        if destination_index in used_destination:
            continue
        used_destination.add(destination_index)
        matches.append((source_index, destination_index, distance))
    return matches


def estimate_similarity_transform(
    source: np.ndarray,
    destination: np.ndarray,
    *,
    match_radius: float = 2.0,
    min_inliers: int = 6,
    scale_min: float = 0.90,
    scale_max: float = 1.10,
    maximum_points: int = 25,
    maximum_trials: int = 20000,
) -> PairRegistration:
    """Estimate a robust source-to-destination transform from star positions.

    Pair distances are translation-independent.  Candidate transforms created
    from similarly sized star pairs are evaluated against all selected stars,
    then refined with their inliers.  This is a deterministic RANSAC-style
    search which needs no scipy or OpenCV.
    """
    source = np.asarray(source, dtype=np.float64)[:maximum_points]
    destination = np.asarray(destination, dtype=np.float64)[:maximum_points]
    if len(source) < min_inliers or len(destination) < min_inliers:
        raise RuntimeError(
            f"Need at least {min_inliers} detected stars in both images; found {len(source)} and {len(destination)}"
        )
    source_pairs = [
        (left, right, float(np.linalg.norm(source[right] - source[left])))
        for left in range(len(source))
        for right in range(left + 1, len(source))
    ]
    destination_pairs = [
        (left, right, float(np.linalg.norm(destination[right] - destination[left])))
        for left in range(len(destination))
        for right in range(left + 1, len(destination))
    ]
    best: PairRegistration | None = None
    trials = 0
    for source_left, source_right, source_length in source_pairs:
        if source_length < 2.0:
            continue
        for destination_left, destination_right, destination_length in destination_pairs:
            if destination_length < source_length * scale_min or destination_length > source_length * scale_max:
                continue
            for destination_pair in ((destination_left, destination_right), (destination_right, destination_left)):
                trials += 1
                if trials > maximum_trials:
                    break
                try:
                    candidate = fit_similarity(
                        source[[source_left, source_right]],
                        destination[list(destination_pair)],
                    )
                except ValueError:
                    continue
                if not scale_min <= candidate.scale <= scale_max:
                    continue
                matches = unique_nearest_matches(candidate, source, destination, match_radius)
                if len(matches) < min_inliers:
                    continue
                for _ in range(2):
                    candidate = fit_similarity(
                        source[[match[0] for match in matches]],
                        destination[[match[1] for match in matches]],
                    )
                    matches = unique_nearest_matches(candidate, source, destination, match_radius)
                    if len(matches) < min_inliers:
                        break
                if len(matches) < min_inliers:
                    continue
                rms = math.sqrt(sum(match[2] ** 2 for match in matches) / len(matches))
                result = PairRegistration(candidate, matches, rms)
                if best is None or (len(result.inlier_pairs), -result.rms_px) > (len(best.inlier_pairs), -best.rms_px):
                    best = result
            if trials > maximum_trials:
                break
        if trials > maximum_trials:
            break
    if best is None:
        raise RuntimeError("No robust star correspondence was found")
    return best


def preview_uint8(plane: np.ndarray) -> np.ndarray:
    finite = plane[np.isfinite(plane) & (plane != 0.0)]
    if finite.size == 0:
        return np.zeros(plane.shape, dtype=np.uint8)
    low, high = np.percentile(finite, [1.0, 99.8])
    if high <= low:
        high = low + 1.0
    return np.clip((plane - low) * 255.0 / (high - low), 0.0, 255.0).astype(np.uint8)


def write_diagnostic_png(
    path: Path,
    reference: np.ndarray,
    target: np.ndarray,
    reference_stars: list[DetectedStar],
    target_stars: list[DetectedStar],
    registration: PairRegistration,
) -> None:
    reference_image = Image.fromarray(preview_uint8(reference), mode="L").convert("RGB")
    target_image = Image.fromarray(preview_uint8(target), mode="L").convert("RGB")
    width, height = reference_image.size
    canvas = Image.new("RGB", (width * 2, height + 22), "black")
    canvas.paste(reference_image, (0, 22))
    canvas.paste(target_image, (width, 22))
    draw = ImageDraw.Draw(canvas)
    draw.text((4, 4), "reference", fill="white")
    draw.text((width + 4, 4), "target", fill="white")
    for star in reference_stars:
        draw.ellipse((star.x - 2, star.y + 20 - 2, star.x + 2, star.y + 20 + 2), outline=(255, 220, 0))
    for star in target_stars:
        draw.ellipse((width + star.x - 2, star.y + 20 - 2, width + star.x + 2, star.y + 20 + 2), outline=(255, 220, 0))
    for source_index, destination_index, _distance in registration.inlier_pairs:
        destination_star = reference_stars[destination_index]
        source_star = target_stars[source_index]
        draw.line(
            (destination_star.x, destination_star.y + 20, width + source_star.x, source_star.y + 20),
            fill=(0, 220, 255),
            width=1,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path)


def build_summary(
    reference_path: Path,
    target_path: Path,
    bin_factor: int,
    reference_stars: list[DetectedStar],
    target_stars: list[DetectedStar],
    registration: PairRegistration,
) -> dict[str, object]:
    transform = registration.transform
    return {
        "reference": str(reference_path),
        "target": str(target_path),
        "bin_factor": bin_factor,
        "coordinate_system": "binned FITS native pixels; target maps to reference",
        "reference_star_count": len(reference_stars),
        "target_star_count": len(target_stars),
        "transform_binned_pixels": asdict(transform),
        "transform_native_pixels": {
            "scale": transform.scale,
            "rotation_deg": transform.rotation_deg,
            "tx": transform.tx * bin_factor,
            "ty": transform.ty * bin_factor,
        },
        "inlier_count": len(registration.inlier_pairs),
        "rms_binned_px": registration.rms_px,
        "rms_native_px": registration.rms_px * bin_factor,
        "inlier_pairs": [
            {
                "target_index": source_index,
                "reference_index": destination_index,
                "residual_binned_px": distance,
            }
            for source_index, destination_index, distance in registration.inlier_pairs
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="Reference FITS; output coordinates are expressed in this image")
    parser.add_argument("target", type=Path, help="Target FITS to map into the reference image")
    parser.add_argument("--output-dir", type=Path, default=Path("pair_registration"))
    parser.add_argument("--bin-factor", type=int, default=2)
    parser.add_argument("--threshold-sigma", type=float, default=6.0)
    parser.add_argument("--max-stars", type=int, default=80)
    parser.add_argument("--match-stars", type=int, default=25)
    parser.add_argument("--min-inliers", type=int, default=6)
    parser.add_argument("--match-radius", type=float, default=2.0, help="Binned-pixel inlier radius")
    parser.add_argument("--scale-min", type=float, default=0.90)
    parser.add_argument("--scale-max", type=float, default=1.10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reference_path = args.reference.resolve()
    target_path = args.target.resolve()
    reference_plane = registration_plane(read_fits(reference_path).data, args.bin_factor)
    target_plane = registration_plane(read_fits(target_path).data, args.bin_factor)
    reference_stars = detect_stars(reference_plane, args.threshold_sigma, args.max_stars)
    target_stars = detect_stars(target_plane, args.threshold_sigma, args.max_stars)
    registration = estimate_similarity_transform(
        points_from_stars(target_stars),
        points_from_stars(reference_stars),
        match_radius=args.match_radius,
        min_inliers=args.min_inliers,
        scale_min=args.scale_min,
        scale_max=args.scale_max,
        maximum_points=args.match_stars,
    )
    output_dir = args.output_dir.resolve()
    summary = build_summary(
        reference_path,
        target_path,
        args.bin_factor,
        reference_stars,
        target_stars,
        registration,
    )
    json_path = output_dir / "pair_registration.json"
    png_path = output_dir / "pair_registration.png"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_diagnostic_png(png_path, reference_plane, target_plane, reference_stars, target_stars, registration)
    transform = registration.transform
    print(
        "Pair registration succeeded: "
        f"inliers={len(registration.inlier_pairs)} rms={registration.rms_px * args.bin_factor:.3f} native px "
        f"scale={transform.scale:.7f} rotation={transform.rotation_deg:+.5f} deg "
        f"tx={transform.tx * args.bin_factor:+.3f} ty={transform.ty * args.bin_factor:+.3f} native px",
        flush=True,
    )
    print(f"Wrote {json_path}", flush=True)
    print(f"Wrote {png_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
