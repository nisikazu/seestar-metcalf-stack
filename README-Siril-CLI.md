# Siril CLI workflow notes

This workspace uses a local portable Siril install:

- Wrapper: `C:\local\codex\seestar\siril-cli.cmd`
- Siril CLI: `C:\local\codex\seestar\tools\siril-1.4.1\siril\bin\siril-cli.exe`
- Verified version: Siril 1.4.1

The local Siril bundle is ignored by Git via `tools/`.

## Smoke test

```cmd
C:\local\codex\seestar\siril-cli.cmd --version
```

## Comet subframe test

The helper below copies a small subset of Seestar comet subframes into a fresh
`siril_work` directory, debayers them, registers on background stars, stacks,
and exports a PNG preview:

```cmd
C:\local\codex\seestar\run-siril-comet-test.cmd
```

Default source:

```text
C:\local\codex\seestar\downloads\C2025 A6 (Lemmon)_sub
```

Typical outputs:

```text
C:\local\codex\seestar\siril_work\C2025_A6_Lemmon_12_color_star_stack-YYYYMMDD-HHMMSS\lemmon_color_star_stack.fit
C:\local\codex\seestar\siril_work\C2025_A6_Lemmon_12_color_star_stack-YYYYMMDD-HHMMSS\lemmon_color_star_stack_preview.png
```

## Important finding

Seestar S30 subframes are read by Siril as one-layer CFA FITS files. Use
`convert ... -debayer` before normal color preview or stacking. Without
debayering, `autostretch` can produce a misleading preview.

The test script uses star registration:

```ssf
convert lemmon -debayer
register lemmon -prefix=r_ -transf=shift
stack r_lemmon rej 3 3 -norm=addscale -out=lemmon_color_star_stack
load lemmon_color_star_stack
autostretch
savepng lemmon_color_star_stack_preview
```

This is not yet moving-target tracking. It is the baseline star-aligned stack
needed before implementing asteroid/comet-rate offset stacking.

## Moving-target stack prototype

The moving-target prototype uses Siril for debayering and background-star
registration, then applies additional per-frame shifts from an ephemeris CSV:

End-to-end wrapper with practical defaults:

```cmd
C:\local\codex\seestar\run-moving-target-pipeline.cmd ^
  --source-dir "C:\local\codex\seestar\downloads\C2025 R2 (SWAN)_sub"
```

With only `--source-dir`, the pipeline:

- splits subframes into sessions using a 60-minute gap;
- selects the latest session;
- creates one run work directory under `siril_work\`;
- stores generated Horizons CSV, Astrometry upload FITS/JSON/WCS, Siril
  conversion files, final FITS/PNG, shift CSV, and summaries in that work
  directory;
- uses the FITS observing site for topocentric Horizons coordinates;
- plate-solves the first selected frame unless `--astrometry-json` or
  `--wcs-fits` is supplied;
- debayers, star-registers, and stacks the same registered frames twice:
  once with moving-target compensation and once with no additional offset for
  a background-star reference stack;
- writes the main stack as uint16 FITS by preserving the linear ADU scale and
  rounding the averaged pixel values.

For Astrometry.net, the solver reads FITS equipment hints when available. S30
subframes include `FOCALLEN=150.0`, `XPIXSZ=2.9`, `YPIXSZ=2.9`, and the image
size, which gives about `3.99 arcsec/pixel` and a `1.20 x 2.13 deg` field. The
upload request uses this as a constrained `scale_lower` / `scale_upper` hint.
Set `ASTROMETRY_NET_SCALE_MARGIN` to change the default +/-20% scale margin, or
`ASTROMETRY_NET_SEARCH_RADIUS_DEG` to change the default 2-degree RA/Dec search
radius.

The default work directory name is built from the FITS `OBJECT` value and the
run time, for example:

```text
C:\local\codex\seestar\siril_work\C2025_R2_SWAN_moving-20260711-100132\
```

Pass `--work-name` to override the object-derived stem; the timestamp is still
added by the stacker.

Reused external inputs passed with `--ephemeris-csv`, `--astrometry-json`, or
`--wcs-fits` are copied into the run directory first, so the run directory is
self-contained apart from the original source subframes.

Large intermediate FITS images generated for Siril registration are deleted by
default after a successful stack. Logs, Siril sequence files, summaries, CSVs,
final FITS files, and preview PNGs are kept. Pass `--no-cleanup` to keep the
intermediate image FITS files for debugging.

The default output filenames are built from the target, exposure time, filter,
the first and last used frame timestamps, and the number of stacked frames, for
example:

```text
C2025_R2_SWAN_20.0s_IRCUT_20251103T095234Z-20251103T105620Z_89frames_metcalf_stack.fit
C2025_R2_SWAN_20.0s_IRCUT_20251103T095234Z-20251103T105620Z_89frames_metcalf_preview.png
C2025_R2_SWAN_20.0s_IRCUT_20251103T095234Z-20251103T105620Z_89frames_star_stack.fit
C2025_R2_SWAN_20.0s_IRCUT_20251103T095234Z-20251103T105620Z_89frames_star_preview.png
```

Pass `--output-prefix` to override this filename stem.

Photometry note: the default `--output-bitpix uint16 --uint16-scale none`
preserves the linear ADU scale. Siril registration may write float FITS
normalized to `0..1`; when the source subframes are unsigned 16-bit FITS, the
stacker restores those registered frames to `0..65535` ADU before the
moving-target shift and average. The stack is computed in floating point and
the final science FITS is rounded to unsigned 16-bit ADU values with standard
FITS `BZERO=32768`, `BSCALE=1`. Do not use `--uint16-scale global` or
`--uint16-scale per-channel` for photometry; those are display-oriented
percentile stretches.

Privacy note: the default pipeline uses `--horizons-center fits-site`, so it
sends the FITS `SITELONG` and `SITELAT` observing-site coordinates to JPL
Horizons. This is intentional for accurate nearby-object work, but use
`--horizons-center geocenter` if site coordinates should not leave the machine.

To reuse an existing Astrometry.net solve and avoid re-uploading the first FITS:

```cmd
C:\local\codex\seestar\run-moving-target-pipeline.cmd ^
  --source-dir "C:\local\codex\seestar\downloads\C2025 R2 (SWAN)_sub" ^
  --astrometry-json "C:\local\codex\seestar\plate_solve\Light_C2025 R2 (SWAN)_20.0s_IRCUT_20251103-185257_20260711-074708_astrometry.json" ^
  --work-name C2025_R2_SWAN_moving
```

Legacy explicit-CSV wrapper:

```cmd
C:\local\codex\seestar\run-moving-target-pipeline.cmd ^
  --source-dir "C:\local\codex\seestar\downloads\98943 Torifune_sub" ^
  --ephemeris-csv "C:\local\codex\seestar\ephemeris\torifune_20260709_linear.csv" ^
  --work-name torifune_20260709_moving
```

Without `--wcs-fits`, the wrapper plate-solves the first selected subframe via
the Python `scripts\astrometry_solve.py` helper and downloads the WCS FITS. This uploads that first FITS
to Astrometry.net. To avoid an upload and reuse an existing WCS:

```cmd
C:\local\codex\seestar\run-moving-target-pipeline.cmd ^
  --source-dir "C:\local\codex\seestar\downloads\98943 Torifune_sub" ^
  --ephemeris-csv "C:\local\codex\seestar\ephemeris\torifune_20260709_linear.csv" ^
  --wcs-fits "C:\local\codex\seestar\downloads\98943_Torifune_20260709\wcs_16288301.fits" ^
  --count 20 ^
  --work-name torifune_20260709_moving
```

First solve the first subframe and keep the WCS file:

```cmd
python C:\local\codex\seestar\scripts\astrometry_solve.py ^
  "C:\local\codex\seestar\downloads\98943 Torifune_sub\Light_98943 Torifune_20.0s_IRCUT_20260709-195156.fit" ^
  "C:\local\codex\seestar\downloads\98943_Torifune_20260709\astrometry_195156.json" ^
  "C:\local\codex\seestar\downloads\98943_Torifune_20260709\wcs_16288301.fits"
```

Then run the moving-target stack:

```cmd
C:\local\codex\seestar\run-moving-target-stack.cmd ^
  --source-dir "C:\local\codex\seestar\downloads\98943 Torifune_sub" ^
  --ephemeris-csv "C:\local\codex\seestar\ephemeris\torifune_20260709_linear.csv" ^
  --wcs-fits "C:\local\codex\seestar\downloads\98943_Torifune_20260709\wcs_16288301.fits" ^
  --count 20 ^
  --work-name torifune_20260709_moving
```

Outputs are written under `siril_work/<work-name>-YYYYMMDD-HHMMSS/`. Current
default filenames use the explicit suffixes `_metcalf_stack.fit`,
`_metcalf_preview.png`, `_star_stack.fit`, `_star_preview.png`, `_shifts.csv`,
and `_summary.json`. The summary keeps `outputs.fits` as an alias for the
Metcalf stack for compatibility.

`moving_target_shifts.csv` records both layers of alignment:

- `star_tx_px`, `star_ty_px`, `star_rotation_deg`, `star_scale`: Siril's
  background-star similarity registration estimate for each frame.
- `extra_dx_px`, `extra_dy_px`: the additional target-motion compensation from
  the WCS-projected ephemeris position at each frame's `DATE-OBS`.
- Frames that Siril cannot register are kept in the CSV with `used=False` and
  are excluded from the final moving-target stack.

Orientation note: Siril's FITS-to-PNG export keeps the visual orientation
expected for Seestar subframes, so the moving-target PNG preview is not flipped
by default. FITS/WCS pixel coordinates are still 1-based FITS coordinates; when
overlaying coordinates on an external top-left-origin image, convert the display
y coordinate explicitly. If a comparison image requires it, pass
`--preview-flip-vertical`.

The CSV ephemeris format is:

```csv
time,ra_deg,dec_deg
2026-07-09T10:51:00Z,161.14075,8.56027777777778
```

`ra` and `dec` sexagesimal strings are also accepted in place of decimal
degrees. The bundled Torifune CSV is a test fixture linearly extrapolated from
two JPL Horizons points in the 2026-07-09 report; production use should replace
it with a fresh orbit/ephemeris calculation.

## Horizons ephemeris CSV

For production moving-target stacks, generate the CSV from JPL Horizons using
the FITS `DATE-OBS` timestamps:

```cmd
C:\local\codex\seestar\generate-horizons-ephemeris.cmd ^
  --source-dir "C:\local\codex\seestar\downloads\C2025 A6 (Lemmon)_sub" ^
  --output "C:\local\codex\seestar\ephemeris\C2025_A6_Lemmon_horizons_geocenter.csv"
```

The default center is geocentric and does not send the FITS observing-site
coordinates. For topocentric ephemerides, which are more appropriate for
nearby minor bodies, explicitly acknowledge the privacy tradeoff:

```cmd
C:\local\codex\seestar\generate-horizons-ephemeris.cmd ^
  --source-dir "C:\local\codex\seestar\downloads\C2025 A6 (Lemmon)_sub" ^
  --output "C:\local\codex\seestar\ephemeris\C2025_A6_Lemmon_horizons_topocentric.csv" ^
  --center fits-site ^
  --allow-site-upload
```

`--center fits-site` sends `SITELONG` and `SITELAT` from the FITS header to JPL
Horizons. The script writes a sidecar `.meta.json` recording the object,
Horizons command, time range, center mode, and row count.

If a subframe directory contains multiple nights, split by large time gaps:

```cmd
C:\local\codex\seestar\generate-horizons-ephemeris.cmd ^
  --source-dir "C:\local\codex\seestar\downloads\C2025 A6 (Lemmon)_sub" ^
  --output "C:\local\codex\seestar\ephemeris\C2025_A6_Lemmon_20251101_horizons.csv" ^
  --session-gap-min 60 ^
  --session-index 1
```

## Known warning

In the Codex sandbox, Siril may print:

```text
Failed to create directory 'C:\Users\nisik\AppData\Local\siril': Permission denied
```

The tested CLI workflow still completes. Running from a normal user console
should usually allow Siril to create its configuration directory.
