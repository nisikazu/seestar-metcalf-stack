# 単純σクリップ実測結果（2026-09-04）

## 目的

Winsorized Sigmaより手軽に使える外れ値除去方式として追加した
`--stack-method sigma-clip`の速度を、既存の
`--stack-method winsorized-sigma`と同じ実データで比較した。

単純σクリップは、各画素の現在のサンプル集合について通常の算術平均と
標本標準偏差（`ddof=1`）を求め、`--clip-low`/`--clip-high`で指定した範囲の
外側を除外する。除外がなくなるまで外側の判定を繰り返し、最後は元サンプル
の算術平均を返す。Winsorized Sigmaの内部Winsorizationは行わない。

## 測定条件

- 入力: `D:\downloads\220PMcNaught_sub`
- セッション: 2、247フレーム
- 天体: 220P/McNaught
- エフェメリスとWCS: 既存ファイルを固定して再利用。通信・ソルブ時間を除外
- 出力領域: `reference`
- 背景補正: `quadratic`
- `--clip-low 3 --clip-high 3`
- `--stack-workers 4`
- `--median-tile-rows 512`（全幅512ピクセル行、4タイル）
- 同じPython production codeで連続実行

各方式1回の実測であり、絶対時間はOSのファイルキャッシュ、空きRAM、他プロセス
の負荷で変動する。両方式は同じ実効worker数と行タイル数で実行しているため、ここ
では方式間の相対差と工程内訳を主に比較する。

実行時の出力先は次のとおり。

- `sigma-clip`: `metcalf_output/220PMcNaught-sigma-clip-20260904-20260904-110804`
- `winsorized-sigma`: `metcalf_output/220PMcNaught-winsorized-sigma-20260904-fair-20260904-111818`

## 結果

| 方式 | スタック部 | 全工程 | order-statistic combine | 247枚の採用 |
| --- | ---: | ---: | ---: | ---: |
| `sigma-clip` | 525.93秒 | 583.79秒 | 301.20秒 | 247/247 |
| `winsorized-sigma` | 817.18秒 | 867.65秒 | 571.98秒 | 247/247 |
| 差（Winsorized - simple） | 291.25秒 | 283.87秒 | 270.77秒 | - |
| 短縮率 | 35.6% | 32.7% | 47.3% | - |

全工程の「全工程」は入力準備、Siril前処理、登録、スタック、出力を含む。
エフェメリス取得とプレートソルブは固定入力を再利用したため含まない。

### スタック部の内訳

| 工程 | `sigma-clip` | `winsorized-sigma` | 差の解釈 |
| --- | ---: | ---: | --- |
| FITS読込 | 298.75秒 | 319.21秒 | 行タイル再読込とOSキャッシュの影響を含む |
| 背景fit | 59.65秒 | 72.36秒 | 実行時負荷のばらつきが支配的 |
| 背景apply | 157.40秒 | 165.22秒 | ほぼ同じ経路 |
| 星resample | 133.98秒 | 151.25秒 | 実行時負荷のばらつきがある |
| Metcalf shift | 135.90秒 | 151.63秒 | 実行時負荷のばらつきがある |
| order-statistic combine | 301.20秒 | 571.98秒 | 主な差が発生する工程 |
| sort | 68.26秒 | 68.53秒 | ほぼ同じ。一度だけin-place sort |
| 内部Winsorization | 0.00秒 | 349.32秒 | 単純σ方式には存在しない |
| 外側rejection | 47.08秒 | 47.16秒 | ほぼ同じ |
| 最終mean | 23.59秒 | 24.64秒 | ほぼ同じ |

工程内訳の各値は、実装上の計測定義に従う。FITS読込、背景補正、resampleなど
はworkerのCPU時間合計、`order-statistic combine`とその詳細値は行タイル最終化の
wall timeまたはその内訳であり、単純加算が全て一致するとは限らない。

## 除外動作

両方式とも247/247フレームを採用した。3 sigmaの同じ閾値でも、中心とスケールの
推定方法が違うため、除外結果は同一ではない。

| 出力 | 方式 | 外側判定の最大回数 | 除外サンプル数 | 除外が発生した画素列 |
| --- | --- | ---: | ---: | ---: |
| 星固定 | `sigma-clip` | 14 | 6,479,376 | 4,736,241 |
| Metcalf固定 | `sigma-clip` | 32 | 7,013,474 | 4,857,855 |
| 星固定 | `winsorized-sigma` | 15 | 7,366,523 | 4,553,346 |
| Metcalf固定 | `winsorized-sigma` | 15 | 8,159,113 | 4,615,080 |

単純σクリップは平均と標準偏差が外れ値に引っ張られるため、外れ値が多い画素
では除外が弱くなったり、反対に反復過程で別のサンプルを追加除外したりする
可能性がある。人工衛星、宇宙線、ホットピクセルを強く抑えたい場合は、速度と
引き換えに`mad-clip`または`winsorized-sigma`を選ぶべきである。

## メモリと実用上の判断

両実行ともプロセスpeak RSSは約3.74 GiB、チェックポイントで測った一時領域の
ピークは約7.64 GiBだった。単純σクリップはWinsorized作業配列と内部反復を持た
ないが、画像cubeそのもの、行タイル分割、自動worker選択は同じである。そのため
メモリ上限の決め方は既存のmedian/MAD/Winsorizedと変わらない。

今回の247枚実測では、単純σクリップはスタック部を約36%、全工程を約33%短縮した。
差の大部分は内部Winsorizationの省略による。したがって、まず短時間で外れ値除去
の効果を試す用途には`sigma-clip`を推奨し、外れ値への頑健性を優先する用途には
`mad-clip`または`winsorized-sigma`を推奨する。

## 再現ログ

- `sigma-clip`のログ: `metcalf_output/metcalf-20260904-110803.log`
- `sigma-clip`のsummary: `metcalf_output/220PMcNaught-sigma-clip-20260904-20260904-110804/moving_target_pipeline_summary.json`
- `winsorized-sigma`のログ: `metcalf_output/metcalf-20260904-111817.log`
- `winsorized-sigma`のsummary: `metcalf_output/220PMcNaught-winsorized-sigma-20260904-fair-20260904-111818/moving_target_pipeline_summary.json`
