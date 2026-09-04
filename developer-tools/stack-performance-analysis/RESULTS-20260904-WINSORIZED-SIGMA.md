# Winsorized Sigma 内側計算の最適化評価: 2026-09-04

## 結論

prefix sum / prefix squared-sum と列ごとの境界二分探索を実装し、既存のWinsorized Sigmaの数値意味を維持できることを確認した。ただし、247枚の実データでは旧実装より速くならなかった。現時点では「数値互換性を確認した実験実装」であり、速度改善としてリリースする判断は保留する。

## 変更内容

productionの呼び出し経路は、`build_order_statistic_tile()`から`finalize_order_statistic_cube()`を経て`_finalize_robust_clip_cube()`へ到達する。今回変更したのは最後のWinsorized Sigma計算部分だけで、registration、Metcalf shift、worker数、行タイルの構造は変更していない。

各outer passで、現在の有効区間`[lo, hi)`と固定された中央値`centre`に対して次を行う。

1. 元サンプルから`deviation = value - centre`を作る。
2. deviationとdeviation²のprefix sumを、既存の画素チャンク内だけに作る。
3. 累積Winsorizationを`effective_radius = min(previous_radius, 1.5 * sigma)`で表す。
4. ソート済み列の各境界をベクトル化した二分探索で求める。
5. prefix sumからWinsorized varianceを計算し、`1.134 * sqrt(variance)`を得る。
6. rejectionと最終meanにはWinsorized値ではなく元のソート済みサンプルを使う。

追加した計測値は`inner_prefix_setup_seconds`、`inner_boundary_search_seconds`、`inner_sigma_seconds`である。全画面サイズの追加cubeは作らず、既存のワークスペース制限内で処理する。

## 正しさ

- 全テスト221件が成功。
- Winsorized専用テスト48件が成功。
- サンプル数3〜247、正負外れ値、重複値、欠損を含む乱数1,200ケースでスカラー基準実装と最終出力値を比較し、最大差`0 ADU`、平均差`0 ADU`だった。
- 内側反復の上限到達は実データのstar/metcalfとも`0`列だった。

なお、現在のAPIは最終`[lo, hi)`を返さないため、乱数1,200ケースで個別境界そのものを保存して比較するテストはまだ追加していない。reject後の最終値と既存回帰テストで確認しているが、次の段階ではscalar oracleから境界・outer pass数も返すテストが望ましい。

## 247枚実データの実測

対象は`220PMcNaught` session 2、247枚、`winsorized-sigma`、`stack-workers=auto`。時間はstack部分のwall timeである。autoのavailable RAMにより行タイル数は同じ5でも行数が変動した。

| 実装 | combine | inner Winsor | sort | outer reject | final mean | stack全体 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 旧版 | 413.487 s | 254.323 s | 39.730 s | 48.475 s | 22.892 s | 677.774 s |
| prefix初版 | 554.358 s | 377.690 s | 29.364 s | 47.628 s | 25.190 s | 833.013 s |
| コピー削減版 | 504.006 s | 326.821 s | 36.654 s | 45.434 s | 23.045 s | 758.624 s |
| 上下境界探索統合版 | 482.817 s | 316.575 s | 28.639 s | 45.490 s | 23.136 s | 740.208 s |

最終版の内訳は、starとMetcalfの合計でprefix構築`135.721 s`、境界探索`98.053 s`、sigma計算`13.129 s`だった。旧版の単純な`clip`と`std`はNumPy内部で効率よく実行されるため、247サンプル程度では、prefix構築と約8回の列境界探索のコストが上回ったと考えられる。

RSSは旧版約`3.49 GiB`、最終版約`3.28 GiB`で、今回の実装による増加は確認されなかった。チェックポイントで観測した一時ディスク使用量は双方約`7.64 GiB`であり、前処理・registrationの一時ファイルが支配的である。

### 再測定

同じ条件で、他のテストを同時に実行していない状態でもう一度測定した。空きRAMが約`4.40 GiB`だったため、`auto`は352行・6タイルを選択した。

| 実装 | 行タイル | combine | inner Winsor | stack全体 |
| --- | ---: | ---: | ---: | ---: |
| 上下境界探索統合版（再測定） | 352行・6タイル | 516.540 s | 335.633 s | 835.726 s |

prefix構築は`140.123 s`、境界探索は`106.378 s`、sigma計算は`14.136 s`だった。star/metcalfのinner pass数は前回と同じ`18,686,231`／`19,030,240`で、アルゴリズムの反復回数が増えたわけではない。前回の432行・5タイルよりタイル処理の回数が増えたため、今回の再測定値は実装差の比較には使わず、`auto`のメモリ判定による変動例として扱う。

## 次の候補

1. サンプル数に応じたhybrid方式を測定する。247枚程度は従来の`clip + std`、さらに大きいNだけprefix方式にする。
2. prefix方式について、初期sigmaの通常`std`もprefixから求める場合を単独測定する。
3. 列ごとの二分探索と、1回の全行比較による境界取得を比較する。
4. scalar oracleが`[lo, hi)`、outer pass数、reject数を返すようにして、1,000ケース以上で完全な意味比較を追加する。

これらを行うまでは、今回の実装を速度改善として既定化・リリースしない。
