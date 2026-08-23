#!/usr/bin/env python
"""Moving-target stack for Seestar/Siril FITS subframes.

Pipeline:
1. Copy a clean subset of source FITS files into a work directory.
2. Use Siril CLI to debayer and register frames on background stars.
3. Use a first-frame WCS and a target ephemeris CSV to compute the target
   pixel in the registered first-frame coordinate system for every frame.
4. Shift each registered frame so the target lands on the selected reference
   pixel, then mean- or median-stack the shifted frames.

The script intentionally depends only on numpy and Pillow in addition to Siril.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image


REPO_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[1]
)
SOFTWARE_VERSION = "0.6.3"
DEFAULT_PYTHON = (
    Path.home()
    / ".cache"
    / "codex-runtimes"
    / "codex-primary-runtime"
    / "dependencies"
    / "python"
    / "python.exe"
)


@dataclass
class FitsImage:
    header: dict[str, object]
    cards: list[str]
    data: np.ndarray


@dataclass
class TargetPoint:
    time: datetime
    ra_deg: float
    dec_deg: float


@dataclass
class SirilRegistration:
    index: int
    selected: bool | None = None
    reference_index: int | None = None
    detected_stars: int | None = None
    fwhm_px: float | None = None
    weighted_fwhm_px: float | None = None
    roundness: float | None = None
    matrix: tuple[float, float, float, float, float, float, float, float, float] | None = None

    @property
    def star_tx_px(self) -> float | None:
        return None if self.matrix is None else self.matrix[2]

    @property
    def star_ty_px(self) -> float | None:
        return None if self.matrix is None else self.matrix[5]

    @property
    def star_rotation_deg(self) -> float | None:
        if self.matrix is None:
            return None
        return math.degrees(math.atan2(self.matrix[3], self.matrix[0]))

    @property
    def star_scale(self) -> float | None:
        if self.matrix is None:
            return None
        return math.hypot(self.matrix[0], self.matrix[3])


@dataclass
class SirilMatchDiagnostics:
    index: int
    initial_pairs: int | None = None
    fitted_pairs: int | None = None
    inlier_fraction: float | None = None


class SirilRegistrationError(RuntimeError):
    """Registration failure with Siril's output retained for diagnosis."""

    def __init__(self, message: str, output: str):
        super().__init__(message)
        self.output = output


class WcsModel:
    def __init__(self, header: dict[str, object] | None = None, calibration: dict[str, object] | None = None):
        self.header = header
        self.calibration = calibration
        if not header and not calibration:
            raise ValueError("Either a WCS FITS header or astrometry calibration is required")

    @classmethod
    def from_wcs_fits(cls, path: Path) -> "WcsModel":
        header, _cards, _offset = read_fits_header(path)
        return cls(header=header)

    @classmethod
    def from_astrometry_json(cls, path: Path, width: int, height: int) -> "WcsModel":
        obj = json.loads(path.read_text(encoding="utf-8"))
        calibration = obj.get("calibration") or obj.get("results", {}).get("calibration")
        if not calibration:
            raise ValueError(f"No calibration object found in {path}")
        calibration = dict(calibration)
        calibration.setdefault("imagew", width)
        calibration.setdefault("imageh", height)
        return cls(calibration=calibration)

    def world_to_pixel(self, ra_deg: float, dec_deg: float) -> tuple[float, float]:
        if self.header:
            return self._world_to_pixel_cd(ra_deg, dec_deg)
        return self._world_to_pixel_calibration(ra_deg, dec_deg)

    def _world_to_pixel_cd(self, ra_deg: float, dec_deg: float) -> tuple[float, float]:
        h = self.header or {}
        ra0 = float(h["CRVAL1"])
        dec0 = float(h["CRVAL2"])
        crpix1 = float(h["CRPIX1"])
        crpix2 = float(h["CRPIX2"])
        cd11 = float(h["CD1_1"])
        cd12 = float(h["CD1_2"])
        cd21 = float(h["CD2_1"])
        cd22 = float(h["CD2_2"])

        xi_deg, eta_deg = tangent_plane_offsets_deg(ra_deg, dec_deg, ra0, dec0)
        det = cd11 * cd22 - cd12 * cd21
        if abs(det) < 1e-20:
            raise ValueError("WCS CD matrix is singular")
        dx = (cd22 * xi_deg - cd12 * eta_deg) / det
        dy = (-cd21 * xi_deg + cd11 * eta_deg) / det
        return crpix1 + dx, crpix2 + dy

    def _world_to_pixel_calibration(self, ra_deg: float, dec_deg: float) -> tuple[float, float]:
        c = self.calibration or {}
        ra0 = float(c["ra"])
        dec0 = float(c["dec"])
        width = float(c.get("imagew") or c.get("width") or 1080)
        height = float(c.get("imageh") or c.get("height") or 1920)
        pixscale = float(c["pixscale"])
        theta = math.radians(float(c["orientation"]))
        xi_deg, eta_deg = tangent_plane_offsets_deg(ra_deg, dec_deg, ra0, dec0)
        east_arcsec = xi_deg * 3600.0
        north_arcsec = eta_deg * 3600.0
        dx = (math.cos(theta) * east_arcsec - math.sin(theta) * north_arcsec) / pixscale
        dy = (math.sin(theta) * east_arcsec + math.cos(theta) * north_arcsec) / pixscale
        return (width + 1.0) / 2.0 + dx, (height + 1.0) / 2.0 + dy

    def to_fits_header(self, width: int, height: int) -> dict[str, object]:
        if self.header:
            keys = [
                "WCSAXES",
                "CTYPE1",
                "CTYPE2",
                "EQUINOX",
                "RADESYS",
                "CRVAL1",
                "CRVAL2",
                "CRPIX1",
                "CRPIX2",
                "CD1_1",
                "CD1_2",
                "CD2_1",
                "CD2_2",
                "CDELT1",
                "CDELT2",
                "CROTA1",
                "CROTA2",
            ]
            return {key: self.header[key] for key in keys if key in self.header}
        c = self.calibration or {}
        pixscale_deg = float(c["pixscale"]) / 3600.0
        theta = math.radians(float(c["orientation"]))
        return {
            "WCSAXES": 2,
            "CTYPE1": "RA---TAN",
            "CTYPE2": "DEC--TAN",
            "CRVAL1": float(c["ra"]),
            "CRVAL2": float(c["dec"]),
            "CRPIX1": (width + 1.0) / 2.0,
            "CRPIX2": (height + 1.0) / 2.0,
            "CD1_1": pixscale_deg * math.cos(theta),
            "CD1_2": pixscale_deg * math.sin(theta),
            "CD2_1": -pixscale_deg * math.sin(theta),
            "CD2_2": pixscale_deg * math.cos(theta),
            "RADESYS": "ICRS",
        }


def tangent_plane_offsets_deg(ra_deg: float, dec_deg: float, ra0_deg: float, dec0_deg: float) -> tuple[float, float]:
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    ra0 = math.radians(ra0_deg)
    dec0 = math.radians(dec0_deg)
    dra = normalize_rad(ra - ra0)
    denom = math.sin(dec) * math.sin(dec0) + math.cos(dec) * math.cos(dec0) * math.cos(dra)
    xi = math.cos(dec) * math.sin(dra) / denom
    eta = (math.sin(dec) * math.cos(dec0) - math.cos(dec) * math.sin(dec0) * math.cos(dra)) / denom
    return math.degrees(xi), math.degrees(eta)


def normalize_rad(value: float) -> float:
    while value <= -math.pi:
        value += 2.0 * math.pi
    while value > math.pi:
        value -= 2.0 * math.pi
    return value


def parse_fits_value(raw: str) -> object:
    value = raw.split("/", 1)[0].strip()
    if value.startswith("'"):
        end = value.find("'", 1)
        return value[1:end].strip() if end >= 0 else value.strip("'")
    if value == "T":
        return True
    if value == "F":
        return False
    try:
        if any(ch in value.upper() for ch in [".", "E", "D"]):
            return float(value.replace("D", "E"))
        return int(value)
    except ValueError:
        return value


def read_fits_header(path: Path) -> tuple[dict[str, object], list[str], int]:
    header: dict[str, object] = {}
    cards: list[str] = []
    with path.open("rb") as handle:
        block_index = 0
        while True:
            block = handle.read(2880)
            if not block:
                raise ValueError(f"FITS END card not found in {path}")
            for offset in range(0, len(block), 80):
                card = block[offset : offset + 80].decode("ascii", errors="replace")
                cards.append(card)
                key = card[:8].strip()
                if key == "END":
                    return header, cards, (block_index + 1) * 2880
                if len(card) > 9 and card[8] == "=":
                    header[key] = parse_fits_value(card[10:])
            block_index += 1


def read_fits(path: Path) -> FitsImage:
    header, cards, data_offset = read_fits_header(path)
    bitpix = int(header["BITPIX"])
    naxis = int(header.get("NAXIS", 0))
    if naxis < 2:
        raise ValueError(f"Unsupported FITS dimensions in {path}")
    width = int(header["NAXIS1"])
    height = int(header["NAXIS2"])
    channels = int(header.get("NAXIS3", 1))
    count = width * height * channels
    dtype_map = {
        8: ">u1",
        16: ">i2",
        32: ">i4",
        -32: ">f4",
        -64: ">f8",
    }
    if bitpix not in dtype_map:
        raise ValueError(f"Unsupported BITPIX={bitpix} in {path}")
    dtype = np.dtype(dtype_map[bitpix])
    with path.open("rb") as handle:
        handle.seek(data_offset)
        raw = handle.read(count * dtype.itemsize)
    data = np.frombuffer(raw, dtype=dtype, count=count).astype(np.float32)
    if channels > 1:
        data = data.reshape((channels, height, width))
    else:
        data = data.reshape((height, width))
    bscale = float(header.get("BSCALE", 1.0))
    bzero = float(header.get("BZERO", 0.0))
    if bscale != 1.0 or bzero != 0.0:
        data = data * bscale + bzero
    return FitsImage(header=header, cards=cards, data=data)


def unsigned_uint16_full_scale(header: dict[str, object]) -> float | None:
    try:
        bitpix = int(header.get("BITPIX", 0))
        bzero = float(header.get("BZERO", 0.0))
        bscale = float(header.get("BSCALE", 1.0))
    except (TypeError, ValueError):
        return None
    if bitpix == 16 and bzero == 32768.0 and bscale == 1.0:
        return 65535.0
    return None


def fits_saturation_level(header: dict[str, object]) -> float | None:
    """Return the physical-count saturation level represented by a FITS image."""
    for key in ("SATURATE", "SATLEVEL"):
        try:
            value = float(header[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0.0:
            return value

    try:
        bitpix = int(header.get("BITPIX", 0))
        bscale = float(header.get("BSCALE", 1.0))
        bzero = float(header.get("BZERO", 0.0))
    except (TypeError, ValueError):
        return None
    integer_limits = {
        8: (0.0, 255.0),
        16: (-32768.0, 32767.0),
        32: (-2147483648.0, 2147483647.0),
    }
    if bitpix not in integer_limits or not math.isfinite(bscale) or not math.isfinite(bzero):
        return None
    raw_low, raw_high = integer_limits[bitpix]
    return max(raw_low * bscale + bzero, raw_high * bscale + bzero)


def normalize_saturation_color(value: str) -> str:
    color = str(value).strip().lstrip("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", color):
        raise ValueError("--saturation-color must be a six-digit RGB hex value such as FF0000")
    return color


def saturation_rgb(value: str) -> tuple[int, int, int]:
    color = normalize_saturation_color(value)
    return tuple(int(color[offset : offset + 2], 16) for offset in (0, 2, 4))


def detect_saturation(
    data: np.ndarray,
    source_header: dict[str, object],
    threshold_percent: float,
) -> tuple[np.ndarray, float | None, float | None, float | None]:
    """Return a 2D mask and count statistics for one registered subframe."""
    level = fits_saturation_level(source_header)
    finite = data[np.isfinite(data)]
    maximum = float(np.max(finite)) if finite.size else None
    if level is None:
        return np.zeros(data.shape[-2:], dtype=bool), None, None, maximum
    threshold = level * threshold_percent / 100.0
    over = np.isfinite(data) & (data > threshold)
    mask = np.any(over, axis=0) if data.ndim == 3 else over
    return mask, level, threshold, maximum


def shift_boolean_mask(mask: np.ndarray, dx: float, dy: float) -> np.ndarray:
    """Conservatively mark every output pixel touched by a shifted true pixel."""
    shifted, valid = shift_plane(mask.astype(np.float32, copy=False), dx, dy)
    return valid & (shifted > 0.0)


def restore_registered_units(image: FitsImage, source_header: dict[str, object]) -> tuple[FitsImage, float]:
    """Siril may write registered float FITS normalized to 0..1; restore ADU."""
    source_full_scale = unsigned_uint16_full_scale(source_header)
    try:
        registered_bitpix = int(image.header.get("BITPIX", 0))
    except (TypeError, ValueError):
        registered_bitpix = 0
    finite = image.data[np.isfinite(image.data)]
    data_max = float(np.max(finite)) if finite.size else 0.0
    if source_full_scale and registered_bitpix < 0 and data_max <= 1.5:
        restored = image.data.astype(np.float64) * source_full_scale
        return FitsImage(header=image.header, cards=image.cards, data=restored), source_full_scale
    return image, 1.0


def format_card(key: str, value: object | None = None, comment: str | None = None) -> str:
    if value is None:
        text = key
    else:
        if isinstance(value, bool):
            value_text = "T" if value else "F"
            text = f"{key:<8}= {value_text:>20}"
        elif isinstance(value, int):
            text = f"{key:<8}= {value:>20d}"
        elif isinstance(value, float):
            text = f"{key:<8}= {value:>20.10E}"
        else:
            safe = str(value).replace("'", "")
            text = f"{key:<8}= '{safe:<18}'"
        if comment:
            text += f" / {comment}"
    return text[:80].ljust(80)


def format_history_card(text: str) -> str:
    """Return a FITS HISTORY card without introducing non-ASCII text."""
    return f"HISTORY {str(text)}"[:80].ljust(80)


def image_shape_chw(data: np.ndarray) -> tuple[int, int, int, np.ndarray]:
    if data.ndim == 2:
        channels = 1
        height, width = data.shape
        out = data[np.newaxis, :, :]
    elif data.ndim == 3:
        channels, height, width = data.shape
        out = data
    else:
        raise ValueError("Only 2D or CHW 3D FITS output is supported")
    return channels, height, width, out


def concatenate_side_by_side(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    if left.shape != right.shape:
        raise ValueError(f"Cannot combine stacks with different shapes: {left.shape} vs {right.shape}")
    axis = 1 if left.ndim == 2 else 2
    return np.concatenate([left, right], axis=axis)


def write_fits_float32(
    path: Path,
    data: np.ndarray,
    source_header: dict[str, object],
    extra: dict[str, object],
    history: list[str] | None = None,
) -> None:
    channels, height, width, out = image_shape_chw(data)

    cards = [
        format_card("SIMPLE", True),
        format_card("BITPIX", -32),
        format_card("NAXIS", 3 if channels > 1 else 2),
        format_card("NAXIS1", width),
        format_card("NAXIS2", height),
    ]
    if channels > 1:
        cards.append(format_card("NAXIS3", channels))
    for key in ["OBJECT", "DATE-OBS", "FILTER", "GAIN", "EXPOSURE"]:
        if key in source_header:
            cards.append(format_card(key, source_header[key]))
    for key, value in extra.items():
        cards.append(format_card(key, value))
    cards.append(format_history_card("Moving-target stack generated by scripts/moving_target_stack.py"))
    for line in history or []:
        cards.append(format_history_card(line))
    cards.append("END".ljust(80))
    header_bytes = "".join(cards).encode("ascii", errors="replace")
    pad = (-len(header_bytes)) % 2880
    header_bytes += b" " * pad

    be = np.nan_to_num(out.astype(np.float32), nan=0.0).astype(">f4", copy=False)
    data_bytes = be.tobytes(order="C")
    data_bytes += b"\0" * ((-len(data_bytes)) % 2880)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header_bytes + data_bytes)


def scale_to_uint16(
    data: np.ndarray,
    mode: str,
    low_percentile: float,
    high_percentile: float,
) -> tuple[np.ndarray, list[dict[str, float]]]:
    channels, _height, _width, out = image_shape_chw(data)
    scaled = np.zeros_like(out, dtype=np.uint16)
    stats: list[dict[str, float]] = []

    if mode == "none":
        clipped = np.clip(out, 0.0, 65535.0)
        scaled = np.rint(clipped).astype(np.uint16)
        for channel in range(channels):
            stats.append({"low": 0.0, "high": 65535.0})
        return scaled, stats

    if mode == "global":
        finite = out[np.isfinite(out)]
        if finite.size == 0:
            low, high = 0.0, 1.0
        else:
            low, high = np.percentile(finite, [low_percentile, high_percentile])
            if high <= low:
                high = low + 1.0
        normalized = np.clip((out - low) / (high - low), 0.0, 1.0)
        scaled = np.rint(normalized * 65535.0).astype(np.uint16)
        for _channel in range(channels):
            stats.append({"low": float(low), "high": float(high)})
        return scaled, stats

    if mode != "per-channel":
        raise ValueError(f"Unknown uint16 scale mode: {mode}")

    for channel in range(channels):
        plane = out[channel]
        finite = plane[np.isfinite(plane)]
        if finite.size == 0:
            low, high = 0.0, 1.0
        else:
            low, high = np.percentile(finite, [low_percentile, high_percentile])
            if high <= low:
                high = low + 1.0
        normalized = np.clip((plane - low) / (high - low), 0.0, 1.0)
        scaled[channel] = np.rint(normalized * 65535.0).astype(np.uint16)
        stats.append({"low": float(low), "high": float(high)})
    return scaled, stats


def write_fits_uint16(
    path: Path,
    data: np.ndarray,
    source_header: dict[str, object],
    extra: dict[str, object],
    scale_mode: str,
    low_percentile: float,
    high_percentile: float,
    history: list[str] | None = None,
) -> list[dict[str, float]]:
    channels, height, width, _out = image_shape_chw(data)
    scaled, stats = scale_to_uint16(data, scale_mode, low_percentile, high_percentile)

    cards = [
        format_card("SIMPLE", True),
        format_card("BITPIX", 16),
        format_card("NAXIS", 3 if channels > 1 else 2),
        format_card("NAXIS1", width),
        format_card("NAXIS2", height),
    ]
    if channels > 1:
        cards.append(format_card("NAXIS3", channels))
    # Store unsigned 16-bit pixels using the standard FITS signed-int offset.
    cards.extend([format_card("BZERO", 32768), format_card("BSCALE", 1)])
    for key in ["OBJECT", "DATE-OBS", "FILTER", "GAIN", "EXPOSURE"]:
        if key in source_header:
            cards.append(format_card(key, source_header[key]))
    for key, value in extra.items():
        cards.append(format_card(key, value))
    cards.extend(
        [
            format_card("MTSCALE", scale_mode),
            format_card("MTLIN", scale_mode == "none", "linear ADU-preserving uint16 output"),
            format_card("MTLOWP", low_percentile),
            format_card("MTHIGHP", high_percentile),
        ]
    )
    for idx, stat in enumerate(stats, start=1):
        cards.append(format_card(f"MTLO{idx}", stat["low"]))
        cards.append(format_card(f"MTHI{idx}", stat["high"]))
    cards.append(format_history_card("Moving-target stack generated by scripts/moving_target_stack.py"))
    for line in history or []:
        cards.append(format_history_card(line))
    cards.append("END".ljust(80))
    header_bytes = "".join(cards).encode("ascii", errors="replace")
    header_bytes += b" " * ((-len(header_bytes)) % 2880)

    signed = (scaled.astype(np.int32) - 32768).astype(">i2", copy=False)
    data_bytes = signed.tobytes(order="C")
    data_bytes += b"\0" * ((-len(data_bytes)) % 2880)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header_bytes + data_bytes)
    return stats


def parse_time(value: object) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_angle(value: str, is_ra: bool) -> float:
    value = str(value).strip()
    if ":" not in value:
        return float(value)
    sign = -1.0 if value.startswith("-") else 1.0
    parts = value.lstrip("+-").split(":")
    a = float(parts[0])
    b = float(parts[1]) if len(parts) > 1 else 0.0
    c = float(parts[2]) if len(parts) > 2 else 0.0
    deg = a + b / 60.0 + c / 3600.0
    if is_ra:
        return deg * 15.0
    return sign * deg


def load_ephemeris(path: Path) -> list[TargetPoint]:
    rows: list[TargetPoint] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            time_text = row.get("time") or row.get("datetime") or row.get("date") or row.get("iso")
            ra_text = row.get("ra_deg") or row.get("ra")
            dec_text = row.get("dec_deg") or row.get("dec")
            if not time_text or not ra_text or not dec_text:
                raise ValueError("Ephemeris CSV must contain time, ra_deg/ra, dec_deg/dec columns")
            rows.append(TargetPoint(parse_time(time_text), parse_angle(ra_text, True), parse_angle(dec_text, False)))
    rows.sort(key=lambda item: item.time)
    if not rows:
        raise ValueError(f"No ephemeris rows found in {path}")
    return rows


def read_ephemeris_metadata(path: Path) -> dict[str, object]:
    """Read optional object/COMMAND metadata without changing CSV compatibility."""
    metadata: dict[str, object] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            row = next(csv.DictReader(handle), None)
    except (OSError, UnicodeDecodeError, csv.Error):
        row = None
    if row:
        for key in ("object", "command", "horizons_object", "horizons_command"):
            value = str(row.get(key) or "").strip()
            if value:
                metadata[key] = value

    sidecar = path.with_suffix(path.suffix + ".meta.json")
    if sidecar.exists():
        try:
            sidecar_metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            sidecar_metadata = {}
        if isinstance(sidecar_metadata, dict):
            for source_key, destination_key in (
                ("object", "object"),
                ("command", "command"),
            ):
                value = str(sidecar_metadata.get(source_key) or "").strip()
                if value:
                    metadata[destination_key] = value
    return metadata


def interpolate_ephemeris(points: list[TargetPoint], when: datetime) -> TargetPoint:
    if len(points) == 1:
        return TargetPoint(when, points[0].ra_deg, points[0].dec_deg)
    when = when.astimezone(timezone.utc)
    if when <= points[0].time:
        lo, hi = points[0], points[1]
    elif when >= points[-1].time:
        lo, hi = points[-2], points[-1]
    else:
        lo, hi = points[0], points[-1]
        for i in range(len(points) - 1):
            if points[i].time <= when <= points[i + 1].time:
                lo, hi = points[i], points[i + 1]
                break
    span = (hi.time - lo.time).total_seconds()
    frac = 0.0 if span == 0 else (when - lo.time).total_seconds() / span
    dra = ((hi.ra_deg - lo.ra_deg + 180.0) % 360.0) - 180.0
    ra = (lo.ra_deg + dra * frac) % 360.0
    dec = lo.dec_deg + (hi.dec_deg - lo.dec_deg) * frac
    return TargetPoint(when, ra, dec)


def registered_valid_mask(data: np.ndarray) -> np.ndarray:
    """Identify finite pixels that are not all-channel registration padding."""
    if data.ndim == 3:
        return np.all(np.isfinite(data), axis=0) & np.any(data != 0.0, axis=0)
    return np.isfinite(data) & (data != 0.0)


def circular_target_mask(
    shape: tuple[int, int],
    x_1based: float,
    y_1based: float,
    radius_px: float,
) -> np.ndarray:
    """Return a deterministic circular mask centered on a WCS pixel position."""
    if len(shape) != 2:
        raise ValueError(f"Target mask shape must be 2D, got {shape}")
    if not math.isfinite(radius_px) or radius_px <= 0.0:
        raise ValueError("Target mask radius must be finite and greater than zero")
    yy, xx = np.indices(shape, dtype=np.float32)
    center_x = np.float32(x_1based - 1.0)
    center_y = np.float32(y_1based - 1.0)
    return (xx - center_x) ** 2 + (yy - center_y) ** 2 <= np.float32(radius_px**2)


def resolve_comet_mask_radius(
    requested_px: float | None,
    fwhm_values: list[float | None],
) -> tuple[float, str]:
    """Resolve an explicit radius or a modest FWHM-based automatic radius."""
    if requested_px is not None:
        if not math.isfinite(requested_px) or requested_px <= 0.0:
            raise ValueError("--comet-mask-radius-px must be finite and greater than zero")
        return float(requested_px), "explicit"
    finite_fwhm = [value for value in fwhm_values if value is not None and math.isfinite(value) and value > 0.0]
    if finite_fwhm:
        median_fwhm = float(np.median(np.asarray(finite_fwhm, dtype=np.float64)))
        return max(6.0, 3.0 * median_fwhm), "auto-fwhm"
    return 8.0, "auto-default"


def resolve_composite_min_star_fraction(
    requested_fraction: float | None,
    used_frames: int,
) -> tuple[float, str]:
    """Resolve the local star-master reliability threshold.

    The default is deliberately conservative for the small target-support
    region.  It is applied only inside the observed target trajectory, so
    registration-edge pixels elsewhere cannot make the whole image comet
    weighted.
    """
    if used_frames < 1:
        raise ValueError("Used frame count must be at least one")
    if requested_fraction is not None:
        if not math.isfinite(requested_fraction) or not 0.0 <= requested_fraction <= 1.0:
            raise ValueError("Composite minimum star fraction must be between zero and one")
        return float(requested_fraction), "explicit"
    return 0.75, "auto-conservative"


def box_filter_2d(data: np.ndarray, radius_px: float) -> np.ndarray:
    """Apply a deterministic edge-padded square mean filter using numpy only."""
    if data.ndim != 2:
        raise ValueError(f"Box filter requires a 2D array, got {data.shape}")
    if not math.isfinite(radius_px) or radius_px < 0.0:
        raise ValueError("Box-filter radius must be finite and non-negative")
    radius = int(round(radius_px))
    if radius == 0:
        return np.asarray(data, dtype=np.float64).copy()
    source = np.nan_to_num(np.asarray(data, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    padded = np.pad(source, ((radius, radius), (radius, radius)), mode="edge")
    integral = np.cumsum(np.cumsum(padded, axis=0), axis=1)
    integral = np.pad(integral, ((1, 0), (1, 0)), mode="constant")
    size = 2 * radius + 1
    total = (
        integral[size:, size:]
        - integral[:-size, size:]
        - integral[size:, :-size]
        + integral[:-size, :-size]
    )
    return total / float(size * size)


def dilate_binary_mask(mask: np.ndarray, radius_px: float) -> np.ndarray:
    """Dilate a 2D boolean mask with the same square footprint used by the tail detector."""
    if mask.ndim != 2:
        raise ValueError(f"Binary-mask dilation requires a 2D array, got {mask.shape}")
    if radius_px <= 0.0:
        return mask.astype(bool, copy=True)
    return box_filter_2d(mask.astype(np.float32), radius_px) > 0.0


def build_reliability_support(
    target_union_mask: np.ndarray,
    contribution_count: np.ndarray,
    used_frames: int,
    minimum_star_fraction: float,
    dilation_px: float = 0.0,
) -> tuple[np.ndarray, dict[str, object]]:
    """Build a local support mask for star-master pixels with weak coverage."""
    if target_union_mask.ndim != 2 or contribution_count.ndim != 2:
        raise ValueError("Reliability masks and counts must be 2D")
    if target_union_mask.shape != contribution_count.shape:
        raise ValueError("Reliability mask and contribution count shapes differ")
    if used_frames < 1:
        raise ValueError("Used frame count must be at least one")
    if not math.isfinite(minimum_star_fraction) or not 0.0 <= minimum_star_fraction <= 1.0:
        raise ValueError("Minimum star fraction must be between zero and one")
    if not math.isfinite(dilation_px) or dilation_px < 0.0:
        raise ValueError("Reliability dilation radius must be finite and non-negative")
    minimum_count = int(math.ceil(float(used_frames) * minimum_star_fraction))
    low_count = contribution_count < minimum_count
    # A zero-count pixel is invalid regardless of an explicitly requested
    # zero threshold.  It must never be filled or treated as a star background.
    low_count |= contribution_count == 0
    local_low = target_union_mask.astype(bool, copy=False) & low_count
    local_zero = target_union_mask.astype(bool, copy=False) & (contribution_count == 0)
    support = dilate_binary_mask(local_low, dilation_px) if np.any(local_low) else local_low.copy()
    diagnostics = {
        "minimum_star_fraction": float(minimum_star_fraction),
        "minimum_star_fraction_source": "explicit",
        "minimum_star_contribution_count": minimum_count,
        "used_frames": int(used_frames),
        "target_union_area_pixels": int(np.count_nonzero(target_union_mask)),
        "zero_contribution_area_pixels": int(np.count_nonzero(local_zero)),
        "low_contribution_area_pixels": int(np.count_nonzero(local_low)),
        "dilated_low_contribution_area_pixels": int(np.count_nonzero(support)),
        "dilation_radius_px": float(dilation_px),
        "low_contribution_fraction_of_frame": float(np.count_nonzero(support) / contribution_count.size),
    }
    return support.astype(bool, copy=False), diagnostics


def robust_local_background_match(
    star_master: np.ndarray,
    comet_master: np.ndarray,
    annulus: np.ndarray,
    star_valid: np.ndarray,
    comet_valid: np.ndarray,
    minimum_pixels: int = 32,
    clip_sigma: float = 5.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Estimate per-channel sky levels from a shared, sigma-clipped annulus."""
    if annulus.ndim != 2 or star_valid.shape != annulus.shape or comet_valid.shape != annulus.shape:
        raise ValueError("Local background masks must match the image plane")
    if minimum_pixels < 1 or not math.isfinite(clip_sigma) or clip_sigma <= 0.0:
        raise ValueError("Invalid local background matching parameters")
    channels, _height, _width, star_chw = image_shape_chw(star_master)
    comet_channels, _comet_height, _comet_width, comet_chw = image_shape_chw(comet_master)
    if comet_channels != channels or comet_chw.shape != star_chw.shape:
        raise ValueError("Local background image shapes differ")
    star_out = np.full(channels, np.nan, dtype=np.float64)
    comet_out = np.full(channels, np.nan, dtype=np.float64)
    counts = np.zeros(channels, dtype=np.int64)
    for channel in range(channels):
        valid = (
            annulus
            & star_valid
            & comet_valid
            & np.isfinite(star_chw[channel])
            & np.isfinite(comet_chw[channel])
            & (star_chw[channel] != 0.0)
            & (comet_chw[channel] != 0.0)
        )
        if not np.any(valid):
            continue
        star_values = star_chw[channel][valid].astype(np.float64, copy=False)
        comet_values = comet_chw[channel][valid].astype(np.float64, copy=False)
        star_center = float(np.median(star_values))
        comet_center = float(np.median(comet_values))
        star_scale = max(
            1.4826 * float(np.median(np.abs(star_values - star_center))),
            np.finfo(np.float64).eps * max(1.0, abs(star_center)),
        )
        comet_scale = max(
            1.4826 * float(np.median(np.abs(comet_values - comet_center))),
            np.finfo(np.float64).eps * max(1.0, abs(comet_center)),
        )
        clipped = (
            (np.abs(star_values - star_center) <= clip_sigma * star_scale)
            & (np.abs(comet_values - comet_center) <= clip_sigma * comet_scale)
        )
        counts[channel] = int(np.count_nonzero(clipped))
        if counts[channel] >= minimum_pixels:
            star_out[channel] = float(np.median(star_values[clipped]))
            comet_out[channel] = float(np.median(comet_values[clipped]))
    return star_out, comet_out, counts


def annular_profile(
    data: np.ndarray,
    center_x_1based: float,
    center_y_1based: float,
    valid_mask: np.ndarray | None = None,
    annuli: tuple[tuple[float, float], ...] = ((0.0, 5.0), (5.0, 10.0), (10.0, 15.0), (15.0, 20.0), (20.0, 30.0), (30.0, 50.0)),
) -> tuple[list[list[float | None]], list[int]]:
    """Return per-channel robust medians in fixed radial annuli."""
    channels, height, width, chw = image_shape_chw(data)
    if valid_mask is not None and valid_mask.shape != (height, width):
        raise ValueError("Annular profile validity mask shape differs")
    yy, xx = np.indices((height, width), dtype=np.float64)
    distance = np.hypot(xx - float(center_x_1based - 1.0), yy - float(center_y_1based - 1.0))
    values: list[list[float | None]] = [[] for _ in range(channels)]
    counts: list[int] = []
    for lower, upper in annuli:
        region = (distance >= lower) & (distance < upper)
        if valid_mask is not None:
            region &= valid_mask
        counts.append(int(np.count_nonzero(region)))
        for channel in range(channels):
            valid = region & np.isfinite(chw[channel]) & (chw[channel] != 0.0)
            channel_values = chw[channel][valid]
            values[channel].append(
                None if channel_values.size == 0 else float(np.median(channel_values.astype(np.float64, copy=False)))
            )
    return values, counts


def connected_component_from_seed(candidate: np.ndarray, seed: np.ndarray) -> np.ndarray:
    """Return the 8-connected candidate component touching a seed mask."""
    if candidate.ndim != 2 or seed.ndim != 2 or candidate.shape != seed.shape:
        raise ValueError("Candidate and seed masks must be matching 2D arrays")
    allowed = candidate.astype(bool, copy=False)
    component = np.zeros_like(allowed, dtype=bool)
    starts = np.flatnonzero(allowed & seed)
    if starts.size == 0:
        return component
    height, width = allowed.shape
    stack = starts.tolist()
    component.flat[starts] = True
    while stack:
        index = stack.pop()
        row, column = divmod(index, width)
        row_start = max(0, row - 1)
        row_end = min(height, row + 2)
        column_start = max(0, column - 1)
        column_end = min(width, column + 2)
        neighbors = np.flatnonzero(allowed[row_start:row_end, column_start:column_end])
        for local in neighbors.tolist():
            neighbor_row, neighbor_column = divmod(local, column_end - column_start)
            neighbor_row += row_start
            neighbor_column += column_start
            if component[neighbor_row, neighbor_column]:
                continue
            component[neighbor_row, neighbor_column] = True
            stack.append(neighbor_row * width + neighbor_column)
    return component


def region_median(
    data: np.ndarray,
    region: np.ndarray,
    valid_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Return per-channel medians for a diagnostic region, with NaN for empty channels."""
    channels, _height, _width, out = image_shape_chw(data)
    if region.shape != out.shape[-2:]:
        raise ValueError(f"Region mask shape {region.shape} != {out.shape[-2:]}")
    if valid_mask is not None and valid_mask.shape != region.shape:
        raise ValueError("Region validity mask must match the region shape")
    medians = np.full(channels, np.nan, dtype=np.float64)
    for channel in range(channels):
        valid = region & np.isfinite(out[channel]) & (out[channel] != 0.0)
        if valid_mask is not None:
            valid &= valid_mask
        values = out[channel][valid]
        if values.size:
            medians[channel] = float(np.median(values))
    return medians


def build_tail_composite_mask(
    shape: tuple[int, int],
    x_1based: float,
    y_1based: float,
    core_radius_px: float,
    support_margin_px: float,
    star_master: np.ndarray,
    comet_master: np.ndarray,
    star_valid: np.ndarray,
    comet_valid: np.ndarray,
    star_background: np.ndarray,
    comet_background: np.ndarray,
    tail_sigma: float = 3.0,
    tail_smoothing_px: float = 5.0,
    tail_length_px: float = 256.0,
) -> tuple[np.ndarray, dict[str, object]]:
    """Extend the circular composite mask with a low-frequency connected structure.

    The detector is intentionally conservative: it only considers positive
    comet-minus-star residuals outside the core, limits the search radius, and
    keeps components that touch the core.  This is an optional heuristic, not a
    replacement for visual inspection of a long-tail result.
    """
    if not math.isfinite(tail_sigma) or tail_sigma <= 0.0:
        raise ValueError("Tail sigma must be finite and greater than zero")
    if not math.isfinite(tail_smoothing_px) or tail_smoothing_px < 0.0:
        raise ValueError("Tail smoothing radius must be finite and non-negative")
    if not math.isfinite(tail_length_px) or tail_length_px <= 0.0:
        raise ValueError("Tail extension length must be finite and greater than zero")
    if star_valid.shape != shape or comet_valid.shape != shape:
        raise ValueError("Tail validity masks must match the image shape")
    yy, xx = np.indices(shape, dtype=np.float64)
    center_x = float(x_1based - 1.0)
    center_y = float(y_1based - 1.0)
    distance = np.hypot(xx - center_x, yy - center_y)
    core_support = distance <= float(core_radius_px)
    seed = distance <= float(core_radius_px + support_margin_px)
    valid = star_valid & comet_valid
    channels, _height, _width, star_chw = image_shape_chw(star_master)
    comet_channels, _comet_height, _comet_width, comet_chw = image_shape_chw(comet_master)
    if (channels, star_chw.shape[-2], star_chw.shape[-1]) != (comet_channels, shape[0], shape[1]):
        raise ValueError("Tail detector image shapes differ")
    if star_background.shape != (channels,) or comet_background.shape != (channels,):
        raise ValueError("Tail detector background vectors must match the channel count")
    offset = star_background - comet_background
    residual = comet_chw.astype(np.float64) + offset[:, np.newaxis, np.newaxis] - star_chw.astype(np.float64)
    channel_valid = np.isfinite(residual) & valid[np.newaxis, :, :]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        signal = np.nanmean(np.where(channel_valid, residual, np.nan), axis=0)
    signal = np.nan_to_num(signal, nan=0.0, posinf=0.0, neginf=0.0)
    smoothed = box_filter_2d(signal, tail_smoothing_px)
    search = valid & ~seed & (distance <= float(core_radius_px + support_margin_px + tail_length_px))
    search_values = smoothed[search]
    diagnostics: dict[str, object] = {
        "method": "tail",
        "tail_sigma": float(tail_sigma),
        "tail_smoothing_radius_px": float(tail_smoothing_px),
        "tail_length_px": float(tail_length_px),
        "search_pixels": int(np.count_nonzero(search)),
        "candidate_pixels": 0,
        "connected_tail_pixels": 0,
        "threshold": None,
        "background_median": None,
        "robust_scale": None,
        "fallback": None,
    }
    if search_values.size == 0:
        diagnostics["fallback"] = "no-valid-search-region"
        return core_support.astype(np.float32), diagnostics
    center = float(np.median(search_values))
    mad = float(np.median(np.abs(search_values - center)))
    scale = max(1.4826 * mad, np.finfo(np.float64).eps * max(1.0, abs(center)))
    threshold = center + float(tail_sigma) * scale
    candidate = search & (smoothed > threshold)
    candidate_pixels = int(np.count_nonzero(candidate))
    diagnostics["candidate_pixels"] = candidate_pixels
    diagnostics["threshold"] = threshold
    diagnostics["background_median"] = center
    diagnostics["robust_scale"] = scale
    if candidate_pixels == 0:
        diagnostics["fallback"] = "no-threshold-crossing"
        return core_support.astype(np.float32), diagnostics
    if candidate_pixels > max(100_000, int(shape[0] * shape[1] * 0.25)):
        diagnostics["fallback"] = "candidate-too-large"
        return core_support.astype(np.float32), diagnostics
    connected = connected_component_from_seed(candidate | seed, seed)
    tail_only = connected & ~seed
    connected_tail_pixels = int(np.count_nonzero(tail_only))
    diagnostics["connected_tail_pixels"] = connected_tail_pixels
    if connected_tail_pixels == 0:
        diagnostics["fallback"] = "no-core-connected-structure"
        return core_support.astype(np.float32), diagnostics
    # Return binary support.  The seed is used only to establish connectivity;
    # the subtractive compositor has no spatial blend boundary.
    return (core_support | tail_only).astype(np.float32), diagnostics


def background_median(data: np.ndarray, excluded_mask: np.ndarray) -> np.ndarray:
    """Estimate per-channel sky level while excluding the composite region."""
    channels, _height, _width, out = image_shape_chw(data)
    if excluded_mask.shape != out.shape[-2:]:
        raise ValueError(f"Background mask shape {excluded_mask.shape} != {out.shape[-2:]}")
    medians = np.zeros(channels, dtype=np.float64)
    valid_region = ~excluded_mask
    for channel in range(channels):
        plane = out[channel]
        valid = valid_region & np.isfinite(plane) & (plane != 0.0)
        values = plane[valid]
        if values.size:
            medians[channel] = float(np.median(values))
    return medians


def shift_image(
    data: np.ndarray,
    dx: float,
    dy: float,
    source_valid: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    if data.ndim == 2:
        shifted, mask = shift_plane(data, dx, dy, source_valid)
        return shifted, mask
    planes = []
    common_mask = None
    for plane in data:
        shifted, mask = shift_plane(plane, dx, dy, source_valid)
        planes.append(shifted)
        common_mask = mask if common_mask is None else (common_mask & mask)
    return np.stack(planes, axis=0), common_mask


def directional_filter_geometry(shift_vectors: list[tuple[float, float]]) -> dict[str, object]:
    """Derive comet, star-trail, and perpendicular filter directions.

    ``shift_image(frame, dx, dy)`` moves a source pixel to ``(x + dx, y +
    dy)``.  In the comet-reference stack the applied moving-target shift is
    therefore the star-trail vector, while the comet motion in the original
    registered frames is its negative.  Keeping this derivation here makes
    the sign convention explicit and testable.
    """
    diagnostics: dict[str, object] = {
        "shift_sample_count": int(len(shift_vectors)),
        "comet_motion_dx_px": None,
        "comet_motion_dy_px": None,
        "comet_motion_angle_deg": None,
        "star_trail_angle_deg": None,
        "directional_filter_angle_deg": None,
        "directional_filter_angle_definition": "star_trail_angle_deg + 90 deg",
        "reason": None,
    }
    if len(shift_vectors) < 2:
        diagnostics["reason"] = "fewer-than-two-shift-samples"
        return diagnostics
    first_dx, first_dy = (float(value) for value in shift_vectors[0])
    last_dx, last_dy = (float(value) for value in shift_vectors[-1])
    star_dx = last_dx - first_dx
    star_dy = last_dy - first_dy
    comet_dx = -star_dx
    comet_dy = -star_dy
    motion = math.hypot(comet_dx, comet_dy)
    diagnostics["comet_motion_dx_px"] = comet_dx
    diagnostics["comet_motion_dy_px"] = comet_dy
    diagnostics["comet_motion_px"] = motion
    if motion <= 1.0e-9 or not math.isfinite(motion):
        diagnostics["reason"] = "negligible-comet-motion"
        return diagnostics
    comet_angle = math.degrees(math.atan2(comet_dy, comet_dx))
    star_angle = math.degrees(math.atan2(star_dy, star_dx))
    directional_angle = star_angle + 90.0
    while directional_angle > 180.0:
        directional_angle -= 360.0
    while directional_angle <= -180.0:
        directional_angle += 360.0
    diagnostics["comet_motion_angle_deg"] = comet_angle
    diagnostics["star_trail_dx_px"] = star_dx
    diagnostics["star_trail_dy_px"] = star_dy
    diagnostics["star_trail_angle_deg"] = star_angle
    diagnostics["directional_filter_angle_deg"] = directional_angle
    return diagnostics


def _sample_image_at_offset(
    data: np.ndarray,
    offset_x: float,
    offset_y: float,
    valid_mask: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Sample ``data`` at pixel coordinates plus an offset using existing bilinear shifting."""
    # shift_image(data, -offset) evaluates the source at destination+offset.
    return shift_image(data, -float(offset_x), -float(offset_y), valid_mask)


def apply_directional_comet_filter(
    data: np.ndarray,
    angle_deg: float,
    size_px: int = 2,
    valid_mask: np.ndarray | None = None,
    minimum_valid_samples: int = 3,
) -> tuple[np.ndarray, dict[str, object]]:
    """Suppress only bright perpendicular line samples with a numpy median.

    For each pixel, samples at ``-size..+size`` along ``angle_deg`` are
    collected with bilinear interpolation.  A bright current pixel is lowered
    to the directional median; dark pixels are never raised.  Invalid or
    non-finite samples are excluded and pixels with too few valid samples are
    left untouched.
    """
    if not math.isfinite(angle_deg):
        raise ValueError("Directional filter angle must be finite")
    if int(size_px) != size_px or size_px < 1:
        raise ValueError("Directional filter size must be a positive integer")
    size = int(size_px)
    if int(minimum_valid_samples) != minimum_valid_samples or minimum_valid_samples < 1:
        raise ValueError("Minimum directional sample count must be a positive integer")
    channels, height, width, chw = image_shape_chw(np.asarray(data))
    if valid_mask is None:
        valid = np.ones((height, width), dtype=bool)
    else:
        if valid_mask.shape != (height, width):
            raise ValueError("Directional filter validity mask shape differs from image")
        valid = valid_mask.astype(bool, copy=False)
    minimum_samples = min(int(minimum_valid_samples), 2 * size + 1)
    radians = math.radians(float(angle_deg))
    unit_x = math.cos(radians)
    unit_y = math.sin(radians)
    sample_values: list[np.ndarray] = []
    sample_validity: list[np.ndarray] = []
    for offset in range(-size, size + 1):
        if offset == 0:
            sampled = chw.astype(np.float64, copy=False)
            sampled_valid = valid.copy()
        else:
            sampled, sampled_valid = _sample_image_at_offset(
                chw,
                float(offset) * unit_x,
                float(offset) * unit_y,
                valid,
            )
            sampled = sampled.astype(np.float64, copy=False)
        sampled_valid = sampled_valid & np.all(np.isfinite(sampled), axis=0)
        sample_values.append(sampled)
        sample_validity.append(sampled_valid)
    values = np.stack(sample_values, axis=0)
    validity = np.stack(sample_validity, axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        median = np.nanmedian(np.where(validity[:, np.newaxis, :, :], values, np.nan), axis=0)
    valid_sample_count = np.count_nonzero(validity, axis=0)
    output = chw.astype(np.float64, copy=True)
    current_valid = valid & np.all(np.isfinite(chw), axis=0)
    replace = current_valid & (valid_sample_count >= minimum_samples) & np.isfinite(median[0])
    replace &= np.isfinite(median).all(axis=0)
    replace_channels = replace[np.newaxis, :, :] & (chw > median)
    output[replace_channels] = median[replace_channels]
    reduction = np.maximum(chw - output, 0.0)
    diagnostics = {
        "angle_deg": float(angle_deg),
        "size_px": size,
        "sample_offsets_px": list(range(-size, size + 1)),
        "minimum_valid_samples": minimum_samples,
        "valid_sample_count": {
            "min": int(np.min(valid_sample_count)) if valid_sample_count.size else 0,
            "median": float(np.median(valid_sample_count)) if valid_sample_count.size else 0.0,
            "max": int(np.max(valid_sample_count)) if valid_sample_count.size else 0,
        },
        "suppressed_pixels": int(np.count_nonzero(replace_channels)),
        "suppressed_value_sum": float(np.sum(reduction[replace_channels])) if np.any(replace_channels) else 0.0,
        "maximum_suppression": float(np.max(reduction[replace_channels])) if np.any(replace_channels) else 0.0,
        "valid_pixel_count": int(np.count_nonzero(current_valid)),
    }
    return (output[0] if data.ndim == 2 else output), diagnostics


def protect_directional_core(
    sigma_master: np.ndarray,
    directional_master: np.ndarray,
    center_x_1based: float,
    center_y_1based: float,
    radius_px: float,
    valid_mask: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, object]]:
    """Restore the sigma master inside the measured comet core radius."""
    if sigma_master.shape != directional_master.shape:
        raise ValueError("Core-protection masters have different shapes")
    if not math.isfinite(radius_px) or radius_px <= 0.0:
        raise ValueError("Core-protection radius must be finite and positive")
    channels, height, width, sigma_chw = image_shape_chw(sigma_master)
    _directional_channels, directional_height, directional_width, directional_chw = image_shape_chw(directional_master)
    if (height, width) != (directional_height, directional_width):
        raise ValueError("Core-protection image planes have different shapes")
    if valid_mask is not None and valid_mask.shape != (height, width):
        raise ValueError("Core-protection validity mask shape differs from image")
    yy, xx = np.indices((height, width), dtype=np.float64)
    core = np.hypot(xx - float(center_x_1based - 1.0), yy - float(center_y_1based - 1.0)) <= float(radius_px)
    if valid_mask is not None:
        core &= valid_mask
    output = directional_chw.astype(np.float64, copy=True)
    output[:, core] = sigma_chw[:, core].astype(np.float64, copy=False)
    return (output[0] if directional_master.ndim == 2 else output), {
        "enabled": True,
        "radius_px": float(radius_px),
        "protected_pixels": int(np.count_nonzero(core)),
        "channels": channels,
    }


def estimate_tail_axis(
    data: np.ndarray,
    center_x_1based: float,
    center_y_1based: float,
    background: np.ndarray,
    valid_mask: np.ndarray,
    core_radius_px: float,
    max_radius_px: float = 300.0,
    smoothing_px: float = 3.0,
) -> dict[str, object]:
    """Estimate a positive tail axis from connected low-frequency comet signal."""
    channels, height, width, chw = image_shape_chw(data)
    if background.shape != (channels,) or valid_mask.shape != (height, width):
        raise ValueError("Tail-axis inputs have incompatible shapes")
    yy, xx = np.indices((height, width), dtype=np.float64)
    dx = xx - float(center_x_1based - 1.0)
    dy = yy - float(center_y_1based - 1.0)
    distance = np.hypot(dx, dy)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        signal = np.nanmean(
            np.where(
                np.isfinite(chw) & valid_mask[np.newaxis, :, :],
                chw.astype(np.float64) - background[:, np.newaxis, np.newaxis],
                np.nan,
            ),
            axis=0,
        )
    signal = np.nan_to_num(signal, nan=0.0, posinf=0.0, neginf=0.0)
    smoothed = box_filter_2d(signal, smoothing_px)
    search = valid_mask & (distance > float(core_radius_px)) & (distance <= float(max_radius_px))
    search_values = smoothed[search]
    diagnostics: dict[str, object] = {
        "available": False,
        "source": "sigma_comet_low_frequency_structure",
        "max_radius_px": float(max_radius_px),
        "candidate_pixels": 0,
        "axis_dx": None,
        "axis_dy": None,
        "angle_deg": None,
        "confidence": None,
        "reason": None,
    }
    if search_values.size < 16:
        diagnostics["reason"] = "insufficient-search-pixels"
        return diagnostics
    center = float(np.median(search_values))
    mad = float(np.median(np.abs(search_values - center)))
    scale = max(1.4826 * mad, np.finfo(np.float64).eps * max(1.0, abs(center)))
    threshold = center + 2.5 * scale
    candidate = search & (smoothed > threshold)
    candidate_pixels = int(np.count_nonzero(candidate))
    diagnostics["candidate_pixels"] = candidate_pixels
    diagnostics["threshold"] = threshold
    diagnostics["robust_scale"] = scale
    if candidate_pixels < 16:
        diagnostics["reason"] = "insufficient-positive-structure"
        return diagnostics
    weights = np.clip(smoothed[candidate] - threshold, 0.0, None).astype(np.float64)
    rows, columns = np.nonzero(candidate)
    coordinates = np.stack(
        [columns.astype(np.float64) - float(center_x_1based - 1.0), rows.astype(np.float64) - float(center_y_1based - 1.0)],
        axis=1,
    )
    total_weight = float(np.sum(weights))
    if total_weight <= 0.0 or not math.isfinite(total_weight):
        diagnostics["reason"] = "non-positive-structure-weight"
        return diagnostics
    centroid = np.sum(coordinates * weights[:, np.newaxis], axis=0) / total_weight
    centered = coordinates - centroid[np.newaxis, :]
    covariance = (centered * weights[:, np.newaxis]).T @ centered / total_weight
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    axis = eigenvectors[:, int(np.argmax(eigenvalues))].astype(np.float64)
    axis_norm = float(np.hypot(axis[0], axis[1]))
    if axis_norm <= 1.0e-12:
        diagnostics["reason"] = "degenerate-structure-axis"
        return diagnostics
    axis /= axis_norm
    if float(np.dot(axis, centroid)) < 0.0:
        axis *= -1.0
    confidence = float(eigenvalues[-1] / max(float(np.sum(eigenvalues)), np.finfo(np.float64).eps))
    diagnostics.update(
        {
            "available": True,
            "axis_dx": float(axis[0]),
            "axis_dy": float(axis[1]),
            "angle_deg": float(math.degrees(math.atan2(axis[1], axis[0]))),
            "confidence": confidence,
            "weighted_pixels": int(np.count_nonzero(weights > 0.0)),
            "weighted_centroid_dx": float(centroid[0]),
            "weighted_centroid_dy": float(centroid[1]),
        }
    )
    return diagnostics


def compare_tail_flux_profiles(
    sigma_master: np.ndarray,
    directional_master: np.ndarray,
    center_x_1based: float,
    center_y_1based: float,
    tail_axis: dict[str, object],
    sigma_background: np.ndarray,
    directional_background: np.ndarray,
    valid_mask: np.ndarray,
    bins: tuple[tuple[float, float], ...] = ((0.0, 10.0), (10.0, 25.0), (25.0, 50.0), (50.0, 100.0), (100.0, 150.0), (150.0, 250.0)),
) -> dict[str, object]:
    """Compare background-subtracted median signal along an estimated tail axis."""
    channels, height, width, sigma_chw = image_shape_chw(sigma_master)
    directional_channels, directional_height, directional_width, directional_chw = image_shape_chw(directional_master)
    if (channels, height, width) != (directional_channels, directional_height, directional_width):
        raise ValueError("Tail profile masters have different shapes")
    if sigma_background.shape != (channels,) or directional_background.shape != (channels,):
        raise ValueError("Tail profile background vectors have incompatible shapes")
    result: dict[str, object] = {
        "axis_available": bool(tail_axis.get("available")),
        "axis_angle_deg": tail_axis.get("angle_deg"),
        "bins_px": [[float(lower), float(upper)] for lower, upper in bins],
        "profiles": [],
        "tail_flux_ratio_directional_vs_sigma": None,
    }
    if not tail_axis.get("available"):
        result["reason"] = tail_axis.get("reason") or "tail-axis-unavailable"
        return result
    axis_x = float(tail_axis["axis_dx"])
    axis_y = float(tail_axis["axis_dy"])
    yy, xx = np.indices((height, width), dtype=np.float64)
    dx = xx - float(center_x_1based - 1.0)
    dy = yy - float(center_y_1based - 1.0)
    along = dx * axis_x + dy * axis_y
    across = np.abs(-dx * axis_y + dy * axis_x)
    sigma_signal = np.nanmean(
        np.where(
            np.isfinite(sigma_chw),
            sigma_chw.astype(np.float64) - sigma_background[:, np.newaxis, np.newaxis],
            np.nan,
        ),
        axis=0,
    )
    directional_signal = np.nanmean(
        np.where(
            np.isfinite(directional_chw),
            directional_chw.astype(np.float64) - directional_background[:, np.newaxis, np.newaxis],
            np.nan,
        ),
        axis=0,
    )
    profile_rows: list[dict[str, object]] = []
    far_sigma_values: list[float] = []
    far_directional_values: list[float] = []
    for lower, upper in bins:
        half_width = max(5.0, min(50.0, float(upper) * 0.20))
        region = valid_mask & (along >= lower) & (along < upper) & (across <= half_width)
        sigma_values = sigma_signal[region]
        directional_values = directional_signal[region]
        sigma_values = sigma_values[np.isfinite(sigma_values)]
        directional_values = directional_values[np.isfinite(directional_values)]
        sigma_median = None if sigma_values.size == 0 else float(np.median(sigma_values))
        directional_median = None if directional_values.size == 0 else float(np.median(directional_values))
        ratio = None
        if sigma_median is not None and directional_median is not None and abs(sigma_median) > 1.0e-9:
            ratio = float(directional_median / sigma_median)
        if lower >= 50.0:
            if sigma_median is not None:
                far_sigma_values.append(sigma_median)
            if directional_median is not None:
                far_directional_values.append(directional_median)
        profile_rows.append(
            {
                "lower_px": float(lower),
                "upper_px": float(upper),
                "transverse_half_width_px": half_width,
                "pixel_count": int(min(sigma_values.size, directional_values.size)),
                "sigma_median_signal": sigma_median,
                "directional_median_signal": directional_median,
                "directional_vs_sigma_ratio": ratio,
            }
        )
    if far_sigma_values and far_directional_values:
        sigma_far = float(np.median(far_sigma_values))
        directional_far = float(np.median(far_directional_values))
        if abs(sigma_far) > 1.0e-9:
            result["tail_flux_ratio_directional_vs_sigma"] = float(directional_far / sigma_far)
    result["profiles"] = profile_rows
    return result


def core_flux_comparison(
    sigma_master: np.ndarray,
    directional_master: np.ndarray,
    center_x_1based: float,
    center_y_1based: float,
    core_radius_px: float,
    sigma_background: np.ndarray,
    directional_background: np.ndarray,
    valid_mask: np.ndarray,
) -> dict[str, object]:
    """Compare positive background-subtracted core flux per channel."""
    channels, height, width, sigma_chw = image_shape_chw(sigma_master)
    directional_channels, directional_height, directional_width, directional_chw = image_shape_chw(directional_master)
    if (channels, height, width) != (directional_channels, directional_height, directional_width):
        raise ValueError("Core comparison masters have different shapes")
    yy, xx = np.indices((height, width), dtype=np.float64)
    core = valid_mask & (
        np.hypot(xx - float(center_x_1based - 1.0), yy - float(center_y_1based - 1.0))
        <= float(core_radius_px)
    )
    sigma_flux: list[float] = []
    directional_flux: list[float] = []
    for channel in range(channels):
        sigma_signal = sigma_chw[channel].astype(np.float64) - float(sigma_background[channel])
        directional_signal = directional_chw[channel].astype(np.float64) - float(directional_background[channel])
        sigma_flux.append(float(np.sum(np.clip(sigma_signal[core], 0.0, None))))
        directional_flux.append(float(np.sum(np.clip(directional_signal[core], 0.0, None))))
    sigma_total = float(np.sum(sigma_flux))
    directional_total = float(np.sum(directional_flux))
    return {
        "core_radius_px": float(core_radius_px),
        "sigma_flux": sigma_flux,
        "directional_flux": directional_flux,
        "sigma_total_positive_background_subtracted_flux": sigma_total,
        "directional_total_positive_background_subtracted_flux": directional_total,
        "core_flux_ratio_directional_vs_sigma": (
            None if abs(sigma_total) <= 1.0e-9 else float(directional_total / sigma_total)
        ),
        "pixel_count": int(np.count_nonzero(core)),
    }


def oriented_trail_metric(
    data: np.ndarray,
    trail_angle_deg: float,
    valid_mask: np.ndarray,
    sample_distance_px: float = 2.0,
) -> dict[str, object]:
    """Measure positive along-trail line excess over perpendicular neighbors."""
    if not math.isfinite(trail_angle_deg) or sample_distance_px <= 0.0:
        return {"value": None, "pixel_count": 0, "reason": "invalid-angle-or-distance"}
    channels, height, width, chw = image_shape_chw(data)
    if valid_mask.shape != (height, width):
        raise ValueError("Trail metric validity mask shape differs from image")
    radians = math.radians(float(trail_angle_deg))
    ux, uy = math.cos(radians), math.sin(radians)
    cross_x, cross_y = -uy, ux
    along_plus, valid_plus = _sample_image_at_offset(chw, sample_distance_px * ux, sample_distance_px * uy, valid_mask)
    along_minus, valid_minus = _sample_image_at_offset(chw, -sample_distance_px * ux, -sample_distance_px * uy, valid_mask)
    cross_plus, cross_valid_plus = _sample_image_at_offset(chw, sample_distance_px * cross_x, sample_distance_px * cross_y, valid_mask)
    cross_minus, cross_valid_minus = _sample_image_at_offset(chw, -sample_distance_px * cross_x, -sample_distance_px * cross_y, valid_mask)
    current_valid = valid_mask & valid_plus & valid_minus & cross_valid_plus & cross_valid_minus
    along = np.nanmean(np.stack([along_plus, along_minus], axis=0), axis=0)
    cross = np.nanmean(np.stack([cross_plus, cross_minus], axis=0), axis=0)
    signal = np.nanmean(along - cross, axis=0)
    values = signal[current_valid & np.isfinite(signal)]
    positive = np.clip(values, 0.0, None)
    return {
        "value": None if positive.size == 0 else float(np.percentile(positive, 90.0)),
        "median_positive_excess": None if positive.size == 0 else float(np.median(positive)),
        "pixel_count": int(positive.size),
        "sample_distance_px": float(sample_distance_px),
        "angle_deg": float(trail_angle_deg),
    }


def inverse_moving_target_shift(
    reference_image: np.ndarray,
    forward_dx: float,
    forward_dy: float,
    source_valid: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Move a reference-position image back to the original star frame.

    The moving-target path uses ``shift_image(frame, reference_x-frame_x,
    reference_y-frame_y)``.  The inverse therefore uses the exact negated
    displacement; keeping this relation in one helper prevents a sign guess
    in the subtractive path.
    """
    return shift_image(reference_image, -float(forward_dx), -float(forward_dy), source_valid)


def subtract_shifted_comet_model(
    star_registered_frame: np.ndarray,
    shifted_comet_model: np.ndarray,
    frame_valid: np.ndarray,
    model_valid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Subtract only valid comet-model pixels without clipping negative values."""
    if star_registered_frame.shape != shifted_comet_model.shape:
        raise ValueError("Star frame and shifted comet model shapes differ")
    if frame_valid.shape != model_valid.shape or frame_valid.shape != star_registered_frame.shape[-2:]:
        raise ValueError("Frame and comet-model validity masks must match the image plane")
    subtract_valid = frame_valid & model_valid
    result = star_registered_frame.astype(np.float64, copy=True)
    if result.ndim == 3:
        result[:, subtract_valid] -= shifted_comet_model[:, subtract_valid].astype(np.float64, copy=False)
    else:
        result[subtract_valid] -= shifted_comet_model[subtract_valid].astype(np.float64, copy=False)
    # Invalid comet-model pixels deliberately retain the original frame.
    # The returned validity is the frame validity, not the model validity.
    return result, frame_valid.copy()


def add_reference_comet_model(
    cometless_stack: np.ndarray,
    reference_comet_model: np.ndarray,
    comet_model_valid: np.ndarray,
    stack_valid: np.ndarray | None = None,
) -> np.ndarray:
    """Reconstruct a scene by adding the reference comet model once."""
    if cometless_stack.shape != reference_comet_model.shape:
        raise ValueError("Cometless stack and reference comet model shapes differ")
    if comet_model_valid.shape != cometless_stack.shape[-2:]:
        raise ValueError("Reference comet-model validity mask must match the image plane")
    result = cometless_stack.astype(np.float64, copy=True)
    add_valid = comet_model_valid if stack_valid is None else (comet_model_valid & stack_valid)
    if result.ndim == 3:
        result[:, add_valid] += reference_comet_model[:, add_valid].astype(np.float64, copy=False)
    else:
        result[add_valid] += reference_comet_model[add_valid].astype(np.float64, copy=False)
    return result


def shift_plane(
    data: np.ndarray,
    dx: float,
    dy: float,
    source_valid: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    height, width = data.shape
    if source_valid is not None and source_valid.shape != (height, width):
        raise ValueError(f"Validity mask shape changed: {source_valid.shape} != {(height, width)}")
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
        valid_indices = np.flatnonzero(valid)
        valid_y = valid_indices // width
        valid_x = valid_indices % width
        kernel_valid = (
            source_valid[y0[valid_y, valid_x], x0[valid_y, valid_x]]
            & source_valid[y0[valid_y, valid_x], x1[valid_y, valid_x]]
            & source_valid[y1[valid_y, valid_x], x0[valid_y, valid_x]]
            & source_valid[y1[valid_y, valid_x], x1[valid_y, valid_x]]
        )
        valid[valid_y, valid_x] = kernel_valid

    out = np.zeros((height, width), dtype=np.float32)
    if not np.any(valid):
        return out, valid
    wx = src_x[valid] - x0[valid]
    wy = src_y[valid] - y0[valid]
    v00 = data[y0[valid], x0[valid]]
    v10 = data[y0[valid], x1[valid]]
    v01 = data[y1[valid], x0[valid]]
    v11 = data[y1[valid], x1[valid]]
    out[valid] = (
        (1.0 - wx) * (1.0 - wy) * v00
        + wx * (1.0 - wy) * v10
        + (1.0 - wx) * wy * v01
        + wx * wy * v11
    )
    return out, valid


def add_to_average(
    sum_image: np.ndarray | None,
    count_image: np.ndarray | None,
    image: np.ndarray,
    mask2d: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    if sum_image is None:
        sum_image = np.zeros_like(image, dtype=np.float64)
        count_shape = image.shape[-2:]
        count_image = np.zeros(count_shape, dtype=np.uint32)
    if count_image is None:
        raise ValueError("count_image must be initialized with sum_image")
    if image.ndim == 3:
        sum_image += image * mask2d[np.newaxis, :, :]
    else:
        sum_image += image * mask2d
    count_image += mask2d.astype(np.uint16)
    return sum_image, count_image


def finalize_average(sum_image: np.ndarray | None, count_image: np.ndarray | None) -> np.ndarray:
    if sum_image is None or count_image is None:
        raise RuntimeError("No frames were available for stacking")
    safe_count = np.maximum(count_image, 1).astype(np.float64)
    if sum_image.ndim == 3:
        stack = sum_image / safe_count[np.newaxis, :, :]
        stack[:, count_image == 0] = 0
    else:
        stack = sum_image / safe_count
        stack[count_image == 0] = 0
    return stack


def sigma_clipped_median(
    values: np.ndarray,
    sigma_low: float = 3.0,
    sigma_high: float = 3.0,
    axis: int = 0,
) -> np.ndarray:
    """Return a robust median after two MAD-based asymmetric rejection passes."""
    if not math.isfinite(sigma_low) or sigma_low <= 0.0:
        raise ValueError("sigma_low must be finite and greater than zero")
    if not math.isfinite(sigma_high) or sigma_high <= 0.0:
        raise ValueError("sigma_high must be finite and greater than zero")
    source = np.asarray(values, dtype=np.float64)
    if source.ndim == 0:
        raise ValueError("Sigma clipping requires at least one sample axis")
    if axis < 0:
        axis += source.ndim
    if axis < 0 or axis >= source.ndim:
        raise ValueError(f"Invalid sigma-clipping axis {axis} for shape {source.shape}")
    clipped = np.where(np.isfinite(source), source, np.nan)
    for _iteration in range(2):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)
            center = np.nanmedian(clipped, axis=axis, keepdims=True)
            mad = np.nanmedian(np.abs(clipped - center), axis=axis, keepdims=True)
        scale = 1.4826 * mad
        scale_floor = np.finfo(np.float64).eps * np.maximum(1.0, np.abs(center))
        scale = np.maximum(scale, scale_floor)
        accepted = np.isfinite(clipped) & (clipped >= center - float(sigma_low) * scale) & (
            clipped <= center + float(sigma_high) * scale
        )
        clipped = np.where(accepted, clipped, np.nan)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        result = np.nanmedian(clipped, axis=axis)
        fallback = np.nanmedian(source, axis=axis)
    result = np.where(np.isfinite(result), result, np.nan_to_num(fallback, nan=0.0))
    return result


class MedianAccumulator:
    """Disk-backed per-pixel median accumulator for large Seestar sequences."""

    def __init__(
        self,
        path: Path,
        capacity: int,
        image_shape: tuple[int, ...],
        exclude_zero_samples: bool = True,
    ):
        self.path = path
        self.capacity = capacity
        self.image_shape = image_shape
        self.count = 0
        self.exclude_zero_samples = exclude_zero_samples
        self.data = np.lib.format.open_memmap(
            path,
            mode="w+",
            dtype=np.float32,
            shape=(capacity, *image_shape),
        )

    def add(self, image: np.ndarray, mask2d: np.ndarray) -> None:
        if self.count >= self.capacity:
            raise RuntimeError("Median accumulator capacity exceeded")
        if image.shape != self.image_shape:
            raise ValueError(f"Median frame shape changed: {image.shape} != {self.image_shape}")
        valid = mask2d[np.newaxis, :, :] if image.ndim == 3 else mask2d
        if self.exclude_zero_samples:
            # Exact-zero samples are normally registration/shift padding for
            # order-statistic stacks, so treat them as missing by default.
            valid = valid & (image != 0.0)
        self.data[self.count] = np.where(valid, image, np.nan).astype(np.float32, copy=False)
        self.count += 1

    def finalize(self, row_chunk: int = 64) -> np.ndarray:
        if self.count == 0:
            raise RuntimeError("No frames were available for median stacking")
        self.data.flush()
        result = np.zeros(self.image_shape, dtype=np.float64)
        height = self.image_shape[-2]
        for row_start in range(0, height, row_chunk):
            row_end = min(row_start + row_chunk, height)
            source_slice = (slice(0, self.count),) + (slice(None),) * (len(self.image_shape) - 2) + (
                slice(row_start, row_end),
                slice(None),
            )
            output_slice = (slice(None),) * (len(self.image_shape) - 2) + (
                slice(row_start, row_end),
                slice(None),
            )
            block = np.asarray(self.data[source_slice])
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=RuntimeWarning)
                median = np.nanmedian(block, axis=0)
            result[output_slice] = np.nan_to_num(median, nan=0.0)
        return result

    def finalize_sigma(
        self,
        sigma_low: float = 3.0,
        sigma_high: float = 3.0,
        row_chunk: int = 16,
    ) -> np.ndarray:
        """Finalize with one robust MAD-based rejection pass per pixel."""
        if self.count == 0:
            raise RuntimeError("No frames were available for sigma-clipped stacking")
        if row_chunk < 1:
            raise ValueError("row_chunk must be at least 1")
        self.data.flush()
        result = np.zeros(self.image_shape, dtype=np.float64)
        height = self.image_shape[-2]
        for row_start in range(0, height, row_chunk):
            row_end = min(row_start + row_chunk, height)
            source_slice = (slice(0, self.count),) + (slice(None),) * (len(self.image_shape) - 2) + (
                slice(row_start, row_end),
                slice(None),
            )
            output_slice = (slice(None),) * (len(self.image_shape) - 2) + (
                slice(row_start, row_end),
                slice(None),
            )
            block = np.asarray(self.data[source_slice])
            result[output_slice] = sigma_clipped_median(
                block,
                sigma_low=sigma_low,
                sigma_high=sigma_high,
                axis=0,
            )
        return result

    def finalize_rankfit(self, fraction_percent: int, degree: int = 5, row_chunk: int = 16) -> np.ndarray:
        """Fit brightness versus rank in the central sample fraction."""
        if self.count == 0:
            raise RuntimeError("No frames were available for rank-fit stacking")
        if not 1 <= fraction_percent <= 100:
            raise ValueError("rank-fit fraction must be an integer from 1 to 100")
        self.data.flush()
        result = np.zeros(self.image_shape, dtype=np.float64)
        height = self.image_shape[-2]
        for row_start in range(0, height, row_chunk):
            row_end = min(row_start + row_chunk, height)
            source_slice = (slice(0, self.count),) + (slice(None),) * (len(self.image_shape) - 2) + (
                slice(row_start, row_end),
                slice(None),
            )
            output_slice = (slice(None),) * (len(self.image_shape) - 2) + (
                slice(row_start, row_end),
                slice(None),
            )
            block = np.asarray(self.data[source_slice])
            ordered = np.sort(block, axis=0).reshape(self.count, -1)
            valid_counts = np.sum(np.isfinite(ordered), axis=0)
            fitted = np.zeros(ordered.shape[1], dtype=np.float64)
            for sample_count in np.unique(valid_counts):
                sample_count = int(sample_count)
                if sample_count == 0:
                    continue
                pixels = valid_counts == sample_count
                selected_count = max(1, math.ceil(sample_count * fraction_percent / 100.0))
                if selected_count < degree + 2:
                    middle = sample_count // 2
                    if sample_count % 2:
                        fitted[pixels] = ordered[middle, pixels]
                    else:
                        fitted[pixels] = (ordered[middle - 1, pixels] + ordered[middle, pixels]) / 2.0
                    continue
                selected_start = (sample_count - selected_count) // 2
                selected = ordered[selected_start : selected_start + selected_count, pixels]
                full_rank = np.arange(sample_count, dtype=np.float64) - (sample_count - 1) / 2.0
                full_rank /= max(np.max(np.abs(full_rank)), 1.0)
                rank = full_rank[selected_start : selected_start + selected_count]
                design = np.polynomial.polynomial.polyvander(rank, degree)
                center_weights = np.linalg.pinv(design)[0]
                fitted[pixels] = center_weights @ selected
            result[output_slice] = fitted.reshape(result[output_slice].shape)
        return result

    def close(self, remove: bool) -> bool:
        self.data.flush()
        del self.data
        if remove and self.path.exists():
            self.path.unlink()
            return True
        return False


def export_preview_png(
    path: Path,
    data: np.ndarray,
    flip_vertical: bool = False,
    low_percentile: float = 5.0,
    high_percentile: float = 99.95,
    warning_mask: np.ndarray | None = None,
    warning_color: tuple[int, int, int] = (255, 0, 0),
    value_limits: tuple[float, float] | None = None,
) -> None:
    # Siril's FITS-to-PNG export keeps the visual orientation expected for
    # Seestar subframes, so the default preview is not flipped. Use
    # --preview-flip-vertical only when comparing against a top-left display
    # coordinate conversion.
    if data.ndim == 2:
        planes = [np.flipud(data) if flip_vertical else data]
    else:
        planes = [
            np.flipud(data[i]) if flip_vertical else data[i]
            for i in range(min(3, data.shape[0]))
        ]
    stretched = []
    for plane in planes:
        # Registration and sub-pixel shifts create exact-zero borders. They
        # are display padding, not samples of the sky background, so exclude
        # them only from the preview percentile calculation.
        finite = plane[np.isfinite(plane) & (plane != 0.0)]
        if finite.size == 0:
            scaled = np.zeros_like(plane, dtype=np.uint8)
        else:
            if value_limits is None:
                lo, hi = np.percentile(finite, [low_percentile, high_percentile])
            else:
                lo, hi = value_limits
                if not math.isfinite(lo) or not math.isfinite(hi):
                    raise ValueError("Preview value limits must be finite")
            if hi <= lo:
                hi = lo + 1.0
            scaled = np.clip((plane - lo) / (hi - lo), 0.0, 1.0)
            scaled = (scaled * 255.0 + 0.5).astype(np.uint8)
        stretched.append(scaled)
    if len(stretched) == 1 and warning_mask is None:
        image = Image.fromarray(stretched[0], mode="L")
    else:
        while len(stretched) < 3:
            stretched.append(stretched[-1])
        rgb = np.stack(stretched[:3], axis=2)
        if warning_mask is not None:
            if warning_mask.shape != data.shape[-2:]:
                raise ValueError(
                    f"Warning mask shape {warning_mask.shape} does not match image shape {data.shape[-2:]}"
                )
            display_mask = np.flipud(warning_mask) if flip_vertical else warning_mask
            rgb[display_mask] = np.asarray(warning_color, dtype=np.uint8)
        image = Image.fromarray(rgb, mode="RGB")
    image.save(path)


def preview_stretch_limits(
    data_items: list[np.ndarray],
    low_percentile: float = 5.0,
    high_percentile: float = 99.95,
    max_samples_per_plane: int = 100_000,
) -> tuple[float, float]:
    """Find one display stretch shared by several images without touching FITS data."""
    if not 0.0 <= low_percentile <= 100.0 or not 0.0 <= high_percentile <= 100.0:
        raise ValueError("Preview percentiles must be between 0 and 100")
    if low_percentile > high_percentile:
        raise ValueError("Preview low percentile must not exceed the high percentile")
    if max_samples_per_plane < 1:
        raise ValueError("max_samples_per_plane must be at least 1")
    samples: list[np.ndarray] = []
    for data in data_items:
        _channels, _height, _width, planes = image_shape_chw(data)
        for plane in planes:
            finite = plane[np.isfinite(plane) & (plane != 0.0)].reshape(-1)
            if finite.size == 0:
                continue
            if finite.size > max_samples_per_plane:
                stride = int(math.ceil(finite.size / max_samples_per_plane))
                finite = finite[::stride]
            samples.append(finite.astype(np.float64, copy=False))
    if not samples:
        return 0.0, 1.0
    values = np.concatenate(samples)
    low, high = np.percentile(values, [low_percentile, high_percentile])
    low = float(low)
    high = float(high)
    if not math.isfinite(low) or not math.isfinite(high):
        return 0.0, 1.0
    if high <= low:
        high = low + 1.0
    return low, high


def export_mask_png(path: Path, mask: np.ndarray, flip_vertical: bool = False) -> None:
    """Write a direct grayscale visualization of the comet-master weight."""
    if mask.ndim != 2:
        raise ValueError(f"Composite mask preview requires a 2D array, got {mask.shape}")
    display = np.flipud(mask) if flip_vertical else mask
    pixels = np.clip(np.nan_to_num(display, nan=0.0), 0.0, 1.0)
    image = Image.fromarray(np.rint(pixels * 255.0).astype(np.uint8), mode="L")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)


def export_count_png(path: Path, counts: np.ndarray, flip_vertical: bool = False) -> None:
    """Write a contribution-count map normalized from zero to its observed maximum."""
    if counts.ndim != 2:
        raise ValueError(f"Contribution-count preview requires a 2D array, got {counts.shape}")
    display = np.flipud(counts) if flip_vertical else counts
    finite = np.isfinite(display)
    maximum = float(np.max(display[finite])) if np.any(finite) else 0.0
    if maximum <= 0.0:
        pixels = np.zeros(display.shape, dtype=np.uint8)
    else:
        normalized = np.nan_to_num(display.astype(np.float64), nan=0.0, posinf=maximum, neginf=0.0)
        pixels = np.rint(np.clip(normalized / maximum, 0.0, 1.0) * 255.0).astype(np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(pixels, mode="L").save(path)


def write_siril_script(
    path: Path,
    basename: str,
    transform: str,
    minpairs: int | None,
    reference_index: int,
) -> None:
    register = f"register {basename} -prefix=r_ -transf={transform}"
    if minpairs:
        register += f" -minpairs={minpairs}"
    path.write_text(
        "\n".join(
            [
                "requires 1.4.0",
                f"convert {basename} -debayer",
                f"setref {basename}_ {reference_index}",
                register,
                "",
            ]
        ),
        encoding="ascii",
    )


def write_siril_findstar_script(path: Path, basename: str, frame_count: int) -> None:
    lines = ["requires 1.4.0"]
    for index in range(1, frame_count + 1):
        lines.extend(
            [
                f"load {basename}_{index:05d}.fit",
                f"findstar -layer=1 -out={basename}_stars_{index:05d}.tsv",
            ]
        )
    path.write_text("\n".join([*lines, ""]), encoding="ascii")


def safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    text = re.sub(r"_+", "_", text).strip("_")
    return text or "target"


def processing_method_token(stack_method: str, rankfit_fraction: int) -> str:
    if stack_method == "rankfit":
        return f"rankfit5_p{rankfit_fraction}"
    return stack_method


def iso_compact(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def format_exposure_token(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return safe_name(f"{float(value):.1f}s")
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return safe_name(f"{float(text):.1f}s")
    return safe_name(text if text.lower().endswith("s") else f"{text}s")


def exposure_filter_from_filename(name: str) -> tuple[str | None, str | None]:
    match = re.search(r"_(\d+(?:\.\d+)?)s_([^_]+)_\d{8}-\d{6}", name)
    if not match:
        return None, None
    return safe_name(f"{match.group(1)}s"), safe_name(match.group(2))


def exposure_filter_tokens(first: FitsImage, first_source_name: str) -> tuple[str | None, str | None]:
    exposure = format_exposure_token(first.header.get("EXPOSURE") or first.header.get("EXPTIME"))
    filter_name = first.header.get("FILTER")
    filter_token = safe_name(str(filter_name)) if filter_name else None
    fallback_exposure, fallback_filter = exposure_filter_from_filename(first_source_name)
    return exposure or fallback_exposure, filter_token or fallback_filter


def default_output_stem(
    first: FitsImage,
    first_source_name: str,
    used_times: list[datetime],
    used_frames: int,
) -> str:
    target = safe_name(str(first.header.get("OBJECT") or "target"))
    exposure, filter_token = exposure_filter_tokens(first, first_source_name)
    acquisition = "_".join(part for part in [exposure, filter_token] if part)
    start = iso_compact(used_times[0])
    end = iso_compact(used_times[-1])
    if acquisition:
        return f"{target}_{acquisition}_{start}-{end}_{used_frames}frames"
    return f"{target}_{start}-{end}_{used_frames}frames"


def select_reference_index(files: list[Path], mode: str, explicit_name: str | None = None) -> int:
    if not files:
        raise ValueError("Cannot select a reference from an empty file list")
    if explicit_name:
        requested_name = Path(explicit_name).name
        exact_matches = [index for index, path in enumerate(files, start=1) if path.name == requested_name]
        matches = exact_matches or [
            index for index, path in enumerate(files, start=1) if path.name.casefold() == requested_name.casefold()
        ]
        if not matches:
            raise ValueError(
                f"--reference-frame-file was not found in the selected frames: {requested_name}. "
                "Check the filename and session/time filters."
            )
        if len(matches) > 1:
            raise ValueError(f"--reference-frame-file matched multiple selected frames: {requested_name}")
        return matches[0]
    if mode == "first":
        return 1
    if mode != "middle":
        raise ValueError(f"Unknown reference-frame mode: {mode}")
    dated: list[tuple[datetime, int]] = []
    for index, path in enumerate(files, start=1):
        header, _cards, _offset = read_fits_header(path)
        dated.append((parse_time(header["DATE-OBS"]), index))
    midpoint = dated[0][0] + (dated[-1][0] - dated[0][0]) / 2
    return min(dated, key=lambda item: (abs((item[0] - midpoint).total_seconds()), item[0]))[1]


def cleanup_intermediate_images(work_dir: Path, basename: str, copied: list[Path], frame_count: int) -> list[str]:
    candidates = [*copied]
    for i in range(1, frame_count + 1):
        candidates.append(work_dir / f"{basename}_{i:05d}.fit")
        candidates.append(work_dir / f"r_{basename}_{i:05d}.fit")
    removed: list[str] = []
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if not path.exists() or not path.is_file():
            continue
        path.unlink()
        removed.append(str(path))
    return removed


def parse_siril_registration(seq_path: Path) -> dict[int, SirilRegistration]:
    if not seq_path.exists():
        return {}
    registrations: dict[int, SirilRegistration] = {}
    sequence_start = 1
    sequence_index = sequence_start
    reference_index: int | None = None
    for raw_line in seq_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if parts[0] == "S" and len(parts) >= 7:
            try:
                sequence_start = int(parts[2])
                reference_index = int(parts[6])
                sequence_index = sequence_start
            except ValueError:
                pass
            continue
        if parts[0] == "I" and len(parts) >= 3:
            try:
                index = int(parts[1])
                selected = parts[2] == "1"
                reg = registrations.setdefault(index, SirilRegistration(index=index))
                reg.selected = selected
                reg.reference_index = reference_index
            except ValueError:
                pass
            continue
        if parts[0].startswith("R") and "H" in parts:
            index = sequence_index
            sequence_index += 1
            h_index = parts.index("H")
            matrix_values = parts[h_index + 1 : h_index + 10]
            if len(matrix_values) != 9:
                continue
            try:
                matrix = tuple(float(value) for value in matrix_values)
                fwhm = float(parts[1])
                weighted_fwhm = float(parts[2])
                roundness = float(parts[3])
                detected_stars = int(float(parts[h_index - 1])) if h_index >= 1 else None
            except ValueError:
                continue
            reg = registrations.setdefault(index, SirilRegistration(index=index))
            reg.reference_index = reference_index
            reg.fwhm_px = fwhm or None
            reg.weighted_fwhm_px = weighted_fwhm or None
            reg.roundness = roundness or None
            reg.detected_stars = detected_stars
            reg.matrix = matrix  # type: ignore[assignment]
    return registrations


def parse_siril_match_diagnostics(output: str) -> dict[int, SirilMatchDiagnostics]:
    """Extract per-frame correspondence counts from Siril's registration log."""
    diagnostics: dict[int, SirilMatchDiagnostics] = {}
    current: SirilMatchDiagnostics | None = None
    patterns = (
        ("initial_pairs", re.compile(r"Initial pair matches:\s*(\d+)", re.IGNORECASE), int),
        ("fitted_pairs", re.compile(r"Pair matches after fitting:\s*(\d+)", re.IGNORECASE), int),
        ("inlier_fraction", re.compile(r"Inliers:\s*([0-9.+-]+)", re.IGNORECASE), float),
    )
    for raw_line in output.splitlines():
        image_match = re.search(r"Matching stars in image\s+(\d+)\s*:\s*done", raw_line, re.IGNORECASE)
        if image_match:
            index = int(image_match.group(1))
            current = diagnostics.setdefault(index, SirilMatchDiagnostics(index=index))
            continue
        if current is None:
            continue
        for attribute, pattern, conversion in patterns:
            match = pattern.search(raw_line)
            if match:
                setattr(current, attribute, conversion(match.group(1)))
                break
    return diagnostics


def parse_siril_findstar_diagnostics(
    output: str,
    work_dir: Path,
    basename: str,
    frame_count: int,
) -> dict[int, SirilRegistration]:
    """Read sequential FINDSTAR results produced after a failed registration."""
    registrations: dict[int, SirilRegistration] = {}
    current_index: int | None = None
    file_pattern = re.compile(rf"Reading FITS:\s+file\s+{re.escape(basename)}_(\d{{5}})\.fit", re.IGNORECASE)
    found_pattern = re.compile(
        r"Found\s+(\d+)\s+Gaussian profile stars.*?\(FWHM\s+([0-9.+-]+)\)",
        re.IGNORECASE,
    )
    for raw_line in output.splitlines():
        file_match = file_pattern.search(raw_line)
        if file_match:
            current_index = int(file_match.group(1))
            continue
        found_match = found_pattern.search(raw_line)
        if current_index is not None and found_match:
            registration = registrations.setdefault(
                current_index,
                SirilRegistration(index=current_index),
            )
            registration.detected_stars = int(found_match.group(1))
            registration.fwhm_px = float(found_match.group(2))
            current_index = None

    for index in range(1, frame_count + 1):
        catalog = work_dir / f"{basename}_stars_{index:05d}.tsv"
        if not catalog.exists():
            continue
        fwhm_pairs: list[tuple[float, float]] = []
        for raw_line in catalog.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw_line or raw_line.startswith("#"):
                continue
            parts = raw_line.split("\t")
            if len(parts) < 9:
                continue
            try:
                fwhm_x = float(parts[7])
                fwhm_y = float(parts[8])
            except ValueError:
                continue
            if fwhm_x > 0.0 and fwhm_y > 0.0:
                fwhm_pairs.append((fwhm_x, fwhm_y))
        registration = registrations.setdefault(index, SirilRegistration(index=index))
        registration.detected_stars = len(fwhm_pairs)
        if fwhm_pairs:
            ratios = [min(x, y) / max(x, y) for x, y in fwhm_pairs]
            registration.roundness = float(np.median(np.asarray(ratios, dtype=np.float64)))
            if registration.fwhm_px is None:
                sizes = [math.sqrt(x * y) for x, y in fwhm_pairs]
                registration.fwhm_px = float(np.median(np.asarray(sizes, dtype=np.float64)))
    return registrations


def merge_registration_diagnostics(
    registrations: dict[int, SirilRegistration],
    diagnostics: dict[int, SirilRegistration],
) -> dict[int, SirilRegistration]:
    for index, diagnostic in diagnostics.items():
        registration = registrations.setdefault(index, SirilRegistration(index=index))
        if registration.detected_stars is None:
            registration.detected_stars = diagnostic.detected_stars
        if registration.fwhm_px is None:
            registration.fwhm_px = diagnostic.fwhm_px
        if registration.weighted_fwhm_px is None:
            registration.weighted_fwhm_px = diagnostic.weighted_fwhm_px
        if registration.roundness is None:
            registration.roundness = diagnostic.roundness
    return registrations


def collect_failed_registration_diagnostics(
    siril_cmd: Path | None,
    registration_dir: Path,
    basename: str,
    frame_count: int,
    verbose: bool,
) -> dict[int, SirilRegistration]:
    script = registration_dir / "diagnose_background_stars.ssf"
    write_siril_findstar_script(script, basename, frame_count)
    print(
        f"[diagnostics] Registration failed; measuring stars in {frame_count} frame(s) individually",
        flush=True,
    )
    try:
        output = run_siril(siril_cmd, registration_dir, script, verbose)
        return parse_siril_findstar_diagnostics(output, registration_dir, basename, frame_count)
    except Exception as error:
        print(f"[warning] Per-frame star diagnostics failed: {error}", file=sys.stderr, flush=True)
        return {}
    finally:
        if not verbose:
            script.unlink(missing_ok=True)
        for index in range(1, frame_count + 1):
            (registration_dir / f"{basename}_stars_{index:05d}.tsv").unlink(missing_ok=True)


def registration_validation_issues(
    files: list[Path],
    registration_dir: Path,
    basename: str,
    registrations: dict[int, SirilRegistration],
    minpairs: int,
) -> dict[int, list[str]]:
    """Return per-frame reasons that a background-star registration is unusable."""
    issues: dict[int, list[str]] = {}
    for index, source in enumerate(files, start=1):
        registration = registrations.get(index)
        registered = registration_dir / f"r_{basename}_{index:05d}.fit"
        reasons: list[str] = []
        if not registered.exists():
            reasons.append("registered FITS was not produced")
        if registration is None:
            reasons.append("Siril registration metadata is missing")
        else:
            if registration.selected is not True:
                reasons.append("not selected by Siril")
            if registration.matrix is None:
                reasons.append("registration transform is missing")
            if registration.detected_stars is None:
                reasons.append("detected-star count is missing")
            elif registration.detected_stars < minpairs:
                reasons.append(f"only {registration.detected_stars} detected star(s); requires {minpairs}")
        if reasons:
            issues[index] = reasons
    return issues


REGISTRATION_DIAGNOSTIC_FIELDS = [
    "index",
    "source",
    "is_reference",
    "used",
    "reason",
    "fwhm_px",
    "weighted_fwhm_px",
    "roundness",
    "detected_stars",
    "initial_matched_pairs",
    "fitted_matched_pairs",
    "inlier_fraction",
    "star_tx_px",
    "star_ty_px",
    "star_rotation_deg",
    "star_scale",
]


def build_registration_diagnostic_rows(
    files: list[Path],
    reference_index: int,
    registrations: dict[int, SirilRegistration],
    matches: dict[int, SirilMatchDiagnostics],
    issues: dict[int, list[str]],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index, source in enumerate(files, start=1):
        registration = registrations.get(index, SirilRegistration(index=index))
        match = matches.get(index, SirilMatchDiagnostics(index=index))
        reasons = issues.get(index, [])
        rows.append(
            {
                "index": index,
                "source": source.name,
                "is_reference": index == reference_index,
                "used": not reasons,
                "reason": "; ".join(reasons),
                "fwhm_px": registration.fwhm_px,
                "weighted_fwhm_px": registration.weighted_fwhm_px,
                "roundness": registration.roundness,
                "detected_stars": registration.detected_stars,
                "initial_matched_pairs": match.initial_pairs,
                "fitted_matched_pairs": match.fitted_pairs,
                "inlier_fraction": match.inlier_fraction,
                "star_tx_px": registration.star_tx_px,
                "star_ty_px": registration.star_ty_px,
                "star_rotation_deg": registration.star_rotation_deg,
                "star_scale": registration.star_scale,
            }
        )
    return rows


def write_registration_diagnostics(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REGISTRATION_DIAGNOSTIC_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_console_safe(text: str, stream = None) -> None:
    stream = stream or sys.stdout
    encoding = stream.encoding or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, end="", file=stream, flush=True)


def siril_failure_reason(output: str) -> str | None:
    markers = (
        "not enough free disk space",
        "not enough space to save the output images",
        "registration aborted",
        "finalizing sequence processing failed",
        "script execution failed",
    )
    matches: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line and any(marker in line.lower() for marker in markers) and line not in matches:
            matches.append(line)
    return "; ".join(matches) if matches else None


def siril_reference_star_count(output: str) -> int | None:
    matches = re.findall(r"Found\s+(\d+)\s+stars\s+in\s+reference", output, flags=re.IGNORECASE)
    return int(matches[-1]) if matches else None


def siril_registration_failure_message(
    output: str,
    reference_index: int,
    reference_name: str,
    diagnostics_path: Path | None = None,
) -> str:
    reference = f"reference {reference_index} ({reference_name})"
    if "No image was registered to the reference" in output:
        message = (
            f"Background-star registration failed: Siril could not align any frame to {reference}. "
            "Choose a sharper frame with more detected stars using --reference-frame-file."
        )
    elif "Not enough free disk space" in output:
        message = "Siril ran out of disk space while registering frames. Free space or select another --work-root."
    else:
        message = f"Siril background-star registration failed for {reference}."
    if diagnostics_path is not None and diagnostics_path.exists():
        message += f" Diagnostics: {diagnostics_path}"
    return message


def resolve_siril_command(explicit: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit.expanduser())
    env_command = os.environ.get("SIRIL_CLI", "").strip()
    if env_command:
        candidates.append(Path(env_command).expanduser())
    if os.name == "nt":
        candidates.append(REPO_ROOT / "tools" / "siril-1.4.1" / "siril" / "bin" / "siril-cli.exe")
        candidates.append(REPO_ROOT / "siril-cli.cmd")
        for name in ("siril-cli.exe", "siril-cli"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))
    else:
        found = shutil.which("siril-cli")
        if found:
            candidates.append(Path(found))
        if sys.platform == "darwin":
            candidates.extend(
                [
                    Path("/Applications/Siril.app/Contents/MacOS/siril-cli"),
                    Path("/Applications/SiriL.app/Contents/MacOS/siril-cli"),
                    Path("/Applications/Siril.app/Contents/MacOS/siril"),
                    Path("/Applications/SiriL.app/Contents/MacOS/siril"),
                ]
            )

    checked: list[str] = []
    for candidate in candidates:
        if not candidate.is_absolute() and candidate.parent == Path("."):
            found = shutil.which(str(candidate))
            if found:
                return Path(found)
        checked.append(str(candidate))
        if candidate.is_file():
            return candidate.resolve()
    details = ", ".join(checked) if checked else "no candidates"
    raise FileNotFoundError(
        "Siril CLI was not found. Install Siril, put siril-cli on PATH, "
        f"set SIRIL_CLI, or pass --siril. Checked: {details}"
    )


def build_siril_command(siril_cmd: Path, work_dir: Path, script_path: Path) -> list[str]:
    arguments = ["-d", str(work_dir), "-s", str(script_path)]
    return [str(siril_cmd), *arguments]


def siril_requires_windows_shell(siril_cmd: Path) -> bool:
    return os.name == "nt" and siril_cmd.suffix.lower() in {".cmd", ".bat"}


def run_siril(siril_cmd: Path | None, work_dir: Path, script_path: Path, verbose: bool = True) -> str:
    resolved_siril = resolve_siril_command(siril_cmd)
    cmd = build_siril_command(resolved_siril, work_dir, script_path)
    use_shell = siril_requires_windows_shell(resolved_siril)
    if verbose:
        process = subprocess.Popen(
            cmd,
            shell=use_shell,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        output_lines: list[str] = []
        if process.stdout is None:
            raise RuntimeError("Siril stdout pipe was not created")
        for line in iter(process.stdout.readline, ""):
            output_lines.append(line)
            write_console_safe(line)
        process.stdout.close()
        returncode = process.wait()
        output = "".join(output_lines)
        failure = siril_failure_reason(output)
        if failure:
            raise SirilRegistrationError(f"Siril registration failed: {failure}", output)
        if returncode != 0:
            raise SirilRegistrationError(f"Siril registration exited with status {returncode}", output)
        return output

    completed = subprocess.run(
        cmd,
        shell=use_shell,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    combined_output = completed.stdout + "\n" + completed.stderr
    failure = siril_failure_reason(combined_output)
    if failure:
        write_console_safe(completed.stdout, sys.stderr)
        write_console_safe(completed.stderr, sys.stderr)
        raise SirilRegistrationError(f"Siril registration failed: {failure}", combined_output)
    if completed.returncode != 0:
        write_console_safe(completed.stdout, sys.stderr)
        write_console_safe(completed.stderr, sys.stderr)
        raise SirilRegistrationError(
            f"Siril registration exited with status {completed.returncode}",
            combined_output,
        )
    for line in completed.stderr.splitlines():
        if "pyproject.toml" in line and "Failed to install Python module" in line:
            continue
        if "Reading sequence failed" in line and "frame.seq" in line:
            continue
        write_console_safe(line + "\n", sys.stderr)
    return combined_output


def make_work_dir(base: Path, name: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    work_dir = base / f"{name}-{stamp}"
    work_dir.mkdir(parents=True, exist_ok=False)
    return work_dir


def prepare_work_dir(work_dir: Path | None, work_root: Path, work_name: str) -> Path:
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir
    return make_work_dir(work_root, work_name)


def repair_windows_cmd_path(path: Path) -> Path:
    text = str(path)
    for quote in ('"', "'"):
        if quote in text:
            prefix = text.split(quote, 1)[0]
            repaired = Path(prefix)
            if repaired.exists():
                print(f"Repaired source path: {path} -> {repaired}", file=sys.stderr)
                return repaired
    if text.endswith('"') or text.endswith("'"):
        repaired = Path(text.rstrip("\"'"))
        if repaired.exists():
            print(f"Repaired source path: {path} -> {repaired}", file=sys.stderr)
            return repaired
    return path


def looks_like_stacked_outputs(files: list[Path]) -> bool:
    fits_files = [path for path in files if is_fits_frame(path)]
    return bool(fits_files) and all(path.name.lower().startswith("stacked_") for path in fits_files)


def is_fits_frame(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in {".fit", ".fits"}


def resolve_source_dir(source_dir: Path, pattern: str) -> Path:
    source_dir = repair_windows_cmd_path(source_dir)
    files = sorted((path for path in source_dir.glob(pattern) if is_fits_frame(path)), key=lambda p: p.name) if source_dir.exists() else []
    sub_candidate = source_dir.with_name(f"{source_dir.name}_sub")
    sub_files = sorted((path for path in sub_candidate.glob(pattern) if is_fits_frame(path)), key=lambda p: p.name) if sub_candidate.exists() else []
    if sub_files and (not files or looks_like_stacked_outputs(files)):
        print(f"Using subframe directory: {sub_candidate}", file=sys.stderr)
        return sub_candidate
    return source_dir


def is_failed_frame(path: Path) -> bool:
    return "_failed_" in path.name.lower()


def choose_files(source_dir: Path, pattern: str, count: int | None, include_failed_frames: bool = False) -> list[Path]:
    source_dir = resolve_source_dir(source_dir, pattern)
    files = sorted((path for path in source_dir.glob(pattern) if is_fits_frame(path)), key=lambda p: p.name)
    if not include_failed_frames:
        original_count = len(files)
        files = [path for path in files if not is_failed_frame(path)]
        skipped = original_count - len(files)
        if skipped:
            print(f"Skipped {skipped} failed frame(s); use --include-failed-frames to keep them.", file=sys.stderr)
    if count:
        files = files[:count]
    if not files:
        raise FileNotFoundError(f"No non-failed files matching {pattern} in {source_dir}")
    return files


def parse_bounded_pair(text: str, offset: int, minimum: int, maximum: int, default: int) -> int:
    if len(text) < offset + 2:
        return default
    token = text[offset : offset + 2]
    if not token.isdigit():
        return default
    value = int(token)
    if value < minimum or value > maximum:
        return default
    return value


def parse_session_at(value: str) -> datetime:
    text = value.strip()
    date_part, separator, time_part = text.partition("-")
    if len(date_part) < 4 or not date_part[:4].isdigit():
        raise ValueError("--session-at requires at least a four-digit year")
    year = int(date_part[:4])
    month = parse_bounded_pair(date_part, 4, 1, 12, 1)
    max_day = calendar.monthrange(year, month)[1]
    day = parse_bounded_pair(date_part, 6, 1, max_day, 1)
    if not separator:
        time_part = ""
    hour = parse_bounded_pair(time_part, 0, 0, 23, 0)
    minute = parse_bounded_pair(time_part, 2, 0, 59, 0)
    second = parse_bounded_pair(time_part, 4, 0, 59, 0)
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    return datetime(year, month, day, hour, minute, second, tzinfo=local_tz).astimezone(timezone.utc)


def split_sessions_by_gap(dated: list[tuple[datetime, Path]], gap_min: float) -> list[list[tuple[datetime, Path]]]:
    sessions: list[list[tuple[datetime, Path]]] = []
    current: list[tuple[datetime, Path]] = []
    previous_time: datetime | None = None
    gap_seconds = gap_min * 60.0
    for item in dated:
        if previous_time is not None and (item[0] - previous_time).total_seconds() > gap_seconds:
            if current:
                sessions.append(current)
            current = []
        current.append(item)
        previous_time = item[0]
    if current:
        sessions.append(current)
    return sessions


def choose_session(
    sessions: list[list[tuple[datetime, Path]]],
    session_index: int,
    session_at: str | None,
) -> list[tuple[datetime, Path]]:
    if session_at:
        threshold = parse_session_at(session_at)
        for session in sessions:
            if session[0][0] >= threshold:
                return session
        raise SystemExit(
            f"--session-at {session_at} did not match any session; "
            f"latest session starts at {sessions[-1][0][0].isoformat()}"
        )
    if session_index < 1 or session_index > len(sessions):
        raise SystemExit(f"--session-index {session_index} is out of range; found {len(sessions)} session(s)")
    return sessions[session_index - 1]


def filter_files_by_time(
    files: list[Path],
    after: str | None,
    before: str | None,
    session_gap_min: float | None,
    session_index: int,
    session_at: str | None = None,
) -> list[Path]:
    if not after and not before and session_gap_min is None and not session_at:
        return files
    dated: list[tuple[datetime, Path]] = []
    for path in files:
        header, _cards, _offset = read_fits_header(path)
        if "DATE-OBS" not in header:
            continue
        dated.append((parse_time(header["DATE-OBS"]), path))
    dated.sort(key=lambda item: item[0])
    if after:
        after_time = parse_time(after)
        dated = [item for item in dated if item[0] >= after_time]
    if before:
        before_time = parse_time(before)
        dated = [item for item in dated if item[0] <= before_time]
    if session_gap_min is not None:
        sessions = split_sessions_by_gap(dated, session_gap_min)
        dated = choose_session(sessions, session_index, session_at)
    elif session_at:
        threshold = parse_session_at(session_at)
        dated = [item for item in dated if item[0] >= threshold]
    return [path for _when, path in dated]


def main() -> int:
    parser = argparse.ArgumentParser(description="Stack Seestar frames on a moving target")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--ephemeris-csv", required=True, type=Path)
    parser.add_argument("--wcs-fits", type=Path)
    parser.add_argument("--astrometry-json", type=Path)
    parser.add_argument("--work-dir", type=Path, help="Use this exact work directory instead of creating one under --work-root")
    parser.add_argument("--work-root", type=Path, default=REPO_ROOT / "metcalf_output")
    parser.add_argument("--work-name", help="Work directory stem. Defaults to '<OBJECT>_<method>'.")
    parser.add_argument("--pattern", default="*.fit*")
    parser.add_argument("--count", type=int)
    parser.add_argument("--after", help="Keep frames at or after this UTC ISO timestamp")
    parser.add_argument("--before", help="Keep frames at or before this UTC ISO timestamp")
    parser.add_argument(
        "--include-failed-frames",
        action="store_true",
        help="Include Seestar files whose names contain '_failed_'. They are skipped by default.",
    )
    parser.add_argument("--session-gap-min", type=float, help="Split frames into sessions at gaps larger than this many minutes")
    parser.add_argument("--session-index", type=int, default=1, help="1-based session to use with --session-gap-min")
    parser.add_argument(
        "--session-at",
        help=(
            "Select the first session whose first DATE-OBS is at or after this local time. "
            "Format: YYYYMMDD or YYYYMMDD-hhmmss; hh, mm, ss must be two digits when present."
        ),
    )
    parser.add_argument("--siril", type=Path, help="Siril CLI path. Defaults to SIRIL_CLI, PATH, or an OS-standard location.")
    parser.add_argument("--basename", default="frame")
    parser.add_argument("--registration-transform", default="similarity")
    parser.add_argument(
        "--registration-minpairs",
        type=int,
        default=6,
        help=(
            "Minimum matched background-star pairs. The reference frame must meet this "
            "requirement; other frames below it are skipped. Defaults to 6."
        ),
    )
    parser.add_argument(
        "--stack-method",
        choices=("mean", "median", "rankfit"),
        default="mean",
        help="Per-pixel combination method. median and rankfit exclude exact-zero samples. Defaults to mean.",
    )
    parser.add_argument(
        "--dual-stack",
        action="store_true",
        help="Also create target-masked star, robust comet, and dual-alignment composite masters.",
    )
    parser.add_argument(
        "--comet-mask-radius-px",
        type=float,
        help="Dual-stack circular comet mask radius in pixels. Defaults to an FWHM-based value.",
    )
    parser.add_argument(
        "--composite-min-star-fraction",
        type=float,
        help=(
            "Minimum local star-master contribution fraction used to protect the composite. "
            "Defaults to a conservative automatic value."
        ),
    )
    parser.add_argument(
        "--comet-clean-method",
        choices=("median", "rankfit", "sigma"),
        help=(
            "Dual-stack comet-master cleaner. Defaults to median, or rankfit when --stack-method=rankfit; "
            "sigma uses numpy-only MAD rejection."
        ),
    )
    parser.add_argument(
        "--comet-sigma-low",
        type=float,
        default=3.0,
        help="Lower-side MAD rejection threshold for --comet-clean-method sigma. Defaults to 3.0.",
    )
    parser.add_argument(
        "--comet-sigma-high",
        type=float,
        default=3.0,
        help="Upper-side MAD rejection threshold for --comet-clean-method sigma. Defaults to 3.0.",
    )
    parser.add_argument(
        "--comet-directional-filter",
        action="store_true",
        help=(
            "Enable the experimental numpy-only directional comet-master cleaner. "
            "Disabled by default; the existing sigma/median output is unchanged when omitted."
        ),
    )
    parser.add_argument(
        "--comet-directional-size",
        type=int,
        default=2,
        help="Directional cleaner half-size in pixels; samples -size..+size. Defaults to 2.",
    )
    parser.add_argument(
        "--composite-mask-method",
        choices=("circle", "tail"),
        default="circle",
        help="Dual-stack composite mask method. circle is the stable default; tail adds connected residual structure.",
    )
    parser.add_argument(
        "--composite-tail-sigma",
        type=float,
        default=3.0,
        help="Robust threshold for --composite-mask-method tail. Defaults to 3.0.",
    )
    parser.add_argument(
        "--composite-tail-smooth-px",
        type=float,
        default=5.0,
        help="Low-frequency smoothing radius for the optional tail mask. Defaults to 5 pixels.",
    )
    parser.add_argument(
        "--composite-tail-length-px",
        type=float,
        default=256.0,
        help="Maximum radial tail-mask extension beyond the core/support region. Defaults to 256 pixels.",
    )
    parser.add_argument("--horizons-object", help="Horizons target label to record in dual-stack metadata")
    parser.add_argument("--horizons-command", help="Horizons COMMAND/designation to record in dual-stack metadata")
    parser.add_argument(
        "--rankfit-fraction",
        type=int,
        default=50,
        help="Central ranked-sample percentage used by rankfit (1-100). Defaults to 50.",
    )
    parser.add_argument(
        "--reference-frame",
        choices=("first", "middle"),
        default="first",
        help="Use the first frame or the frame nearest the session midpoint as registration/WCS reference.",
    )
    parser.add_argument(
        "--padding-policy",
        choices=("valid", "legacy"),
        default="valid",
        help=(
            "Handle all-zero Siril registration padding as missing pixels and normalize mean stacks "
            "by an integer per-pixel contribution count (valid, default), or reproduce the previous "
            "padding behavior (legacy)."
        ),
    )
    parser.add_argument(
        "--zero-sample-policy",
        choices=("exclude", "include"),
        default="exclude",
        help=(
            "Exclude or include exact-zero samples in median and rank-fit stacks. "
            "Defaults to exclude."
        ),
    )
    parser.add_argument(
        "--reference-frame-file",
        help="Use this FITS filename as the registration/WCS reference; overrides --reference-frame.",
    )
    parser.add_argument(
        "--output-prefix",
        help="Output filename stem. Defaults to '<OBJECT>_<start>-<end>_<N>frames'.",
    )
    parser.add_argument("--preview-flip-vertical", action="store_true")
    parser.add_argument("--output-bitpix", choices=("float32", "uint16"), default="float32")
    parser.add_argument("--uint16-scale", choices=("none", "global", "per-channel"), default="none")
    parser.add_argument("--scale-low-percentile", type=float, default=0.0)
    parser.add_argument("--scale-high-percentile", type=float, default=100.0)
    parser.add_argument("--preview-low-percentile", type=float, default=5.0)
    parser.add_argument("--preview-high-percentile", type=float, default=99.95)
    parser.add_argument(
        "--saturation-warning",
        type=str.lower,
        choices=("enable", "disable"),
        default="disable",
        help="Enable or disable separate saturation-warning preview PNGs. Defaults to disable.",
    )
    parser.add_argument(
        "--saturation-threshold-percent",
        type=float,
        default=90.0,
        help="Warn above this percentage of the FITS saturation level. Defaults to 90.",
    )
    parser.add_argument(
        "--saturation-color",
        default="FF0000",
        help="Warning overlay color as six-digit RGB hex. Defaults to FF0000.",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep intermediate image FITS files generated for Siril registration.",
    )
    parser.set_defaults(verbose=True)
    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", help="Show registration and per-frame stack progress (default).")
    parser.add_argument("--no-verbose", dest="verbose", action="store_false", help="Hide detailed registration and per-frame progress.")
    args = parser.parse_args()

    if not 1 <= args.rankfit_fraction <= 100:
        parser.error("--rankfit-fraction must be an integer from 1 to 100")
    if args.registration_minpairs < 1:
        parser.error("--registration-minpairs must be at least 1")
    if args.comet_mask_radius_px is not None and (
        not math.isfinite(args.comet_mask_radius_px) or args.comet_mask_radius_px <= 0.0
    ):
        parser.error("--comet-mask-radius-px must be finite and greater than zero")
    if args.composite_min_star_fraction is not None and (
        not math.isfinite(args.composite_min_star_fraction)
        or not 0.0 <= args.composite_min_star_fraction <= 1.0
    ):
        parser.error("--composite-min-star-fraction must be between zero and one")
    if not math.isfinite(args.comet_sigma_low) or args.comet_sigma_low <= 0.0:
        parser.error("--comet-sigma-low must be finite and greater than zero")
    if not math.isfinite(args.comet_sigma_high) or args.comet_sigma_high <= 0.0:
        parser.error("--comet-sigma-high must be finite and greater than zero")
    if not math.isfinite(args.composite_tail_sigma) or args.composite_tail_sigma <= 0.0:
        parser.error("--composite-tail-sigma must be finite and greater than zero")
    if not math.isfinite(args.composite_tail_smooth_px) or args.composite_tail_smooth_px < 0.0:
        parser.error("--composite-tail-smooth-px must be finite and non-negative")
    if not math.isfinite(args.composite_tail_length_px) or args.composite_tail_length_px <= 0.0:
        parser.error("--composite-tail-length-px must be finite and greater than zero")
    if not 0.0 < args.saturation_threshold_percent <= 100.0:
        parser.error("--saturation-threshold-percent must be greater than 0 and at most 100")
    try:
        args.saturation_color = normalize_saturation_color(args.saturation_color)
    except ValueError as error:
        parser.error(str(error))

    if not args.wcs_fits and not args.astrometry_json:
        parser.error("--wcs-fits or --astrometry-json is required")

    args.source_dir = resolve_source_dir(args.source_dir, args.pattern)
    files = filter_files_by_time(
        choose_files(args.source_dir, args.pattern, None, args.include_failed_frames),
        args.after,
        args.before,
        args.session_gap_min,
        args.session_index,
        args.session_at,
    )
    if args.count:
        files = files[: args.count]
    if not files:
        raise FileNotFoundError("No files remain after time/session filtering")
    reference_index = select_reference_index(files, args.reference_frame, args.reference_frame_file)
    reference_mode = "file" if args.reference_frame_file else args.reference_frame
    reference_source = files[reference_index - 1]
    ephemeris = load_ephemeris(args.ephemeris_csv)
    if not args.work_name:
        reference_header, _cards, _offset = read_fits_header(reference_source)
        target = safe_name(str(reference_header.get("OBJECT") or reference_source.parent.name))
        args.work_name = f"{target}_{processing_method_token(args.stack_method, args.rankfit_fraction)}"
    work_dir = prepare_work_dir(args.work_dir, args.work_root, args.work_name)
    registration_dir = work_dir / "registration_images"
    registration_dir.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    try:
        if args.verbose:
            print(f"[prepare] Copying {len(files)} source frames for Siril registration", flush=True)
        for i, source in enumerate(files, start=1):
            if args.verbose:
                print(f"[prepare] frame {i}/{len(files)}: {source.name}", flush=True)
            destination = registration_dir / f"{args.basename}_src_{i:05d}.fit"
            copied.append(destination)
            shutil.copy2(source, destination)
    except Exception:
        if not args.no_cleanup:
            cleanup_intermediate_images(registration_dir, args.basename, copied, len(copied))
        raise

    siril_script = registration_dir / "register_background_stars.ssf"
    write_siril_script(
        siril_script,
        args.basename,
        args.registration_transform,
        args.registration_minpairs,
        reference_index,
    )
    registration_seq = registration_dir / f"{args.basename}_.seq"
    try:
        if args.verbose:
            print(f"[registration] Siril background-star registration: {len(copied)} frames", flush=True)
        siril_output = run_siril(args.siril, registration_dir, siril_script, args.verbose)
        star_registrations = parse_siril_registration(registration_seq)
        match_diagnostics = parse_siril_match_diagnostics(siril_output)
        registration_issues = registration_validation_issues(
            files,
            registration_dir,
            args.basename,
            star_registrations,
            args.registration_minpairs,
        )
        registration_diagnostic_rows = build_registration_diagnostic_rows(
            files,
            reference_index,
            star_registrations,
            match_diagnostics,
            registration_issues,
        )
        registration_snapshot_csv = work_dir / "registration_diagnostics.csv"
        write_registration_diagnostics(registration_snapshot_csv, registration_diagnostic_rows)
        print(f"[registration] Diagnostics: {registration_snapshot_csv}", flush=True)
        reference_issues = registration_issues.get(reference_index)
        if reference_issues:
            details = "; ".join(reference_issues)
            print(
                "[warning] The selected reference frame has insufficient background stars; "
                "no stack will be created.\n"
                f"  - {reference_index}/{len(files)} {reference_source.name}: {details}",
                file=sys.stderr,
                flush=True,
            )
            raise RuntimeError("Selected reference frame cannot support background-star registration; see warning above")
        registered_count = sum(
            (registration_dir / f"r_{args.basename}_{i:05d}.fit").exists()
            for i in range(1, len(copied) + 1)
        )
        if args.verbose:
            usable_count = len(copied) - len(registration_issues)
            print(
                f"[registration] Siril produced {registered_count}/{len(copied)} registered frames; "
                f"{usable_count}/{len(copied)} will be stacked",
                flush=True,
            )
    except SirilRegistrationError as error:
        star_registrations = parse_siril_registration(registration_seq)
        star_registrations = merge_registration_diagnostics(
            star_registrations,
            collect_failed_registration_diagnostics(
                args.siril,
                registration_dir,
                args.basename,
                len(files),
                args.verbose,
            ),
        )
        match_diagnostics = parse_siril_match_diagnostics(error.output)
        registration_issues = registration_validation_issues(
            files,
            registration_dir,
            args.basename,
            star_registrations,
            args.registration_minpairs,
        )
        registration_snapshot_csv = work_dir / "registration_diagnostics.csv"
        write_registration_diagnostics(
            registration_snapshot_csv,
            build_registration_diagnostic_rows(
                files,
                reference_index,
                star_registrations,
                match_diagnostics,
                registration_issues,
            ),
        )
        print(f"[registration] Diagnostics: {registration_snapshot_csv}", flush=True)
        reference_registration = star_registrations.get(reference_index)
        reference_stars = (
            reference_registration.detected_stars
            if reference_registration is not None
            else siril_reference_star_count(error.output)
        )
        if reference_stars is not None and reference_stars < args.registration_minpairs:
            print(
                "[warning] The selected reference frame has insufficient background stars; "
                "no stack will be created.\n"
                f"  - {reference_index}/{len(files)} {reference_source.name}: only {reference_stars} "
                f"background star(s); requires {args.registration_minpairs}",
                file=sys.stderr,
                flush=True,
            )
        if not args.no_cleanup:
            removed = cleanup_intermediate_images(registration_dir, args.basename, copied, len(copied))
            print(f"[cleanup] Removed {len(removed)} intermediate FITS files after registration failure", flush=True)
        raise RuntimeError(
            siril_registration_failure_message(
                error.output,
                reference_index,
                reference_source.name,
                registration_snapshot_csv,
            )
        ) from None
    except Exception:
        if not args.no_cleanup:
            removed = cleanup_intermediate_images(registration_dir, args.basename, copied, len(copied))
            print(f"[cleanup] Removed {len(removed)} intermediate FITS files after registration failure", flush=True)
        raise
    reference = read_fits(copied[reference_index - 1])
    height = int(reference.header["NAXIS2"])
    width = int(reference.header["NAXIS1"])
    if args.wcs_fits:
        wcs = WcsModel.from_wcs_fits(args.wcs_fits)
    else:
        wcs = WcsModel.from_astrometry_json(args.astrometry_json, width, height)

    reference_time = parse_time(reference.header["DATE-OBS"])
    reference_target = interpolate_ephemeris(ephemeris, reference_time)
    reference_x, reference_y = wcs.world_to_pixel(reference_target.ra_deg, reference_target.dec_deg)
    ephemeris_metadata = read_ephemeris_metadata(args.ephemeris_csv)
    dual_comet_method = args.comet_clean_method or ("rankfit" if args.stack_method == "rankfit" else "median")
    composite_method = "subtractive" if args.dual_stack else None
    comet_mask_radius_px: float | None = None
    composite_support_margin_px: float | None = None
    comet_mask_radius_source: str | None = None
    composite_support_margin_source: str | None = None
    if args.dual_stack:
        comet_mask_radius_px, comet_mask_radius_source = resolve_comet_mask_radius(
            args.comet_mask_radius_px,
            [registration.fwhm_px for registration in star_registrations.values()],
        )
        composite_support_margin_px = max(4.0, float(comet_mask_radius_px))
        composite_support_margin_source = "auto-mask-radius"
        if args.verbose:
            print(
                f"[dual] target mask radius={comet_mask_radius_px:.3f}px ({comet_mask_radius_source}); "
                f"support margin={composite_support_margin_px:.3f}px ({composite_support_margin_source}); "
                f"clean comet method={dual_comet_method}",
                flush=True,
            )
    saturation_enabled = args.saturation_warning == "enable"
    warning_color_rgb = saturation_rgb(args.saturation_color)
    metcalf_saturation_mask = np.zeros((height, width), dtype=bool) if saturation_enabled else None
    star_saturation_mask = np.zeros((height, width), dtype=bool) if saturation_enabled else None
    saturated_frame_count = 0
    saturation_level_unavailable_frames = 0

    sum_image: np.ndarray | None = None
    count_image: np.ndarray | None = None
    star_sum_image: np.ndarray | None = None
    star_count_image: np.ndarray | None = None
    median_stack: MedianAccumulator | None = None
    median_star_stack: MedianAccumulator | None = None
    dual_star_sum_image: np.ndarray | None = None
    dual_star_count_image: np.ndarray | None = None
    dual_target_union_mask: np.ndarray | None = None
    dual_comet_count_image: np.ndarray | None = None
    dual_star_stack: MedianAccumulator | None = None
    dual_comet_stack: MedianAccumulator | None = None
    subtractive_frame_specs: list[tuple[int, Path, float, float]] = []
    dual_shift_vectors: list[tuple[float, float]] = []
    if args.dual_stack:
        dual_star_count_image = np.zeros((height, width), dtype=np.uint32)
        dual_target_union_mask = np.zeros((height, width), dtype=bool)
        dual_comet_count_image = np.zeros((height, width), dtype=np.uint32)
    frame_rows: list[dict[str, object]] = []
    used_times: list[datetime] = []
    used = 0
    if args.verbose:
        print(
            f"[stack] padding policy={args.padding_policy}; zero-sample policy={args.zero_sample_policy}",
            flush=True,
        )

    for i, source in enumerate(copied, start=1):
        if args.verbose:
            print(
                f"[stack:{args.stack_method}] frame {i}/{len(copied)}: {files[i - 1].name}",
                flush=True,
            )
        registered = registration_dir / f"r_{args.basename}_{i:05d}.fit"
        star_reg = star_registrations.get(i, SirilRegistration(index=i))
        match_diag = match_diagnostics.get(i, SirilMatchDiagnostics(index=i))
        registration_metrics = {
            "is_reference": i == reference_index,
            "fwhm_px": star_reg.fwhm_px,
            "weighted_fwhm_px": star_reg.weighted_fwhm_px,
            "roundness": star_reg.roundness,
            "detected_stars": star_reg.detected_stars,
            "initial_matched_pairs": match_diag.initial_pairs,
            "fitted_matched_pairs": match_diag.fitted_pairs,
            "inlier_fraction": match_diag.inlier_fraction,
            "star_pairs": match_diag.fitted_pairs,
            "star_selected": star_reg.selected,
            "star_reference_index": star_reg.reference_index,
            "star_tx_px": star_reg.star_tx_px,
            "star_ty_px": star_reg.star_ty_px,
            "star_rotation_deg": star_reg.star_rotation_deg,
            "star_scale": star_reg.star_scale,
        }
        issues = registration_issues.get(i)
        if issues:
            frame_rows.append(
                {
                    "index": i,
                    "source": files[i - 1].name,
                    "used": False,
                    "reason": "; ".join(issues),
                    **registration_metrics,
                }
            )
            continue
        source_header, _cards, _offset = read_fits_header(source)
        frame_time = parse_time(source_header["DATE-OBS"])
        target = interpolate_ephemeris(ephemeris, frame_time)
        x, y = wcs.world_to_pixel(target.ra_deg, target.dec_deg)
        dx = reference_x - x
        dy = reference_y - y
        image, registered_unit_scale = restore_registered_units(read_fits(registered), source_header)
        source_valid = registered_valid_mask(image.data) if args.padding_policy == "valid" else None
        shifted, mask2d = shift_image(image.data, dx, dy, source_valid)
        star_shifted, star_mask2d = shift_image(image.data, 0.0, 0.0, source_valid)
        dual_target_mask: np.ndarray | None = None
        dual_star_mask2d = star_mask2d
        if args.dual_stack:
            if comet_mask_radius_px is None:
                raise RuntimeError("Dual-stack target mask radius was not resolved")
            dual_target_mask = circular_target_mask(
                (height, width),
                x,
                y,
                comet_mask_radius_px,
            )
            if dual_target_union_mask is None:
                raise RuntimeError("Dual-stack target union mask was not initialized")
            dual_target_union_mask |= dual_target_mask
            dual_star_mask2d = star_mask2d & ~dual_target_mask
        saturation_level: float | None = None
        saturation_threshold_count: float | None = None
        subframe_max_count: float | None = None
        saturated_pixel_count = 0
        frame_saturation_warning = False
        if saturation_enabled:
            (
                frame_saturation_mask,
                saturation_level,
                saturation_threshold_count,
                subframe_max_count,
            ) = detect_saturation(image.data, source_header, args.saturation_threshold_percent)
            if saturation_level is None:
                saturation_level_unavailable_frames += 1
                if args.verbose:
                    print(
                        f"[saturation] level unavailable for {files[i - 1].name}; no pixels marked",
                        flush=True,
                    )
            else:
                saturated_pixel_count = int(np.count_nonzero(frame_saturation_mask))
                frame_saturation_warning = saturated_pixel_count > 0
                if frame_saturation_warning:
                    saturated_frame_count += 1
                    if star_saturation_mask is None or metcalf_saturation_mask is None:
                        raise RuntimeError("Saturation warning masks were not initialized")
                    star_saturation_mask |= frame_saturation_mask & star_mask2d
                    metcalf_saturation_mask |= shift_boolean_mask(frame_saturation_mask, dx, dy)
                    if args.verbose:
                        print(
                            f"[saturation] {files[i - 1].name}: max={subframe_max_count:.3f}, "
                            f"threshold={saturation_threshold_count:.3f}, pixels={saturated_pixel_count}",
                            flush=True,
                        )
        if args.stack_method == "mean":
            sum_image, count_image = add_to_average(sum_image, count_image, shifted, mask2d)
            star_sum_image, star_count_image = add_to_average(
                star_sum_image,
                star_count_image,
                star_shifted,
                star_mask2d,
            )
        else:
            if median_stack is None:
                median_stack = MedianAccumulator(
                    work_dir / f"{args.stack_method}_metcalf_frames.npy",
                    len(files),
                    shifted.shape,
                    exclude_zero_samples=args.zero_sample_policy == "exclude",
                )
                median_star_stack = MedianAccumulator(
                    work_dir / f"{args.stack_method}_star_frames.npy",
                    len(files),
                    star_shifted.shape,
                    exclude_zero_samples=args.zero_sample_policy == "exclude",
                )
            median_stack.add(shifted, mask2d)
            if median_star_stack is None:
                raise RuntimeError("Star median accumulator was not initialized")
            median_star_stack.add(star_shifted, star_mask2d)
        if args.dual_stack:
            if dual_target_mask is None or dual_star_count_image is None or dual_comet_count_image is None:
                raise RuntimeError("Dual-stack masks were not initialized")
            dual_shift_vectors.append((float(dx), float(dy)))
            dual_comet_count_mask = mask2d
            if args.zero_sample_policy == "exclude":
                nonzero = np.any(shifted != 0.0, axis=0) if shifted.ndim == 3 else shifted != 0.0
                dual_comet_count_mask = dual_comet_count_mask & nonzero
            dual_comet_count_image += dual_comet_count_mask.astype(np.uint16)
            if args.stack_method == "mean":
                dual_star_sum_image, dual_star_count_image = add_to_average(
                    dual_star_sum_image,
                    dual_star_count_image,
                    star_shifted,
                    dual_star_mask2d,
                )
            else:
                dual_star_count_mask = dual_star_mask2d
                if args.zero_sample_policy == "exclude":
                    nonzero = np.any(star_shifted != 0.0, axis=0) if star_shifted.ndim == 3 else star_shifted != 0.0
                    dual_star_count_mask = dual_star_count_mask & nonzero
                dual_star_count_image += dual_star_count_mask.astype(np.uint16)
                if dual_star_stack is None:
                    dual_star_stack = MedianAccumulator(
                        work_dir / f"dual_{args.stack_method}_star_frames.npy",
                        len(files),
                        star_shifted.shape,
                        exclude_zero_samples=args.zero_sample_policy == "exclude",
                    )
                dual_star_stack.add(star_shifted, dual_star_mask2d)
            if dual_comet_stack is None:
                dual_comet_stack = MedianAccumulator(
                    work_dir / f"dual_{dual_comet_method}_comet_frames.npy",
                    len(files),
                    shifted.shape,
                    exclude_zero_samples=args.zero_sample_policy == "exclude",
                )
            dual_comet_stack.add(shifted, mask2d)
            if composite_method == "subtractive":
                subtractive_frame_specs.append((i, registered, dx, dy))
        used += 1
        used_times.append(frame_time)
        frame_rows.append(
            {
                "index": i,
                "source": files[i - 1].name,
                "registered": registered.name,
                "used": True,
                "date_obs": frame_time.isoformat(),
                "ra_deg": target.ra_deg,
                "dec_deg": target.dec_deg,
                "target_x_1based": x,
                "target_y_1based": y,
                "extra_dx_px": dx,
                "extra_dy_px": dy,
                **registration_metrics,
                "registered_unit_scale": registered_unit_scale,
                "saturation_warning": frame_saturation_warning if saturation_enabled else None,
                "saturation_level": saturation_level,
                "saturation_threshold_count": saturation_threshold_count,
                "subframe_max_count": subframe_max_count,
                "saturated_pixel_count": saturated_pixel_count if saturation_enabled else None,
            }
        )

    if used == 0:
        raise RuntimeError("No registered frames were available for moving-target stacking")

    median_temp_removed: list[str] = []
    if args.verbose:
        print(
            f"[stack:{args.stack_method}] finalizing {used}/{len(copied)} accepted frames",
            flush=True,
        )
    if args.stack_method == "mean":
        stack = finalize_average(sum_image, count_image)
        star_stack = finalize_average(star_sum_image, star_count_image)
    elif args.stack_method == "median":
        if median_stack is None or median_star_stack is None:
            raise RuntimeError("Median accumulators were not initialized")
        stack = median_stack.finalize()
        star_stack = median_star_stack.finalize()
        if median_stack.close(remove=not args.no_cleanup):
            median_temp_removed.append(str(median_stack.path))
        if median_star_stack.close(remove=not args.no_cleanup):
            median_temp_removed.append(str(median_star_stack.path))
    else:
        if median_stack is None or median_star_stack is None:
            raise RuntimeError("Rank-fit accumulators were not initialized")
        stack = median_stack.finalize_rankfit(args.rankfit_fraction)
        star_stack = median_star_stack.finalize_rankfit(args.rankfit_fraction)
        if median_stack.close(remove=not args.no_cleanup):
            median_temp_removed.append(str(median_stack.path))
        if median_star_stack.close(remove=not args.no_cleanup):
            median_temp_removed.append(str(median_star_stack.path))
    dual_star_master: np.ndarray | None = None
    dual_comet_master: np.ndarray | None = None
    dual_comet_sigma_master: np.ndarray | None = None
    dual_comet_directional_master: np.ndarray | None = None
    directional_filter_geometry_diagnostics: dict[str, object] = {}
    directional_filter_diagnostics: dict[str, object] = {
        "enabled": bool(args.comet_directional_filter),
        "applied": False,
        "size_px": int(args.comet_directional_size),
        "core_protection": False,
    }
    if args.dual_stack:
        if dual_comet_stack is None or dual_comet_count_image is None:
            raise RuntimeError("Dual-stack comet accumulator was not initialized")
        if args.stack_method == "mean":
            dual_star_master = finalize_average(dual_star_sum_image, dual_star_count_image)
        elif args.stack_method == "median":
            if dual_star_stack is None:
                raise RuntimeError("Dual-stack star accumulator was not initialized")
            dual_star_master = dual_star_stack.finalize()
        else:
            if dual_star_stack is None:
                raise RuntimeError("Dual-stack star accumulator was not initialized")
            dual_star_master = dual_star_stack.finalize_rankfit(args.rankfit_fraction)
        if dual_comet_method == "median":
            dual_comet_master = dual_comet_stack.finalize()
        elif dual_comet_method == "rankfit":
            dual_comet_master = dual_comet_stack.finalize_rankfit(args.rankfit_fraction)
        else:
            dual_comet_master = dual_comet_stack.finalize_sigma(
                sigma_low=args.comet_sigma_low,
                sigma_high=args.comet_sigma_high,
            )
        dual_comet_sigma_master = dual_comet_master.astype(np.float64, copy=True)
        if args.comet_directional_filter:
            directional_filter_geometry_diagnostics = directional_filter_geometry(dual_shift_vectors)
            directional_filter_diagnostics.update(directional_filter_geometry_diagnostics)
            filter_angle = directional_filter_geometry_diagnostics.get("directional_filter_angle_deg")
            dual_comet_valid_for_clean = dual_comet_count_image > 0
            if filter_angle is None:
                dual_comet_directional_master = dual_comet_sigma_master.copy()
                directional_filter_diagnostics["reason"] = directional_filter_geometry_diagnostics.get(
                    "reason", "direction-unavailable"
                )
            else:
                dual_comet_directional_master, directional_stats = apply_directional_comet_filter(
                    dual_comet_sigma_master,
                    float(filter_angle),
                    size_px=args.comet_directional_size,
                    valid_mask=dual_comet_valid_for_clean,
                )
                directional_filter_diagnostics.update(directional_stats)
                directional_filter_diagnostics["applied"] = True
                dual_comet_directional_master, core_protection_stats = protect_directional_core(
                    dual_comet_sigma_master,
                    dual_comet_directional_master,
                    reference_x,
                    reference_y,
                    comet_mask_radius_px,
                    valid_mask=dual_comet_valid_for_clean,
                )
                directional_filter_diagnostics["core_protection"] = core_protection_stats
            dual_comet_master = dual_comet_directional_master
        if dual_star_stack is not None and dual_star_stack.close(remove=not args.no_cleanup):
            median_temp_removed.append(str(dual_star_stack.path))
        if dual_comet_stack.close(remove=not args.no_cleanup):
            median_temp_removed.append(str(dual_comet_stack.path))
    comparison_stack = concatenate_side_by_side(star_stack, stack)
    if args.verbose:
        print("[output] Writing Metcalf, star-aligned, comparison FITS, and previews", flush=True)
    base_output_stem = args.output_prefix or default_output_stem(
        reference,
        reference_source.name,
        used_times,
        used,
    )
    method_token = processing_method_token(args.stack_method, args.rankfit_fraction)
    output_stem = f"{base_output_stem}_{method_token}"
    output_fits = work_dir / f"{output_stem}_metcalf_stack.fit"
    output_png = work_dir / f"{output_stem}_metcalf_preview.png"
    star_output_fits = work_dir / f"{output_stem}_star_stack.fit"
    star_output_png = work_dir / f"{output_stem}_star_preview.png"
    comparison_output_fits = work_dir / f"{output_stem}_star_left_metcalf_right.fit"
    comparison_output_png = work_dir / f"{output_stem}_star_left_metcalf_right_preview.png"
    dual_star_output_fits: Path | None = None
    dual_star_output_png: Path | None = None
    dual_comet_output_fits: Path | None = None
    dual_comet_output_png: Path | None = None
    dual_composite_output_fits: Path | None = None
    dual_composite_output_png: Path | None = None
    dual_mask_output_png: Path | None = None
    dual_star_count_output_png: Path | None = None
    dual_comparison_stars_output_png: Path | None = None
    dual_comparison_comet_output_png: Path | None = None
    dual_comparison_composite_output_png: Path | None = None
    dual_composite_weight_output_png: Path | None = None
    dual_composite_reliability_output_png: Path | None = None
    dual_metadata_json: Path | None = None
    dual_diagnostics_json: Path | None = None
    dual_comet_sigma_output_fits: Path | None = None
    dual_comet_sigma_output_png: Path | None = None
    dual_comet_directional_output_fits: Path | None = None
    dual_comet_directional_output_png: Path | None = None
    dual_comet_directional_difference_output_fits: Path | None = None
    dual_comet_directional_difference_output_png: Path | None = None
    subtractive_star_output_fits: Path | None = None
    subtractive_cometless_output_png: Path | None = None
    subtractive_residual_output_png: Path | None = None
    subtractive_stars_master: np.ndarray | None = None
    subtractive_residual: np.ndarray | None = None
    subtractive_count_image: np.ndarray | None = None
    subtractive_background: dict[str, object] = {}
    subtractive_residual_statistics: dict[str, object] = {}
    dual_comparison_preview_limits: tuple[float, float] | None = None
    tail_mask_diagnostics: dict[str, object] = {"method": args.composite_mask_method}
    composite: np.ndarray | None = None
    composite_mask: np.ndarray | None = None
    effective_composite_mask: np.ndarray | None = None
    composite_offset: np.ndarray | None = None
    star_background: np.ndarray | None = None
    comet_background: np.ndarray | None = None
    global_star_background: np.ndarray | None = None
    global_comet_background: np.ndarray | None = None
    composite_support: np.ndarray | None = None
    composite_reliability: np.ndarray | None = None
    reliability_diagnostics: dict[str, object] = {}
    background_match_method = "global"
    local_star_background: np.ndarray | None = None
    local_comet_background: np.ndarray | None = None
    local_background_pixel_counts: np.ndarray | None = None
    local_background_annulus: np.ndarray | None = None
    local_background_annulus_inner_radius: float | None = None
    local_background_annulus_outer_radius: float | None = None
    halo_profile: dict[str, object] = {}
    tail_axis_diagnostics: dict[str, object] = {}
    tail_flux_profile: dict[str, object] = {}
    core_flux_diagnostics: dict[str, object] = {}
    trail_metric_before: dict[str, object] = {}
    trail_metric_after: dict[str, object] = {}
    negative_residual_before: dict[str, object] = {}
    negative_residual_after: dict[str, object] = {}
    dual_star_valid: np.ndarray | None = None
    dual_comet_valid: np.ndarray | None = None
    if args.dual_stack:
        if (
            dual_star_master is None
            or dual_comet_master is None
            or comet_mask_radius_px is None
            or composite_support_margin_px is None
            or dual_star_count_image is None
            or dual_comet_count_image is None
            or dual_target_union_mask is None
        ):
            raise RuntimeError("Dual-stack masters were not finalized")
        core_target_mask = circular_target_mask(
            (height, width),
            reference_x,
            reference_y,
            comet_mask_radius_px,
        )
        dual_star_valid = dual_star_count_image > 0
        dual_comet_valid = dual_comet_count_image > 0
        background_region = ~core_target_mask
        global_star_background = background_median(dual_star_master, background_region)
        global_comet_background = background_median(dual_comet_master, background_region)
        star_background = global_star_background.copy()
        comet_background = global_comet_background.copy()
        if args.composite_mask_method == "tail":
            tail_support, tail_mask_diagnostics = build_tail_composite_mask(
                (height, width),
                reference_x,
                reference_y,
                comet_mask_radius_px,
                composite_support_margin_px,
                dual_star_master,
                dual_comet_master,
                dual_star_valid,
                dual_comet_valid,
                global_star_background,
                global_comet_background,
                tail_sigma=args.composite_tail_sigma,
                tail_smoothing_px=args.composite_tail_smooth_px,
                tail_length_px=args.composite_tail_length_px,
            )
        else:
            tail_support = circular_target_mask(
                (height, width),
                reference_x,
                reference_y,
                comet_mask_radius_px,
            )
        minimum_star_fraction, minimum_star_fraction_source = resolve_composite_min_star_fraction(
            args.composite_min_star_fraction,
            used,
        )
        reliability_dilation_px = max(1.0, min(2.0, float(composite_support_margin_px) * 0.25))
        reliability_support, reliability_diagnostics = build_reliability_support(
            dual_target_union_mask,
            dual_star_count_image,
            used,
            minimum_star_fraction,
            dilation_px=reliability_dilation_px,
        )
        reliability_diagnostics["minimum_star_fraction_source"] = minimum_star_fraction_source
        composite_support = (tail_support > 0.0) | reliability_support
        composite_support_pixels = int(np.count_nonzero(composite_support))
        composite_mask = composite_support.astype(np.float32)
        tail_mask_diagnostics["binary_support_pixels"] = int(np.count_nonzero(tail_support > 0.0))
        tail_mask_diagnostics["combined_support_pixels"] = composite_support_pixels

        yy, xx = np.indices((height, width), dtype=np.float64)
        reference_x0 = float(reference_x - 1.0)
        reference_y0 = float(reference_y - 1.0)
        distance = np.hypot(xx - reference_x0, yy - reference_y0)
        support_distance = distance[composite_support]
        support_extent = float(np.max(support_distance)) if support_distance.size else 0.0
        annulus_inner_radius = max(
            float(comet_mask_radius_px + composite_support_margin_px + 4.0),
            support_extent + 4.0,
        )
        max_radius_to_edge = min(
            reference_x0,
            float(width - 1) - reference_x0,
            reference_y0,
            float(height - 1) - reference_y0,
        )
        annulus_outer_radius = min(
            max_radius_to_edge,
            max(annulus_inner_radius + 24.0, annulus_inner_radius + float(composite_support_margin_px) * 4.0),
        )
        local_background_annulus_inner_radius = annulus_inner_radius
        local_background_annulus_outer_radius = annulus_outer_radius
        if annulus_outer_radius > annulus_inner_radius + 8.0:
            local_background_annulus = (
                (distance >= annulus_inner_radius)
                & (distance < annulus_outer_radius)
                & ~composite_support
            )
            minimum_count = int(math.ceil(float(used) * minimum_star_fraction))
            local_star_valid = dual_star_valid & (dual_star_count_image >= minimum_count)
            local_star_background, local_comet_background, local_background_pixel_counts = (
                robust_local_background_match(
                    dual_star_master,
                    dual_comet_master,
                    local_background_annulus,
                    local_star_valid,
                    dual_comet_valid,
                )
            )
        else:
            local_star_background = np.full_like(global_star_background, np.nan, dtype=np.float64)
            local_comet_background = np.full_like(global_comet_background, np.nan, dtype=np.float64)
            local_background_pixel_counts = np.zeros_like(global_star_background, dtype=np.int64)
            local_background_annulus = np.zeros((height, width), dtype=bool)
        local_background_ok = (
            local_star_background is not None
            and local_comet_background is not None
            and local_background_pixel_counts is not None
            and np.all(np.isfinite(local_star_background))
            and np.all(np.isfinite(local_comet_background))
            and np.all(local_background_pixel_counts >= 32)
        )
        if local_background_ok:
            star_background = local_star_background
            comet_background = local_comet_background
            background_match_method = "local_annulus"
        else:
            star_background = global_star_background
            comet_background = global_comet_background
            background_match_method = "global_fallback"
        if local_background_annulus is None:
            local_background_annulus = np.zeros((height, width), dtype=bool)
        adjusted_comet = dual_comet_master.astype(np.float64, copy=True)
        local_offset = star_background - comet_background
        channels, _profile_height, _profile_width, adjusted_comet_chw = image_shape_chw(adjusted_comet)
        adjusted_comet_chw += local_offset[:, np.newaxis, np.newaxis]
        profile_annuli = ((0.0, 5.0), (5.0, 10.0), (10.0, 15.0), (15.0, 20.0), (20.0, 30.0), (30.0, 50.0))
        star_profile, profile_counts = annular_profile(
            dual_star_master,
            reference_x,
            reference_y,
            dual_star_valid,
            profile_annuli,
        )
        raw_comet_profile, _raw_comet_profile_counts = annular_profile(
            dual_comet_master,
            reference_x,
            reference_y,
            dual_comet_valid,
            profile_annuli,
        )
        comet_profile, _comet_profile_counts = annular_profile(
            adjusted_comet,
            reference_x,
            reference_y,
            dual_comet_valid,
            profile_annuli,
        )
        composite_reliability = np.clip(
            dual_star_count_image.astype(np.float32) / float(used),
            0.0,
            1.0,
        )
        dual_star_output_fits = work_dir / f"{output_stem}_stars.fit"
        dual_star_output_png = work_dir / f"{output_stem}_stars_preview.png"
        dual_comet_output_fits = work_dir / f"{output_stem}_comet_clean.fit"
        dual_comet_output_png = work_dir / f"{output_stem}_comet_preview.png"
        dual_composite_output_fits = work_dir / f"{output_stem}_comet_stars.fit"
        dual_composite_output_png = work_dir / f"{output_stem}_comet_stars_preview.png"
        dual_mask_output_png = work_dir / f"{output_stem}_composite_mask.png"
        dual_star_count_output_png = work_dir / f"{output_stem}_stars_contribution_count.png"
        dual_comparison_stars_output_png = work_dir / f"{output_stem}_comparison_stars.png"
        dual_comparison_comet_output_png = work_dir / f"{output_stem}_comparison_comet.png"
        dual_comparison_composite_output_png = work_dir / f"{output_stem}_comparison_composite.png"
        dual_composite_weight_output_png = work_dir / f"{output_stem}_composite_weight.png"
        dual_composite_reliability_output_png = work_dir / f"{output_stem}_composite_reliability.png"
        dual_metadata_json = work_dir / f"{output_stem}_composite_metadata.json"
        dual_diagnostics_json = work_dir / f"{output_stem}_composite_diagnostics.json"
        dual_comparison_preview_limits = preview_stretch_limits(
            [dual_star_master, dual_comet_master],
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
        )
        if args.comet_directional_filter:
            if dual_comet_sigma_master is None or dual_comet_master is None or dual_comet_valid is None:
                raise RuntimeError("Directional comet diagnostics require sigma and directional masters")
            dual_comet_sigma_output_fits = work_dir / f"{output_stem}_comet_sigma.fit"
            dual_comet_sigma_output_png = work_dir / f"{output_stem}_comet_sigma_preview.png"
            dual_comet_directional_output_fits = work_dir / f"{output_stem}_comet_directional.fit"
            dual_comet_directional_output_png = work_dir / f"{output_stem}_comet_directional_preview.png"
            dual_comet_directional_difference_output_fits = (
                work_dir / f"{output_stem}_comet_directional_difference.fit"
            )
            dual_comet_directional_difference_output_png = (
                work_dir / f"{output_stem}_comet_directional_difference_preview.png"
            )
            tail_axis_diagnostics = estimate_tail_axis(
                dual_comet_sigma_master,
                reference_x,
                reference_y,
                comet_background,
                dual_comet_valid,
                comet_mask_radius_px,
                max_radius_px=max(250.0, float(args.composite_tail_length_px)),
            )
            tail_flux_profile = compare_tail_flux_profiles(
                dual_comet_sigma_master,
                dual_comet_master,
                reference_x,
                reference_y,
                tail_axis_diagnostics,
                comet_background,
                comet_background,
                dual_comet_valid,
            )
            core_flux_diagnostics = core_flux_comparison(
                dual_comet_sigma_master,
                dual_comet_master,
                reference_x,
                reference_y,
                comet_mask_radius_px,
                comet_background,
                comet_background,
                dual_comet_valid,
            )
            trail_angle = directional_filter_geometry_diagnostics.get("star_trail_angle_deg")
            if trail_angle is not None:
                trail_metric_before = oriented_trail_metric(
                    dual_comet_sigma_master,
                    float(trail_angle),
                    dual_comet_valid,
                )
                trail_metric_after = oriented_trail_metric(
                    dual_comet_master,
                    float(trail_angle),
                    dual_comet_valid,
                )
                before_value = trail_metric_before.get("value")
                after_value = trail_metric_after.get("value")
                if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
                    directional_filter_diagnostics["trail_suppression_ratio"] = (
                        None
                        if float(before_value) <= 1.0e-12
                        else float((float(before_value) - float(after_value)) / float(before_value))
                    )
            directional_filter_diagnostics["tail_axis"] = tail_axis_diagnostics
            directional_filter_diagnostics["tail_flux_profile"] = tail_flux_profile
            directional_filter_diagnostics["core_flux"] = core_flux_diagnostics
            directional_filter_diagnostics["trail_metric_before"] = trail_metric_before
            directional_filter_diagnostics["trail_metric_after"] = trail_metric_after
        if composite_method == "subtractive":
            if dual_comet_valid is None:
                raise RuntimeError("Subtractive composite requires a valid comet-master mask")
            subtractive_accumulator = MedianAccumulator(
                work_dir / "subtractive_cometless_frames.npy",
                max(1, len(subtractive_frame_specs)),
                dual_comet_master.shape,
                exclude_zero_samples=False,
            )
            subtractive_count_image = np.zeros((height, width), dtype=np.uint32)
            negative_pixels = 0
            residual_pixels = 0
            residual_after_min = math.inf
            residual_after_max = -math.inf
            residual_after_sum = 0.0
            residual_after_sum_sq = 0.0
            residual_after_medians: list[float] = []
            model_valid_pixels = 0
            residual_per_frame: list[dict[str, object]] = []
            negative_before_pixels = 0
            residual_before_pixels = 0
            residual_before_min = math.inf
            residual_before_max = -math.inf
            residual_before_sum = 0.0
            residual_before_sum_sq = 0.0
            residual_before_medians: list[float] = []
            _model_channels, _model_height, _model_width, adjusted_comet_chw = image_shape_chw(adjusted_comet)
            model_background_chw = np.asarray(star_background, dtype=np.float64)
            comet_signal_model = adjusted_comet.astype(np.float64, copy=True)
            _signal_channels, _signal_height, _signal_width, comet_signal_chw = image_shape_chw(comet_signal_model)
            comet_signal_chw -= model_background_chw[:, np.newaxis, np.newaxis]
            sigma_signal_model: np.ndarray | None = None
            if args.comet_directional_filter and dual_comet_sigma_master is not None:
                sigma_signal_model = dual_comet_sigma_master.astype(np.float64, copy=True)
                _sigma_signal_channels, _sigma_signal_height, _sigma_signal_width, sigma_signal_chw = image_shape_chw(
                    sigma_signal_model
                )
                sigma_signal_chw += local_offset[:, np.newaxis, np.newaxis]
                sigma_signal_chw -= model_background_chw[:, np.newaxis, np.newaxis]
            for frame_index, registered_path, forward_dx, forward_dy in subtractive_frame_specs:
                source_header, _cards, _offset = read_fits_header(files[frame_index - 1])
                registered_image, _registered_unit_scale = restore_registered_units(
                    read_fits(registered_path), source_header
                )
                frame_valid = (
                    registered_valid_mask(registered_image.data)
                    if args.padding_policy == "valid"
                    else np.ones(registered_image.data.shape[-2:], dtype=bool)
                )
                shifted_model, shifted_model_valid = inverse_moving_target_shift(
                    comet_signal_model,
                    forward_dx,
                    forward_dy,
                    dual_comet_valid,
                )
                cometless_frame, cometless_valid = subtract_shifted_comet_model(
                    registered_image.data,
                    shifted_model,
                    frame_valid,
                    shifted_model_valid,
                )
                if sigma_signal_model is not None:
                    shifted_sigma_model, shifted_sigma_valid = inverse_moving_target_shift(
                        sigma_signal_model,
                        forward_dx,
                        forward_dy,
                        dual_comet_valid,
                    )
                    sigma_residual_valid = cometless_valid & shifted_sigma_valid
                    _sigma_channels, _srh, _srw, shifted_sigma_chw = image_shape_chw(shifted_sigma_model)
                    sigma_values = registered_image.data
                    _registered_sigma_channels, _rsh, _rsw, registered_sigma_chw = image_shape_chw(sigma_values)
                    frame_before_values: list[np.ndarray] = []
                    for sigma_channel in range(_sigma_channels):
                        before_values = (
                            registered_sigma_chw[sigma_channel] - shifted_sigma_chw[sigma_channel]
                        )[sigma_residual_valid]
                        before_values = before_values[np.isfinite(before_values)]
                        if before_values.size:
                            negative_before_pixels += int(np.count_nonzero(before_values < 0.0))
                            residual_before_pixels += int(before_values.size)
                            residual_before_min = min(residual_before_min, float(np.min(before_values)))
                            residual_before_max = max(residual_before_max, float(np.max(before_values)))
                            residual_before_sum += float(np.sum(before_values))
                            residual_before_sum_sq += float(np.sum(before_values * before_values))
                            frame_before_values.append(before_values)
                    if frame_before_values:
                        residual_before_medians.append(float(np.median(np.concatenate(frame_before_values))))
                subtractive_accumulator.add(cometless_frame.astype(np.float32), cometless_valid)
                subtractive_count_image += cometless_valid.astype(np.uint32)
                valid_residual = cometless_valid & shifted_model_valid
                channels, _rh, _rw, registered_chw = image_shape_chw(registered_image.data)
                _model_channels, _mh, _mw, model_chw = image_shape_chw(shifted_model)
                frame_stats: dict[str, object] = {
                    "index": int(frame_index),
                    "model_valid_pixels": int(np.count_nonzero(shifted_model_valid)),
                    "frame_valid_pixels": int(np.count_nonzero(frame_valid)),
                    "channels": [],
                }
                channel_stats: list[dict[str, object]] = []
                for channel in range(channels):
                    values = (registered_chw[channel] - model_chw[channel])[valid_residual]
                    values = values[np.isfinite(values)]
                    if values.size == 0:
                        channel_stats.append({"count": 0})
                        continue
                    negative_count = int(np.count_nonzero(values < 0.0))
                    negative_pixels += negative_count
                    residual_pixels += int(values.size)
                    residual_after_min = min(residual_after_min, float(np.min(values)))
                    residual_after_max = max(residual_after_max, float(np.max(values)))
                    residual_after_sum += float(np.sum(values))
                    residual_after_sum_sq += float(np.sum(values * values))
                    residual_after_medians.append(float(np.median(values)))
                    channel_stats.append(
                        {
                            "count": int(values.size),
                            "min": float(np.min(values)),
                            "max": float(np.max(values)),
                            "median": float(np.median(values)),
                            "mean": float(np.mean(values)),
                            "std": float(np.std(values)),
                            "negative_count": negative_count,
                            "negative_fraction": float(negative_count / values.size),
                        }
                    )
                frame_stats["channels"] = channel_stats
                residual_per_frame.append(frame_stats)
                model_valid_pixels += int(np.count_nonzero(shifted_model_valid))
            subtractive_stars_master = subtractive_accumulator.finalize_sigma(
                sigma_low=args.comet_sigma_low,
                sigma_high=args.comet_sigma_high,
            )
            if subtractive_accumulator.close(remove=not args.no_cleanup):
                median_temp_removed.append(str(subtractive_accumulator.path))
            core_exclusion = core_target_mask
            comet_model_background = model_background_chw.copy()
            raw_comet_model_background = background_median(dual_comet_master, core_exclusion)
            subtractive_background_region = local_background_annulus
            if np.count_nonzero(subtractive_background_region) < 32:
                subtractive_background_region = ~core_exclusion
                subtractive_background_match_method = "global_fallback"
            else:
                subtractive_background_match_method = "local_annulus"
            cometless_background_raw = background_median(
                subtractive_stars_master,
                ~subtractive_background_region,
            )
            cometless_background_offset = star_background - cometless_background_raw
            _subtractive_channels, _subtractive_height, _subtractive_width, subtractive_stars_chw = image_shape_chw(
                subtractive_stars_master
            )
            subtractive_stars_chw += cometless_background_offset[:, np.newaxis, np.newaxis]
            cometless_background = background_median(
                subtractive_stars_master,
                ~subtractive_background_region,
            )
            subtractive_composite = add_reference_comet_model(
                subtractive_stars_master,
                comet_signal_model,
                dual_comet_valid,
                subtractive_count_image > 0,
            )
            subtractive_residual = subtractive_stars_master - dual_star_master
            final_background = background_median(
                subtractive_composite,
                ~subtractive_background_region,
            )
            subtractive_background = {
                "comet_model_background": comet_model_background.tolist(),
                "comet_model_background_raw": raw_comet_model_background.tolist(),
                "cometless_background_raw": cometless_background_raw.tolist(),
                "cometless_background_offset": cometless_background_offset.tolist(),
                "cometless_background": cometless_background.tolist(),
                "final_background": final_background.tolist(),
                "background_match_method": subtractive_background_match_method,
                "cometless_contribution_count": {
                    "min": int(np.min(subtractive_count_image)),
                    "median": float(np.median(subtractive_count_image)),
                    "max": int(np.max(subtractive_count_image)),
                    "nonzero_pixels": int(np.count_nonzero(subtractive_count_image)),
                },
                "negative_pixel_fraction_before_final_stack": float(
                    negative_pixels / residual_pixels if residual_pixels else 0.0
                ),
                "comet_model_valid_fraction": float(
                    model_valid_pixels / (max(1, len(subtractive_frame_specs)) * height * width)
                ),
                "subtraction_residual_statistics": {
                    "frame_count": len(residual_per_frame),
                    "negative_pixels": negative_pixels,
                    "finite_pixels": residual_pixels,
                    "per_frame": residual_per_frame,
                    "cometless_minus_existing_star_master": {
                        "min": float(np.nanmin(subtractive_residual)),
                        "max": float(np.nanmax(subtractive_residual)),
                        "median": float(np.nanmedian(subtractive_residual)),
                        "mean": float(np.nanmean(subtractive_residual)),
                        "std": float(np.nanstd(subtractive_residual)),
                    },
                },
            }
            subtractive_residual_statistics = dict(subtractive_background["subtraction_residual_statistics"])
            negative_residual_after = {
                "negative_pixel_fraction": float(negative_pixels / residual_pixels if residual_pixels else 0.0),
                "negative_pixels": int(negative_pixels),
                "finite_pixels": int(residual_pixels),
                "residual_min": None if residual_pixels == 0 else float(residual_after_min),
                "residual_median": None if not residual_after_medians else float(np.median(residual_after_medians)),
                "residual_std": (
                    float(
                        math.sqrt(
                            max(
                                0.0,
                                residual_after_sum_sq / residual_pixels
                                - (residual_after_sum / residual_pixels) ** 2,
                            )
                        )
                    )
                    if residual_pixels
                    else 0.0
                ),
                "definition": "selected comet model subtraction before final cometless stack",
            }
            if sigma_signal_model is not None:
                before_mean = residual_before_sum / residual_before_pixels if residual_before_pixels else 0.0
                before_variance = (
                    residual_before_sum_sq / residual_before_pixels - before_mean * before_mean
                    if residual_before_pixels
                    else 0.0
                )
                negative_residual_before = {
                    "negative_pixel_fraction": float(
                        negative_before_pixels / residual_before_pixels if residual_before_pixels else 0.0
                    ),
                    "negative_pixels": int(negative_before_pixels),
                    "finite_pixels": int(residual_before_pixels),
                    "residual_min": None if residual_before_pixels == 0 else float(residual_before_min),
                    "residual_median": (
                        None if not residual_before_medians else float(np.median(residual_before_medians))
                    ),
                    "residual_std": float(math.sqrt(max(0.0, before_variance))),
                    "definition": "sigma comet model subtraction before final cometless stack",
                }
            composite = subtractive_composite
            # Keep the core/tail/reliability mask as a diagnostic image.  The
            # subtractive algorithm itself has no spatial blend boundary: it
            # models the comet wherever the shifted model is valid, so its
            # effective weight is recorded separately below.
            composite_mask = composite_mask.copy()
            effective_composite_mask = dual_comet_valid.astype(np.float32)
            composite_offset = local_offset.copy()
            background_match_method = f"subtractive_model+{subtractive_background_match_method}_postmatch"
            star_background = cometless_background
            comet_background = comet_model_background
            composite_reliability = np.clip(
                subtractive_count_image.astype(np.float32) / float(max(1, used)),
                0.0,
                1.0,
            )
            composite_profile, _subtractive_profile_counts = annular_profile(
                composite,
                reference_x,
                reference_y,
                subtractive_count_image > 0,
                profile_annuli,
            )
            halo_profile = {
                "annuli_px": [[float(lower), float(upper)] for lower, upper in profile_annuli],
                "valid_pixel_count": profile_counts,
                "star_master": star_profile,
                "comet_master": raw_comet_profile,
                "comet_master_background_matched": comet_profile,
                "composite": composite_profile,
            }
            dual_comparison_preview_limits = preview_stretch_limits(
                [dual_star_master, dual_comet_master, composite],
                low_percentile=args.preview_low_percentile,
                high_percentile=args.preview_high_percentile,
            )
            dual_composite_output_fits = work_dir / f"{output_stem}_comet_stars_subtractive.fit"
            dual_composite_output_png = work_dir / f"{output_stem}_comet_stars_subtractive_preview.png"
            dual_composite_weight_output_png = work_dir / f"{output_stem}_composite_weight.png"
            dual_composite_reliability_output_png = work_dir / f"{output_stem}_composite_reliability.png"
            subtractive_star_output_fits = work_dir / f"{output_stem}_stars_subtractive.fit"
            subtractive_cometless_output_png = work_dir / f"{output_stem}_cometless_star_preview.png"
            subtractive_residual_output_png = work_dir / f"{output_stem}_subtractive_residual_preview.png"
    saturation_output_png = (
        work_dir / f"{output_stem}_metcalf_saturation_warning.png" if saturation_enabled else None
    )
    star_saturation_output_png = (
        work_dir / f"{output_stem}_star_saturation_warning.png" if saturation_enabled else None
    )
    comparison_saturation_output_png = (
        work_dir / f"{output_stem}_star_left_metcalf_right_saturation_warning.png"
        if saturation_enabled
        else None
    )
    shifts_csv = work_dir / f"{output_stem}_shifts.csv"
    registration_diagnostics_csv = work_dir / f"{output_stem}_registration_diagnostics.csv"
    summary_json = work_dir / f"{output_stem}_summary.json"
    star_wcs_header = wcs.to_fits_header(width, height)
    extra_header = {
        **star_wcs_header,
        "MTSTACK": True,
        "MTFRAMES": used,
        "MTXREF": reference_x,
        "MTYREF": reference_y,
        "MTREFRA": reference_target.ra_deg,
        "MTREFDEC": reference_target.dec_deg,
        "STKMODE": args.stack_method,
        "PADPOL": args.padding_policy,
        "ZEROPOL": args.zero_sample_policy if args.stack_method != "mean" else "n/a",
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": reference_mode,
        "REFINDEX": reference_index,
        "MTUNITS": "ADU",
    }
    star_extra_header = {
        **star_wcs_header,
        "STARSTK": True,
        "MTSTACK": False,
        "MTFRAMES": used,
        "MTUNITS": "ADU",
        "STKMODE": args.stack_method,
        "PADPOL": args.padding_policy,
        "ZEROPOL": args.zero_sample_policy if args.stack_method != "mean" else "n/a",
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": reference_mode,
        "REFINDEX": reference_index,
    }
    comparison_extra_header = {
        **star_wcs_header,
        "COMBSTK": True,
        "COMBLEFT": "star_stack",
        "COMBRGHT": "metcalf_stack",
        "COMBW": width,
        "STARSTK": True,
        "MTSTACK": True,
        "MTFRAMES": used,
        "MTUNITS": "ADU",
        "STKMODE": args.stack_method,
        "PADPOL": args.padding_policy,
        "ZEROPOL": args.zero_sample_policy if args.stack_method != "mean" else "n/a",
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": reference_mode,
        "REFINDEX": reference_index,
    }
    dual_history = [
        "Dual-alignment composite generated from the same source frames",
        "Star-aligned master + comet-aligned master",
        "This image is a composite and should not be used directly for photometry",
    ]
    dual_component_history = [
        "Dual-alignment component generated from the same source frames",
        "Star-aligned master and comet-aligned master are separate data products",
    ]
    dual_star_extra_header: dict[str, object] | None = None
    dual_comet_extra_header: dict[str, object] | None = None
    dual_composite_extra_header: dict[str, object] | None = None
    if args.dual_stack:
        if comet_mask_radius_px is None or composite_support_margin_px is None:
            raise RuntimeError("Dual-stack header parameters were not resolved")
        dual_star_extra_header = {
            **star_wcs_header,
            "MTPROC": "DUALCOMP",
            "MTCOMP": "STAR",
            "MTFRAMES": used,
            "MTMASKR": comet_mask_radius_px,
            "MTSUPMR": composite_support_margin_px,
            "MTMSKMD": args.composite_mask_method,
            "MTREFUTC": reference_time.isoformat().replace("+00:00", "Z"),
            "STARSTK": True,
            "MTUNITS": "ADU",
            "STKMODE": args.stack_method,
            "REFMODE": reference_mode,
            "REFINDEX": reference_index,
        }
        dual_comet_extra_header = {
            **star_wcs_header,
            "MTPROC": "DUALCOMP",
            "MTCOMP": "COMET",
            "MTFRAMES": used,
            "MTMASKR": comet_mask_radius_px,
            "MTSUPMR": composite_support_margin_px,
            "MTMSKMD": args.composite_mask_method,
            "MTCLEAN": f"{dual_comet_method}+directional" if args.comet_directional_filter else dual_comet_method,
            "MTSIGLO": args.comet_sigma_low,
            "MTSIGHI": args.comet_sigma_high,
            "MTREFUTC": reference_time.isoformat().replace("+00:00", "Z"),
            "MTSTACK": True,
            "MTUNITS": "ADU",
            "STKMODE": dual_comet_method,
            "REFMODE": reference_mode,
            "REFINDEX": reference_index,
            "MTDIRF": bool(args.comet_directional_filter),
            "MTDIRSZ": args.comet_directional_size if args.comet_directional_filter else 0,
        }
        dual_composite_extra_header = {
            **star_wcs_header,
            "MTPROC": "DUALCOMP",
            "MTCOMP": "STAR+COMET",
            "MTREFUTC": reference_time.isoformat().replace("+00:00", "Z"),
            "MTMASKR": comet_mask_radius_px,
            "MTSUPMR": composite_support_margin_px,
            "MTMSKMD": args.composite_mask_method,
            "MTCLEAN": f"{dual_comet_method}+directional" if args.comet_directional_filter else dual_comet_method,
            "MTFRAMES": used,
            "MTSTACK": True,
            "STARSTK": True,
            "MTUNITS": "ADU",
            "STKMODE": dual_comet_method,
            "REFMODE": reference_mode,
            "REFINDEX": reference_index,
            "MTDIRF": bool(args.comet_directional_filter),
            "MTDIRSZ": args.comet_directional_size if args.comet_directional_filter else 0,
        }
    uint16_stats: list[dict[str, float]] | None = None
    star_uint16_stats: list[dict[str, float]] | None = None
    comparison_uint16_stats: list[dict[str, float]] | None = None
    if args.output_bitpix == "uint16":
        uint16_stats = write_fits_uint16(
            output_fits,
            stack,
            reference.header,
            extra_header,
            args.uint16_scale,
            args.scale_low_percentile,
            args.scale_high_percentile,
        )
        star_uint16_stats = write_fits_uint16(
            star_output_fits,
            star_stack,
            reference.header,
            star_extra_header,
            args.uint16_scale,
            args.scale_low_percentile,
            args.scale_high_percentile,
        )
        comparison_uint16_stats = write_fits_uint16(
            comparison_output_fits,
            comparison_stack,
            reference.header,
            comparison_extra_header,
            args.uint16_scale,
            args.scale_low_percentile,
            args.scale_high_percentile,
        )
    else:
        write_fits_float32(output_fits, stack.astype(np.float32), reference.header, extra_header)
        write_fits_float32(star_output_fits, star_stack.astype(np.float32), reference.header, star_extra_header)
        write_fits_float32(
            comparison_output_fits,
            comparison_stack.astype(np.float32),
            reference.header,
            comparison_extra_header,
        )
    if args.dual_stack:
        if (
            dual_star_output_fits is None
            or dual_comet_output_fits is None
            or dual_composite_output_fits is None
            or dual_star_master is None
            or dual_comet_master is None
            or composite is None
            or dual_star_extra_header is None
            or dual_comet_extra_header is None
            or dual_composite_extra_header is None
        ):
            raise RuntimeError("Dual-stack output paths or masters were not initialized")
        if args.output_bitpix == "uint16":
            write_fits_uint16(
                dual_star_output_fits,
                dual_star_master,
                reference.header,
                dual_star_extra_header,
                args.uint16_scale,
                args.scale_low_percentile,
                args.scale_high_percentile,
                history=dual_component_history,
            )
            write_fits_uint16(
                dual_comet_output_fits,
                dual_comet_master,
                reference.header,
                dual_comet_extra_header,
                args.uint16_scale,
                args.scale_low_percentile,
                args.scale_high_percentile,
                history=dual_component_history,
            )
            write_fits_uint16(
                dual_composite_output_fits,
                composite,
                reference.header,
                dual_composite_extra_header,
                args.uint16_scale,
                args.scale_low_percentile,
                args.scale_high_percentile,
                history=dual_history,
            )
        else:
            write_fits_float32(
                dual_star_output_fits,
                dual_star_master.astype(np.float32),
                reference.header,
                dual_star_extra_header,
                history=dual_component_history,
            )
            write_fits_float32(
                dual_comet_output_fits,
                dual_comet_master.astype(np.float32),
                reference.header,
                dual_comet_extra_header,
                history=dual_component_history,
            )
            write_fits_float32(
                dual_composite_output_fits,
                composite.astype(np.float32),
                reference.header,
                dual_composite_extra_header,
                history=dual_history,
            )
        if args.comet_directional_filter:
            if (
                dual_comet_sigma_master is None
                or dual_comet_directional_master is None
                or dual_comet_sigma_output_fits is None
                or dual_comet_directional_output_fits is None
                or dual_comet_directional_difference_output_fits is None
            ):
                raise RuntimeError("Directional comet output paths or masters were not initialized")
            sigma_header = dict(dual_comet_extra_header)
            sigma_header["MTCLEAN"] = dual_comet_method
            directional_header = dict(dual_comet_extra_header)
            directional_header["MTCLEAN"] = f"{dual_comet_method}+directional"
            difference = dual_comet_sigma_master - dual_comet_directional_master
            write_fits_float32(
                dual_comet_sigma_output_fits,
                dual_comet_sigma_master.astype(np.float32),
                reference.header,
                sigma_header,
                history=[
                    "Experimental directional-clean comparison input",
                    "Original sigma/median comet master before directional suppression",
                ],
            )
            write_fits_float32(
                dual_comet_directional_output_fits,
                dual_comet_directional_master.astype(np.float32),
                reference.header,
                directional_header,
                history=[
                    "Experimental numpy-only directional comet cleaner",
                    "Bright samples are lowered to a perpendicular directional median; dark samples are preserved",
                ],
            )
            write_fits_float32(
                dual_comet_directional_difference_output_fits,
                difference.astype(np.float32),
                reference.header,
                directional_header,
                history=[
                    "Sigma comet master minus directional comet master",
                    "Positive values indicate signal suppressed by the directional cleaner",
                ],
            )
        if composite_method == "subtractive":
            if subtractive_star_output_fits is None or subtractive_stars_master is None:
                raise RuntimeError("Subtractive star output was not initialized")
            write_fits_float32(
                subtractive_star_output_fits,
                subtractive_stars_master.astype(np.float32),
                reference.header,
                dual_star_extra_header,
                history=[
                    "Experimental DSS-style subtractive star master",
                    "Comet model subtracted from star-registered frames without intermediate clipping",
                ],
            )
    export_preview_png(
        output_png,
        stack,
        flip_vertical=args.preview_flip_vertical,
        low_percentile=args.preview_low_percentile,
        high_percentile=args.preview_high_percentile,
    )
    export_preview_png(
        star_output_png,
        star_stack,
        flip_vertical=args.preview_flip_vertical,
        low_percentile=args.preview_low_percentile,
        high_percentile=args.preview_high_percentile,
    )
    export_preview_png(
        comparison_output_png,
        comparison_stack,
        flip_vertical=args.preview_flip_vertical,
        low_percentile=args.preview_low_percentile,
        high_percentile=args.preview_high_percentile,
    )
    if args.dual_stack:
        if (
            dual_star_output_png is None
            or dual_comet_output_png is None
            or dual_composite_output_png is None
            or dual_mask_output_png is None
            or dual_star_count_output_png is None
            or dual_comparison_stars_output_png is None
            or dual_comparison_comet_output_png is None
            or dual_comparison_composite_output_png is None
            or dual_composite_weight_output_png is None
            or dual_composite_reliability_output_png is None
            or dual_star_master is None
            or dual_comet_master is None
            or composite is None
            or composite_mask is None
            or dual_comparison_preview_limits is None
            or dual_star_count_image is None
        ):
            raise RuntimeError("Dual-stack preview paths or masters were not initialized")
        export_preview_png(
            dual_star_output_png,
            dual_star_master,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
        )
        export_preview_png(
            dual_comet_output_png,
            dual_comet_master,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
        )
        if args.comet_directional_filter:
            if (
                dual_comet_sigma_master is None
                or dual_comet_directional_master is None
                or dual_comet_sigma_output_png is None
                or dual_comet_directional_output_png is None
                or dual_comet_directional_difference_output_png is None
            ):
                raise RuntimeError("Directional comet preview outputs or masters were not initialized")
            export_preview_png(
                dual_comet_sigma_output_png,
                dual_comet_sigma_master,
                flip_vertical=args.preview_flip_vertical,
                low_percentile=args.preview_low_percentile,
                high_percentile=args.preview_high_percentile,
            )
            export_preview_png(
                dual_comet_directional_output_png,
                dual_comet_directional_master,
                flip_vertical=args.preview_flip_vertical,
                low_percentile=args.preview_low_percentile,
                high_percentile=args.preview_high_percentile,
            )
            export_preview_png(
                dual_comet_directional_difference_output_png,
                dual_comet_sigma_master - dual_comet_directional_master,
                flip_vertical=args.preview_flip_vertical,
                low_percentile=args.preview_low_percentile,
                high_percentile=args.preview_high_percentile,
            )
        export_preview_png(
            dual_composite_output_png,
            composite,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
        )
        if composite_method == "subtractive":
            if (
                subtractive_stars_master is None
                or subtractive_cometless_output_png is None
                or subtractive_residual_output_png is None
                or subtractive_residual is None
            ):
                raise RuntimeError("Subtractive preview outputs were not initialized")
            export_preview_png(
                subtractive_cometless_output_png,
                subtractive_stars_master,
                flip_vertical=args.preview_flip_vertical,
                low_percentile=args.preview_low_percentile,
                high_percentile=args.preview_high_percentile,
            )
            export_preview_png(
                subtractive_residual_output_png,
                subtractive_residual,
                flip_vertical=args.preview_flip_vertical,
                low_percentile=args.preview_low_percentile,
                high_percentile=args.preview_high_percentile,
            )
        export_preview_png(
            dual_comparison_stars_output_png,
            dual_star_master,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            value_limits=dual_comparison_preview_limits,
        )
        export_preview_png(
            dual_comparison_comet_output_png,
            dual_comet_master,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            value_limits=dual_comparison_preview_limits,
        )
        export_preview_png(
            dual_comparison_composite_output_png,
            composite,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            value_limits=dual_comparison_preview_limits,
        )
        export_mask_png(
            dual_mask_output_png,
            composite_mask,
            flip_vertical=args.preview_flip_vertical,
        )
        export_count_png(
            dual_star_count_output_png,
            dual_star_count_image,
            flip_vertical=args.preview_flip_vertical,
        )
        if effective_composite_mask is None or composite_reliability is None:
            raise RuntimeError("Composite diagnostic previews were not initialized")
        export_mask_png(
            dual_composite_weight_output_png,
            effective_composite_mask,
            flip_vertical=args.preview_flip_vertical,
        )
        export_mask_png(
            dual_composite_reliability_output_png,
            composite_reliability,
            flip_vertical=args.preview_flip_vertical,
        )
    if saturation_enabled:
        if metcalf_saturation_mask is None or star_saturation_mask is None:
            raise RuntimeError("Saturation warning masks were not initialized")
        comparison_saturation_mask = concatenate_side_by_side(
            star_saturation_mask,
            metcalf_saturation_mask,
        )
        export_preview_png(
            saturation_output_png,
            stack,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            warning_mask=metcalf_saturation_mask,
            warning_color=warning_color_rgb,
        )
        export_preview_png(
            star_saturation_output_png,
            star_stack,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            warning_mask=star_saturation_mask,
            warning_color=warning_color_rgb,
        )
        export_preview_png(
            comparison_saturation_output_png,
            comparison_stack,
            flip_vertical=args.preview_flip_vertical,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            warning_mask=comparison_saturation_mask,
            warning_color=warning_color_rgb,
        )
        print(
            f"[saturation] warning previews: {saturated_frame_count}/{used} frames; "
            f"star pixels={int(np.count_nonzero(star_saturation_mask))}; "
            f"metcalf pixels={int(np.count_nonzero(metcalf_saturation_mask))}",
            flush=True,
        )

    print(f"[result] Stacked {used}/{len(copied)} frames; skipped {len(copied) - used}", flush=True)

    with shifts_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "index",
            "source",
            "registered",
            "used",
            "reason",
            "date_obs",
            "ra_deg",
            "dec_deg",
            "target_x_1based",
            "target_y_1based",
            "extra_dx_px",
            "extra_dy_px",
            "is_reference",
            "fwhm_px",
            "weighted_fwhm_px",
            "roundness",
            "detected_stars",
            "initial_matched_pairs",
            "fitted_matched_pairs",
            "inlier_fraction",
            "star_selected",
            "star_reference_index",
            "star_pairs",
            "star_tx_px",
            "star_ty_px",
            "star_rotation_deg",
            "star_scale",
            "registered_unit_scale",
            "saturation_warning",
            "saturation_level",
            "saturation_threshold_count",
            "subframe_max_count",
            "saturated_pixel_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(frame_rows)

    write_registration_diagnostics(registration_diagnostics_csv, frame_rows)
    print(f"[result] Registration diagnostics: {registration_diagnostics_csv}", flush=True)

    if args.dual_stack:
        if (
            dual_metadata_json is None
            or dual_diagnostics_json is None
            or composite_mask is None
            or effective_composite_mask is None
            or composite_offset is None
            or star_background is None
            or comet_background is None
            or dual_star_count_image is None
            or dual_target_union_mask is None
            or dual_comet_count_image is None
            or comet_mask_radius_px is None
            or composite_support_margin_px is None
            or global_star_background is None
            or global_comet_background is None
            or star_background is None
            or comet_background is None
            or local_star_background is None
            or local_comet_background is None
            or local_background_pixel_counts is None
            or composite_support is None
            or composite_reliability is None
        ):
            raise RuntimeError("Dual-stack metadata inputs were not initialized")
        observation_times = [
            parse_time(row["date_obs"])
            for row in frame_rows
            if row.get("used") and row.get("date_obs")
        ]
        rejected_frames = [
            {
                "index": row.get("index"),
                "source": row.get("source"),
                "reason": row.get("reason"),
            }
            for row in frame_rows
            if not row.get("used")
        ]
        horizons_target = str(
            args.horizons_object
            or ephemeris_metadata.get("object")
            or reference.header.get("OBJECT")
            or reference_source.parent.name
        )
        horizons_command = str(
            args.horizons_command
            or ephemeris_metadata.get("command")
            or ephemeris_metadata.get("horizons_command")
            or ""
        )
        core_mask = circular_target_mask(
            (height, width),
            reference_x,
            reference_y,
            comet_mask_radius_px,
        )

        def count_summary(counts: np.ndarray) -> dict[str, float | int | None]:
            if counts.size == 0:
                return {"min": None, "max": None, "median": None, "nonzero_pixels": 0}
            values = counts.astype(np.float64, copy=False)
            return {
                "min": int(np.min(counts)),
                "max": int(np.max(counts)),
                "median": float(np.median(values)),
                "nonzero_pixels": int(np.count_nonzero(counts)),
            }

        target_region_mask = dual_target_union_mask
        target_surrounding_mask = dilate_binary_mask(
            target_region_mask,
            max(4.0, float(math.ceil(comet_mask_radius_px + composite_support_margin_px))),
        ) & ~target_region_mask
        target_region_background = region_median(
            dual_star_master,
            target_region_mask,
            dual_star_valid,
        )
        target_surrounding_background = region_median(
            dual_star_master,
            target_surrounding_mask,
            dual_star_valid,
        )
        target_background_delta = target_region_background - target_surrounding_background
        target_core_counts = dual_star_count_image[core_mask]
        target_region_counts = dual_star_count_image[target_region_mask]

        metadata = {
            "source_files": [str(path.resolve()) for path in files],
            "source_frame_count": len(files),
            "used_frame_count": used,
            "observation_start": min(observation_times).isoformat() if observation_times else None,
            "observation_end": max(observation_times).isoformat() if observation_times else None,
            "reference_frame": reference_source.name,
            "reference_frame_path": str(reference_source.resolve()),
            "reference_utc": reference_time.isoformat(),
            "reference_target_pixel_1based": {"x": reference_x, "y": reference_y},
            "horizons_target": horizons_target,
            "horizons_command": horizons_command or None,
            "horizons_designation": horizons_command or horizons_target,
            "star_stack_method": args.stack_method,
            "comet_stack_method": dual_comet_method,
            "comet_clean_method": dual_comet_method,
            "comet_sigma_low": args.comet_sigma_low,
            "comet_sigma_high": args.comet_sigma_high,
            "mask_radius_px": comet_mask_radius_px,
            "mask_radius_source": comet_mask_radius_source,
            "support_margin_px": composite_support_margin_px,
            "support_margin_source": composite_support_margin_source,
            "composite_min_star_fraction": minimum_star_fraction,
            "composite_min_star_fraction_source": minimum_star_fraction_source,
            "composite_mask_method": args.composite_mask_method,
            "composite_method": composite_method,
            "directional_filter_enabled": bool(args.comet_directional_filter),
            "directional_filter_size": int(args.comet_directional_size),
            "composite_tail_parameters": {
                "sigma": args.composite_tail_sigma,
                "smooth_px": args.composite_tail_smooth_px,
                "length_px": args.composite_tail_length_px,
            },
            "comparison_preview_stretch": {
                "low": dual_comparison_preview_limits[0] if dual_comparison_preview_limits else None,
                "high": dual_comparison_preview_limits[1] if dual_comparison_preview_limits else None,
                "low_percentile": args.preview_low_percentile,
                "high_percentile": args.preview_high_percentile,
            },
            "software_version": SOFTWARE_VERSION,
            "composite": True,
            "source_ephemeris_csv": str(args.ephemeris_csv),
            "padding_policy": args.padding_policy,
            "zero_sample_policy": args.zero_sample_policy,
        }
        diagnostics = {
            "software_version": SOFTWARE_VERSION,
            "star_master_background_median": star_background.tolist(),
            "comet_master_background_median": comet_background.tolist(),
            "background_offset": composite_offset.tolist(),
            "background_match_method": background_match_method,
            "global_star_background": global_star_background.tolist(),
            "global_comet_background": global_comet_background.tolist(),
            "global_background_offset": (global_star_background - global_comet_background).tolist(),
            "local_star_background": [
                None if not math.isfinite(value) else float(value) for value in local_star_background
            ],
            "local_comet_background": [
                None if not math.isfinite(value) else float(value) for value in local_comet_background
            ],
            "local_background_offset": [
                None
                if not math.isfinite(value)
                else float(value)
                for value in (local_star_background - local_comet_background)
            ],
            "annulus_valid_pixel_count": local_background_pixel_counts.tolist(),
            "local_background_annulus_area_pixels": int(np.count_nonzero(local_background_annulus)),
            "local_background_annulus_inner_radius_px": local_background_annulus_inner_radius,
            "local_background_annulus_outer_radius_px": local_background_annulus_outer_radius,
            "reliability": reliability_diagnostics,
            "target_mask_area_pixels": int(np.count_nonzero(core_mask)),
            "composite_mask_area_pixels": int(np.count_nonzero(composite_mask > 0.0)),
            "effective_composite_mask_area_pixels": int(np.count_nonzero(effective_composite_mask > 0.0)),
            "composite_mask_mean_weight": float(np.mean(composite_mask)),
            "composite_support_area_pixels": int(np.count_nonzero(composite_support)),
            "composite_weight_min": float(np.min(effective_composite_mask)),
            "composite_weight_max": float(np.max(effective_composite_mask)),
            "composite_mask_method": args.composite_mask_method,
            "tail_mask": tail_mask_diagnostics,
            "valid_frames": used,
            "source_frames": len(files),
            "rejected_failed_frames": rejected_frames,
            "rejected_failed_frame_count": len(rejected_frames),
            "star_valid_pixel_counts": count_summary(dual_star_count_image),
            "star_target_region_contribution_count": count_summary(target_region_counts),
            "star_target_core_contribution_count": count_summary(target_core_counts),
            "comet_valid_pixel_counts": count_summary(dual_comet_count_image),
            "star_target_region_area_pixels": int(np.count_nonzero(target_region_mask)),
            "star_master_target_region_background_median": [
                None if not math.isfinite(value) else float(value) for value in target_region_background
            ],
            "star_master_target_surrounding_background_median": [
                None if not math.isfinite(value) else float(value) for value in target_surrounding_background
            ],
            "star_master_target_background_delta": [
                None if not math.isfinite(value) else float(value) for value in target_background_delta
            ],
            "comparison_preview_stretch": {
                "low": dual_comparison_preview_limits[0] if dual_comparison_preview_limits else None,
                "high": dual_comparison_preview_limits[1] if dual_comparison_preview_limits else None,
                "low_percentile": args.preview_low_percentile,
                "high_percentile": args.preview_high_percentile,
            },
            "star_valid_pixel_fraction": float(np.count_nonzero(dual_star_count_image) / (height * width)),
            "comet_valid_pixel_fraction": float(np.count_nonzero(dual_comet_count_image) / (height * width)),
            "halo_profile": halo_profile,
            "directional_filter_enabled": bool(args.comet_directional_filter),
            "directional_filter_size": int(args.comet_directional_size),
            "directional_filter_geometry": directional_filter_geometry_diagnostics,
            "directional_filter": directional_filter_diagnostics,
            "comet_motion_dx_px": directional_filter_geometry_diagnostics.get("comet_motion_dx_px"),
            "comet_motion_dy_px": directional_filter_geometry_diagnostics.get("comet_motion_dy_px"),
            "comet_motion_angle_deg": directional_filter_geometry_diagnostics.get("comet_motion_angle_deg"),
            "star_trail_angle_deg": directional_filter_geometry_diagnostics.get("star_trail_angle_deg"),
            "directional_filter_angle_deg": directional_filter_geometry_diagnostics.get(
                "directional_filter_angle_deg"
            ),
            "trail_metric_before": trail_metric_before,
            "trail_metric_after": trail_metric_after,
            "trail_suppression_ratio": directional_filter_diagnostics.get("trail_suppression_ratio"),
            "core_flux_ratio_directional_vs_sigma": core_flux_diagnostics.get(
                "core_flux_ratio_directional_vs_sigma"
            ),
            "core_flux": core_flux_diagnostics,
            "tail_flux_profile": tail_flux_profile,
        }
        if composite_method == "subtractive":
            diagnostics.update(
                {
                    "subtractive": True,
                    "comet_model_method": dual_comet_method,
                    "comet_model_background": subtractive_background.get("comet_model_background"),
                    "comet_model_background_raw": subtractive_background.get("comet_model_background_raw"),
                    "cometless_background_raw": subtractive_background.get("cometless_background_raw"),
                    "cometless_background_offset": subtractive_background.get("cometless_background_offset"),
                    "cometless_background": subtractive_background.get("cometless_background"),
                    "final_background": subtractive_background.get("final_background"),
                    "subtractive_background_match_method": subtractive_background.get(
                        "background_match_method"
                    ),
                    "negative_pixel_fraction_before_final_stack": subtractive_background.get(
                        "negative_pixel_fraction_before_final_stack", 0.0
                    ),
                    "comet_model_valid_fraction": subtractive_background.get("comet_model_valid_fraction", 0.0),
                    "subtraction_residual_statistics": subtractive_residual_statistics,
                    "cometless_contribution_count": subtractive_background.get("cometless_contribution_count"),
                    "subtractive_background_model": "background_subtracted_comet_signal_subtract_then_add_once",
                    "negative_residual_before": negative_residual_before,
                    "negative_residual_after": negative_residual_after,
                }
            )
        else:
            diagnostics["subtractive"] = False
        dual_metadata_json.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        dual_diagnostics_json.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[dual] Metadata: {dual_metadata_json}", flush=True)
        print(f"[dual] Diagnostics: {dual_diagnostics_json}", flush=True)

    removed_intermediate_images: list[str] = []
    if not args.no_cleanup:
        removed_intermediate_images = cleanup_intermediate_images(registration_dir, args.basename, copied, len(files))

    summary = {
        "source_dir": str(args.source_dir),
        "work_dir": str(work_dir),
        "registration_dir": str(registration_dir),
        "ephemeris_csv": str(args.ephemeris_csv),
        "wcs_fits": str(args.wcs_fits) if args.wcs_fits else None,
        "astrometry_json": str(args.astrometry_json) if args.astrometry_json else None,
        "registration_transform": args.registration_transform,
        "registration_minpairs": args.registration_minpairs,
        "registration_seq": str(registration_seq),
        "preview_flip_vertical": args.preview_flip_vertical,
        "preview_low_percentile": args.preview_low_percentile,
        "preview_high_percentile": args.preview_high_percentile,
        "saturation_warning": {
            "mode": args.saturation_warning,
            "enabled": saturation_enabled,
            "threshold_percent": args.saturation_threshold_percent,
            "color": args.saturation_color,
            "saturated_frames": saturated_frame_count if saturation_enabled else None,
            "level_unavailable_frames": saturation_level_unavailable_frames if saturation_enabled else None,
            "star_marked_pixels": (
                int(np.count_nonzero(star_saturation_mask))
                if star_saturation_mask is not None
                else None
            ),
            "metcalf_marked_pixels": (
                int(np.count_nonzero(metcalf_saturation_mask))
                if metcalf_saturation_mask is not None
                else None
            ),
        },
        "cleanup_intermediate_images": not args.no_cleanup,
        "removed_intermediate_images": removed_intermediate_images,
        "removed_intermediate_image_count": len(removed_intermediate_images),
        "removed_median_temporary_files": median_temp_removed,
        "include_failed_frames": args.include_failed_frames,
            "dual_stack": args.dual_stack,
        "composite_method": composite_method,
        "dual_comet_method": dual_comet_method if args.dual_stack else None,
        "comet_clean_method": dual_comet_method if args.dual_stack else None,
        "comet_sigma_low": args.comet_sigma_low if args.dual_stack else None,
        "comet_sigma_high": args.comet_sigma_high if args.dual_stack else None,
        "composite_mask_method": args.composite_mask_method if args.dual_stack else None,
        "composite_tail_sigma": args.composite_tail_sigma if args.dual_stack else None,
        "composite_tail_smooth_px": args.composite_tail_smooth_px if args.dual_stack else None,
        "composite_tail_length_px": args.composite_tail_length_px if args.dual_stack else None,
        "composite_min_star_fraction": (
            args.composite_min_star_fraction if args.dual_stack else None
        ),
        "dual_comparison_preview_limits": (
            {"low": dual_comparison_preview_limits[0], "high": dual_comparison_preview_limits[1]}
            if dual_comparison_preview_limits
            else None
        ),
        "comet_mask_radius_px": comet_mask_radius_px,
        "composite_support_margin_px": composite_support_margin_px,
        "comet_mask_radius_source": comet_mask_radius_source,
        "composite_support_margin_source": composite_support_margin_source,
        "output_bitpix": args.output_bitpix,
        "uint16_scale": args.uint16_scale if args.output_bitpix == "uint16" else None,
        "uint16_scale_low_percentile": args.scale_low_percentile if args.output_bitpix == "uint16" else None,
        "uint16_scale_high_percentile": args.scale_high_percentile if args.output_bitpix == "uint16" else None,
        "uint16_channel_stats": uint16_stats,
        "star_uint16_channel_stats": star_uint16_stats,
        "comparison_uint16_channel_stats": comparison_uint16_stats,
        "input_frames": len(files),
        "used_frames": used,
        "stack_method": args.stack_method,
        "stack_method_token": method_token,
        "padding_policy": args.padding_policy,
        "zero_sample_policy": args.zero_sample_policy if args.stack_method != "mean" else None,
        "rankfit_fraction_percent": args.rankfit_fraction if args.stack_method == "rankfit" else None,
        "rankfit_polynomial_degree": 5 if args.stack_method == "rankfit" else None,
        "reference_frame_mode": reference_mode,
        "reference_frame_index": reference_index,
        "reference_frame": reference_source.name,
        "reference_date_obs": reference_time.isoformat(),
        "reference_target": {
            "ra_deg": reference_target.ra_deg,
            "dec_deg": reference_target.dec_deg,
            "x_1based": reference_x,
            "y_1based": reference_y,
        },
        "linear_units": "ADU",
        "outputs": {
            "fits": str(output_fits),
            "preview_png": str(output_png),
            "metcalf_fits": str(output_fits),
            "metcalf_preview_png": str(output_png),
            "star_fits": str(dual_star_output_fits or star_output_fits),
            "star_preview_png": str(dual_star_output_png or star_output_png),
            "legacy_star_fits": str(star_output_fits) if args.dual_stack else None,
            "legacy_star_preview_png": str(star_output_png) if args.dual_stack else None,
            "comparison_fits": str(comparison_output_fits),
            "comparison_preview_png": str(comparison_output_png),
            "metcalf_saturation_warning_png": (
                str(saturation_output_png) if saturation_output_png else None
            ),
            "star_saturation_warning_png": (
                str(star_saturation_output_png) if star_saturation_output_png else None
            ),
            "comparison_saturation_warning_png": (
                str(comparison_saturation_output_png) if comparison_saturation_output_png else None
            ),
            "dual_star_fits": str(dual_star_output_fits) if dual_star_output_fits else None,
            "dual_star_preview_png": str(dual_star_output_png) if dual_star_output_png else None,
            "dual_comet_fits": str(dual_comet_output_fits) if dual_comet_output_fits else None,
            "dual_comet_preview_png": str(dual_comet_output_png) if dual_comet_output_png else None,
            "dual_composite_fits": str(dual_composite_output_fits) if dual_composite_output_fits else None,
            "dual_composite_preview_png": str(dual_composite_output_png) if dual_composite_output_png else None,
            "dual_composite_mask_png": str(dual_mask_output_png) if dual_mask_output_png else None,
            "dual_stars_contribution_count_png": (
                str(dual_star_count_output_png) if dual_star_count_output_png else None
            ),
            "dual_comparison_stars_png": (
                str(dual_comparison_stars_output_png) if dual_comparison_stars_output_png else None
            ),
            "dual_comparison_comet_png": (
                str(dual_comparison_comet_output_png) if dual_comparison_comet_output_png else None
            ),
            "dual_comparison_composite_png": (
                str(dual_comparison_composite_output_png) if dual_comparison_composite_output_png else None
            ),
            "dual_composite_weight_png": (
                str(dual_composite_weight_output_png) if dual_composite_weight_output_png else None
            ),
            "dual_composite_reliability_png": (
                str(dual_composite_reliability_output_png) if dual_composite_reliability_output_png else None
            ),
            "dual_composite_metadata_json": str(dual_metadata_json) if dual_metadata_json else None,
            "dual_composite_diagnostics_json": str(dual_diagnostics_json) if dual_diagnostics_json else None,
            "comet_sigma_fits": str(dual_comet_sigma_output_fits) if dual_comet_sigma_output_fits else None,
            "comet_sigma_preview_png": str(dual_comet_sigma_output_png) if dual_comet_sigma_output_png else None,
            "comet_directional_fits": (
                str(dual_comet_directional_output_fits) if dual_comet_directional_output_fits else None
            ),
            "comet_directional_preview_png": (
                str(dual_comet_directional_output_png) if dual_comet_directional_output_png else None
            ),
            "comet_directional_difference_fits": (
                str(dual_comet_directional_difference_output_fits)
                if dual_comet_directional_difference_output_fits
                else None
            ),
            "comet_directional_difference_preview_png": (
                str(dual_comet_directional_difference_output_png)
                if dual_comet_directional_difference_output_png
                else None
            ),
            "stars_subtractive_fits": str(subtractive_star_output_fits) if subtractive_star_output_fits else None,
            "cometless_star_preview_png": (
                str(subtractive_cometless_output_png) if subtractive_cometless_output_png else None
            ),
            "subtractive_residual_preview_png": (
                str(subtractive_residual_output_png) if subtractive_residual_output_png else None
            ),
            "stars_fits": str(dual_star_output_fits) if dual_star_output_fits else None,
            "stars_preview_png": str(dual_star_output_png) if dual_star_output_png else None,
            "comet_clean_fits": str(dual_comet_output_fits) if dual_comet_output_fits else None,
            "comet_preview_png": str(dual_comet_output_png) if dual_comet_output_png else None,
            "composite_fits": str(dual_composite_output_fits) if dual_composite_output_fits else None,
            "composite_preview_png": str(dual_composite_output_png) if dual_composite_output_png else None,
            "composite_mask_png": str(dual_mask_output_png) if dual_mask_output_png else None,
            "stars_contribution_count_png": (
                str(dual_star_count_output_png) if dual_star_count_output_png else None
            ),
            "comparison_stars_png": (
                str(dual_comparison_stars_output_png) if dual_comparison_stars_output_png else None
            ),
            "comparison_comet_png": (
                str(dual_comparison_comet_output_png) if dual_comparison_comet_output_png else None
            ),
            "comparison_composite_png": (
                str(dual_comparison_composite_output_png) if dual_comparison_composite_output_png else None
            ),
            "composite_weight_png": (
                str(dual_composite_weight_output_png) if dual_composite_weight_output_png else None
            ),
            "composite_reliability_png": (
                str(dual_composite_reliability_output_png) if dual_composite_reliability_output_png else None
            ),
            "composite_metadata_json": str(dual_metadata_json) if dual_metadata_json else None,
            "composite_diagnostics_json": str(dual_diagnostics_json) if dual_diagnostics_json else None,
            "comet_sigma_fits": str(dual_comet_sigma_output_fits) if dual_comet_sigma_output_fits else None,
            "comet_sigma_preview_png": str(dual_comet_sigma_output_png) if dual_comet_sigma_output_png else None,
            "comet_directional_fits": (
                str(dual_comet_directional_output_fits) if dual_comet_directional_output_fits else None
            ),
            "comet_directional_preview_png": (
                str(dual_comet_directional_output_png) if dual_comet_directional_output_png else None
            ),
            "comet_directional_difference_fits": (
                str(dual_comet_directional_difference_output_fits)
                if dual_comet_directional_difference_output_fits
                else None
            ),
            "comet_directional_difference_preview_png": (
                str(dual_comet_directional_difference_output_png)
                if dual_comet_directional_difference_output_png
                else None
            ),
            "shifts_csv": str(shifts_csv),
            "registration_diagnostics_csv": str(registration_diagnostics_csv),
        },
    }
    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    console_summary = dict(summary)
    console_summary["removed_intermediate_images"] = []
    print(json.dumps(console_summary, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("ERROR: Processing cancelled.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as error:
        message = " ".join(str(error).splitlines()) or error.__class__.__name__
        print(f"ERROR: {message}", file=sys.stderr)
        raise SystemExit(1) from None
