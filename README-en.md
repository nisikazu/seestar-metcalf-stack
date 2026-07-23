# Seestar Metcalf Stack

[日本語](README.md) | [macOS setup (Japanese)](README-macOS.md)

Seestar Metcalf Stack turns Seestar subframe FITS files into a stack that
follows a moving comet or asteroid. It also creates a star-aligned stack from
the same frames, plus a side-by-side comparison FITS.

This is a post-processing tool. It does not control a Seestar and does not need
the Seestar PEM/private communication key.

## Workflow: from observation to stack

This tool processes the individual subframes produced by a Seestar observation.
Before using it, observe a comet or asteroid and keep the original frame files.

1. Select the comet or asteroid in the Seestar app and start the observation.
2. Turn **subframe saving ON** in the capture settings. A final stacked image
   alone is not sufficient because the pipeline needs the individual exposure
   times and frames.
3. After the observation, copy the subframe directory to the PC. You can use
   USB file access, or put the Seestar in STA mode and retrieve the directory
   through network file sharing. The directory normally ends in `_sub` and
   contains `.fit` or `.fits` files.
4. Drag the subframe directory onto `seestar-metcalf-stack.cmd`, or run the
   command shown below.

## External tools

The raw subframes do not by themselves provide the complete answer to three
questions: where the image points, where the moving object was at each exposure,
and how the background stars shifted. These tools provide those separate answers:

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
- **Python, NumPy, and Pillow** are needed when running or modifying the source
  scripts. The distributed `seestar-metcalf-stack.exe` contains the Python
  runtime needed for normal use, so ordinary users do not need to install Python
  or these libraries separately.

Astrometry.net, Horizons, and Siril are therefore not interchangeable extras:
they respectively answer *where the image points*, *where the target moved*,
and *how the background-star field moved*. Siril detects background stars and
estimates translation, rotation, and scale between frames. Python then performs
the final Metcalf stack, star-fixed stack, linear FITS writing, and previews.

## Requirements and package choices

- Windows 10/11, or macOS 13 or newer when running the Python source
- Internet access for Astrometry.net and JPL Horizons
- An Astrometry.net API key
- Siril 1.4 or newer

The standard `seestar-metcalf-stack-vX.Y.Z.zip` includes
`seestar-metcalf-stack.exe`, so normal execution does not require a separate
Python installation. It does not bundle Siril: install Siril separately and
place `siril-cli.exe` on `PATH`, or set the `SIRIL_CLI` environment variable to
its full path.

If Siril is not already installed, use the larger Windows convenience asset
`seestar-metcalf-stack-siril-vX.Y.Z.zip` as the recommended package. It includes
Siril and a `seestar-metcalf-stack.exe` containing the Python runtime, so a normal
user does not need to install either Siril or Python separately. Its Siril files
remain covered by GPLv3; see the notices included in that package.

If Siril is already installed, or a smaller download is preferred, use
`seestar-metcalf-stack-vX.Y.Z.zip`. It also includes `seestar-metcalf-stack.exe`,
so Python is not required for normal execution. Install Siril separately and put
`siril-cli.exe` on `PATH`, or set `SIRIL_CLI` to its full path.

For an upgrade, the Siril-free package can replace the application files. Copy
these items from the previous installation into the new folder to retain the
bundled tools, API key, and previous outputs:

- `tools` (when using the Siril-bundled package)
- `.astrometry_api_key`
- `metcalf_output`

This migration also lets a Siril-bundled installation move to the smaller
Siril-free package. If Siril is not installed separately, continue using the
Siril-bundled package instead.

The source scripts remain included for inspection and development. If you edit
`scripts/*.py`, remove `seestar-metcalf-stack.exe` or rebuild it before running:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-seestar-metcalf-stack-exe.ps1
```

The command prefers the EXE when it is present, so leaving an old EXE beside
modified Python code would run the old code. On its first run, the build script
installs PyInstaller into `.build`, which requires network access.

## First-time setup

1. If Siril is not installed, extract the Siril-bundled package. Normal EXE
   execution then needs no separate Python dependency installation.
2. If using the Siril-free package, install Siril separately and make
   `siril-cli.exe` available on `PATH`, or set `SIRIL_CLI`.
3. Run the Python dependency installer only if you plan to use or modify the
   Python fallback:

   ```bat
   setup-python-deps.cmd
   ```

4. Obtain an Astrometry.net API key:

   1. Open the [Astrometry.net sign-in page](https://nova.astrometry.net/signin).
   2. Sign in or create an account with one of the external identity providers shown on the page, such as a Google account.
   3. After signing in, open `API` or `API Help` in the top menu. You can also open the [API Help page directly](https://nova.astrometry.net/api_help).
   4. Copy the alphanumeric value shown after `Your API key is xxxxxx...`.

5. In Windows Explorer, open the extracted Seestar Metcalf Stack directory.
   Right-click an empty area inside the directory and choose `Open in Terminal`.

6. In that terminal, replace `YOUR_API_KEY` with the value copied above:

   ```bat
   set-astrometry-api-key.cmd YOUR_API_KEY
   ```

The key is stored in `.astrometry_api_key` beside the scripts.

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

For normal use, simply drag the subframe folder onto
`seestar-metcalf-stack.cmd`. The output folder opens after a successful run.
To select a session, processing method, target name, or another option,
right-click an empty area in the installation directory, choose
`Open in Terminal`, and run the command with options as shown in the examples
above.

The pipeline automatically obtains a Horizons ephemeris, solves the reference
frame, registers the background stars, and writes all final products under
`metcalf_output\<target>_<method>-YYYYMMDD-HHMMSS`.

Verbose output is enabled by default for the CMD, shell launcher, EXE, and
Python entry point. The console first shows every detected session and marks the
selected one, then shows each pipeline stage, Siril output, stack method, and
`current/total` frame progress. The same output is appended while the process
runs to `metcalf_output\metcalf-YYYYMMDD-HHMMSS.log`. On successful completion,
the output folder opens in Explorer. When invoking the EXE or Python entry
point directly. Add `--no-verbose` to suppress detailed progress. Use
`--no-open-output` to keep Explorer or Finder from opening after success.

For Python installation, Siril discovery, Terminal use, and Finder drag and
drop on macOS, see [the macOS setup guide](README-macOS.md).

### Free space for large sessions

Siril temporarily stores both debayered and registered images during
background-star registration. Sessions containing hundreds of frames can
therefore require substantially more free space than the source FITS files.
If Siril reports `Not enough free disk space`, free more space, select another
drive with `--work-root D:\metcalf_output`, or reduce the run with an option
such as `--count 400`. Intermediate FITS files are removed automatically after
a registration failure unless `--no-cleanup` is specified.

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

## When Horizons cannot identify the target

By default, the pipeline reads the FITS `OBJECT` value and generates JPL Horizons search candidates from common Seestar naming forms. Automatic identification can fail when the Seestar label differs from the registered Horizons designation, or when a comet has multiple apparition solutions or fragments.

The following log messages indicate that target identification stopped in Horizons:

```text
Target candidate did not resolve: ...
No matches found.
Horizons response did not contain $$SOE/$$EOE ephemeris markers
Could not identify target '...' in JPL Horizons.
```

A returned list of multiple matches also means that the target and orbit solution were not unique. Recover using the following steps.

### 1. Override the target with its official designation

Find the official name, comet designation, or asteroid number with [JPL Horizons](https://ssd.jpl.nasa.gov/horizons/) or the [Horizons Lookup API](https://ssd-api.jpl.nasa.gov/doc/horizons_lookup.html), then override the FITS `OBJECT` value with `--horizons-object`. This option still applies name normalization and candidate fallback searches.

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-object "C/2025 R2 (SWAN)"
```

### 2. Pass a raw Horizons COMMAND

If you know a working Horizons search expression or ID, pass it unchanged with `--horizons-command`. This bypasses automatic name conversion and is therefore more deterministic.

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "DES=24P;CAP;NOFRAG"
```

- `DES=24P`: search for the official designation 24P
- `CAP`: choose the appropriate closest-apparition solution
- `NOFRAG`: exclude fragments such as `73P-A` and select the parent comet

For a numbered asteroid, use its number followed by a semicolon:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "98943;"
```

When Horizons lists several orbit solutions, you can select the `Record #` corresponding to the required epoch:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "90001033;"
```

Horizons record numbers can change. Prefer an official designation with `CAP` / `NOFRAG` for normal use, and use a record number when processing historical observations that require a specific orbit solution. In PowerShell, quote the entire COMMAND because an unquoted semicolon separates commands.

### 3. Use an existing ephemeris CSV

If you generated a timestamp/RA/Dec CSV separately, bypass target lookup and use that file directly:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --ephemeris-csv "C:\path\to\horizons.csv"
```

The CSV does not need a coordinate row at every subframe timestamp. For each FITS observation time, the tool linearly interpolates RA and Dec between the surrounding CSV rows. Frames before or after the CSV time range are linearly extrapolated from the first or last two rows.

Except during a close approach, the apparent motion is usually close to uniform and linear over an observing session of a few hours. Provide at least two coordinate rows that bracket the complete session: one at or before the first exposure and one at or after the last exposure. This keeps every frame within the interpolation range and avoids errors from extrapolation. Add more points within the session when curvature during a close approach or other nonlinear motion is significant.

The priority order is an existing `--ephemeris-csv`, `--horizons-command`, `--horizons-object`, then the FITS `OBJECT` value.

### Please report names that fail automatic identification

Names that do not resolve help us improve the normalization and fallback logic. Please contact us through [GitHub Issues](https://github.com/nisikazu/seestar-metcalf-stack/issues) or [@RollerRacers](https://twitter.com/RollerRacers) with:

- the Seestar Metcalf Stack version
- the exact FITS `OBJECT` value
- the intended official target name or designation
- the log section from `Trying Horizons target:` through the final error
- any `--horizons-object`, `--horizons-command`, or CSV input that succeeded

Do not publish your Astrometry.net API key, observing location, personal information, or FITS files. Check the log for private information before attaching it.

## Other useful options

Include Seestar files whose names contain `_failed_`:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --include-failed-frames
```

Use an existing Astrometry.net result:

```bat
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
