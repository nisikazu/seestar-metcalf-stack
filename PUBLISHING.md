# Publishing Seestar Metcalf Stack

This project should be published as a focused post-processing tool, not as the
entire local Seestar research workspace.

## Recommended GitHub Repository Contents

Use the Siril-free package contents as the GitHub repository root:

- `README.md`
- `README-en.md`
- `README-macOS.md`
- `TROUBLESHOOTING.md`
- `TROUBLESHOOTING-en.md`
- `CHANGELOG.md`
- `CHANGELOG-en.md`
- `DEVELOPMENT.md`
- `PUBLISHING.md`
- `PLATE-SOLVE-BENCHMARK.md`
- `README-Siril-CLI.md`
- `requirements.txt`
- `seestar-metcalf-stack.cmd`
- `seestar-metcalf-stack.sh`
- `seestar-metcalf-stack.exe`
- `build-seestar-metcalf-stack-exe.ps1`
- `build-release-packages.ps1`
- `run-plate-solve-benchmark.cmd`
- `run-siril-scale-tolerance.cmd`
- `release-package-manifest.psd1`
- `verify-release-packages.ps1`
- `setup-python-deps.cmd`
- `setup-macos.sh`
- `set-astrometry-api-key.cmd`
- `set-astrometry-api-key.sh`
- `macos/`
- `siril-cli.cmd`
- `scripts/astrometry_solve.py`
- `scripts/`
- `tests/`
- `.github/workflows/tests.yml`
- `.gitignore`
- `THIRD-PARTY-NOTICES.md`
- `LICENSE` (MIT)

The Windows CMD and macOS shell launcher accept the source folder as their first
argument. The Windows launcher is a drag-and-drop target. On macOS,
`setup-macos.sh` builds a Finder droplet that opens Terminal and invokes the same
Python CLI. Both launchers only select a runtime; logging, progress reporting,
error handling, and opening the output directory belong to the Python CLI.

Do not publish local observing data, Seestar PEM files, API keys, logs, packaged
zips, `downloads/`, `siril_work/`, `metcalf_output/`, `plate_solve/`, or the broader Seestar
control/reverse-engineering workspace.

## Release Assets

Before creating a release, update the `Unreleased` sections in both changelogs
to the release version and date, then verify the public tree:

```powershell
python -m unittest discover -s tests -p "test_*.py"
git diff --check
```

Confirm that no observing data, API key, FITS/PNG output, log, or local Siril
installation is included in the staged files. The bug-report template under
`.github/ISSUE_TEMPLATE/` should remain available in the public repository.

Before packaging a SharpCap Live Stack release, run one complete aligned
StackLog fixture with the Siril-bundled package and verify all of the following:

- Siril applies the CameraSettings calibration, cosmetic correction, and debayering plan;
- the log reports `Using SharpCap StackLog alignment after Siril preprocessing`;
- copied/calibrated same-name frames are selected instead of stale absolute CSV paths;
- Metcalf, star-fixed, and side-by-side FITS/PNG products are created.

Build both release zips through the single release entry point. The package
scope is defined only in `release-package-manifest.psd1`:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-release-packages.ps1 -Version X.Y.Z
```

This command rebuilds the Windows EXE, creates both directory trees and ZIPs,
and then verifies their contents. The Siril copy is checked against the pinned
source by exact file count and total byte count. The completed ZIP is opened
again and checked for required executables, licenses, minimum Siril file count,
and uncompressed size. The standard ZIP is checked to ensure that Siril was not
included accidentally. Any mismatch stops the build with a non-zero exit code.

Successful verification writes `dist/SHA256SUMS-vX.Y.Z.txt`. Upload the two ZIPs
and this checksum file. Do not upload an asset if this command did not finish
with both `Verifying ... OK` messages.

For a deliberate fast rebuild using an already current EXE, add
`-SkipExeBuild`. `build-release-packages.ps1` is the only supported release
package entry point, so package contents cannot diverge between two scripts.

Upload all three files from `dist/` to the GitHub Release:

- `seestar-metcalf-stack-vX.Y.Z.zip`: Siril-free package with Windows EXE and macOS source launchers
- `seestar-metcalf-stack-siril-vX.Y.Z.zip`: recommended Windows convenience package with Siril bundled
  and `seestar-metcalf-stack.exe` containing the Python runtime
- `SHA256SUMS-vX.Y.Z.txt`: SHA-256 checksums produced only after both ZIPs pass validation

The Siril-bundled zip must retain:

- `THIRD-PARTY-NOTICES.md`
- `SIRIL-LICENSE-GPLv3.md`
- `SIRIL-SOURCE.txt`

## Versioning

Use the same version number for both release assets. The source repository should
match that release version.

## License

The project code is released under the MIT License, copyright 2026 Nishida
Kazufumi (@RollerRacers). The Siril-bundled convenience zip remains subject to
Siril's GPLv3 notices for the bundled Siril component.

## Suggested Release Text

```text
Seestar Metcalf Stack vX.Y.Z

Post-process Seestar subframe FITS for moving targets such as comets and
asteroids. Generates a Metcalf/moving-target stack, a star-reference stack, and a
side-by-side comparison FITS.

Assets:
- seestar-metcalf-stack-siril-vX.Y.Z.zip: recommended Windows package with Siril bundled.
- seestar-metcalf-stack-vX.Y.Z.zip: Siril-free package and macOS source launchers.

Requirements:
- Siril CLI for preprocessing, local plate solving, and registration when needed
- Astrometry.net API key is optional fallback when Siril cannot solve locally
- Network access to JPL Horizons

The Windows packages include `seestar-metcalf-stack.exe`; Python is only needed
when rebuilding the executable or using the source fallback. macOS currently
uses the Python source setup documented in `README-macOS.md`.
```
