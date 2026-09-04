# 変更履歴

この文書は、利用者に影響する変更を記録します。開発上の判断、既知の制約、引き継ぎ情報は[DEVELOPMENT.md](DEVELOPMENT.md)を参照してください。

形式は[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)を参考にしています。

## Unreleased

## v0.9.7 - 2026-09-04

### 追加

- `--output-region reference|union|N|M%`を追加しました。既定の`reference`は従来の基準フレーム範囲、`union`は採用フレームの全範囲、整数`N`は`N`枚以上、`M%`は採用フレームの`M%`以上が重なる領域の外接矩形を出力します。
- 登録座標系と出力canvasの原点・shapeを分離したまま、平均、メジアン、ランクフィット、valid mask、飽和警告、WCS再基準化へ拡張領域を適用します。選択内容はFITSヘッダーとsummary JSONへ記録します。
- `--stack-method sigma-clip`を追加しました。`--clip-low`/`--clip-high`はMAD-clipとWinsorized Sigmaを含む3方式で共通です。単純σクリップは通常の平均と標本標準偏差で範囲外を除外し、安定するまで繰り返して残りを平均する、手軽な外れ値除去方式です。
- `--stack-method mad-clip`と`--stack-method winsorized-sigma`を追加しました。`--clip-low`/`--clip-high`は上下を個別指定でき、既定値はともに3です。前者はmedian/MADで1回範囲外を除外し、後者はSiril互換の内部Winsorizationでsigmaを収束させた後、元サンプルを安定するまで除外して平均します。
- Winsorized Sigmaの内部sigma収束判定を相対変化1%（`0.01`）へ変更しました。多数フレームの処理時間を抑えつつ、最終判定は元サンプルに対するrejectと平均を維持します。

### 既知の制限

- SharpCap StackLogの位置合わせ経路は、前処理済み画像が既に基準範囲へ切り出されているため、拡張出力はまだ利用できません。

### 検証

- WCSを除去した220P/McNaughtの実FITSで、同梱SirilとAstrometry.netの実通信プレートソルブを検証しました。Sirilは1.54秒、Astrometry.netは37.47秒で完了し、双方が実WCS FITSを生成しました。

## v0.9.6 - 2026-09-02

### 改善

- median/rankfitを全幅の行タイル単位で処理するようにし、全採用フレーム×全画面の巨大な一時キューブを不要にしました。背景近似とフレーム評価を先に確定し、各タイルだけをRAM上で結合します。
- median/rankfitのproduction経路ではcubeをディスクへ書き出さず、RAM上の行タイルだけで処理します。
- `--median-tile-rows auto|N`を追加しました。`N`は分割数ではなく、1回に処理する縦方向のピクセル行数です。既定の`auto`は星固定・Metcalf固定の作業キューブ合計をavailable RAMのおよそ半分以下に収め、選択結果を画面とsummary JSONへ記録します。確保失敗時は未確定タイルを破棄し、行数を半分にして再試行します。

## v0.9.5 - 2026-08-31

### 追加

- Windows向けの`seestar-open-storage.cmd`を追加しました。`seestar.local`のIPv4、APモードの既定アドレス、ローカルネットワーク探索の順にSeestarを見つけ、ネットワークファイル共有の`MyWorks`をExplorerで開きます。
- `net view`に依存せず、TCP 445と`\\<IPv4>\EMMC Images\MyWorks`への直接接続を確認します。失敗時はWindowsの未認証guestログオン、SMB署名必須、SMB暗号化必須を診断し、必要な対処とセキュリティ上の影響を表示します。Seestarアプリに未確認の共有ON/OFF設定があるとは案内しません。
- 診断を読めるよう、失敗時は任意のキーが押されるまでランチャーを閉じません。
- READMEへ、ローカル グループ ポリシーで設定を探すヒント、管理者PowerShellの設定例、PC全体へ影響するセキュリティ上の注意を追加しました。

## v0.9.4 - 2026-08-29

### 追加

- `seestar-fixed-stack.cmd`を追加しました。変光星などのサブフレームを背景星基準だけで位置合わせし、HorizonsやMetcalf移動を使わず`*_fixed_stack.fit`を生成します。
- 固定モードは平均、float32、飽和警告ON、二次背景補正を既定とし、既存のコマンドラインオプションで個別に上書きできます。
- 最終FITSへ`CREATOR`、`SWVER`、`PLTSOLVR`、`TIMESYS`、`DATE-BEG`、`DATE-AVG`、`MJD-AVG`、`DATE-END`、`TELAPSE`、`TOTEXP`、`NCOMBINE`を追加し、`HISTORY`でMetcalf、星固定、比較、fixed stackを区別します。
- Siril WCSに含まれるSIP歪み補正の次数と`A/B/AP/BP`全係数を、3次に固定せず最終FITSへ継承します。

### 修正

- 空白を含む基準フレーム名をSirilスクリプト内で引用し、CFA前処理とローカルプレートソルブが`file not found`になる問題を修正しました。この問題はSirilローカルソルブを導入したv0.7.1から潜在していました。
- Astrometry.netのWCS FITSをAPI JSON用URLではなく公式の`/wcs_file/{job_id}`から取得し、JSON解だけでなく完全なWCS FITSも保存できるようにしました。

## v0.9.3 - 2026-08-24

### 改善

- Sirilの背景星登録を`register -2pass`へ変更し、位置合わせ行列だけを取得するようにしました。通常のSiril登録経路では登録済みFITSを生成しません。
- 背景星登録行列とフレームごとのMetcalf移動量を合成し、元の前処理済み画像から移動天体固定像を1回のbilinear resamplingで作るようにしました。星固定像も同じ登録行列から1回だけresamplingします。
- 220P/McNaughtの242枚実測で、登録FITSは242枚から0枚、一時領域の計測ピークは約13.23 GiBから7.48 GiBへ43.5%減少しました。温キャッシュのend-to-endはv0.9.0の182.81秒から176.11秒へ3.7%短縮しました。
- summary JSONへsource staging、Siril前処理、登録、スタック、出力、全pipelineのwall time、Python process peak RSS、登録ディレクトリの一時容量checkpointを追加しました。

### 内部設計

- Sirilの下原点座標行列をNumPy/FITS配列座標へ変換し、`-2pass`が自動選択した基準から利用者指定基準へ行列を再基準化します。出力canvasのshape/originは登録座標系から独立しており、将来のexpanded canvasを妨げません。
- 旧版のSiril既定Lanczos-4登録とPython bilinear shiftの2回補間から、1回のbilinear補間へ変わるため、旧版とは画素単位で完全一致しません。同一入力星の開口測光では新経路のRGB平均偏差が`-0.22%～+0.05%`、旧Lanczos-4は`+0.45%～+1.07%`で、新経路のほうが入力光量をよく保存しました。242枚の有効footprintは旧版と一致し、6画素半径の移動天体開口のRGB差は`-0.28%～+0.37%`でした。
- 行列座標、正負・整数・小数shift、mono/RGB、画像端、valid/saturation mask、任意canvas shape/originを含む全170テストが成功しました。

## v0.9.1 - 2026-08-24

### 修正

- JPL Horizonsが一部の時点で`DES=220P;CAP;NOFRAG`などの`CAP`/`NOFRAG`付き彗星検索を構文エラーとして返す問題に対応しました。自動検索時は従来の修飾子付き正式符号を最初に試し、Horizonsが構文として拒否した場合は`DES=220P;`のような修飾子なし正式符号へ自動フォールバックします。SBDBから得た候補にも同じフォールバックを追加しました。

## v0.9.0 - 2026-08-24

### 改善

- Metcalf pure translationをslice-based bilinear処理へ変更し、背景面適用、星固定加算、平均加算も不要なfull-frame一時配列を避けるよう高速化しました。
- `--stack-workers auto|1|2|4`を追加しました。既定の`auto`はavailable RAMとサブフレーム・出力canvasの大きさを保守的に見積もり、最大4 workerから安全な並列数を選びます。明示したworker数は初期値として優先します。
- workerのメモリ確保に失敗した場合、全workerを停止し、未加算バッチのlocal resultを破棄して、同じバッチ全体を`4→2→1`で再実行します。加算は成功済みバッチだけを入力順にmain threadで行うため、部分結果が混ざらず、worker数によらず同じ画素値を得ます。
- 登録済みFITSの背景fit・適用・スタックを1回の読込へ統合しました。source copy・変換像・staged calibrationは前処理成功後、最終前処理画像は登録完了後、登録画像は寄与確定後に順次削除します。
- summary JSONと画面へ、FITS読込、背景fit・適用、星固定加算、Metcalf shift・加算、スタック全体の処理時間を出力します。
- 220Pの242採用フレームを2回測定し、auto=4でstack wall time `84.510/73.418秒`を確認しました。旧版と同じend-to-endベンチマークでは`quadratic`平均`181.470秒`となり、旧`736.298秒`に対して約4.06倍です。両長時間実行のMetcalf・星固定・比較float32 FITSはSHA-256まで一致しています。

### 内部設計

- registration座標系とstack output canvasを`StackCanvas`で分離しました。現行出力は従来どおりReference frame範囲ですが、将来の寄与枚数・割合に基づくexpanded canvasでresamplerを置き換えずに済む構造です。
- MemoryError時のworker停止・未確定result破棄・global accumulator非変更・同一バッチ再実行、および中間ファイル削除失敗をfailure injectionで検証しました。

## v0.8.2 - 2026-08-23

### 追加

- `--preview-sun-pa-left`で、JPL Horizonsから求めた太陽方向を左に置く移動天体固定プレビューを追加しました。通常、反太陽方向のダストテイルを右向きに表示できます。移動天体固定FITSには拡張ヘッダ`SUN_PA`（太陽の位置角）、`ASUN_PA`（反太陽方向）、`SUNRA`、`SUNDEC`、`SUNCENTR`、`SUNSRC`も記録します。
- `--preview-at UL|UR|LL|LR`と`--annotate-size`で、N/E方位マークと太陽方向矢印をプレビューへ描けるようにしました。既定は`--preview-at UL`です。注釈付きPNGに加え、任意配置用の小型透過`*_annotation_overlay.png`も出力します。不要な場合は`--preview-at none`を指定します。

## v0.8.1 - 2026-08-22

### 修正

- `--preview-north-up`で使用するWCSの配列Y軸規約とPillow回転角を修正しました。斜め向きのWCSでは従来のPNGが天の北からずれていました。科学用FITS、WCS、通常プレビューには影響しません。

## v0.8.0 - 2026-08-21

### 追加

- `--preview-north-up`で、プレートソルブ済みWCSを使い天の北を上にした移動天体固定・星固定・比較表示PNGを追加出力できるようにしました。科学用FITSと通常プレビューは変更しません。
- `--background-normalization plane|quadratic`を追加しました。登録済み各サブフレームの背景をRGBごとに一次平面または二次曲面として推定し、実データ領域だけから差し引きます。
- `--preview-stretch sigma`と`--preview-sigma-low`、`--preview-sigma-high`を追加しました。

### 変更

- 背景補正の既定を、50x50タイルのsigma-clipped medianから二次曲面を求める`quadratic`に変更しました。大きな彗星やDSOなど面モデルで守れない対象には`--background-normalization none`または`offset`を指定できます。
- 背景補正では各フレームの推定背景を演算中に0付近まで差し引き、最後に保存レンジ対策だけの一定オフセットを加える方式にしました。
- 表示用PNGの既定を、各RGBチャンネルの単純な平均・標準偏差による`-1σ`から`+3σ`の線形伸長へ変更しました。星を外れ値として除外しないため、背景ノイズだけを過大に表示しません。従来の方式は`--preview-stretch percentile`で使えます。

## v0.7.3 - 2026-08-18

### 改善

- Sirilが利用するVizieR星表サーバーからHTTP 503が返った場合、倍率を変える前に同じ条件を2秒・4秒待って最大3回試すようにしました。
- Astrometry.net、JPL Horizons、JPL SBDBの通信失敗も待機付きで再試行し、断念時にサービス名、試行回数、最後の原因を表示するようにしました。
- VizieRの503が継続し、Astrometry.net APIキーも未設定の場合は、APIキー取得ページと設定コマンドを表示して終了するようにしました。
- Windowsランチャーは異常終了時だけキー入力を待ち、エラーとログの場所を読める状態にします。正常終了時は出力フォルダを開いて自動終了します。

### 修正

- Horizons/Astrometry子プロセスの具体的な`ERROR:`を親プロセスでも保持し、汎用エラーへ置き換わらないようにしました。
- Plate Solveベンチマークの`--siril-cache-mode cold-each`が、存在しないSirilキャッシュディレクトリとWindowsの長すぎるパスにより即時失敗する問題を修正しました。短い一時ディレクトリに試行ごとの空キャッシュを作成します。

## v0.7.2 - 2026-08-17

### 変更

- 利用者向けRelease ZIPからベンチマーク、単体テスト、GitHub Actions、Release作業用ファイル、古い実験資料を除外し、実行・セットアップ・再構築に必要なファイルへ配布範囲を整理しました。
- Plate Solveベンチマーク一式をソースリポジトリの`developer-tools/plate-solve-benchmark/`へまとめ、出力も同フォルダの`results/`へ分離しました。
- パッケージ検証へ開発専用ファイルの禁止リストを追加し、今後誤ってZIPへ混入した場合はビルドを失敗させるようにしました。

## v0.7.1 - 2026-08-17

### 追加

- SharpCapの`*.CameraSettings.txt`からmaster dark、master flat、ホットピクセル、クールピクセルの設定を読み、Sirilで補正してからデベイヤする処理を追加しました。
- `Hot Pixel Sensitivity`が0以外ならSirilのホットピクセル補正を既定sigma 3で有効にします。SharpCap固有の数値はSirilへ直接換算しません。
- `--preprocessing`、dark/flatファイルと有効・無効指定、hot/cold pixelの有効・無効とsigma指定を追加しました。コマンド指定はCameraSettingsより優先します。
- 基準フレームをSirilで先にPlate Solveし、失敗した場合だけAstrometry.netへフォールバックする`--plate-solver auto|siril|astrometry`を追加しました。
- Siril WCSを基準フレーム名の`*_siril_wcs.fits`へ保存し、再実行時に再利用します。

### 変更

- SharpCap StackLogのX/Y offsetとrotationが完全な場合も、補正とデベイヤはSirilへ統一しました。StackLogは背景星位置合わせの変換だけを置き換えます。
- Astrometry.net APIキーは通常のSeestar処理の必須要件ではなく、SirilでPlate Solveできない場合の任意フォールバックになりました。

### 修正

- `stacklog.csv`がない通常のSharpCap FITS撮影フォルダでも`*.CameraSettings.txt`を検出し、記録された補正設定を基準フレームと全スタックフレームへ適用します。
- Sirilのhot/cold pixel補正で無効側にsigma 0を渡して通常画素を大量補正していた問題を修正しました。
- 入力フォルダへ保存した`*_siril_wcs.fits`などのPlate Solveキャッシュをサブフレームとして再読込しないようにしました。

## v0.7.0 - 2026-08-14

### 追加

- SharpCap 4.1.10745以降のLive Stackセッションを`stacklog.csv`から自動認識し、`Raw frame file`でraw frameを対応付けられるようにしました。
- 既定ではSharpCapがスタック成功と記録したフレームだけを採用し、StackLogの時刻、検出星数、FWHM、X/Y offset、rotationを利用します。
- SharpCapの背景星位置合わせ情報が全採用フレームにそろっている場合、Pythonで登録してSirilを自動的に省略します。
- SharpCap RAW PNG/TIFF用に`--bayer-pattern RGGB|BGGR|GRBG|GBRG`を追加しました。
- `stacklog.csv`をraw frameフォルダ内または1つ上から検出し、コピー後にCSVの旧絶対パスが壊れていても、ドロップしたフォルダ内の同名画像を優先して対応付けるようにしました。
- `stacklog.csv`自体を入力パスとして受け取り、その親フォルダをコピー済みセッションの処理対象として扱えるようにしました。
- SharpCap PNG/TIFF入力では、対象天体または座標CSVと`--pixel-scale-arcsec`の明示を必須にしました。観測地は省略可能で、地心座標へフォールバックします。

### 安全性

- SharpCapVersionをCameraSettingsから確認し、既知の古いStackLog時刻問題を避けるため4.1.10745未満またはバージョン不明のセッションを停止します。
- SharpCap FITSの`OBSLONG`と`OBSLAT`もAstrometry.netアップロード用コピーから除去します。
- コピー先に同名raw frameが複数ある場合は、誤った画像を選ばず曖昧性エラーで停止します。CSVに記録された元の絶対パスは、コピー先の同名画像より後に評価します。

## v0.6.1 - 2026-08-08

### 修正

- 配布パッケージへCA証明書セットを同梱し、同梱Pythonが利用可能な証明書ストアを見つけられないWindows環境でもJPL HorizonsとAstrometry.netへHTTPS接続できるようにしました。

## v0.6.0 - 2026-08-05

### 追加

- `.fit`と`.fits`の両方を標準入力として扱い、SharpCapのFITSを処理できるようにしました。
- `--site-longitude`、`--site-latitude`で東経・北緯を指定でき、FITSヘッダーより優先できるようにしました。観測地情報がない場合はgeocenterへフォールバックします。
- `--pixel-scale-arcsec`で、FITSに画素スケールがない場合の秒角/画素を指定できるようにしました。

### 修正

- Astrometry.netのWCS取得応答がHTMLなどの不正データだった場合に、FITSとして保存せず、正常なJSONキャリブレーションへフォールバックするようにしました。
- SharpCapなどのフォルダ内にある`.fits.invalid`のような退避ファイルを入力対象から除外するようにしました。

## v0.5.5 - 2026-08-04

### 修正

- 背景星登録が失敗した場合、デベイヤ済みの各フレームをSirilで個別に再解析し、`registration_diagnostics.csv`へ検出星数、FWHM、roundnessを記録するようにしました。登録できない基準フレームでも、CSVを見て`--reference-frame-file`の候補を選べます。

## v0.5.4 - 2026-08-02

### 改善

- 平均スタックは、位置合わせ・移動天体シフト後に実データが存在する画素だけを加算し、画素ごとの整数採用枚数で正規化する方式を既定にしました。画像外の0 paddingで周辺が暗くなる問題を防ぎます。
- サブピクセル移動では4近傍すべてが有効な場合だけ補間し、画像外の0との補間や外挿を行いません。従来結果との比較には`--padding-policy legacy`を使用できます。
- median/rankfitの0サンプル除外を`--zero-sample-policy exclude`として明示し、非推奨の従来動作は`include`で再現できます。
- `*_registration_diagnostics.csv`を追加しました。全フレームのFWHM、weighted FWHM、roundness、検出星数、対応星数、inlier率、X/Y移動量、回転角、倍率、採否と理由を一覧できます。
- 基準不良などでスタック前に停止しても、作業フォルダへ`registration_diagnostics.csv`を残します。

### 修正

- Siril `.seq`の検出星数を対応星数として表示していた項目を修正し、Sirilログから初期対応数とフィッティング後対応数を別々に取得します。
- スタック失敗時にPython Tracebackをコンソールへ二重表示せず、原因、基準フレーム、診断CSV、復旧操作を示す短いエラーメッセージを表示します。詳細Tracebackは実行ログに残します。

## v0.5.3 - 2026-07-31

### 修正

- WindowsでSirilを`.cmd`または`.bat`経由で起動する際、空白を含む展開先・作業先・スクリプトパスが`cmd.exe`で分割される不具合を修正しました。
- Siril同梱版では、パッケージ内の`tools/.../siril-cli.exe`を優先して直接起動します。空白を含むパスでのバッチ解釈を避けます。
- Windowsの長すぎるパスによる出力失敗を、トラブルシュートに記載しました。

## v0.5.2 - 2026-07-31

### 変更

- 任意の基準フレームは、実行前に番号を確定できない`--reference-frame-index`ではなく、`--reference-frame-file`にFITSファイル名を指定して選べるようにしました。
- 基準フレームが`--registration-minpairs`個以上の背景星対を得られない場合は、検出数と必要数を表示して停止します。
- 雲、障害物、薄明などで位置合わせできない**基準以外**のフレームは、処理全体を失敗にせず除外して残りをスタックします。`*_shifts.csv`の`used`、`reason`、`star_pairs`で確認できます。
- 完了時に`Stacked 使用枚数/対象枚数; skipped 除外枚数`を表示します。

### 文書

- [改訂内容とトラブルシュート](TROUBLESHOOTING.md)を追加しました。基準フレームの選び方、登録失敗の確認、空白を含むパス、Siril、Astrometry.net、Horizonsの復旧方法をまとめています。

## v0.5.1 - 2026-07-25

### 追加

- `--saturation-warning enable`で、いずれかのサブフレームが指定した飽和閾値を超えた画素を、プレビューPNGだけに警告色で重ねて出力します。
- `--saturation-threshold-percent`と`--saturation-color`で閾値と色を変更できます。

### 注意

- 警告は各RGBチャンネルのいずれかが閾値を超えた画素を対象にします。線形FITSの画素値そのものは変更しません。

## v0.5.0 - 2026-07-21

### 追加

- macOSのPythonソース実行、shellランチャー、Finder用ドラッグ&ドロップアプリを追加しました。
- Windows/macOSの単体テストをGitHub Actionsで実行するようにしました。

## v0.4.0 - 2026-07-14

### 変更

- Astrometry.net処理をPythonへ統合し、Node.js依存を削除しました。
- Windows EXEとSiril同梱・非同梱の配布ZIPを追加しました。
