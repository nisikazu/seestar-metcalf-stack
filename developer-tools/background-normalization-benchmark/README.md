# Background Normalization Benchmark

Developer-only tool for comparing the elapsed time of `none`, `offset`,
`plane`, and `quadratic` background normalization on the latest 220P session.
It is not part of release packages.

The default source is `D:\downloads\220PMcNaught_sub`. The tool reuses the
latest cached 220P Horizons CSV under `metcalf_output` and the latest
`*_siril_wcs.fits` in the source folder. Thus the comparison excludes network
ephemeris retrieval and plate solving, while retaining registration, background
fitting, stacking, and writing the normal outputs.

Run from the repository root:

```powershell
.\developer-tools\background-normalization-benchmark\run-220p-background-benchmark.cmd
```

Use `--repeat 3` for repeated measurements. `--source-dir`,
`--ephemeris-csv`, and `--wcs-fits` override the defaults. Results are stored
under `C:\local\codex\seestar\background_benchmark` by default as CSV,
Markdown, per-run logs, and each regular pipeline output directory. This
deliberately short path avoids Windows path-length failures while staging the
long Horizons CSV filename and writing long comparison FITS names.

The default measures every frame in the latest session. Use `--count 20` only
for a quick functional trial; it is not representative of the full-session
runtime.
