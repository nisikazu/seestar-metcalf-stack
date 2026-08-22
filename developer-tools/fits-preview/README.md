# FITS Preview Experiment

Developer-only tool for converting one FITS image into a display PNG without
running the Metcalf stack pipeline. It uses the same preview stretch as the
main stacker and never modifies the FITS data or WCS header.

Run from the repository root:

```powershell
.\developer-tools\fits-preview\run-fits-preview.cmd "D:\downloads\220PMcNaught_sub\example.fit"
```

The default is the main tool's simple sigma stretch (`-1 sigma` to `+3 sigma`)
with exact-zero registration padding excluded from the scale estimate. Use
`--stretch percentile` with `--low-percentile` and `--high-percentile` for a
percentile stretch, or `--output` to select a different PNG path. The sigma
range is explicit and adjustable, for example:

```powershell
.\developer-tools\fits-preview\run-fits-preview.cmd input.fit --sigma-low -1 --sigma-high 3
```

Use `--north-up` (or the main-tool-compatible alias `--preview-north-up`) to
write a WCS-rotated PNG with celestial north upward. The input FITS must
contain a usable CD matrix or PC/CDELT WCS. The image is stretched before
rotation, so the black corners introduced by rotation cannot change the preview
brightness range.

Use `--sun-pa-left` (or `--preview-sun-pa-left`) with a Metcalf FITS that has
`SUN_PA` and `MTREFDEC` headers to rotate the solar direction to image left.
This puts the anti-solar direction, where a dust tail usually extends, to the
right. It uses the FITS WCS and is mutually exclusive with `--north-up`.

Use `--preview-at UL|UR|LL|LR` to write N/E sticks and a pale-yellow Sun arrow
into the PNG, and `--annotate-size 60` to set its radius in pixels. This
developer tool defaults to `--preview-at none` because it can preview arbitrary
FITS files that do not have solar headers.
The FITS must have numeric `SUN_PA` and `MTREFDEC` (or `CRVAL2`) headers.
Annotation is applied after any requested display rotation. This also writes a
compact `*_annotation_overlay.png`: an RGBA PNG containing only the N/E/Sun
marks, with every other pixel fully transparent. Its marker uses the requested
radius (`--annotate-size`), is centered in a small square with protective
padding, and is independent of `--preview-at`; place it anywhere when
compositing in another presentation tool.

This is the intended experimental home for further display-only additions. It
never modifies science data or FITS WCS headers.
