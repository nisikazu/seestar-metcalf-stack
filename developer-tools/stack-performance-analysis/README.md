# Stack performance analysis

This developer-only benchmark measures the expensive per-frame operations in
the Python stacker against retained `registration_images/r_*.fit` files. It is
not included in release packages.

```powershell
python .\developer-tools\stack-performance-analysis\stack_microbenchmark.py `
  "C:\path\to\registration_images" --frames 6 --repeats 2
```

The benchmark retains the pre-optimization coordinate-grid translation,
full-grid quadratic background application, zero-shift resampling, and
temporary-producing accumulator locally. It compares those legacy algorithms
with the current production implementations, checks the complete valid mask,
and reports centre-target/reference-star aperture differences. It also measures
bounded NumPy thread parallelism at 1, 2, and 4 workers.

See [RESULTS-20260823.md](RESULTS-20260823.md) for the original prototype result
and the production-integration measurements. The two full 242-frame v0.9.0
runs are also available as machine-readable
[`LONG-RUN-20260824.csv`](LONG-RUN-20260824.csv).
