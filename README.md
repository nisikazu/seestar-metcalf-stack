# Seestar Metcalf Stack

[日本語](README-ja.md)

Seestar Metcalf Stack turns Seestar subframe FITS files into a stack that
follows a moving comet or asteroid. It also creates a star-aligned stack from
the same frames, plus a side-by-side comparison FITS.

This is a post-processing tool. It does not control a Seestar and does not need
the Seestar PEM/private communication key.

## What the dependencies do

The setup has several parts because each one supplies information that is not
reliably present in a raw Seestar subframe:

- **Astrometry.net** plate-solves one reference frame. The solve establishes the
  exact sky coordinates, image scale, and orientation, so an ephemeris position
  can be converted into an image-pixel position. An Astrometry.net account and
  API key are required. `set-astrometry-api-key.cmd` saves the key for the tool.
- **JPL Horizons** supplies the target's RA/Dec at every exposure time. Those
  positions determine how far the comet or asteroid moved between frames. No
  JPL API key is required.
- **Siril** detects background stars and estimates each frame's translation,
  rotation, and scale relative to the reference frame. This tool then adds the
  Horizons-derived moving-target offset and performs the final pixel combine.
- **Python, NumPy, and Pillow** run the pipeline, calculate shifts and stacks,
  write linear FITS files, and create display previews.
- **Python** also handles the Astrometry.net upload, polling, calibration download,
  and resumable submission checkpoint.

Astrometry.net, Horizons, and Siril are therefore not interchangeable extras:
they respectively answer *where the image points*, *where the target moved*,
and *how the background-star field moved*.

## Requirements and package choices

- Windows 10 or 11
- Python 3.10 or newer
- Internet access for Astrometry.net and JPL Horizons
- An Astrometry.net API key
- Siril 1.4 or newer

The normal GitHub source and `seestar-metcalf-stack-vX.Y.Z.zip` require Python
3.10 or newer and do not bundle Siril. Install Siril separately and place
`siril-cli.exe` on `PATH`, or set the `SIRIL_CLI` environment variable to its
full path.

The larger Windows convenience asset
`seestar-metcalf-stack-siril-vX.Y.Z.zip` includes Siril and a
`seestar-metcalf-stack.exe` containing the Python runtime. Choose it if you want the
lowest setup cost. Its Siril files remain covered by GPLv3; see the notices
included in that package. The convenience package does not require a separate
Python installation.

## First-time setup

1. Install Python. Install Siril too unless using the bundled build.
2. Run the Python dependency installer from the extracted package:

   ```bat
   setup-python-deps.cmd
   ```

3. Sign in to [Astrometry.net](https://nova.astrometry.net/), open its
   [API help page](https://nova.astrometry.net/api_help), and copy your API key.
4. Save the key with the included assistant command:

   ```bat
   set-astrometry-api-key.cmd YOUR_API_KEY
   ```

The key is stored in `.astrometry_api_key` beside the scripts. That file is
ignored by Git and must not be published.

## Choose an observing session

Start by listing the sessions detected in a Seestar subframe folder. Listing is
local-only: it does not contact Astrometry.net, Horizons, or Siril.

```bat
seestar-metcalf-stack.cmd "C:\path\to\98943 Torifune_sub" --list-sessions
```

The output shows a 1-based session number, frame count, and local/UTC start and
end times. Sessions are separated when the gap between consecutive frames is
greater than 60 minutes. With no selector, the latest session is used.

Select a listed session by number:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --session-index 2
```

Or select the first session starting at or after a local date/time:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --session-at 20260709-195000
```

`--session-at` accepts `YYYYMMDD` or `YYYYMMDD-hhmmss`. It is interpreted in
the PC's local time zone. Missing time fields become `00`; hour, minute, and
second fields must be two digits. Invalid time fields become `00`, and invalid
month/day fields become `01`.

## Run a stack

The simplest run uses the latest session, arithmetic mean, and its first frame
as the reference:

```bat
seestar-metcalf-stack.cmd "C:\path\to\C2025 R2 (SWAN)_sub"
```

You can also drag the subframe folder onto `seestar-metcalf-stack-drop.cmd`. The
output folder opens after a successful run.

The pipeline automatically obtains a Horizons ephemeris, solves the reference
frame, registers the background stars, and writes all final products under
`metcalf_output\<target>_<method>-YYYYMMDD-HHMMSS`.

### Plate-solve cache

The first successful solve is cached beside the source subframes using the
reference FITS filename:

- `<reference-stem>_astrometry.json`
- `<reference-stem>_wcs.fits`
- `<reference-stem>_astrometry_submission.json` while/resuming a submission

Later runs using the same reference frame validate and reuse the cached WCS or
JSON calibration without uploading the FITS again. If a previous run uploaded
successfully but was interrupted while waiting for the result, the saved
submission ID is resumed instead of making another upload. A different
`--reference-frame` may select a different FITS and therefore has its own cache.
Use `--solve-dir` only when you want the persistent cache somewhere other than
the source folder.

### Mean, median, or rank-fit

Mean is the default and generally provides the best signal-to-noise ratio when
the input frames are clean:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method mean
```

Median is more resistant to satellites, airplanes, hot pixels, and other
one-frame outliers. In a Metcalf stack, it reduces star trails and is intended
to improve the accuracy of comet photometry. However, it is slower, uses large
temporary disk-backed arrays, and usually has lower statistical efficiency.
Exact-zero padding is always excluded from the median samples:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method median
```

Rank-fit sorts the nonzero samples at each pixel, keeps the central percentage,
fits a fifth-degree polynomial to brightness versus normalized rank, and returns
the fitted value at the median rank. The default central percentage is 50:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method rankfit --rankfit-fraction 50
```

`--rankfit-fraction` is an integer from 1 through 100. Output names and run
folders record it as `rankfit5_p50`. If fewer than seven central samples are
available, that pixel falls back to the nonzero median.

Output names always contain `_mean_`, `_median_`, or `_rankfit5_pNN_`. FITS
headers record the method in `STKMODE`, and rank-fit products also record
`RFFRAC` and `RFDEG`.

### First or midpoint reference frame

The first frame is the default registration, WCS, timestamp, and coordinate
reference. For a long session, the frame nearest the temporal midpoint can
reduce the largest registration and moving-target offsets:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame middle
```

The selected frame is sent to Astrometry.net and is explicitly set as Siril's
registration reference. Its `DATE-OBS` and WCS are written to the final FITS.
The FITS headers also contain `REFMODE`, `REFINDEX`, `MTREFRA`, and `MTREFDEC`.

## Outputs

Output names contain target, exposure, filter, UTC time range, used frame count,
and combine method, for example:

`C2025_R2_SWAN_20.0s_IRCUT_20251103T095234Z-20251103T105620Z_90frames_median_metcalf_stack.fit`

- `*_metcalf_stack.fit`: linear moving-target stack
- `*_star_stack.fit`: linear background-star stack from the same accepted frames
- `*_star_left_metcalf_right.fit`: both stacks side by side; WCS applies to the
  star-aligned left half
- `*_metcalf_preview.png`, `*_star_preview.png`: stretched display previews,
  not photometry products
- `*_shifts.csv`: per-frame star registration and target-motion offsets
- `*_summary.json`, `moving_target_pipeline_summary.json`: reproducibility data

Final FITS values remain linear ADU data. Intermediate calculations use floating
point. The default unsigned 16-bit output uses no rescaling; use
`--output-bitpix float32` when you want to preserve fractional interpolation
values directly.

Large Siril and median temporary image arrays are removed after a successful
run. Use `--no-cleanup` to keep them for diagnosis.

## Other useful options

Include Seestar files whose names contain `_failed_`:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --include-failed-frames
```

Use an existing Horizons CSV or Astrometry.net result:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --ephemeris-csv "C:\path\to\ephemeris.csv"
seestar-metcalf-stack.cmd "C:\path\to\frames" --astrometry-json "C:\path\to\solution.json"
```

Use geocentric Horizons coordinates instead of sending the FITS observing site:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-center geocenter
```

When quoting a Windows path for a `.cmd` file, omit the trailing backslash:
use `"C:\path\to\frames"`, not `"C:\path\to\frames\"`.

## Privacy

Astrometry.net receives one sanitized reference FITS. Site-location FITS cards
are removed before upload. By default, JPL Horizons receives the observing site
from the FITS header to calculate topocentric coordinates. Use
`--horizons-center geocenter` or your own `--ephemeris-csv` if you do not want to
send that site information.

## License and author

Seestar Metcalf Stack is released under the MIT License.

Copyright (c) 2026 **Nishida Kazufumi**
([@RollerRacers](https://twitter.com/RollerRacers)).

Siril is GPLv3 software and is not part of the MIT-licensed project code. See
`THIRD-PARTY-NOTICES.md` for details.
