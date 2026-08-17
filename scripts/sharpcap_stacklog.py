#!/usr/bin/env python3
"""Read SharpCap 4.1+ Live Stack frame logs without fixed column positions."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


MINIMUM_SHARPCAP_VERSION = (4, 1, 10745, 0)
REQUIRED_COLUMNS = {
    "date/time",
    "frame index",
    "frame stacked?",
    "frame offset x (pixels)",
    "frame offset y (pixels)",
    "frame rotation (degrees)",
    "raw frame file",
}


@dataclass(frozen=True)
class SharpCapFrame:
    path: Path
    time: datetime
    stack_time: datetime
    frame_index: int
    stacked: bool
    detected_stars: int | None
    brightness: float | None
    fwhm_px: float | None
    offset_x_px: float | None
    offset_y_px: float | None
    rotation_deg: float | None

    @property
    def has_alignment(self) -> bool:
        return self.offset_x_px is not None and self.offset_y_px is not None and self.rotation_deg is not None


@dataclass
class SharpCapSession:
    root: Path
    stacklog: Path
    settings_file: Path | None
    settings: dict[str, str]
    version_text: str | None
    version: tuple[int, ...] | None
    exposure_seconds: float | None
    object_name: str
    frames: list[SharpCapFrame]
    missing_raw_rows: int
    rejected_rows: int
    alignment_enabled: bool
    alignment_complete: bool


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def parse_iso_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo or timezone.utc
        when = when.replace(tzinfo=local_tz)
    return when.astimezone(timezone.utc)


def parse_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def parse_int(value: str | None) -> int | None:
    number = parse_float(value)
    return int(number) if number is not None else None


def parse_bool(value: str | None) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y"}


def find_named_file(directory: Path, filename: str) -> Path | None:
    if not directory.is_dir():
        return None
    expected = filename.casefold()
    for candidate in directory.iterdir():
        if candidate.is_file() and candidate.name.casefold() == expected:
            return candidate
    return None


def find_stacklog(source_dir: Path) -> tuple[Path, Path] | None:
    source = source_dir.resolve()
    if source.is_file():
        if source.name.casefold() == "stacklog.csv":
            return source.parent, source
        return None
    direct = find_named_file(source, "stacklog.csv")
    if direct is not None:
        return source, direct
    parent = source.parent
    candidate = find_named_file(parent, "stacklog.csv")
    if candidate is not None:
        return parent, candidate
    return None


def read_settings(*roots: Path) -> tuple[Path | None, dict[str, str]]:
    visited: set[Path] = set()
    path: Path | None = None
    for root in roots:
        resolved = root.resolve()
        if resolved in visited or not resolved.is_dir():
            continue
        visited.add(resolved)
        candidates = sorted(
            (
                candidate
                for candidate in resolved.iterdir()
                if candidate.is_file() and candidate.name.casefold().endswith(".camerasettings.txt")
            ),
            key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name),
        )
        if candidates:
            path = candidates[-1]
            break
    if path is None:
        return None, {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return path, values


def parse_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = re.search(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", value)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def version_at_least(version: tuple[int, ...], required: tuple[int, ...]) -> bool:
    length = max(len(version), len(required))
    return (*version, *([0] * (length - len(version)))) >= (*required, *([0] * (length - len(required))))


def parse_exposure_seconds(settings: dict[str, str]) -> float | None:
    value = settings.get("Exposure", "")
    match = re.fullmatch(r"\s*([0-9.]+)\s*(us|ms|s)?\s*", value, flags=re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "s").casefold()
    return number / 1_000_000.0 if unit == "us" else number / 1000.0 if unit == "ms" else number


def resolve_raw_file(source_dir: Path, root: Path, supplied: str) -> Path | None:
    text = supplied.strip().strip('"')
    if not text:
        return None
    basename = re.split(r"[\\/]", text)[-1]
    source = source_dir.resolve()
    session_root = root.resolve()
    local_candidates = {
        candidate.resolve()
        for candidate in (
            source / basename,
            source / "rawframes" / basename,
            session_root / basename,
            session_root / "rawframes" / basename,
        )
        if candidate.is_file()
    }
    if len(local_candidates) == 1:
        return next(iter(local_candidates))
    if len(local_candidates) > 1:
        locations = ", ".join(str(path) for path in sorted(local_candidates, key=str))
        raise ValueError(f"Raw frame filename is ambiguous after relocation: {basename}: {locations}")

    recursive_matches: set[Path] = set()
    for search_root in (source, session_root):
        if search_root.is_dir():
            recursive_matches.update(
                path.resolve()
                for path in search_root.rglob("*")
                if path.is_file() and path.name.casefold() == basename.casefold()
            )
    if len(recursive_matches) == 1:
        return next(iter(recursive_matches))
    if len(recursive_matches) > 1:
        locations = ", ".join(str(path) for path in sorted(recursive_matches, key=str))
        raise ValueError(f"Raw frame filename is ambiguous after relocation: {basename}: {locations}")

    # The path recorded by SharpCap is intentionally last. A copied and
    # calibrated same-name frame beside the dropped folder must win over the
    # original raw file when both still exist.
    supplied_path = Path(text)
    if supplied_path.is_file():
        return supplied_path.resolve()
    return None


def fits_midpoint(path: Path) -> datetime | None:
    if path.suffix.casefold() not in {".fit", ".fits"}:
        return None
    try:
        from moving_target_stack import read_fits_header, parse_time

        header, _cards, _offset = read_fits_header(path)
        for key in ("DATE-AVG", "DATE-MID"):
            if key in header:
                return parse_time(header[key])
        if "DATE-OBS" in header and ("EXPTIME" in header or "EXPOSURE" in header):
            exposure = float(header.get("EXPTIME") or header.get("EXPOSURE") or 0.0)
            return parse_time(header["DATE-OBS"]) + timedelta(seconds=exposure / 2.0)
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return None


def infer_object_name(root: Path) -> str:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", root.parent.name) and root.parent.parent.name:
        return root.parent.parent.name
    return root.parent.name or root.name


def load_sharpcap_session(source_dir: Path, include_rejected: bool = False) -> SharpCapSession | None:
    source = source_dir.resolve()
    found = find_stacklog(source_dir)
    if found is None:
        return None
    root, stacklog = found
    settings_file, settings = read_settings(source, root, source.parent)
    version_text = settings.get("SharpCapVersion")
    version = parse_version(version_text)
    required = ".".join(str(item) for item in MINIMUM_SHARPCAP_VERSION)
    if version is None:
        raise RuntimeError(
            f"SharpCapVersion was not found in a CameraSettings file beside {stacklog}. "
            f"Version {required} or later is required because early 4.1 StackLog timestamps may be incorrect."
        )
    if not version_at_least(version, MINIMUM_SHARPCAP_VERSION):
        raise RuntimeError(
            f"SharpCap {version_text} is older than supported version {required}; "
            "early 4.1 StackLog timestamps may be incorrect."
        )
    exposure_seconds = parse_exposure_seconds(settings)
    frames: list[SharpCapFrame] = []
    missing_raw_rows = 0
    rejected_rows = 0
    with stacklog.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"SharpCap StackLog has no header: {stacklog}")
        field_map = {normalize_header(name): name for name in reader.fieldnames if name is not None}
        missing_columns = sorted(REQUIRED_COLUMNS - set(field_map))
        if missing_columns:
            raise ValueError(f"SharpCap StackLog is missing required column(s): {', '.join(missing_columns)}")
        for raw_row in reader:
            row = {key: raw_row.get(original, "") for key, original in field_map.items()}
            stacked = parse_bool(row.get("frame stacked?"))
            if not stacked:
                rejected_rows += 1
                if not include_rejected:
                    continue
            raw_path = resolve_raw_file(source, root, row.get("raw frame file", ""))
            if raw_path is None:
                missing_raw_rows += 1
                continue
            stack_time = parse_iso_time(row["date/time"])
            midpoint = fits_midpoint(raw_path)
            if midpoint is None:
                midpoint = stack_time - timedelta(seconds=(exposure_seconds or 0.0) / 2.0)
            frames.append(
                SharpCapFrame(
                    path=raw_path,
                    time=midpoint,
                    stack_time=stack_time,
                    frame_index=parse_int(row.get("frame index")) or len(frames) + 1,
                    stacked=stacked,
                    detected_stars=parse_int(row.get("detected star count")),
                    brightness=parse_float(row.get("frame star brightness")),
                    fwhm_px=parse_float(row.get("frame star fwhm")),
                    offset_x_px=parse_float(row.get("frame offset x (pixels)")),
                    offset_y_px=parse_float(row.get("frame offset y (pixels)")),
                    rotation_deg=parse_float(row.get("frame rotation (degrees)")),
                )
            )
    frames.sort(key=lambda frame: (frame.time, frame.frame_index))
    if not frames:
        raise FileNotFoundError(f"No usable raw frames referenced by {stacklog}")
    alignment_enabled = parse_bool(settings.get("LiveStack.AlignFrames"))
    return SharpCapSession(
        root=root,
        stacklog=stacklog,
        settings_file=settings_file,
        settings=settings,
        version_text=version_text,
        version=version,
        exposure_seconds=exposure_seconds,
        object_name=infer_object_name(root),
        frames=frames,
        missing_raw_rows=missing_raw_rows,
        rejected_rows=rejected_rows,
        alignment_enabled=alignment_enabled,
        alignment_complete=alignment_enabled and all(frame.stacked and frame.has_alignment for frame in frames),
    )


def write_manifest(
    path: Path,
    session: SharpCapSession,
    selected_paths: list[Path],
    preprocessing: dict[str, object] | None = None,
) -> Path:
    selected = {item.resolve() for item in selected_paths}
    frames = []
    selected_frames: list[SharpCapFrame] = []
    for frame in session.frames:
        if frame.path.resolve() not in selected:
            continue
        selected_frames.append(frame)
        payload = asdict(frame)
        payload["path"] = str(frame.path)
        payload["time"] = frame.time.isoformat()
        payload["stack_time"] = frame.stack_time.isoformat()
        frames.append(payload)
    document = {
        "format": "sharpcap-live-stack-v1",
        "root": str(session.root),
        "stacklog": str(session.stacklog),
        "settings_file": str(session.settings_file) if session.settings_file else None,
        "settings": session.settings,
        "preprocessing": preprocessing,
        "sharpcap_version": session.version_text,
        "exposure_seconds": session.exposure_seconds,
        "object": session.object_name,
        "alignment_complete": session.alignment_enabled and all(
            frame.stacked and frame.has_alignment for frame in selected_frames
        ),
        "alignment_enabled": session.alignment_enabled,
        "frames": frames,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def load_manifest(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != "sharpcap-live-stack-v1" or not isinstance(payload.get("frames"), list):
        raise ValueError(f"Unsupported SharpCap manifest: {path}")
    return payload
