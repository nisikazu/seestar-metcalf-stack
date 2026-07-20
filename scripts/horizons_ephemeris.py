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
SBDB_API_URL = "https://ssd-api.jpl.nasa.gov/sbdb.api"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class FitsFrame:
    path: Path
    date_obs: datetime
    object_name: str | None
    site_long_deg: float | None
    site_lat_deg: float | None
    site_elevation_m: float | None
    ra_deg: float | None
    dec_deg: float | None


@dataclass(frozen=True)
class ObjectCandidate:
    command: str
    label: str
    source: str
    confidence: str


class HorizonsIdentificationError(RuntimeError):
    """The target command did not identify a usable Horizons object."""


class HorizonsResponseError(RuntimeError):
    """Horizons returned a response that could not be parsed as ephemeris."""


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
    parser.add_argument("-v", "--verbose", action="store_true", help="Show each Horizons HTTP attempt immediately")
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
                ra_deg=as_float(header.get("RA") or header.get("OBJCTRA")),
                dec_deg=as_float(header.get("DEC") or header.get("OBJCTDEC")),
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


def _add_candidate(candidates: list[ObjectCandidate], command: str, label: str, source: str, confidence: str) -> None:
    command = command.strip()
    if not command or any(item.command.upper() == command.upper() for item in candidates):
        return
    candidates.append(ObjectCandidate(command, label, source, confidence))


def generate_object_candidates(object_name: str) -> list[ObjectCandidate]:
    """Create safe Horizons commands from common Seestar/MPC naming forms."""
    text = re.sub(r"\s+", " ", object_name.strip().strip("'\""))
    candidates: list[ObjectCandidate] = []
    if not text:
        return candidates
    if text.upper().startswith(("DES=", "COMNAM=")):
        _add_candidate(candidates, text, text, "explicit-horizons-command", "high")
        return candidates

    comet = re.match(
        r"^(?P<prefix>[PCDXA])\s*/?\s*(?P<year>\d{4})\s*(?P<half>[A-Z]\d+)"
        r"(?:\s*\((?P<name>[^)]+)\))?$",
        text,
        flags=re.IGNORECASE,
    )
    if comet:
        prefix = comet.group("prefix").upper()
        designation = f"{prefix}/{comet.group('year')} {comet.group('half').upper()}"
        _add_candidate(candidates, f"DES={designation};CAP;NOFRAG", designation, "comet-designation", "high")
        if comet.group("name"):
            _add_candidate(candidates, f"NAME={comet.group('name').strip()};", comet.group("name").strip(), "comet-name", "medium")

    compact_periodic = re.match(r"^(?P<number>\d{1,4})(?P<prefix>[PCDXA])(?P<name>[A-Za-z][A-Za-z0-9 ._-]*)$", text)
    if compact_periodic:
        designation = f"{compact_periodic.group('number')}{compact_periodic.group('prefix').upper()}"
        _add_candidate(candidates, f"DES={designation};CAP;NOFRAG", designation, "compact-periodic-comet", "high")
        name = compact_periodic.group("name").strip(" _-")
        if name:
            _add_candidate(candidates, f"NAME={name};", name, "compact-comet-name", "medium")

    spaced_periodic = re.match(r"^(?P<number>\d{1,4})\s*(?P<prefix>[PCDXA])\s+(?P<name>.+)$", text, flags=re.IGNORECASE)
    if spaced_periodic:
        designation = f"{spaced_periodic.group('number')}{spaced_periodic.group('prefix').upper()}"
        _add_candidate(candidates, f"DES={designation};CAP;NOFRAG", designation, "spaced-periodic-comet", "high")
        _add_candidate(candidates, f"NAME={spaced_periodic.group('name').strip()};", spaced_periodic.group("name").strip(), "spaced-comet-name", "medium")

    numbered = re.match(r"^\(?(?P<number>\d{1,7})\)?(?:\s+(?P<name>.+))?$", text)
    if numbered:
        _add_candidate(candidates, f"{numbered.group('number')};", numbered.group("number"), "numbered-asteroid", "high")
        if numbered.group("name"):
            _add_candidate(candidates, f"NAME={numbered.group('name').strip()};", numbered.group("name").strip(), "asteroid-name", "medium")

    provisional = re.match(r"^(\d{4}\s+[A-Z]{1,2}\d{0,2})$", text, flags=re.IGNORECASE)
    if provisional:
        designation = re.sub(r"\s+", " ", provisional.group(1).upper())
        _add_candidate(candidates, f"DES={designation};", designation, "provisional-designation", "high")

    packed = re.match(r"^[A-Z]\d{2}[A-Z]\d{2}[A-Z]\d?$", text, flags=re.IGNORECASE)
    if packed:
        designation = text.upper()
        _add_candidate(candidates, f"DES={designation};", designation, "packed-mpc-designation", "high")

    _add_candidate(candidates, f"NAME={text};", text, "name-fallback", "low")
    _add_candidate(candidates, text, text, "raw-fallback", "low")
    return candidates


def normalize_command(object_name: str) -> str:
    candidates = generate_object_candidates(object_name)
    return candidates[0].command if candidates else object_name.strip()


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


def fetch_result(url: str, retries: int, retry_delay_sec: float, verbose: bool = False) -> str:
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        if verbose:
            print(f"Horizons HTTP attempt {attempt}/{max(1, retries)} (timeout 60s)", file=sys.stderr, flush=True)
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
        lowered = result.lower()
        error_type = HorizonsIdentificationError if any(
            marker in lowered for marker in ("no matches found", "multiple matches", "small-body index search results", "ambiguous")
        ) else HorizonsResponseError
        raise error_type(
            "Horizons response did not contain $$SOE/$$EOE ephemeris markers. "
            "The target command may not have resolved to a unique ephemeris object. "
            f"Response begins:\n{snippet}"
        )
    body = result.split("$$SOE", 1)[1].split("$$EOE", 1)[0]
    rows: list[EphemerisRow] = []
    for line, expected_time in zip((line for line in body.splitlines() if line.strip()), expected_times):
        fields = next(csv.reader([line], skipinitialspace=True))
        if len(fields) < 5:
            raise HorizonsResponseError(f"Could not parse Horizons CSV row: {line}")
        rows.append(
            EphemerisRow(
                time=expected_time,
                horizons_time=fields[0].strip(),
                ra_deg=float(fields[3]),
                dec_deg=float(fields[4]),
            )
        )
    return rows


def sbdb_lookup_terms(object_name: str) -> list[str]:
    """Return conservative lookup strings for JPL SBDB fallback search."""
    text = re.sub(r"\s+", " ", object_name.strip().strip("'\""))
    terms = [text]
    compact = re.match(r"^(\d{1,4})([PCDXA])([A-Za-z].*)$", text, flags=re.IGNORECASE)
    if compact:
        terms.extend([f"{compact.group(1)}{compact.group(2).upper()}", compact.group(3).strip(" _-")])
    spaced = re.match(r"^(\d{1,4})([PCDXA])\s+(.+)$", text, flags=re.IGNORECASE)
    if spaced:
        terms.extend([f"{spaced.group(1)}{spaced.group(2).upper()}", spaced.group(3).strip()])
    comet = re.match(r"^[PCDXA]\s*/?\s*(\d{4})\s*([A-Z]\d+)(?:\s*\(([^)]+)\))?$", text, flags=re.IGNORECASE)
    if comet:
        terms.append(f"{comet.group(1)} {comet.group(2).upper()}")
        if comet.group(3):
            terms.append(comet.group(3).strip())
    return list(dict.fromkeys(term for term in terms if term))


def fetch_sbdb_candidates(object_name: str, retries: int, retry_delay_sec: float) -> list[ObjectCandidate]:
    """Ask SBDB only after direct Horizons commands fail; no site data is sent."""
    found: list[ObjectCandidate] = []
    for term in sbdb_lookup_terms(object_name):
        query = urllib.parse.urlencode({"sstr": term, "full-prec": "true"})
        url = f"{SBDB_API_URL}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            print(f"SBDB lookup failed for {term!r}: {exc}", file=sys.stderr)
            continue
        obj = payload.get("object")
        if not isinstance(obj, dict):
            continue
        designation = str(obj.get("des") or "").strip()
        fullname = str(obj.get("fullname") or "").strip()
        if designation:
            _add_candidate(found, f"DES={designation};CAP;NOFRAG", fullname or designation, "SBDB-designation", "high")
    return found


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


def query_group(
    command: str,
    group: list[datetime],
    args: argparse.Namespace,
    site_long: float | None,
    site_lat: float | None,
    site_elev_km: float | None,
) -> list[EphemerisRow]:
    url = build_query(command, group, args.center, site_long, site_lat, site_elev_km)
    result = fetch_result(url, args.retries, args.retry_delay_sec, args.verbose)
    return parse_horizons_result(result, group)


def resolve_object_command(
    object_name: str,
    args: argparse.Namespace,
    first_group: list[datetime],
    site_long: float | None,
    site_lat: float | None,
    site_elev_km: float | None,
) -> tuple[ObjectCandidate, list[EphemerisRow], list[ObjectCandidate]]:
    """Resolve a FITS OBJECT using direct forms, then SBDB canonicalization."""
    candidates = generate_object_candidates(object_name)
    attempted: list[ObjectCandidate] = []
    for candidate in candidates:
        attempted.append(candidate)
        print(
            f"Trying Horizons target: {candidate.command} "
            f"(source={candidate.source}, confidence={candidate.confidence})",
            file=sys.stderr,
        )
        try:
            rows = query_group(candidate.command, first_group, args, site_long, site_lat, site_elev_km)
        except HorizonsIdentificationError as exc:
            print(f"Target candidate did not resolve: {candidate.command}: {exc}", file=sys.stderr)
            continue
        return candidate, rows, attempted

    sbdb_candidates = fetch_sbdb_candidates(object_name, args.retries, args.retry_delay_sec)
    for candidate in sbdb_candidates:
        if any(item.command.upper() == candidate.command.upper() for item in attempted):
            continue
        attempted.append(candidate)
        print(f"Trying SBDB-derived Horizons target: {candidate.command}", file=sys.stderr)
        try:
            rows = query_group(candidate.command, first_group, args, site_long, site_lat, site_elev_km)
        except HorizonsIdentificationError as exc:
            print(f"SBDB candidate did not resolve: {candidate.command}: {exc}", file=sys.stderr)
            continue
        return candidate, rows, attempted

    attempted_text = ", ".join(item.command for item in attempted) or "(none)"
    raise SystemExit(
        f"Could not identify target {object_name!r} in JPL Horizons. "
        f"Tried: {attempted_text}. Use --command with an explicit Horizons COMMAND value."
    )


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(line_buffering=True, write_through=True)
    args = parse_args()
    frames = filter_frames(args, load_frames(args.source_dir, args.limit))
    if not frames:
        raise SystemExit(f"No FITS frames with DATE-OBS found in {args.source_dir}")

    object_name = args.object or frames[0].object_name
    if not object_name and not args.command:
        raise SystemExit("Target object was not provided and FITS OBJECT header is missing")
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

    times = [frame.date_obs for frame in frames]
    groups = list(chunked(times, max(1, args.chunk_size)))
    if args.command:
        selected = ObjectCandidate(args.command, args.command, "explicit-command", "explicit")
        first_rows = query_group(args.command, groups[0], args, site_long, site_lat, site_elev_km)
        attempted = [selected]
    else:
        selected, first_rows, attempted = resolve_object_command(
            object_name or "", args, groups[0], site_long, site_lat, site_elev_km
        )

    all_rows: list[EphemerisRow] = list(first_rows)
    for index, group in enumerate(groups[1:], start=2):
        print(f"Horizons request {index}/{len(groups)}: {iso_z(group[0])} .. {iso_z(group[-1])}", file=sys.stderr)
        all_rows.extend(query_group(selected.command, group, args, site_long, site_lat, site_elev_km))

    header_ra = frames[0].ra_deg
    header_dec = frames[0].dec_deg
    header_offset_arcsec: float | None = None
    if header_ra is not None and header_dec is not None and first_rows:
        import math

        delta_ra = (first_rows[0].ra_deg - header_ra + 180.0) % 360.0 - 180.0
        mean_dec_rad = math.radians((first_rows[0].dec_deg + header_dec) / 2.0)
        header_offset_arcsec = math.hypot(delta_ra * math.cos(mean_dec_rad), first_rows[0].dec_deg - header_dec) * 3600.0
        if header_offset_arcsec > 300.0:
            print(
                f"Warning: Horizons position differs from FITS RA/DEC by {header_offset_arcsec / 60.0:.2f} arcmin; "
                "this is a warning, not an automatic rejection.",
                file=sys.stderr,
            )

    meta: dict[str, object] = {
        "object": object_name or command,
        "command": selected.command,
        "object_raw": object_name,
        "object_normalized": selected.label,
        "resolution_method": selected.source,
        "attempted_commands": [item.command for item in attempted],
        "center": args.center,
        "frame_count": len(frames),
        "include_failed_frames": args.include_failed_frames,
        "first_time": iso_z(frames[0].date_obs),
        "last_time": iso_z(frames[-1].date_obs),
        "site_long_deg": site_long if args.center == "fits-site" else None,
        "site_lat_deg": site_lat if args.center == "fits-site" else None,
        "site_elevation_km": site_elev_km if args.center == "fits-site" else None,
        "source": "JPL Horizons API",
        "fits_header_ra_deg": header_ra,
        "fits_header_dec_deg": header_dec,
        "header_offset_arcsec": header_offset_arcsec,
    }
    write_csv(args.output, all_rows, meta)

    meta_path = args.meta_output or args.output.with_suffix(args.output.suffix + ".meta.json")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"Wrote {len(all_rows)} Horizons ephemeris rows: {args.output}")
    print(f"Wrote metadata: {meta_path}")
    print(f"Object: {meta['object']}  COMMAND={selected.command}  resolution={selected.source}  center={args.center}")
    print(f"Range: {meta['first_time']} .. {meta['last_time']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
