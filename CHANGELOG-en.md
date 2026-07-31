# Changelog

This document records changes that affect users. See [DEVELOPMENT.md](DEVELOPMENT.md) for engineering decisions, known limitations, and handover notes.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
