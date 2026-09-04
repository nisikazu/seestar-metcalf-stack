# Star background analysis

This developer-only experiment measures residual background-star tracks in a
moving-target stack. It is not included in release packages and does not
change the production stacker.

The input must be a completed run made with `--no-cleanup`, so that
`registration_images/pp_frame_*.fit` and `pp_frame_.seq` remain available.
The tool reuses the production registration transforms and reads the prepared
frames once per output-row tile. It writes candidate FITS/PNG products and
`star_background_metrics.csv`.

The metric is the high-frequency positive residual on a mask made by moving
static stars from the star-aligned stack by each frame's Metcalf shift. A
same-area shifted mask is the control region. This is a diagnostic score, not
a photometric truth measurement.

Future tuning points for a GUI and the safety boundary between aesthetic
processing and photometric processing are documented in
[`TRACK-EXCLUDE-TUNING.md`](TRACK-EXCLUDE-TUNING.md).

Example:

```powershell
python .\developer-tools\star-background-analysis\star_background_analysis.py `
  --source-dir "D:\downloads\220PMcNaught_sub" `
  --prepared-dir "C:\local\codex\seestar\background_benchmark\220P-star-background-cube-20260903\registration_images" `
  --registration-seq "C:\local\codex\seestar\background_benchmark\220P-star-background-cube-20260903\registration_images\pp_frame_.seq" `
  --shifts-csv "C:\local\codex\seestar\background_benchmark\220P-star-background-cube-20260903\220PMcNaught_20.0s_IRCUT_20260819T164737Z-20260819T184122Z_247frames_median_shifts.csv" `
  --reference-star-stack "C:\local\codex\seestar\background_benchmark\220P-star-background-cube-20260903\220PMcNaught_20.0s_IRCUT_20260819T164737Z-20260819T184122Z_247frames_median_star_stack.fit" `
  --header-source "D:\downloads\220PMcNaught_sub\Light_220PMcNaught_20.0s_IRCUT_20260820-014759.fit" `
  --output-dir "C:\local\codex\seestar\background_benchmark\220P-star-background-analysis-20260903"
```

Candidates are the current mean and median, upper-trimmed means, a temporal
IQR clip, a local-density histogram-mode approximation, and mean accumulation
after excluding the predicted star-track mask. The latter is an experiment
only because masking can remove target flux where a star crosses the comet.
