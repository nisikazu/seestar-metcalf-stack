#!/usr/bin/env python3
"""Compare TAN/SIP FITS WCS solutions on the same image pixel grid."""

from __future__ import annotations

import argparse
import csv
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from moving_target_stack import read_fits_header, wcs_cd_matrix  # noqa: E402


ARCSEC_PER_RADIAN = 180.0 * 3600.0 / math.pi
SIP_TERM = re.compile(r"^(A|B)_(\d+)_(\d+)$")


def parse_solution(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("solution must be LABEL=PATH")
    label, raw_path = value.split("=", 1)
    label = label.strip()
    path = Path(raw_path.strip().strip('"'))
    if not label:
        raise argparse.ArgumentTypeError("solution label must not be empty")
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"WCS FITS does not exist: {path}")
    return label, path.resolve()


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def wrapped_delta_degrees(value: float) -> float:
    return (value + 180.0) % 360.0 - 180.0


def angular_separation_arcsec(
    ra1_deg: float,
    dec1_deg: float,
    ra2_deg: float,
    dec2_deg: float,
) -> float:
    ra1 = math.radians(ra1_deg)
    dec1 = math.radians(dec1_deg)
    ra2 = math.radians(ra2_deg)
    dec2 = math.radians(dec2_deg)
    first = (math.cos(dec1) * math.cos(ra1), math.cos(dec1) * math.sin(ra1), math.sin(dec1))
    second = (math.cos(dec2) * math.cos(ra2), math.cos(dec2) * math.sin(ra2), math.sin(dec2))
    dot = sum(left * right for left, right in zip(first, second))
    cross = (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )
    cross_norm = math.sqrt(sum(value * value for value in cross))
    return math.atan2(cross_norm, min(1.0, max(-1.0, dot))) * ARCSEC_PER_RADIAN


def relative_offsets_arcsec(
    ra1_deg: float,
    dec1_deg: float,
    ra2_deg: float,
    dec2_deg: float,
) -> tuple[float, float]:
    """Return small-angle east/north offsets from point 1 to point 2."""
    delta_ra = math.radians(wrapped_delta_degrees(ra2_deg - ra1_deg))
    mean_dec = math.radians((dec1_deg + dec2_deg) * 0.5)
    east = delta_ra * math.cos(mean_dec) * ARCSEC_PER_RADIAN
    north = math.radians(dec2_deg - dec1_deg) * ARCSEC_PER_RADIAN
    return east, north


def evaluate_sip(header: dict[str, object], prefix: str, u: float, v: float) -> float:
    total = 0.0
    for key, raw_value in header.items():
        match = SIP_TERM.match(key)
        if not match or match.group(1) != prefix:
            continue
        i = int(match.group(2))
        j = int(match.group(3))
        total += float(raw_value) * (u**i) * (v**j)
    return total


def tangent_plane_to_sky(
    xi_deg: float,
    eta_deg: float,
    ra0_deg: float,
    dec0_deg: float,
) -> tuple[float, float]:
    xi = math.radians(xi_deg)
    eta = math.radians(eta_deg)
    ra0 = math.radians(ra0_deg)
    dec0 = math.radians(dec0_deg)
    denominator = math.cos(dec0) - eta * math.sin(dec0)
    ra = ra0 + math.atan2(xi, denominator)
    dec = math.atan2(
        math.sin(dec0) + eta * math.cos(dec0),
        math.hypot(denominator, xi),
    )
    return math.degrees(ra) % 360.0, math.degrees(dec)


@dataclass(frozen=True)
class LocalGeometry:
    scale_x_arcsec: float
    scale_y_arcsec: float
    area_scale_arcsec: float
    y_axis_pa_deg: float
    parity: str


@dataclass
class TanSipWcs:
    label: str
    path: Path
    header: dict[str, object]

    @classmethod
    def load(cls, label: str, path: Path) -> "TanSipWcs":
        header, _cards, _data_offset = read_fits_header(path)
        required = {"CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2"}
        missing = sorted(required - header.keys())
        if missing:
            raise ValueError(f"{label} lacks WCS cards: {', '.join(missing)}")
        ctype1 = str(header.get("CTYPE1", "RA---TAN")).upper()
        ctype2 = str(header.get("CTYPE2", "DEC--TAN")).upper()
        if "TAN" not in ctype1 or "TAN" not in ctype2:
            raise ValueError(f"{label} is not a supported TAN WCS: {ctype1}, {ctype2}")
        wcs_cd_matrix(header)
        return cls(label=label, path=path, header=header)

    def pixel_to_world(self, x: float, y: float) -> tuple[float, float]:
        u = x - float(self.header["CRPIX1"])
        v = y - float(self.header["CRPIX2"])
        corrected_u = u + evaluate_sip(self.header, "A", u, v)
        corrected_v = v + evaluate_sip(self.header, "B", u, v)
        cd11, cd12, cd21, cd22 = wcs_cd_matrix(self.header)
        xi_deg = cd11 * corrected_u + cd12 * corrected_v
        eta_deg = cd21 * corrected_u + cd22 * corrected_v
        return tangent_plane_to_sky(
            xi_deg,
            eta_deg,
            float(self.header["CRVAL1"]),
            float(self.header["CRVAL2"]),
        )

    def local_geometry(self, x: float, y: float) -> LocalGeometry:
        x_minus = self.pixel_to_world(x - 0.5, y)
        x_plus = self.pixel_to_world(x + 0.5, y)
        y_minus = self.pixel_to_world(x, y - 0.5)
        y_plus = self.pixel_to_world(x, y + 0.5)
        east_x, north_x = relative_offsets_arcsec(*x_minus, *x_plus)
        east_y, north_y = relative_offsets_arcsec(*y_minus, *y_plus)
        scale_x = math.hypot(east_x, north_x)
        scale_y = math.hypot(east_y, north_y)
        determinant = east_x * north_y - east_y * north_x
        return LocalGeometry(
            scale_x_arcsec=scale_x,
            scale_y_arcsec=scale_y,
            area_scale_arcsec=math.sqrt(abs(determinant)),
            y_axis_pa_deg=math.degrees(math.atan2(east_y, north_y)) % 360.0,
            parity="positive" if determinant >= 0.0 else "negative",
        )

    def sip_description(self) -> str:
        a_order = int(self.header.get("A_ORDER", 0) or 0)
        b_order = int(self.header.get("B_ORDER", 0) or 0)
        return f"A{a_order}/B{b_order}" if a_order or b_order else "none"


def evenly_spaced(first: float, last: float, count: int) -> list[float]:
    if count <= 1:
        return [(first + last) * 0.5]
    return [first + (last - first) * index / (count - 1) for index in range(count)]


def rms(values: Iterable[float]) -> float:
    collected = list(values)
    return math.sqrt(sum(value * value for value in collected) / len(collected))


def format_float(value: float, digits: int = 4) -> str:
    return f"{value:.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-image", required=True, type=Path)
    parser.add_argument("--solution", action="append", required=True, type=parse_solution)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--grid-x", type=int, default=9)
    parser.add_argument("--grid-y", type=int, default=13)
    parser.add_argument("--title", default="FITS WCS comparison")
    args = parser.parse_args()

    reference_image = args.reference_image.resolve()
    reference_header, _cards, _offset = read_fits_header(reference_image)
    width = int(reference_header["NAXIS1"])
    height = int(reference_header["NAXIS2"])
    center_x = (width + 1.0) * 0.5
    center_y = (height + 1.0) * 0.5
    solutions = [TanSipWcs.load(label, path) for label, path in args.solution]
    if len(solutions) < 2:
        parser.error("at least two --solution values are required")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    x_values = evenly_spaced(1.0, float(width), args.grid_x)
    y_values = evenly_spaced(1.0, float(height), args.grid_y)
    points = [(x, y) for y in y_values for x in x_values]

    solution_rows: list[dict[str, object]] = []
    for solution in solutions:
        center_ra, center_dec = solution.pixel_to_world(center_x, center_y)
        geometry = solution.local_geometry(center_x, center_y)
        solution_rows.append(
            {
                "label": solution.label,
                "path": str(solution.path),
                "center_ra_deg": center_ra,
                "center_dec_deg": center_dec,
                "scale_x_arcsec_per_pixel": geometry.scale_x_arcsec,
                "scale_y_arcsec_per_pixel": geometry.scale_y_arcsec,
                "area_scale_arcsec_per_pixel": geometry.area_scale_arcsec,
                "y_axis_pa_deg": geometry.y_axis_pa_deg,
                "parity": geometry.parity,
                "sip": solution.sip_description(),
            }
        )

    point_rows: list[dict[str, object]] = []
    pair_rows: list[dict[str, object]] = []
    for first_index, first in enumerate(solutions[:-1]):
        first_geometry = first.local_geometry(center_x, center_y)
        for second in solutions[first_index + 1 :]:
            second_geometry = second.local_geometry(center_x, center_y)
            raw_points: list[dict[str, float]] = []
            for x, y in points:
                first_ra, first_dec = first.pixel_to_world(x, y)
                second_ra, second_dec = second.pixel_to_world(x, y)
                east, north = relative_offsets_arcsec(first_ra, first_dec, second_ra, second_dec)
                separation = angular_separation_arcsec(first_ra, first_dec, second_ra, second_dec)
                raw_points.append(
                    {
                        "x": x,
                        "y": y,
                        "first_ra_deg": first_ra,
                        "first_dec_deg": first_dec,
                        "second_ra_deg": second_ra,
                        "second_dec_deg": second_dec,
                        "east_arcsec": east,
                        "north_arcsec": north,
                        "separation_arcsec": separation,
                    }
                )

            mean_east = sum(row["east_arcsec"] for row in raw_points) / len(raw_points)
            mean_north = sum(row["north_arcsec"] for row in raw_points) / len(raw_points)
            residuals: list[float] = []
            for row in raw_points:
                residual = math.hypot(
                    row["east_arcsec"] - mean_east,
                    row["north_arcsec"] - mean_north,
                )
                residuals.append(residual)
                point_rows.append(
                    {
                        "first": first.label,
                        "second": second.label,
                        **row,
                        "translation_removed_residual_arcsec": residual,
                    }
                )

            first_center = first.pixel_to_world(center_x, center_y)
            second_center = second.pixel_to_world(center_x, center_y)
            center_east, center_north = relative_offsets_arcsec(*first_center, *second_center)
            separations = [row["separation_arcsec"] for row in raw_points]
            pair_rows.append(
                {
                    "first": first.label,
                    "second": second.label,
                    "center_east_arcsec": center_east,
                    "center_north_arcsec": center_north,
                    "center_separation_arcsec": angular_separation_arcsec(*first_center, *second_center),
                    "grid_mean_east_arcsec": mean_east,
                    "grid_mean_north_arcsec": mean_north,
                    "grid_median_separation_arcsec": percentile(separations, 50.0),
                    "grid_rms_separation_arcsec": rms(separations),
                    "grid_p95_separation_arcsec": percentile(separations, 95.0),
                    "grid_max_separation_arcsec": max(separations),
                    "translation_removed_rms_arcsec": rms(residuals),
                    "translation_removed_max_arcsec": max(residuals),
                    "scale_difference_percent": (
                        second_geometry.area_scale_arcsec / first_geometry.area_scale_arcsec - 1.0
                    )
                    * 100.0,
                    "y_axis_pa_difference_deg": wrapped_delta_degrees(
                        second_geometry.y_axis_pa_deg - first_geometry.y_axis_pa_deg
                    ),
                    "parity_match": first_geometry.parity == second_geometry.parity,
                }
            )

    def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    write_csv(output_dir / "solutions.csv", solution_rows)
    write_csv(output_dir / "pairwise_summary.csv", pair_rows)
    write_csv(output_dir / "pairwise_grid.csv", point_rows)

    report_lines = [
        f"# {args.title}",
        "",
        f"- Reference image: `{reference_image}`",
        f"- Image size: {width} x {height} pixels",
        f"- Comparison grid: {args.grid_x} x {args.grid_y} ({len(points)} points, FITS 1-based pixels)",
        "",
        "## Solutions",
        "",
        "| Solution | Center RA (deg) | Center Dec (deg) | Scale X | Scale Y | Area scale | +Y PA | Parity | SIP |",
        "|---|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for row in solution_rows:
        report_lines.append(
            "| {label} | {ra} | {dec} | {sx} | {sy} | {sa} | {pa} | {parity} | {sip} |".format(
                label=row["label"],
                ra=format_float(float(row["center_ra_deg"]), 9),
                dec=format_float(float(row["center_dec_deg"]), 9),
                sx=format_float(float(row["scale_x_arcsec_per_pixel"]), 6),
                sy=format_float(float(row["scale_y_arcsec_per_pixel"]), 6),
                sa=format_float(float(row["area_scale_arcsec_per_pixel"]), 6),
                pa=format_float(float(row["y_axis_pa_deg"]), 6),
                parity=row["parity"],
                sip=row["sip"],
            )
        )
    report_lines.extend(
        [
            "",
            "Scales are arcsec/pixel. `+Y PA` is the position angle of increasing FITS Y, measured from celestial north through east.",
            "",
            "## Pairwise Results",
            "",
            "Offsets are `second - first`. The shape residual removes the best constant east/north offset from every grid point.",
            "",
            "| First -> second | Center sep | Mean E | Mean N | Grid RMS | Grid p95 | Grid max | Shape RMS | Shape max | Scale diff | PA diff |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in pair_rows:
        report_lines.append(
            "| {first} -> {second} | {center} | {east} | {north} | {rms_value} | {p95} | {maximum} | {shape_rms} | {shape_max} | {scale}% | {pa} deg |".format(
                first=row["first"],
                second=row["second"],
                center=format_float(float(row["center_separation_arcsec"]), 4),
                east=format_float(float(row["grid_mean_east_arcsec"]), 4),
                north=format_float(float(row["grid_mean_north_arcsec"]), 4),
                rms_value=format_float(float(row["grid_rms_separation_arcsec"]), 4),
                p95=format_float(float(row["grid_p95_separation_arcsec"]), 4),
                maximum=format_float(float(row["grid_max_separation_arcsec"]), 4),
                shape_rms=format_float(float(row["translation_removed_rms_arcsec"]), 4),
                shape_max=format_float(float(row["translation_removed_max_arcsec"]), 4),
                scale=format_float(float(row["scale_difference_percent"]), 6),
                pa=format_float(float(row["y_axis_pa_difference_deg"]), 6),
            )
        )
    report_lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- Center separation measures disagreement at the geometric image center, not CRVAL-to-CRVAL distance.",
            "- Grid RMS/max include both a constant pointing offset and field-dependent geometry differences.",
            "- Shape RMS/max remove the mean east/north offset and therefore emphasize scale, rotation, and distortion differences.",
            "- Pairwise agreement is not by itself an absolute astrometric error measurement; it is strongest when independent catalog solvers agree.",
            "",
        ]
    )
    report_path = output_dir / "wcs_comparison.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"Wrote {report_path}")
    print(f"Wrote {output_dir / 'solutions.csv'}")
    print(f"Wrote {output_dir / 'pairwise_summary.csv'}")
    print(f"Wrote {output_dir / 'pairwise_grid.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
