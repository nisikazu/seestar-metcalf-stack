# Changelog

This document records changes that affect users. See [DEVELOPMENT.md](DEVELOPMENT.md) for engineering decisions, known limitations, and handover notes.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## v0.7.2 - 2026-08-17

### Changed

- Removed benchmarks, unit tests, GitHub Actions, release automation, and legacy experiment notes from end-user Release ZIPs, leaving only runtime, setup, rebuilding, license, and user/handover documentation.
- Collected plate-solve benchmark tools under `developer-tools/plate-solve-benchmark/` in the source repository and moved their default output under that directory's `results/` folder.
- Added forbidden development-path validation so future package builds fail if source-only assets leak into either staging directories or ZIP archives.

## v0.7.1 - 2026-08-17

### Added

- Read SharpCap `*.CameraSettings.txt` master-dark, master-flat, hot-pixel, and cold-pixel settings and apply them with Siril before debayering.
- Treat nonzero `Hot Pixel Sensitivity` as enabling Siril hot-pixel correction at the default sigma of 3 without translating SharpCap's incompatible numeric scale.
- Add preprocessing, master-file override, per-correction enable/disable, cosmetic sigma, and `--plate-solver auto|siril|astrometry` options.
- Plate-solve locally with Siril first, cache `*_siril_wcs.fits`, and use Astrometry.net only as fallback.

### Changed

- Complete SharpCap StackLog transforms now replace only background-star registration; Siril remains responsible for calibration and debayering.
- The Astrometry.net API key is optional for normal Seestar processing.

### Fixed

- Detect `*.CameraSettings.txt` in ordinary SharpCap FITS capture directories without `stacklog.csv`, and apply the recorded preprocessing to both the reference frame and all stacked frames.
- Avoid passing zero as the disabled side of Siril hot/cold-pixel correction, which could correct millions of ordinary pixels.
- Exclude cached `*_siril_wcs.fits` and related plate-solve artifacts when scanning source subframes again.

## v0.7.0 - 2026-08-14

### Added

- Detect SharpCap 4.1.10745+ Live Stack sessions through `stacklog.csv` and join raw frames through `Raw frame file`.
- Use only SharpCap-successful rows by default and retain timestamp, detected-star count, FWHM, X/Y offsets, and rotation.
- Skip Siril automatically when every selected SharpCap frame has complete background-star alignment data; Python applies the recorded transforms.
- Add `--bayer-pattern RGGB|BGGR|GRBG|GBRG` for SharpCap RAW PNG/TIFF frames.
- Find `stacklog.csv` either inside the raw-frame directory or one level above it, and prefer relocated same-name images under the dropped directory over stale absolute CSV paths.
- Accept `stacklog.csv` itself as the source path and use its parent directory as the copied session root.
- Require a target or ephemeris CSV and `--pixel-scale-arcsec` for SharpCap PNG/TIFF input. The observing site remains optional and falls back to the geocenter.

### Security

- Require a known SharpCap version of 4.1.10745 or later to avoid early 4.1 StackLog timestamp bugs.
- Remove SharpCap `OBSLONG` and `OBSLAT` cards from the Astrometry.net upload copy.
- Stop on ambiguous duplicate raw-frame filenames instead of silently selecting the wrong file. The original absolute CSV path is considered only after relocated local candidates.

## v0.6.1 - 2026-08-08

### Fixed

- Bundle a CA certificate set in release packages so HTTPS access to JPL Horizons and Astrometry.net works on Windows systems whose packaged Python runtime cannot discover a usable certificate store.

## v0.6.0 - 2026-08-05

### Added

- Accept both `.fit` and `.fits` as standard input files, including FITS exported by SharpCap.
- Add `--site-longitude` and `--site-latitude` for east-positive longitude and north-positive latitude overrides. If no site is available in the FITS or command line, Horizons now falls back to the geocenter.
- Add `--pixel-scale-arcsec` for files that do not record their image scale.

### Fixed

- Validate Astrometry.net WCS downloads before saving them. HTML or other non-FITS responses are rejected and the valid JSON calibration is used instead.
- Ignore backup files such as `.fits.invalid` when the default `*.fit*` pattern is used.

## v0.5.5 - 2026-08-04

### Fixed

- When background-star registration fails, every debayered frame is now measured sequentially with Siril. `registration_diagnostics.csv` retains detected-star count, FWHM, and roundness so a better `--reference-frame-file` can be selected even when the original reference cannot register any frame.

## v0.5.4 - 2026-08-02

### Improved

- Mean stacks now accumulate only pixels backed by real registered/shifted image data and normalize by an integer per-pixel contribution count. This prevents zero padding from darkening low-overlap borders.
- Sub-pixel motion is interpolated only when all four source neighbors are valid; image-edge zero interpolation and extrapolation are avoided. Use `--padding-policy legacy` to compare with the previous behavior.
- The median/rankfit zero-sample behavior is explicit through `--zero-sample-policy exclude`; the discouraged legacy result remains available with `include`.
- Added `*_registration_diagnostics.csv` with every frame's FWHM, weighted FWHM, roundness, detected stars, matched pairs, inlier fraction, X/Y translation, rotation, scale, acceptance, and rejection reason.
- An early `registration_diagnostics.csv` remains in the work directory even when an unsuitable reference prevents stacking.

### Fixed

- Corrected the former labeling of Siril `.seq` detected-star counts as matched pairs. Initial and fitted correspondence counts are now parsed separately from the Siril log.
- Stack failures no longer print duplicate Python tracebacks to the console. Users receive a concise error with the cause, reference, diagnostics path, and recovery action; the detailed traceback remains in the run log.

## v0.5.3 - 2026-07-31

### Fixed

- Fixed Windows launches of Siril through `.cmd` or `.bat` where `cmd.exe` split installation, working, or script paths containing spaces.
- Siril-bundled packages now prefer the packaged `tools/.../siril-cli.exe` and start it directly, avoiding batch-file parsing for paths with spaces.
- Documented Windows output failures caused by overly long paths in the troubleshooting guide.

## v0.5.2 - 2026-07-31

### Changed

- `--reference-frame-file` now selects a specific reference FITS by filename. It replaces `--reference-frame-index`, whose filtered index cannot be known reliably before a run.
- The run stops with the detected and required star counts when the selected reference frame does not meet `--registration-minpairs`.
- Frames other than the reference that cannot be registered because of cloud, an obstruction, or twilight are skipped instead of failing the whole run. Inspect `used`, `reason`, and `star_pairs` in `*_shifts.csv`.
- Completion now reports `Stacked used/total frames; skipped count`.

### Documentation

- Added [Changes and troubleshooting](TROUBLESHOOTING-en.md), covering reference-frame selection, registration failures, paths with spaces, and recovery for Siril, Astrometry.net, and Horizons issues.

## v0.5.1 - 2026-07-25

### Added

- `--saturation-warning enable` overlays a warning color only on preview PNG pixels that exceeded the selected saturation threshold in any subframe.
- `--saturation-threshold-percent` and `--saturation-color` configure the threshold and color.

### Notes

- A warning is generated when any RGB channel exceeds the threshold. Linear FITS pixel values are not modified.

## v0.5.0 - 2026-07-21

### Added

- Added macOS Python-source execution, a shell launcher, and a Finder drag-and-drop application.
- Added Windows/macOS GitHub Actions unit tests.

## v0.4.0 - 2026-07-14

### Changed

- Moved Astrometry.net processing into Python and removed the Node.js dependency.
- Added a Windows EXE and Siril-bundled/Siril-free release ZIPs.
