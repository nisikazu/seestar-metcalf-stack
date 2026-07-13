#!/usr/bin/env python3
"""Upload one FITS to Astrometry.net and save its calibration result."""

from __future__ import annotations

import json
import math
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


SCRIPT_DIR = (
    pathlib.Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else pathlib.Path(__file__).resolve().parent.parent
)
DEFAULT_FILE = SCRIPT_DIR / "downloads" / "98943 Torifune_sub" / "Light_98943 Torifune_20.0s_IRCUT_failed_20260708-210152.fit"
API_BASE = os.environ.get("ASTROMETRY_NET_API_BASE", "https://nova.astrometry.net/api").rstrip("/")
FETCH_RETRIES = int(os.environ.get("ASTROMETRY_NET_FETCH_RETRIES", "8"))
SCALE_MARGIN = float(os.environ.get("ASTROMETRY_NET_SCALE_MARGIN", "0.2"))
SEARCH_RADIUS_DEG = float(os.environ.get("ASTROMETRY_NET_SEARCH_RADIUS_DEG", "2.0"))


def retry_delay(attempt: int) -> float:
    return min(60.0, 2.0 * 2 ** max(0, attempt - 1))


def read_api_key() -> str:
    env_key = os.environ.get("ASTROMETRY_NET_API_KEY", "").strip()
    if env_key:
        return env_key
    key_path = SCRIPT_DIR / ".astrometry_api_key"
    if key_path.exists():
        file_key = key_path.read_text(encoding="utf-8").strip()
        if file_key:
            return file_key
    raise RuntimeError(
        "Astrometry.net API key was not found. Set ASTROMETRY_NET_API_KEY "
        "or put it in .astrometry_api_key."
    )


def request_bytes(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    method: str | None = None,
    retries: int = FETCH_RETRIES,
) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, max(1, retries) + 1):
        request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                status = getattr(response, "status", 200)
                body = response.read()
                if status < 500 and status != 429:
                    return body
                last_error = RuntimeError(f"HTTP {status} from {url}")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:400]
            last_error = RuntimeError(f"HTTP {error.code} from {url}: {body}")
            if error.code < 500 and error.code != 429:
                raise last_error from error
        except (OSError, urllib.error.URLError) as error:
            last_error = error
        if attempt < retries:
            delay = retry_delay(attempt)
            print(
                f"Astrometry request failed ({attempt}/{retries}); "
                f"retrying in {round(delay)}s: {last_error}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError(str(last_error or "Astrometry request failed")) from last_error


def json_request(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None) -> dict:
    body = request_bytes(url, data=data, headers=headers)
    try:
        parsed = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Non-JSON response from {url}: {body[:400]!r}") from error
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}")
    return parsed


def post_json_form(url: str, payload: dict) -> dict:
    data = urllib.parse.urlencode({"request-json": json.dumps(payload)}).encode("utf-8")
    return json_request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})


def login(api_key: str) -> str:
    result = post_json_form(f"{API_BASE}/login", {"apikey": api_key})
    if result.get("status") != "success" or not result.get("session"):
        raise RuntimeError(f"Astrometry login failed: {json.dumps(result)}")
    return str(result["session"])


def multipart_body(fields: dict[str, str], file_name: str, file_data: bytes) -> tuple[bytes, str]:
    boundary = f"----seestar-metcalf-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n",
            file_data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def parse_fits_value(raw: str) -> object:
    value = raw.split("/", 1)[0].strip()
    if value.startswith("'"):
        end = value.find("'", 1)
        return value[1:end if end >= 0 else None].strip()
    if value == "T":
        return True
    if value == "F":
        return False
    try:
        number = float(value.replace("D", "E"))
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def read_fits_header(file_path: pathlib.Path) -> dict[str, object]:
    header: dict[str, object] = {}
    with file_path.open("rb") as handle:
        while True:
            block = handle.read(2880)
            if not block:
                return header
            for offset in range(0, len(block), 80):
                card = block[offset : offset + 80].decode("ascii", errors="replace")
                if card.startswith("END"):
                    return header
                if len(card) >= 10 and card[8] == "=":
                    header[card[:8].strip()] = parse_fits_value(card[10:])


def positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def estimate_scale_hint(header: dict[str, object]) -> dict | None:
    focal_mm = positive_number(header.get("FOCALLEN"))
    x_pix_um = positive_number(header.get("XPIXSZ"))
    y_pix_um = positive_number(header.get("YPIXSZ"))
    if not focal_mm or not x_pix_um or not y_pix_um:
        return None
    x_bin = positive_number(header.get("XBINNING")) or positive_number(header.get("CCDXBIN")) or 1
    y_bin = positive_number(header.get("YBINNING")) or positive_number(header.get("CCDYBIN")) or 1
    x_scale = 206.265 * x_pix_um * x_bin / focal_mm
    y_scale = 206.265 * y_pix_um * y_bin / focal_mm
    arcsec_per_pix = (x_scale + y_scale) / 2
    if not math.isfinite(arcsec_per_pix) or arcsec_per_pix <= 0:
        return None
    margin = SCALE_MARGIN if math.isfinite(SCALE_MARGIN) and SCALE_MARGIN > 0 else 0.2
    width = positive_number(header.get("NAXIS1"))
    height = positive_number(header.get("NAXIS2"))
    return {
        "arcsecPerPix": arcsec_per_pix,
        "lower": arcsec_per_pix * max(0.05, 1 - margin),
        "upper": arcsec_per_pix * (1 + margin),
        "focalMm": focal_mm,
        "pixelUm": (x_pix_um + y_pix_um) / 2,
        "binning": {"x": x_bin, "y": y_bin},
        "fovDeg": (
            {
                "width": width * x_scale / 3600,
                "height": height * y_scale / 3600,
                "diagonal": math.hypot(width * x_scale, height * y_scale) / 3600,
            }
            if width and height
            else None
        ),
    }


def upload_file(session: str, file_path: pathlib.Path) -> tuple[dict, dict, dict, dict]:
    header = read_fits_header(file_path)
    scale_hint = estimate_scale_hint(header)
    request = {
        "session": session,
        "publicly_visible": "n",
        "allow_modifications": "d",
        "allow_commercial_use": "d",
        "scale_units": "arcsecperpix",
        "scale_type": "ul",
        "scale_lower": scale_hint["lower"] if scale_hint else 3.2,
        "scale_upper": scale_hint["upper"] if scale_hint else 4.8,
        "center_ra": header.get("RA"),
        "center_dec": header.get("DEC"),
        "radius": SEARCH_RADIUS_DEG,
        "downsample_factor": 2,
        "tweak_order": 2,
    }
    request = {key: value for key, value in request.items() if value is not None}
    body, content_type = multipart_body(
        {"request-json": json.dumps(request)},
        file_path.name,
        file_path.read_bytes(),
    )
    response_body = request_bytes(
        f"{API_BASE}/upload",
        data=body,
        headers={"Content-Type": content_type},
    )
    try:
        uploaded = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Non-JSON upload response: {response_body[:400]!r}") from error
    if not isinstance(uploaded, dict) or uploaded.get("status") != "success":
        raise RuntimeError(f"Astrometry upload failed: {json.dumps(uploaded)}")
    return uploaded, request, header, scale_hint


def wait_for_job(submission_id: str, timeout_seconds: float = 15 * 60) -> tuple[str, dict]:
    started = time.monotonic()
    last_submission: dict | None = None
    while time.monotonic() - started < timeout_seconds:
        last_submission = json_request(f"{API_BASE}/submissions/{submission_id}")
        jobs = [job for job in last_submission.get("jobs", []) if job is not None]
        if jobs:
            return str(jobs[0]), last_submission
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for an astrometry job. Last submission: {json.dumps(last_submission)}")


def wait_for_solve(job_id: str, timeout_seconds: float = 20 * 60) -> dict:
    started = time.monotonic()
    last_status: dict | None = None
    while time.monotonic() - started < timeout_seconds:
        last_status = json_request(f"{API_BASE}/jobs/{job_id}")
        if last_status.get("status") in {"success", "failure"}:
            return last_status
        time.sleep(10)
    raise TimeoutError(f"Timed out waiting for solve. Last job status: {json.dumps(last_status)}")


def fetch_results(job_id: str) -> dict:
    results = {}
    for name, suffix in (("calibration", "calibration/"), ("info", "info/"), ("annotations", "annotations/")):
        try:
            results[name] = json_request(f"{API_BASE}/jobs/{job_id}/{suffix}")
        except Exception as error:  # Preserve partial Astrometry results.
            results[name] = {"error": str(error)}
    return results


def angular_separation_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    d2r = math.pi / 180
    a1, d1, a2, d2 = ra1 * d2r, dec1 * d2r, ra2 * d2r, dec2 * d2r
    cosine = math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2)
    return math.acos(max(-1.0, min(1.0, cosine))) / d2r


def parse_args(argv: list[str]) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, str]:
    fits_path = pathlib.Path(argv[0]) if len(argv) > 0 else DEFAULT_FILE
    output_path = pathlib.Path(argv[1]) if len(argv) > 1 else SCRIPT_DIR / "downloads" / "98943_Torifune_astrometry_result.json"
    wcs_path = pathlib.Path(argv[2]) if len(argv) > 2 else output_path.with_name(output_path.stem + "_wcs.fits")
    resume_id = argv[3] if len(argv) > 3 else os.environ.get("ASTROMETRY_NET_SUBID", "")
    return fits_path, output_path, wcs_path, resume_id


def main(argv: list[str] | None = None) -> int:
    fits_path, output_path, wcs_path, resume_id = parse_args(argv or sys.argv[1:])
    if not fits_path.exists():
        raise FileNotFoundError(f"FITS file not found: {fits_path}")
    header = read_fits_header(fits_path)
    scale_hint = estimate_scale_hint(header)
    upload = None
    request = {"resumed": True}
    submission_id = resume_id.strip()
    if submission_id:
        print(f"Resuming Astrometry submission id: {submission_id}")
    else:
        print(f"Uploading {fits_path}")
        session = login(read_api_key())
        print("Astrometry login succeeded")
        upload, request, header, scale_hint = upload_file(session, fits_path)
        submission_id = str(upload["subid"])
        print(f"Submission id: {submission_id}")
        if scale_hint:
            fov = scale_hint.get("fovDeg") or {}
            print(
                f"Scale hint: {scale_hint['arcsecPerPix']:.3f} arcsec/pix; "
                f"FOV {fov.get('width', '?') if isinstance(fov, dict) else '?'} x "
                f"{fov.get('height', '?') if isinstance(fov, dict) else '?'} deg"
            )
        submission_path = output_path.with_name(output_path.stem + "_submission.json")
        submission_path.parent.mkdir(parents=True, exist_ok=True)
        submission_path.write_text(
            json.dumps(
                {
                    "fitsPath": str(fits_path),
                    "subid": submission_id,
                    "upload": upload,
                    "scaleHint": scale_hint,
                    "uploadRequest": {**request, "session": "[redacted]"},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"Wrote submission checkpoint: {submission_path}")

    job_id, submission = wait_for_job(submission_id)
    print(f"Job id: {job_id}")
    status = wait_for_solve(job_id)
    print(f"Job status: {status.get('status')}")
    results = fetch_results(job_id) if status.get("status") == "success" else {}
    wcs_download = None
    if status.get("status") == "success":
        try:
            wcs_data = request_bytes(f"{API_BASE}/jobs/{job_id}/wcs_file/")
            wcs_path.parent.mkdir(parents=True, exist_ok=True)
            wcs_path.write_bytes(wcs_data)
            wcs_download = {"filePath": str(wcs_path), "bytes": len(wcs_data)}
        except Exception as error:
            wcs_download = {"error": str(error)}

    calibration = results.get("calibration") or {}
    header_ra, header_dec = header.get("RA"), header.get("DEC")
    solved_ra, solved_dec = (
        (calibration.get("ra"), calibration.get("dec"))
        if isinstance(calibration, dict)
        else (None, None)
    )
    offset = None
    try:
        if all(value is not None for value in (header_ra, header_dec, solved_ra, solved_dec)):
            degrees = angular_separation_deg(float(header_ra), float(header_dec), float(solved_ra), float(solved_dec))
            offset = {"degrees": degrees, "arcmin": degrees * 60, "arcsec": degrees * 3600}
    except (TypeError, ValueError):
        offset = None
    summary = {
        "fitsPath": str(fits_path),
        "subid": submission_id,
        "jobid": job_id,
        "status": status,
        "submission": submission,
        "uploadRequest": {**request, "session": "[redacted]" if request.get("session") else None},
        "scaleHint": scale_hint,
        "header": {key: header.get(key) for key in ("OBJECT", "DATE-OBS", "RA", "DEC", "EXPOSURE", "FILTER", "GAIN")},
        "calibration": calibration,
        "offsetFromHeader": offset,
        "results": results,
        "urls": {
            "status": f"https://nova.astrometry.net/status/{submission_id}",
            "job": f"https://nova.astrometry.net/user_images/{results.get('info', {}).get('user_image', '') if isinstance(results.get('info'), dict) else ''}",
            "wcs": f"https://nova.astrometry.net/wcs_file/{job_id}",
            "annotated": f"https://nova.astrometry.net/annotated_display/{job_id}",
        },
        "files": {"wcs": wcs_download},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {output_path}")
    if wcs_download and wcs_download.get("filePath"):
        print(f"Wrote WCS {wcs_download['filePath']}")
    if offset:
        print(f"Solved center RA={solved_ra} Dec={solved_dec}; header offset={offset['arcmin']:.2f} arcmin")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
