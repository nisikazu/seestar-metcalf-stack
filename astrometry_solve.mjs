#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const defaultFile = path.join(
  scriptDir,
  "downloads",
  "98943 Torifune_sub",
  "Light_98943 Torifune_20.0s_IRCUT_failed_20260708-210152.fit",
);
const fitsPath = process.argv[2] || defaultFile;
const outputPath = process.argv[3] || path.join(scriptDir, "downloads", "98943_Torifune_astrometry_result.json");
const wcsOutputPath =
  process.argv[4] ||
  outputPath.replace(/\.json$/i, "_wcs.fits");
const resumeSubid = process.argv[5] || process.env.ASTROMETRY_NET_SUBID || "";
const apiBase = process.env.ASTROMETRY_NET_API_BASE || "https://nova.astrometry.net/api";
const fetchRetries = Number(process.env.ASTROMETRY_NET_FETCH_RETRIES || 8);
const scaleMargin = Number(process.env.ASTROMETRY_NET_SCALE_MARGIN || 0.2);
const searchRadiusDeg = Number(process.env.ASTROMETRY_NET_SEARCH_RADIUS_DEG || 2.0);

function retryDelayMs(attempt) {
  return Math.min(60000, 2000 * 2 ** Math.max(0, attempt - 1));
}

function readApiKey() {
  const envKey = process.env.ASTROMETRY_NET_API_KEY?.trim();
  if (envKey) return envKey;
  const keyPath = path.join(scriptDir, ".astrometry_api_key");
  if (fs.existsSync(keyPath)) {
    const fileKey = fs.readFileSync(keyPath, "utf8").trim();
    if (fileKey) return fileKey;
  }
  throw new Error(
    "Astrometry.net API key was not found. Set ASTROMETRY_NET_API_KEY or put it in .astrometry_api_key.",
  );
}

async function postJsonForm(url, payload) {
  const body = new URLSearchParams();
  body.set("request-json", JSON.stringify(payload));
  const response = await fetchWithRetry(url, { method: "POST", body });
  const text = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`Non-JSON response from ${url}: ${text.slice(0, 400)}`);
  }
  if (!response.ok) throw new Error(`HTTP ${response.status} from ${url}: ${text}`);
  return parsed;
}

async function login(apiKey) {
  const result = await postJsonForm(`${apiBase}/login`, { apikey: apiKey });
  if (result.status !== "success" || !result.session) {
    throw new Error(`Astrometry login failed: ${JSON.stringify(result)}`);
  }
  return result.session;
}

async function uploadFile(session, filePath) {
  const header = readFitsHeader(filePath);
  const centerRa = Number(header.RA);
  const centerDec = Number(header.DEC);
  const scaleHint = estimateScaleHint(header);
  const request = {
    session,
    publicly_visible: "n",
    allow_modifications: "d",
    allow_commercial_use: "d",
    scale_units: "arcsecperpix",
    scale_type: "ul",
    scale_lower: scaleHint?.lower ?? 3.2,
    scale_upper: scaleHint?.upper ?? 4.8,
    center_ra: Number.isFinite(centerRa) ? centerRa : undefined,
    center_dec: Number.isFinite(centerDec) ? centerDec : undefined,
    radius: searchRadiusDeg,
    downsample_factor: 2,
    tweak_order: 2,
  };
  for (const [key, value] of Object.entries(request)) {
    if (typeof value === "undefined") delete request[key];
  }

  const form = new FormData();
  form.set("request-json", JSON.stringify(request));
  const bytes = await fs.promises.readFile(filePath);
  form.set("file", new Blob([bytes], { type: "application/octet-stream" }), path.basename(filePath));

  const response = await fetchWithRetry(`${apiBase}/upload`, { method: "POST", body: form });
  const text = await response.text();
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new Error(`Non-JSON upload response: ${text.slice(0, 400)}`);
  }
  if (!response.ok || parsed.status !== "success") {
    throw new Error(`Astrometry upload failed: ${JSON.stringify(parsed)}`);
  }
  return { upload: parsed, request, header, scaleHint };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getJson(url) {
  const response = await fetchWithRetry(url);
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} from ${url}: ${text.slice(0, 400)}`);
  }
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`Non-JSON response from ${url}: ${text.slice(0, 400)}`);
  }
}

async function fetchWithRetry(url, options = {}, retries = fetchRetries) {
  let lastError = null;
  for (let attempt = 1; attempt <= retries; attempt += 1) {
    try {
      const response = await fetch(url, options);
      if (response.status < 500 && response.status !== 429) return response;
      lastError = new Error(`HTTP ${response.status} from ${url}`);
    } catch (err) {
      lastError = err;
    }
    if (attempt < retries) {
      const delay = retryDelayMs(attempt);
      console.warn(`Astrometry request failed (${attempt}/${retries}); retrying in ${Math.round(delay / 1000)}s: ${lastError.message}`);
      await sleep(delay);
    }
  }
  throw lastError;
}

async function waitForJob(subid, timeoutMs = 15 * 60 * 1000) {
  const startedAt = Date.now();
  let lastSubmission = null;
  while (Date.now() - startedAt < timeoutMs) {
    lastSubmission = await getJson(`${apiBase}/submissions/${subid}`);
    const jobs = (lastSubmission.jobs || []).filter((job) => job !== null);
    if (jobs.length > 0) return { jobid: jobs[0], submission: lastSubmission };
    await sleep(10000);
  }
  throw new Error(`Timed out waiting for an astrometry job. Last submission: ${JSON.stringify(lastSubmission)}`);
}

async function waitForSolve(jobid, timeoutMs = 20 * 60 * 1000) {
  const startedAt = Date.now();
  let lastStatus = null;
  while (Date.now() - startedAt < timeoutMs) {
    lastStatus = await getJson(`${apiBase}/jobs/${jobid}`);
    if (lastStatus.status === "success") return lastStatus;
    if (lastStatus.status === "failure") return lastStatus;
    await sleep(10000);
  }
  throw new Error(`Timed out waiting for solve. Last job status: ${JSON.stringify(lastStatus)}`);
}

async function fetchResults(jobid) {
  const [calibration, info, annotations] = await Promise.all([
    getJson(`${apiBase}/jobs/${jobid}/calibration/`).catch((err) => ({ error: err.message })),
    getJson(`${apiBase}/jobs/${jobid}/info/`).catch((err) => ({ error: err.message })),
    getJson(`${apiBase}/jobs/${jobid}/annotations/`).catch((err) => ({ error: err.message })),
  ]);
  return { calibration, info, annotations };
}

async function downloadBinary(url, filePath) {
  const response = await fetchWithRetry(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} while downloading ${url}`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  await fs.promises.mkdir(path.dirname(filePath), { recursive: true });
  await fs.promises.writeFile(filePath, bytes);
  return { filePath, bytes: bytes.length };
}

function parseFitsValue(raw) {
  const value = raw.split("/")[0].trim();
  if (value.startsWith("'")) return value.slice(1, value.indexOf("'", 1)).trim();
  if (value === "T") return true;
  if (value === "F") return false;
  const num = Number(value.replace("D", "E"));
  return Number.isFinite(num) ? num : value;
}

function readFitsHeader(filePath) {
  const fd = fs.openSync(filePath, "r");
  try {
    const header = {};
    const block = Buffer.alloc(2880);
    while (fs.readSync(fd, block, 0, block.length, null) > 0) {
      for (let offset = 0; offset < block.length; offset += 80) {
        const card = block.toString("ascii", offset, offset + 80);
        if (card.startsWith("END")) return header;
        if (card[8] === "=") header[card.slice(0, 8).trim()] = parseFitsValue(card.slice(10));
      }
    }
    return header;
  } finally {
    fs.closeSync(fd);
  }
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

function estimateScaleHint(header) {
  const focalMm = finiteNumber(header.FOCALLEN);
  const xPixUm = finiteNumber(header.XPIXSZ);
  const yPixUm = finiteNumber(header.YPIXSZ);
  if (!focalMm || !xPixUm || !yPixUm) return null;
  const xBin = finiteNumber(header.XBINNING) || finiteNumber(header.CCDXBIN) || 1;
  const yBin = finiteNumber(header.YBINNING) || finiteNumber(header.CCDYBIN) || 1;
  const xScale = (206.265 * xPixUm * xBin) / focalMm;
  const yScale = (206.265 * yPixUm * yBin) / focalMm;
  const arcsecPerPix = (xScale + yScale) / 2;
  if (!Number.isFinite(arcsecPerPix) || arcsecPerPix <= 0) return null;
  const margin = Number.isFinite(scaleMargin) && scaleMargin > 0 ? scaleMargin : 0.2;
  const lower = arcsecPerPix * Math.max(0.05, 1 - margin);
  const upper = arcsecPerPix * (1 + margin);
  const width = finiteNumber(header.NAXIS1);
  const height = finiteNumber(header.NAXIS2);
  return {
    arcsecPerPix,
    lower,
    upper,
    focalMm,
    pixelUm: (xPixUm + yPixUm) / 2,
    binning: { x: xBin, y: yBin },
    fovDeg:
      width && height
        ? {
            width: (width * xScale) / 3600,
            height: (height * yScale) / 3600,
            diagonal: Math.hypot(width * xScale, height * yScale) / 3600,
          }
        : null,
  };
}

function angularSeparationDeg(ra1, dec1, ra2, dec2) {
  const d2r = Math.PI / 180;
  const a1 = ra1 * d2r;
  const d1 = dec1 * d2r;
  const a2 = ra2 * d2r;
  const d2 = dec2 * d2r;
  const cosSep = Math.sin(d1) * Math.sin(d2) + Math.cos(d1) * Math.cos(d2) * Math.cos(a1 - a2);
  return Math.acos(Math.max(-1, Math.min(1, cosSep))) / d2r;
}

async function main() {
  if (!fs.existsSync(fitsPath)) throw new Error(`FITS file not found: ${fitsPath}`);
  const header = readFitsHeader(fitsPath);
  const scaleHint = estimateScaleHint(header);
  let upload = null;
  let request = { resumed: true };
  let subid = resumeSubid.trim();
  if (subid) {
    console.log(`Resuming Astrometry submission id: ${subid}`);
  } else {
    const apiKey = readApiKey();
    console.log(`Uploading ${fitsPath}`);
    const session = await login(apiKey);
    console.log("Astrometry login succeeded");
    const uploaded = await uploadFile(session, fitsPath);
    upload = uploaded.upload;
    request = uploaded.request;
    subid = String(upload.subid);
    console.log(`Submission id: ${subid}`);
    if (scaleHint) {
      console.log(
        `Scale hint: ${scaleHint.arcsecPerPix.toFixed(3)} arcsec/pix; FOV ${scaleHint.fovDeg?.width?.toFixed(2) ?? "?"} x ${scaleHint.fovDeg?.height?.toFixed(2) ?? "?"} deg`,
      );
    }
    const submissionPath = outputPath.replace(/\.json$/i, "_submission.json");
    await fs.promises.mkdir(path.dirname(submissionPath), { recursive: true });
    await fs.promises.writeFile(
      submissionPath,
      `${JSON.stringify({ fitsPath, subid, upload, scaleHint, uploadRequest: { ...request, session: "[redacted]" } }, null, 2)}\n`,
    );
    console.log(`Wrote submission checkpoint: ${submissionPath}`);
  }
  const { jobid, submission } = await waitForJob(subid);
  console.log(`Job id: ${jobid}`);
  const status = await waitForSolve(jobid);
  console.log(`Job status: ${status.status}`);
  const results = status.status === "success" ? await fetchResults(jobid) : {};
  const wcsDownload =
    status.status === "success"
      ? await downloadBinary(`${apiBase}/jobs/${jobid}/wcs_file/`, wcsOutputPath).catch((err) => ({
          error: err.message,
        }))
      : null;
  const calibration = results.calibration || {};
  const headerRa = Number(header.RA);
  const headerDec = Number(header.DEC);
  const solvedRa = Number(calibration.ra);
  const solvedDec = Number(calibration.dec);
  const offsetDeg =
    Number.isFinite(headerRa) && Number.isFinite(headerDec) && Number.isFinite(solvedRa) && Number.isFinite(solvedDec)
      ? angularSeparationDeg(headerRa, headerDec, solvedRa, solvedDec)
      : null;
  const summary = {
    fitsPath,
    subid,
    jobid,
    status,
    submission,
    uploadRequest: { ...request, session: request.session ? "[redacted]" : undefined },
    scaleHint,
    header: {
      OBJECT: header.OBJECT,
      DATE_OBS: header["DATE-OBS"],
      RA: header.RA,
      DEC: header.DEC,
      EXPOSURE: header.EXPOSURE,
      FILTER: header.FILTER,
      GAIN: header.GAIN,
    },
    calibration,
    offsetFromHeader: offsetDeg == null ? null : {
      degrees: offsetDeg,
      arcmin: offsetDeg * 60,
      arcsec: offsetDeg * 3600,
    },
    results,
    urls: {
      status: `https://nova.astrometry.net/status/${upload.subid}`,
      job: `https://nova.astrometry.net/user_images/${results.info?.user_image || ""}`,
      wcs: `https://nova.astrometry.net/wcs_file/${jobid}`,
      annotated: `https://nova.astrometry.net/annotated_display/${jobid}`,
    },
    files: {
      wcs: wcsDownload,
    },
  };
  await fs.promises.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.promises.writeFile(outputPath, `${JSON.stringify(summary, null, 2)}\n`);
  console.log(`Wrote ${outputPath}`);
  if (wcsDownload?.filePath) console.log(`Wrote WCS ${wcsDownload.filePath}`);
  if (summary.offsetFromHeader) {
    console.log(
      `Solved center RA=${solvedRa} Dec=${solvedDec}; header offset=${summary.offsetFromHeader.arcmin.toFixed(2)} arcmin`,
    );
  }
}

main().catch((err) => {
  console.error(err.stack || String(err));
  process.exitCode = 1;
});
