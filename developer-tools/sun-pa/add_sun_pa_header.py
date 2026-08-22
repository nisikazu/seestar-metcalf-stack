#!/usr/bin/env python3
"""Add solar position-angle keywords to existing moving-target FITS stacks."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from moving_target_stack import parse_time, read_fits, sun_pa_header_comments, update_fits_header_cards
from sun_pa import ObserverCenter, fetch_sun_position, sun_pa_fits_header


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Add SUN_PA and related solar ephemeris headers to Metcalf stack FITS files."
    )
    parser.add_argument("fits", nargs="+", type=Path, help="Existing moving-target stack FITS file(s).")
    parser.add_argument(
        "--center",
        choices=("geocenter", "fits-site"),
        default="geocenter",
        help="Horizons observer center. Defaults to geocenter.",
    )
    parser.add_argument("--site-longitude", type=float, help="Longitude east, degrees. Required for fits-site.")
    parser.add_argument("--site-latitude", type=float, help="Latitude north, degrees. Required for fits-site.")
    parser.add_argument("--elevation-km", type=float, default=0.0, help="Site elevation in km. Defaults to 0.")
    parser.add_argument("--retries", type=int, default=3, help="Horizons retries. Defaults to 3.")
    parser.add_argument("--dry-run", action="store_true", help="Calculate and report values without updating FITS headers.")
    return parser.parse_args()


def target_coordinates(header: dict[str, object], path: Path) -> tuple[float, float]:
    try:
        return float(header["MTREFRA"]), float(header["MTREFDEC"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path} lacks numeric MTREFRA/MTREFDEC moving-target reference coordinates") from exc


def main() -> int:
    args = parse_args()
    if args.center == "fits-site" and (args.site_longitude is None or args.site_latitude is None):
        raise SystemExit("--center fits-site requires both --site-longitude and --site-latitude")
    center = ObserverCenter(
        args.center,
        args.site_longitude if args.center == "fits-site" else None,
        args.site_latitude if args.center == "fits-site" else None,
        args.elevation_km if args.center == "fits-site" else None,
    )
    for path in args.fits:
        image = read_fits(path)
        if "DATE-OBS" not in image.header:
            raise ValueError(f"{path} lacks DATE-OBS")
        target_ra, target_dec = target_coordinates(image.header, path)
        sun = fetch_sun_position(parse_time(image.header["DATE-OBS"]), center, retries=args.retries, verbose=True)
        values = sun_pa_fits_header(target_ra, target_dec, sun)
        action = "Would write" if args.dry_run else "Wrote"
        print(
            f"{action} {path.name}: SUN_PA={values['SUN_PA']:.5f} deg, "
            f"ASUN_PA={values['ASUN_PA']:.5f} deg, center={values['SUNCENTR']}"
        )
        if not args.dry_run:
            update_fits_header_cards(path, sun_pa_header_comments(values))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
