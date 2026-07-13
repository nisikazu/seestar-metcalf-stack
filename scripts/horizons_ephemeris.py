#!/usr/bin/env python
"""Generate a moving-target ephemeris CSV from JPL Horizons.

The primary input is a Seestar subframe directory.  The script reads FITS
DATE-OBS timestamps, asks Horizons for apparent RA/Dec at those exact times,
and writes a CSV that scripts/moving_target_stack.py can consume.

By default the query is geocentric.  Topocentric site coordinates from FITS
headers are only sent when --center fits-site and --allow-site-upload are both
provided, because SITELONG/SITELAT can be privacy-sensitive.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


API_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class FitsFrame:
    path: Path
    date_obs: datetime
    object_name: str | None
    site_long_deg: float | None
    site_lat_deg: float | None
    site_elevation_m: float | None


@dataclass
class EphemerisRow:
    time: datetime
    horizons_time: str
    ra_deg: float
    dec_deg: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create RA/Dec CSV from JPL Horizons for Seestar FITS frames")
    parser.add_argument("--source-dir", required=True, type=Path, help="Directory containing Seestar .fit/.fits frames")
    parser.add_argument("--output", required=True, type=Path, help="Output CSV path")
    parser.add_argument("--object", help="Target name/designation. Defaults to the FITS OBJECT header")
    parser.add_argument("--command", help="Raw Horizons COMMAND value. Overrides --object normalization")
    parser.add_argument(
        "--center",
        choices=("geocenter", "fits-site"),
        default="geocenter",
        help="Observer center. geocenter is privacy-safe; fits-site sends SITELONG/SITELAT to JPL.",
    )
    parser.add_argument(
        "--allow-site-upload",
        action="store_true",
        help="Required with --center fits-site to acknowledge sending FITS site coordinates to JPL Horizons.",
    )
    parser.add_argument("--elevation-km", type=float, help="Override site elevation in km for --center fits-site")
    parser.add_argument("--chunk-size", type=int, default=50, help="Number of timestamps per Horizons request")
    parser.add_argument("--retries", type=int, default=3, help="Retries per Horizons request")
    parser.add_argument("--retry-delay-sec", type=float, default=2.0, help="Initial retry delay in seconds")
    parser.add_argument("--limit", type=int, help="Use only the first N frames, useful for smoke tests")
    parser.add_argument("--after", help="Keep frames at or after this UTC ISO timestamp")
    parser.add_argument("--before", help="Keep frames at or before this UTC ISO timestamp")
    parser.add_argument("--session-gap-min", type=float, help="Split frames into sessions at gaps larger than this many minutes")
    parser.add_argument("--session-index", type=int, default=1, help="1-based session to use with --session-gap-min")
    parser.add_argument(
        "--include-failed-frames",
        action="store_true",
        help="Include Seestar files whose names contain '_failed_'. They are skipped by default.",
    )
    parser.add_argument("--meta-output", type=Path, help="Optional JSON metadata output path")
    return parser.parse_args()


def read_fits_header(path: Path) -> dict[str, object]:
    cards: list[str] = []
    with path.open("rb") as handle:
        while True:
            block = handle.read(2880)
            if not block:
                break
            for idx in range(0, len(block), 80):
                card = block[idx : idx + 80].decode("ascii", errors="ignore")
                cards.append(card)
                if card.startswith("END"):
                    return parse_cards(cards)
    return parse_cards(cards)


def parse_cards(cards: Iterable[str]) -> dict[str, object]:
    header: dict[str, object] = {}
    for card in cards:
        if len(card) < 10 or card[8] != "=":
            continue
        key = card[:8].strip()
        raw = card[10:]
        value_part = raw.split("/", 1)[0].strip()
        if not value_part:
            continue
        if value_part.startswith("'"):
            end = value_part.find("'", 1)
            header[key] = value_part[1:end] if end >= 0 else value_part.strip("'")
        elif value_part in ("T", "F"):
            header[key] = value_part == "T"
        else:
            try:
                header[key] = int(value_part)
            except ValueError:
                try:
                    header[key] = float(value_part.replace("D", "E"))
                except ValueError:
                    header[key] = value_part
    return header


def parse_fits_datetime(value: object) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def parse_cli_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def as_float(value: object | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_frames(source_dir: Path, limit: int | None = None) -> list[FitsFrame]:
    paths = sorted(list(source_dir.glob("*.fit")) + list(source_dir.glob("*.fits")))
    if limit is not None:
        paths = paths[:limit]
    frames: list[FitsFrame] = []
    for path in paths:
        header = read_fits_header(path)
        if "DATE-OBS" not in header:
            continue
        frames.append(
            FitsFrame(
                path=path,
                date_obs=parse_fits_datetime(header["DATE-OBS"]),
                object_name=str(header.get("OBJECT")).strip() if header.get("OBJECT") else None,
                site_long_deg=as_float(header.get("SITELONG")),
                site_lat_deg=as_float(header.get("SITELAT")),
                site_elevation_m=as_float(header.get("SITEELEV") or header.get("ELEVATIO") or header.get("ELEVATION")),
            )
        )
    frames.sort(key=lambda frame: frame.date_obs)
    return frames


def is_failed_frame(path: Path) -> bool:
    return "_failed_" in path.name.lower()


def filter_frames(args: argparse.Namespace, frames: list[FitsFrame]) -> list[FitsFrame]:
    if not args.include_failed_frames:
        original_count = len(frames)
        frames = [frame for frame in frames if not is_failed_frame(frame.path)]
        skipped = original_count - len(frames)
        if skipped:
            print(f"Skipped {skipped} failed frame(s); use --include-failed-frames to keep them.", file=sys.stderr)
    if args.after:
        after = parse_cli_time(args.after)
        frames = [frame for frame in frames if frame.date_obs >= after]
    if args.before:
        before = parse_cli_time(args.before)
        frames = [frame for frame in frames if frame.date_obs <= before]
    if args.session_gap_min is not None:
        sessions: list[list[FitsFrame]] = []
        current: list[FitsFrame] = []
        gap_seconds = args.session_gap_min * 60.0
        previous: FitsFrame | None = None
        for frame in frames:
            if previous is not None and (frame.date_obs - previous.date_obs).total_seconds() > gap_seconds:
                if current:
                    sessions.append(current)
                current = []
            current.append(frame)
            previous = frame
        if current:
            sessions.append(current)
        if args.session_index < 1 or args.session_index > len(sessions):
            raise SystemExit(f"--session-index {args.session_index} is out of range; found {len(sessions)} session(s)")
        frames = sessions[args.session_index - 1]
    return frames


def normalize_command(object_name: str) -> str:
    text = object_name.strip()
    if text.upper().startswith("DES=") or text.upper().startswith("COMNAM="):
        return text

    comet = re.search(r"\b([CPDXA])\s*/?\s*(\d{4})\s+([A-Z]\d+)\b", text, flags=re.IGNORECASE)
    if comet:
        prefix, year, half_month = comet.groups()
        return f"DES={prefix.upper()}/{year} {half_month.upper()};CAP;NOFRAG"

    numbered = re.search(r"\((\d+)\)", text)
    if numbered:
        return f"{numbered.group(1)};"

    leading_number = re.match(r"^(\d+)(?:\s+|$)", text)
    if leading_number:
        return f"{leading_number.group(1)};"

    return text


def horizons_time(when: datetime) -> str:
    when = when.astimezone(timezone.utc)
    month = MONTHS[when.month - 1]
    frac = f"{when.microsecond:06d}".rstrip("0")
    second = f"{when.second:02d}" + (f".{frac}" if frac else "")
    return f"{when.year:04d}-{month}-{when.day:02d} {when.hour:02d}:{when.minute:02d}:{second}"


def iso_z(when: datetime) -> str:
    when = when.astimezone(timezone.utc)
    text = when.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return text.replace(".000000Z", "Z")


def chunked(items: list[datetime], size: int) -> Iterable[list[datetime]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def quoted(value: str) -> str:
    return f"'{value}'"


def build_query(
    command: str,
    times: list[datetime],
    center: str,
    site_long: float | None,
    site_lat: float | None,
    site_elevation_km: float | None,
) -> str:
    params: dict[str, str] = {
        "format": "json",
        "COMMAND": quoted(command),
        "OBJ_DATA": quoted("NO"),
        "MAKE_EPHEM": quoted("YES"),
        "EPHEM_TYPE": quoted("OBSERVER"),
        "CENTER": quoted("500@399" if center == "geocenter" else "coord@399"),
        "TLIST_TYPE": quoted("CAL"),
        "TLIST": " ".join(quoted(horizons_time(item)) for item in times),
        "TIME_TYPE": quoted("UT"),
        "TIME_DIGITS": quoted("FRACSEC"),
        "ANG_FORMAT": quoted("DEG"),
        "CSV_FORMAT": quoted("YES"),
        "EXTRA_PREC": quoted("YES"),
        "QUANTITIES": quoted("1"),
    }
    if center == "fits-site":
        if site_long is None or site_lat is None:
            raise ValueError("SITELONG/SITELAT are required for --center fits-site")
        elev = 0.0 if site_elevation_km is None else site_elevation_km
        params["COORD_TYPE"] = quoted("GEODETIC")
        params["SITE_COORD"] = quoted(f"{site_long:.8f},{site_lat:.8f},{elev:.5f}")

    return API_URL + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)


def fetch_result(url: str, retries: int, retry_delay_sec: float) -> str:
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except Exception as exc:  # Horizons occasionally returns transient 5xx.
            last_error = exc
            if attempt >= max(1, retries):
                raise
            delay = retry_delay_sec * (2 ** (attempt - 1))
            print(f"Horizons request failed on attempt {attempt}/{retries}: {exc}; retrying in {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
    else:
        raise RuntimeError(f"Horizons request failed: {last_error}")

    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    result = payload.get("result")
    if not result:
        raise RuntimeError(f"Unexpected Horizons response keys: {sorted(payload.keys())}")
    return result


def parse_horizons_result(result: str, expected_times: list[datetime]) -> list[EphemerisRow]:
    if "$$SOE" not in result or "$$EOE" not in result:
        snippet = "\n".join(line.rstrip() for line in result.splitlines()[:40])
        raise RuntimeError(
            "Horizons response did not contain $$SOE/$$EOE ephemeris markers. "
            "The target command may not have resolved to a unique ephemeris object. "
            f"Response begins:\n{snippet}"
        )
    body = result.split("$$SOE", 1)[1].split("$$EOE", 1)[0]
    rows: list[EphemerisRow] = []
    for line, expected_time in zip((line for line in body.splitlines() if line.strip()), expected_times):
        fields = next(csv.reader([line], skipinitialspace=True))
        if len(fields) < 5:
            raise RuntimeError(f"Could not parse Horizons CSV row: {line}")
        rows.append(
            EphemerisRow(
                time=expected_time,
                horizons_time=fields[0].strip(),
                ra_deg=float(fields[3]),
                dec_deg=float(fields[4]),
            )
        )
    return rows


def write_csv(path: Path, rows: list[EphemerisRow], meta: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "time",
        "ra_deg",
        "dec_deg",
        "horizons_time",
        "object",
        "command",
        "center",
        "site_long_deg",
        "site_lat_deg",
        "site_elevation_km",
        "source",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "time": iso_z(row.time),
                    "ra_deg": f"{row.ra_deg:.12f}",
                    "dec_deg": f"{row.dec_deg:.12f}",
                    "horizons_time": row.horizons_time,
                    "object": meta["object"],
                    "command": meta["command"],
                    "center": meta["center"],
                    "site_long_deg": "" if meta.get("site_long_deg") is None else meta["site_long_deg"],
                    "site_lat_deg": "" if meta.get("site_lat_deg") is None else meta["site_lat_deg"],
                    "site_elevation_km": "" if meta.get("site_elevation_km") is None else meta["site_elevation_km"],
                    "source": "JPL Horizons API",
                }
            )


def main() -> int:
    args = parse_args()
    frames = filter_frames(args, load_frames(args.source_dir, args.limit))
    if not frames:
        raise SystemExit(f"No FITS frames with DATE-OBS found in {args.source_dir}")

    object_name = args.object or frames[0].object_name
    if not object_name and not args.command:
        raise SystemExit("Target object was not provided and FITS OBJECT header is missing")
    command = args.command or normalize_command(object_name or "")

    site_long = frames[0].site_long_deg
    site_lat = frames[0].site_lat_deg
    site_elev_km = args.elevation_km
    if site_elev_km is None and frames[0].site_elevation_m is not None:
        site_elev_km = frames[0].site_elevation_m / 1000.0

    if args.center == "fits-site" and not args.allow_site_upload:
        raise SystemExit(
            "--center fits-site would send SITELONG/SITELAT from FITS headers to JPL Horizons. "
            "Re-run with --allow-site-upload only when that privacy tradeoff is intentional."
        )

    all_rows: list[EphemerisRow] = []
    times = [frame.date_obs for frame in frames]
    groups = list(chunked(times, max(1, args.chunk_size)))
    for index, group in enumerate(groups, start=1):
        print(f"Horizons request {index}/{len(groups)}: {iso_z(group[0])} .. {iso_z(group[-1])}", file=sys.stderr)
        url = build_query(command, group, args.center, site_long, site_lat, site_elev_km)
        result = fetch_result(url, args.retries, args.retry_delay_sec)
        all_rows.extend(parse_horizons_result(result, group))

    meta: dict[str, object] = {
        "object": object_name or command,
        "command": command,
        "center": args.center,
        "frame_count": len(frames),
        "include_failed_frames": args.include_failed_frames,
        "first_time": iso_z(frames[0].date_obs),
        "last_time": iso_z(frames[-1].date_obs),
        "site_long_deg": site_long if args.center == "fits-site" else None,
        "site_lat_deg": site_lat if args.center == "fits-site" else None,
        "site_elevation_km": site_elev_km if args.center == "fits-site" else None,
        "source": "JPL Horizons API",
    }
    write_csv(args.output, all_rows, meta)

    meta_path = args.meta_output or args.output.with_suffix(args.output.suffix + ".meta.json")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {len(all_rows)} Horizons ephemeris rows: {args.output}")
    print(f"Wrote metadata: {meta_path}")
    print(f"Object: {meta['object']}  COMMAND={command}  center={args.center}")
    print(f"Range: {meta['first_time']} .. {meta['last_time']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
