#!/usr/bin/env python
"""Create a display-only PNG preview from one FITS image.

This developer tool is the experiment entry point for future WCS overlays.
It intentionally does not plate-solve, change a FITS file, or alter headers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from fits_preview import annotate_preview_png, export_preview_png, rotate_preview_png, write_annotation_overlay_png  # noqa: E402
from moving_target_stack import WcsModel, north_up_rotation_degrees, read_fits, sun_pa_left_rotation_degrees  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a display PNG from one FITS image.")
    parser.add_argument("input_fits", type=Path, help="Input .fit or .fits image.")
    parser.add_argument("--output", type=Path, help="Output PNG. Defaults beside the FITS file.")
    parser.add_argument("--stretch", choices=("sigma", "percentile"), default="sigma")
    parser.add_argument("--sigma-low", type=float, default=-1.0)
    parser.add_argument("--sigma-high", type=float, default=3.0)
    parser.add_argument("--low-percentile", type=float, default=5.0)
    parser.add_argument("--high-percentile", type=float, default=99.95)
    parser.add_argument(
        "--north-up",
        "--preview-north-up",
        dest="north_up",
        action="store_true",
        help="Rotate the display PNG from its FITS WCS so celestial north is up.",
    )
    parser.add_argument(
        "--sun-pa-left",
        "--preview-sun-pa-left",
        dest="sun_pa_left",
        action="store_true",
        help="Rotate the display PNG from FITS WCS so the SUN_PA direction is left.",
    )
    parser.add_argument("--preview-at", choices=("none", "UL", "UR", "LL", "LR"), default="none")
    parser.add_argument("--annotate-size", type=float, default=60.0, help="Annotation radius in pixels. Defaults to 60.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.input_fits.resolve()
    if source.suffix.lower() not in {".fit", ".fits"}:
        raise ValueError(f"Input must be a FITS file: {source}")
    if not source.is_file():
        raise FileNotFoundError(f"Input FITS was not found: {source}")
    if args.sigma_high <= args.sigma_low:
        raise ValueError("--sigma-high must be greater than --sigma-low")
    if args.high_percentile <= args.low_percentile:
        raise ValueError("--high-percentile must be greater than --low-percentile")
    if args.north_up and args.sun_pa_left:
        raise ValueError("Choose either --north-up or --sun-pa-left, not both")
    if args.annotate_size <= 0.0:
        raise ValueError("--annotate-size must be positive")

    if args.output:
        output = args.output.resolve()
    elif args.north_up:
        output = source.with_name(f"{source.stem}_north_up_preview.png")
    elif args.sun_pa_left:
        output = source.with_name(f"{source.stem}_sun_pa_left_preview.png")
    else:
        output = source.with_name(f"{source.stem}_preview.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    image = read_fits(source)
    rotate_mode = args.north_up or args.sun_pa_left
    intermediate = output.with_name(f".{output.stem}.unrotated.png") if rotate_mode else output
    try:
        export_preview_png(
            intermediate,
            image.data,
            stretch=args.stretch,
            sigma_low=args.sigma_low,
            sigma_high=args.sigma_high,
            low_percentile=args.low_percentile,
            high_percentile=args.high_percentile,
        )
        rotation = None
        if args.north_up:
            rotation = north_up_rotation_degrees(WcsModel(header=image.header))
            rotate_preview_png(intermediate, output, rotation)
        elif args.sun_pa_left:
            try:
                sun_pa = float(image.header["SUN_PA"])
                reference_dec = float(image.header.get("MTREFDEC", image.header["CRVAL2"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("--sun-pa-left requires numeric SUN_PA and MTREFDEC (or CRVAL2) FITS headers") from exc
            rotation = sun_pa_left_rotation_degrees(WcsModel(header=image.header), reference_dec, sun_pa)
            rotate_preview_png(intermediate, output, rotation)
        if args.preview_at != "none":
            try:
                sun_pa = float(image.header["SUN_PA"])
                reference_dec = float(image.header.get("MTREFDEC", image.header["CRVAL2"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("--preview-at requires numeric SUN_PA and MTREFDEC (or CRVAL2) FITS headers") from exc
            wcs = WcsModel(header=image.header)
            annotate_preview_png(
                output,
                output,
                wcs.cd_matrix(),
                reference_dec,
                sun_pa,
                image_rotation_degrees=rotation or 0.0,
                corner=args.preview_at,
                radius_px=args.annotate_size,
            )
            overlay_output = output.with_name(f"{output.stem}_annotation_overlay.png")
            write_annotation_overlay_png(
                overlay_output,
                wcs.cd_matrix(),
                reference_dec,
                sun_pa,
                image_rotation_degrees=rotation or 0.0,
                radius_px=args.annotate_size,
            )
    finally:
        if intermediate != output and intermediate.exists():
            intermediate.unlink()
    print(f"Input:   {source}")
    print(f"Output:  {output}")
    print(f"Shape:   {tuple(image.data.shape)}")
    print(f"Stretch: {args.stretch}")
    if rotation is not None:
        orientation = "north-up" if args.north_up else "sun-pa-left"
        print(f"{orientation} rotation: {rotation:.6f} deg")
    if args.preview_at != "none":
        print(f"Annotation: N/E/Sun at {args.preview_at}; radius={args.annotate_size:g}px")
        print(f"Overlay: {overlay_output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Error: {error}", file=sys.stderr)
        raise SystemExit(1)
