# Seestar Metcalf Stack

[Changes and troubleshooting](TROUBLESHOOTING-en.md) | [Changelog](CHANGELOG-en.md)

[日本語](README.md) | [macOS setup (Japanese)](README-macOS.md)

Seestar Metcalf Stack turns subframe FITS files from Seestar, DWARF, and similar
instruments, or SharpCap Live Stack raw frames into a stack that follows a moving comet or asteroid. The same processing
base can also produce a background-star-aligned fixed-target stack for variable-star work.

This is a post-processing tool. It does not control a Seestar and does not need
the Seestar PEM/private communication key.

## Workflow: from observation to stack

This tool processes individual subframes produced by Seestar, DWARF, SharpCap,
and similar imaging workflows. Observe a comet or asteroid and keep the
original frame files.

1. Select the comet or asteroid in the imaging device or capture software and
   start the observation.
2. Turn **subframe saving ON** in the capture settings. A final stacked image
   alone is not sufficient because the pipeline needs the individual exposure
   times and frames.
3. After the observation, copy the subframe directory to the PC. For Seestar,
   use USB file access or STA-mode network file sharing. Its directory normally
   ends in `_sub` and contains `.fit` or `.fits` files.
4. Drag the subframe directory onto `seestar-metcalf-stack.cmd`, or run the
   command shown below.

### Open Seestar internal storage from Windows

Connect the PC and Seestar to the same STA network, or connect the PC to the Seestar AP,
then double-click `seestar-open-storage.cmd`. The helper resolves `seestar.local` to
IPv4 and directly tries `\\<IPv4>\EMMC Images\MyWorks` without relying on `net view`
share enumeration. If the STA address fails, it also tries the AP address `10.0.0.1`.
No PEM or API key is required.

It is normal for `ping seestar.local` to display an IPv6 address. This helper explicitly
selects the IPv4 A record for SMB access. If automatic discovery is unavailable, pass a
known address:

```bat
.\seestar-open-storage.cmd 192.168.0.23
```

The helper diagnoses the connection in this order:

1. Resolve `seestar.local` to IPv4.
2. Check whether TCP 445 (SMB) is reachable.
3. Directly open `\\<IPv4>\EMMC Images\MyWorks`.
4. On failure, report the Windows insecure-guest, required-signing, and
   required-encryption settings.
5. If the STA address fails, also check `10.0.0.1`.

**An open TCP 445 port followed by `net view` error 53 does not prove that the Seestar
share is disabled.** If Windows says that organizational security policies block
unauthenticated guest access, the Windows SMB client is rejecting the Seestar guest
connection.

To change the setting through the Windows UI, search the Start menu for `gpedit.msc`
and look for **Lanman Workstation > Enable insecure guest logons**. If the helper also
reports signing or encryption as `enabled`, disable the corresponding requirement in
the same policy editor. If those items aren't available, use the PowerShell method below.

To inspect the current values, open PowerShell as Administrator and run:

```powershell
Get-SmbClientConfiguration |
  Select-Object EnableInsecureGuestLogons, RequireSecuritySignature, RequireEncryption
```

If you understand and accept the security trade-off, allow guest access with:

```powershell
Set-SmbClientConfiguration -EnableInsecureGuestLogons $true -Force
```

Only when `RequireSecuritySignature` is `True`, also disable the signing requirement,
which is incompatible with guest access:

```powershell
Set-SmbClientConfiguration -RequireSecuritySignature $false -Force
```

Likewise, only when `RequireEncryption` is `True`, disable required encryption:

```powershell
Set-SmbClientConfiguration -RequireEncryption $false -Force
```

These settings affect all outbound SMB client connections on the PC, not only the
Seestar. Allowing unauthenticated guest access and disabling required signing reduce
protection against spoofed servers and adversary-in-the-middle attacks; disabling
required encryption also reduces protection against eavesdropping. Use them only on a
trusted network, record the original values, and restore those values after file copying.
This helper never changes the settings automatically. Consult the system administrator
when policy changes are prohibited on a managed PC.

For the Windows behavior and security implications, see Microsoft's documentation for
[insecure guest logons](https://learn.microsoft.com/windows-server/storage/file-server/enable-insecure-guest-logons-smb2-and-smb3),
[SMB signing](https://learn.microsoft.com/windows-server/storage/file-server/smb-signing),
and [required SMB encryption](https://learn.microsoft.com/windows-server/storage/file-server/configure-smb-client-require-encryption).

### Fixed-target stacking for variable stars

On Windows, drag the subframe directory onto `seestar-fixed-stack.cmd`. This
mode does not query JPL Horizons or apply a Metcalf motion. It uses only the
background-star registration determined by Siril or a SharpCap StackLog and
writes `*_fixed_stack.fit`. A target name and observing-site coordinates are
not required, while plate solving still runs to attach a WCS.

The fixed-mode defaults are mean combination, float32 FITS, saturation warnings
enabled, and per-frame quadratic background correction. Every default can be
overridden with the normal CLI options:

```bat
.\seestar-fixed-stack.cmd "C:\path\to\variable_star_sub"
.\seestar-fixed-stack.cmd "C:\path\to\variable_star_sub" --background-normalization none
.\seestar-fixed-stack.cmd "C:\path\to\variable_star_sub" --saturation-warning disable --output-bitpix uint16
```

Choose `--background-normalization none|offset|plane|quadratic` for the observing
goal. `plane` or the default `quadratic` can remove changing low-altitude light
pollution gradients frame by frame; use `none` when retaining the uncorrected
linear count relationship is more important. Supply saved individual subframes,
not a firmware-processed final stack.

### Information required for each input type

| Input | Target | Pixel scale | Observing site | Astrometry.net API key |
| --- | --- | --- | --- | --- |
| Seestar/DWARF FITS | Prefer FITS `OBJECT`; override only when missing | Prefer FITS focal-length/pixel metadata; otherwise use `--pixel-scale-arcsec` | Prefer FITS; geocenter fallback | Normally unnecessary; used only if Siril cannot solve locally |
| SharpCap FITS | Specify when `OBJECT` is absent | Record focal length in SharpCap or specify the option | FITS or command option; optional | Normally unnecessary; fallback only |
| SharpCap PNG/TIFF | Requires `--horizons-object`, `--horizons-command`, or `--ephemeris-csv` | Requires `--pixel-scale-arcsec` | Optional; recommended for close approaches | Normally unnecessary; fallback only |

When a Seestar or DWARF FITS set contains a target name, timestamps, approximate
center, and a nearly correct image scale, dropping the subframe directory onto
the launcher is sufficient. The default `--plate-solver auto` tries Siril
locally first, so basic processing works without an Astrometry.net API key.
If required metadata is absent, the tool stops and reports the option that must
be supplied rather than continuing with an unsafe guess.

The default input pattern is `*.fit*`, so both `.fit` and `.fits` files are
accepted. Other files in the directory are not sent to Siril. This also allows
FITS frames exported by SharpCap to be used. If the FITS files do not contain
an observing site, Horizons automatically falls back to the geocenter.

### SharpCap Live Stack input

For SharpCap 4.1.10745 or later, enable Live Stack raw-frame saving, `Create CSV
log of frame information for each stack`, and alignment. When every selected
row has X/Y offsets and rotation, the pipeline reuses those transforms for
background-star alignment. Only rows whose `Frame Stacked?` value is true are
used by default.

The tool reads `*.CameraSettings.txt` and uses Siril to apply the recorded master
dark, master flat, hot-pixel correction, and cold-pixel correction before
debayering. A nonzero `Hot Pixel Sensitivity` enables hot-pixel correction, but
the SharpCap value is not converted to a Siril threshold; Siril's default sigma
of 3 is used. Relocated masters are also searched by basename in nearby `darks`
and `flats` directories.

With CameraSettings present, no separate hot-pixel setup is needed. Siril
applies the correction and debayers the raw frames into the same kind of color
image expected from Seestar subframes before stacking. PNG/TIFF files normally
do not contain the target name or image scale, so supply those values from a
terminal as shown below. SharpCap FITS can still be a drag-and-drop input when
the required values are already present in its headers.

For frames already calibrated by another program, pass `--preprocessing disable`
to prevent double correction. Preserve the **same filename, dimensions,
orientation, and crop** if StackLog transforms will be reused.

The recommended approach is to copy the complete session and replace only the
images inside the copied `rawframes` directory:

```text
10P_processed_session\
  stacklog.csv
  Stack.CameraSettings.txt
  rawframes\
    frame_00001.png
    frame_00002.png
```

If only the raw-frame directory is copied, `stacklog.csv` and the
`*.CameraSettings.txt` file may instead be copied into that directory. The tool
searches the dropped directory first and then its parent. CameraSettings provides
the SharpCap version, PNG/TIFF exposure, and calibration/cosmetic-correction
settings. Copy the referenced masters too, or specify `--dark-file` and
`--flat-file`.

Instead of a folder, you may drag and drop `stacklog.csv` itself onto
`seestar-metcalf-stack.cmd`. Its parent directory becomes the session source,
and matching frame basenames are resolved from that directory or its descendants.

```text
10P_processed_rawframes\
  stacklog.csv
  Stack.CameraSettings.txt
  frame_00001.png
  frame_00002.png
```

The old absolute paths stored in the CSV may be broken after copying. Files are
matched by the `Raw frame file` basename, and a same-name image under the
dropped directory takes priority over the recorded old path. Ambiguous duplicate
filenames stop the run instead of silently selecting the wrong image.

For PNG/TIFF Live Stack raw frames, specify the target and pixel scale. The
observing site is optional:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\10P_processed_rawframes" --horizons-object "10P/Tempel 2" --pixel-scale-arcsec 2.392 --site-longitude 139.6 --site-latitude 35.9
```

Use `--horizons-object`, `--horizons-command`, or an existing
`--ephemeris-csv` for the target. `--pixel-scale-arcsec` is in arcseconds per
pixel. Longitude is east-positive and latitude is north-positive. If the site
is omitted, Horizons uses the geocenter. This is often a small difference for
ordinary comet or asteroid sessions lasting a few hours, but accurate site
coordinates are important for a close Earth approach where topocentric parallax
is significant.

The command-line site coordinates take precedence over `SITELONG` and `SITELAT`
in the FITS headers. If the FITS camera metadata does not provide a pixel
scale, provide the approximate scale in **arcseconds per pixel**. With effective
focal length in mm and pixel pitch in micrometers, calculate it as:

```text
pixel scale [arcsec/pixel] = 206.265 * pixel pitch [um] / effective focal length [mm]
```

For binned images, multiply the physical pixel pitch by the binning factor.
For example, 250 mm and 2.9 um gives `206.265 * 2.9 / 250 = 2.392` arcsec/pixel.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-object "10P/Tempel 2" --pixel-scale-arcsec 2.392
```

Astrometry.net's JSON calibration is sufficient for stacking. If its optional
WCS download returns an HTML error page or another non-FITS response, the tool
rejects that response and continues with the valid JSON calibration instead.

A two-dimensional Bayer PNG/TIFF without embedded Bayer metadata can be
specified with `--bayer-pattern RGGB` (or `BGGR`, `GRBG`, `GBRG`). FITS
`DATE-AVG` is preferred; PNG/TIFF exposure midpoint is estimated from StackLog
and CameraSettings. Siril always performs preprocessing and debayering. Complete
StackLog transforms replace only Siril registration; incomplete or disabled
alignment uses Siril for registration too. See
[SharpCap timestamp design](SHARPCAP-TIMESTAMPS.md).

Command-line calibration choices override CameraSettings. If a recorded master
cannot be found after relocation, the run stops with a clear request for
`--dark-file` or `--flat-file` rather than silently using uncalibrated data.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\SharpCap\session" --dark-file "C:\masters\dark.fit" --flat-file "C:\masters\flat.fit"
.\seestar-metcalf-stack.cmd "C:\path\to\processed\frames" --preprocessing disable
```

Individual `--dark-correction`, `--flat-correction`,
`--hot-pixel-correction`, and `--cold-pixel-correction` switches accept
`auto`, `enable`, or `disable`. `--hot-pixel-sigma` and `--cold-pixel-sigma`
override Siril's cosmetic-correction thresholds.

## External tools

The raw subframes do not by themselves provide the complete answer to three
questions: where the image points, where the moving object was at each exposure,
and how the background stars shifted. These tools provide those separate answers:

- **Siril** applies dark/flat and hot/cold-pixel corrections, debayers frames,
  plate-solves the reference locally, and estimates background-star registration
  matrices when StackLog transforms are unavailable. Seestar FITS normally supplies a
  reliable center and image scale, making the local solve fast.
- **Astrometry.net** is an optional fallback when Siril cannot solve the
  reference. Its account and API key are needed only for that fallback.
- **JPL Horizons** supplies the target's RA/Dec at every exposure time in
  moving-target mode. Fixed-target mode does not contact Horizons. No JPL API
  key is required.
- **Python, NumPy, and Pillow** are needed when running or modifying the source
  scripts. The distributed `seestar-metcalf-stack.exe` contains the Python
  runtime needed for normal use, so ordinary users do not need to install Python
  or these libraries separately.

Siril determines where the image points and prepares the raw pixels; Horizons
determines where the target moved. Complete SharpCap StackLog transforms can
replace only Siril's background-star registration. Otherwise Siril
`register -2pass` estimates matrices without writing registered FITS. Python
composes each matrix with its Metcalf translation, resamples the preprocessed
frame once, and produces the Metcalf stack, star-fixed stack, linear FITS, and
previews.

## Requirements and package choices

- Windows 10/11, or macOS 13 or newer when running the Python source
- Internet access for JPL Horizons in moving-target mode; fixed-target mode does not require it
- Siril 1.4 or newer
- An Astrometry.net API key only for optional fallback solving

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

In 0.7.x, Siril is still required when every SharpCap frame has X/Y offsets and
rotation because it performs calibration, debayering, and plate solving. StackLog
replaces only registration-transform estimation.

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

1. Download a ZIP from the [official GitHub Releases page](https://github.com/nisikazu/seestar-metcalf-stack/releases). Before extracting it on Windows, right-click the ZIP and open `Properties`. If the bottom of the `General` tab says that the file came from another computer and shows an `Unblock` checkbox, select `Unblock`, click `OK`, and then extract the ZIP. If the checkbox is absent, no action is needed. Do not unblock a ZIP whose source you cannot verify.
2. If Siril is not installed, extract the Siril-bundled package. Normal EXE execution then needs no separate Python dependency installation.
3. With the Siril-free package, install Siril separately and make `siril-cli.exe` available on `PATH`, or set `SIRIL_CLI`. Siril is required in 0.7.x even when SharpCap X/Y offsets and rotation are complete.
4. Run the Python dependency installer only if you plan to use or modify the
   Python fallback:

   ```bat
   .\setup-python-deps.cmd
   ```

5. Optionally obtain an Astrometry.net API key if you want fallback solving when Siril cannot solve a reference:

   1. Open the [Astrometry.net sign-in page](https://nova.astrometry.net/signin).
   2. Sign in or create an account with one of the external identity providers shown on the page, such as a Google account.
   3. After signing in, open `API` or `API Help` in the top menu. You can also open the [API Help page directly](https://nova.astrometry.net/api_help).
   4. Copy the alphanumeric value shown after `Your API key is xxxxxx...`.

6. In Windows Explorer, open the extracted Seestar Metcalf Stack directory.
   Right-click an empty area inside the directory and choose `Open in Terminal`.

7. In that terminal, replace `YOUR_API_KEY` with the value copied in step 5. PowerShell requires `.\`, which means the current directory, before a `.cmd` or `.exe` stored in that directory:

   ```bat
   .\set-astrometry-api-key.cmd YOUR_API_KEY
   ```

The key is stored in `.astrometry_api_key` beside the scripts.

## Choose an observing session

Start by listing the sessions detected in a Seestar subframe folder. Listing is
local-only: it does not contact Astrometry.net, Horizons, or Siril.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\98943 Torifune_sub" --list-sessions
```

The output shows a 1-based session number, frame count, and local/UTC start and
end times. Sessions are separated when the gap between consecutive frames is
greater than 60 minutes. With no selector, the latest session is used.

Select a listed session by number:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --session-index 2
```

Or select the first session starting at or after a local date/time:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --session-at 20260709-195000
```

`--session-at` accepts `YYYYMMDD` or `YYYYMMDD-hhmmss`. It is interpreted in
the PC's local time zone. Missing time fields become `00`; hour, minute, and
second fields must be two digits. Invalid time fields become `00`, and invalid
month/day fields become `01`.

## Run a stack

The simplest run uses the latest session, arithmetic mean, and its first frame
as the reference:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\C2025 R2 (SWAN)_sub"
```

For normal use, simply drag the subframe folder onto
`seestar-metcalf-stack.cmd`. The output folder opens after a successful run.
To select a session, processing method, target name, or another option,
right-click an empty area in the installation directory, choose
`Open in Terminal`, and run the command with options as shown in the examples
above.

The pipeline automatically obtains a Horizons ephemeris, plate-solves and
preprocesses the reference with Siril, registers background stars through
StackLog or Siril, and writes all final products under
`metcalf_output\<target>_<method>-YYYYMMDD-HHMMSS`.
Astrometry.net is contacted only when Siril solving fails.

Verbose output is enabled by default for the CMD, shell launcher, EXE, and
Python entry point. The console first shows every detected session and marks the
selected one, then shows each pipeline stage, Siril output, stack method, and
`current/total` frame progress. The same output is appended while the process
runs to `metcalf_output\metcalf-YYYYMMDD-HHMMSS.log`. On successful completion,
the output folder opens in Explorer. When invoking the EXE or Python entry
point directly. Add `--no-verbose` to suppress detailed progress. Use
`--no-open-output` to keep Explorer or Finder from opening after success.

The default is `--stack-workers auto`. Immediately before stacking, the program
checks available RAM and the subframe/output-canvas dimensions. It reserves the
larger of 25% of available RAM or 512 MiB, then selects the largest safe count
from 4, 2, and 1. The decision and memory estimate are printed and recorded in
the summary JSON. An explicit `--stack-workers 1|2|4` overrides the initial
automatic choice.

If a worker allocation fails, every worker is stopped and all local results from
the still-uncommitted batch are discarded. The complete same batch is retried at
4 -> 2 -> 1 workers. Workers never write directly to global sum/count arrays;
the main thread accumulates only fully successful batches in input order. A
retry therefore cannot mix partial results, and worker count does not change
output pixels. FITS read, background fit/application, star and Metcalf
accumulation, Metcalf shift, and total stacking timings are also printed and
recorded in the summary JSON.

For Python installation, Siril discovery, Terminal use, and Finder drag and
drop on macOS, see [the macOS setup guide](README-macOS.md).

### Free space for large sessions

During Siril conversion, correction, and debayering, source, converted, and
preprocessed images temporarily coexist. Sessions containing hundreds of frames
can therefore require substantially more free space than the source FITS files.
Normal Siril registration uses `register -2pass` to obtain matrices and does not
write registered FITS files.
If Siril reports `Not enough free disk space`, free more space, select another
drive with `--work-root D:\metcalf_output`, or reduce the run with an option
such as `--count 400`. Source copies, conversion images, and staged calibration
files are removed after preprocessing succeeds. Unneeded preprocessed inputs are
removed after their stack contribution is accepted. The complete SharpCap
StackLog path still materializes transformed images from the recorded transforms.
Use `--no-cleanup` to retain intermediates for diagnosis.

### Plate-solve cache

The first successful solve is cached beside the source subframes using the
reference FITS filename:

- `<reference-stem>_siril_wcs.fits`
- `<reference-stem>_astrometry.json`
- `<reference-stem>_wcs.fits`
- `<reference-stem>_astrometry_submission.json` while/resuming a submission

Later runs using the same reference frame validate and reuse the cached Siril
WCS, Astrometry.net WCS, or JSON calibration without uploading the FITS again.
If a previous run uploaded
successfully but was interrupted while waiting for the result, the saved
submission ID is resumed instead of making another upload. A different
`--reference-frame` may select a different FITS and therefore has its own cache.
Use `--solve-dir` only when you want the persistent cache somewhere other than
the source folder.

### Mean, median, or rank-fit

Mean is the default and generally provides the best signal-to-noise ratio when
the input frames are clean. Python composes the background-star matrix and
Metcalf translation and applies one bilinear resampling. Pixels outside that
transformed frame do not contribute to the sum. Each output pixel is
divided by its integer number of contributing frames, and interpolation is
accepted only when all four bilinear source pixels are valid. This avoids
extrapolation and prevents low-coverage borders from becoming artificially
dark:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method mean
```

Use `--padding-policy legacy` only to reproduce the padding behavior of an
older release for comparison:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method mean --padding-policy legacy
```

### Normalize a time-varying sky background

Changes in altitude, twilight, moonlight, thin cloud, or other conditions can change the background from frame to frame. Even with per-pixel contribution counts, regions covered only by early frames can then remain brighter or darker. `--background-normalization` subtracts the fitted background from every usable registered frame, bringing it close to zero before stacking:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames"
```

The default `quadratic` mode splits the valid image into a 50x50 tile grid, samples each tile at four-pixel intervals for an RGB sigma-clipped median, and fits a second-order surface. A single residual-MAD rejection pass removes outlying tiles before refitting, producing deterministic results while reducing the influence of stars and ordinary small comets. Select `none` to disable background correction, `offset` to equalize only DC level, or `plane` for a first-order plane. Only when `--padding-policy legacy` is explicitly selected, omitted background correction falls back to `none` for legacy comparisons.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --background-normalization plane
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --background-normalization quadratic
```

The correction is applied only to real image pixels; registration and Metcalf-shift padding never contributes to either background estimation or the stack. Arithmetic retains signed values. Only after stacking, the arithmetic mean of the accepted frames' RGB local DC levels is added to real output pixels as a constant range safeguard for non-negative storage formats; it does not restore any fitted slope. `BGNORM`, `BGGOAL=zero`, `BGREF1` through `BGREF3` (the final output offsets), and per-frame background, subtraction, and tile diagnostics in the shifts CSV record the operation. These modes require the default `--padding-policy valid`.

Preview PNGs now default to a linear `-1 sigma` to `+3 sigma` stretch based on the simple mean and standard deviation of valid pixels in each RGB channel. Bright stars remain in this estimate so that noise and background variation are not over-emphasized. Use `--preview-stretch percentile` to restore the earlier percentile stretch.

This feature is intended for ordinary comet and asteroid fields. A very large comet or an extended DSO can be indistinguishable from the sky background and may be partly subtracted by a surface model. Use `none` or `offset` for those targets.

Median is more resistant to satellites, airplanes, hot pixels, and other
one-frame outliers. In a Metcalf stack, it reduces star trails and is intended
to improve the accuracy of comet photometry. However, it is slower, uses large
temporary disk-backed arrays, and usually has lower statistical efficiency.
Exact-zero padding is excluded from the median samples by default:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method median
```

Use `--zero-sample-policy include` to include exact zeros for comparison with
legacy results. The same option also applies to rank-fit stacks. This is not
recommended for normal stacking: in low-overlap areas, zero padding can become
the median and produce large completely black regions. Use it only when exact
zeros must intentionally be included, such as for a legacy comparison:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method median --zero-sample-policy include
```

Rank-fit sorts the nonzero samples at each pixel, keeps the central percentage,
fits a fifth-degree polynomial to brightness versus normalized rank, and returns
the fitted value at the median rank. The default central percentage is 50:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method rankfit --rankfit-fraction 50
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
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame middle
```

To use a particular subframe as the reference, specify its filename rather than its filtered index. Quote names containing spaces. The selected reference must have at least `--registration-minpairs` background-star pairs (default: 6), or the run stops with a warning. Other frames that cannot be registered, for example during cloud passage, are skipped and recorded in the shifts CSV; the remaining usable frames are stacked.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame-file "Light_C2025 R2 (SWAN)_20.0s_IRCUT_20251103-185613.fit"
```

The selected frame is first solved locally by Siril and is explicitly set as the
registration reference. It is sent to Astrometry.net only if Siril fails. Its
`DATE-OBS` and WCS are written to the final FITS.
The FITS headers also contain `REFMODE`, `REFINDEX`, `MTREFRA`, and `MTREFDEC`.

When a Siril solution contains SIP distortion, every available `A_*`, `B_*`,
`AP_*`, and `BP_*` order and coefficient is preserved in the final FITS; the
implementation is not limited to third order. Final products also record
`CREATOR`, `SWVER`, the plate solver in `PLTSOLVR`, accepted-frame count in
`NCOMBINE`, `TIMESYS=UTC`, the first accepted exposure start in `DATE-BEG`, and
the final accepted exposure end in `DATE-END`. When every accepted frame has
usable exposure metadata, `DATE-AVG` and `MJD-AVG` record the exposure-weighted
mean of the accepted exposure midpoints, `TELAPSE` records the elapsed seconds
from `DATE-BEG` to `DATE-END`, and `TOTEXP` records the sum of accepted exposure
seconds. These four derived values are omitted rather than estimated if any
accepted exposure duration is unknown. Reference-frame `DATE-OBS` and `EXPTIME`
remain unchanged because the WCS belongs to that reference and a mean stack is
still in per-frame ADU units. `HISTORY` identifies Metcalf, star-aligned,
comparison, or fixed-stack products.

### Subframe saturation warning

For comet or comparison-star photometry, a stacked image can hide saturation
that occurred in an individual subframe. Enable separate warning PNGs that mark
pixels exceeding 90 percent of the subframe saturation level in red:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --saturation-warning enable
```

The default is `--saturation-warning disable`. The threshold and warning color
can be selected explicitly:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --saturation-warning enable --saturation-threshold-percent 90 --saturation-color FF0000
```

`--saturation-threshold-percent` must be greater than 0 and no greater than 100.
`--saturation-color` is a six-digit RGB hexadecimal value. For a normal Seestar
unsigned 16-bit FITS, the full-scale level is 65535, so the default detects
values strictly greater than 58981.5. A FITS `SATURATE` or `SATLEVEL` value
takes precedence when present. `DATAMAX` is not used because it may describe
the observed maximum in that image rather than the detector saturation level.

Detection is performed on each Siril star-registered subframe after restoring
its ADU scale. Masks are propagated independently into the star-aligned and
moving-target-aligned coordinates. The color overlay is written only to the
dedicated warning PNGs; science FITS and ordinary preview PNGs are unchanged.

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
- `*_metcalf_north_up_preview.png`, `*_star_north_up_preview.png`, and
  `*_star_left_metcalf_right_north_up_preview.png`: display PNGs rotated with
  the plate-solved WCS so celestial north is up, created by
  `--preview-north-up`; the FITS files and ordinary previews are unchanged
- `*_metcalf_sun_pa_left_preview.png`: moving-target preview with the solar
  direction at left and the anti-solar direction at right, created by
  `--preview-sun-pa-left`; this usually puts a dust tail on the right
- `*_annotated_preview.png`: display preview with N/E orientation sticks and a
  Sun-direction arrow, created by `--preview-at UL|UR|LL|LR`; it is drawn after a
  requested north-up or Sun-left display rotation
- `*_annotation_overlay.png`: compact transparent RGBA PNG containing only the
  same N/E/Sun mark. Its physical marker radius is `--annotate-size`, and it
  has only protective padding, so it can be placed freely in a presentation or
  image editor
- `*_metcalf_saturation_warning.png`, `*_star_saturation_warning.png`: optional
  warning previews created by `--saturation-warning enable`
- `*_star_left_metcalf_right_saturation_warning.png`: side-by-side warning
  preview
- `*_shifts.csv`: per-frame star registration and target-motion offsets
- `*_registration_diagnostics.csv`: every frame's registration quality and
  transform, intended for diagnosing low acceptance and choosing a better reference
- `*_summary.json`, `moving_target_pipeline_summary.json`: reproducibility data

To create previews with celestial north up, use the following option. A plate
solution or valid cached WCS is required.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --preview-north-up
```

Use `--preview-sun-pa-left` to put the solar direction at left and show a dust
tail toward the right. The normal pipeline queries JPL Horizons at the reference
time and writes `SUN_PA` (solar position angle, north through east) and
`ASUN_PA` (anti-solar direction) into the moving-target FITS. A solar-query
failure does not stop the ordinary stack, but solar-direction preview options
then cannot be created.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --preview-sun-pa-left
```

N/E sticks and a Sun arrow are created at the upper-left corner by default
(`--preview-at UL`) with a 60-pixel radius. Set the corner of the annotated
preview with `--preview-at UR|LL|LR`, change the physical radius with, for
example, `--annotate-size 120`, or use `--preview-at none` to omit annotations.
The accompanying
`*_annotation_overlay.png` is a compact transparent sprite independent of the
corner setting, so place it anywhere when composing a figure.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --preview-sun-pa-left --preview-at LR --annotate-size 60
```

The registration diagnostics table records the index, source filename,
reference/used status, exclusion reason, FWHM, weighted FWHM, roundness,
detected stars, initial and fitted correspondence counts, inlier fraction,
and the background-star X/Y translation, rotation, and scale. Lower FWHM is
sharper and roundness nearer 1 is more circular. The reference frame has no
correspondence count because it is not matched against itself. The transform
columns describe the Siril matrix that maps each frame into reference-frame
coordinates.

If processing stops before the final stack, for example because the reference
is unsuitable, an early `registration_diagnostics.csv` snapshot remains in
the work directory with the same columns.

When few frames are accepted, inspect `reason` and `fitted_matched_pairs`
first. A useful alternative reference generally has many detected stars, low
FWHM, and high roundness. Correspondence counts are measured against the
currently selected reference, so verify another candidate by rerunning with
that filename through `--reference-frame-file`.

Final FITS values remain linear ADU data. Intermediate calculations use floating
point. The default unsigned 16-bit output uses no rescaling; use
`--output-bitpix float32` when you want to preserve fractional interpolation
values directly.

Source/conversion/calibration staging is removed after preprocessing. On the
normal Siril path, preprocessed inputs are removed after their accepted stack
contribution and no registered FITS are created. SharpCap StackLog transformed
images are removed after each accepted contribution, and median temporary arrays
after finalization. Use `--no-cleanup` to keep them for diagnosis.

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
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-object "C/2025 R2 (SWAN)"
```

### 2. Pass a raw Horizons COMMAND

If you know a working Horizons search expression or ID, pass it unchanged with `--horizons-command`. This bypasses automatic name conversion and is therefore more deterministic.

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "DES=24P;CAP;NOFRAG"
```

- `DES=24P`: search for the official designation 24P
- `CAP`: choose the appropriate closest-apparition solution
- `NOFRAG`: exclude fragments such as `73P-A` and select the parent comet

For a numbered asteroid, use its number followed by a semicolon:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "98943;"
```

When Horizons lists several orbit solutions, you can select the `Record #` corresponding to the required epoch:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "90001033;"
```

Horizons record numbers can change. Prefer an official designation with `CAP` / `NOFRAG` for normal use, and use a record number when processing historical observations that require a specific orbit solution. In PowerShell, quote the entire COMMAND because an unquoted semicolon separates commands.

### 3. Use an existing ephemeris CSV

If you generated a timestamp/RA/Dec CSV separately, bypass target lookup and use that file directly:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --ephemeris-csv "C:\path\to\horizons.csv"
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

Choose plate-solver behavior:

```bat
rem Default: Siril first, Astrometry.net only as fallback
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --plate-solver auto

rem Local Siril only, with no image upload
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --plate-solver siril
```

Include Seestar files whose names contain `_failed_`:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --include-failed-frames
```

Use an existing Astrometry.net result:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --astrometry-json "C:\path\to\solution.json"
```

Use geocentric Horizons coordinates instead of sending the FITS observing site:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-center geocenter
```

When quoting a Windows path for a `.cmd` file, omit the trailing backslash:
use `"C:\path\to\frames"`, not `"C:\path\to\frames\"`.

## Privacy

Astrometry.net receives one sanitized reference FITS only when Siril plate
solving fails and fallback is available. Site-location FITS cards are removed
before upload. By default, JPL Horizons receives the observing site
from the FITS header to calculate topocentric coordinates. Use
`--horizons-center geocenter` or your own `--ephemeris-csv` if you do not want to
send that site information.

## License and author

Seestar Metcalf Stack is released under the MIT License.

Copyright (c) 2026 **Nishida Kazufumi**
([@RollerRacers](https://twitter.com/RollerRacers)).

Siril is GPLv3 software and is not part of the MIT-licensed project code. See
`THIRD-PARTY-NOTICES.md` for details.
