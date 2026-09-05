"""Compare the five latest 220P stack-method production outputs.

This is a developer-only diagnostic.  It deliberately measures the final
products with common masks rather than changing the production stack path.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "developer-tools" / "star-background-analysis"))

import moving_target_stack as stacker  # noqa: E402
from star_background_analysis import (  # noqa: E402
    shifted_mask_without_wrap,
    shifted_star_track_mask,
    static_star_mask,
)


BASE = REPO_ROOT / "metcalf_output"
RUNS = {
    "mean": BASE / "220PMcNaught_mean-20260905-110641",
    "median": BASE / "220PMcNaught_median-20260905-110922",
    "mad-clip": BASE / "220PMcNaught_mad-clip-20260905-111419",
    "winsorized-sigma": BASE / "220PMcNaught_winsorized-sigma-20260905-112022",
    "sigma-clip": BASE / "220PMcNaught_sigma-clip-20260905-113133",
}
OUTPUT_DIR = BASE / "220PMcNaught-stack-method-comparison-20260905"


def output_fit(run_dir: Path) -> Path:
    matches = sorted(run_dir.glob("*_metcalf_stack.fit"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one Metcalf stack FITS in {run_dir}, found {len(matches)}")
    return matches[0]


def load_product(run_dir: Path) -> tuple[np.ndarray, dict[str, object]]:
    summary = json.loads((run_dir / "moving_target_pipeline_summary.json").read_text(encoding="utf-8"))
    data = np.asarray(stacker.read_fits(output_fit(run_dir)).data, dtype=np.float64)
    if data.ndim == 2:
        data = data[np.newaxis, ...]
    if data.ndim != 3:
        raise RuntimeError(f"Expected mono or RGB output, got shape {data.shape}")
    return data, summary


def box_blur(plane: np.ndarray, radius: int = 12) -> np.ndarray:
    radius = max(1, int(radius))
    source = np.nan_to_num(np.asarray(plane, dtype=np.float64), nan=0.0)
    padded = np.pad(source, radius, mode="edge")
    integral = np.pad(np.cumsum(np.cumsum(padded, axis=0), axis=1), ((1, 0), (1, 0)))
    width = 2 * radius + 1
    return (
        integral[width:, width:]
        - integral[:-width, width:]
        - integral[width:, :-width]
        + integral[:-width, :-width]
    ) / float(width * width)


def robust_sigma(values: np.ndarray) -> float:
    finite = np.asarray(values, dtype=np.float64)
    finite = finite[np.isfinite(finite)]
    if finite.size < 2:
        return float("nan")
    return float(1.4826 * np.median(np.abs(finite - np.median(finite))))


def disk_mask(shape: tuple[int, int], x: float, y: float, radius: float) -> np.ndarray:
    height, width = shape
    yy, xx = np.ogrid[:height, :width]
    return (xx - x) ** 2 + (yy - y) ** 2 <= radius * radius


def load_shift_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows = [row for row in rows if row.get("used", "").strip().casefold() in {"true", "1", "yes"}]
    rows.sort(key=lambda row: int(row["index"]))
    if not rows:
        raise RuntimeError(f"No used shifts in {path}")
    return rows


def build_track_masks(
    star_stack: np.ndarray,
    rows: list[dict[str, str]],
    target_x: float,
    target_y: float,
    shape: tuple[int, int],
    radius: int = 8,
) -> tuple[np.ndarray, np.ndarray, float, list[tuple[int, int, float]]]:
    positions_mask, positions = static_star_mask(star_stack, target_x, target_y, 48, radius)
    del positions_mask
    candidates: list[tuple[float, np.ndarray]] = []
    for sign in (1.0, -1.0):
        union = np.zeros(shape, dtype=bool)
        for row in rows:
            dx = sign * float(row["extra_dx_px"])
            dy = sign * float(row["extra_dy_px"])
            union |= shifted_star_track_mask(positions, dx, dy, 0, shape[0], shape[1], radius)
        candidates.append((sign, union & ~disk_mask(shape, target_x, target_y, 70.0)))
    return candidates[0][1], candidates[1][1], 1.0, positions


def track_score(data: np.ndarray, track: np.ndarray, controls: list[np.ndarray]) -> dict[str, float]:
    gray = np.nanmean(data, axis=0)
    residual = gray - box_blur(gray, 12)
    track_values = residual[track & np.isfinite(residual)]
    control_values = [residual[mask & np.isfinite(residual)] for mask in controls]
    control_positive = [float(np.mean(np.maximum(values, 0.0))) for values in control_values if values.size]
    track_positive = float(np.mean(np.maximum(track_values, 0.0)))
    control_median = float(np.median(control_positive)) if control_positive else float("nan")
    excess = track_positive - control_median
    return {
        "track_positive_excess_adu": track_positive,
        "control_positive_excess_adu": control_median,
        "track_excess_over_control_adu": excess,
        "track_to_control": track_positive / control_median if control_median > 0 else float("nan"),
        "track_p95_residual_adu": float(np.percentile(track_values, 95.0)),
        "control_p95_residual_adu": float(np.median([np.percentile(values, 95.0) for values in control_values if values.size])),
    }


def select_background_roi(
    reference: np.ndarray,
    track: np.ndarray,
    target_x: float,
    target_y: float,
    tile: int = 128,
) -> tuple[slice, slice, dict[str, float]]:
    gray = np.nanmean(reference, axis=0)
    residual = gray - box_blur(gray, 12)
    height, width = gray.shape
    target = disk_mask((height, width), target_x, target_y, 80.0)
    candidates: list[tuple[float, slice, slice, dict[str, float]]] = []
    for y0 in range(0, height - tile + 1, tile):
        for x0 in range(0, width - tile + 1, tile):
            ys = slice(y0, y0 + tile)
            xs = slice(x0, x0 + tile)
            area = np.ones((tile, tile), dtype=bool)
            excluded = target[ys, xs] | track[ys, xs]
            if np.mean(excluded) > 0.01:
                continue
            values = residual[ys, xs][~excluded]
            if values.size < tile * tile * 0.95:
                continue
            score = robust_sigma(values) + 0.05 * float(np.percentile(np.abs(values), 99.0))
            details = {
                "x0": float(x0),
                "y0": float(y0),
                "width": float(tile),
                "height": float(tile),
                "raw_median_adu": float(np.median(gray[ys, xs][~excluded])),
                "highpass_robust_sigma_adu": robust_sigma(values),
            }
            candidates.append((score, ys, xs, details))
    if not candidates:
        raise RuntimeError("Could not find a common star-free background ROI")
    _score, ys, xs, details = min(candidates, key=lambda item: item[0])
    return ys, xs, details


def photometry(
    data: np.ndarray,
    target_x: float,
    target_y: float,
    robust_noise_sigma: float,
    standard_noise_sigma: float,
) -> dict[str, float]:
    shape = data.shape[-2:]
    aperture = disk_mask(shape, target_x, target_y, 8.0)
    annulus = disk_mask(shape, target_x, target_y, 32.0) & ~disk_mask(shape, target_x, target_y, 18.0)
    values = np.nanmean(data, axis=0)
    background = float(np.median(values[annulus & np.isfinite(values)]))
    aperture_values = values[aperture & np.isfinite(values)]
    signal = float(np.sum(aperture_values - background))
    center = disk_mask(shape, target_x, target_y, 1.5)
    center_values = values[center & np.isfinite(values)]
    center_excess = float(np.median(center_values) - background)
    return {
        "comet_aperture_signal_adu": signal,
        "comet_center_excess_adu": center_excess,
        "comet_aperture_pixels": float(aperture_values.size),
        "comet_aperture_snr_robust": signal / (robust_noise_sigma * math.sqrt(aperture_values.size)) if robust_noise_sigma > 0 else float("nan"),
        "comet_aperture_snr_standard": signal / (standard_noise_sigma * math.sqrt(aperture_values.size)) if standard_noise_sigma > 0 else float("nan"),
        "comet_background_adu": background,
    }


def main() -> int:
    products: dict[str, np.ndarray] = {}
    summaries: dict[str, dict[str, object]] = {}
    for method, run_dir in RUNS.items():
        products[method], summaries[method] = load_product(run_dir)
    reference_summary = summaries["mean"]
    first_row = load_shift_rows(Path(reference_summary["stack"]["registration_seq"]).parent.parent / "220PMcNaught_20.0s_IRCUT_20260819T164737Z-20260819T184122Z_247frames_mean_shifts.csv")
    # The summary stores 1-based target coordinates in the registration diagnostics.
    target_x = float(first_row[0]["target_x_1based"]) - 1.0
    target_y = float(first_row[0]["target_y_1based"]) - 1.0
    shift_path = RUNS["mean"] / "220PMcNaught_20.0s_IRCUT_20260819T164737Z-20260819T184122Z_247frames_mean_shifts.csv"
    rows = load_shift_rows(shift_path)
    star_stack = stacker.read_fits(next(RUNS["mean"].glob("*_star_stack.fit"))).data
    plus, minus, _unused, positions = build_track_masks(
        star_stack,
        rows,
        target_x,
        target_y,
        products["mean"].shape[-2:],
    )
    mean_residual = np.nanmean(products["mean"], axis=0)
    mean_residual = mean_residual - box_blur(mean_residual, 12)
    plus_score = float(np.mean(np.maximum(mean_residual[plus], 0.0)))
    minus_score = float(np.mean(np.maximum(mean_residual[minus], 0.0)))
    track = plus if plus_score >= minus_score else minus
    sign = "+" if track is plus else "-"
    controls = []
    for dy, dx in ((150, 150), (-150, 150), (150, -150), (-150, -150), (250, 0), (0, 250)):
        controls.append(shifted_mask_without_wrap(track, dy, dx))
    ys, xs, roi_details = select_background_roi(products["mean"], track, target_x, target_y)
    result_rows: list[dict[str, object]] = []
    for method, data in products.items():
        gray = np.nanmean(data, axis=0)
        residual = gray - box_blur(gray, 12)
        roi_values = residual[ys, xs]
        noise = robust_sigma(roi_values)
        standard_noise = float(np.std(roi_values))
        row: dict[str, object] = {
            "method": method,
            "output_fit": str(output_fit(RUNS[method])),
            "frames": summaries[method]["stack"]["used_frames"],
            "stack_seconds": summaries[method]["stack"]["stack_timing_seconds"]["total_stacking_wall"],
            "pipeline_seconds": summaries[method]["stack"]["pipeline_timing_seconds"]["total_pipeline_wall"],
            "peak_rss_bytes": summaries[method]["stack"]["process_peak_rss_bytes"],
            "background_highpass_robust_sigma_adu": noise,
            "background_highpass_standard_sigma_adu": standard_noise,
        }
        row.update(track_score(data, track, controls))
        row.update(photometry(data, target_x, target_y, noise, standard_noise))
        result_rows.append(row)

    mean_excess = float(next(row["track_excess_over_control_adu"] for row in result_rows if row["method"] == "mean"))
    for row in result_rows:
        row["track_removal_vs_mean_percent"] = 100.0 * (1.0 - float(row["track_excess_over_control_adu"]) / mean_excess) if mean_excess > 0 else float("nan")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / "stack_method_comparison.csv"
    fieldnames = list(result_rows[0].keys())
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(result_rows)
    report_path = OUTPUT_DIR / "stack_method_comparison.md"
    lines = [
        "# 220P スタック方式比較（2026-09-05）",
        "",
        "同じ220P・セッション2（247/247枚）で生成された最新5出力を、共通条件で比較した開発者向け診断です。速度は各production summaryのwall time、品質値は最終Metcalf FITSから算出しました。",
        "",
        "## 条件",
        "",
        f"- 彗星中心: 0-based pixel ({target_x:.3f}, {target_y:.3f})。開口半径8 px、背景環18--32 px。",
        f"- 背景σ: 星軌跡と彗星を避けた共通{int(roi_details['width'])}x{int(roi_details['height'])} px領域の高周波残差。主値はロバストσ=`1.4826 x MAD`、通常σ=`std`も併記。ROI左上=({int(roi_details['x0'])}, {int(roi_details['y0'])})、生値中央値={roi_details['raw_median_adu']:.3f} ADU。",
        f"- 星軌跡: 静止星48個、半径8 px、Metcalfシフトで生成した予測軌跡。符号は平均スタックで残差が大きい方（{sign} shift）を採用。対照領域6個の中央値を差し引いた。",
        "- `track_removal_vs_mean_percent` は、軌跡領域の正値高周波残差がmeanから何%減ったかであり、絶対的な星数ではない。",
        "",
        "## 結果",
        "",
        "| 方式 | stack [s] | 全工程 [s] | 背景σrobust/std [ADU] | 彗星中心 [ADU] | 開口信号 [ADU] | 開口S/Nrobust | 軌跡超過 [ADU] | mean比除去 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result_rows:
        lines.append(
            f"| {row['method']} | {float(row['stack_seconds']):.2f} | {float(row['pipeline_seconds']):.2f} | "
            f"{float(row['background_highpass_robust_sigma_adu']):.3f}/{float(row['background_highpass_standard_sigma_adu']):.3f} | "
            f"{float(row['comet_center_excess_adu']):.3f} | {float(row['comet_aperture_signal_adu']):.3f} | "
            f"{float(row['comet_aperture_snr_robust']):.2f} | "
            f"{float(row['track_excess_over_control_adu']):.3f} | {float(row['track_removal_vs_mean_percent']):.1f}% |"
        )
    lines.extend([
        "",
        "## スタック時間の内訳",
        "",
        "`fits_read` から `metcalf_shift` まではworker CPU時間の合計、order-statistic系は行タイル最終化の経過時間です。したがって列を単純合計して `stack [s]` を再計算するものではありません。",
        "",
        "| 方式 | FITS read | 背景fit | 背景apply | star resample | Metcalf shift | order combine | sort | outer reject | inner Winsor | final mean |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for method, summary in summaries.items():
        timing = summary["stack"]["stack_timing_seconds"]
        lines.append(
            f"| {method} | {float(timing['fits_read']):.2f} | {float(timing['background_fit']):.2f} | "
            f"{float(timing['background_apply']):.2f} | {float(timing['star_resample']):.2f} | "
            f"{float(timing['metcalf_shift']):.2f} | {float(timing['order_statistic_combine']):.2f} | "
            f"{float(timing['order_statistic_sort']):.2f} | {float(timing['order_statistic_outer_rejection']):.2f} | "
            f"{float(timing['order_statistic_inner_winsorization']):.2f} | "
            f"{float(timing['order_statistic_final_mean']):.2f} |"
        )
    lines.extend([
        "",
        "## 解釈上の注意",
        "",
        "- 彗星中心値と開口信号は、同じ出力画像内の背景環を引いた相対値であり、絶対測光値ではない。",
        "- S/Nは上記の共通背景ROIのロバストσを使った近似値。通常σ基準のS/NはCSVの `comet_aperture_snr_standard` に含めた。補間相関やクリップによる非ガウス性は含めていない。",
        "- 星軌跡指標は既存の開発用診断と同じ考え方だが、今回のproduction出力には中間フレームが残っていないため、最終画像の残差から評価した。",
        "- 各方式で位置合わせ、背景補正、採用フレームは同じ。方式間の差は主に最終画素合成で生じる。",
        "",
        f"詳細な数値: `{csv_path}`",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {csv_path}")
    print(f"Wrote {report_path}")
    print(f"Track mask: {len(positions)} stars, sign={sign}, pixels={int(np.count_nonzero(track))}")
    for row in result_rows:
        print(
            f"{row['method']:18s} stack={float(row['stack_seconds']):8.2f}s "
            f"pipeline={float(row['pipeline_seconds']):8.2f}s "
            f"bg_sigma={float(row['background_highpass_robust_sigma_adu']):8.3f} "
            f"track_excess={float(row['track_excess_over_control_adu']):8.3f} "
            f"snr={float(row['comet_aperture_snr_robust']):7.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
