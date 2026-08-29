# Changelog

This document records changes that affect users. See [DEVELOPMENT.md](DEVELOPMENT.md) for engineering decisions, known limitations, and handover notes.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## v0.9.4 - 2026-08-29

### Added

- Add `seestar-fixed-stack.cmd` for background-star-aligned subframe stacking without a Horizons query or Metcalf motion. It writes `*_fixed_stack.fit` for variable-star and other fixed-target work.
- Fixed mode defaults to mean, float32, saturation warnings enabled, and quadratic background correction; existing CLI options override each setting.
- Add `CREATOR`, `SWVER`, `PLTSOLVR`, `TIMESYS`, `DATE-BEG`, `DATE-AVG`, `MJD-AVG`, `DATE-END`, `TELAPSE`, `TOTEXP`, and `NCOMBINE` to final FITS products, and identify Metcalf, star-aligned, comparison, and fixed-stack products in `HISTORY`.
- Preserve every SIP distortion order and `A/B/AP/BP` coefficient supplied by a Siril WCS instead of assuming a third-order limit.

### Fixed

- Quote reference-frame names containing spaces in Siril scripts so CFA preprocessing and local plate solving no longer fail with `file not found`. This issue had been latent since local Siril solving was introduced in v0.7.1.
- Download Astrometry.net WCS FITS files from the official `/wcs_file/{job_id}` site endpoint instead of an API JSON URL, so a successful solve now stores the complete WCS FITS as well as the JSON calibration.

## v0.9.3 - 2026-08-24

### Improved

- Use Siril `register -2pass` to obtain background-star registration matrices without writing registered FITS files on the normal Siril registration path.
- Compose each star-registration matrix with its Metcalf translation, then create the moving-target image with one bilinear resampling of the preprocessed source. The star-fixed image is likewise resampled only once from that source.
- On the 242-frame 220P/McNaught benchmark, registered FITS output fell from 242 files to zero and measured temporary storage fell 43.5%, from about 13.23 GiB to 7.48 GiB. Warm-cache end-to-end time improved 3.7%, from 182.81 to 176.11 seconds.
- Record source staging, Siril preprocessing, registration, stacking, output, total pipeline wall time, Python-process peak RSS, and registration-directory storage checkpoints in the summary JSON.

### Internal design

- Convert Siril's bottom-origin matrices into NumPy/FITS array coordinates and rebase the automatic `-2pass` reference onto the user-selected reference. Output-canvas shape and origin remain independent from registration coordinates for future expanded-canvas policies.
- Replacing the old Siril-default Lanczos-4 registration plus Python bilinear shift with one bilinear interpolation intentionally changes exact pixels. Aperture tests against the same input stars measured per-channel mean errors of `-0.22% to +0.05%` for the new path versus `+0.45% to +1.07%` for old Lanczos-4. The 242-frame valid footprint matched, and a six-pixel-radius moving-target aperture changed by `-0.28% to +0.37%` between complete old and new stacks.
- All 170 tests passed, including matrix coordinates, positive/negative integer/fractional shifts, mono/RGB, edges, valid/saturation masks, and independent canvas shape/origin.

## v0.9.1 - 2026-08-24

### Fixed

- Handle a current JPL Horizons API compatibility problem where comet searches such as `DES=220P;CAP;NOFRAG` can be rejected with a syntax error. Automatic resolution now tries the historical `CAP`/`NOFRAG` form first, then falls back to an unqualified designation such as `DES=220P;` when Horizons rejects the modifiers. The same fallback is available for SBDB-derived candidates.

## v0.9.0 - 2026-08-24

### Improved

- Replace Metcalf pure translation with slice-based bilinear processing and avoid unnecessary full-frame temporary arrays in background application, zero-shift star accumulation, and mean accumulation.
- Add `--stack-workers auto|1|2|4`. The default `auto` conservatively estimates fixed and per-worker memory from available RAM and independent source/canvas dimensions, then selects up to four workers. An explicit count overrides the initial choice.
- If a worker allocation fails, stop all workers, discard the complete uncommitted batch, and retry that same batch at 4 -> 2 -> 1 workers. Only fully successful batches reach the deterministic main-thread accumulator.
- Read each registered FITS only once for background fit, application, and stacking. Remove source copies, converted images, staged calibration files, preprocessed images, and accepted registered images at the earliest safe stage.
- Print and record FITS read, background fit/application, star accumulation, Metcalf shift/accumulation, total stack timing, RAM estimate, selected worker count, and any fallback events.
- Two 242-frame production runs selected four workers and completed stacking in 84.510 and 73.418 seconds. The same-scope quadratic end-to-end benchmark averaged 181.470 seconds versus 736.298 seconds before v0.9, a 4.06x speedup. The Metcalf, star-aligned, and comparison float32 FITS outputs from both long runs were byte-identical by SHA-256.

### Internal design

- Separate registration coordinates from the output canvas through `StackCanvas`; the current reference-footprint output remains unchanged while a future contribution-count/percentage expanded canvas can reuse the same resampler.
- Add failure-injection coverage for worker shutdown, local-result disposal, untouched global accumulators, same-batch retry, one-worker exhaustion, and progressive-cleanup failures.

## v0.8.2 - 2026-08-23

### Added

- Add `--preview-sun-pa-left`, which places the JPL Horizons solar direction at left in a moving-target preview. This normally puts an anti-solar dust tail to the right. Moving-target FITS files now record the extension headers `SUN_PA` (solar position angle), `ASUN_PA` (anti-solar direction), `SUNRA`, `SUNDEC`, `SUNCENTR`, and `SUNSRC`.
- Add `--preview-at UL|UR|LL|LR` and `--annotate-size` to draw N/E orientation sticks and a Sun-direction arrow into display previews. `--preview-at UL` is the default. Alongside the annotated preview, the stacker writes a compact transparent `*_annotation_overlay.png` for free placement in a figure; use `--preview-at none` to omit annotations.

## v0.8.1 - 2026-08-22

### Fixed

- Correct the WCS array-Y convention and Pillow rotation used by `--preview-north-up`. The previous PNG output was not aligned with celestial north for an oblique WCS. Science FITS files, WCS, and ordinary previews are unaffected.

## v0.8.0 - 2026-08-21

### Added

- Add `--preview-north-up` to write moving-target, star-fixed, and comparison display PNGs rotated with the plate-solved WCS so celestial north is up. Science FITS files and ordinary previews remain unchanged.
- Add `--background-normalization plane|quadratic` to fit and subtract an RGB first-order plane or second-order surface from real pixels in every registered subframe.
- Add `--preview-stretch sigma`, `--preview-sigma-low`, and `--preview-sigma-high`.

### Changed

- Make `quadratic` the default background correction: it fits a second-order surface from sigma-clipped medians of a 50x50 tile grid. Use `--background-normalization none` or `offset` for very large comets or DSOs that cannot be protected by the surface model.
- Subtract each frame's fitted background to near zero during signed arithmetic, then add only a constant range-safeguard offset after stacking.
- Make the preview default a linear `-1 sigma` to `+3 sigma` stretch using the simple mean and standard deviation in each RGB channel. Bright stars intentionally remain in the estimate so background noise is not over-emphasized. Use `--preview-stretch percentile` for the previous method.

## v0.7.3 - 2026-08-18

### Improved

- When Siril's VizieR catalogue request returns HTTP 503, retry the same scale up to three times with 2- and 4-second waits before changing scale.
- Retry Astrometry.net, JPL Horizons, and JPL SBDB communication failures with backoff, and report the service, attempt count, and final cause when retries are exhausted.
- If VizieR remains unavailable and no Astrometry.net API key is configured, show the API-key page and setup command before stopping.
- The Windows launcher now waits for a key only after an unsuccessful run so the error and log path remain readable. Successful runs open the output folder and exit automatically.

### Fixed

- Preserve the specific `ERROR:` emitted by Horizons and Astrometry child processes instead of replacing it with a generic worker error.
- Fix `--siril-cache-mode cold-each` failing immediately because its Siril catalogue-cache directory was absent or exceeded the Windows path limit. Each trial now uses a short temporary empty cache.

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
