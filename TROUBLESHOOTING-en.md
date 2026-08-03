# Changes and Troubleshooting

This document explains recent Seestar Metcalf Stack behavior, common failures, and how to inspect a run. See the [README](README-en.md) for installation and normal use.

## Changes in 2026-07-31

- `--reference-frame-index` was removed because a filtered frame number cannot be known reliably before a run.
- Use `--reference-frame-file` with a subframe filename to choose a specific reference image. The file must belong to the selected session.
- If the reference frame lacks enough background stars, the program reports the reference filename, detected count, and required count, then stops without producing a stack.
- Registration failures in non-reference frames are expected during cloud passage, obstructions, or pointing changes. Those frames are skipped and the remaining usable frames are stacked.
- Completion always reports `Stacked used/total frames; skipped excluded`. Output FITS names also record the number of frames actually used.

## Choosing a reference frame

The first frame is the default. For a long session, choose the temporal midpoint with:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame middle
```

To select a specific subframe, pass its filename, not the folder path:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame-file "Light_C2025 R2 (SWAN)_20.0s_IRCUT_20251103-185613.fit"
```

Quote paths and filenames containing spaces. On Windows, do not put a trailing `\` immediately before the closing quote.

## Background-star registration and skipped frames

`--registration-minpairs` is the minimum number of matched background-star pairs used by Siril. The default is 6.

The reference frame must meet this condition because it establishes the WCS and registration coordinate system. Select a clear frame with well-detected stars rather than lowering this value casually. Non-reference frames that fail registration are skipped, not fatal.

Inspect `*_shifts.csv` in the output folder:

- `used=true` means the frame was stacked.
- `used=false` means it was skipped; `reason` records why.
- `star_pairs` is the number of matched background-star pairs.

For example, `Stacked 53/64 frames; skipped 11` means that 53 of 64 selected frames were used. If too many frames are skipped, choose another session with `--session-index` or `--session-at`, or make a folder containing only a good continuous interval.

## Common problems

### `--reference-frame-file was not found`

The filename is not in the selected session or time range. Use `--list-sessions` to inspect sessions, then provide the exact filename including its extension.

### `The selected reference frame has insufficient background stars`

Choose a clearer reference frame. Clouds, twilight, poor focus, obstructions, and images just after a pointing adjustment are common causes. Try `--reference-frame middle` or specify a different filename.

Even when processing stops, `registration_diagnostics.csv` in the work directory records `detected_stars`, `fwhm_px`, and `roundness` for every frame. First keep candidates whose `detected_stars` meets `--registration-minpairs`, then prefer a smaller FWHM and larger roundness when choosing `--reference-frame-file`.

### Few frames were stacked

Open `*_shifts.csv` and inspect `reason`. Poor frames are intentionally skipped. If no usable registered frames remain, no stack is created and the run exits with an error. If the reference also fails, choose another reference.

### `No files matching *.fit`

Use the Seestar subframe folder, usually ending in `_sub`, rather than a final stacked-image folder. For `.fits` files, add `--pattern "*.fits"`.

### Siril is missing or fails

Use the Siril bundle, or install Siril and put `siril-cli` on PATH. `Not enough free disk space` means the output volume needs more free space. Add `--no-cleanup` when diagnosing a run to retain registered intermediate FITS files.

### `FileNotFoundError` while writing output on Windows

The combined installation, input, output, and target-name path may exceed the Windows path-length limit. Spaces are supported, but keep the directory structure short. For example, use a short installation directory such as `C:\Seestar Metcalf Stack` and avoid deeply nested OneDrive folders or long target-name nesting.

### Astrometry.net or Horizons fails

Check the API key and network for Astrometry.net. Cached `*_astrometry.json` and `*_wcs.fits` in the source folder are reused for the same reference. For Horizons target-name failures, use `--horizons-object`, `--horizons-command`, or a prepared `--ephemeris-csv` as described in the README.

## Useful files for a bug report

Do not include API keys or private information. Include the command, console output, `metcalf-*.log`, `*_shifts.csv`, `*_summary.json`, Seestar model/firmware, Siril version, and a few representative subframes when possible.
