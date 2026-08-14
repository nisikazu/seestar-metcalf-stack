# SharpCap画像の撮影時刻を扱うための設計資料

更新日: 2026-08-13  
状態: SharpCap 4.1.10745以降のLive Stack `stacklog.csv`入力を実装済み。通常連続撮影の汎用画像時刻推定は設計段階。

## 目的

Seestar Metcalf StackでSharpCapのFITS、PNG、TIFFなどを扱う際に、各フレームの露光中央時刻をできるだけ正確に決定するための根拠と実装方針を記録する。

移動天体のフレーム間移動量は撮影時刻から計算するため、単にファイルの並び順を得るだけでは不十分である。一方、通常の彗星撮影では数十ミリ秒程度の誤差はほぼ無視できるため、利用可能な情報の出典と精度を明示しながら実用的な時刻を決定する。

## 重要な結論

- `stacklog.csv` はSharpCap Live Stackが生成するフレーム単位ログであり、通常の連続撮影で常に生成されるログではない。
- Live Stackのraw frameでは、`stacklog.csv` の `Raw frame file` と実ファイル名を照合して時刻を取得する方法を第一候補とする。`stacklog.csv`はドロップしたraw frameフォルダ内または1つ上に置ける。
- PNG番号と `Frame Index` を算術的に対応させてはならない。保存されなかった候補フレームが存在するためである。
- `Date/Time` は列位置を固定せず、CSVヘッダ名を読んで特定する。
- タイムゾーンオフセットが時刻文字列に含まれる場合は、その値を最優先する。
- 露光中央時刻は実用上 `stacklog時刻 - 露光時間 / 2` とする。ただし、これはカメラ、USB、SDKによる転送遅延を補正していない推定値である。
- 通常の連続撮影では、画像内メタデータ、ファイル名、filesystem mtime、明示指定された開始時刻と間隔の順に利用する。`stacklog.csv` がないこと自体は異常ではない。

## stacklog.csvの位置付け

SharpCap 4.1公式マニュアルでは、Live Stackingの設定に `Create CSV log of frame information for each stack` があり、スタック候補となった各フレームの情報をCSVへ記録すると説明されている。

公式資料で確認できる主な列は次のとおりである。

| ヘッダ名 | 意味 |
| --- | --- |
| `Date/Time` | フレームがcaptureされた日時 |
| `Frame Index` | stacking process内のフレーム番号 |
| `Frame Stacked?` | スタックに採用されたか |
| `Detected Star Count` | 検出星数 |
| `Frame Star Brightness` | 星の明るさ指標 |
| `Frame Star FWHM` | 平均FWHM |
| `Frame Offset X (pixels)` | スタック基準からのX方向のずれ |
| `Frame Offset Y (pixels)` | スタック基準からのY方向のずれ |
| `Frame Rotation (degrees)` | スタック基準からの回転角 |
| `Raw frame file` | 保存されたraw frameのファイル名 |

SharpCap開発者Robin Glover氏は、CSVの先頭行が各列を定義すると説明している。したがってparserは「第1列が時刻」のような列番号依存にせず、正規化したヘッダ名で列を識別する。

### CSV行と画像ファイルは1対1とは限らない

`stacklog.csv` の1行は、stacking processが検討したフレームを表す。必ず1つのPNG、FITS、TIFFなどに対応するわけではない。

SharpCapのRaw Framesには、スタックに採用されたフレームだけを保存する `Save Stacked` と、すべてを保存する `Save All` などがある。`Save Stacked` の場合、alignment失敗、FWHM/brightness filter、pause、dither中などのフレームは画像として保存されない。このため次のような並びは正常である。

```text
Frame Index 1   Raw frame file = 空
Frame Index 2   Raw frame file = 空
Frame Index 3   Raw frame file = ...\frame_00001.png
```

この例では `frame_00001.png` は `Frame Index 3` に対応する。`frame_00001 = Frame Index 1` と推定せず、`Raw frame file` 列をキーに実ファイルとjoinする。

照合時には絶対パスと相対パスの違い、区切り文字、引用符、大文字小文字を正規化し、basenameで一致を確認する。コピー後のCSVに旧絶対パスが残っていても、ドロップしたフォルダ内またはそのセッションフォルダ内の同名画像を優先する。元の絶対パスはローカル候補が見つからない場合だけ最後に試す。同名ファイルが複数見つかり一意に決められない場合は、誤対応を避けるためエラーで停止する。

## 下処理済みraw frameへの差し替え

SharpCap Live Stackのraw frameは、Live Stack側のdark、flat、ホットピクセル処理を適用する前の入力として扱う。本ツールはこれらの補正を行わない。測光や微弱天体処理のため下処理が必要なら、セッションフォルダをコピーし、コピー側のraw frameだけを補正済み画像へ置き換える。

StackLogの各行との対応を維持するため、補正後もファイル名を変更しない。記録されたX/Y offsetとrotationをそのまま適用できるよう、画像サイズ、画素座標系、向き、切り抜き範囲も維持し、リサイズ、回転、反転、クロップを行わない。

推奨構造は、`stacklog.csv`と`*.CameraSettings.txt`をraw frameフォルダの1つ上に残す元のセッション構造である。raw frameフォルダだけをコピーする場合は、この2ファイルもraw frameフォルダ内へコピーできる。CameraSettingsはSharpCapバージョンの確認とExposure取得に必要である。

## Date/Timeの解釈

StackLogの説明は `Date & Time that frame was captured` としている。一方、SharpCapの一般的なframe timestampについて公式マニュアルは、多くのカメラではPCの時計を使い、フレームがSharpCapへdeliveryされた時点を記録すると説明している。この時刻は実際の露光終了より後であり、カメラからPCへの転送時間とメーカーSDK内の処理時間を含む。

したがって、StackLogの `Date/Time` を「SharpCapがフレームを受信した時刻」とみなすのは、一般timestamp仕様と実測結果に基づく強い推定である。ただし、StackLogの説明自体がdelivery timestampだと明記しているわけではないため、ログや文書では推定であることを区別する。

### 実測例

```text
StackLog       2026-08-07T01:08:25.6046741+09:00
UTC            2026-08-06T16:08:25.6046741Z
PNG mtime      2026-08-06T16:08:25.587892Z
差             約16.8 ms
```

StackLog時刻とPNG保存時刻が約17 ms以内で一致しており、画像保存直前付近のフレーム受信時刻という解釈と整合する。

### 露光中央時刻

露光時間を `Exposure` とすると、Metcalf stackで用いる実用的な推定値は次のとおりである。

```text
t_mid_estimated = t_stacklog - Exposure / 2
```

20.000秒露光の実測例では次の値になる。

```text
2026-08-06T16:08:25.6046741Z - 10.000 s
= 2026-08-06T16:08:15.6046741Z
```

物理的により厳密な関係は次のとおりである。

```text
t_mid_true = t_stacklog - camera/USB/SDK delivery latency - Exposure / 2
```

20秒程度の彗星撮影では数十msの転送遅延は通常無視できる。一方、掩蔽観測など10 ms級の絶対時刻精度を要求する用途には、この推定値をそのまま使用しない。

## SharpCapが生成する関連情報

| 形式・ファイル | 時刻・撮影情報の扱い |
| --- | --- |
| `stacklog.csv` | Live Stackの候補フレーム単位情報。`Raw frame file` で保存画像と対応付ける |
| `.CameraSettings.txt` | 撮影開始時のCamera Control設定。Exposure取得に利用できる |
| PNG | SharpCap公式仕様では、ほとんどメタデータを持たない |
| FITS | exposureや撮影時刻などの画像メタデータを格納できる |
| TIFF | 設定によりFITS相当情報をDescriptionへ格納できる場合がある |
| SER | フレーム単位timestampを保持できる |
| ADV | timestampを含む豊富なフレーム情報を保持できる |

`Save capture settings file alongside each capture` は既定で有効であり、撮影データと対応する名前の `.CameraSettings.txt` が作られる。これは露光時間などのセッション設定には有用だが、通常は個々のフレームの正確な時刻を直接与えるものではない。

SharpCapはスタックから何も保存しなかった場合、StackLogだけを含む空フォルダを残さない設計である。そのため、ログが存在しないことだけから撮影やスタック候補フレームがなかったとは判断できない。

## 時刻ソースの優先順位

### Live Stack raw frame

1. `stacklog.csv` の `Raw frame file` が対象画像と一致する行の `Date/Time`
2. FITS、SER、ADV、AstroTIFFなどに格納された明示的なフレーム時刻
3. SharpCapのper-frame filename timestamp
4. filesystem mtime
5. Capture/Stack開始時刻とframe index、intervalからの推定

### 通常の連続撮影

通常の連続撮影では `stacklog.csv` は前提にしない。

1. FITS、SER、ADV、AstroTIFFなどに格納された明示的なフレーム時刻
2. per-frame filename timestamp
3. filesystem mtime
4. `--capture-start-time` と `--capture-interval` のような明示指定による推定

`.CameraSettings.txt` はExposureや設定の取得に使う。セッション名や親フォルダ名の時刻は、各フレーム時刻を復元する最後のfallbackとしてのみ利用する。

## タイムゾーンの扱い

時刻は次の優先順位で解釈する。

1. timestampに `Z`、`+09:00` などのoffsetがある場合は、そのoffsetを採用する。
2. offsetのないnaive timestampで `--capture-time-zone` が指定されている場合は、そのtimezoneを適用する。
3. offsetがなく指定もない場合は、撮影日時におけるPCのlocal timezoneとして扱う。
4. timezoneを確定できない場合は警告し、推定根拠をログへ残す。

`--capture-time-zone` はoffsetのない時刻だけに適用する。埋め込まれたoffsetを通常オプションで上書きすると、正しい時刻を壊す危険がある。壊れたメタデータを強制再解釈する必要が生じた場合は、将来 `--force-capture-time-zone` のような別オプションとして設計する。

## バージョン差と異常検出

StackLogはSharpCap 4.1で導入された機能である。4.1初期betaでは、`Date/Time` が各フレーム時刻ではなく後からStackLogを書き出した時刻付近になる不具合が公式フォーラムで報告され、Robin Glover氏が修正している。また4.1.10745.0には、最初の保存直後にstack resetするとStackLogが保存されない問題の修正が記録されている。

時刻不具合そのものが修正された正確なbuild番号は公開情報だけでは断定できない。古い4.1 betaデータでは次の検査を行う。

- SharpCapバージョンを `.CameraSettings.txt` などから記録する。
- フレーム時刻が単調増加しているか検査する。
- 隣接フレームの間隔がExposureや実測cadenceと大きく矛盾しないか検査する。
- 多数の行がほぼ同一時刻になっていないか検査する。
- filesystem mtimeとの差を検査する。
- 異常時はStackLog時刻を無条件に採用せず、警告して別ソースへfallbackする。

2026-08-10公開のSharpCap 4.2.15037.0 betaまでの公開変更履歴にはStackLog schema変更の記載は確認されていない。ただし、将来も同一schemaである保証にはならない。未知の列は無視し、必要列をヘッダ名で探索する実装とする。

## Parserと診断ログの要件

推奨処理順は次のとおりである。

```text
CSV header名で列を探索
        ↓
Raw frame fileで実ファイルと対応付け
        ↓
Date/Timeをoffset-aware ISO 8601としてparse
        ↓
CameraSettings.txtなどからExposureを取得
        ↓
t_mid = t_stacklog - Exposure / 2
        ↓
mtimeとの差を検査
        ↓
前後frame間隔とExposureの整合性を検査
```

解析ログには、少なくとも次の情報を残す。

```text
Timestamp source : SharpCap stacklog.csv
Stack frame index: 3
Timestamp type   : SharpCap frame capture/delivery timestamp (estimated interpretation)
Timestamp TZ     : +09:00 embedded in stacklog
Exposure         : 20.000 s (CameraSettings.txt)
Exposure midpoint: 2026-08-06T16:08:15.6046741Z
File mtime delta : -16.8 ms
```

時刻の採用元、timezone、露光時間の出典、補正内容を残すことで、Metcalf stack結果を後から再検証できる。

## 実装時のテスト項目

- ヘッダ順を入れ替えたCSVでも正しく列を認識する。
- 未知の列が追加されても処理を継続する。
- `Raw frame file` が空の行を画像へ誤対応させない。
- `frame_00001.png` が `Frame Index 1` とは限らないケースを検証する。
- Windows絶対パス、相対パス、 `/` と `\`、引用符、大文字小文字を正規化できる。
- コピー元の絶対パスがまだ存在しても、ドロップしたコピー先の同名ファイルを優先する。
- `stacklog.csv`がraw frameフォルダ内または1つ上にある構造を認識する。
- 同名ファイルが複数ある場合に曖昧性を検出し、黙って選択しない。
- offset付き、`Z`、naive timestampを正しくUTCへ変換する。
- 古いSharpCapの異常な非単調時刻や同一時刻を検出する。
- Exposureが取得できない場合に中央時刻を捏造せず、採用した代替規則を警告する。
- 通常の連続撮影でStackLogがなくても、画像メタデータや明示指定で処理できる。

## 参考にした一次情報

- [SharpCap 4.1 User Manual - Capturing and Processing Images](https://docs.sharpcap.co.uk/4.1/6_GettingGoodImages.htm)
- [SharpCap公式フォーラム - StackLogのDate/Time不具合に関する議論](https://forums.sharpcap.co.uk/viewtopic.php?t=6615)
- [SharpCap公式フォーラム - StackLogの列定義に関する説明](https://forums.sharpcap.co.uk/viewtopic.php?t=7955)
- [SharpCap公式フォーラム - frame timestampの意味](https://forums.sharpcap.co.uk/viewtopic.php?t=2268)
- [SharpCap 4.1 Beta Release Notes](https://www.sharpcap.co.uk/sharpcap/sharpcap-downloads/sharpcap-4-1-beta)

## 今後の実装候補

- 通常連続撮影向けのoffset-aware ISO 8601 parserと `--capture-time-zone`。
- `--capture-start-time`、`--capture-interval` による通常連続撮影のfallback。
- timestamp source、Exposure source、mtime deltaを成果CSVへさらに詳しく記録。
- SharpCapバージョン別の既知不具合警告。

## 実装済みの範囲

- 入力フォルダまたは`rawframes`の親から`stacklog.csv`と`.CameraSettings.txt`を自動探索する。
- CSV列をヘッダ名で認識し、`Raw frame file`のbasenameを現在のraw frameへ照合する。
- 既定で`Frame Stacked? = 1`だけを採用し、SharpCapの失敗フレームを除外する。
- FITSの`DATE-AVG`または`DATE-OBS + EXPTIME/2`、PNG/TIFFのStackLog時刻とExposureから露光中央時刻を求める。
- SharpCap 4.1.10745未満とバージョン不明のセッションを安全のため停止する。
- X/Y offsetとrotationが完全な場合はPythonで背景星登録し、Sirilを省略する。
- alignment情報が不完全な場合は、画像を正規FITSへ変換してSirilへフォールバックする。
