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

The v0.9.3 matrix-only registration, fused-resampling validation, 242-frame
runtime/RAM/storage measurements, and photometry comparison are recorded in
[`RESULTS-20260824-v0.9.3.md`](RESULTS-20260824-v0.9.3.md). Raw tables are
[`LONG-RUN-V093-20260824.csv`](LONG-RUN-V093-20260824.csv) and
[`PHOTOMETRY-V093-20260824.csv`](PHOTOMETRY-V093-20260824.csv).

Use `compare_stack_products.py` to compare complete valid masks, common-pixel
differences, a moving-target aperture, and independently recentered reference
stars:

```powershell
python .\developer-tools\stack-performance-analysis\compare_stack_products.py `
  old.fit new.fit --output comparison.json `
  --aperture-radius 10 --annulus-inner 14 --annulus-outer 22 `
  --recenter-radius 12
```
