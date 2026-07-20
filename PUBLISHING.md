# Publishing Seestar Metcalf Stack

This project should be published as a focused post-processing tool, not as the
entire local Seestar research workspace.

## Recommended GitHub Repository Contents

Use the Siril-free package contents as the GitHub repository root:

- `README.md`
- `README-en.md`
- `requirements.txt`
- `seestar-metcalf-stack.cmd`
- `seestar-metcalf-stack.exe`
- `build-seestar-metcalf-stack-exe.ps1`
- `setup-python-deps.cmd`
- `set-astrometry-api-key.cmd`
- `siril-cli.cmd`
- `scripts/astrometry_solve.py`
- `scripts/`
- `tests/`
- `.github/workflows/tests.yml`
- `.gitignore`
- `THIRD-PARTY-NOTICES.md`
- `LICENSE` (MIT)

The main CMD accepts the source folder as its first argument and can also be
used as a drag-and-drop target. It prefers the bundled EXE and falls back to
Python when the EXE is absent. If Python code is changed, remove the EXE or
rebuild it so the CMD does not run stale code.

Do not publish local observing data, Seestar PEM files, API keys, logs, packaged
zips, `downloads/`, `siril_work/`, `metcalf_output/`, `plate_solve/`, or the broader Seestar
control/reverse-engineering workspace.

## Release Assets

Create both release zips:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\package-seestar-metcalf-stack.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File .\package-seestar-metcalf-stack-siril.ps1
```

Upload both files from `dist/` to the GitHub Release:

- `seestar-metcalf-stack-vX.Y.Z.zip`: standard package with EXE, Siril-free
- `seestar-metcalf-stack-siril-vX.Y.Z.zip`: Windows convenience package with Siril bundled
  and `seestar-metcalf-stack.exe` containing the Python runtime

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
- seestar-metcalf-stack-vX.Y.Z.zip: recommended package; install Siril separately.
- seestar-metcalf-stack-siril-vX.Y.Z.zip: Windows convenience package with Siril bundled.

Requirements:
- Siril CLI
- Astrometry.net API key
- Network access to Astrometry.net and JPL Horizons

The standard package includes `seestar-metcalf-stack.exe`; Python is only
needed when rebuilding the executable or using the Python fallback.
```
