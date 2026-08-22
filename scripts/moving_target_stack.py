#!/usr/bin/env python
"""Moving-target stack for Seestar or SharpCap subframes.

Pipeline:
1. Copy a clean subset of source FITS files into a work directory.
2. Use Siril CLI to debayer and register frames on background stars.
3. Use a first-frame WCS and a target ephemeris CSV to compute the target
   pixel in the registered first-frame coordinate system for every frame.
4. Shift each registered frame so the target lands on the selected reference
   pixel, then mean- or median-stack the shifted frames.

SharpCap Live Stack offsets can replace Siril registration when complete.
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

from fits_preview import annotate_preview_png, export_preview_png, rotate_preview_png, write_annotation_overlay_png
from sharpcap_stacklog import load_manifest
from siril_preprocessing import PreprocessingPlan, build_sequence_preprocess_script, stage_preprocessing_files
from sun_pa import fetch_sun_position, observer_center_from_ephemeris_csv, sun_pa_fits_header


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


def wcs_cd_matrix(header: dict[str, object]) -> tuple[float, float, float, float]:
    try:
        return tuple(float(header[key]) for key in ("CD1_1", "CD1_2", "CD2_1", "CD2_2"))  # type: ignore[return-value]
    except (KeyError, TypeError, ValueError):
        pass
    try:
        cdelt1 = float(header["CDELT1"])
        cdelt2 = float(header["CDELT2"])
        pc11 = float(header.get("PC1_1", 1.0))
        pc12 = float(header.get("PC1_2", 0.0))
        pc21 = float(header.get("PC2_1", 0.0))
        pc22 = float(header.get("PC2_2", 1.0))
        return pc11 * cdelt1, pc12 * cdelt1, pc21 * cdelt2, pc22 * cdelt2
    except (KeyError, TypeError, ValueError):
        pass
    try:
        cdelt1 = float(header["CDELT1"])
        cdelt2 = float(header["CDELT2"])
        angle = math.radians(float(header.get("CROTA2", header.get("CROTA1", 0.0))))
        return (
            cdelt1 * math.cos(angle),
            -cdelt2 * math.sin(angle),
            cdelt1 * math.sin(angle),
            cdelt2 * math.cos(angle),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("WCS has neither a CD matrix nor usable PC/CDELT terms") from error


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

    def cd_matrix(self) -> tuple[float, float, float, float]:
        """Return the sky-per-pixel matrix used by this solution."""
        if self.header:
            return wcs_cd_matrix(self.header)
        c = self.calibration or {}
        pixscale_deg = float(c["pixscale"]) / 3600.0
        theta = math.radians(float(c["orientation"]))
        return (
            pixscale_deg * math.cos(theta),
            pixscale_deg * math.sin(theta),
            -pixscale_deg * math.sin(theta),
            pixscale_deg * math.cos(theta),
        )

    def _world_to_pixel_cd(self, ra_deg: float, dec_deg: float) -> tuple[float, float]:
        h = self.header or {}
        ra0 = float(h["CRVAL1"])
        dec0 = float(h["CRVAL2"])
        crpix1 = float(h["CRPIX1"])
        crpix2 = float(h["CRPIX2"])
        cd11, cd12, cd21, cd22 = wcs_cd_matrix(h)

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
                "PC1_1",
                "PC1_2",
                "PC2_1",
                "PC2_2",
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


def read_raster(path: Path) -> FitsImage:
    with Image.open(path) as image:
        array = np.asarray(image)
    original_dtype = array.dtype
    if array.ndim == 3:
        if array.shape[2] < 3:
            array = array[:, :, 0]
        else:
            array = np.moveaxis(array[:, :, :3], 2, 0)
    header: dict[str, object] = {"NAXIS1": int(array.shape[-1]), "NAXIS2": int(array.shape[-2])}
    if np.issubdtype(original_dtype, np.integer):
        header["SATURATE"] = int(np.iinfo(original_dtype).max)
    return FitsImage(header=header, cards=[], data=array.astype(np.float32))


def debayer_bilinear(data: np.ndarray, pattern: str) -> np.ndarray:
    """Convert a 2D Bayer mosaic to CHW RGB while preserving known samples."""
    if data.ndim != 2:
        return data
    pattern = pattern.strip().upper()
    if pattern not in {"RGGB", "BGGR", "GRBG", "GBRG"}:
        raise ValueError(f"Unsupported Bayer pattern: {pattern}")
    height, width = data.shape
    colors = np.asarray(list(pattern)).reshape(2, 2)
    kernel = np.asarray([[1.0, 2.0, 1.0], [2.0, 4.0, 2.0], [1.0, 2.0, 1.0]], dtype=np.float64)
    planes: list[np.ndarray] = []
    for color in "RGB":
        mask = np.zeros((height, width), dtype=np.float64)
        for y in range(2):
            for x in range(2):
                if colors[y, x] == color:
                    mask[y::2, x::2] = 1.0
        padded_data = np.pad(data.astype(np.float64) * mask, 1, mode="edge")
        padded_mask = np.pad(mask, 1, mode="edge")
        numerator = np.zeros((height, width), dtype=np.float64)
        denominator = np.zeros((height, width), dtype=np.float64)
        for ky in range(3):
            for kx in range(3):
                weight = kernel[ky, kx]
                numerator += padded_data[ky : ky + height, kx : kx + width] * weight
                denominator += padded_mask[ky : ky + height, kx : kx + width] * weight
        plane = np.divide(numerator, denominator, out=np.zeros_like(numerator), where=denominator > 0)
        plane[mask.astype(bool)] = data[mask.astype(bool)]
        planes.append(plane.astype(np.float32))
    return np.stack(planes, axis=0)


def read_source_image(path: Path, bayer_pattern: str | None = None, *, debayer: bool = True) -> FitsImage:
    image = read_fits(path) if path.suffix.lower() in {".fit", ".fits"} else read_raster(path)
    pattern = bayer_pattern or str(image.header.get("BAYERPAT") or image.header.get("COLORTYP") or "").strip()
    if image.data.ndim == 2 and pattern:
        image.header["BAYERPAT"] = pattern.strip().upper()
    if debayer and image.data.ndim == 2 and pattern:
        image = FitsImage(header=image.header, cards=image.cards, data=debayer_bilinear(image.data, pattern))
    return image


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


def update_fits_header_cards(path: Path, updates: dict[str, tuple[object, str]]) -> None:
    """Update primary-HDU keywords in place without moving the FITS data."""
    _header, cards, data_offset = read_fits_header(path)
    replacement_keys = {key.upper() for key in updates}
    end_index = next((index for index, card in enumerate(cards) if card[:8].strip() == "END"), None)
    if end_index is None:
        raise ValueError(f"FITS END card not found in {path}")
    kept = [card for card in cards[:end_index] if card[:8].strip().upper() not in replacement_keys]
    kept.extend(format_card(key, value, comment) for key, (value, comment) in updates.items())
    kept.append("END".ljust(80))
    header_capacity = data_offset // 80
    if len(kept) > header_capacity:
        raise ValueError(f"FITS header has no room for {len(updates)} additional cards: {path}")
    encoded = "".join(kept).encode("ascii", errors="replace").ljust(data_offset, b" ")
    with path.open("r+b") as handle:
        handle.write(encoded)


def sun_pa_header_comments(values: dict[str, object]) -> dict[str, tuple[object, str]]:
    return {
        "SUN_PA": (values["SUN_PA"], "deg; Sun PA from north through east"),
        "ASUN_PA": (values["ASUN_PA"], "deg; anti-solar PA from north through east"),
        "SUNRA": (values["SUNRA"], "deg; Horizons solar RA at DATE-OBS"),
        "SUNDEC": (values["SUNDEC"], "deg; Horizons solar Dec at DATE-OBS"),
        "SUNCENTR": (values["SUNCENTR"], "Horizons observer center"),
        "SUNSRC": (values["SUNSRC"], "solar ephemeris source"),
    }


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
    for key in [
        "OBJECT",
        "DATE-OBS",
        "FILTER",
        "GAIN",
        "EXPOSURE",
        "EXPTIME",
        "BAYERPAT",
        "COLORTYP",
        "RA",
        "DEC",
        "OBJCTRA",
        "OBJCTDEC",
        "FOCALLEN",
        "XPIXSZ",
        "YPIXSZ",
        "XBINNING",
        "YBINNING",
        "CCDXBIN",
        "CCDYBIN",
        "SITELONG",
        "SITELAT",
    ]:
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


def write_registered_float(
    path: Path,
    image: FitsImage,
    date_obs: datetime,
    object_name: str,
    exposure_seconds: float | None = None,
) -> None:
    header = dict(image.header)
    header.setdefault("OBJECT", object_name)
    header["DATE-OBS"] = date_obs.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if exposure_seconds is not None:
        header["EXPOSURE"] = exposure_seconds
    saturation = fits_saturation_level(header)
    if saturation is not None:
        header["SATURATE"] = saturation
    extra = {key: header[key] for key in ("SATURATE", "SATLEVEL") if key in header}
    write_fits_float32(path, image.data.astype(np.float32), header, extra)


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


def registered_valid_mask(data: np.ndarray) -> np.ndarray:
    """Identify finite pixels that are not all-channel registration padding."""
    if data.ndim == 3:
        return np.all(np.isfinite(data), axis=0) & np.any(data != 0.0, axis=0)
    return np.isfinite(data) & (data != 0.0)


BACKGROUND_ROI_FRACTION = 0.70
BACKGROUND_SAMPLE_STEP = 8
BACKGROUND_SIGMA = 3.0
BACKGROUND_ITERATIONS = 3
BACKGROUND_TILE_ROWS = 50
BACKGROUND_TILE_COLUMNS = 50
BACKGROUND_MIN_TILE_PIXELS = 96
BACKGROUND_TILE_SAMPLE_STEP = 4
BACKGROUND_MIN_TILE_SAMPLES = 12
BACKGROUND_FIT_OUTLIER_SIGMA = 3.0


@dataclass
class BackgroundModel:
    """A per-channel background model in normalized image coordinates."""

    mode: str
    coefficients: np.ndarray
    tile_count: int
    rejected_tile_counts: np.ndarray
    residual_rms: np.ndarray

    @property
    def levels(self) -> np.ndarray:
        """Model level at the image centre, where normalized x=y=0."""
        return self.coefficients[:, 0]


def sigma_clipped_median(values: np.ndarray, sigma: float = BACKGROUND_SIGMA, iterations: int = BACKGROUND_ITERATIONS) -> float:
    """Return a robust background level without letting stars set the DC level."""
    samples = np.asarray(values, dtype=np.float64)
    samples = samples[np.isfinite(samples)]
    if samples.size == 0:
        raise ValueError("No finite pixels were available for background estimation")
    for _ in range(iterations):
        center = float(np.median(samples))
        mad = float(np.median(np.abs(samples - center)))
        if not math.isfinite(mad) or mad <= 0.0:
            return center
        robust_sigma = 1.4826 * mad
        kept = samples[np.abs(samples - center) <= sigma * robust_sigma]
        if kept.size == 0 or kept.size == samples.size:
            return center
        samples = kept
    return float(np.median(samples))


def estimate_background_levels(data: np.ndarray, valid_mask: np.ndarray) -> np.ndarray:
    """Estimate one DC level per channel from a sampled central valid region."""
    if valid_mask.shape != data.shape[-2:]:
        raise ValueError(f"Background validity mask shape changed: {valid_mask.shape} != {data.shape[-2:]}")
    height, width = valid_mask.shape
    roi_height = max(1, int(round(height * BACKGROUND_ROI_FRACTION)))
    roi_width = max(1, int(round(width * BACKGROUND_ROI_FRACTION)))
    y0 = (height - roi_height) // 2
    x0 = (width - roi_width) // 2
    region = np.s_[y0 : y0 + roi_height : BACKGROUND_SAMPLE_STEP, x0 : x0 + roi_width : BACKGROUND_SAMPLE_STEP]
    sampled_valid = valid_mask[region]
    if int(np.count_nonzero(sampled_valid)) < 128:
        raise ValueError("Fewer than 128 valid sampled pixels were available for background estimation")
    planes = data[np.newaxis, :, :] if data.ndim == 2 else data
    levels: list[float] = []
    for plane in planes:
        values = plane[region][sampled_valid]
        if int(np.count_nonzero(np.isfinite(values))) < 128:
            raise ValueError("A channel has too few finite pixels for background estimation")
        levels.append(sigma_clipped_median(values))
    return np.asarray(levels, dtype=np.float64)


def background_term_count(mode: str) -> int:
    if mode == "offset":
        return 1
    if mode == "plane":
        return 3
    if mode == "quadratic":
        return 6
    raise ValueError(f"Unsupported background normalization mode: {mode}")


def background_design_matrix(x: np.ndarray, y: np.ndarray, mode: str) -> np.ndarray:
    """Return [1], [1,x,y], or [1,x,y,x2,xy,y2] in stable normalized coordinates."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if mode == "offset":
        return np.ones((x.size, 1), dtype=np.float64)
    if mode == "plane":
        return np.column_stack((np.ones_like(x), x, y))
    if mode == "quadratic":
        return np.column_stack((np.ones_like(x), x, y, x * x, x * y, y * y))
    raise ValueError(f"Unsupported background normalization mode: {mode}")


def background_grid(height: int, width: int, mode: str) -> np.ndarray:
    """Return a term-by-y-by-x array for evaluating a background model."""
    x = np.linspace(-1.0, 1.0, width, dtype=np.float64)
    y = np.linspace(-1.0, 1.0, height, dtype=np.float64)
    xx, yy = np.meshgrid(x, y)
    if mode == "offset":
        return np.ones((1, height, width), dtype=np.float64)
    if mode == "plane":
        return np.stack((np.ones_like(xx), xx, yy))
    if mode == "quadratic":
        return np.stack((np.ones_like(xx), xx, yy, xx * xx, xx * yy, yy * yy))
    raise ValueError(f"Unsupported background normalization mode: {mode}")


def background_tile_samples(data: np.ndarray, valid_mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return robust RGB background values sampled from a deterministic 50x50 grid."""
    if valid_mask.shape != data.shape[-2:]:
        raise ValueError(f"Background validity mask shape changed: {valid_mask.shape} != {data.shape[-2:]}")
    planes = data[np.newaxis, :, :] if data.ndim == 2 else data
    height, width = valid_mask.shape
    tile_height = math.ceil(height / BACKGROUND_TILE_ROWS)
    tile_width = math.ceil(width / BACKGROUND_TILE_COLUMNS)
    padded_height = tile_height * BACKGROUND_TILE_ROWS
    padded_width = tile_width * BACKGROUND_TILE_COLUMNS

    # Arrange all tiles as rows. NumPy then computes every robust tile median
    # in compiled code rather than making thousands of Python calls per frame.
    padded_valid = np.zeros((padded_height, padded_width), dtype=bool)
    padded_valid[:height, :width] = valid_mask
    tile_valid = padded_valid.reshape(BACKGROUND_TILE_ROWS, tile_height, BACKGROUND_TILE_COLUMNS, tile_width)
    tile_valid = tile_valid.transpose(0, 2, 1, 3).reshape(-1, tile_height * tile_width)
    weights = np.count_nonzero(tile_valid, axis=1).astype(np.float64)
    sampled_tile_height = math.ceil(tile_height / BACKGROUND_TILE_SAMPLE_STEP)
    sampled_tile_width = math.ceil(tile_width / BACKGROUND_TILE_SAMPLE_STEP)
    sampled_valid_grid = padded_valid.reshape(BACKGROUND_TILE_ROWS, tile_height, BACKGROUND_TILE_COLUMNS, tile_width)
    sampled_valid_grid = sampled_valid_grid[:, ::BACKGROUND_TILE_SAMPLE_STEP, :, ::BACKGROUND_TILE_SAMPLE_STEP]
    sampled_valid = sampled_valid_grid.transpose(0, 2, 1, 3).reshape(
        -1, sampled_tile_height * sampled_tile_width
    )
    sampled_weights = np.count_nonzero(sampled_valid, axis=1)
    x_coordinates = np.broadcast_to(np.arange(padded_width, dtype=np.float64), (padded_height, padded_width))
    y_coordinates = np.broadcast_to(np.arange(padded_height, dtype=np.float64)[:, np.newaxis], (padded_height, padded_width))

    def sampled_coordinates(coordinates: np.ndarray) -> np.ndarray:
        grid = coordinates.reshape(BACKGROUND_TILE_ROWS, tile_height, BACKGROUND_TILE_COLUMNS, tile_width)
        grid = grid[:, ::BACKGROUND_TILE_SAMPLE_STEP, :, ::BACKGROUND_TILE_SAMPLE_STEP]
        return grid.transpose(0, 2, 1, 3).reshape(-1, sampled_tile_height * sampled_tile_width)

    sampled_x = sampled_coordinates(x_coordinates)
    sampled_y = sampled_coordinates(y_coordinates)
    values_by_channel: list[np.ndarray] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        for plane in planes:
            padded = np.full((padded_height, padded_width), np.nan, dtype=np.float64)
            padded[:height, :width] = np.where(valid_mask, plane, np.nan)
            tiles = padded.reshape(BACKGROUND_TILE_ROWS, tile_height, BACKGROUND_TILE_COLUMNS, tile_width)
            tiles = tiles[:, ::BACKGROUND_TILE_SAMPLE_STEP, :, ::BACKGROUND_TILE_SAMPLE_STEP]
            tiles = tiles.transpose(0, 2, 1, 3).reshape(-1, sampled_tile_height * sampled_tile_width)
            initial = np.nanmedian(tiles, axis=1)
            mad = np.nanmedian(np.abs(tiles - initial[:, np.newaxis]), axis=1)
            robust_sigma = 1.4826 * mad
            keep = np.isfinite(tiles)
            clipping_tiles = robust_sigma > 0.0
            if np.any(clipping_tiles):
                keep[clipping_tiles] &= (
                    np.abs(tiles[clipping_tiles] - initial[clipping_tiles, np.newaxis])
                    <= BACKGROUND_SIGMA * robust_sigma[clipping_tiles, np.newaxis]
                )
            values_by_channel.append(np.nanmedian(np.where(keep, tiles, np.nan), axis=1))

    samples = np.column_stack(values_by_channel)
    center_x = np.sum(np.where(sampled_valid, sampled_x, 0.0), axis=1) / np.maximum(sampled_weights, 1)
    center_y = np.sum(np.where(sampled_valid, sampled_y, 0.0), axis=1) / np.maximum(sampled_weights, 1)
    positions_x = 2.0 * (center_x / max(width - 1.0, 1.0)) - 1.0
    positions_y = 2.0 * (center_y / max(height - 1.0, 1.0)) - 1.0
    usable = (
        (weights >= BACKGROUND_MIN_TILE_PIXELS)
        & (sampled_weights >= BACKGROUND_MIN_TILE_SAMPLES)
        & np.all(np.isfinite(samples), axis=1)
    )
    if not np.any(usable):
        raise ValueError("No valid tiles were available for background surface fitting")
    return positions_x[usable], positions_y[usable], weights[usable], samples[usable]


def fit_background_surface(data: np.ndarray, valid_mask: np.ndarray, mode: str) -> BackgroundModel:
    """Fit a deterministic robust plane or quadratic surface to tile medians."""
    if mode == "offset":
        levels = estimate_background_levels(data, valid_mask)
        return BackgroundModel(
            mode=mode,
            coefficients=levels[:, np.newaxis],
            tile_count=0,
            rejected_tile_counts=np.zeros(levels.size, dtype=np.int32),
            residual_rms=np.zeros(levels.size, dtype=np.float64),
        )
    x, y, weights, samples = background_tile_samples(data, valid_mask)
    design = background_design_matrix(x, y, mode)
    term_count = design.shape[1]
    minimum_tiles = max(term_count + 3, term_count * 2)
    if samples.shape[0] < minimum_tiles:
        raise ValueError(
            f"Only {samples.shape[0]} background tiles were usable; {minimum_tiles} are required for {mode} fitting"
        )
    sqrt_weights = np.sqrt(weights)
    weighted_design = design * sqrt_weights[:, np.newaxis]
    coefficients: list[np.ndarray] = []
    rejected_counts: list[int] = []
    residual_rms: list[float] = []
    for channel in range(samples.shape[1]):
        values = samples[:, channel]
        coefficient, *_ = np.linalg.lstsq(weighted_design, values * sqrt_weights, rcond=None)
        residual = values - design @ coefficient
        residual_centre = float(np.median(residual))
        residual_mad = float(np.median(np.abs(residual - residual_centre)))
        keep = np.ones(residual.size, dtype=bool)
        if math.isfinite(residual_mad) and residual_mad > 0.0:
            robust_sigma = 1.4826 * residual_mad
            keep = np.abs(residual - residual_centre) <= BACKGROUND_FIT_OUTLIER_SIGMA * robust_sigma
        if int(np.count_nonzero(keep)) >= minimum_tiles:
            kept_weights = sqrt_weights[keep]
            coefficient, *_ = np.linalg.lstsq(
                design[keep] * kept_weights[:, np.newaxis],
                values[keep] * kept_weights,
                rcond=None,
            )
        else:
            keep[:] = True
        final_residual = values - design @ coefficient
        coefficients.append(coefficient)
        rejected_counts.append(int(np.count_nonzero(~keep)))
        residual_rms.append(float(np.sqrt(np.average(final_residual[keep] ** 2, weights=weights[keep]))))
    return BackgroundModel(
        mode=mode,
        coefficients=np.stack(coefficients),
        tile_count=int(samples.shape[0]),
        rejected_tile_counts=np.asarray(rejected_counts, dtype=np.int32),
        residual_rms=np.asarray(residual_rms, dtype=np.float64),
    )


def apply_background_offset(data: np.ndarray, valid_mask: np.ndarray, offset_levels: np.ndarray) -> np.ndarray:
    """Apply a per-channel DC offset only to real source pixels, never to padding."""
    if valid_mask.shape != data.shape[-2:]:
        raise ValueError(f"Background validity mask shape changed: {valid_mask.shape} != {data.shape[-2:]}")
    planes = data[np.newaxis, :, :] if data.ndim == 2 else data
    offsets = np.asarray(offset_levels, dtype=np.float64).reshape(-1)
    if offsets.size != planes.shape[0]:
        raise ValueError(f"Background offset channel count changed: {offsets.size} != {planes.shape[0]}")
    result = planes.astype(np.float64, copy=True)
    result[:, valid_mask] += offsets[:, np.newaxis]
    return result[0] if data.ndim == 2 else result


def apply_background_model(
    data: np.ndarray,
    valid_mask: np.ndarray,
    coefficient_delta: np.ndarray,
    mode: str,
) -> np.ndarray:
    """Apply an additive background correction to real source pixels only."""
    if mode == "offset":
        return apply_background_offset(data, valid_mask, coefficient_delta[:, 0])
    if valid_mask.shape != data.shape[-2:]:
        raise ValueError(f"Background validity mask shape changed: {valid_mask.shape} != {data.shape[-2:]}")
    planes = data[np.newaxis, :, :] if data.ndim == 2 else data
    coefficients = np.asarray(coefficient_delta, dtype=np.float64)
    if coefficients.shape != (planes.shape[0], background_term_count(mode)):
        raise ValueError(
            f"Background coefficient shape changed: {coefficients.shape} != "
            f"{(planes.shape[0], background_term_count(mode))}"
        )
    correction = np.tensordot(coefficients, background_grid(*valid_mask.shape, mode), axes=(1, 0))
    result = planes.astype(np.float64, copy=True)
    result[:, valid_mask] += correction[:, valid_mask]
    return result[0] if data.ndim == 2 else result


def add_background_output_offset(
    data: np.ndarray,
    coverage_mask: np.ndarray,
    output_levels: np.ndarray,
) -> np.ndarray:
    """Add a constant output-range offset only where the final stack has data."""
    if coverage_mask.shape != data.shape[-2:]:
        raise ValueError(f"Background coverage mask shape changed: {coverage_mask.shape} != {data.shape[-2:]}")
    planes = data[np.newaxis, :, :] if data.ndim == 2 else data
    levels = np.asarray(output_levels, dtype=np.float64).reshape(-1)
    if levels.size != planes.shape[0]:
        raise ValueError(f"Background output channel count changed: {levels.size} != {planes.shape[0]}")
    result = planes.astype(np.float64, copy=True)
    result[:, coverage_mask] += levels[:, np.newaxis]
    return result[0] if data.ndim == 2 else result


def mean_background_dc_levels(models_by_index: dict[int, BackgroundModel]) -> np.ndarray:
    """Return the RGB arithmetic mean of accepted frames' fitted DC backgrounds."""
    if not models_by_index:
        raise ValueError("No background models were supplied")
    return np.mean(
        np.stack([model.coefficients[:, 0] for model in models_by_index.values()]),
        axis=0,
    )


def collect_background_normalization(
    copied: list[Path],
    source_files: list[Path],
    registration_dir: Path,
    processed_basename: str,
    registration_issues: dict[int, list[str]],
    mode: str,
    verbose_mode: bool,
) -> tuple[np.ndarray, dict[int, BackgroundModel], dict[int, np.ndarray]]:
    """Fit and remove each frame's background, retaining a final output range offset."""
    models_by_index: dict[int, BackgroundModel] = {}
    for index, source in enumerate(copied, start=1):
        if registration_issues.get(index):
            continue
        registered = registration_dir / f"r_{processed_basename}_{index:05d}.fit"
        source_header, _cards, _offset = read_fits_header(source)
        image, _registered_unit_scale = restore_registered_units(read_fits(registered), source_header)
        try:
            models_by_index[index] = fit_background_surface(image.data, registered_valid_mask(image.data), mode)
        except ValueError as error:
            raise RuntimeError(
                f"Cannot estimate the background of usable frame {index} ({source_files[index - 1].name}): {error}"
            ) from error
        if verbose_mode:
            model = models_by_index[index]
            rendered = ", ".join(f"{level:.3f}" for level in model.levels)
            details = "" if mode == "offset" else f"; tiles={model.tile_count}; rejected={model.rejected_tile_counts.tolist()}"
            print(f"[background] frame {index}/{len(copied)}: [{rendered}] ADU{details}", flush=True)
    if not models_by_index:
        raise RuntimeError("No registered frames were available for background normalization")
    coefficient_shapes = {model.coefficients.shape for model in models_by_index.values()}
    if len(coefficient_shapes) != 1:
        raise RuntimeError("Registered frames have inconsistent channel counts for background normalization")
    # Arithmetic is performed around a zero background. The arithmetic mean
    # DC level is retained only as a post-stack output-range offset for
    # non-negative formats.
    output_levels = mean_background_dc_levels(models_by_index)
    corrections_by_index = {
        index: -model.coefficients for index, model in models_by_index.items()
    }
    channel_counts = {model.levels.size for model in models_by_index.values()}
    if len(channel_counts) != 1:
        raise RuntimeError("Registered frames have inconsistent channel counts for background normalization")
    if verbose_mode:
        rendered = ", ".join(f"{level:.3f}" for level in output_levels)
        details = "" if mode == "offset" else f"; model={mode}; tiles={BACKGROUND_TILE_ROWS}x{BACKGROUND_TILE_COLUMNS}"
        print(
            f"[background] each frame is fitted to zero; final output offset: [{rendered}] ADU{details}",
            flush=True,
        )
    return output_levels, models_by_index, corrections_by_index


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


def transform_image(
    data: np.ndarray,
    tx: float,
    ty: float,
    rotation_deg: float,
    source_valid: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply a forward center-based rotation and translation using inverse sampling."""
    if abs(rotation_deg) < 1.0e-12:
        return shift_image(data, tx, ty, source_valid)
    planes = data[np.newaxis, :, :] if data.ndim == 2 else data
    _channels, height, width = planes.shape
    if source_valid is not None and source_valid.shape != (height, width):
        raise ValueError(f"Validity mask shape changed: {source_valid.shape} != {(height, width)}")
    yy, xx = np.indices((height, width), dtype=np.float64)
    cx = (width - 1.0) / 2.0
    cy = (height - 1.0) / 2.0
    angle = math.radians(rotation_deg)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    out_x = xx - cx - tx
    out_y = yy - cy - ty
    src_x = cosine * out_x + sine * out_y + cx
    src_y = -sine * out_x + cosine * out_y + cy
    x0 = np.floor(src_x).astype(np.int32)
    y0 = np.floor(src_y).astype(np.int32)
    x1 = x0 + 1
    y1 = y0 + 1
    valid = (x0 >= 0) & (y0 >= 0) & (x1 < width) & (y1 < height)
    if source_valid is not None and np.any(valid):
        positions = np.flatnonzero(valid)
        py = positions // width
        px = positions % width
        kernel_valid = (
            source_valid[y0[py, px], x0[py, px]]
            & source_valid[y0[py, px], x1[py, px]]
            & source_valid[y1[py, px], x0[py, px]]
            & source_valid[y1[py, px], x1[py, px]]
        )
        valid[py, px] = kernel_valid
    output = np.zeros_like(planes, dtype=np.float32)
    if np.any(valid):
        wx = src_x[valid] - x0[valid]
        wy = src_y[valid] - y0[valid]
        for channel, plane in enumerate(planes):
            output[channel][valid] = (
                (1.0 - wx) * (1.0 - wy) * plane[y0[valid], x0[valid]]
                + wx * (1.0 - wy) * plane[y0[valid], x1[valid]]
                + (1.0 - wx) * wy * plane[y1[valid], x0[valid]]
                + wx * wy * plane[y1[valid], x1[valid]]
            )
    return (output[0] if data.ndim == 2 else output), valid


def relative_sharpcap_transform(frame: dict[str, object], reference: dict[str, object]) -> tuple[float, float, float]:
    """Convert StackLog transforms-to-stack-reference into a transform-to-selected-reference."""
    frame_angle = float(frame["rotation_deg"])
    reference_angle = float(reference["rotation_deg"])
    delta_angle = frame_angle - reference_angle
    delta_x = float(frame["offset_x_px"]) - float(reference["offset_x_px"])
    delta_y = float(frame["offset_y_px"]) - float(reference["offset_y_px"])
    inverse_reference = math.radians(-reference_angle)
    tx = math.cos(inverse_reference) * delta_x - math.sin(inverse_reference) * delta_y
    ty = math.sin(inverse_reference) * delta_x + math.cos(inverse_reference) * delta_y
    return tx, ty, delta_angle


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


def north_up_rotation_degrees(wcs: WcsModel) -> float:
    """Return the PIL rotation that puts increasing Declination upward."""
    cd11, cd12, cd21, cd22 = wcs.cd_matrix()
    det = cd11 * cd22 - cd12 * cd21
    if abs(det) < 1e-20:
        raise ValueError("Cannot orient preview: WCS CD matrix is singular")
    # Inverse CD times the sky-north vector (dRA, dDec)=(0, 1). WcsModel
    # returns this in the same native row coordinates used by the stack and
    # by the preview PNG.
    pixel_x = -cd12 / det
    pixel_y = cd11 / det
    current_angle = math.atan2(pixel_y, pixel_x)
    # Pillow's positive ``Image.rotate`` angles are visually counterclockwise.
    # With the display Y axis pointing downward, this *subtracts* the supplied
    # angle from a display-vector angle. Rotate the current north vector onto
    # -90 degrees (up), rather than applying the inverse rotation.
    raw_angle = math.degrees(current_angle + math.pi / 2.0)
    return (raw_angle + 180.0) % 360.0 - 180.0


def position_angle_rotation_degrees(
    wcs: WcsModel,
    reference_dec_deg: float,
    position_angle_deg: float,
    target_display_angle_deg: float,
) -> float:
    """Return the PIL rotation that maps a sky PA to a display direction.

    Position angle is measured from celestial north through east. The WCS CD
    matrix uses RA/Dec coordinate increments, so the RA component is divided
    by cos(Dec) before the vector is transformed back to image coordinates.
    """
    cd11, cd12, cd21, cd22 = wcs.cd_matrix()
    determinant = cd11 * cd22 - cd12 * cd21
    if abs(determinant) < 1e-20:
        raise ValueError("Cannot orient preview: WCS CD matrix is singular")
    cos_dec = math.cos(math.radians(reference_dec_deg))
    if abs(cos_dec) < 1e-12:
        raise ValueError("Cannot orient preview at a celestial pole")
    pa_radians = math.radians(position_angle_deg)
    # Unit local tangent vector expressed as coordinate deltas (dRA, dDec).
    world_ra = math.sin(pa_radians) / cos_dec
    world_dec = math.cos(pa_radians)
    pixel_x = (cd22 * world_ra - cd12 * world_dec) / determinant
    pixel_y = (-cd21 * world_ra + cd11 * world_dec) / determinant
    current_angle = math.atan2(pixel_y, pixel_x)
    # PIL positive rotations subtract from the display-vector angle because
    # the display Y axis points down. Rotate current onto the requested angle.
    rotation = math.degrees(current_angle - target_display_angle_deg)
    return (rotation + 180.0) % 360.0 - 180.0


def sun_pa_left_rotation_degrees(wcs: WcsModel, reference_dec_deg: float, sun_pa_deg: float) -> float:
    """Return the display rotation that places the Sun direction at image left."""
    return position_angle_rotation_degrees(wcs, reference_dec_deg, sun_pa_deg, math.pi)


def rotate_comparison_preview_png(
    left_source: Path,
    right_source: Path,
    destination: Path,
    angle_degrees: float,
) -> None:
    with Image.open(left_source) as left_image, Image.open(right_source) as right_image:
        left = left_image.convert("RGB")
        right = right_image.convert("RGB")
    resample = Image.Resampling.BICUBIC
    left = left.rotate(angle_degrees, resample=resample, expand=True, fillcolor=(0, 0, 0))
    right = right.rotate(angle_degrees, resample=resample, expand=True, fillcolor=(0, 0, 0))
    output = Image.new("RGB", (left.width + right.width, max(left.height, right.height)), (0, 0, 0))
    output.paste(left, (0, 0))
    output.paste(right, (left.width, 0))
    output.save(destination)


def write_siril_script(
    path: Path,
    basename: str,
    transform: str,
    minpairs: int | None,
    reference_index: int,
    debayer: bool = True,
) -> None:
    register = f"register {basename} -prefix=r_ -transf={transform}"
    if minpairs:
        register += f" -minpairs={minpairs}"
    convert = f"convert {basename} -debayer" if debayer else f"convert {basename}"
    path.write_text(
        "\n".join(["requires 1.4.0", convert, f"setref {basename}_ {reference_index}", register, ""]),
        encoding="ascii",
    )


def write_siril_registration_script(
    path: Path,
    sequence_basename: str,
    transform: str,
    minpairs: int | None,
    reference_index: int,
) -> None:
    register = f"register {sequence_basename}_ -prefix=r_ -transf={transform}"
    if minpairs:
        register += f" -minpairs={minpairs}"
    path.write_text(
        "\n".join(["requires 1.4.0", f"setref {sequence_basename}_ {reference_index}", register, ""]),
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


def select_manifest_reference_index(
    files: list[Path], rows: list[dict[str, object]], mode: str, explicit_name: str | None = None
) -> int:
    if explicit_name:
        return select_reference_index(files, "first", explicit_name)
    if mode == "first":
        return 1
    dated = [(parse_time(row["time"]), index) for index, row in enumerate(rows, start=1)]
    midpoint = dated[0][0] + (dated[-1][0] - dated[0][0]) / 2
    return min(dated, key=lambda item: (abs((item[0] - midpoint).total_seconds()), item[0]))[1]


def cleanup_intermediate_images(
    work_dir: Path,
    basename: str,
    copied: list[Path],
    frame_count: int,
    processed_basename: str | None = None,
) -> list[str]:
    candidates = [*copied]
    for i in range(1, frame_count + 1):
        candidates.append(work_dir / f"{basename}_{i:05d}.fit")
        candidates.append(work_dir / f"r_{basename}_{i:05d}.fit")
        if processed_basename and processed_basename != basename:
            candidates.append(work_dir / f"{processed_basename}_{i:05d}.fit")
            candidates.append(work_dir / f"r_{processed_basename}_{i:05d}.fit")
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
    "background_ch1_adu",
    "background_ch2_adu",
    "background_ch3_adu",
    "background_offset_ch1_adu",
    "background_offset_ch2_adu",
    "background_offset_ch3_adu",
    "background_model",
    "background_fit_tiles",
    "background_fit_rejected_ch1",
    "background_fit_rejected_ch2",
    "background_fit_rejected_ch3",
    "background_fit_rms_ch1_adu",
    "background_fit_rms_ch2_adu",
    "background_fit_rms_ch3_adu",
    "background_coefficients",
    "background_correction_coefficients",
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
    work_dir = work_dir.resolve()
    script_path = script_path.resolve()
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
    if not path.is_file() or path.suffix.lower() not in {".fit", ".fits"}:
        return False
    stem = path.stem.casefold()
    return not stem.endswith(("_siril_wcs", "_wcs", "_upload_sanitized"))


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
    parser.add_argument("--frame-manifest", type=Path, help="Normalized SharpCap Live Stack frame manifest")
    parser.add_argument("--preprocessing-plan", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--bayer-pattern",
        choices=("RGGB", "BGGR", "GRBG", "GBRG"),
        help="Bayer pattern for SharpCap RAW PNG/TIFF when metadata is unavailable.",
    )
    parser.add_argument("--ephemeris-csv", required=True, type=Path)
    parser.add_argument(
        "--sun-pa",
        choices=("auto", "off"),
        default="auto",
        help=(
            "Write SUN_PA/ASUN_PA from a Horizons solar query at the reference DATE-OBS when "
            "the ephemeris CSV records its observer center. Defaults to auto."
        ),
    )
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
        "--background-normalization",
        choices=("none", "offset", "plane", "quadratic"),
        default=None,
        help=(
            "Subtract each usable frame's quadratic fitted background by default, or select none, offset, "
            "or plane to change the correction. Legacy padding defaults to none."
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
    parser.add_argument(
        "--preview-north-up",
        action="store_true",
        help="Add preview PNGs rotated using the solved WCS so celestial north is up.",
    )
    parser.add_argument(
        "--preview-sun-pa-left",
        action="store_true",
        help="Add a Metcalf preview PNG rotated using WCS so the SUN_PA direction is left.",
    )
    parser.add_argument(
        "--preview-annotate",
        action="store_true",
        help="Add N/E orientation sticks and a Sun-direction arrow to the selected Metcalf display preview.",
    )
    parser.add_argument(
        "--annotate-at",
        choices=("UL", "UR", "LL", "LR"),
        default="UL",
        help="Corner for --preview-annotate: UL, UR, LL, or LR. Defaults to UL.",
    )
    parser.add_argument(
        "--annotate-size",
        type=float,
        default=60.0,
        help="Annotation radius in pixels for --preview-annotate. Defaults to 60.",
    )
    parser.add_argument("--output-bitpix", choices=("float32", "uint16"), default="float32")
    parser.add_argument("--uint16-scale", choices=("none", "global", "per-channel"), default="none")
    parser.add_argument("--scale-low-percentile", type=float, default=0.0)
    parser.add_argument("--scale-high-percentile", type=float, default=100.0)
    parser.add_argument("--preview-low-percentile", type=float, default=5.0)
    parser.add_argument("--preview-high-percentile", type=float, default=99.95)
    parser.add_argument(
        "--preview-stretch",
        choices=("percentile", "sigma"),
        default="sigma",
        help="Preview display stretch: simple mean/stddev sigma (default) or percentile.",
    )
    parser.add_argument("--preview-sigma-low", type=float, default=-1.0)
    parser.add_argument("--preview-sigma-high", type=float, default=3.0)
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
    if args.background_normalization is None:
        args.background_normalization = "none" if args.padding_policy == "legacy" else "quadratic"
    if args.preview_sigma_high <= args.preview_sigma_low:
        parser.error("--preview-sigma-high must be greater than --preview-sigma-low")
    if args.preview_north_up and args.preview_sun_pa_left:
        parser.error("choose either --preview-north-up or --preview-sun-pa-left, not both")
    if args.annotate_size <= 0.0:
        parser.error("--annotate-size must be positive")
    if args.registration_minpairs < 1:
        parser.error("--registration-minpairs must be at least 1")
    if args.background_normalization != "none" and args.padding_policy != "valid":
        parser.error("--background-normalization offset, plane, and quadratic require --padding-policy valid")
    if not 0.0 < args.saturation_threshold_percent <= 100.0:
        parser.error("--saturation-threshold-percent must be greater than 0 and at most 100")
    try:
        args.saturation_color = normalize_saturation_color(args.saturation_color)
    except ValueError as error:
        parser.error(str(error))

    if not args.wcs_fits and not args.astrometry_json:
        parser.error("--wcs-fits or --astrometry-json is required")

    manifest: dict[str, object] | None = load_manifest(args.frame_manifest) if args.frame_manifest else None
    manifest_rows: list[dict[str, object]] = list(manifest["frames"]) if manifest else []
    if manifest is not None:
        files = [Path(str(row["path"])) for row in manifest_rows]
        args.source_dir = Path(str(manifest["root"]))
    else:
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
        if manifest is not None:
            manifest_rows = manifest_rows[: args.count]
    if not files:
        raise FileNotFoundError("No files remain after time/session filtering")
    reference_index = (
        select_manifest_reference_index(files, manifest_rows, args.reference_frame, args.reference_frame_file)
        if manifest is not None
        else select_reference_index(files, args.reference_frame, args.reference_frame_file)
    )
    reference_mode = "file" if args.reference_frame_file else args.reference_frame
    reference_source = files[reference_index - 1]
    ephemeris = load_ephemeris(args.ephemeris_csv)
    if not args.work_name:
        reference_header = read_source_image(reference_source, args.bayer_pattern).header
        target = safe_name(str(reference_header.get("OBJECT") or (manifest or {}).get("object") or reference_source.parent.name))
        args.work_name = f"{target}_{processing_method_token(args.stack_method, args.rankfit_fraction)}"
    work_dir = prepare_work_dir(args.work_dir, args.work_root, args.work_name)
    registration_dir = work_dir / "registration_images"
    registration_dir.mkdir(parents=True, exist_ok=True)
    use_sharpcap_registration = bool(manifest and manifest.get("alignment_complete"))
    manifest_reference = manifest_rows[reference_index - 1] if use_sharpcap_registration else None
    preprocessing_payload = None
    if args.preprocessing_plan:
        preprocessing_payload = json.loads(args.preprocessing_plan.read_text(encoding="utf-8"))
    elif isinstance(manifest, dict):
        preprocessing_payload = manifest.get("preprocessing")
    preprocessing_plan = PreprocessingPlan.from_dict(preprocessing_payload)

    copied: list[Path] = []
    cfa = False
    try:
        if args.verbose:
            print(f"[prepare] Copying source frames for Siril preprocessing: {len(files)} frames", flush=True)
        for i, source in enumerate(files, start=1):
            if args.verbose:
                print(f"[prepare] frame {i}/{len(files)}: {source.name}", flush=True)
            destination = registration_dir / f"{args.basename}_src_{i:05d}.fit"
            copied.append(destination)
            if manifest is not None:
                row = manifest_rows[i - 1]
                image = read_source_image(source, args.bayer_pattern, debayer=False)
                if image.data.ndim == 2:
                    pattern = str(image.header.get("BAYERPAT") or "").strip()
                    if not pattern:
                        raise ValueError(
                            f"CFA Bayer pattern is missing for {source.name}; provide --bayer-pattern"
                        )
                    cfa = True
                write_registered_float(
                    destination,
                    image,
                    parse_time(row["time"]),
                    str(manifest.get("object") or source.parent.name),
                    float(manifest["exposure_seconds"]) if manifest.get("exposure_seconds") is not None else None,
                )
            else:
                shutil.copy2(source, destination)
                if i == 1:
                    raw_image = read_source_image(source, args.bayer_pattern, debayer=False)
                    cfa = raw_image.data.ndim == 2 and bool(
                        str(raw_image.header.get("BAYERPAT") or raw_image.header.get("COLORTYP") or "").strip()
                    )
    except Exception:
        if not args.no_cleanup:
            cleanup_intermediate_images(registration_dir, args.basename, copied, len(copied))
        raise

    preprocess_script = registration_dir / "preprocess_and_debayer.ssf"
    staged_preprocessing_plan = stage_preprocessing_files(preprocessing_plan, registration_dir)
    preprocess_text, processed_sequence = build_sequence_preprocess_script(
        args.basename,
        staged_preprocessing_plan,
        cfa=cfa,
    )
    preprocess_script.write_text(preprocess_text, encoding="ascii")
    if args.verbose:
        print(
            "[preprocess] Siril calibration/debayer: "
            f"dark={'on' if preprocessing_plan.dark_enabled else 'off'}; "
            f"flat={'on' if preprocessing_plan.flat_enabled else 'off'}; "
            f"hot={'on' if preprocessing_plan.hot_pixel_enabled else 'off'}; "
            f"cold={'on' if preprocessing_plan.cold_pixel_enabled else 'off'}; "
            f"cfa={'yes' if cfa else 'no'}",
            flush=True,
        )
    run_siril(args.siril, registration_dir, preprocess_script, args.verbose)
    processed_basename = processed_sequence.rstrip("_")
    processed_files = [registration_dir / f"{processed_basename}_{i:05d}.fit" for i in range(1, len(files) + 1)]
    missing_processed = [path for path in processed_files if not path.is_file()]
    if missing_processed:
        raise RuntimeError(
            f"Siril preprocessing produced only {len(processed_files) - len(missing_processed)}/{len(processed_files)} frame(s)"
        )

    if use_sharpcap_registration:
        if args.verbose:
            print(f"[registration] Applying SharpCap StackLog alignment: {len(files)} frames", flush=True)
        for i, prepared in enumerate(processed_files, start=1):
            row = manifest_rows[i - 1]
            image = read_fits(prepared)
            tx, ty, angle = relative_sharpcap_transform(row, manifest_reference)
            aligned, _valid = transform_image(image.data, tx, ty, angle)
            registered = registration_dir / f"r_{processed_basename}_{i:05d}.fit"
            write_registered_float(
                registered,
                FitsImage(header=image.header, cards=image.cards, data=aligned),
                parse_time(row["time"]),
                str(manifest.get("object") or files[i - 1].parent.name),
                float(manifest["exposure_seconds"]) if manifest.get("exposure_seconds") is not None else None,
            )

    siril_script = registration_dir / "register_background_stars.ssf"
    write_siril_registration_script(
        siril_script,
        processed_basename,
        args.registration_transform,
        args.registration_minpairs,
        reference_index,
    )
    registration_seq = registration_dir / f"{processed_basename}_.seq"
    try:
        if use_sharpcap_registration:
            print("[registration] Using SharpCap StackLog alignment after Siril preprocessing", flush=True)
            star_registrations = {}
            reference_stack_index = int(manifest_reference["frame_index"])
            for index, row in enumerate(manifest_rows, start=1):
                tx, ty, angle = relative_sharpcap_transform(row, manifest_reference)
                radians = math.radians(angle)
                star_registrations[index] = SirilRegistration(
                    index=index,
                    selected=True,
                    reference_index=reference_index,
                    detected_stars=int(row["detected_stars"]) if row.get("detected_stars") is not None else None,
                    fwhm_px=float(row["fwhm_px"]) if row.get("fwhm_px") is not None else None,
                    matrix=(math.cos(radians), -math.sin(radians), tx, math.sin(radians), math.cos(radians), ty, 0.0, 0.0, 1.0),
                )
            match_diagnostics = {}
            registration_issues = {}
            registration_diagnostic_rows = build_registration_diagnostic_rows(
                files, reference_index, star_registrations, match_diagnostics, registration_issues
            )
            registration_snapshot_csv = work_dir / "registration_diagnostics.csv"
            write_registration_diagnostics(registration_snapshot_csv, registration_diagnostic_rows)
            print(f"[registration] Diagnostics: {registration_snapshot_csv}", flush=True)
            if args.verbose:
                print(
                    f"[registration] SharpCap produced {len(copied)}/{len(copied)} registered frames; "
                    f"StackLog reference index={reference_stack_index}",
                    flush=True,
                )
        elif args.verbose:
            print(f"[registration] Siril background-star registration: {len(copied)} frames", flush=True)
        if not use_sharpcap_registration:
            siril_output = run_siril(args.siril, registration_dir, siril_script, args.verbose)
            star_registrations = parse_siril_registration(registration_seq)
            match_diagnostics = parse_siril_match_diagnostics(siril_output)
            registration_issues = registration_validation_issues(
                files,
                registration_dir,
                processed_basename,
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
                (registration_dir / f"r_{processed_basename}_{i:05d}.fit").exists()
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
                processed_basename,
                len(files),
                args.verbose,
            ),
        )
        match_diagnostics = parse_siril_match_diagnostics(error.output)
        registration_issues = registration_validation_issues(
            files,
            registration_dir,
            processed_basename,
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
            removed = cleanup_intermediate_images(
                registration_dir, args.basename, copied, len(copied), processed_basename
            )
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
            removed = cleanup_intermediate_images(
                registration_dir, args.basename, copied, len(copied), processed_basename
            )
            print(f"[cleanup] Removed {len(removed)} intermediate FITS files after registration failure", flush=True)
        raise
    reference = read_fits(processed_files[reference_index - 1])
    height = int(reference.header["NAXIS2"])
    width = int(reference.header["NAXIS1"])
    if args.wcs_fits:
        wcs = WcsModel.from_wcs_fits(args.wcs_fits)
    else:
        wcs = WcsModel.from_astrometry_json(args.astrometry_json, width, height)

    reference_time = parse_time(reference.header["DATE-OBS"])
    reference_target = interpolate_ephemeris(ephemeris, reference_time)
    reference_x, reference_y = wcs.world_to_pixel(reference_target.ra_deg, reference_target.dec_deg)
    sun_header: dict[str, object] = {}
    sun_pa_status = "off"
    if args.sun_pa == "auto":
        observer_center = observer_center_from_ephemeris_csv(args.ephemeris_csv)
        if observer_center is None:
            sun_pa_status = "unavailable-observer"
            print(
                "SUN_PA not written: the ephemeris CSV does not record a usable Horizons observer center.",
                file=sys.stderr,
            )
        else:
            try:
                sun = fetch_sun_position(reference_time, observer_center, verbose=args.verbose)
                sun_header = sun_pa_fits_header(reference_target.ra_deg, reference_target.dec_deg, sun)
                sun_pa_status = "written"
                if args.verbose:
                    print(
                        f"SUN_PA={sun_header['SUN_PA']:.5f} deg "
                        f"(anti-solar={sun_header['ASUN_PA']:.5f} deg; {sun_header['SUNCENTR']})"
                    )
            except Exception as exc:
                sun_pa_status = f"query-failed: {exc.__class__.__name__}"
                print(f"SUN_PA not written: Horizons solar query failed: {exc}", file=sys.stderr)
    background_output_levels: np.ndarray | None = None
    background_models_by_index: dict[int, BackgroundModel] = {}
    background_corrections_by_index: dict[int, np.ndarray] = {}
    if args.background_normalization != "none":
        print(f"[background] Estimating per-frame {args.background_normalization} models", flush=True)
        (
            background_output_levels,
            background_models_by_index,
            background_corrections_by_index,
        ) = collect_background_normalization(
            copied,
            files,
            registration_dir,
            processed_basename,
            registration_issues,
            args.background_normalization,
            args.verbose,
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
    metcalf_coverage: np.ndarray | None = None
    star_coverage: np.ndarray | None = None
    median_stack: MedianAccumulator | None = None
    median_star_stack: MedianAccumulator | None = None
    frame_rows: list[dict[str, object]] = []
    used_times: list[datetime] = []
    used = 0
    if args.verbose:
        print(
            f"[stack] padding policy={args.padding_policy}; zero-sample policy={args.zero_sample_policy}; "
            f"background normalization={args.background_normalization}",
            flush=True,
        )

    for i, source in enumerate(copied, start=1):
        if args.verbose:
            print(
                f"[stack:{args.stack_method}] frame {i}/{len(copied)}: {files[i - 1].name}",
                flush=True,
            )
        registered = registration_dir / f"r_{processed_basename}_{i:05d}.fit"
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
        saturation_level: float | None = None
        saturation_threshold_count: float | None = None
        subframe_max_count: float | None = None
        saturated_pixel_count = 0
        frame_saturation_warning = False
        background_model = background_models_by_index.get(i)
        background_correction = background_corrections_by_index.get(i)
        stack_data = image.data
        if args.background_normalization != "none":
            if source_valid is None or background_model is None or background_correction is None:
                raise RuntimeError(f"Background normalization data is missing for usable frame {i}")
            stack_data = apply_background_model(
                image.data,
                source_valid,
                background_correction,
                args.background_normalization,
            )
        shifted, mask2d = shift_image(stack_data, dx, dy, source_valid)
        star_shifted, star_mask2d = shift_image(stack_data, 0.0, 0.0, source_valid)
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
                    exclude_zero_samples=(
                        args.zero_sample_policy == "exclude" and args.background_normalization == "none"
                    ),
                )
                median_star_stack = MedianAccumulator(
                    work_dir / f"{args.stack_method}_star_frames.npy",
                    len(files),
                    star_shifted.shape,
                    exclude_zero_samples=(
                        args.zero_sample_policy == "exclude" and args.background_normalization == "none"
                    ),
                )
            median_stack.add(shifted, mask2d)
            if median_star_stack is None:
                raise RuntimeError("Star median accumulator was not initialized")
            median_star_stack.add(star_shifted, star_mask2d)
        if metcalf_coverage is None:
            metcalf_coverage = np.zeros(mask2d.shape, dtype=np.uint32)
            star_coverage = np.zeros(star_mask2d.shape, dtype=np.uint32)
        if star_coverage is None:
            raise RuntimeError("Star coverage image was not initialized")
        metcalf_coverage += mask2d.astype(np.uint16)
        star_coverage += star_mask2d.astype(np.uint16)
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
                "background_ch1_adu": float(background_model.levels[0]) if background_model is not None else None,
                "background_ch2_adu": float(background_model.levels[1]) if background_model is not None and background_model.levels.size > 1 else None,
                "background_ch3_adu": float(background_model.levels[2]) if background_model is not None and background_model.levels.size > 2 else None,
                "background_offset_ch1_adu": float(background_correction[0, 0]) if background_correction is not None else None,
                "background_offset_ch2_adu": float(background_correction[1, 0]) if background_correction is not None and background_correction.shape[0] > 1 else None,
                "background_offset_ch3_adu": float(background_correction[2, 0]) if background_correction is not None and background_correction.shape[0] > 2 else None,
                "background_model": background_model.mode if background_model is not None else None,
                "background_fit_tiles": background_model.tile_count if background_model is not None else None,
                "background_fit_rejected_ch1": int(background_model.rejected_tile_counts[0]) if background_model is not None else None,
                "background_fit_rejected_ch2": int(background_model.rejected_tile_counts[1]) if background_model is not None and background_model.rejected_tile_counts.size > 1 else None,
                "background_fit_rejected_ch3": int(background_model.rejected_tile_counts[2]) if background_model is not None and background_model.rejected_tile_counts.size > 2 else None,
                "background_fit_rms_ch1_adu": float(background_model.residual_rms[0]) if background_model is not None else None,
                "background_fit_rms_ch2_adu": float(background_model.residual_rms[1]) if background_model is not None and background_model.residual_rms.size > 1 else None,
                "background_fit_rms_ch3_adu": float(background_model.residual_rms[2]) if background_model is not None and background_model.residual_rms.size > 2 else None,
                "background_coefficients": json.dumps(background_model.coefficients.tolist()) if background_model is not None else None,
                "background_correction_coefficients": json.dumps(background_correction.tolist()) if background_correction is not None else None,
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
    if args.background_normalization != "none":
        if background_output_levels is None or metcalf_coverage is None or star_coverage is None:
            raise RuntimeError("Background output offset data is missing")
        stack = add_background_output_offset(stack, metcalf_coverage > 0, background_output_levels)
        star_stack = add_background_output_offset(star_stack, star_coverage > 0, background_output_levels)
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
    north_up_angle = None
    north_up_output_png = None
    north_up_star_output_png = None
    north_up_comparison_output_png = None
    if args.preview_north_up:
        north_up_angle = north_up_rotation_degrees(wcs)
        north_up_output_png = work_dir / f"{output_stem}_metcalf_north_up_preview.png"
        north_up_star_output_png = work_dir / f"{output_stem}_star_north_up_preview.png"
        north_up_comparison_output_png = work_dir / f"{output_stem}_star_left_metcalf_right_north_up_preview.png"
    sun_pa_left_angle = None
    sun_pa_left_output_png = None
    if args.preview_sun_pa_left:
        if "SUN_PA" not in sun_header:
            raise RuntimeError(
                "--preview-sun-pa-left requires a Horizons solar position. "
                "Use an ephemeris CSV with observer metadata and do not set --sun-pa off."
            )
        sun_pa_left_angle = sun_pa_left_rotation_degrees(
            wcs,
            reference_target.dec_deg,
            float(sun_header["SUN_PA"]),
        )
        sun_pa_left_output_png = work_dir / f"{output_stem}_metcalf_sun_pa_left_preview.png"
    annotated_output_png = None
    annotation_overlay_png = None
    annotation_rotation = 0.0
    annotation_source_png = output_png
    if args.preview_annotate:
        if "SUN_PA" not in sun_header:
            raise RuntimeError(
                "--preview-annotate requires a Horizons solar position. "
                "Use an ephemeris CSV with observer metadata and do not set --sun-pa off."
            )
        if args.preview_north_up:
            annotation_rotation = north_up_angle if north_up_angle is not None else 0.0
            annotation_source_png = north_up_output_png if north_up_output_png is not None else output_png
            annotated_output_png = work_dir / f"{output_stem}_metcalf_north_up_annotated_preview.png"
            annotation_overlay_png = work_dir / f"{output_stem}_metcalf_north_up_annotation_overlay.png"
        elif args.preview_sun_pa_left:
            annotation_rotation = sun_pa_left_angle if sun_pa_left_angle is not None else 0.0
            annotation_source_png = sun_pa_left_output_png if sun_pa_left_output_png is not None else output_png
            annotated_output_png = work_dir / f"{output_stem}_metcalf_sun_pa_left_annotated_preview.png"
            annotation_overlay_png = work_dir / f"{output_stem}_metcalf_sun_pa_left_annotation_overlay.png"
        else:
            annotated_output_png = work_dir / f"{output_stem}_metcalf_annotated_preview.png"
            annotation_overlay_png = work_dir / f"{output_stem}_metcalf_annotation_overlay.png"
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
    background_header = {"BGNORM": args.background_normalization}
    if background_output_levels is not None:
        background_header.update(
            {
                "BGEST": "sigclip-med",
                "BGGOAL": "zero",
                "BGOUTUSE": "range-only",
                "BGOUTEST": "mean-dc",
                "BGSIGMA": BACKGROUND_SIGMA,
                "BGTILER": BACKGROUND_TILE_ROWS if args.background_normalization != "offset" else 0,
                "BGTILEC": BACKGROUND_TILE_COLUMNS if args.background_normalization != "offset" else 0,
                "BGTSTEP": BACKGROUND_TILE_SAMPLE_STEP if args.background_normalization != "offset" else 0,
                "BGOUTSIG": BACKGROUND_FIT_OUTLIER_SIGMA if args.background_normalization != "offset" else 0.0,
            }
        )
        if args.background_normalization == "offset":
            background_header.update(
                {
                    "BGROI": BACKGROUND_ROI_FRACTION,
                    "BGSAMPLE": BACKGROUND_SAMPLE_STEP,
                }
            )
        for channel, output_level in enumerate(background_output_levels, start=1):
            background_header[f"BGREF{channel}"] = float(output_level)
    effective_zero_sample_policy = (
        "mask-only" if args.background_normalization != "none" and args.stack_method != "mean" else args.zero_sample_policy
    )
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
        "ZEROPOL": effective_zero_sample_policy if args.stack_method != "mean" else "n/a",
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": reference_mode,
        "REFINDEX": reference_index,
        "MTUNITS": "ADU",
        **sun_header,
        **background_header,
    }
    star_extra_header = {
        **star_wcs_header,
        "STARSTK": True,
        "MTSTACK": False,
        "MTFRAMES": used,
        "MTUNITS": "ADU",
        "STKMODE": args.stack_method,
        "PADPOL": args.padding_policy,
        "ZEROPOL": effective_zero_sample_policy if args.stack_method != "mean" else "n/a",
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": reference_mode,
        "REFINDEX": reference_index,
        **background_header,
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
        "ZEROPOL": effective_zero_sample_policy if args.stack_method != "mean" else "n/a",
        "RFFRAC": args.rankfit_fraction if args.stack_method == "rankfit" else 0,
        "RFDEG": 5 if args.stack_method == "rankfit" else 0,
        "REFMODE": reference_mode,
        "REFINDEX": reference_index,
        **background_header,
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
        low_percentile=args.preview_low_percentile,
        high_percentile=args.preview_high_percentile,
        stretch=args.preview_stretch,
        sigma_low=args.preview_sigma_low,
        sigma_high=args.preview_sigma_high,
    )
    export_preview_png(
        star_output_png,
        star_stack,
        low_percentile=args.preview_low_percentile,
        high_percentile=args.preview_high_percentile,
        stretch=args.preview_stretch,
        sigma_low=args.preview_sigma_low,
        sigma_high=args.preview_sigma_high,
    )
    export_preview_png(
        comparison_output_png,
        comparison_stack,
        low_percentile=args.preview_low_percentile,
        high_percentile=args.preview_high_percentile,
        stretch=args.preview_stretch,
        sigma_low=args.preview_sigma_low,
        sigma_high=args.preview_sigma_high,
    )
    if args.preview_north_up:
        if north_up_angle is None or north_up_output_png is None or north_up_star_output_png is None or north_up_comparison_output_png is None:
            raise RuntimeError("North-up preview paths were not initialized")
        rotate_preview_png(output_png, north_up_output_png, north_up_angle)
        rotate_preview_png(star_output_png, north_up_star_output_png, north_up_angle)
        rotate_comparison_preview_png(
            star_output_png,
            output_png,
            north_up_comparison_output_png,
            north_up_angle,
        )
        print(f"[preview] North-up PNGs written (rotation={north_up_angle:.3f} deg)", flush=True)
    if args.preview_sun_pa_left:
        if sun_pa_left_angle is None or sun_pa_left_output_png is None:
            raise RuntimeError("Sun-PA-left preview path was not initialized")
        rotate_preview_png(output_png, sun_pa_left_output_png, sun_pa_left_angle)
        print(
            f"[preview] Sun-PA-left PNG written (rotation={sun_pa_left_angle:.3f} deg; "
            f"SUN_PA={sun_header['SUN_PA']:.3f} deg)",
            flush=True,
        )
    if args.preview_annotate:
        if annotated_output_png is None or annotation_overlay_png is None:
            raise RuntimeError("Annotated preview path was not initialized")
        annotate_preview_png(
            annotation_source_png,
            annotated_output_png,
            wcs.cd_matrix(),
            reference_target.dec_deg,
            float(sun_header["SUN_PA"]),
            image_rotation_degrees=annotation_rotation,
            corner=args.annotate_at,
            radius_px=args.annotate_size,
        )
        write_annotation_overlay_png(
            annotation_overlay_png,
            wcs.cd_matrix(),
            reference_target.dec_deg,
            float(sun_header["SUN_PA"]),
            image_rotation_degrees=annotation_rotation,
            radius_px=args.annotate_size,
        )
        print(f"[preview] Annotated PNG and transparent overlay written at {args.annotate_at}", flush=True)
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
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            stretch=args.preview_stretch,
            sigma_low=args.preview_sigma_low,
            sigma_high=args.preview_sigma_high,
            warning_mask=metcalf_saturation_mask,
            warning_color=warning_color_rgb,
        )
        export_preview_png(
            star_saturation_output_png,
            star_stack,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            stretch=args.preview_stretch,
            sigma_low=args.preview_sigma_low,
            sigma_high=args.preview_sigma_high,
            warning_mask=star_saturation_mask,
            warning_color=warning_color_rgb,
        )
        export_preview_png(
            comparison_saturation_output_png,
            comparison_stack,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            stretch=args.preview_stretch,
            sigma_low=args.preview_sigma_low,
            sigma_high=args.preview_sigma_high,
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
            "background_ch1_adu",
            "background_ch2_adu",
            "background_ch3_adu",
            "background_offset_ch1_adu",
            "background_offset_ch2_adu",
            "background_offset_ch3_adu",
            "background_model",
            "background_fit_tiles",
            "background_fit_rejected_ch1",
            "background_fit_rejected_ch2",
            "background_fit_rejected_ch3",
            "background_fit_rms_ch1_adu",
            "background_fit_rms_ch2_adu",
            "background_fit_rms_ch3_adu",
            "background_coefficients",
            "background_correction_coefficients",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(frame_rows)

    write_registration_diagnostics(registration_diagnostics_csv, frame_rows)
    print(f"[result] Registration diagnostics: {registration_diagnostics_csv}", flush=True)

    removed_intermediate_images: list[str] = []
    if not args.no_cleanup:
        removed_intermediate_images = cleanup_intermediate_images(
            registration_dir, args.basename, copied, len(files), processed_basename
        )

    summary = {
        "source_dir": str(args.source_dir),
        "work_dir": str(work_dir),
        "registration_dir": str(registration_dir),
        "ephemeris_csv": str(args.ephemeris_csv),
        "wcs_fits": str(args.wcs_fits) if args.wcs_fits else None,
        "astrometry_json": str(args.astrometry_json) if args.astrometry_json else None,
        "registration_transform": args.registration_transform,
        "registration_source": "sharpcap-stacklog" if use_sharpcap_registration else "siril",
        "frame_manifest": str(args.frame_manifest) if args.frame_manifest else None,
        "preprocessing": preprocessing_plan.to_dict(),
        "registration_minpairs": args.registration_minpairs,
        "registration_seq": str(registration_seq),
        "preview_north_up": args.preview_north_up,
        "preview_north_up_rotation_deg": north_up_angle,
        "preview_sun_pa_left": args.preview_sun_pa_left,
        "preview_sun_pa_left_rotation_deg": sun_pa_left_angle,
        "preview_annotate": args.preview_annotate,
        "annotate_at": args.annotate_at if args.preview_annotate else None,
        "annotate_size_px": args.annotate_size if args.preview_annotate else None,
        "preview_stretch": args.preview_stretch,
        "preview_sigma_low": args.preview_sigma_low,
        "preview_sigma_high": args.preview_sigma_high,
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
        "zero_sample_policy": effective_zero_sample_policy if args.stack_method != "mean" else None,
        "background_normalization": {
            "mode": args.background_normalization,
            "estimator": "sigma-clipped median" if background_output_levels is not None else None,
            "sigma": BACKGROUND_SIGMA if background_output_levels is not None else None,
            "roi_fraction": BACKGROUND_ROI_FRACTION if args.background_normalization == "offset" else None,
            "sample_step": BACKGROUND_SAMPLE_STEP if args.background_normalization == "offset" else None,
            "tile_rows": BACKGROUND_TILE_ROWS if args.background_normalization in {"plane", "quadratic"} else None,
            "tile_columns": BACKGROUND_TILE_COLUMNS if args.background_normalization in {"plane", "quadratic"} else None,
            "tile_sample_step": BACKGROUND_TILE_SAMPLE_STEP if args.background_normalization in {"plane", "quadratic"} else None,
            "fit_outlier_sigma": BACKGROUND_FIT_OUTLIER_SIGMA if args.background_normalization in {"plane", "quadratic"} else None,
            "frame_background_goal": "zero",
            "output_offset_purpose": "constant range safeguard applied after stacking",
            "output_offset_estimator": "arithmetic mean of accepted per-frame DC backgrounds",
            "output_offset_adu": background_output_levels.tolist() if background_output_levels is not None else None,
        },
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
        "sun_position_angle": {
            "mode": args.sun_pa,
            "status": sun_pa_status,
            **({"sun_pa_deg": sun_header["SUN_PA"], "anti_solar_pa_deg": sun_header["ASUN_PA"]} if sun_header else {}),
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
            "north_up_preview_png": str(north_up_output_png) if north_up_output_png else None,
            "north_up_star_preview_png": str(north_up_star_output_png) if north_up_star_output_png else None,
            "north_up_comparison_preview_png": str(north_up_comparison_output_png) if north_up_comparison_output_png else None,
            "sun_pa_left_preview_png": str(sun_pa_left_output_png) if sun_pa_left_output_png else None,
            "annotated_preview_png": str(annotated_output_png) if annotated_output_png else None,
            "annotation_overlay_png": str(annotation_overlay_png) if annotation_overlay_png else None,
            "metcalf_saturation_warning_png": (
                str(saturation_output_png) if saturation_output_png else None
            ),
            "star_saturation_warning_png": (
                str(star_saturation_output_png) if star_saturation_output_png else None
            ),
            "comparison_saturation_warning_png": (
                str(comparison_saturation_output_png) if comparison_saturation_output_png else None
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
