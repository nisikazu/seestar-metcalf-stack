"""Solar position-angle helpers for moving-target FITS products."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from horizons_ephemeris import build_query, fetch_result, parse_horizons_result


@dataclass(frozen=True)
class ObserverCenter:
    center: str
    longitude_deg: float | None = None
    latitude_deg: float | None = None
    elevation_km: float | None = None


@dataclass(frozen=True)
class SunPosition:
    ra_deg: float
    dec_deg: float
    center: ObserverCenter


def sun_position_angle_deg(
    target_ra_deg: float,
    target_dec_deg: float,
    sun_ra_deg: float,
    sun_dec_deg: float,
) -> float:
    """Return the Sun PA from celestial north through east in degrees.

    This is the division-free form of the spherical position-angle equation,
    which remains well behaved near the celestial poles.
    """
    alpha1 = math.radians(target_ra_deg)
    delta1 = math.radians(target_dec_deg)
    alpha2 = math.radians(sun_ra_deg)
    delta2 = math.radians(sun_dec_deg)
    delta_alpha = alpha2 - alpha1
    y = math.sin(delta_alpha) * math.cos(delta2)
    x = math.cos(delta1) * math.sin(delta2) - math.sin(delta1) * math.cos(delta2) * math.cos(delta_alpha)
    return math.degrees(math.atan2(y, x)) % 360.0


def anti_solar_position_angle_deg(sun_pa_deg: float) -> float:
    return (sun_pa_deg + 180.0) % 360.0


def _as_optional_float(value: object | None) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def observer_center_from_ephemeris_csv(path: Path) -> ObserverCenter | None:
    """Read the Horizons observer center recorded in an ephemeris CSV."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle), None)
    if not row:
        return None
    center = str(row.get("center") or "").strip().lower()
    if center == "geocenter":
        return ObserverCenter(center="geocenter")
    if center != "fits-site":
        return None
    longitude = _as_optional_float(row.get("site_long_deg"))
    latitude = _as_optional_float(row.get("site_lat_deg"))
    elevation = _as_optional_float(row.get("site_elevation_km"))
    if longitude is None or latitude is None:
        return None
    return ObserverCenter("fits-site", longitude, latitude, elevation)


def fetch_sun_position(
    when: datetime,
    center: ObserverCenter,
    retries: int = 3,
    retry_delay_sec: float = 2.0,
    verbose: bool = False,
) -> SunPosition:
    """Query apparent solar RA/Dec from Horizons at the supplied observer center."""
    query = build_query(
        "10",
        [when],
        center.center,
        center.longitude_deg,
        center.latitude_deg,
        center.elevation_km,
    )
    result = fetch_result(query, retries, retry_delay_sec, verbose)
    rows = parse_horizons_result(result, [when])
    if not rows:
        raise RuntimeError("Horizons returned no solar ephemeris row")
    row = rows[0]
    return SunPosition(row.ra_deg, row.dec_deg, center)


def sun_pa_fits_header(target_ra_deg: float, target_dec_deg: float, sun: SunPosition) -> dict[str, object]:
    """Return compact non-standard FITS keywords for a reference target point."""
    sun_pa = sun_position_angle_deg(target_ra_deg, target_dec_deg, sun.ra_deg, sun.dec_deg)
    return {
        "SUN_PA": sun_pa,
        "ASUN_PA": anti_solar_position_angle_deg(sun_pa),
        "SUNRA": sun.ra_deg,
        "SUNDEC": sun.dec_deg,
        "SUNCENTR": "FITS-SIT" if sun.center.center == "fits-site" else "GEOCENTR",
        "SUNSRC": "HORIZONS",
    }
