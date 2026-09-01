#!/usr/bin/env python
"""Moving-target stack for Seestar or SharpCap subframes.

Pipeline:
1. Copy a clean subset of source FITS files into a work directory.
2. Use Siril CLI to debayer frames and solve background-star registration matrices.
3. Use a reference-frame WCS and a target ephemeris CSV to compute the target
   pixel in the selected reference coordinate system for every frame.
4. Combine each star-registration matrix with the Metcalf translation, resample
   the source once, then mean-, median-, or rank-fit-stack the shifted frames.

SharpCap Live Stack offsets can replace Siril registration when complete.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import gc
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
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

SOFTWARE_NAME = "Seestar Metcalf Stack"
SOFTWARE_VERSION = "0.9.5"
SIP_KEY_PATTERN = re.compile(r"^(?:A|B|AP|BP)_(?:ORDER|DMAX|\d+_\d+)$")
SOURCE_METADATA_KEYS = (
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
)
STACK_HEADER_COMMENTS = {
    "CREATOR": "software that generated this FITS",
    "SWVER": "generator software version",
    "TIMESYS": "time scale for DATE-* and MJD-* keywords",
    "DATE-BEG": "UTC start of first accepted exposure",
    "DATE-AVG": "UTC exposure-weighted mean midpoint",
    "DATE-END": "UTC end of last accepted exposure",
    "MJD-AVG": "d; DATE-AVG as modified Julian date",
    "TELAPSE": "s; elapsed time from DATE-BEG to DATE-END",
    "TOTEXP": "s; sum of accepted subframe exposures",
    "NCOMBINE": "number of accepted subframes",
    "PLTSOLVR": "reference-frame plate solver",
}


@dataclass
class FitsImage:
    header: dict[str, object]
    cards: list[str]
    data: np.ndarray


@dataclass(frozen=True)
class StackCanvas:
    """Output footprint expressed in the background-registration coordinate system."""

    shape: tuple[int, int]
    origin_x: float = 0.0
    origin_y: float = 0.0

    def __post_init__(self) -> None:
        height, width = self.shape
        if height <= 0 or width <= 0:
            raise ValueError(f"Stack canvas dimensions must be positive: {self.shape}")

    @classmethod
    def reference_footprint(cls, shape: tuple[int, int]) -> "StackCanvas":
        return cls(shape=(int(shape[0]), int(shape[1])))

    def is_identity_for(self, source_shape: tuple[int, int]) -> bool:
        return self.shape == source_shape and self.origin_x == 0.0 and self.origin_y == 0.0

    def registration_to_output_pixel(self, x_1based: float, y_1based: float) -> tuple[float, float]:
        """Convert a registration-coordinate pixel to this canvas' FITS pixel coordinates."""
        return x_1based - self.origin_x, y_1based - self.origin_y

    def rebase_wcs_header(self, header: dict[str, object]) -> dict[str, object]:
        """Move a registration-coordinate WCS origin onto this output canvas."""
        rebased = dict(header)
        if "CRPIX1" in rebased:
            rebased["CRPIX1"] = float(rebased["CRPIX1"]) - self.origin_x
        if "CRPIX2" in rebased:
            rebased["CRPIX2"] = float(rebased["CRPIX2"]) - self.origin_y
        return rebased


@dataclass(frozen=True)
class BilinearTranslationPlan:
    """Reusable source/output slices and mask for one translation onto a canvas."""

    output_shape: tuple[int, int]
    output_slice: tuple[slice, slice] | None
    source_y0: int
    source_y1: int
    source_x0: int
    source_x1: int
    weight_x: float
    weight_y: float
    valid_mask: np.ndarray
    identity: bool = False


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
            result = {key: self.header[key] for key in keys if key in self.header}
            # SIP orders are not limited to three. Preserve every forward and
            # inverse polynomial term emitted by the plate solver, regardless
            # of its declared order. Re-basing the canvas adjusts only CRPIX;
            # the SIP coordinates (pixel minus CRPIX) and coefficients remain
            # unchanged.
            for key, value in self.header.items():
                normalized = key.upper()
                if SIP_KEY_PATTERN.fullmatch(normalized):
                    result[normalized] = value
            return result
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


def shift_boolean_mask(
    mask: np.ndarray,
    dx: float,
    dy: float,
    canvas: StackCanvas | None = None,
) -> np.ndarray:
    """Conservatively mark every output pixel touched by a shifted true pixel."""
    shifted, valid = shift_plane(mask.astype(np.float32, copy=False), dx, dy, canvas=canvas)
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
            # MJD needs substantially more precision than ordinary diagnostic
            # values: 1e-10 day is about 8.6 microseconds.
            precision = 15 if key.upper().startswith("MJD-") else 10
            text = f"{key:<8}= {value:>20.{precision}E}"
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


def stack_history_text(extra: dict[str, object]) -> str:
    if str(extra.get("TARGMODE", "")).casefold() == "fixed":
        product = "Fixed stack"
    elif bool(extra.get("COMBSTK")):
        product = "Star/Metcalf comparison"
    elif bool(extra.get("STARSTK")) and not bool(extra.get("MTSTACK")):
        product = "Star-aligned stack"
    elif bool(extra.get("MTSTACK")):
        product = "Metcalf stack"
    else:
        product = "Intermediate image"
    return f"{product} generated by {SOFTWARE_NAME} v{SOFTWARE_VERSION}"


def format_extra_card(key: str, value: object) -> str:
    return format_card(key, value, STACK_HEADER_COMMENTS.get(key))


def history_card(text: str) -> str:
    return f"HISTORY {text}"[:80].ljust(80)


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
    for key in SOURCE_METADATA_KEYS:
        if key in source_header:
            cards.append(format_card(key, source_header[key]))
    for key, value in extra.items():
        cards.append(format_extra_card(key, value))
    cards.append(history_card(stack_history_text(extra)))
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
    for key in SOURCE_METADATA_KEYS:
        if key in source_header:
            cards.append(format_card(key, source_header[key]))
    for key, value in extra.items():
        cards.append(format_extra_card(key, value))
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
    cards.append(history_card(stack_history_text(extra)))
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


def fits_utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def datetime_to_mjd(value: datetime) -> float:
    unix_epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return 40587.0 + (value.astimezone(timezone.utc) - unix_epoch).total_seconds() / 86400.0


def exposure_seconds_from_header(header: dict[str, object]) -> float | None:
    for key in ("EXPTIME", "EXPOSURE"):
        try:
            value = float(header[key])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0.0:
            return value
    return None


def infer_plate_solver_name(wcs_fits: Path | None, astrometry_json: Path | None) -> str:
    if astrometry_json is not None:
        return "Astrometry.net"
    if wcs_fits is None:
        return "unknown"
    name = wcs_fits.name.casefold()
    if name.endswith("_siril_wcs.fits") or name.endswith("_siril_wcs.fit"):
        return "Siril"
    if name.endswith("_wcs.fits") or name.endswith("_wcs.fit"):
        return "Astrometry.net"
    return "Provided WCS"


def stack_session_header(
    used_times: list[datetime],
    used_exposures: list[float | None],
    plate_solver_name: str,
) -> dict[str, object]:
    if not used_times or len(used_times) != len(used_exposures):
        raise ValueError("Accepted frame times and exposures must be non-empty and have equal lengths")

    end_times = [
        observed + timedelta(seconds=exposure) if exposure is not None else observed
        for observed, exposure in zip(used_times, used_exposures)
    ]
    solver_label = {
        "siril": "Siril",
        "astrometry.net": "Astrometry.net",
        "explicit-wcs": "Provided WCS",
    }.get(plate_solver_name.casefold(), plate_solver_name)
    header: dict[str, object] = {
        "CREATOR": SOFTWARE_NAME,
        "SWVER": SOFTWARE_VERSION,
        "TIMESYS": "UTC",
        "DATE-BEG": fits_utc_timestamp(min(used_times)),
        "DATE-END": fits_utc_timestamp(max(end_times)),
        "NCOMBINE": len(used_times),
        "PLTSOLVR": solver_label,
    }
    if all(exposure is not None for exposure in used_exposures):
        exposures = [float(exposure) for exposure in used_exposures if exposure is not None]
        total_exposure = math.fsum(exposures)
        first_start = min(used_times)
        last_end = max(end_times)
        weighted_midpoint_seconds = math.fsum(
            exposure * ((observed + timedelta(seconds=exposure / 2.0)) - first_start).total_seconds()
            for observed, exposure in zip(used_times, exposures)
        ) / total_exposure
        average_time = first_start + timedelta(seconds=weighted_midpoint_seconds)
        header.update(
            {
                "DATE-AVG": fits_utc_timestamp(average_time),
                "MJD-AVG": datetime_to_mjd(average_time),
                "TELAPSE": (last_end - first_start).total_seconds(),
                "TOTEXP": total_exposure,
            }
        )
    return header


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
MEBIBYTE = 1024 * 1024
GIBIBYTE = 1024 * MEBIBYTE
AUTO_WORKER_COUNTS = (4, 2, 1)
AUTO_WORKER_UNKNOWN_RAM_DEFAULT = 2
AUTO_WORKER_RESERVE_MIN_BYTES = 512 * MEBIBYTE
AUTO_WORKER_RESERVE_FRACTION = 0.25
AUTO_WORKER_FIXED_OVERHEAD_BYTES = 64 * MEBIBYTE
MEDIAN_TILE_RAM_FRACTION = 0.50
MEDIAN_TILE_UNKNOWN_RAM_ROWS = 16
RANKFIT_WORKSPACE_BYTES = 16 * MEBIBYTE


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


@dataclass(frozen=True)
class StackFrameTask:
    """Metadata needed to process one preprocessed frame without shared state."""

    index: int
    source_name: str
    prepared: Path
    source_header: dict[str, object]
    source_to_registration: tuple[float, float, float, float, float, float, float, float, float]
    frame_time: datetime
    target: TargetPoint
    target_x: float
    target_y: float
    dx: float
    dy: float


@dataclass
class StackFrameResult:
    """Heavy per-frame products returned for deterministic serial accumulation."""

    task: StackFrameTask
    star_data: np.ndarray
    star_mask: np.ndarray
    metcalf_data: np.ndarray
    metcalf_mask: np.ndarray
    prepared_unit_scale: float
    background_model: BackgroundModel | None
    background_correction: np.ndarray | None
    star_saturation_mask: np.ndarray | None
    metcalf_saturation_mask: np.ndarray | None
    saturation_level: float | None
    saturation_threshold_count: float | None
    subframe_max_count: float | None
    saturated_pixel_count: int
    timings: dict[str, float]


@dataclass
class StackFrameAnalysis:
    """Frame-wide metadata evaluated before an order-statistic cube is allocated."""

    task: StackFrameTask
    prepared_unit_scale: float
    background_model: BackgroundModel | None
    background_correction: np.ndarray | None
    saturation_level: float | None
    saturation_threshold_count: float | None
    subframe_max_count: float | None
    saturated_pixel_count: int
    timings: dict[str, float]


@dataclass
class StackTileFrameResult:
    """One frame resampled only onto the current output-row tile."""

    task: StackFrameTask
    star_data: np.ndarray
    star_mask: np.ndarray
    metcalf_data: np.ndarray
    metcalf_mask: np.ndarray
    star_saturation_mask: np.ndarray | None
    metcalf_saturation_mask: np.ndarray | None
    timings: dict[str, float]


@dataclass(frozen=True)
class StackWorkerMemoryEstimate:
    """Conservative array-allocation estimate used by automatic worker selection."""

    source_shape: tuple[int, ...]
    canvas_shape: tuple[int, int]
    fixed_bytes: int
    per_worker_bytes: int

    def projected_bytes(self, workers: int) -> int:
        return self.fixed_bytes + self.per_worker_bytes * workers


@dataclass
class StackWorkerPlan:
    """Initial worker decision and any runtime allocation fallbacks."""

    requested: str | int
    initial_workers: int
    current_workers: int
    available_bytes: int | None
    reserve_bytes: int | None
    estimate: StackWorkerMemoryEstimate
    reason: str
    fallback_events: list[dict[str, object]]


@dataclass
class MedianTilePlan:
    """Bounded in-memory cube plan for median and rank-fit stacking."""

    requested: str | int
    initial_rows: int
    current_rows: int
    available_bytes: int | None
    budget_bytes: int | None
    bytes_per_row: int
    frame_count: int
    channels: int
    width: int
    height: int
    cube_count: int
    reason: str
    fallback_events: list[dict[str, object]]

    def cube_bytes(self, rows: int | None = None) -> int:
        return self.bytes_per_row * (self.current_rows if rows is None else rows)


@dataclass
class OrderStatisticTileResult:
    """Completed row tile, committed only after every frame and combination succeeds."""

    star_data: np.ndarray
    star_coverage: np.ndarray
    metcalf_data: np.ndarray | None
    metcalf_coverage: np.ndarray | None
    star_saturation_mask: np.ndarray | None
    metcalf_saturation_mask: np.ndarray | None
    timings: dict[str, float]


@dataclass
class OrderStatisticStackResult:
    """Full output assembled from independent order-statistic row tiles."""

    star_data: np.ndarray
    star_coverage: np.ndarray
    metcalf_data: np.ndarray | None
    metcalf_coverage: np.ndarray | None
    star_saturation_mask: np.ndarray | None
    metcalf_saturation_mask: np.ndarray | None
    timings: dict[str, float]


def parse_stack_workers(value: str) -> str | int:
    normalized = str(value).strip().lower()
    if normalized == "auto":
        return "auto"
    try:
        workers = int(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--stack-workers must be auto, 1, 2, or 4") from error
    if workers not in {1, 2, 4}:
        raise argparse.ArgumentTypeError("--stack-workers must be auto, 1, 2, or 4")
    return workers


def parse_median_tile_rows(value: str) -> str | int:
    normalized = str(value).strip().lower()
    if normalized == "auto":
        return "auto"
    try:
        rows = int(normalized)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--median-tile-rows must be auto or a positive integer") from error
    if rows < 1:
        raise argparse.ArgumentTypeError("--median-tile-rows must be auto or a positive integer")
    return rows


def format_memory_size(byte_count: int | None) -> str:
    if byte_count is None:
        return "unknown"
    if byte_count >= GIBIBYTE:
        return f"{byte_count / GIBIBYTE:.2f} GiB"
    return f"{byte_count / MEBIBYTE:.1f} MiB"


def available_ram_bytes() -> int | None:
    """Return conservatively usable physical RAM reported by the host OS."""
    if os.name == "nt":
        try:
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        except (AttributeError, OSError, ValueError):
            pass

    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        try:
            for line in meminfo.read_text(encoding="ascii", errors="replace").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass

    if sys.platform == "darwin":
        try:
            output = subprocess.check_output(["vm_stat"], text=True, encoding="utf-8", errors="replace")
            page_match = re.search(r"page size of\s+(\d+)\s+bytes", output)
            if page_match:
                pages: dict[str, int] = {}
                for line in output.splitlines():
                    match = re.match(r"Pages (free|inactive|speculative):\s+(\d+)\.", line.strip())
                    if match:
                        pages[match.group(1)] = int(match.group(2))
                if pages:
                    return sum(pages.values()) * int(page_match.group(1))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        available_pages = int(os.sysconf("SC_AVPHYS_PAGES"))
        if page_size > 0 and available_pages > 0:
            return page_size * available_pages
    except (AttributeError, OSError, ValueError):
        pass
    return None


def estimate_stack_worker_memory(
    source_shape: tuple[int, ...],
    canvas: StackCanvas,
    background_mode: str,
    saturation_enabled: bool,
) -> StackWorkerMemoryEstimate:
    """Estimate peak live arrays from source/canvas dimensions, without assuming equal footprints."""
    if len(source_shape) == 2:
        channels = 1
        source_height, source_width = source_shape
    elif len(source_shape) == 3:
        channels, source_height, source_width = source_shape
    else:
        raise ValueError(f"Unsupported stack frame shape for RAM estimation: {source_shape}")
    source_pixels = int(source_height) * int(source_width)
    canvas_pixels = int(canvas.shape[0]) * int(canvas.shape[1])
    source_samples = channels * source_pixels
    canvas_samples = channels * canvas_pixels

    # Fixed arrays are budgeted as the common mean-stack case even for median/rankfit.
    # This deliberately leaves room for the reference image and Python/output overhead.
    fixed_bytes = (
        source_samples * np.dtype(np.float32).itemsize
        + 2 * canvas_samples * np.dtype(np.float64).itemsize
        + 2 * canvas_pixels * np.dtype(np.uint32).itemsize
        + AUTO_WORKER_FIXED_OVERHEAD_BYTES
    )
    if saturation_enabled:
        fixed_bytes += 2 * canvas_pixels * np.dtype(bool).itemsize

    # Model the largest processing phase rather than summing arrays whose lifetimes do
    # not overlap. Registered-unit restoration and background correction can both be
    # float64, while translated products remain float32.
    read_phase = 2 * source_samples * np.dtype(np.float32).itemsize
    background_phase = source_samples * np.dtype(np.float64).itemsize
    if background_mode != "none":
        background_phase += source_samples * np.dtype(np.float64).itemsize
        background_phase += source_pixels * np.dtype(np.float64).itemsize
    resampling_phase = (
        source_samples * np.dtype(np.float64).itemsize
        + 2 * canvas_samples * np.dtype(np.float32).itemsize
        + canvas_pixels * np.dtype(np.float64).itemsize
        + source_pixels * np.dtype(bool).itemsize
        + 2 * canvas_pixels * np.dtype(bool).itemsize
    )
    if saturation_enabled:
        resampling_phase += source_pixels * np.dtype(bool).itemsize + 2 * canvas_pixels * np.dtype(bool).itemsize
    per_worker_bytes = max(read_phase, background_phase, resampling_phase) + 16 * MEBIBYTE
    return StackWorkerMemoryEstimate(
        source_shape=tuple(int(value) for value in source_shape),
        canvas_shape=canvas.shape,
        fixed_bytes=int(fixed_bytes),
        per_worker_bytes=int(per_worker_bytes),
    )


def select_stack_worker_plan(
    requested: str | int,
    estimate: StackWorkerMemoryEstimate,
    available_bytes: int | None = None,
) -> StackWorkerPlan:
    available = available_ram_bytes() if available_bytes is None else available_bytes
    if requested != "auto":
        workers = int(requested)
        return StackWorkerPlan(
            requested=requested,
            initial_workers=workers,
            current_workers=workers,
            available_bytes=available,
            reserve_bytes=None,
            estimate=estimate,
            reason="explicit user setting",
            fallback_events=[],
        )
    if available is None:
        workers = AUTO_WORKER_UNKNOWN_RAM_DEFAULT
        return StackWorkerPlan(
            requested=requested,
            initial_workers=workers,
            current_workers=workers,
            available_bytes=None,
            reserve_bytes=None,
            estimate=estimate,
            reason="available RAM could not be measured; conservative fallback",
            fallback_events=[],
        )

    reserve = max(AUTO_WORKER_RESERVE_MIN_BYTES, int(available * AUTO_WORKER_RESERVE_FRACTION))
    budget = max(0, available - reserve)
    workers = 1
    for candidate in AUTO_WORKER_COUNTS:
        if estimate.projected_bytes(candidate) <= budget:
            workers = candidate
            break
    reason = "largest worker count fitting the conservative RAM budget"
    if estimate.projected_bytes(1) > budget:
        reason = "even one worker exceeds the conservative estimate; using the minimum"
    return StackWorkerPlan(
        requested=requested,
        initial_workers=workers,
        current_workers=workers,
        available_bytes=available,
        reserve_bytes=reserve,
        estimate=estimate,
        reason=reason,
        fallback_events=[],
    )


def describe_stack_worker_plan(plan: StackWorkerPlan) -> str:
    estimate = plan.estimate
    frame_text = "x".join(str(value) for value in estimate.source_shape)
    canvas_text = "x".join(str(value) for value in estimate.canvas_shape)
    projected = estimate.projected_bytes(plan.initial_workers)
    if plan.requested == "auto":
        return (
            f"[workers:auto] available RAM={format_memory_size(plan.available_bytes)}; "
            f"reserve={format_memory_size(plan.reserve_bytes)}; frame={frame_text}; canvas={canvas_text}; "
            f"fixed={format_memory_size(estimate.fixed_bytes)}; "
            f"per-worker={format_memory_size(estimate.per_worker_bytes)}; "
            f"projected={format_memory_size(projected)}; selected={plan.initial_workers}; {plan.reason}"
        )
    return (
        f"[workers] explicit={plan.initial_workers}; available RAM={format_memory_size(plan.available_bytes)}; "
        f"frame={frame_text}; canvas={canvas_text}; projected={format_memory_size(projected)}; "
        "the user setting takes precedence over the estimate"
    )


def order_statistic_image_shape(source_shape: tuple[int, ...], canvas: StackCanvas) -> tuple[int, ...]:
    if len(source_shape) == 2:
        return canvas.shape
    if len(source_shape) == 3:
        return (int(source_shape[0]), *canvas.shape)
    raise ValueError(f"Unsupported order-statistic image shape: {source_shape}")


def select_median_tile_plan(
    requested: str | int,
    frame_count: int,
    image_shape: tuple[int, ...],
    target_mode: str,
    available_bytes: int | None = None,
) -> MedianTilePlan:
    """Choose full-width cube rows without using more than half of available RAM in auto mode."""
    if frame_count < 1:
        raise ValueError("Median tile planning requires at least one frame")
    if len(image_shape) == 2:
        channels = 1
        height, width = image_shape
    elif len(image_shape) == 3:
        channels, height, width = image_shape
    else:
        raise ValueError(f"Unsupported median image shape: {image_shape}")
    cube_count = 2 if target_mode == "moving" else 1
    bytes_per_row = (
        int(frame_count)
        * int(channels)
        * int(width)
        * np.dtype(np.float32).itemsize
        * cube_count
    )
    bytes_per_row += int(channels) * int(width) * np.dtype(np.uint32).itemsize * cube_count
    available = available_ram_bytes() if available_bytes is None else available_bytes
    budget: int | None = None
    if requested == "auto":
        if available is None:
            rows = min(int(height), MEDIAN_TILE_UNKNOWN_RAM_ROWS)
            reason = "available RAM could not be measured; conservative row fallback"
        else:
            budget = max(1, int(available * MEDIAN_TILE_RAM_FRACTION))
            rows = max(1, min(int(height), budget // max(bytes_per_row, 1)))
            if rows < height and rows >= 16:
                rows = max(16, rows // 16 * 16)
            reason = "largest full-width row tile fitting 50% of available RAM"
            if bytes_per_row > budget:
                reason = "one cube row exceeds 50% of available RAM; using the minimum"
    else:
        rows = min(int(height), int(requested))
        reason = "explicit user row count"
    return MedianTilePlan(
        requested=requested,
        initial_rows=rows,
        current_rows=rows,
        available_bytes=available,
        budget_bytes=budget,
        bytes_per_row=int(bytes_per_row),
        frame_count=int(frame_count),
        channels=int(channels),
        width=int(width),
        height=int(height),
        cube_count=cube_count,
        reason=reason,
        fallback_events=[],
    )


def describe_median_tile_plan(plan: MedianTilePlan) -> str:
    requested = "auto" if plan.requested == "auto" else str(plan.requested)
    tile_count = math.ceil(plan.height / plan.initial_rows)
    return (
        f"[median-tiles] requested={requested}; available RAM={format_memory_size(plan.available_bytes)}; "
        f"cube budget={format_memory_size(plan.budget_bytes)}; frames={plan.frame_count}; "
        f"channels={plan.channels}; width={plan.width}; cubes={plan.cube_count}; "
        f"rows={plan.initial_rows}; tiles={tile_count}; "
        f"working cubes={format_memory_size(plan.cube_bytes(plan.initial_rows))}; {plan.reason}"
    )


def ordered_bounded_map(function, items, max_workers: int):
    """Run at most ``max_workers`` frame jobs while yielding input order."""
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    iterator = iter(items)
    if max_workers == 1:
        for item in iterator:
            yield function(item)
        return

    pending: deque[tuple[object, Future]] = deque()
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="metcalf-frame") as executor:
        for _ in range(max_workers):
            try:
                item = next(iterator)
            except StopIteration:
                break
            pending.append((item, executor.submit(function, item)))
        while pending:
            _item, future = pending.popleft()
            yield future.result()
            try:
                item = next(iterator)
            except StopIteration:
                continue
            pending.append((item, executor.submit(function, item)))


def run_worker_batch(function, batch: list[object], max_workers: int) -> list[object]:
    """Finish a whole batch before exposing any result to the accumulator."""
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    if not batch:
        return []
    if max_workers == 1:
        return [function(item) for item in batch]

    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="metcalf-frame")
    futures: list[Future] = []
    results: list[object] = []
    try:
        futures = [executor.submit(function, item) for item in batch]
        # Keep results local until every future succeeds. This is the transaction
        # boundary that prevents a failed batch from dirtying global accumulators.
        results = [future.result() for future in futures]
        return results
    except BaseException:
        for future in futures:
            future.cancel()
        results.clear()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def lower_stack_worker_count(workers: int) -> int | None:
    if workers == 4:
        return 2
    if workers == 2:
        return 1
    return None


def adaptive_ordered_bounded_map(function, items, plan: StackWorkerPlan, on_fallback=None):
    """Yield committed batches in order, retrying an uncommitted batch after MemoryError."""
    sequence = list(items)
    offset = 0
    while offset < len(sequence):
        # Keep the transaction membership fixed even if its executor is reduced
        # from 4 -> 2 -> 1 workers. No subset becomes visible to the accumulator
        # until every item from this original batch succeeds together.
        batch = sequence[offset : offset + plan.current_workers]
        while True:
            try:
                results = run_worker_batch(function, batch, plan.current_workers)
                break
            except MemoryError as error:
                error_text = str(error) or error.__class__.__name__
                # A Future keeps its exception traceback, and that traceback can retain
                # successful sibling results through the dead batch frame. Break it before
                # retrying so the lower-worker attempt starts after local arrays are freed.
                error.__traceback__ = None
                previous_workers = plan.current_workers
                next_workers = lower_stack_worker_count(previous_workers)
                if next_workers is None:
                    raise MemoryError(
                        "A frame allocation failed with one stack worker. Close other applications, "
                        "reduce the image size, or use a machine with more available RAM."
                    ) from error
                # run_worker_batch waits for the executor before returning here. No result
                # from this batch has been yielded, so only earlier batches are committed.
                plan.current_workers = next_workers
                event = {
                    "batch_offset": offset,
                    "batch_first_frame": getattr(batch[0], "index", offset + 1),
                    "batch_size": len(batch),
                    "from_workers": previous_workers,
                    "to_workers": next_workers,
                    "available_bytes_after_failure": available_ram_bytes(),
                    "error": error_text,
                }
                plan.fallback_events.append(event)
                gc.collect()
                if on_fallback is not None:
                    on_fallback(event)
        for result in results:
            yield result
        offset += len(batch)


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


@lru_cache(maxsize=8)
def background_axes(height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Cache separable normalized coordinates without retaining full-frame term grids."""
    x = np.linspace(-1.0, 1.0, width, dtype=np.float64)
    y = np.linspace(-1.0, 1.0, height, dtype=np.float64)
    x.setflags(write=False)
    y.setflags(write=False)
    return x, y


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
    tile_x = np.arange(padded_width, dtype=np.float64).reshape(BACKGROUND_TILE_COLUMNS, tile_width)
    tile_x = tile_x[:, ::BACKGROUND_TILE_SAMPLE_STEP]
    tile_y = np.arange(padded_height, dtype=np.float64).reshape(BACKGROUND_TILE_ROWS, tile_height)
    tile_y = tile_y[:, ::BACKGROUND_TILE_SAMPLE_STEP]
    sampled_shape = (
        BACKGROUND_TILE_ROWS,
        BACKGROUND_TILE_COLUMNS,
        sampled_tile_height,
        sampled_tile_width,
    )
    sampled_x = np.broadcast_to(tile_x[np.newaxis, :, np.newaxis, :], sampled_shape).reshape(
        -1, sampled_tile_height * sampled_tile_width
    )
    sampled_y = np.broadcast_to(tile_y[:, np.newaxis, :, np.newaxis], sampled_shape).reshape(
        -1, sampled_tile_height * sampled_tile_width
    )
    values_by_channel: list[np.ndarray] = []
    active_tiles = sampled_weights > 0
    for plane in planes:
        padded = np.full((padded_height, padded_width), np.nan, dtype=np.float64)
        padded[:height, :width] = np.where(valid_mask, plane, np.nan)
        tiles = padded.reshape(BACKGROUND_TILE_ROWS, tile_height, BACKGROUND_TILE_COLUMNS, tile_width)
        tiles = tiles[:, ::BACKGROUND_TILE_SAMPLE_STEP, :, ::BACKGROUND_TILE_SAMPLE_STEP]
        tiles = tiles.transpose(0, 2, 1, 3).reshape(-1, sampled_tile_height * sampled_tile_width)
        initial = np.full(tiles.shape[0], np.nan, dtype=np.float64)
        initial[active_tiles] = np.nanmedian(tiles[active_tiles], axis=1)
        mad = np.full(tiles.shape[0], np.nan, dtype=np.float64)
        mad[active_tiles] = np.nanmedian(
            np.abs(tiles[active_tiles] - initial[active_tiles, np.newaxis]),
            axis=1,
        )
        robust_sigma = 1.4826 * mad
        keep = np.isfinite(tiles)
        clipping_tiles = active_tiles & (robust_sigma > 0.0)
        if np.any(clipping_tiles):
            keep[clipping_tiles] &= (
                np.abs(tiles[clipping_tiles] - initial[clipping_tiles, np.newaxis])
                <= BACKGROUND_SIGMA * robust_sigma[clipping_tiles, np.newaxis]
            )
        channel_values = np.full(tiles.shape[0], np.nan, dtype=np.float64)
        channel_values[active_tiles] = np.nanmedian(
            np.where(keep[active_tiles], tiles[active_tiles], np.nan),
            axis=1,
        )
        values_by_channel.append(channel_values)

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
    for channel, offset in enumerate(offsets):
        np.add(result[channel], offset, out=result[channel], where=valid_mask)
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
    result = planes.astype(np.float64, copy=True)
    height, width = valid_mask.shape
    x, y = background_axes(height, width)
    x_row = x[np.newaxis, :]
    y_column = y[:, np.newaxis]
    x_squared = (x * x)[np.newaxis, :]
    y_squared = (y * y)[:, np.newaxis]
    for channel, coefficient in enumerate(coefficients):
        terms = (
            (float(coefficient[0]), None),
            (float(coefficient[1]), x_row),
            (float(coefficient[2]), y_column),
        )
        for scalar, basis in terms:
            value = scalar if basis is None else scalar * basis
            np.add(result[channel], value, out=result[channel], where=valid_mask)
        if mode == "quadratic":
            for scalar, basis in (
                (float(coefficient[3]), x_squared),
                (float(coefficient[5]), y_squared),
            ):
                np.add(result[channel], scalar * basis, out=result[channel], where=valid_mask)
            cross_term = (float(coefficient[4]) * y_column) * x_row
            np.add(result[channel], cross_term, out=result[channel], where=valid_mask)
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


def shift_image(
    data: np.ndarray,
    dx: float,
    dy: float,
    source_valid: np.ndarray | None = None,
    canvas: StackCanvas | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    output_canvas = canvas or StackCanvas.reference_footprint(data.shape[-2:])
    plan = build_bilinear_translation_plan(data.shape[-2:], dx, dy, source_valid, output_canvas)
    if data.ndim == 2:
        shifted, mask = apply_bilinear_translation_plan(data, plan)
        return shifted, mask
    planes = []
    for plane in data:
        shifted, _mask = apply_bilinear_translation_plan(plane, plan)
        planes.append(shifted)
    return np.stack(planes, axis=0), plan.valid_mask


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


def matrix3(values: tuple[float, ...] | np.ndarray) -> np.ndarray:
    matrix = np.asarray(values, dtype=np.float64)
    if matrix.size != 9:
        raise ValueError(f"Registration matrix must contain 9 values, received {matrix.size}")
    matrix = matrix.reshape(3, 3)
    if not np.all(np.isfinite(matrix)):
        raise ValueError("Registration matrix contains a non-finite value")
    return matrix


def translation_matrix(dx: float, dy: float) -> np.ndarray:
    return np.asarray(((1.0, 0.0, dx), (0.0, 1.0, dy), (0.0, 0.0, 1.0)), dtype=np.float64)


def siril_matrix_to_array_coordinates(
    matrix: tuple[float, ...] | np.ndarray,
    source_shape: tuple[int, int],
    registration_shape: tuple[int, int],
) -> np.ndarray:
    """Convert Siril's bottom-origin registration matrix to FITS-array row coordinates."""
    source_height, _source_width = source_shape
    registration_height, _registration_width = registration_shape
    source_flip = np.asarray(
        ((1.0, 0.0, 0.0), (0.0, -1.0, source_height - 1.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    registration_flip = np.asarray(
        ((1.0, 0.0, 0.0), (0.0, -1.0, registration_height - 1.0), (0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    # Both flip matrices are self-inverse. Siril's H maps source to reference.
    return registration_flip @ matrix3(matrix) @ source_flip


def compose_output_transform(
    source_to_registration: tuple[float, ...] | np.ndarray,
    dx: float,
    dy: float,
) -> np.ndarray:
    """Map source pixels directly to the requested output alignment in registration coordinates."""
    return translation_matrix(dx, dy) @ matrix3(source_to_registration)


def _normalize_bilinear_axis(
    coordinate: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    low = np.floor(coordinate).astype(np.int32)
    weight = coordinate - low
    near_low = np.abs(weight) < 1.0e-9
    near_high = np.abs(1.0 - weight) < 1.0e-9
    if np.any(near_high):
        low[near_high] += 1
        weight[near_high] = 0.0
    weight[near_low] = 0.0
    high = low + 1
    integral = weight == 0.0
    high[integral] = low[integral]
    return low, high, weight


def affine_valid_mask(
    source_shape: tuple[int, int],
    output_to_source: np.ndarray,
    source_valid: np.ndarray | None,
    output_shape: tuple[int, int],
    *,
    tile_rows: int = 96,
) -> np.ndarray:
    """Return the exact four-neighbour bilinear support mask for an affine transform."""
    source_height, source_width = source_shape
    output_height, output_width = output_shape
    output_x = np.arange(output_width, dtype=np.float64)
    valid_output = np.zeros(output_shape, dtype=bool)
    rows_per_tile = max(1, int(tile_rows))
    for output_y0 in range(0, output_height, rows_per_tile):
        output_y1 = min(output_height, output_y0 + rows_per_tile)
        output_y = np.arange(output_y0, output_y1, dtype=np.float64)[:, np.newaxis]
        source_x = (
            output_to_source[0, 0] * output_x[np.newaxis, :]
            + output_to_source[0, 1] * output_y
            + output_to_source[0, 2]
        )
        source_y = (
            output_to_source[1, 0] * output_x[np.newaxis, :]
            + output_to_source[1, 1] * output_y
            + output_to_source[1, 2]
        )
        x0, x1, _wx = _normalize_bilinear_axis(source_x)
        y0, y1, _wy = _normalize_bilinear_axis(source_y)
        valid = (
            np.isfinite(source_x)
            & np.isfinite(source_y)
            & (x0 >= 0)
            & (y0 >= 0)
            & (x1 < source_width)
            & (y1 < source_height)
        )
        if source_valid is not None and np.any(valid):
            positions = np.flatnonzero(valid)
            py = positions // output_width
            px = positions % output_width
            valid[py, px] = (
                source_valid[y0[py, px], x0[py, px]]
                & source_valid[y0[py, px], x1[py, px]]
                & source_valid[y1[py, px], x0[py, px]]
                & source_valid[y1[py, px], x1[py, px]]
            )
        valid_output[output_y0:output_y1] = valid
    return valid_output


def pillow_affine_coefficients(output_to_source: np.ndarray) -> tuple[float, ...]:
    """Convert array-index coordinates to Pillow's pixel-centre affine convention."""
    adjusted = np.asarray(output_to_source, dtype=np.float64).copy()
    adjusted[:2, 2] += 0.5 - 0.5 * np.sum(adjusted[:2, :2], axis=1)
    return tuple(float(value) for value in adjusted[:2].ravel())


def resample_affine_with_pillow(
    planes: np.ndarray,
    output_to_source: np.ndarray,
    source_valid: np.ndarray | None,
    output_shape: tuple[int, int],
    *,
    tile_rows: int = 96,
) -> tuple[np.ndarray, np.ndarray]:
    """Use Pillow's C bilinear kernel while retaining our strict validity semantics."""
    output_height, output_width = output_shape
    coefficients = pillow_affine_coefficients(output_to_source)
    transformed_planes = []
    for plane in planes:
        source = Image.fromarray(np.asarray(plane, dtype=np.float32))
        transformed = source.transform(
            (output_width, output_height),
            Image.Transform.AFFINE,
            coefficients,
            resample=Image.Resampling.BILINEAR,
            fillcolor=0.0,
        )
        transformed_planes.append(np.asarray(transformed, dtype=np.float32))
    output = np.stack(transformed_planes, axis=0)
    valid = affine_valid_mask(
        planes.shape[-2:],
        output_to_source,
        source_valid,
        output_shape,
        tile_rows=tile_rows,
    )
    output[:, ~valid] = 0.0
    return output, valid


def resample_affine(
    data: np.ndarray,
    source_to_output: tuple[float, ...] | np.ndarray,
    source_valid: np.ndarray | None,
    canvas: StackCanvas,
    *,
    tile_rows: int = 96,
) -> tuple[np.ndarray, np.ndarray]:
    """Inverse-sample an affine/projective source transform onto an independent canvas."""
    planes = data[np.newaxis, :, :] if data.ndim == 2 else data
    if planes.ndim != 3:
        raise ValueError(f"Affine resampling expects a 2D or CHW image, received shape {data.shape}")
    _channels, source_height, source_width = planes.shape
    if source_valid is not None and source_valid.shape != (source_height, source_width):
        raise ValueError(
            f"Validity mask shape changed: {source_valid.shape} != {(source_height, source_width)}"
        )
    transform = matrix3(source_to_output)
    if (
        canvas.is_identity_for((source_height, source_width))
        and np.allclose(transform, np.eye(3), rtol=0.0, atol=1.0e-10)
    ):
        valid = np.ones(canvas.shape, dtype=bool) if source_valid is None else source_valid.copy()
        result = planes.astype(np.float64, copy=True)
        return (result[0] if data.ndim == 2 else result), valid

    # Preserve the optimized slice path for exact pure translations.
    if np.allclose(transform[:2, :2], np.eye(2), rtol=0.0, atol=1.0e-12) and np.allclose(
        transform[2], (0.0, 0.0, 1.0), rtol=0.0, atol=1.0e-12
    ):
        return shift_image(data, float(transform[0, 2]), float(transform[1, 2]), source_valid, canvas)

    try:
        inverse = np.linalg.inv(transform)
    except np.linalg.LinAlgError as error:
        raise ValueError("Registration transform is singular") from error

    affine = np.allclose(inverse[2], (0.0, 0.0, 1.0), rtol=0.0, atol=1.0e-12)
    if affine:
        output_array_to_registration = translation_matrix(canvas.origin_x, canvas.origin_y)
        output_to_source = inverse @ output_array_to_registration
        output, valid_output = resample_affine_with_pillow(
            planes,
            output_to_source,
            source_valid,
            canvas.shape,
            tile_rows=tile_rows,
        )
        return (output[0] if data.ndim == 2 else output), valid_output

    output_height, output_width = canvas.shape
    output = np.zeros((planes.shape[0], output_height, output_width), dtype=np.float32)
    valid_output = np.zeros(canvas.shape, dtype=bool)
    output_x = np.arange(output_width, dtype=np.float64) + canvas.origin_x
    rows_per_tile = max(1, int(tile_rows))
    for output_y0 in range(0, output_height, rows_per_tile):
        output_y1 = min(output_height, output_y0 + rows_per_tile)
        output_y = np.arange(output_y0, output_y1, dtype=np.float64)[:, np.newaxis] + canvas.origin_y
        source_x = (
            inverse[0, 0] * output_x[np.newaxis, :] + inverse[0, 1] * output_y + inverse[0, 2]
        )
        source_y = (
            inverse[1, 0] * output_x[np.newaxis, :] + inverse[1, 1] * output_y + inverse[1, 2]
        )
        if affine:
            finite_denominator: bool | np.ndarray = True
        else:
            denominator = inverse[2, 0] * output_x[np.newaxis, :] + inverse[2, 1] * output_y + inverse[2, 2]
            finite_denominator = np.isfinite(denominator) & (np.abs(denominator) > 1.0e-15)
            safe_denominator = np.where(finite_denominator, denominator, 1.0)
            source_x /= safe_denominator
            source_y /= safe_denominator
        x0, x1, wx = _normalize_bilinear_axis(source_x)
        y0, y1, wy = _normalize_bilinear_axis(source_y)
        valid = (
            finite_denominator
            & np.isfinite(source_x)
            & np.isfinite(source_y)
            & (x0 >= 0)
            & (y0 >= 0)
            & (x1 < source_width)
            & (y1 < source_height)
        )
        if source_valid is not None and np.any(valid):
            positions = np.flatnonzero(valid)
            tile_width = output_width
            py = positions // tile_width
            px = positions % tile_width
            kernel_valid = (
                source_valid[y0[py, px], x0[py, px]]
                & source_valid[y0[py, px], x1[py, px]]
                & source_valid[y1[py, px], x0[py, px]]
                & source_valid[y1[py, px], x1[py, px]]
            )
            valid[py, px] = kernel_valid
        target_valid = valid_output[output_y0:output_y1]
        target_valid[:] = valid
        if not np.any(valid):
            continue
        valid_x0 = x0[valid]
        valid_x1 = x1[valid]
        valid_y0 = y0[valid]
        valid_y1 = y1[valid]
        valid_wx = wx[valid]
        valid_wy = wy[valid]
        weight00 = (1.0 - valid_wx) * (1.0 - valid_wy)
        weight10 = valid_wx * (1.0 - valid_wy)
        weight01 = (1.0 - valid_wx) * valid_wy
        weight11 = valid_wx * valid_wy
        for channel, plane in enumerate(planes):
            blended = (
                weight00 * plane[valid_y0, valid_x0]
                + weight10 * plane[valid_y0, valid_x1]
                + weight01 * plane[valid_y1, valid_x0]
                + weight11 * plane[valid_y1, valid_x1]
            )
            output[channel, output_y0:output_y1][valid] = blended
    return (output[0] if data.ndim == 2 else output), valid_output


def resample_boolean_affine(
    mask: np.ndarray,
    source_to_output: tuple[float, ...] | np.ndarray,
    canvas: StackCanvas,
) -> np.ndarray:
    """Conservatively mark output pixels touched by a transformed true source pixel."""
    transformed, valid = resample_affine(mask.astype(np.float32, copy=False), source_to_output, None, canvas)
    return valid & (transformed > 0.0)


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
    canvas: StackCanvas | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    output_canvas = canvas or StackCanvas.reference_footprint(data.shape)
    plan = build_bilinear_translation_plan(data.shape, dx, dy, source_valid, output_canvas)
    return apply_bilinear_translation_plan(data, plan)


def build_bilinear_translation_plan(
    source_shape: tuple[int, int],
    dx: float,
    dy: float,
    source_valid: np.ndarray | None,
    canvas: StackCanvas,
) -> BilinearTranslationPlan:
    """Map a registered source image onto an independently defined output canvas."""
    source_height, source_width = source_shape
    if source_valid is not None and source_valid.shape != source_shape:
        raise ValueError(f"Validity mask shape changed: {source_valid.shape} != {source_shape}")
    output_height, output_width = canvas.shape
    identity = (
        canvas.is_identity_for(source_shape)
        and abs(dx) < 1.0e-9
        and abs(dy) < 1.0e-9
    )
    if identity:
        valid = np.ones(source_shape, dtype=bool) if source_valid is None else source_valid.copy()
        return BilinearTranslationPlan(
            output_shape=canvas.shape,
            output_slice=(slice(0, source_height), slice(0, source_width)),
            source_y0=0,
            source_y1=source_height,
            source_x0=0,
            source_x1=source_width,
            weight_x=0.0,
            weight_y=0.0,
            valid_mask=valid,
            identity=True,
        )

    # A row tile of an unshifted frame is an exact crop, not a bilinear
    # translation. Preserve every source edge while keeping the legacy support
    # semantics for actual nonzero shifts.
    rounded_origin_x = int(round(canvas.origin_x))
    rounded_origin_y = int(round(canvas.origin_y))
    direct_crop = (
        abs(dx) < 1.0e-9
        and abs(dy) < 1.0e-9
        and abs(canvas.origin_x - rounded_origin_x) < 1.0e-9
        and abs(canvas.origin_y - rounded_origin_y) < 1.0e-9
        and rounded_origin_x >= 0
        and rounded_origin_y >= 0
        and rounded_origin_x + output_width <= source_width
        and rounded_origin_y + output_height <= source_height
    )
    if direct_crop:
        output_slice = (slice(0, output_height), slice(0, output_width))
        valid = (
            np.ones(canvas.shape, dtype=bool)
            if source_valid is None
            else source_valid[
                rounded_origin_y : rounded_origin_y + output_height,
                rounded_origin_x : rounded_origin_x + output_width,
            ].copy()
        )
        return BilinearTranslationPlan(
            output_shape=canvas.shape,
            output_slice=output_slice,
            source_y0=rounded_origin_y,
            source_y1=rounded_origin_y + output_height,
            source_x0=rounded_origin_x,
            source_x1=rounded_origin_x + output_width,
            weight_x=0.0,
            weight_y=0.0,
            valid_mask=valid,
        )

    # Canvas pixel (u, v) represents registration coordinate
    # (u + origin_x, v + origin_y). Inverse sampling therefore reads source
    # coordinate (u + origin_x - dx, v + origin_y - dy).
    source_x_offset = int(math.floor(canvas.origin_x - dx))
    source_y_offset = int(math.floor(canvas.origin_y - dy))
    weight_x = float(canvas.origin_x - dx - source_x_offset)
    weight_y = float(canvas.origin_y - dy - source_y_offset)
    output_x0 = max(0, -source_x_offset)
    output_x1 = min(output_width, source_width - 1 - source_x_offset)
    output_y0 = max(0, -source_y_offset)
    output_y1 = min(output_height, source_height - 1 - source_y_offset)
    valid = np.zeros(canvas.shape, dtype=bool)
    if output_x1 <= output_x0 or output_y1 <= output_y0:
        return BilinearTranslationPlan(
            output_shape=canvas.shape,
            output_slice=None,
            source_y0=0,
            source_y1=0,
            source_x0=0,
            source_x1=0,
            weight_x=weight_x,
            weight_y=weight_y,
            valid_mask=valid,
        )

    source_x0 = output_x0 + source_x_offset
    source_x1 = output_x1 + source_x_offset
    source_y0 = output_y0 + source_y_offset
    source_y1 = output_y1 + source_y_offset
    output_slice = (slice(output_y0, output_y1), slice(output_x0, output_x1))
    valid_region = np.ones((output_y1 - output_y0, output_x1 - output_x0), dtype=bool)
    if source_valid is not None:
        valid_region &= source_valid[source_y0:source_y1, source_x0:source_x1]
        valid_region &= source_valid[source_y0:source_y1, source_x0 + 1 : source_x1 + 1]
        valid_region &= source_valid[source_y0 + 1 : source_y1 + 1, source_x0:source_x1]
        valid_region &= source_valid[source_y0 + 1 : source_y1 + 1, source_x0 + 1 : source_x1 + 1]
    valid[output_slice] = valid_region
    return BilinearTranslationPlan(
        output_shape=canvas.shape,
        output_slice=output_slice,
        source_y0=source_y0,
        source_y1=source_y1,
        source_x0=source_x0,
        source_x1=source_x1,
        weight_x=weight_x,
        weight_y=weight_y,
        valid_mask=valid,
    )


def apply_bilinear_translation_plan(
    data: np.ndarray,
    plan: BilinearTranslationPlan,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply a pure-translation plan without constructing full coordinate grids."""
    if data.ndim != 2:
        raise ValueError(f"Translation plan expects a 2D plane, received shape {data.shape}")
    if plan.identity:
        return data.astype(np.float64, copy=True), plan.valid_mask
    output = np.zeros(plan.output_shape, dtype=np.float32)
    if plan.output_slice is None or not np.any(plan.valid_mask):
        return output, plan.valid_mask
    y0, y1 = plan.source_y0, plan.source_y1
    x0, x1 = plan.source_x0, plan.source_x1
    wx, wy = plan.weight_x, plan.weight_y
    if wx == 0.0 and wy == 0.0:
        blended = data[y0:y1, x0:x1]
    elif wx == 0.0:
        blended = (1.0 - wy) * data[y0:y1, x0:x1] + wy * data[y0 + 1 : y1 + 1, x0:x1]
    elif wy == 0.0:
        blended = (1.0 - wx) * data[y0:y1, x0:x1] + wx * data[y0:y1, x0 + 1 : x1 + 1]
    else:
        blended = (
            (1.0 - wx) * (1.0 - wy) * data[y0:y1, x0:x1]
            + wx * (1.0 - wy) * data[y0:y1, x0 + 1 : x1 + 1]
            + (1.0 - wx) * wy * data[y0 + 1 : y1 + 1, x0:x1]
            + wx * wy * data[y0 + 1 : y1 + 1, x0 + 1 : x1 + 1]
        )
    target = output[plan.output_slice]
    valid_region = plan.valid_mask[plan.output_slice]
    target[valid_region] = blended[valid_region]
    return output, plan.valid_mask


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
    where = mask2d[np.newaxis, :, :] if image.ndim == 3 else mask2d
    np.add(sum_image, image, out=sum_image, where=where)
    np.add(count_image, mask2d, out=count_image, casting="unsafe")
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


def process_stack_frame(
    task: StackFrameTask,
    canvas: StackCanvas,
    padding_policy: str,
    background_mode: str,
    saturation_enabled: bool,
    saturation_threshold_percent: float,
    target_mode: str = "moving",
) -> StackFrameResult:
    """Read and transform one frame without mutating any stack accumulator."""
    timings = {
        "fits_read": 0.0,
        "background_fit": 0.0,
        "background_apply": 0.0,
        "star_resample": 0.0,
        "metcalf_shift": 0.0,
        "saturation": 0.0,
    }
    started = time.perf_counter()
    image, prepared_unit_scale = restore_registered_units(
        read_fits(task.prepared),
        task.source_header,
    )
    source_valid = registered_valid_mask(image.data) if padding_policy == "valid" else None
    resample_source_valid = (
        None if source_valid is None or bool(np.all(source_valid)) else source_valid
    )
    timings["fits_read"] = time.perf_counter() - started

    saturation_level: float | None = None
    saturation_threshold_count: float | None = None
    subframe_max_count: float | None = None
    saturated_pixel_count = 0
    frame_saturation_mask: np.ndarray | None = None
    if saturation_enabled:
        started = time.perf_counter()
        (
            frame_saturation_mask,
            saturation_level,
            saturation_threshold_count,
            subframe_max_count,
        ) = detect_saturation(image.data, task.source_header, saturation_threshold_percent)
        saturated_pixel_count = int(np.count_nonzero(frame_saturation_mask))
        timings["saturation"] = time.perf_counter() - started

    background_model: BackgroundModel | None = None
    background_correction: np.ndarray | None = None
    stack_data = image.data
    if background_mode != "none":
        if source_valid is None:
            raise RuntimeError(
                f"Background normalization requires a validity mask for frame {task.index} ({task.source_name})"
            )
        started = time.perf_counter()
        try:
            background_model = fit_background_surface(image.data, source_valid, background_mode)
        except ValueError as error:
            raise RuntimeError(
                f"Cannot estimate the background of usable frame {task.index} ({task.source_name}): {error}"
            ) from error
        timings["background_fit"] = time.perf_counter() - started
        background_correction = -background_model.coefficients
        started = time.perf_counter()
        stack_data = apply_background_model(
            image.data,
            source_valid,
            background_correction,
            background_mode,
        )
        timings["background_apply"] = time.perf_counter() - started

    source_to_registration = matrix3(task.source_to_registration)
    if canvas.is_identity_for(stack_data.shape[-2:]) and np.allclose(
        source_to_registration, np.eye(3), rtol=0.0, atol=1.0e-10
    ):
        star_data = stack_data
        star_mask = np.ones(canvas.shape, dtype=bool) if source_valid is None else source_valid
    else:
        started = time.perf_counter()
        star_data, star_mask = resample_affine(
            stack_data,
            source_to_registration,
            resample_source_valid,
            canvas,
        )
        timings["star_resample"] = time.perf_counter() - started

    if target_mode == "fixed":
        metcalf_transform = source_to_registration
        metcalf_data = star_data
        metcalf_mask = star_mask
    else:
        started = time.perf_counter()
        metcalf_transform = compose_output_transform(source_to_registration, task.dx, task.dy)
        metcalf_data, metcalf_mask = resample_affine(
            stack_data,
            metcalf_transform,
            resample_source_valid,
            canvas,
        )
        timings["metcalf_shift"] = time.perf_counter() - started

    star_saturation_mask: np.ndarray | None = None
    metcalf_saturation_mask: np.ndarray | None = None
    if frame_saturation_mask is not None and saturated_pixel_count > 0:
        started = time.perf_counter()
        star_saturation_mask = resample_boolean_affine(
            frame_saturation_mask,
            source_to_registration,
            canvas,
        ) & star_mask
        if target_mode == "fixed":
            metcalf_saturation_mask = star_saturation_mask
        else:
            metcalf_saturation_mask = resample_boolean_affine(
                frame_saturation_mask,
                metcalf_transform,
                canvas,
            ) & metcalf_mask
        timings["saturation"] += time.perf_counter() - started

    return StackFrameResult(
        task=task,
        star_data=star_data,
        star_mask=star_mask,
        metcalf_data=metcalf_data,
        metcalf_mask=metcalf_mask,
        prepared_unit_scale=prepared_unit_scale,
        background_model=background_model,
        background_correction=background_correction,
        star_saturation_mask=star_saturation_mask,
        metcalf_saturation_mask=metcalf_saturation_mask,
        saturation_level=saturation_level,
        saturation_threshold_count=saturation_threshold_count,
        subframe_max_count=subframe_max_count,
        saturated_pixel_count=saturated_pixel_count,
        timings=timings,
    )


def analyze_order_statistic_frame(
    task: StackFrameTask,
    padding_policy: str,
    background_mode: str,
    saturation_enabled: bool,
    saturation_threshold_percent: float,
) -> StackFrameAnalysis:
    """Evaluate frame-wide models before allocating any median/rank-fit cube."""
    timings = {
        "fits_read": 0.0,
        "background_fit": 0.0,
        "saturation": 0.0,
    }
    started = time.perf_counter()
    image, prepared_unit_scale = restore_registered_units(read_fits(task.prepared), task.source_header)
    source_valid = registered_valid_mask(image.data) if padding_policy == "valid" else None
    timings["fits_read"] = time.perf_counter() - started

    saturation_level: float | None = None
    saturation_threshold_count: float | None = None
    subframe_max_count: float | None = None
    saturated_pixel_count = 0
    if saturation_enabled:
        started = time.perf_counter()
        saturation_mask, saturation_level, saturation_threshold_count, subframe_max_count = detect_saturation(
            image.data,
            task.source_header,
            saturation_threshold_percent,
        )
        saturated_pixel_count = int(np.count_nonzero(saturation_mask))
        timings["saturation"] = time.perf_counter() - started

    background_model: BackgroundModel | None = None
    background_correction: np.ndarray | None = None
    if background_mode != "none":
        if source_valid is None:
            raise RuntimeError(
                f"Background normalization requires a validity mask for frame {task.index} ({task.source_name})"
            )
        started = time.perf_counter()
        try:
            background_model = fit_background_surface(image.data, source_valid, background_mode)
        except ValueError as error:
            raise RuntimeError(
                f"Cannot estimate the background of usable frame {task.index} ({task.source_name}): {error}"
            ) from error
        timings["background_fit"] = time.perf_counter() - started
        background_correction = -background_model.coefficients

    return StackFrameAnalysis(
        task=task,
        prepared_unit_scale=prepared_unit_scale,
        background_model=background_model,
        background_correction=background_correction,
        saturation_level=saturation_level,
        saturation_threshold_count=saturation_threshold_count,
        subframe_max_count=subframe_max_count,
        saturated_pixel_count=saturated_pixel_count,
        timings=timings,
    )


def process_stack_tile_frame(
    task: StackFrameTask,
    analysis: StackFrameAnalysis,
    canvas: StackCanvas,
    padding_policy: str,
    background_mode: str,
    saturation_enabled: bool,
    target_mode: str = "moving",
) -> StackTileFrameResult:
    """Read one frame and produce only the requested output-row tile."""
    timings = {
        "fits_read": 0.0,
        "background_apply": 0.0,
        "star_resample": 0.0,
        "metcalf_shift": 0.0,
        "saturation": 0.0,
    }
    started = time.perf_counter()
    image, prepared_unit_scale = restore_registered_units(read_fits(task.prepared), task.source_header)
    if not math.isclose(prepared_unit_scale, analysis.prepared_unit_scale, rel_tol=0.0, abs_tol=1.0e-12):
        raise RuntimeError(f"Registered-unit scale changed while rereading frame {task.index}")
    source_valid = registered_valid_mask(image.data) if padding_policy == "valid" else None
    resample_source_valid = None if source_valid is None or bool(np.all(source_valid)) else source_valid
    timings["fits_read"] = time.perf_counter() - started

    stack_data = image.data
    if background_mode != "none":
        if source_valid is None or analysis.background_correction is None:
            raise RuntimeError(f"Background analysis is missing for usable frame {task.index}")
        started = time.perf_counter()
        stack_data = apply_background_model(
            image.data,
            source_valid,
            analysis.background_correction,
            background_mode,
        )
        timings["background_apply"] = time.perf_counter() - started

    source_to_registration = matrix3(task.source_to_registration)
    if canvas.is_identity_for(stack_data.shape[-2:]) and np.allclose(
        source_to_registration, np.eye(3), rtol=0.0, atol=1.0e-10
    ):
        star_data = stack_data
        star_mask = np.ones(canvas.shape, dtype=bool) if source_valid is None else source_valid
    else:
        started = time.perf_counter()
        star_data, star_mask = resample_affine(
            stack_data,
            source_to_registration,
            resample_source_valid,
            canvas,
        )
        timings["star_resample"] = time.perf_counter() - started

    if target_mode == "fixed":
        metcalf_transform = source_to_registration
        metcalf_data = star_data
        metcalf_mask = star_mask
    else:
        started = time.perf_counter()
        metcalf_transform = compose_output_transform(source_to_registration, task.dx, task.dy)
        metcalf_data, metcalf_mask = resample_affine(
            stack_data,
            metcalf_transform,
            resample_source_valid,
            canvas,
        )
        timings["metcalf_shift"] = time.perf_counter() - started

    star_saturation_mask: np.ndarray | None = None
    metcalf_saturation_mask: np.ndarray | None = None
    if (
        saturation_enabled
        and analysis.saturated_pixel_count > 0
        and analysis.saturation_threshold_count is not None
    ):
        started = time.perf_counter()
        over = np.isfinite(image.data) & (image.data > analysis.saturation_threshold_count)
        frame_saturation_mask = np.any(over, axis=0) if image.data.ndim == 3 else over
        star_saturation_mask = resample_boolean_affine(
            frame_saturation_mask,
            source_to_registration,
            canvas,
        ) & star_mask
        if target_mode == "fixed":
            metcalf_saturation_mask = star_saturation_mask
        else:
            metcalf_saturation_mask = resample_boolean_affine(
                frame_saturation_mask,
                metcalf_transform,
                canvas,
            ) & metcalf_mask
        timings["saturation"] = time.perf_counter() - started

    return StackTileFrameResult(
        task=task,
        star_data=star_data,
        star_mask=star_mask,
        metcalf_data=metcalf_data,
        metcalf_mask=metcalf_mask,
        star_saturation_mask=star_saturation_mask,
        metcalf_saturation_mask=metcalf_saturation_mask,
        timings=timings,
    )


def add_order_statistic_sample(
    cube: np.ndarray,
    index: int,
    image: np.ndarray,
    mask2d: np.ndarray,
    exclude_zero_samples: bool,
    sample_counts: np.ndarray | None = None,
) -> None:
    """Store one tile in a preallocated cube, encoding missing samples as NaN."""
    destination = cube[index]
    if destination.shape != image.shape:
        raise ValueError(f"Order-statistic tile shape changed: {image.shape} != {destination.shape}")
    destination.fill(np.nan)
    valid = mask2d[np.newaxis, :, :] if image.ndim == 3 else mask2d
    if exclude_zero_samples:
        valid = valid & (image != 0.0)
    np.copyto(destination, image, where=valid, casting="unsafe")
    if sample_counts is not None:
        if sample_counts.shape != image.shape:
            raise ValueError(f"Order-statistic count shape changed: {sample_counts.shape} != {image.shape}")
        np.add(sample_counts, valid, out=sample_counts, casting="unsafe")


def finalize_median_cube(cube: np.ndarray, sample_counts: np.ndarray) -> np.ndarray:
    """Sort a tile cube in place and select its per-pixel finite median."""
    if sample_counts.shape != cube.shape[1:]:
        raise ValueError(f"Median sample-count shape changed: {sample_counts.shape} != {cube.shape[1:]}")
    cube.sort(axis=0)
    ordered = cube.reshape(cube.shape[0], -1)
    valid_counts = sample_counts.reshape(-1)
    median = np.zeros(ordered.shape[1], dtype=np.float64)
    for sample_count_value in np.unique(valid_counts):
        sample_count = int(sample_count_value)
        if sample_count == 0:
            continue
        pixels = valid_counts == sample_count
        middle = sample_count // 2
        if sample_count % 2:
            median[pixels] = ordered[middle, pixels]
        else:
            median[pixels] = (ordered[middle - 1, pixels] + ordered[middle, pixels]) / 2.0
    return median.reshape(cube.shape[1:])


def finalize_rankfit_cube(
    cube: np.ndarray,
    sample_counts: np.ndarray,
    fraction_percent: int,
    degree: int = 5,
) -> np.ndarray:
    """Sort an in-memory tile cube in place and evaluate its central rank fit."""
    if not 1 <= fraction_percent <= 100:
        raise ValueError("rank-fit fraction must be an integer from 1 to 100")
    if sample_counts.shape != cube.shape[1:]:
        raise ValueError(f"Rank-fit sample-count shape changed: {sample_counts.shape} != {cube.shape[1:]}")
    cube.sort(axis=0)
    ordered = cube.reshape(cube.shape[0], -1)
    valid_counts = sample_counts.reshape(-1)
    fitted = np.zeros(ordered.shape[1], dtype=np.float64)
    for sample_count_value in np.unique(valid_counts):
        sample_count = int(sample_count_value)
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
        full_rank = np.arange(sample_count, dtype=np.float64) - (sample_count - 1) / 2.0
        full_rank /= max(np.max(np.abs(full_rank)), 1.0)
        rank = full_rank[selected_start : selected_start + selected_count]
        design = np.polynomial.polynomial.polyvander(rank, degree)
        center_weights = np.linalg.pinv(design)[0]
        pixel_indices = np.flatnonzero(pixels)
        # Boolean selection of every tile pixel at once can create another
        # cube-sized temporary. Bound the mixed float32/float64 matrix-product
        # workspace independently of the selected tile height.
        bytes_per_pixel = selected_count * (
            np.dtype(np.float32).itemsize + np.dtype(np.float64).itemsize
        ) + np.dtype(np.float64).itemsize
        pixel_chunk = max(1, RANKFIT_WORKSPACE_BYTES // max(bytes_per_pixel, 1))
        for pixel_start in range(0, pixel_indices.size, pixel_chunk):
            chunk_indices = pixel_indices[pixel_start : pixel_start + pixel_chunk]
            selected = ordered[
                selected_start : selected_start + selected_count,
                chunk_indices,
            ]
            fitted[chunk_indices] = center_weights @ selected
    return fitted.reshape(cube.shape[1:])


def finalize_order_statistic_cube(
    cube: np.ndarray,
    sample_counts: np.ndarray,
    method: str,
    rankfit_fraction: int,
) -> np.ndarray:
    """Finalize one bounded cube, mutating it to avoid a second cube-sized allocation."""
    if method == "median":
        return finalize_median_cube(cube, sample_counts)
    if method == "rankfit":
        return finalize_rankfit_cube(cube, sample_counts, rankfit_fraction)
    raise ValueError(f"Unsupported order-statistic stack method: {method}")


def build_order_statistic_tile(
    tasks: list[StackFrameTask],
    analyses: dict[int, StackFrameAnalysis],
    source_shape: tuple[int, ...],
    canvas: StackCanvas,
    method: str,
    rankfit_fraction: int,
    exclude_zero_samples: bool,
    padding_policy: str,
    background_mode: str,
    saturation_enabled: bool,
    target_mode: str,
    worker_plan: StackWorkerPlan,
    on_worker_fallback=None,
    on_frame=None,
) -> OrderStatisticTileResult:
    """Build and combine one row tile without exposing partial results to the full stack."""
    timings = {
        "fits_read": 0.0,
        "background_apply": 0.0,
        "star_resample": 0.0,
        "star_accumulation": 0.0,
        "metcalf_shift": 0.0,
        "metcalf_accumulation": 0.0,
        "saturation": 0.0,
        "order_statistic_combine": 0.0,
    }
    tile_image_shape = order_statistic_image_shape(source_shape, canvas)
    star_cube = np.empty((len(tasks), *tile_image_shape), dtype=np.float32)
    metcalf_cube = (
        np.empty((len(tasks), *tile_image_shape), dtype=np.float32)
        if target_mode == "moving"
        else None
    )
    star_coverage = np.zeros(canvas.shape, dtype=np.uint32)
    metcalf_coverage = np.zeros(canvas.shape, dtype=np.uint32) if target_mode == "moving" else None
    star_sample_counts = np.zeros(tile_image_shape, dtype=np.uint32)
    metcalf_sample_counts = (
        np.zeros(tile_image_shape, dtype=np.uint32)
        if target_mode == "moving"
        else None
    )
    star_saturation_mask = np.zeros(canvas.shape, dtype=bool) if saturation_enabled else None
    metcalf_saturation_mask = (
        np.zeros(canvas.shape, dtype=bool)
        if saturation_enabled and target_mode == "moving"
        else None
    )

    worker = lambda task: process_stack_tile_frame(
        task,
        analyses[task.index],
        canvas,
        padding_policy,
        background_mode,
        saturation_enabled,
        target_mode,
    )
    for cube_index, result in enumerate(
        adaptive_ordered_bounded_map(worker, tasks, worker_plan, on_worker_fallback)
    ):
        if on_frame is not None:
            on_frame(cube_index + 1, result.task)
        for operation, elapsed in result.timings.items():
            timings[operation] += elapsed
        started = time.perf_counter()
        add_order_statistic_sample(
            star_cube,
            cube_index,
            result.star_data,
            result.star_mask,
            exclude_zero_samples,
            star_sample_counts,
        )
        np.add(star_coverage, result.star_mask, out=star_coverage, casting="unsafe")
        timings["star_accumulation"] += time.perf_counter() - started
        if target_mode == "moving":
            if metcalf_cube is None or metcalf_coverage is None:
                raise RuntimeError("Metcalf tile cube was not initialized")
            if metcalf_sample_counts is None:
                raise RuntimeError("Metcalf tile sample counts were not initialized")
            started = time.perf_counter()
            add_order_statistic_sample(
                metcalf_cube,
                cube_index,
                result.metcalf_data,
                result.metcalf_mask,
                exclude_zero_samples,
                metcalf_sample_counts,
            )
            np.add(metcalf_coverage, result.metcalf_mask, out=metcalf_coverage, casting="unsafe")
            timings["metcalf_accumulation"] += time.perf_counter() - started
        if star_saturation_mask is not None and result.star_saturation_mask is not None:
            star_saturation_mask |= result.star_saturation_mask
        if metcalf_saturation_mask is not None and result.metcalf_saturation_mask is not None:
            metcalf_saturation_mask |= result.metcalf_saturation_mask

    started = time.perf_counter()
    star_data = finalize_order_statistic_cube(star_cube, star_sample_counts, method, rankfit_fraction)
    metcalf_data = (
        finalize_order_statistic_cube(metcalf_cube, metcalf_sample_counts, method, rankfit_fraction)
        if metcalf_cube is not None and metcalf_sample_counts is not None
        else None
    )
    timings["order_statistic_combine"] += time.perf_counter() - started
    return OrderStatisticTileResult(
        star_data=star_data,
        star_coverage=star_coverage,
        metcalf_data=metcalf_data,
        metcalf_coverage=metcalf_coverage,
        star_saturation_mask=star_saturation_mask,
        metcalf_saturation_mask=metcalf_saturation_mask,
        timings=timings,
    )


def stack_order_statistic_rows(
    tasks: list[StackFrameTask],
    analyses: dict[int, StackFrameAnalysis],
    source_shape: tuple[int, ...],
    canvas: StackCanvas,
    method: str,
    rankfit_fraction: int,
    exclude_zero_samples: bool,
    padding_policy: str,
    background_mode: str,
    saturation_enabled: bool,
    target_mode: str,
    worker_plan: StackWorkerPlan,
    tile_plan: MedianTilePlan,
    on_worker_fallback=None,
    on_tile_fallback=None,
    on_tile_started=None,
    on_frame=None,
) -> OrderStatisticStackResult:
    """Assemble a full median/rank-fit product from bounded full-width row cubes."""
    image_shape = order_statistic_image_shape(source_shape, canvas)
    star_data = np.zeros(image_shape, dtype=np.float64)
    metcalf_data = np.zeros(image_shape, dtype=np.float64) if target_mode == "moving" else None
    star_coverage = np.zeros(canvas.shape, dtype=np.uint32)
    metcalf_coverage = np.zeros(canvas.shape, dtype=np.uint32) if target_mode == "moving" else None
    star_saturation_mask = np.zeros(canvas.shape, dtype=bool) if saturation_enabled else None
    metcalf_saturation_mask = (
        np.zeros(canvas.shape, dtype=bool)
        if saturation_enabled and target_mode == "moving"
        else None
    )
    timings: dict[str, float] = {
        "fits_read": 0.0,
        "background_apply": 0.0,
        "star_resample": 0.0,
        "star_accumulation": 0.0,
        "metcalf_shift": 0.0,
        "metcalf_accumulation": 0.0,
        "saturation": 0.0,
        "order_statistic_combine": 0.0,
    }
    row_start = 0
    tile_index = 0
    while row_start < canvas.shape[0]:
        row_count = min(tile_plan.current_rows, canvas.shape[0] - row_start)
        while True:
            row_end = row_start + row_count
            tile_canvas = StackCanvas(
                shape=(row_count, canvas.shape[1]),
                origin_x=canvas.origin_x,
                origin_y=canvas.origin_y + row_start,
            )
            if on_tile_started is not None:
                on_tile_started(tile_index + 1, row_start, row_end, row_count)
            try:
                tile = build_order_statistic_tile(
                    tasks,
                    analyses,
                    source_shape,
                    tile_canvas,
                    method,
                    rankfit_fraction,
                    exclude_zero_samples,
                    padding_policy,
                    background_mode,
                    saturation_enabled,
                    target_mode,
                    worker_plan,
                    on_worker_fallback,
                    (
                        None
                        if on_frame is None
                        else lambda frame_number, task: on_frame(
                            tile_index + 1,
                            row_start,
                            row_end,
                            frame_number,
                            len(tasks),
                            task,
                        )
                    ),
                )
                break
            except MemoryError as error:
                error_text = str(error) or error.__class__.__name__
                error.__traceback__ = None
                if row_count <= 1:
                    raise MemoryError(
                        "An order-statistic cube allocation failed at one output row. Close other applications, "
                        "reduce the image width/frame count, or use a machine with more available RAM."
                    ) from error
                next_rows = max(1, row_count // 2)
                event = {
                    "row_start": row_start,
                    "from_rows": row_count,
                    "to_rows": next_rows,
                    "available_bytes_after_failure": available_ram_bytes(),
                    "error": error_text,
                }
                tile_plan.fallback_events.append(event)
                tile_plan.current_rows = next_rows
                row_count = min(next_rows, canvas.shape[0] - row_start)
                gc.collect()
                if on_tile_fallback is not None:
                    on_tile_fallback(event)

        output_slice = (
            (slice(row_start, row_end), slice(None))
            if len(image_shape) == 2
            else (slice(None), slice(row_start, row_end), slice(None))
        )
        star_data[output_slice] = tile.star_data
        star_coverage[row_start:row_end] = tile.star_coverage
        if target_mode == "moving":
            if metcalf_data is None or metcalf_coverage is None:
                raise RuntimeError("Metcalf order-statistic output was not initialized")
            if tile.metcalf_data is None or tile.metcalf_coverage is None:
                raise RuntimeError("Metcalf order-statistic tile is missing")
            metcalf_data[output_slice] = tile.metcalf_data
            metcalf_coverage[row_start:row_end] = tile.metcalf_coverage
        if star_saturation_mask is not None and tile.star_saturation_mask is not None:
            star_saturation_mask[row_start:row_end] = tile.star_saturation_mask
        if metcalf_saturation_mask is not None and tile.metcalf_saturation_mask is not None:
            metcalf_saturation_mask[row_start:row_end] = tile.metcalf_saturation_mask
        for operation, elapsed in tile.timings.items():
            timings[operation] += elapsed
        row_start = row_end
        tile_index += 1

    return OrderStatisticStackResult(
        star_data=star_data,
        star_coverage=star_coverage,
        metcalf_data=metcalf_data,
        metcalf_coverage=metcalf_coverage,
        star_saturation_mask=star_saturation_mask,
        metcalf_saturation_mask=metcalf_saturation_mask,
        timings=timings,
    )


class MedianAccumulator:
    """Legacy disk-backed accumulator retained as a numerical regression oracle."""

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
    register = f"register {sequence_basename}_ -2pass -transf={transform}"
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
    return remove_intermediate_paths(candidates)


def remove_intermediate_paths(candidates) -> list[str]:
    """Remove existing intermediate files once no later stage can read them."""
    removed: list[str] = []
    seen: set[Path] = set()
    for path in candidates:
        path = Path(path)
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


def directory_file_stats(path: Path) -> dict[str, int]:
    file_count = 0
    total_bytes = 0
    if not path.is_dir():
        return {"file_count": 0, "bytes": 0}
    for candidate in path.rglob("*"):
        try:
            if candidate.is_file():
                file_count += 1
                total_bytes += candidate.stat().st_size
        except OSError:
            continue
    return {"file_count": file_count, "bytes": total_bytes}


def process_peak_rss_bytes() -> int | None:
    """Return this Python process' peak resident set without adding a dependency."""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            get_current_process = ctypes.windll.kernel32.GetCurrentProcess
            get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_current_process.argtypes = []
            get_current_process.restype = wintypes.HANDLE
            get_process_memory_info.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(ProcessMemoryCounters),
                wintypes.DWORD,
            ]
            get_process_memory_info.restype = wintypes.BOOL
            if get_process_memory_info(get_current_process(), ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize)
        except (AttributeError, OSError, ValueError):
            return None
        return None
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return peak if sys.platform == "darwin" else peak * 1024
    except (ImportError, OSError, ValueError):
        return None


def cleanup_after_preprocessing(registration_dir: Path, processed_files: list[Path]) -> list[str]:
    """Keep only the final sequence needed by registration plus small Siril metadata."""
    preserved = {path.resolve() for path in processed_files}
    candidates = [
        path
        for pattern in ("*.fit", "*.fits", "*.fts")
        for path in registration_dir.glob(pattern)
        if path.resolve() not in preserved
    ]
    calibration_dir = registration_dir / "calibration"
    if calibration_dir.is_dir():
        candidates.extend(path for path in calibration_dir.iterdir() if path.is_file())
    removed = remove_intermediate_paths(candidates)
    if calibration_dir.is_dir():
        try:
            calibration_dir.rmdir()
        except OSError:
            pass
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


def rebase_siril_registrations(
    registrations: dict[int, SirilRegistration],
    requested_reference_index: int,
) -> dict[int, SirilRegistration]:
    """Express Siril two-pass matrices in the user-selected reference coordinates."""
    reference = registrations.get(requested_reference_index)
    if reference is None or reference.matrix is None:
        return registrations
    try:
        auto_reference_to_requested = np.linalg.inv(matrix3(reference.matrix))
    except (ValueError, np.linalg.LinAlgError):
        return registrations
    for registration in registrations.values():
        registration.reference_index = requested_reference_index
        if registration.matrix is None:
            continue
        rebased = auto_reference_to_requested @ matrix3(registration.matrix)
        if abs(rebased[2, 2]) > 1.0e-15:
            rebased /= rebased[2, 2]
        registration.matrix = tuple(float(value) for value in rebased.ravel())  # type: ignore[assignment]
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
    *,
    require_registered_fits: bool = True,
) -> dict[int, list[str]]:
    """Return per-frame reasons that a background-star registration is unusable."""
    issues: dict[int, list[str]] = {}
    for index, source in enumerate(files, start=1):
        registration = registrations.get(index)
        registered = registration_dir / f"r_{basename}_{index:05d}.fit"
        reasons: list[str] = []
        if require_registered_fits and not registered.exists():
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
    parser = argparse.ArgumentParser(description="Stack registered astronomical subframes")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--target-mode", choices=("moving", "fixed"), default="moving")
    parser.add_argument("--frame-manifest", type=Path, help="Normalized SharpCap Live Stack frame manifest")
    parser.add_argument("--preprocessing-plan", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--bayer-pattern",
        choices=("RGGB", "BGGR", "GRBG", "GBRG"),
        help="Bayer pattern for SharpCap RAW PNG/TIFF when metadata is unavailable.",
    )
    parser.add_argument("--ephemeris-csv", type=Path)
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
    parser.add_argument("--plate-solver-name", help=argparse.SUPPRESS)
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
        "--stack-workers",
        type=parse_stack_workers,
        default="auto",
        metavar="auto|1|2|4",
        help=(
            "Parallel workers for per-frame FITS/background/shift processing. "
            "auto selects 1, 2, or 4 from available RAM and frame size (default)."
        ),
    )
    parser.add_argument(
        "--median-tile-rows",
        type=parse_median_tile_rows,
        default="auto",
        metavar="auto|N",
        help=(
            "Full-width output rows held in each median/rank-fit working cube. "
            "auto uses at most about half of currently available RAM (default)."
        ),
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
        "--preview-at",
        choices=("none", "UL", "UR", "LL", "LR"),
        default=None,
        help="Draw N/E and Sun annotations at this preview corner. Moving mode defaults to UL; fixed mode to none.",
    )
    parser.add_argument(
        "--annotate-size",
        type=float,
        default=60.0,
        help="Annotation radius in pixels for --preview-at. Defaults to 60.",
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
        default=None,
        help="Enable or disable separate saturation-warning preview PNGs. Fixed mode defaults to enable.",
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
        help="Keep intermediate image FITS files generated during preprocessing and stacking.",
    )
    parser.set_defaults(verbose=True)
    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", help="Show registration and per-frame stack progress (default).")
    parser.add_argument("--no-verbose", dest="verbose", action="store_false", help="Hide detailed registration and per-frame progress.")
    args = parser.parse_args()
    pipeline_wall_started = time.perf_counter()

    if not 1 <= args.rankfit_fraction <= 100:
        parser.error("--rankfit-fraction must be an integer from 1 to 100")
    if args.background_normalization is None:
        args.background_normalization = "none" if args.padding_policy == "legacy" else "quadratic"
    if args.saturation_warning is None:
        args.saturation_warning = "enable" if args.target_mode == "fixed" else "disable"
    if args.preview_at is None:
        args.preview_at = "none" if args.target_mode == "fixed" else "UL"
    if args.target_mode == "moving" and args.ephemeris_csv is None:
        parser.error("--ephemeris-csv is required in moving-target mode")
    if args.target_mode == "fixed" and args.preview_sun_pa_left:
        parser.error("--preview-sun-pa-left is available only in moving-target mode")
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
    ephemeris = load_ephemeris(args.ephemeris_csv) if args.target_mode == "moving" else None
    if not args.work_name:
        reference_header = read_source_image(reference_source, args.bayer_pattern).header
        target = safe_name(str(reference_header.get("OBJECT") or (manifest or {}).get("object") or reference_source.parent.name))
        mode = "fixed_" if args.target_mode == "fixed" else ""
        args.work_name = f"{target}_{mode}{processing_method_token(args.stack_method, args.rankfit_fraction)}"
    work_dir = prepare_work_dir(args.work_dir, args.work_root, args.work_name)
    registration_dir = work_dir / "registration_images"
    registration_dir.mkdir(parents=True, exist_ok=True)
    pipeline_timing_seconds = {
        "source_staging": 0.0,
        "siril_preprocessing": 0.0,
        "registration": 0.0,
        "stacking": 0.0,
        "output": 0.0,
        "total_pipeline_wall": 0.0,
    }
    temporary_storage_samples: list[dict[str, object]] = []
    temporary_peak_bytes = 0

    def record_temporary_storage(stage: str) -> None:
        nonlocal temporary_peak_bytes
        stats = directory_file_stats(registration_dir)
        sample = {"stage": stage, **stats}
        temporary_storage_samples.append(sample)
        temporary_peak_bytes = max(temporary_peak_bytes, stats["bytes"])
        if args.verbose:
            print(
                f"[storage] {stage}: {stats['file_count']} file(s), "
                f"{format_memory_size(stats['bytes'])}",
                flush=True,
            )
    use_sharpcap_registration = bool(manifest and manifest.get("alignment_complete"))
    manifest_reference = manifest_rows[reference_index - 1] if use_sharpcap_registration else None
    preprocessing_payload = None
    if args.preprocessing_plan:
        preprocessing_payload = json.loads(args.preprocessing_plan.read_text(encoding="utf-8"))
    elif isinstance(manifest, dict):
        preprocessing_payload = manifest.get("preprocessing")
    preprocessing_plan = PreprocessingPlan.from_dict(preprocessing_payload)

    removed_intermediate_images: list[str] = []
    copied: list[Path] = []
    source_headers: list[dict[str, object]] = []
    cfa = False
    source_staging_started = time.perf_counter()
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
                source_headers.append(dict(image.header))
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
                source_header, _cards, _offset = read_fits_header(source)
                source_headers.append(source_header)
                if i == 1:
                    raw_image = read_source_image(source, args.bayer_pattern, debayer=False)
                    cfa = raw_image.data.ndim == 2 and bool(
                        str(raw_image.header.get("BAYERPAT") or raw_image.header.get("COLORTYP") or "").strip()
                    )
    except Exception:
        if not args.no_cleanup:
            cleanup_intermediate_images(registration_dir, args.basename, copied, len(copied))
        raise
    pipeline_timing_seconds["source_staging"] = time.perf_counter() - source_staging_started
    record_temporary_storage("after-source-staging")

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
    preprocessing_started = time.perf_counter()
    run_siril(args.siril, registration_dir, preprocess_script, args.verbose)
    pipeline_timing_seconds["siril_preprocessing"] = time.perf_counter() - preprocessing_started
    processed_basename = processed_sequence.rstrip("_")
    processed_files = [registration_dir / f"{processed_basename}_{i:05d}.fit" for i in range(1, len(files) + 1)]
    missing_processed = [path for path in processed_files if not path.is_file()]
    if missing_processed:
        raise RuntimeError(
            f"Siril preprocessing produced only {len(processed_files) - len(missing_processed)}/{len(processed_files)} frame(s)"
        )
    record_temporary_storage("after-siril-preprocessing")
    if not args.no_cleanup:
        preprocessing_removed = cleanup_after_preprocessing(registration_dir, processed_files)
        removed_intermediate_images.extend(preprocessing_removed)
        print(
            f"[cleanup] Removed {len(preprocessing_removed)} source/conversion/calibration files after preprocessing",
            flush=True,
        )
        record_temporary_storage("after-preprocessing-cleanup")

    registration_started = time.perf_counter()
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
            star_registrations = rebase_siril_registrations(star_registrations, reference_index)
            match_diagnostics = parse_siril_match_diagnostics(siril_output)
            registration_issues = registration_validation_issues(
                files,
                registration_dir,
                processed_basename,
                star_registrations,
                args.registration_minpairs,
                require_registered_fits=False,
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
            if args.verbose:
                usable_count = len(copied) - len(registration_issues)
                print(
                    f"[registration] Siril -2pass produced matrix-only alignment; "
                    f"{usable_count}/{len(copied)} frame(s) will be stacked without registered FITS",
                    flush=True,
                )
    except SirilRegistrationError as error:
        star_registrations = parse_siril_registration(registration_seq)
        star_registrations = rebase_siril_registrations(star_registrations, reference_index)
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
            require_registered_fits=use_sharpcap_registration,
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
    pipeline_timing_seconds["registration"] = time.perf_counter() - registration_started
    record_temporary_storage("after-registration")
    registered_fits_after_registration = sum(
        1 for path in registration_dir.glob(f"r_{processed_basename}_*.fit*") if path.is_file()
    )
    reference = read_fits(processed_files[reference_index - 1])
    height = int(reference.header["NAXIS2"])
    width = int(reference.header["NAXIS1"])
    # The current product uses the reference footprint, but translation and
    # accumulation receive an explicit registration-coordinate canvas so a
    # future expanded footprint can change shape/origin without replacing the
    # resampler.
    stack_canvas = StackCanvas.reference_footprint((height, width))
    canvas_height, canvas_width = stack_canvas.shape
    if args.wcs_fits:
        wcs = WcsModel.from_wcs_fits(args.wcs_fits)
    else:
        wcs = WcsModel.from_astrometry_json(args.astrometry_json, width, height)

    reference_time = parse_time(reference.header["DATE-OBS"])
    if args.target_mode == "moving":
        if ephemeris is None:
            raise RuntimeError("Moving-target ephemeris was not loaded")
        reference_target = interpolate_ephemeris(ephemeris, reference_time)
    else:
        fixed_wcs_header = wcs.to_fits_header(width, height)
        reference_target = TargetPoint(
            reference_time,
            float(fixed_wcs_header["CRVAL1"]),
            float(fixed_wcs_header["CRVAL2"]),
        )
    reference_x, reference_y = wcs.world_to_pixel(reference_target.ra_deg, reference_target.dec_deg)
    output_reference_x, output_reference_y = stack_canvas.registration_to_output_pixel(reference_x, reference_y)
    sun_header: dict[str, object] = {}
    sun_pa_status = "off"
    if args.target_mode == "moving" and args.sun_pa == "auto":
        if args.ephemeris_csv is None:
            raise RuntimeError("Moving-target ephemeris path is unavailable")
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
        print(
            f"[background] Fitting and applying per-frame {args.background_normalization} models during stacking",
            flush=True,
        )
    saturation_enabled = args.saturation_warning == "enable"
    warning_color_rgb = saturation_rgb(args.saturation_color)
    metcalf_saturation_mask = (
        np.zeros(stack_canvas.shape, dtype=bool)
        if saturation_enabled and args.target_mode == "moving"
        else None
    )
    star_saturation_mask = np.zeros(stack_canvas.shape, dtype=bool) if saturation_enabled else None
    saturated_frame_count = 0
    saturation_level_unavailable_frames = 0

    sum_image: np.ndarray | None = None
    count_image: np.ndarray | None = None
    star_sum_image: np.ndarray | None = None
    star_count_image: np.ndarray | None = None
    metcalf_coverage: np.ndarray | None = None
    star_coverage: np.ndarray | None = None
    median_tile_plan: MedianTilePlan | None = None
    frame_rows: list[dict[str, object]] = []
    used_times: list[datetime] = []
    used_exposures: list[float | None] = []
    used = 0
    if args.verbose:
        print(
            f"[stack] padding policy={args.padding_policy}; zero-sample policy={args.zero_sample_policy}; "
            f"background normalization={args.background_normalization}",
            flush=True,
        )

    stack_wall_started = time.perf_counter()
    stack_timing_seconds = {
        "fits_read": 0.0,
        "background_fit": 0.0,
        "background_apply": 0.0,
        "star_resample": 0.0,
        "star_accumulation": 0.0,
        "metcalf_shift": 0.0,
        "metcalf_accumulation": 0.0,
        "saturation": 0.0,
        "order_statistic_analysis": 0.0,
        "order_statistic_combine": 0.0,
        "total_stacking_wall": 0.0,
    }
    stack_tasks: list[StackFrameTask] = []
    registration_metrics_by_index: dict[int, dict[str, object]] = {}
    for i, source in enumerate(copied, start=1):
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
        registration_metrics_by_index[i] = registration_metrics
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
        source_header = source_headers[i - 1]
        frame_time = parse_time(source_header["DATE-OBS"])
        if args.target_mode == "moving":
            if ephemeris is None:
                raise RuntimeError("Moving-target ephemeris was not loaded")
            target = interpolate_ephemeris(ephemeris, frame_time)
            x, y = wcs.world_to_pixel(target.ra_deg, target.dec_deg)
            dx = reference_x - x
            dy = reference_y - y
        else:
            target = TargetPoint(frame_time, reference_target.ra_deg, reference_target.dec_deg)
            x, y = reference_x, reference_y
            dx = 0.0
            dy = 0.0
        if use_sharpcap_registration:
            prepared = registration_dir / f"r_{processed_basename}_{i:05d}.fit"
            source_to_registration = np.eye(3, dtype=np.float64)
        else:
            if star_reg.matrix is None:
                raise RuntimeError(f"Registration matrix is missing for usable frame {i}")
            prepared = processed_files[i - 1]
            source_shape = (int(source_header["NAXIS2"]), int(source_header["NAXIS1"]))
            source_to_registration = siril_matrix_to_array_coordinates(
                star_reg.matrix,
                source_shape,
                stack_canvas.shape,
            )
        stack_tasks.append(
            StackFrameTask(
                index=i,
                source_name=files[i - 1].name,
                prepared=prepared,
                source_header=source_header,
                source_to_registration=tuple(float(value) for value in source_to_registration.ravel()),
                frame_time=frame_time,
                target=target,
                target_x=x,
                target_y=y,
                dx=dx,
                dy=dy,
            )
        )

    if not stack_tasks:
        raise RuntimeError("No registered frame transforms were available for stacking")
    if not args.no_cleanup:
        if use_sharpcap_registration:
            skipped_inputs = [
                registration_dir / f"r_{processed_basename}_{index:05d}.fit"
                for index in registration_issues
            ]
            pre_stack_candidates = [*copied, *processed_files, *skipped_inputs]
        else:
            skipped_inputs = [processed_files[index - 1] for index in registration_issues]
            pre_stack_candidates = [*copied, *skipped_inputs]
        pre_stack_removed = remove_intermediate_paths(pre_stack_candidates)
        removed_intermediate_images.extend(pre_stack_removed)
        print(
            f"[cleanup] Removed {len(pre_stack_removed)} completed/unused FITS files before stacking; "
            "usable preprocessed frames remain until their contributions are accepted",
            flush=True,
        )
        record_temporary_storage("after-pre-stack-cleanup")

    worker_memory_estimate = estimate_stack_worker_memory(
        tuple(reference.data.shape),
        stack_canvas,
        args.background_normalization,
        saturation_enabled,
    )
    worker_plan = select_stack_worker_plan(args.stack_workers, worker_memory_estimate)
    print(describe_stack_worker_plan(worker_plan), flush=True)

    if args.verbose:
        print(
            f"[stack] processing {len(stack_tasks)} usable frame(s) with {worker_plan.initial_workers} worker(s)",
            flush=True,
        )

    def report_worker_fallback(event: dict[str, object]) -> None:
        print(
            "[workers:fallback] Memory allocation failed with "
            f"{event['from_workers']} workers at uncommitted batch starting frame "
            f"{event['batch_first_frame']}. All workers stopped and local batch results were discarded; "
            f"retrying the same batch with {event['to_workers']} worker(s). "
            f"Available RAM now={format_memory_size(event['available_bytes_after_failure'])}",
            file=sys.stderr,
            flush=True,
        )

    def record_frame_analysis(result: StackFrameResult | StackFrameAnalysis) -> bool:
        nonlocal used, saturated_frame_count, saturation_level_unavailable_frames
        task = result.task
        i = task.index
        background_model = result.background_model
        background_correction = result.background_correction
        if background_model is not None:
            background_models_by_index[i] = background_model
            if background_correction is None:
                raise RuntimeError(f"Background correction is missing for usable frame {i}")
            background_corrections_by_index[i] = background_correction
            if args.verbose:
                rendered = ", ".join(f"{level:.3f}" for level in background_model.levels)
                details = (
                    ""
                    if args.background_normalization == "offset"
                    else f"; tiles={background_model.tile_count}; rejected={background_model.rejected_tile_counts.tolist()}"
                )
                print(f"[background] frame {i}/{len(copied)}: [{rendered}] ADU{details}", flush=True)

        frame_saturation_warning = result.saturated_pixel_count > 0 and result.saturation_level is not None
        if saturation_enabled:
            if result.saturation_level is None:
                saturation_level_unavailable_frames += 1
                if args.verbose:
                    print(
                        f"[saturation] level unavailable for {task.source_name}; no pixels marked",
                        flush=True,
                    )
            elif frame_saturation_warning:
                saturated_frame_count += 1
                if args.verbose:
                    print(
                        f"[saturation] {task.source_name}: max={result.subframe_max_count:.3f}, "
                        f"threshold={result.saturation_threshold_count:.3f}, pixels={result.saturated_pixel_count}",
                        flush=True,
                    )

        used += 1
        used_times.append(task.frame_time)
        used_exposures.append(exposure_seconds_from_header(task.source_header))
        registration_metrics = registration_metrics_by_index[i]
        frame_rows.append(
            {
                "index": i,
                "source": task.source_name,
                "stack_input": task.prepared.name,
                "used": True,
                "date_obs": task.frame_time.isoformat(),
                "ra_deg": task.target.ra_deg,
                "dec_deg": task.target.dec_deg,
                "target_x_1based": task.target_x,
                "target_y_1based": task.target_y,
                "extra_dx_px": task.dx,
                "extra_dy_px": task.dy,
                **registration_metrics,
                "prepared_unit_scale": result.prepared_unit_scale,
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
                "saturation_level": result.saturation_level,
                "saturation_threshold_count": result.saturation_threshold_count,
                "subframe_max_count": result.subframe_max_count,
                "saturated_pixel_count": result.saturated_pixel_count if saturation_enabled else None,
            }
        )
        return frame_saturation_warning

    if args.stack_method == "mean":
        worker = lambda task: process_stack_frame(
            task,
            stack_canvas,
            args.padding_policy,
            args.background_normalization,
            saturation_enabled,
            args.saturation_threshold_percent,
            args.target_mode,
        )
        for result in adaptive_ordered_bounded_map(worker, stack_tasks, worker_plan, report_worker_fallback):
            task = result.task
            if args.verbose:
                print(
                    f"[stack:{args.stack_method}] frame {task.index}/{len(copied)}: {task.source_name}",
                    flush=True,
                )
            for operation, elapsed in result.timings.items():
                stack_timing_seconds[operation] += elapsed
            frame_saturation_warning = record_frame_analysis(result)
            if frame_saturation_warning:
                if star_saturation_mask is None or result.star_saturation_mask is None:
                    raise RuntimeError("Saturation warning masks were not initialized")
                star_saturation_mask |= result.star_saturation_mask
                if args.target_mode == "moving":
                    if metcalf_saturation_mask is None or result.metcalf_saturation_mask is None:
                        raise RuntimeError("Metcalf saturation warning masks were not initialized")
                    metcalf_saturation_mask |= result.metcalf_saturation_mask
            started = time.perf_counter()
            star_sum_image, star_count_image = add_to_average(
                star_sum_image,
                star_count_image,
                result.star_data,
                result.star_mask,
            )
            stack_timing_seconds["star_accumulation"] += time.perf_counter() - started
            if args.target_mode == "moving":
                started = time.perf_counter()
                sum_image, count_image = add_to_average(
                    sum_image,
                    count_image,
                    result.metcalf_data,
                    result.metcalf_mask,
                )
                stack_timing_seconds["metcalf_accumulation"] += time.perf_counter() - started
            if not args.no_cleanup:
                removed_intermediate_images.extend(remove_intermediate_paths([task.prepared]))
    else:
        analyses_by_index: dict[int, StackFrameAnalysis] = {}
        analysis_wall_started = time.perf_counter()
        analysis_worker = lambda task: analyze_order_statistic_frame(
            task,
            args.padding_policy,
            args.background_normalization,
            saturation_enabled,
            args.saturation_threshold_percent,
        )
        for analysis in adaptive_ordered_bounded_map(
            analysis_worker,
            stack_tasks,
            worker_plan,
            report_worker_fallback,
        ):
            task = analysis.task
            if args.verbose:
                print(
                    f"[analyze:{args.stack_method}] frame {task.index}/{len(copied)}: {task.source_name}",
                    flush=True,
                )
            for operation, elapsed in analysis.timings.items():
                stack_timing_seconds[operation] += elapsed
            analyses_by_index[task.index] = analysis
            record_frame_analysis(analysis)
        stack_timing_seconds["order_statistic_analysis"] = time.perf_counter() - analysis_wall_started

        tile_image_shape = order_statistic_image_shape(tuple(reference.data.shape), stack_canvas)
        median_tile_plan = select_median_tile_plan(
            args.median_tile_rows,
            len(stack_tasks),
            tile_image_shape,
            args.target_mode,
        )
        print(describe_median_tile_plan(median_tile_plan), flush=True)

        def report_median_tile_fallback(event: dict[str, object]) -> None:
            print(
                "[median-tiles:fallback] Memory allocation failed for uncommitted rows "
                f"{event['row_start'] + 1} onward with {event['from_rows']} rows. "
                f"The tile cube was discarded; retrying with {event['to_rows']} rows. "
                f"Available RAM now={format_memory_size(event['available_bytes_after_failure'])}",
                file=sys.stderr,
                flush=True,
            )

        def report_tile_started(tile_number: int, row_start: int, row_end: int, row_count: int) -> None:
            if args.verbose:
                print(
                    f"[stack:{args.stack_method}] tile {tile_number}: rows "
                    f"{row_start + 1}-{row_end}/{stack_canvas.shape[0]} ({row_count} rows)",
                    flush=True,
                )

        def report_tile_frame(
            tile_number: int,
            row_start: int,
            row_end: int,
            frame_number: int,
            frame_count: int,
            task: StackFrameTask,
        ) -> None:
            if args.verbose:
                print(
                    f"[stack:{args.stack_method}] tile {tile_number} rows {row_start + 1}-{row_end}: "
                    f"frame {frame_number}/{frame_count} ({task.source_name})",
                    flush=True,
                )

        tiled = stack_order_statistic_rows(
            stack_tasks,
            analyses_by_index,
            tuple(reference.data.shape),
            stack_canvas,
            args.stack_method,
            args.rankfit_fraction,
            args.zero_sample_policy == "exclude" and args.background_normalization == "none",
            args.padding_policy,
            args.background_normalization,
            saturation_enabled,
            args.target_mode,
            worker_plan,
            median_tile_plan,
            report_worker_fallback,
            report_median_tile_fallback,
            report_tile_started,
            report_tile_frame,
        )
        star_stack = tiled.star_data
        star_coverage = tiled.star_coverage
        star_saturation_mask = tiled.star_saturation_mask
        if args.target_mode == "moving":
            if tiled.metcalf_data is None or tiled.metcalf_coverage is None:
                raise RuntimeError("Metcalf order-statistic stack is missing")
            stack = tiled.metcalf_data
            metcalf_coverage = tiled.metcalf_coverage
            metcalf_saturation_mask = tiled.metcalf_saturation_mask
        else:
            stack = star_stack
            metcalf_coverage = star_coverage
        for operation, elapsed in tiled.timings.items():
            stack_timing_seconds[operation] += elapsed
        if not args.no_cleanup:
            removed_intermediate_images.extend(remove_intermediate_paths([task.prepared for task in stack_tasks]))

    frame_rows.sort(key=lambda row: int(row["index"]))

    if used == 0:
        raise RuntimeError("No registered frame transforms were available for stacking")

    if args.background_normalization != "none":
        background_output_levels = mean_background_dc_levels(background_models_by_index)
        if args.verbose:
            rendered = ", ".join(f"{level:.3f}" for level in background_output_levels)
            details = (
                ""
                if args.background_normalization == "offset"
                else f"; model={args.background_normalization}; tiles={BACKGROUND_TILE_ROWS}x{BACKGROUND_TILE_COLUMNS}"
            )
            print(
                f"[background] each frame was fitted to zero; final output offset: [{rendered}] ADU{details}",
                flush=True,
            )

    median_temp_removed: list[str] = []
    if args.verbose:
        print(
            f"[stack:{args.stack_method}] finalizing {used}/{len(copied)} accepted frames",
            flush=True,
        )
    if args.stack_method == "mean":
        star_stack = finalize_average(star_sum_image, star_count_image)
        star_coverage = star_count_image
        if args.target_mode == "moving":
            stack = finalize_average(sum_image, count_image)
            metcalf_coverage = count_image
        else:
            stack = star_stack
            metcalf_coverage = star_coverage
    if args.background_normalization != "none":
        if background_output_levels is None or metcalf_coverage is None or star_coverage is None:
            raise RuntimeError("Background output offset data is missing")
        star_stack = add_background_output_offset(star_stack, star_coverage > 0, background_output_levels)
        if args.target_mode == "moving":
            stack = add_background_output_offset(stack, metcalf_coverage > 0, background_output_levels)
        else:
            stack = star_stack
    stack_timing_seconds["total_stacking_wall"] = time.perf_counter() - stack_wall_started
    pipeline_timing_seconds["stacking"] = stack_timing_seconds["total_stacking_wall"]
    record_temporary_storage("after-stack-input-cleanup")
    timing_parts = "; ".join(
        f"{name}={elapsed:.3f}s"
        for name, elapsed in stack_timing_seconds.items()
        if name != "total_stacking_wall"
    )
    print(
        f"[timing] workers={worker_plan.current_workers}; requested={args.stack_workers}; "
        f"initial={worker_plan.initial_workers}; fallbacks={len(worker_plan.fallback_events)}; {timing_parts}; "
        f"total_stacking_wall={stack_timing_seconds['total_stacking_wall']:.3f}s",
        flush=True,
    )
    output_started = time.perf_counter()
    moving_mode = args.target_mode == "moving"
    comparison_stack = concatenate_side_by_side(star_stack, stack) if moving_mode else None
    if args.verbose:
        if moving_mode:
            print("[output] Writing Metcalf, star-aligned, comparison FITS, and previews", flush=True)
        else:
            print("[output] Writing fixed-target FITS and preview", flush=True)
    base_output_stem = args.output_prefix or default_output_stem(
        reference,
        reference_source.name,
        used_times,
        used,
    )
    method_token = processing_method_token(args.stack_method, args.rankfit_fraction)
    output_stem = f"{base_output_stem}_{method_token}"
    if moving_mode:
        output_fits = work_dir / f"{output_stem}_metcalf_stack.fit"
        output_png = work_dir / f"{output_stem}_metcalf_preview.png"
        star_output_fits = work_dir / f"{output_stem}_star_stack.fit"
        star_output_png = work_dir / f"{output_stem}_star_preview.png"
        comparison_output_fits = work_dir / f"{output_stem}_star_left_metcalf_right.fit"
        comparison_output_png = work_dir / f"{output_stem}_star_left_metcalf_right_preview.png"
    else:
        output_fits = work_dir / f"{output_stem}_fixed_stack.fit"
        output_png = work_dir / f"{output_stem}_fixed_preview.png"
        star_output_fits = None
        star_output_png = None
        comparison_output_fits = None
        comparison_output_png = None
    north_up_angle = None
    north_up_output_png = None
    north_up_star_output_png = None
    north_up_comparison_output_png = None
    if args.preview_north_up:
        north_up_angle = north_up_rotation_degrees(wcs)
        suffix = "metcalf" if moving_mode else "fixed"
        north_up_output_png = work_dir / f"{output_stem}_{suffix}_north_up_preview.png"
        if moving_mode:
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
    if moving_mode and args.preview_at != "none" and "SUN_PA" not in sun_header:
        print(
            "Preview annotation skipped: SUN_PA is unavailable. "
            "The stack succeeded; use --preview-at none to suppress this warning.",
            file=sys.stderr,
        )
    if args.preview_at != "none" and "SUN_PA" in sun_header:
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
    saturation_kind = "metcalf" if moving_mode else "fixed"
    saturation_output_png = work_dir / f"{output_stem}_{saturation_kind}_saturation_warning.png" if saturation_enabled else None
    star_saturation_output_png = (
        work_dir / f"{output_stem}_star_saturation_warning.png" if saturation_enabled and moving_mode else None
    )
    comparison_saturation_output_png = (
        work_dir / f"{output_stem}_star_left_metcalf_right_saturation_warning.png"
        if saturation_enabled and moving_mode
        else None
    )
    shifts_csv = work_dir / f"{output_stem}_shifts.csv"
    registration_diagnostics_csv = work_dir / f"{output_stem}_registration_diagnostics.csv"
    summary_json = work_dir / f"{output_stem}_summary.json"
    star_wcs_header = stack_canvas.rebase_wcs_header(
        wcs.to_fits_header(canvas_width, canvas_height)
    )
    plate_solver_name = args.plate_solver_name or infer_plate_solver_name(args.wcs_fits, args.astrometry_json)
    session_header = stack_session_header(used_times, used_exposures, plate_solver_name)
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
        **session_header,
        "TARGMODE": "moving",
        "MTSTACK": True,
        "MTFRAMES": used,
        "MTXREF": output_reference_x,
        "MTYREF": output_reference_y,
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
        **session_header,
        "TARGMODE": "moving",
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
        **session_header,
        "TARGMODE": "moving",
        "COMBSTK": True,
        "COMBLEFT": "star_stack",
        "COMBRGHT": "metcalf_stack",
        "COMBW": canvas_width,
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
    fixed_extra_header = {
        **star_wcs_header,
        **session_header,
        "TARGMODE": "fixed",
        "FIXEDSTK": True,
        "STARSTK": True,
        "MTSTACK": False,
        "STKUNITS": "ADU",
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
        if moving_mode:
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
            uint16_stats = write_fits_uint16(
                output_fits,
                star_stack,
                reference.header,
                fixed_extra_header,
                args.uint16_scale,
                args.scale_low_percentile,
                args.scale_high_percentile,
            )
    else:
        if moving_mode:
            write_fits_float32(output_fits, stack.astype(np.float32), reference.header, extra_header)
            write_fits_float32(star_output_fits, star_stack.astype(np.float32), reference.header, star_extra_header)
            write_fits_float32(
                comparison_output_fits,
                comparison_stack.astype(np.float32),
                reference.header,
                comparison_extra_header,
            )
        else:
            write_fits_float32(output_fits, star_stack.astype(np.float32), reference.header, fixed_extra_header)
    export_preview_png(
        output_png,
        stack,
        low_percentile=args.preview_low_percentile,
        high_percentile=args.preview_high_percentile,
        stretch=args.preview_stretch,
        sigma_low=args.preview_sigma_low,
        sigma_high=args.preview_sigma_high,
    )
    if moving_mode:
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
        if north_up_angle is None or north_up_output_png is None:
            raise RuntimeError("North-up preview paths were not initialized")
        rotate_preview_png(output_png, north_up_output_png, north_up_angle)
        if moving_mode:
            if star_output_png is None or north_up_star_output_png is None or north_up_comparison_output_png is None:
                raise RuntimeError("Moving-target north-up preview paths were not initialized")
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
    if annotated_output_png is not None:
        if annotated_output_png is None or annotation_overlay_png is None:
            raise RuntimeError("Annotated preview path was not initialized")
        annotate_preview_png(
            annotation_source_png,
            annotated_output_png,
            wcs.cd_matrix(),
            reference_target.dec_deg,
            float(sun_header["SUN_PA"]),
            image_rotation_degrees=annotation_rotation,
            corner=args.preview_at,
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
        print(f"[preview] Annotated PNG and transparent overlay written at {args.preview_at}", flush=True)
    if saturation_enabled:
        if star_saturation_mask is None:
            raise RuntimeError("Saturation warning masks were not initialized")
        primary_saturation_mask = metcalf_saturation_mask if moving_mode else star_saturation_mask
        if primary_saturation_mask is None:
            raise RuntimeError("Primary saturation warning mask was not initialized")
        export_preview_png(
            saturation_output_png,
            stack,
            low_percentile=args.preview_low_percentile,
            high_percentile=args.preview_high_percentile,
            stretch=args.preview_stretch,
            sigma_low=args.preview_sigma_low,
            sigma_high=args.preview_sigma_high,
            warning_mask=primary_saturation_mask,
            warning_color=warning_color_rgb,
        )
        if moving_mode:
            if comparison_stack is None or star_saturation_output_png is None or comparison_saturation_output_png is None:
                raise RuntimeError("Moving-target saturation warning outputs were not initialized")
            comparison_saturation_mask = concatenate_side_by_side(
                star_saturation_mask,
                primary_saturation_mask,
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
                f"metcalf pixels={int(np.count_nonzero(primary_saturation_mask))}",
                flush=True,
            )
        else:
            print(
                f"[saturation] fixed warning preview: {saturated_frame_count}/{used} frames; "
                f"marked pixels={int(np.count_nonzero(star_saturation_mask))}",
                flush=True,
            )

    print(f"[result] Stacked {used}/{len(copied)} frames; skipped {len(copied) - used}", flush=True)

    with shifts_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "index",
            "source",
            "stack_input",
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
            "prepared_unit_scale",
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

    if not args.no_cleanup:
        removed_intermediate_images.extend(
            cleanup_intermediate_images(
                registration_dir,
                args.basename,
                copied,
                len(files),
                processed_basename,
            )
        )
    record_temporary_storage("final")
    pipeline_timing_seconds["output"] = time.perf_counter() - output_started
    pipeline_timing_seconds["total_pipeline_wall"] = time.perf_counter() - pipeline_wall_started
    peak_rss_bytes = process_peak_rss_bytes()

    summary = {
        "source_dir": str(args.source_dir),
        "work_dir": str(work_dir),
        "registration_dir": str(registration_dir),
        "target_mode": args.target_mode,
        "ephemeris_csv": str(args.ephemeris_csv) if args.ephemeris_csv else None,
        "wcs_fits": str(args.wcs_fits) if args.wcs_fits else None,
        "astrometry_json": str(args.astrometry_json) if args.astrometry_json else None,
        "software_name": SOFTWARE_NAME,
        "software_version": SOFTWARE_VERSION,
        "plate_solution_source": session_header["PLTSOLVR"],
        "session_start_utc": session_header["DATE-BEG"],
        "session_average_utc": session_header.get("DATE-AVG"),
        "session_average_mjd": session_header.get("MJD-AVG"),
        "session_end_utc": session_header["DATE-END"],
        "session_elapsed_seconds": session_header.get("TELAPSE"),
        "total_exposure_seconds": session_header.get("TOTEXP"),
        "exposure_metadata_complete": "TOTEXP" in session_header,
        "registration_transform": args.registration_transform,
        "registration_source": "sharpcap-stacklog" if use_sharpcap_registration else "siril",
        "registration_mode": (
            "sharpcap-materialized-registration"
            if use_sharpcap_registration
            else "siril-2pass-matrix-only"
        ),
        "registered_fits_after_registration": registered_fits_after_registration,
        "resampling": (
            "prealigned-sharpcap-star-accumulation"
            if not moving_mode and use_sharpcap_registration
            else "single-pass-star-matrix"
            if not moving_mode
            else "prealigned-sharpcap-plus-metcalf-translation"
            if use_sharpcap_registration
            else "single-pass-star-matrix-plus-metcalf-translation"
        ),
        "frame_manifest": str(args.frame_manifest) if args.frame_manifest else None,
        "preprocessing": preprocessing_plan.to_dict(),
        "registration_minpairs": args.registration_minpairs,
        "registration_seq": str(registration_seq),
        "preview_north_up": args.preview_north_up,
        "preview_north_up_rotation_deg": north_up_angle,
        "preview_sun_pa_left": args.preview_sun_pa_left,
        "preview_sun_pa_left_rotation_deg": sun_pa_left_angle,
        "preview_at": args.preview_at,
        "preview_annotate": annotated_output_png is not None,
        "annotate_size_px": args.annotate_size if annotated_output_png else None,
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
            "fixed_marked_pixels": (
                int(np.count_nonzero(star_saturation_mask))
                if not moving_mode and star_saturation_mask is not None
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
        "stack_workers": worker_plan.current_workers,
        "stack_workers_requested": args.stack_workers,
        "stack_worker_selection": {
            "initial_workers": worker_plan.initial_workers,
            "final_workers": worker_plan.current_workers,
            "reason": worker_plan.reason,
            "available_ram_bytes": worker_plan.available_bytes,
            "reserve_bytes": worker_plan.reserve_bytes,
            "fixed_estimate_bytes": worker_plan.estimate.fixed_bytes,
            "per_worker_estimate_bytes": worker_plan.estimate.per_worker_bytes,
            "initial_projected_bytes": worker_plan.estimate.projected_bytes(worker_plan.initial_workers),
            "source_shape": list(worker_plan.estimate.source_shape),
            "canvas_shape": list(worker_plan.estimate.canvas_shape),
            "fallback_events": worker_plan.fallback_events,
        },
        "median_tile_rows_requested": args.median_tile_rows if args.stack_method != "mean" else None,
        "median_tile_plan": (
            {
                "initial_rows": median_tile_plan.initial_rows,
                "final_rows": median_tile_plan.current_rows,
                "available_ram_bytes": median_tile_plan.available_bytes,
                "cube_budget_bytes": median_tile_plan.budget_bytes,
                "bytes_per_row": median_tile_plan.bytes_per_row,
                "initial_cube_bytes": median_tile_plan.cube_bytes(median_tile_plan.initial_rows),
                "final_cube_bytes": median_tile_plan.cube_bytes(median_tile_plan.current_rows),
                "initial_tile_count": math.ceil(median_tile_plan.height / median_tile_plan.initial_rows),
                "final_tile_count": math.ceil(median_tile_plan.height / median_tile_plan.current_rows),
                "frame_count": median_tile_plan.frame_count,
                "channels": median_tile_plan.channels,
                "width": median_tile_plan.width,
                "height": median_tile_plan.height,
                "simultaneous_cubes": median_tile_plan.cube_count,
                "reason": median_tile_plan.reason,
                "fallback_events": median_tile_plan.fallback_events,
            }
            if median_tile_plan is not None
            else None
        ),
        "stack_canvas": {
            "policy": "reference-footprint",
            "shape": [canvas_height, canvas_width],
            "origin_x": stack_canvas.origin_x,
            "origin_y": stack_canvas.origin_y,
        },
        "stack_timing_seconds": stack_timing_seconds,
        "stack_timing_note": (
            "per-operation worker CPU sums; order_statistic_analysis and total_stacking_wall are elapsed wall time; "
            "median/rank-fit FITS reads and background application include repeated row-tile passes"
        ),
        "pipeline_timing_seconds": pipeline_timing_seconds,
        "temporary_storage": {
            "scope": str(registration_dir),
            "samples": temporary_storage_samples,
            "measured_peak_bytes": temporary_peak_bytes,
            "measurement_note": "Checkpoint samples; transient peaks between checkpoints are not captured.",
        },
        "process_peak_rss_bytes": peak_rss_bytes,
        "process_peak_rss_note": "Peak RSS of the Python stacking process only; Siril child-process memory is excluded.",
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
            "x_1based": output_reference_x,
            "y_1based": output_reference_y,
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
            "fixed_fits": str(output_fits) if not moving_mode else None,
            "fixed_preview_png": str(output_png) if not moving_mode else None,
            "metcalf_fits": str(output_fits) if moving_mode else None,
            "metcalf_preview_png": str(output_png) if moving_mode else None,
            "star_fits": str(star_output_fits) if star_output_fits else None,
            "star_preview_png": str(star_output_png) if star_output_png else None,
            "comparison_fits": str(comparison_output_fits) if comparison_output_fits else None,
            "comparison_preview_png": str(comparison_output_png) if comparison_output_png else None,
            "north_up_preview_png": str(north_up_output_png) if north_up_output_png else None,
            "north_up_star_preview_png": str(north_up_star_output_png) if north_up_star_output_png else None,
            "north_up_comparison_preview_png": str(north_up_comparison_output_png) if north_up_comparison_output_png else None,
            "sun_pa_left_preview_png": str(sun_pa_left_output_png) if sun_pa_left_output_png else None,
            "annotated_preview_png": str(annotated_output_png) if annotated_output_png else None,
            "annotation_overlay_png": str(annotation_overlay_png) if annotation_overlay_png else None,
            "metcalf_saturation_warning_png": (
                str(saturation_output_png) if moving_mode and saturation_output_png else None
            ),
            "fixed_saturation_warning_png": (
                str(saturation_output_png) if not moving_mode and saturation_output_png else None
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
