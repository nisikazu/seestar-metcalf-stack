# プレートソルブ・ベンチマーク

同じFITS画像をAstrometry.netとSirilで繰り返しプレートソルブし、与えた画角の正確さが処理時間と成功率へ与える影響を比較する開発・検証用ツールです。メトカーフスタック本体の実行には必要ありません。

このフォルダはGitHubのソースリポジトリだけに置き、利用者向けRelease ZIPには同梱しません。以下のコマンドはリポジトリの`developer-tools\plate-solve-benchmark`をカレントディレクトリとして実行します。

## 測定条件

正しいピクセル画角を基準として、次の6条件を各10回、合計60回実行します。

| ソルバー | 与えるピクセル画角 |
|---|---:|
| Astrometry.net | 正しい値の0.5倍、1倍、2倍 |
| Siril | 正しい値の0.5倍、1倍、2倍 |

実行順によるネットワーク負荷やキャッシュの偏りを減らすため、各反復内の6条件は固定seedでランダム化します。失敗とタイムアウトを成功時間へ混ぜず、条件ごとに次を出力します。

- 成功、失敗、タイムアウトの回数
- 成功試行の平均、標本標準偏差、中央値、最小、最大時間
- 失敗とタイムアウトを含む全試行の平均時間

Astrometry.netには、各条件で提示したピクセル画角を中心として、既定で`提示値÷2.2～提示値×2.2`を探索範囲として送ります。0.5倍、1倍、2倍のすべてで正解画角を範囲内に含め、誤った推定値から解決するまでの実行時間を比較します。倍率は`--astrometry-scale-range-factor`で変更できます。

SirilにはFITSヘッダの概略中心座標を使わせ、ピクセル画角そのものではなく、固定した実効ピクセルピッチと次式で換算した焦点距離を渡します。

```text
焦点距離 [mm] = 206.265 * 実効ピクセルピッチ [um] / ピクセル画角 [arcsec/pixel]
```

FITSにピクセルピッチがなければ1 umを仮定します。Sirilが画角を得る際にはピクセルピッチと焦点距離の比だけが必要なので、指定したピクセル画角は変わりません。

## 実行前の確認

このベンチマークは既定でAstrometry.netへ同じ画像を30回アップロードします。観測地のFITSカードを除いたコピーを送りますが、画像データ、概略中心座標、画角ヒントは外部サービスへ送信されます。`--confirm-astrometry-uploads`は、この回数と送信内容を確認したことを明示するオプションです。

Sirilも選択した星カタログを取得するためにネットワークを使う場合があります。Astrometry.netの時間にはアップロード、サーバー待ち、解析、結果取得を含み、Sirilの時間にはプロセス起動、FITS読込、カタログ照合、WCS保存を含みます。したがって、これはアルゴリズム単体ではなく、実際の利用に近いエンドツーエンド時間の比較です。

最初に外部通信を行わないドライランを推奨します。

```powershell
.\run-benchmark.cmd "C:\path\to\frame.fit" `
  --pixel-scale-arcsec 3.99 `
  --effective-pixel-size-um 2.9 `
  --dry-run
```

表示される中心座標、正しい画角、60試行の順番を確認した後に本測定します。

```powershell
.\run-benchmark.cmd "C:\path\to\frame.fit" `
  --pixel-scale-arcsec 3.99 `
  --effective-pixel-size-um 2.9 `
  --astrometry-key-file "C:\path\to\.astrometry_api_key" `
  --confirm-astrometry-uploads
```

正しいピクセル画角、実効ピクセルピッチ、概略中心座標は可能な限りFITSから推定します。推定できない項目や、意図的に固定したい項目は明示できます。

```powershell
.\run-benchmark.cmd "C:\path\to\frame.fits" `
  --pixel-scale-arcsec 0.959 `
  --effective-pixel-size-um 2.9 `
  --ra-deg 320.1234 `
  --dec-deg -12.3456 `
  --siril "C:\Program Files\Siril\bin\siril-cli.exe" `
  --confirm-astrometry-uploads
```

## 主なオプション

| オプション | 意味 |
|---|---|
| `--pixel-scale-arcsec` | 正解として扱うピクセル画角 [arcsec/pixel] |
| `--effective-pixel-size-um` | Sirilの焦点距離換算に使う実効ピクセルピッチ [um] |
| `--ra-deg`, `--dec-deg` | Astrometry.netへ渡す概略中心 [deg, J2000]。SirilはFITSヘッダの中心を使用 |
| `--repeats` | 各ソルバー・画角条件の反復数。既定10 |
| `--timeout-seconds` | 1試行の制限時間。既定300秒 |
| `--seed` | 条件順をランダム化するseed |
| `--astrometry-scale-range-factor` | Astrometry.netの探索範囲倍率。既定2.2 |
| `--solver` | `both`、`astrometry`、`siril`のいずれか |
| `--scale-case` | `all`、`half`、`correct`、`double`。単一条件の再測定に使用 |
| `--siril-catalog` | Sirilへ明示するカタログ |
| `--siril-cache-mode` | `reuse`は通常キャッシュを自動再利用（既定）。`cold-each`は各試行を空の隔離キャッシュで開始 |
| `--output-dir` | 結果保存先 |
| `--dry-run` | 推定値と試行順だけ表示 |

## 出力

既定ではこのフォルダ内の`results\FITS名-実行時刻`へ保存します。

- `benchmark_config.json`: 入力条件と実際の試行順
- `benchmark_runs.csv`: 全試行の時間、成否、解の中心・画角、ログへのパス
- `benchmark_summary.csv`: 条件別の統計値
- `benchmark_comparison.csv`: 同じ画角条件における両ソルバーの成功平均時間と比率
- `benchmark_summary.md`: 人が読みやすい集計表
- `NNN_solver_scale_repeat\solver.log`: 各試行の生ログ
- `NNN_solver_scale_repeat\*.fit`または`*.json`: 各ソルバーの解

測定中も`benchmark_runs.csv`は1試行ごとに更新されるため、中断した場合でも完了済みの結果を確認できます。
各ソルバーの応答待ち中は15秒ごとに経過時間と`solver.log`の場所を表示します。

## Sirilの画角許容範囲を調べる

Siril内蔵ソルバーへ渡すピクセル画角を段階的に変え、どこまで解けるかを調べる専用テストです。

```powershell
.\run-siril-scale-tolerance.cmd "C:\path\to\frame.fits"
```

既定では正しい画角の0.50～2.00倍を18点で粗く調べます。任意の倍率と反復数も指定できます。

```powershell
.\run-siril-scale-tolerance.cmd "C:\path\to\frame.fits" `
  --factors "0.80,0.82,0.84,0.90,1.00,1.10,1.20,1.25,1.26,1.30" `
  --repeats 3
```

- `siril_scale_tolerance.csv`: 倍率ごとの成否、時間、解座標、実測ピクセル画角
- `siril_scale_tolerance.md`: 1.0倍を含む連続成功範囲の要約
- `NNN_siril_factor-*\solver.log`: 各倍率のSiril生ログ

許容範囲はSirilの版だけでなく、画像の星数、中心座標の誤差、選択された星表にも依存します。異なる機材や画像でも同じ境界になるとは限りません。

## 結果を読む際の注意

- 同じFITSの反復なので、OS、Siril、外部サービスのキャッシュが後半を速くする可能性があります。乱順化は条件間の偏りを減らしますが、影響を完全には除去しません。
- Astrometry.net側の待ち行列や回線状態は測定ごとに変化します。別日時の差より、同じ測定内の条件差を優先して解釈してください。
- Sirilが最初にカタログを取得する実行だけ遅くなる場合があります。通常は既存キャッシュを消去せず再利用します。毎回の初回取得性能を測る場合は`--siril-cache-mode cold-each`を指定します。隔離キャッシュを使うため、普段のSirilキャッシュは変更しません。
- この比較のSirilはSiril内蔵プレートソルバーです。SirilからローカルAstrometry.netを呼ぶ`-localasnet`は比較対象ではありません。
