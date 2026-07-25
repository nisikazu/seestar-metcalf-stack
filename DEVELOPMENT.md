# Seestar Metcalf Stack 開発・引き継ぎノート

最終更新: 2026-07-25

この文書は、Seestar Metcalf Stackを改造する人、保守する人、または開発を
引き継ぐ人のための技術記録です。一般利用者向けの操作方法は`README.md`、
macOSの導入方法は`README-macOS.md`、リリース作業は`PUBLISHING.md`を参照して
ください。

## 現在の状態

- 最新の公開Releaseは`v0.5.1`です。
- `v0.5.1`にはHorizons復旧手順、座標CSVの補間・外挿説明、
  Astrometry.net APIキー取得手順の改善、飽和警告、開発文書の配布同梱が
  含まれます。
- 2026-07-25時点でGitHubに登録された未解決Issueはありません。ただし、
  後述する実装上の制約と未検証事項は残っています。
- この公開リポジトリの対象は、Seestarサブフレームを処理するメトカーフ
  スタック機能だけです。Seestar本体制御、通信プロトコル調査、PEM、観測データは
  別のローカル作業領域に置き、ここへ混ぜないでください。

## 設計方針

### Python CLIを正本にする

処理本体と利用者向けの進行表示、ログ、エラー処理、出力フォルダを開く処理は
Python CLIへ集約します。

- `seestar-metcalf-stack.cmd`: Windows用の薄いランチャー
- `seestar-metcalf-stack.sh`: macOS用の薄いランチャー
- `Seestar Metcalf Stack.app`: Finderからフォルダを渡すための薄いdroplet
- `scripts/moving_target_pipeline.py`: 公開CLIと処理全体のオーケストレーター

Windowsランチャーは同じフォルダにEXEがあればEXEを優先し、なければ`.venv`、
最後にシステムPythonを使います。Pythonコードを変更したのに古いEXEを残すと、
変更前のEXEが実行されます。開発中はEXEを削除するか、必ず再ビルドしてください。

### 外部ツールとの役割分担

- Astrometry.net: 基準フレームの中心座標、画角、回転を確定する
- JPL Horizons: 各露光時刻の天体の赤経・赤緯を取得する
- Siril: デベイヤと背景星基準のフレーム間位置合わせを行う
- Python/NumPy: 天体移動量を画素移動へ変換し、最終的な画素結合とFITS出力を行う

Sirilは最終スタックを行いません。Sirilが生成した星位置合わせ済みフレームを
Pythonが読み、メトカーフスタックと星固定スタックを同じ画素結合方式で作ります。

## 処理の流れ

1. FITSの`DATE-OBS`を読み、既定では60分を超える空白でセッションを分割します。
2. 指定がなければ最新セッションを選びます。ファイル名に`_failed_`を含む
   フレームは既定で除外します。
3. FITSの`OBJECT`からHorizons検索候補を作り、各フレーム時刻のtopocentric座標を
   取得します。明示CSV、COMMAND、天体名の上書きもできます。
4. 基準フレームをAstrometry.netでプレートソルブします。基準は先頭または
   セッション時刻中間に最も近いフレームです。
5. SirilでCFA FITSをデベイヤし、背景星を基準に登録します。既定の変換は
   `similarity`で、平行移動、回転、等方的な倍率を推定します。
6. WCSと各時刻の天体座標から、基準フレームに対する天体の追加移動量を求めます。
7. 登録済みフレームを双線形補間でサブピクセル移動し、メトカーフ基準と
   星固定基準を同じ結合方式でスタックします。
8. 線形FITS、表示用PNG、フレーム別シフトCSV、処理要約JSON、ログを出力します。
9. 成功時は大きな中間FITSを削除し、成果物フォルダを開きます。

## 主要ファイル

| ファイル | 責務 |
| --- | --- |
| `scripts/moving_target_pipeline.py` | 引数、セッション選択、作業ディレクトリ、外部処理の順序、ログ、キャッシュ、EXE内部ディスパッチ |
| `scripts/horizons_ephemeris.py` | FITS時刻・観測地の読出し、天体名候補、SBDBフォールバック、Horizons CSV生成 |
| `scripts/astrometry_solve.py` | APIログイン、FITS送信、再試行、ジョブ待機、WCS/JSON取得、submission再開 |
| `scripts/moving_target_stack.py` | FITS入出力、WCS、Siril登録、画素シフト、平均・メジアン・ランクフィット、成果物生成 |
| `tests/test_moving_target_options.py` | 名称解決、セッション、スタック方式、プレビュー、キャッシュ、Siril失敗判定、OS差の単体テスト |
| `package-seestar-metcalf-stack*.ps1` | Sirilなし版とSiril同梱版の配布物作成 |
| `build-seestar-metcalf-stack-exe.ps1` | PyInstallerによるWindows one-file EXE作成 |

`README-Siril-CLI.md`は開発初期の実験記録です。現在は存在しない補助スクリプトや
ローカル絶対パスも含むため、現行仕様の正本にはしないでください。

## 数値処理とデータの扱い

### 座標CSV

CSVは`time`、`ra_deg`、`dec_deg`列を基本とし、列名には実装上の別名もあります。
各FITSの時刻とCSV時刻が完全一致する必要はありません。

- 範囲内は前後2点から赤経・赤緯を時間について線形補間します。
- 赤経の0度/360度境界は最短方向で補間します。
- 範囲外は先頭または末尾の2点から線形外挿します。
- 通常の数時間観測では、撮影期間をまたぐ最低2点が実用上の基準です。
- 近接通過など見かけの運動が非線形になる場合は点を増やす必要があります。

自動生成するHorizons CSVは、原則として各サブフレーム時刻の座標を持ちます。

### WCS

Astrometry.netのWCS FITSに基本的なCD行列があればそれを使い、利用できない場合は
JSON calibrationの中心、pixel scale、orientationからTAN WCSを構成します。
プレートソルブ結果は既定で基準FITSと同じフォルダへ、基準FITSのstemを使って
保存し再利用します。

- 星固定FITSのWCSは、基準フレーム座標系を表します。
- メトカーフFITSにも同じ基準WCSを記録します。移動天体は基準時刻の座標位置へ
  固定されますが、流れた背景星全体を単一WCSで表せるわけではありません。
- 左右結合FITSは左側が星固定、右側がメトカーフです。ヘッダーWCSは左側の
  星固定画像を基準に読めるよう配置されています。右半分へそのWCSをそのまま
  適用してはいけません。

### 画素結合

- `mean`: 有効画素ごとの算術平均。加算は`float64`です。
- `median`: 画素ごとの中央値。登録・シフトで生じる厳密な0を欠損として除外します。
- `rankfit`: 0を除外して明るさ順に並べ、中央の指定割合へ5次多項式を当て、
  順位中央の値を返します。標本が少ない画素は中央値へフォールバックします。

メジアンとランクフィットは、全フレームを`float32`のdisk-backed memmapへ置くため、
平均より大きな一時ディスク領域を使います。プレビューPNGは非線形な表示用
ストレッチであり、測光には使えません。

### 線形FITS

Sirilが登録後のunsigned 16-bit画像を`0..1`のfloatへ正規化したと判断した場合、
Python側で`0..65535 ADU`へ戻してから演算します。既定の
`--output-bitpix uint16 --uint16-scale none`は、平均後の値を再ストレッチせず、
0未満と65535超をクリップして丸めます。途中の平均演算は浮動小数点です。

補間後の小数値や範囲外値を失わず調査したい場合は`--output-bitpix float32`を
使います。`global`と`per-channel`のuint16 scaleは表示向けで、測光用ではありません。

### 飽和警告

`--saturation-warning enable`では、Siril登録済みサブフレームをADUへ戻した直後に
飽和マスクを作ります。FITSの`SATURATE`、`SATLEVEL`を優先し、なければ
整数`BITPIX`、`BSCALE`、`BZERO`から物理的な最大値を求めます。通常のSeestar
unsigned 16-bitは65535です。閾値と等しい画素は含めず、厳密に超えた画素だけを
飽和候補とします。RGB画像はどれか1チャンネルが超えれば該当します。
`DATAMAX`は画像内の実測最大値を示す場合があるため、飽和レベルには使いません。

同じマスクを星固定座標ではそのまま、メトカーフ座標では天体移動量だけシフトして
累積します。補間で触れた出力画素を保守的にすべて警告対象にします。通常PNGと
線形FITSは変更せず、専用の`*_saturation_warning.png`だけへ指定色を描きます。
CSVにはフレームごとの最大値、判定閾値、飽和画素数を、summary JSONには集計値と
設定を残します。

判定位置は生CFAではなくSiril登録後です。位置合わせ補間によってピークがわずかに
低下する可能性があるため、これは元FITSの厳密な飽和監査ではなく、最終スタック上で
疑わしい場所を見つけるための警告機能です。

## 機能追加の履歴

| 時期 | 主な追加 |
| --- | --- |
| v0.3.0 / 2026-07-14 | 初回公開。セッション分割、Horizons座標、Astrometry.net解、Siril星登録、メトカーフ/星固定/左右比較FITS、平均・メジアン・ランクフィット、線形FITS、PNG、CSV、JSONを統合 |
| v0.4.0 / 2026-07-14 | Astrometry.net補助処理をNode.jsからPythonへ移し、Node.js依存を削除。PyInstaller EXEと2種類のWindows配布ZIPを追加 |
| v0.4.1 / 2026-07-14 | 公開名を`seestar-metcalf-stack`へ統一。第1引数をソースフォルダとし、CMDへのドラッグ&ドロップと通常CLIを同じ入口へ統合 |
| v0.4.3相当 / 2026-07-17 | `24PSchaumasse`、`10PTempel`、`C2025A6 (Lemmon)`、番号付き小惑星など、Seestar表記からHorizons候補を作るロバストな天体特定とSBDBフォールバックを追加 |
| v0.4.4 / 2026-07-20 | verboseを標準化し、セッション一覧、処理段階、Siril出力、現在枚数/総枚数をリアルタイム表示。ログへ同じ内容を記録 |
| v0.4.5 / 2026-07-20 | メトカーフスタックだけに絞った公開リポジトリ構成へ整理。失敗検出と利用者向けREADMEを改善 |
| v0.5.0 / 2026-07-21 | macOS用Pythonセットアップ、薄いshellランチャー、Finder dropletを追加。Windows/macOSのCI単体テストを整備 |
| v0.5.0後 / 2026-07-22 | Horizonsの名称解決失敗から`--horizons-object`、`--horizons-command`、CSVで復旧する手順を日英READMEへ追加 |
| v0.5.0後 / 2026-07-22 | 座標CSVの線形補間・外挿と、撮影期間をまたぐ2点以上を推奨する条件を文書化 |
| v0.5.0後 / 2026-07-25 | サブフレーム飽和を星固定・メトカーフ固定の両座標へ伝播し、科学FITSとは別の警告PNGへ表示する任意機能を追加 |
| v0.5.1 / 2026-07-25 | 上記のREADME改善と飽和警告を公開。`DEVELOPMENT.md`、`PUBLISHING.md`、`README-Siril-CLI.md`を両配布ZIPへ同梱 |

初回公開時点ですでに含まれていた重要な試作改良には、次のものがあります。

- 星固定とメトカーフを同じルーチン・同じ結合方式で作る
- 左を星固定、右をメトカーフとする比較FITS/PNG
- 中間演算を浮動小数点で行い、既定uint16でADUを再スケールしない
- メジアンの0 padding除外と、0を除外したプレビューpercentile
- 5次ランクフィットと中央採用率のファイル名・FITSヘッダー記録
- 先頭/時刻中間の基準フレーム選択
- 基準FITS名単位のAstrometry.netキャッシュ
- 成功時の中間画像削除と`--no-cleanup`

## バグ修正の履歴

| 問題 | 原因と対応 |
| --- | --- |
| 旧PowerShellランチャーがnull例外や無応答になる | 子プロセス出力の扱いと責務が複雑だったため、進行表示とログをPythonへ集約し、CMDを薄くした |
| フォルダの末尾`\`と引用符で後続オプションがソースパスに混入する | CLI側に既存パスを使った修復を追加し、READMEでも末尾`\`を避けるよう案内した |
| `siril-cli.cmd`が自分自身を再帰呼出しする | PATH探索と同梱wrapperの関係を修正し、v0.4.2で再帰を防止した |
| Sirilが登録失敗を表示しても終了コード0になる | `Not enough free disk space`、`Registration aborted`、`Script execution failed`など出力中の失敗語を検出して例外にするよう修正した |
| Siril登録中に進行が止まったように見える | stdoutを逐次転送し、各処理段階とフレーム番号を即時表示するよう修正した |
| HorizonsでSeestarの詰めた天体名が見つからない | 周期彗星、非周期彗星、番号付き小惑星の候補生成とSBDB照会を追加した |
| Astrometry.net通信が一時切断すると最初から送信し直す | HTTP再試行とsubmission ID checkpointからの再開を追加した |
| WCS FITSを読めない場合に全処理が停止する | 必須WCSカードを検査し、Astrometry.net JSON calibrationへフォールバックした |
| 同じ基準フレームを試行のたび再送信する | 基準FITSのstemでWCS/JSONをソースフォルダへキャッシュするよう変更した |
| メジアン画像の端で0が中央値になる | 登録・シフト由来の厳密な0を欠損として除外するよう変更した |
| 0 paddingに引かれてプレビューが白黒2値に近くなる | PNGの表示レンジ計算から厳密な0を除外した |
| Siril登録後のfloat FITSでADUスケールが失われる | unsigned 16-bit入力と正規化済みfloat出力を検出し、ADUへ復元してから結合するよう修正した |
| 中間ファイルが作業領域外へ散らばる | 1回の実行に対応するwork directoryへ、CSV、アップロード用FITS、Siril生成物、成果物、要約を集約した |

## 2026-07-24の変更と理由

コミット`4803d8b`で日英READMEとmacOS READMEを変更しました。プログラム動作や
EXEには変更ありません。

- Astrometry.netのログイン画面、外部認証または新規登録、`API Help`、APIキーの
  コピーまでを初心者が追える粒度にした
- Windowsでは展開フォルダの空き領域から`ターミナルで開く`を選び、APIキー設定
  コマンドを実行する手順を追加した
- PowerShellとコマンドプロンプトの違いやGit管理など、初回利用者がその場で
  必要としない説明を削除した
- ドラッグ&ドロップと、セッション・処理方式・天体名を指定するCLI実行の違いを、
  初回設定ではなく実際の「使い方」へ移した
- macOSも同じ粒度のAPI取得手順に揃えた

変更理由は、文書を機能の羅列ではなく、各段階でその操作を必要とする読者へ
必要な情報だけを提示する構成にするためです。

## 既知の制約・未解決事項

### 優先度が高いもの

1. **`.fits`の既定探索**
   READMEは`.fit`と`.fits`を入力として説明していますが、既定patternは`*.fit`です。
   `.fits`だけのフォルダは`--pattern "*.fits"`が必要です。両拡張子を既定で扱う
   改修とテストが必要です。
2. **macOS実機検証**
   CIはmacOS上のPython単体テストとshell構文を確認しますが、Sirilを含む実際の
   end-to-end処理、Finder droplet、Gatekeeperは作者所有のMacで未検証です。
   macOSバイナリは未署名・未配布です。
3. **外部サービスを含む統合テスト**
   Astrometry.net、Horizons、実Sirilを通すCIはありません。API変更、応答形式変更、
   rate limit、長時間停止は単体テストでは検出できません。
4. **Astrometryキャッシュの同一性**
   キャッシュキーは基準FITSのファイル名stemで、内容hashではありません。同名の
   FITSを差し替えると古い解を再利用する可能性があります。

### 数値・天文上の制約

- 手作りCSVは線形補間・線形外挿です。地球近接時の強い曲率には高密度な座標か、
  将来の高次補間が必要です。
- JSON calibrationから作るWCSは中心、scale、orientationによるTAN近似です。
  SIPなどの光学歪み項は保持・評価していません。Astrometry.net WCS FITSでも、
  現在の座標変換は基本CD行列を使います。
- Sirilの既定`similarity`は平行移動、回転、等方倍率を扱いますが、局所歪みや
  非線形変形は扱いません。
- メトカーフFITSの背景星は流れるため、画像全体に一意な天球WCSが成立するわけでは
  ありません。左右比較FITSのWCSは左半分だけを基準にします。
- メジアンとランクフィットは厳密な0をpaddingとみなします。0 ADUが科学的に
  有効な装置では、その標本も除外される点に注意が必要です。
- 本ツールはdark、flat、bias、photometric zero point、誤差伝播を実装していません。
  線形ADUを保つことと、測光較正済みであることは別です。
- timezoneなしの`DATE-OBS`はUTCとして解釈します。

### 入出力と性能の制約

- 内蔵FITS readerはprimary imageの2DまたはCHW 3D画像、`BITPIX` 8/16/32/-32/-64を
  対象とします。FITS extension、tile compression、一般的な多次元FITSは未対応です。
- メジアン/ランクフィットはフレーム数×画像サイズのfloat32 memmapを2組使います。
  Sirilのデベイヤ・登録画像も加わるため、大規模セッションは空き容量を多く使います。
- 平均の有効画素countは現在uint16です。現実的ではありませんが、65535枚を超える
  セッションではoverflowします。
- 実機データで主に確認したのはSeestar S30です。S50、S30 Pro、将来firmwareが
  出力するFITSカードや画像形状は追加検証が必要です。
- FITSの上下方向は表示ソフトによって見え方が異なります。内部配列を既定では反転
  せず、PNG比較用に`--preview-flip-vertical`を残しています。

## 将来やりたいこと

優先順位の目安です。互換性を壊す変更はIssueまたはPRで意図を記録してください。

1. `.fit`と`.fits`を追加指定なしで同時に探索する
2. Astrometryキャッシュへファイルサイズ/hashを記録し、同名差し替えを検出する
3. Seestar FITSに十分なWCSがある場合は検証してAstrometry.net送信を省略する
4. HTTPをmockしたAstrometry/Horizons統合テストと、Siril stubによるpipeline testを追加する
5. 実MacでSiril、Finder droplet、Apple Siliconを検証し、将来は署名済みバイナリを作る
6. 近接天体向けに補間誤差を評価し、必要なら球面上の高次補間を追加する
7. 名称解決失敗の事例をIssueからテストケースへ蓄積する
8. coverage map、標本数、分散、不確かさを成果物として出し、測光評価をしやすくする
9. sigma-clipped meanなど、線形性と外れ値除去を両立する結合方式を検討する
10. Siril中間データの容量見積り、部分処理、再開機能を改善する
11. Windows/macOSのビルドとGitHub Release作成をCIで再現可能にする

## テストと変更時の確認

### 単体テスト

```text
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

GitHub ActionsはWindowsとmacOS、Python 3.12で同じテストを実行します。主な対象は
名称正規化、セッション、median/rankfit、プレビュー、基準選択、solve cache、
Astrometry request、Siril失敗検出、飽和閾値・マスク伝播・警告色、
ランチャーから見たOS差です。

### 手動確認

処理本体を変更した場合は、公開できない実観測データをローカルに用意して、最低限
次を確認してください。

1. `--list-sessions`がネットワークなしで正しいセッションを示す
2. キャッシュ済みWCS/JSONと座標CSVを使い、少数フレームの`mean`を完走する
3. `median`と`rankfit`で0 paddingが画像を支配しない
4. メトカーフ、星固定、左右比較のFITS/PNGがすべて生成される
5. `*_shifts.csv`と`*_summary.json`のフレーム数、時刻、基準フレームが一致する
6. uint16 `none`とfloat32の画素値が想定したADU関係を保つ
7. 成功時cleanupと`--no-cleanup`の両方を確認する
8. 通信中断後にAstrometry submissionを再開できる
9. `--saturation-warning enable`で専用PNGだけが生成され、通常PNGとFITSが変わらない
10. `*_shifts.csv`とsummary JSONの飽和件数が、確認した元フレームと一致する

実観測FITS、APIキー、観測地点、ログはリポジトリへcommitしないでください。

## ビルド・配布・引き継ぎ

### Windows EXE

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-seestar-metcalf-stack-exe.ps1
```

PyInstaller one-file EXEは`build\seestar-metcalf-stack.exe`へ生成されます。Pythonの
変更後にEXEを更新しないことが最も起こりやすい配布ミスです。

### 配布ZIP

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\package-seestar-metcalf-stack.ps1 -Version X.Y.Z
powershell -NoProfile -ExecutionPolicy Bypass -File .\package-seestar-metcalf-stack-siril.ps1 -Version X.Y.Z
```

両パッケージに`DEVELOPMENT.md`、`PUBLISHING.md`、`README-Siril-CLI.md`を含めます。
Siril同梱版ではGPLv3のlicenseとsource offerを必ず維持してください。

### 引き継ぎチェックリスト

1. `main`を正本とし、Release tagとパッケージversionを一致させる
2. Python変更時は単体テスト、実データsmoke test、EXE再生成を行う
3. 日英READMEとmacOS READMEで利用者向け仕様を同期する
4. 新しいCLI引数はPythonへ実装し、CMD/SHへロジックを増やさない
5. 新しい天体名の正規化は必ず単体テストを追加する
6. 出力FITSの線形性、WCS、ヘッダー、ファイル名を変更するときは互換性を記録する
7. 配布ZIPを展開し、EXE優先実行とPython fallbackの両方を確認する
8. GitHub Release作成前に`PUBLISHING.md`とthird-party noticeを確認する
9. Seestar制御・通信解析のファイルをこの公開リポジトリへ入れない

## プライバシー境界

- Astrometry.netへ送る基準FITSからは、既知の観測地カードを空白化します。ただし、
  画像、撮影時刻、天体名などは送信されます。
- 既定のtopocentric Horizons計算では、FITSの観測経度・緯度をJPLへ送信します。
- `--horizons-center geocenter`または手作りCSVで観測地送信を避けられます。
- `.astrometry_api_key`は実行時のローカル設定であり、配布物には入れません。

外部送信項目を増やす変更では、READMEとこの文書の両方を更新してください。
