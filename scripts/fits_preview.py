"""Display-only FITS/array preview helpers.

This module deliberately does not modify science data or WCS headers. It is
shared by the stacker and developer tools so display experiments can be
isolated from stacking logic.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


def export_preview_png(
    path: Path,
    data: np.ndarray,
    low_percentile: float = 5.0,
    high_percentile: float = 99.95,
    stretch: str = "sigma",
    sigma_low: float = -1.0,
    sigma_high: float = 3.0,
    warning_mask: np.ndarray | None = None,
    warning_color: tuple[int, int, int] = (255, 0, 0),
) -> None:
    """Write a display PNG using the stacker's established stretch rules."""
    if data.ndim == 2:
        planes = [data]
    else:
        planes = [data[index] for index in range(min(3, data.shape[0]))]
    stretched = []
    for plane in planes:
        # Exact-zero pixels are registration padding, not sky samples.
        finite = plane[np.isfinite(plane) & (plane != 0.0)]
        if finite.size == 0:
            scaled = np.zeros_like(plane, dtype=np.uint8)
        else:
            if stretch == "percentile":
                low, high = np.percentile(finite, [low_percentile, high_percentile])
            elif stretch == "sigma":
                center = float(np.mean(finite))
                standard_deviation = float(np.std(finite))
                if not math.isfinite(standard_deviation) or standard_deviation <= 0.0:
                    standard_deviation = 1.0
                low = center + sigma_low * standard_deviation
                high = center + sigma_high * standard_deviation
            else:
                raise ValueError(f"Unsupported preview stretch: {stretch}")
            if high <= low:
                high = low + 1.0
            scaled = np.clip((plane - low) / (high - low), 0.0, 1.0)
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
            rgb[warning_mask] = np.asarray(warning_color, dtype=np.uint8)
        image = Image.fromarray(rgb, mode="RGB")
    image.save(path)


def rotate_preview_png(source: Path, destination: Path, angle_degrees: float) -> None:
    """Rotate an already-stretched preview without changing its FITS data."""
    with Image.open(source) as source_image:
        image = source_image.convert("RGB") if source_image.mode not in ("L", "RGB") else source_image.copy()
    fill = 0 if image.mode == "L" else (0, 0, 0)
    image.rotate(angle_degrees, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=fill).save(destination)


def annotation_display_angle_degrees(
    cd_matrix: tuple[float, float, float, float],
    reference_dec_deg: float,
    position_angle_deg: float,
    image_rotation_degrees: float = 0.0,
) -> float:
    """Project celestial position angle into a rotated PNG display direction."""
    cd11, cd12, cd21, cd22 = cd_matrix
    determinant = cd11 * cd22 - cd12 * cd21
    if abs(determinant) < 1e-20:
        raise ValueError("Cannot annotate preview: WCS CD matrix is singular")
    cos_dec = math.cos(math.radians(reference_dec_deg))
    if abs(cos_dec) < 1e-12:
        raise ValueError("Cannot annotate preview at a celestial pole")
    pa_radians = math.radians(position_angle_deg)
    world_ra = math.sin(pa_radians) / cos_dec
    world_dec = math.cos(pa_radians)
    pixel_x = (cd22 * world_ra - cd12 * world_dec) / determinant
    pixel_y = (-cd21 * world_ra + cd11 * world_dec) / determinant
    # Pillow rotations subtract from the display-vector angle.
    return math.degrees(math.atan2(pixel_y, pixel_x)) - image_rotation_degrees


def _annotation_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Use a scalable common font when available, with a safe PIL fallback."""
    candidates = (
        ["DejaVuSans-Bold.ttf", "Arial Bold.ttf", "arialbd.ttf", "DejaVuSans.ttf", "Arial.ttf", "arial.ttf"]
        if bold
        else ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf"]
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _point(origin: tuple[float, float], angle_degrees: float, length: float) -> tuple[float, float]:
    angle = math.radians(angle_degrees)
    return origin[0] + math.cos(angle) * length, origin[1] + math.sin(angle) * length


def _corner_origin(width: int, height: int, radius: float, corner: str) -> tuple[float, float]:
    # Keep the full annotation circle one-third radius clear of both image edges.
    inset = radius * 4.0 / 3.0
    return {
        "UL": (inset, inset),
        "UR": (width - inset, inset),
        "LL": (inset, height - inset),
        "LR": (width - inset, height - inset),
    }[corner]


def annotate_preview_png(
    source: Path,
    destination: Path,
    cd_matrix: tuple[float, float, float, float],
    reference_dec_deg: float,
    sun_pa_deg: float,
    image_rotation_degrees: float = 0.0,
    corner: str = "UL",
    radius_px: float = 60.0,
) -> None:
    """Overlay N/E orientation sticks and a Sun-direction arrow on a PNG."""
    with Image.open(source) as source_image:
        image = source_image.convert("RGBA")
    overlay = annotation_overlay_image(
        image.size,
        cd_matrix,
        reference_dec_deg,
        sun_pa_deg,
        image_rotation_degrees=image_rotation_degrees,
        corner=corner,
        radius_px=radius_px,
    )
    Image.alpha_composite(image, overlay).convert("RGB").save(destination)


def write_annotation_overlay_png(
    destination: Path,
    cd_matrix: tuple[float, float, float, float],
    reference_dec_deg: float,
    sun_pa_deg: float,
    image_rotation_degrees: float = 0.0,
    radius_px: float = 60.0,
) -> None:
    """Write a compact, freely placeable RGBA N/E/Sun annotation sprite.

    ``radius_px`` remains the physical drawing radius. The output is a square
    centered on the marker, with only enough transparent padding to preserve
    the arrowhead, labels, and circumpunct. It deliberately has no image-corner
    concept: callers choose its final placement when they composite it.
    """
    annotation_sprite_image(
        cd_matrix,
        reference_dec_deg,
        sun_pa_deg,
        image_rotation_degrees=image_rotation_degrees,
        radius_px=radius_px,
    ).save(destination)


def annotation_overlay_image(
    image_size: tuple[int, int],
    cd_matrix: tuple[float, float, float, float],
    reference_dec_deg: float,
    sun_pa_deg: float,
    image_rotation_degrees: float = 0.0,
    corner: str = "UL",
    radius_px: float = 60.0,
) -> Image.Image:
    """Render a transparent N/E/Sun overlay for a final display image size.

    The annotation is evaluated after any display rotation, so its orientation
    remains correct for native, north-up, and Sun-left preview variants.
    """
    if corner not in {"UL", "UR", "LL", "LR"}:
        raise ValueError("Annotation corner must be one of UL, UR, LL, or LR")
    if radius_px <= 0.0:
        raise ValueError("Annotation radius must be positive")
    image = Image.new("RGBA", image_size, (0, 0, 0, 0))
    radius = min(float(radius_px), max(12.0, min(image.width, image.height) * 0.22))
    origin = _corner_origin(image.width, image.height, radius, corner)
    _draw_annotation(
        image,
        origin,
        radius,
        cd_matrix,
        reference_dec_deg,
        sun_pa_deg,
        image_rotation_degrees,
    )
    return image


def annotation_sprite_image(
    cd_matrix: tuple[float, float, float, float],
    reference_dec_deg: float,
    sun_pa_deg: float,
    image_rotation_degrees: float = 0.0,
    radius_px: float = 60.0,
) -> Image.Image:
    """Return a compact transparent annotation layer, centered on its marker."""
    if radius_px <= 0.0:
        raise ValueError("Annotation radius must be positive")
    radius = float(radius_px)
    # The Sun symbol slightly extends beyond the arrow radius. This protects it
    # while keeping the file a compact layer rather than a full-frame overlay.
    edge_padding = max(4, math.ceil(radius * 0.15))
    center = math.ceil(radius) + edge_padding
    image = Image.new("RGBA", (center * 2, center * 2), (0, 0, 0, 0))
    _draw_annotation(
        image,
        (float(center), float(center)),
        radius,
        cd_matrix,
        reference_dec_deg,
        sun_pa_deg,
        image_rotation_degrees,
    )
    return image


def _draw_annotation(
    image: Image.Image,
    origin: tuple[float, float],
    radius: float,
    cd_matrix: tuple[float, float, float, float],
    reference_dec_deg: float,
    sun_pa_deg: float,
    image_rotation_degrees: float,
) -> None:
    """Draw one annotation at a known center into an RGBA image."""
    draw = ImageDraw.Draw(image)
    north_angle = annotation_display_angle_degrees(cd_matrix, reference_dec_deg, 0.0, image_rotation_degrees)
    east_angle = annotation_display_angle_degrees(cd_matrix, reference_dec_deg, 90.0, image_rotation_degrees)
    sun_angle = annotation_display_angle_degrees(cd_matrix, reference_dec_deg, sun_pa_deg, image_rotation_degrees)
    white = (255, 255, 255)
    yellow = (255, 238, 160)
    shadow = (0, 0, 0)
    line_width = 3
    axis_width = max(1, round(line_width * 2 * 0.70))
    axis_length = radius * 0.65
    font = _annotation_font(max(8, round(radius * 0.30)), bold=True)

    axis_segments = [(origin, _point(origin, angle, axis_length)) for angle in (north_angle, east_angle)]
    sun_start = _point(origin, sun_angle, radius * 0.10)
    sun_end = _point(origin, sun_angle, radius)
    arrow_size = radius * 0.18
    sun_segments = [(sun_start, sun_end)]
    sun_segments.extend(
        (sun_end, _point(sun_end, head_angle, arrow_size))
        for head_angle in (sun_angle + 155.0, sun_angle + 205.0)
    )
    # Draw every outline first, then every colored stroke. This prevents a
    # later black outline from cutting through an already-drawn colored mark.
    for start, end in axis_segments:
        draw.line((start, end), fill=shadow, width=axis_width + 2)
    for start, end in sun_segments:
        draw.line((start, end), fill=shadow, width=line_width + 2)
    for start, end in axis_segments:
        draw.line((start, end), fill=white, width=axis_width)
    for start, end in sun_segments:
        draw.line((start, end), fill=yellow, width=line_width)
    # N is placed clockwise from its stick; E counterclockwise from its stick.
    text_stroke_width = 1
    axis_outline_half_width = (axis_width + 2) / 2.0
    for label, angle, perpendicular_angle in (
        ("N", north_angle, north_angle + 90.0),
        ("E", east_angle, east_angle - 90.0),
    ):
        bounds = draw.textbbox((0, 0), label, font=font, stroke_width=text_stroke_width)
        text_radius = math.hypot(bounds[2] - bounds[0], bounds[3] - bounds[1]) / 2.0
        # Displace from the line by the full label radius plus both outlines.
        label_offset = text_radius + axis_outline_half_width + 1.0
        location = _point(_point(origin, angle, axis_length * 0.9), perpendicular_angle, label_offset)
        draw.text(
            location,
            label,
            fill=white,
            font=font,
            anchor="mm",
            stroke_width=text_stroke_width,
            stroke_fill=shadow,
        )
    # A drawn circumpunct is font-independent and remains legible in packages.
    sun_radius = radius * 0.115
    sun_outline_half_width = (line_width + 2) / 2.0
    # Keep the circumpunct's outer edge clear of the arrow's black outline.
    sun_mark_offset = sun_radius + sun_outline_half_width * 2.0 + 1.0
    sun_mark = _point(_point(origin, sun_angle, radius * 0.9), sun_angle - 90.0, sun_mark_offset)
    draw.ellipse(
        (sun_mark[0] - sun_radius, sun_mark[1] - sun_radius, sun_mark[0] + sun_radius, sun_mark[1] + sun_radius),
        outline=shadow,
        width=line_width + 2,
    )
    draw.ellipse(
        (sun_mark[0] - sun_radius, sun_mark[1] - sun_radius, sun_mark[0] + sun_radius, sun_mark[1] + sun_radius),
        outline=yellow,
        width=line_width,
    )
    dot_radius = max(1.0, radius * 0.028)
    draw.ellipse(
        (sun_mark[0] - dot_radius, sun_mark[1] - dot_radius, sun_mark[0] + dot_radius, sun_mark[1] + dot_radius),
        fill=yellow,
        outline=shadow,
    )
