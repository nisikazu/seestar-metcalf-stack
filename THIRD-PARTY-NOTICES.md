# Third-Party Notices

Seestar Metcalf Stack project code is licensed under the MIT License. These
notices describe separately licensed dependencies and bundled components.

## Siril

The standard Seestar Metcalf Stack package does not bundle Siril. Users install
Siril separately and the launcher finds `siril-cli.exe` through `SIRIL_CLI`,
`PATH`, or common Windows install locations.

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

`numpy` and `Pillow` are installed separately by `setup-python-deps.cmd` from
`requirements.txt`; they are not bundled in this zip.

## Node.js

Node.js is not bundled. It is used only for Astrometry.net upload/solve support
through `astrometry_solve.mjs`.
