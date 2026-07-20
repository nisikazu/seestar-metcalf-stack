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
    star_pairs: int | None = None
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


def write_fits_float32(path: Path, data: np.ndarray, source_header: dict[str, object], extra: dict[str, object]) -> None:
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
    cards.append("HISTORY Moving-target stack generated by scripts/moving_target_stack.py".ljust(80))
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
    cards.append("HISTORY Moving-target stack generated by scripts/moving_target_stack.py".ljust(80))
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


def shift_image(data: np.ndarray, dx: float, dy: float) -> tuple[np.ndarray, np.ndarray]:
    if data.ndim == 2:
        shifted, mask = shift_plane(data, dx, dy)
        return shifted, mask
    planes = []
    common_mask = None
    for plane in data:
        shifted, mask = shift_plane(plane, dx, dy)
        planes.append(shifted)
        common_mask = mask if common_mask is None else (common_mask & mask)
    return np.stack(planes, axis=0), common_mask


def shift_plane(data: np.ndarray, dx: float, dy: float) -> tuple[np.ndarray, np.ndarray]:
    height, width = data.shape
    if abs(dx) < 1.0e-9 and abs(dy) < 1.0e-9:
        return data.astype(np.float64, copy=True), np.ones((height, width), dtype=bool)
    yy, xx = np.indices((height, width), dtype=np.float32)
    src_x = xx - np.float32(dx)
    src_y = yy - np.float32(dy)
    x0 = np.floor(src_x).astype(np.int32)
    y0 = np.floor(src_y).astype(np.int32)
    x1 = x0 + 1
    y1 = y0 + 1
    valid = (x0 >= 0) & (y0 >= 0) & (x1 < width) & (y1 < height)

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
        count_image = np.zeros(count_shape, dtype=np.uint16)
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


class MedianAccumulator:
    """Disk-backed per-pixel median accumulator for large Seestar sequences."""

    def __init__(self, path: Path, capacity: int, image_shape: tuple[int, ...]):
        self.path = path
        self.capacity = capacity
        self.image_shape = image_shape
        self.count = 0
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
        # Exact-zero samples are registration/shift padding for order-statistic
        # stacks. Treat them as missing for both median and rank-fit methods.
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
            lo, hi = np.percentile(finite, [low_percentile, high_percentile])
            if hi <= lo:
                hi = lo + 1.0
            scaled = np.clip((plane - lo) / (hi - lo), 0.0, 1.0)
            scaled = (scaled * 255.0 + 0.5).astype(np.uint8)
        stretched.append(scaled)
    if len(stretched) == 1:
        image = Image.fromarray(stretched[0], mode="L")
    else:
        while len(stretched) < 3:
            stretched.append(stretched[-1])
        image = Image.fromarray(np.stack(stretched[:3], axis=2), mode="RGB")
    image.save(path)


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


def select_reference_index(files: list[Path], mode: str) -> int:
    if not files:
        raise ValueError("Cannot select a reference from an empty file list")
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
                star_pairs = int(float(parts[h_index - 1])) if h_index >= 1 else None
            except ValueError:
                continue
            reg = registrations.setdefault(index, SirilRegistration(index=index))
            reg.reference_index = reference_index
            reg.star_pairs = star_pairs
            reg.matrix = matrix  # type: ignore[assignment]
    return registrations


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


def run_siril(siril_cmd: Path, work_dir: Path, script_path: Path, verbose: bool = False) -> None:
    if not siril_cmd.exists():
        raise FileNotFoundError(f"Siril wrapper not found: {siril_cmd}")
    cmd = ["cmd.exe", "/c", str(siril_cmd), "-d", str(work_dir), "-s", str(script_path)]
    if verbose:
        process = subprocess.Popen(
            cmd,
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
            raise RuntimeError(f"Siril registration failed: {failure}")
        if returncode != 0:
            raise subprocess.CalledProcessError(returncode, cmd, output=output)
        return

    completed = subprocess.run(
        cmd,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    write_console_safe(completed.stdout)
    combined_output = completed.stdout + "\n" + completed.stderr
    failure = siril_failure_reason(combined_output)
    if failure:
        write_console_safe(completed.stderr, sys.stderr)
        raise RuntimeError(f"Siril registration failed: {failure}")
    if completed.returncode != 0:
        write_console_safe(completed.stderr, sys.stderr)
        raise subprocess.CalledProcessError(
            completed.returncode,
            cmd,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    for line in completed.stderr.splitlines():
        if "pyproject.toml" in line and "Failed to install Python module" in line:
            continue
        if "Reading sequence failed" in line and "frame.seq" in line:
            continue
        write_console_safe(line + "\n", sys.stderr)


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
    fits_files = [path for path in files if path.suffix.lower() in {".fit", ".fits"}]
    return bool(fits_files) and all(path.name.lower().startswith("stacked_") for path in fits_files)


def resolve_source_dir(source_dir: Path, pattern: str) -> Path:
    source_dir = repair_windows_cmd_path(source_dir)
    files = sorted(source_dir.glob(pattern), key=lambda p: p.name) if source_dir.exists() else []
    sub_candidate = source_dir.with_name(f"{source_dir.name}_sub")
    sub_files = sorted(sub_candidate.glob(pattern), key=lambda p: p.name) if sub_candidate.exists() else []
    if sub_files and (not files or looks_like_stacked_outputs(files)):
        print(f"Using subframe directory: {sub_candidate}", file=sys.stderr)
        return sub_candidate
    return source_dir


def is_failed_frame(path: Path) -> bool:
    return "_failed_" in path.name.lower()


def choose_files(source_dir: Path, pattern: str, count: int | None, include_failed_frames: bool = False) -> list[Path]:
    source_dir = resolve_source_dir(source_dir, pattern)
    files = sorted(source_dir.glob(pattern), key=lambda p: p.name)
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
    parser.add_argument("--pattern", default="*.fit")
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
    parser.add_argument("--siril", type=Path, default=REPO_ROOT / "siril-cli.cmd")
    parser.add_argument("--basename", default="frame")
    parser.add_argument("--registration-transform", default="similarity")
    parser.add_argument("--registration-minpairs", type=int, default=6)
    parser.add_argument(
        "--stack-method",
        choices=("mean", "median", "rankfit"),
        default="mean",
        help="Per-pixel combination method. median and rankfit exclude exact-zero samples. Defaults to mean.",
    )
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
        "--no-cleanup",
        action="store_true",
        help="Keep intermediate image FITS files generated for Siril registration.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Show registration and per-frame stack progress.")
    args = parser.parse_args()

    if not 1 <= args.rankfit_fraction <= 100:
        parser.error("--rankfit-fraction must be an integer from 1 to 100")

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
    reference_index = select_reference_index(files, args.reference_frame)
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
    try:
        if args.verbose:
            print(f"[registration] Siril background-star registration: {len(copied)} frames", flush=True)
        run_siril(args.siril, registration_dir, siril_script, args.verbose)
        registered_count = sum(
            (registration_dir / f"r_{args.basename}_{i:05d}.fit").exists()
            for i in range(1, len(copied) + 1)
        )
        if registered_count == 0:
            raise RuntimeError("Siril registration produced no registered FITS frames")
        if args.verbose:
            print(f"[registration] Registered {registered_count}/{len(copied)} frames", flush=True)
    except Exception:
        if not args.no_cleanup:
            removed = cleanup_intermediate_images(registration_dir, args.basename, copied, len(copied))
            print(f"[cleanup] Removed {len(removed)} intermediate FITS files after registration failure", flush=True)
        raise
    registration_seq = registration_dir / f"{args.basename}_.seq"
    star_registrations = parse_siril_registration(registration_seq)

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

    sum_image: np.ndarray | None = None
    count_image: np.ndarray | None = None
    star_sum_image: np.ndarray | None = None
    star_count_image: np.ndarray | None = None
    median_stack: MedianAccumulator | None = None
    median_star_stack: MedianAccumulator | None = None
    frame_rows: list[dict[str, object]] = []
    used_times: list[datetime] = []
    used = 0

    for i, source in enumerate(copied, start=1):
        if args.verbose:
            print(
                f"[stack:{args.stack_method}] frame {i}/{len(copied)}: {files[i - 1].name}",
                flush=True,
            )
        registered = registration_dir / f"r_{args.basename}_{i:05d}.fit"
        star_reg = star_registrations.get(i, SirilRegistration(index=i))
        if not registered.exists():
            frame_rows.append(
                {
                    "index": i,
                    "source": files[i - 1].name,
                    "used": False,
                    "reason": "no registered frame",
                    "star_selected": star_reg.selected,
                    "star_reference_index": star_reg.reference_index,
                    "star_pairs": star_reg.star_pairs,
                    "star_tx_px": star_reg.star_tx_px,
                    "star_ty_px": star_reg.star_ty_px,
                    "star_rotation_deg": star_reg.star_rotation_deg,
                    "star_scale": star_reg.star_scale,
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
        shifted, mask2d = shift_image(image.data, dx, dy)
        star_shifted, star_mask2d = shift_image(image.data, 0.0, 0.0)
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
                )
                median_star_stack = MedianAccumulator(
                    work_dir / f"{args.stack_method}_star_frames.npy",
                    len(files),
                    star_shifted.shape,
                )
            median_stack.add(shifted, mask2d)
            if median_star_stack is None:
                raise RuntimeError("Star median accumulator was not initialized")
            median_star_stack.add(star_shifted, star_mask2d)
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
                "star_selected": star_reg.selected,
                "star_reference_index": star_reg.reference_index,
                "star_pairs": star_reg.star_pairs,
                "star_tx_px": star_reg.star_tx_px,
                "star_ty_px": star_reg.star_ty_px,
                "star_rotation_deg": star_reg.star_rotation_deg,
                "star_scale": star_reg.star_scale,
                "registered_unit_scale": registered_unit_scale,
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
    shifts_csv = work_dir / f"{output_stem}_shifts.csv"
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
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": args.reference_frame,
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
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": args.reference_frame,
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
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": args.reference_frame,
        "REFINDEX": reference_index,
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
            "star_selected",
            "star_reference_index",
            "star_pairs",
            "star_tx_px",
            "star_ty_px",
            "star_rotation_deg",
            "star_scale",
            "registered_unit_scale",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(frame_rows)

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
        "cleanup_intermediate_images": not args.no_cleanup,
        "removed_intermediate_images": removed_intermediate_images,
        "removed_intermediate_image_count": len(removed_intermediate_images),
        "removed_median_temporary_files": median_temp_removed,
        "include_failed_frames": args.include_failed_frames,
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
        "rankfit_fraction_percent": args.rankfit_fraction if args.stack_method == "rankfit" else None,
        "rankfit_polynomial_degree": 5 if args.stack_method == "rankfit" else None,
        "reference_frame_mode": args.reference_frame,
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
            "star_fits": str(star_output_fits),
            "star_preview_png": str(star_output_png),
            "comparison_fits": str(comparison_output_fits),
            "comparison_preview_png": str(comparison_output_png),
            "shifts_csv": str(shifts_csv),
        },
    }
    summary_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    console_summary = dict(summary)
    console_summary["removed_intermediate_images"] = []
    print(json.dumps(console_summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
