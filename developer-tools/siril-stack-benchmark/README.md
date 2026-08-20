# Siril Stack Benchmark (developer tool)

This tool is not included in release packages. It creates a small synthetic
FITS sequence and tests Siril's CLI stack behavior:

- Siril registration followed by mean and median stacking.
- Mean stacking with and without `-maximize`, to compare output framing.
- A known custom offset applied in Python before Siril stacking.

The last case is intentional. Siril's supported `stack` command consumes the
registration transforms stored in the sequence; it does not expose a
per-frame X/Y offset argument. The report therefore distinguishes direct
custom-offset support from the pre-aligned-image workaround.

Run from the repository root:

```powershell
.\developer-tools\siril-stack-benchmark\run-siril-stack-benchmark.cmd
```

Use `--siril` when Siril is installed elsewhere and `--output` to select the
temporary result directory. The generated `report.json` includes output
dimensions, elapsed times, known offsets, and Siril logs.
