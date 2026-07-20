# Third-Party Notices

Seestar Metcalf Stack project code is licensed under the MIT License. These
notices describe separately licensed dependencies and bundled components.

## Siril

The standard Seestar Metcalf Stack package does not bundle Siril. Users install
Siril separately and the launcher finds its CLI through `SIRIL_CLI`, `PATH`, or
common Windows and macOS installation locations.

The Windows convenience release asset named `seestar-metcalf-stack-siril-...zip`
bundles Siril 1.4.1 so that the registration step can run without a separate
Siril installation.

Siril is licensed under the GNU General Public License version 3. In the
Siril-bundled convenience package, see `SIRIL-LICENSE-GPLv3.md` and
`SIRIL-SOURCE.txt`.

Upstream:

- https://siril.org/
- https://gitlab.com/free-astro/siril

## Python Packages

`numpy` and `Pillow` are installed from `requirements.txt` when the Python
source setup is used. They are also embedded in the Windows executable built by
PyInstaller. Their own licenses continue to apply to those embedded copies.

## Python and PyInstaller

The Windows executable contains the Python runtime and is assembled with
PyInstaller. Python and PyInstaller remain subject to their respective upstream
licenses. Node.js is not required or bundled by this project.
