# Seestar Metcalf Stack

[改訂内容とトラブルシュート](TROUBLESHOOTING.md) | [変更履歴](CHANGELOG.md)

[English](README-en.md) | [macOSセットアップ](README-macOS.md)

SeestarやDWARFなどのサブフレームFITS、またはSharpCap Live Stackのraw frameから、彗星や小惑星を追跡したメトカーフスタックを作るWindows/macOS向けツールです。同じフレームから背景星固定スタックと、両者を左右に並べた比較FITSも作成します。

これは撮影後の画像処理専用ツールです。Seestar本体は制御せず、Seestar通信用のPEMや秘密キーも必要ありません。

## このソフトを使う流れ

このツールは、Seestar、DWARF、SharpCapなどで撮影したサブフレームを後から処理するソフトです。まず彗星や小惑星を観測し、元の1枚ごとの画像を保存しておきます。

1. 撮影機器または撮影ソフトで彗星・小惑星を選び、観測を開始します。
2. 撮影設定で**サブフレーム保存をON**にします。保存されていないスタック済み画像だけでは、このツールでフレームごとの移動を計算できません。
3. 観測終了後、サブフレームのフォルダをPCへコピーします。SeestarではUSB経由、またはSTAモードのネットワークファイル共有を利用できます。フォルダ名は通常`*_sub`で、内部に`.fit`または`.fits`ファイルが入ります。
4. Windowsでは `seestar-metcalf-stack.cmd`、macOSではセットアップ時に作る `Seestar Metcalf Stack.app` へサブフレームフォルダをドラッグ&ドロップするか、コマンドで処理します。

### 入力ごとに必要な情報

| 入力 | 対象天体 | 画素スケール | 観測地 | Astrometry.net APIキー |
| --- | --- | --- | --- | --- |
| Seestar/DWARFのFITS | FITSの`OBJECT`を優先。不足時だけ`--horizons-object`等で指定 | FITSの焦点距離・画素情報を優先。不足時だけ`--pixel-scale-arcsec` | FITSを優先。省略時は地心座標 | 通常は不要。Sirilで解けない場合のフォールバックにだけ使用 |
| SharpCapのFITS | `OBJECT`がなければ指定 | SharpCapで焦点距離を記録するか、オプションで指定 | FITSまたはオプション。省略可能 | 通常は不要。Sirilで解けない場合のみ使用 |
| SharpCapのPNG/TIFF | `--horizons-object`、`--horizons-command`、または`--ephemeris-csv`が必要 | `--pixel-scale-arcsec`が必要 | 省略可能。近接天体では指定を推奨 | 通常は不要。Sirilで解けない場合のみ使用 |

SeestarやDWARFのFITSで天体名、撮影時刻、中心座標、ほぼ正しい画角が記録されていれば、サブフレームフォルダをランチャーへドロップするだけで処理できます。既定の`--plate-solver auto`は最初にSirilでローカル解決するため、Astrometry.net APIキーを設定していなくても最低限の処理が可能です。

FITSの記録内容は機種や撮影ソフトによって異なります。不足している値があれば、本ツールは必要なオプションを示して停止します。推測のまま処理を続けることはありません。

### SharpCapで撮影する場合

SharpCap 4.1.10745以降のLive Stackで、raw frame保存、`Create CSV log of frame information for each stack`、背景星位置合わせを有効にします。採用フレームすべてのX/Y offsetとrotationが`stacklog.csv`にそろっていれば、本ツールはその変換を背景星位置合わせに再利用します。デフォルトでは`Frame Stacked?`が成功のraw frameだけを使います。

本ツールは`*.CameraSettings.txt`を読み、撮影時に指定されていたmaster dark、master flat、ホットピクセル補正、クールピクセル補正をSirilでraw frameへ適用してからデベイヤします。`Hot Pixel Sensitivity`が0以外ならホットピクセル補正を有効にしますが、SharpCapとSirilで数値の尺度が異なるため、値そのものは変換せずSirilの既定sigma 3を使います。masterファイルを一緒に移動した場合は記録されたbasenameから近隣の`darks`/`flats`フォルダも探します。

このためCameraSettingsがそろったSharpCapデータでは、ホットピクセル補正を個別に設定する必要はありません。Sirilが補正とデベイヤを行い、Seestarサブフレームと同じカラー画像の状態へそろえてからスタックします。ただし、PNG/TIFFには対象天体名や画角が通常入らないため、初回は下記のようにターミナルから追加情報を指定します。SharpCap FITSでこれらがヘッダーに記録されている場合は、フォルダのドラッグ&ドロップだけで実行できます。

すでに別ソフトで補正済みの画像へ置き換えた場合は、二重補正を避けるため`--preprocessing disable`を指定してください。StackLog変換を再利用するには、補正後も**元と同じファイル名、画像サイズ、向き、切り抜き範囲**を保つ必要があります。リサイズ、回転、反転、クロップは行わないでください。

推奨構造は次のとおりです。セッションフォルダを丸ごとコピーし、コピー側の`rawframes`だけを同名の補正済み画像へ置き換えます。

```text
10P_processed_session\
  stacklog.csv
  Stack.CameraSettings.txt
  rawframes\
    frame_00001.png
    frame_00002.png
```

`rawframes`フォルダだけをコピーする場合は、`stacklog.csv`と`*.CameraSettings.txt`もその中へコピーできます。本ツールはドロップしたフォルダ内を先に探し、次に1つ上のフォルダを探します。CameraSettingsはSharpCapのバージョン、PNG/TIFFの露光時間、dark/flatとホット・クールピクセル補正の設定を得るために使います。記録されたmaster dark/flatもコピーするか、`--dark-file`/`--flat-file`で明示してください。

フォルダの代わりに、その中の`stacklog.csv`を`seestar-metcalf-stack.cmd`へドラッグ&ドロップしても実行できます。この場合は`stacklog.csv`の親フォルダを処理対象とし、同じフォルダまたはその配下からCSVに記録された同名フレームを探します。

```text
10P_processed_rawframes\
  stacklog.csv
  Stack.CameraSettings.txt
  frame_00001.png
  frame_00002.png
```

コピー後はCSV内の旧絶対パスが存在しなくても構いません。`Raw frame file`のファイル名を使い、ドロップしたフォルダ内の同名画像を旧パスより優先して対応付けます。同名画像が複数ある場合は誤対応を避けるため停止します。

PNG/TIFFのLive Stack raw frameでは、次をコマンドで指定します。

- 対象天体: `--horizons-object`、`--horizons-command`、または作成済みの`--ephemeris-csv`
- 画素スケール: `--pixel-scale-arcsec`（秒角/画素）
- 観測地: `--site-longitude`（東経を正）と`--site-latitude`（北緯を正）。省略可能

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\10P_processed_rawframes" --horizons-object "10P/Tempel 2" --pixel-scale-arcsec 2.392 --site-longitude 139.6 --site-latitude +35.9
```

観測地を省略した場合はJPL Horizonsの地心座標を使います。通常の彗星・小惑星を数時間処理する用途では差が小さいことが多い一方、地球へ近接中の天体では地心視差が無視できないため、正確な観測地を指定してください。

画素スケールは、実効焦点距離をmm、画素ピッチをµmとして次の式で計算できます。

```text
画素スケール [秒角/画素] = 206.265 × 画素ピッチ [µm] ÷ 実効焦点距離 [mm]
```

ビニングを行っている場合は、画素ピッチにビニング倍率を掛けた実効画素ピッチを使います。例えば、実効焦点距離250 mm、画素ピッチ2.9 µmなら、`206.265 × 2.9 ÷ 250 = 2.392` 秒角/画素です。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\SharpCap\frames" --horizons-object "10P/Tempel 2" --pixel-scale-arcsec 2.392
```

SharpCap Live StackのPNG/TIFFが2D Bayer RAWで、画像自身にBayer patternが記録されていない場合は、CameraSettingsとカメラ仕様を確認して明示します。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\SharpCap\session" --horizons-object "10P/Tempel 2" --pixel-scale-arcsec 2.392 --bayer-pattern RGGB
```

PNGでは画像内に撮影時刻がほぼ残らないため、`stacklog.csv`の時刻からCameraSettingsの露光時間の半分を引いて露光中央時刻を求めます。FITSに`DATE-AVG`がある場合はそれを優先します。Sirilが補正とデベイヤを担当し、StackLogの位置合わせ記録が不完全、位置合わせがOFF、または`--include-sharpcap-rejected`で失敗フレームも含めた場合は、背景星位置合わせもSirilで行います。詳細な根拠は[SharpCap時刻設計資料](SHARPCAP-TIMESTAMPS.md)を参照してください。

CameraSettingsよりコマンド指定を優先します。masterファイルが移動後に見つからない場合は、誤って未補正のまま続行せず、必要な`--dark-file`または`--flat-file`を示して停止します。

```bat
rem master dark/flatを明示して処理
.\seestar-metcalf-stack.cmd "C:\path\to\SharpCap\session" --dark-file "C:\masters\dark.fit" --flat-file "C:\masters\flat.fit"

rem 補正済みフレームなのでCameraSettingsの補正をすべて無効化
.\seestar-metcalf-stack.cmd "C:\path\to\processed\frames" --preprocessing disable
```

個別には`--dark-correction`、`--flat-correction`、`--hot-pixel-correction`、`--cold-pixel-correction`へ`auto`、`enable`、`disable`を指定できます。ホット・クールピクセルのSiril閾値は`--hot-pixel-sigma`と`--cold-pixel-sigma`で変更できます。

## 必要な外部ツール一覧

サブフレームを用意しただけでは、画像が空のどこを向いているか、撮影中に天体がどこへ動いたか、背景星をどう重ねるかが分かりません。次のツールがそれぞれ別の役割を担います。

- **Siril** はdark/flat補正、ホット・クールピクセル補正、デベイヤ、基準フレームのプレートソルブ、必要な場合の背景星位置合わせを行います。Seestar FITSでは記録された中心座標と画素スケールを使うため、通常は短時間でローカル解決できます。
- **Astrometry.net** はSirilで基準フレームを解決できない場合のフォールバックです。利用する場合だけアカウントとAPIキーが必要で、同梱の`set-astrometry-api-key.cmd`で設定できます。
- **JPL Horizons** は各露光時刻における対象天体の赤経・赤緯を返します。この固有運動から、フレームごとに追加すべき移動量を求めます。JPLのAPIキーは不要です。
- **Python、NumPy、Pillow** はソースコードを実行・改造する場合に必要です。配布版の `seestar-metcalf-stack.exe` には実行に必要なPythonランタイムが含まれているため、通常の利用者はPythonやライブラリを別途インストールする必要はありません。

処理の分担は、Sirilが「raw画像を補正・カラー化し、画像がどこを向いているか」を、Horizonsが「対象がどう動いたか」を決めます。背景星の平行移動・回転は、完全なSharpCap StackLogがあればその記録を使い、なければSirilが推定します。最終的なメトカーフスタック、星固定スタック、線形FITSの書き出しはPython側で行います。

## 必要なものと配布版の違い

- Windows 10/11、またはPythonソース版を実行するmacOS 13以降
- JPL Horizonsへ接続できるネットワーク
- Siril 1.4以降
- Astrometry.net APIキー（SirilでPlate Solveできない場合の任意のフォールバック）

Sirilをまだインストールしていない利用者には、容量の大きい `seestar-metcalf-stack-siril-vX.Y.Z.zip` を標準版として推奨します。Sirilと実行用EXEを含むため、PythonやSirilを別途インストールする必要がありません。同梱されるSiril部分にはGPLv3が適用されます。

すでにSirilをインストール済みの場合や、配布サイズを小さくしたい場合は `seestar-metcalf-stack-vX.Y.Z.zip` を使います。この版も `seestar-metcalf-stack.exe` を含むため、通常の実行にPythonの別途インストールは不要です。Sirilは別途インストールし、`siril-cli.exe` にPATHを通すか、環境変数 `SIRIL_CLI` にフルパスを設定します。

SharpCap Live StackのX/Y offsetとrotationが全採用フレームにそろっていても、0.7.xではSirilを補正・デベイヤとPlate Solveに使います。StackLogは背景星位置合わせだけを置き換えます。

バージョンアップ時は、Sirilなし版を展開して新しいファイルへ更新できます。旧版から次のものを新しいフォルダへコピーすると、SirilやAPIキー、過去の出力を引き継げます。

- `tools` フォルダ（Siril同梱版を使っていた場合）
- `.astrometry_api_key`
- `metcalf_output` フォルダ

Siril同梱版から更新する場合も、同じ3つを新しいSirilなし版へ移せます。Sirilを別途インストールしていない場合は、引き続きSiril同梱版を使用してください。

Pythonコードを改造した場合は、古いEXEが優先実行されないよう `seestar-metcalf-stack.exe` を削除するか、`build-seestar-metcalf-stack-exe.ps1` でEXEを再生成してください。初回ビルド時はPyInstallerを `.build` へ自動導入するため、ネットワーク接続が必要です。

## 初回セットアップ

1. [GitHubの公式Release](https://github.com/nisikazu/seestar-metcalf-stack/releases)からZIPをダウンロードします。WindowsでZIPを右クリックして`プロパティ`を開き、`全般`タブの下部に`セキュリティ: このファイルは他のコンピューターから取得したものです`と`許可する`が表示された場合は、`許可する`にチェックを入れて`OK`を押してから展開します。この表示がなければ、そのまま展開できます。配布元を確認できないZIPではブロックを解除しないでください。
2. Sirilをまだ導入していない場合はSiril同梱版を展開します。通常のEXE実行だけならPython依存パッケージのインストールは不要です。
3. Sirilなし版を使う場合はSirilを別途インストールし、`siril-cli.exe`をPATHへ追加するか`SIRIL_CLI`を設定します。
4. Pythonコードを実行・改造する場合だけ、展開したフォルダで依存パッケージを準備します。

   ```bat
   .\setup-python-deps.cmd
   ```

5. SirilでPlate Solveできない場合にも処理を継続したいときは、任意でAstrometry.netのAPIキーを次の手順で取得します。

   1. ブラウザで[Astrometry.netのログイン画面](https://nova.astrometry.net/signin)を開きます。
   2. Googleアカウントなど、画面に表示される外部認証を使ってログイン、または新規登録します。
   3. ログイン後、画面上部の `API` または `API Help` を開きます。[API Helpを直接開く](https://nova.astrometry.net/api_help)こともできます。
   4. ページに表示される `Your API key is xxxxxx...` の英数字部分をコピーします。

6. Windowsで展開したSeestar Metcalf Stackのフォルダをエクスプローラーで開きます。ファイルではなくフォルダ内の空いている場所を右クリックし、`ターミナルで開く`を選びます。

7. 開いたターミナルで、`YOUR_API_KEY`を手順5でコピーした文字列に置き換えて実行します。PowerShellでは現在のフォルダにあるコマンドを実行するとき、先頭に`.\`が必要です。

   ```bat
   .\set-astrometry-api-key.cmd YOUR_API_KEY
   ```

キーはツールと同じフォルダの `.astrometry_api_key` に保存されます。

## 最初にセッションを確認する

まずサブフレームフォルダ内の撮影セッションを一覧表示できます。この操作はローカルだけで完結し、Astrometry.net、Horizons、Sirilを呼びません。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\98943 Torifune_sub" --list-sessions
```

一覧には1から始まるセッション番号、フレーム数、ローカル時刻とUTCの開始・終了時刻が表示されます。連続するFITSの間隔が60分を超えたところで別セッションになります。何も指定しなければ最新セッションを処理します。

一覧の番号で選ぶ場合:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --session-index 2
```

指定したローカル日時以後に開始する最初のセッションを選ぶ場合:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --session-at 20260709-195000
```

`--session-at` は `YYYYMMDD` または `YYYYMMDD-hhmmss` 形式です。時刻はPCのローカル時刻として解釈されます。省略した時刻桁は `00`、時分秒の1桁指定や範囲外値も `00`、範囲外の月日は `01` として扱います。

## スタックを実行する

最新セッションを平均処理し、先頭フレームを基準にする基本実行:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\C2025 R2 (SWAN)_sub"
```

基本的な使い方は、処理したいサブフレームフォルダを `seestar-metcalf-stack.cmd`へドラッグ&ドロップするだけです。成功すると出力フォルダが開きます。セッション、処理方式、天体名などを指定する場合は、インストールフォルダの空いている場所を右クリックして`ターミナルで開く`を選び、上記の例のようにオプションを付けて実行してください。

処理はHorizons座標取得、Sirilによる基準フレームのPlate Solveと画像前処理、StackLogまたはSirilによる背景星位置合わせ、最終スタックまで自動で進みます。SirilでPlate Solveできなかった場合だけAstrometry.netへフォールバックします。出力先は `metcalf_output\<target>_<処理方式>-YYYYMMDD-HHMMSS` です。方式部分は `mean`、`median`、または `rankfit5_p50` のようになります。

詳細表示はCMD、シェル、EXE、Pythonのどの入口でも標準で有効です。最初に全セッションと選択されたセッションを表示し、その後は処理段階、Sirilの出力、スタック方式、`現在枚数/総枚数`を表示します。同じ内容が実行中から `metcalf_output\metcalf-YYYYMMDD-HHMMSS.log` へ追記されます。正常終了時には成果物の出力フォルダをExplorerまたはFinderで開きます。詳細表示を抑制する場合は `--no-verbose`、成果物フォルダを開かない場合は `--no-open-output` を指定してください。macOSの準備とFinderドラッグ&ドロップについては [macOSセットアップ](README-macOS.md) を参照してください。

### 大規模セッションの空き容量

Sirilの背景星位置合わせでは、デベイヤ済み画像と登録済み画像を一時的に保存します。数百枚のセッションでは、元FITSの合計より大きな空き容量が必要です。Sirilが `Not enough free disk space` を表示した場合は、空き容量を増やす、`--work-root D:\metcalf_output` のように別ドライブを使う、または `--count 400` のように処理枚数を減らしてください。登録失敗時の中間FITSはデフォルトで自動削除されます。`--no-cleanup`を指定した場合は残ります。

### プレートソルブ結果のキャッシュ

最初に解決した結果は、サブフレームのソースフォルダへ基準FITS名を使って保存します。

- `<基準FITSのstem>_siril_wcs.fits`
- `<基準FITSのstem>_astrometry.json`
- `<基準FITSのstem>_wcs.fits`
- 送信途中または再開用の `<基準FITSのstem>_astrometry_submission.json`

次回以降はSiril WCS、Astrometry.net WCS、JSON calibrationの順に検証して再利用します。Sirilで解決できれば画像をAstrometry.netへ送信しません。Astrometry.netへのアップロード後に処理が中断した場合も、保存されたsubmission IDから既存ジョブを再開します。`--reference-frame`によって別の基準FITSが選ばれれば、そのFITS専用の別キャッシュになります。ソースフォルダ以外へ永続キャッシュを置きたい場合だけ `--solve-dir` を指定します。

### 平均、メジアン、ランクフィット

デフォルトの平均は、入力が良好なら一般に最も高いS/Nを得やすい方式です。Sirilの位置合わせやメトカーフシフトによって画像外になった画素は加算せず、画素ごとの整数の寄与枚数で割ります。補間に必要な4近傍がすべて実画像内にある場合だけ採用するため、外挿を行わず、重なり枚数が少ない周辺部でも平均輝度が暗くなりません。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method mean
```

以前の版と同じpadding処理を再現して比較する場合だけ、`--padding-policy legacy`を指定します。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method mean --padding-policy legacy
```

### 時間変化する空の明るさを揃える

高度の変化、薄明、月や雲によってサブフレームごとの背景が変わると、画素ごとの寄与枚数で平均しても、早い時刻のフレームだけが寄与する周辺が明るく（または暗く）残ることがあります。`--background-normalization`は、登録済みの各フレームから推定した背景を差し引いて背景を0付近にしてからスタックします。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames"
```

既定の`quadratic`は、実画像の有効画素を50x50タイルに分け、各タイルを4画素間隔でサンプリングしたsigma-clipped medianを使ってRGBごとの二次曲面をフィットします。一度フィットした残差のMADから外れるタイルを除外して一回だけ再フィットするため、星や小さな彗星の影響を抑えながら、毎回同じ結果になります。背景補正を無効化するには`none`、DC成分だけを揃えるには`offset`、一次平面を使うには`plane`を選べます。従来比較用の`--padding-policy legacy`を指定した場合だけ、補正未指定時は`none`になります。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --background-normalization plane
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --background-normalization quadratic
```

補正は実画像の有効画素だけに適用され、登録・シフトで生じた画像外の0は加算にも背景推定にも使いません。スタック演算中は負値も保持します。最後に、採用フレームごとのRGB局所DC値を算術平均した一定の出力オフセットだけを実データ領域へ加えます。これは保存形式で負値を避けるための表現レンジ対策で、背景の傾斜面を復元するものではありません。`BGNORM`、`BGGOAL=zero`、`BGREF1`〜`BGREF3`（最終出力オフセット）、shifts CSVの各フレーム背景・差引き係数・タイル診断に記録されます。これらの補正は有効画素マスクを必要とするため`--padding-policy valid`（既定）でのみ利用できます。

表示用PNGは既定で、各RGBチャンネルの有効画素の単純な平均・標準偏差を基準に`-1σ`から`+3σ`へ線形伸長します。星を除外しないため、背景ノイズだけを過大に強調しません。従来のパーセンタイル伸長を使うには`--preview-stretch percentile`を指定します。

この機能は通常の彗星・小惑星向けです。視野の大部分を占める大きな彗星やDSOは空の背景と区別できず、面モデルによって一部が差し引かれる可能性があります。そのような対象では`none`または`offset`を選んでください。

画素ごとのメジアンは、人工衛星、飛行機、ホットピクセルなど少数フレームだけに現れる外れ値に強い方式です。メジアンはメトカーフスタッキング像において星の軌跡を低減し、彗星光度の精度向上を図ります。一方で平均より遅く、大きなディスク上の一時配列を使い、統計的な効率も通常は平均より低くなります。メジアンでは登録・シフト境界に現れる完全な0をデフォルトで母集団から除外します。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method median
```

厳密な0も中央値の母集団に含めて従来処理と比較する場合は、`--zero-sample-policy include`を追加します。この指定はランクフィットにも適用されます。ただし、重なりの少ない領域ではpaddingの0が中央値を占め、真っ黒な領域が広く発生するため、通常のスタックには非推奨です。旧版との比較など、0を含める必要が明確な場合だけ使用してください。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method median --zero-sample-policy include
```

ランクフィットは、各画素の非0サンプルを明るさ順に並べ、中央の指定割合を採用し、正規化順位に対する明るさを5次多項式でフィットして中央値順位での関数値を返します。既定の採用率は50%です。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method rankfit --rankfit-fraction 50
```

`--rankfit-fraction` は1〜100の整数です。出力名と実行フォルダには `rankfit5_p50` のように採用率を記録します。中央候補が7点未満の画素は非0メジアンへフォールバックします。

出力名には `_mean_`、`_median_`、または `_rankfit5_pNN_` が入り、FITSヘッダーの `STKMODE` に方式を記録します。ランクフィットでは `RFFRAC` と `RFDEG` に採用率と次数も記録します。

### 先頭または時刻中間の基準フレーム

デフォルトは先頭フレームです。長時間セッションでは、撮影開始と終了の時刻中間に最も近いフレームを基準にすると、最大の位置合わせ量や天体シフト量を抑えられます。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame middle
```

任意のサブフレームを基準にする場合は、番号ではなくファイル名を指定します。空白を含む名前は引用符で囲みます。選んだ基準フレームで `--registration-minpairs`（既定6）以上の背景星対を取得できない場合は、警告して処理を中止します。雲の通過などで登録できない他のフレームは、shifts CSVに理由を記録して除外し、利用可能なフレームだけをスタックします。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame-file "Light_C2025 R2 (SWAN)_20.0s_IRCUT_20251103-185613.fit"
```

選ばれたフレームをSirilでPlate Solveし、位置合わせ基準にも明示設定します。Sirilで解けなかった場合だけAstrometry.netへ送ります。最終FITSの`DATE-OBS`とWCS座標はこの基準フレームを反映します。`REFMODE`、`REFINDEX`、`MTREFRA`、`MTREFDEC`にも基準情報を残します。

### サブフレームの飽和警告

彗星や比較星の測光では、スタック画像だけを見ると元サブフレームの飽和を見落とすことがあります。次の指定で、いずれかのサブフレームが飽和レベルの90%を超えた画素を赤く示す警告PNGを追加生成できます。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --saturation-warning enable
```

既定は `--saturation-warning disable` です。判定割合と警告色は変更できます。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --saturation-warning enable --saturation-threshold-percent 90 --saturation-color FF0000
```

`--saturation-threshold-percent` は0より大きく100以下、`--saturation-color` は6桁のRGB 16進数です。Seestarのunsigned 16-bit FITSでは通常65535を飽和レベルとし、既定では58981.5を超えた画素を検出します。FITSに `SATURATE` または `SATLEVEL` があれば、その値を優先します。画像内の実測最大値を表すことがある `DATAMAX` は飽和レベルとして使いません。

判定はSirilで背景星位置合わせした各サブフレームをADUへ戻した後に行います。マスクは星固定像と移動天体固定像へそれぞれ伝播するため、両方の座標系で該当位置を確認できます。警告色は専用PNGだけに描画され、測光用FITSと通常プレビューPNGは変更しません。

## 出力

ファイル名には対象、露光時間、フィルター、UTCの開始・終了時刻、使用枚数、平均/メジアン方式が入ります。

例: `C2025_R2_SWAN_20.0s_IRCUT_20251103T095234Z-20251103T105620Z_90frames_median_metcalf_stack.fit`

- `*_metcalf_stack.fit`: 移動天体固定の線形FITS
- `*_star_stack.fit`: 同じ採用フレームによる背景星固定の線形FITS
- `*_star_left_metcalf_right.fit`: 左に星固定、右に移動天体固定を並べたFITS。WCSは左半分に有効
- `*_metcalf_preview.png`、`*_star_preview.png`: 表示用ストレッチ画像。測光には使用しません
- `*_metcalf_north_up_preview.png`、`*_star_north_up_preview.png`、`*_star_left_metcalf_right_north_up_preview.png`: `--preview-north-up`指定時に、プレートソルブしたWCSを使って天の北を上に回転した表示用PNG。元のFITSと通常プレビューは変更しません
- `*_metcalf_sun_pa_left_preview.png`: `--preview-sun-pa-left`指定時に、太陽方向を左、反太陽方向を右に置いた移動天体固定の表示用PNG。通常、ダストテイルを右向きに表示できます
- `*_annotated_preview.png`: `--preview-at UL|UR|LL|LR`指定時に作る、N/E方位マークと太陽方向矢印を重ねた表示用PNG。北上または太陽左と併用した場合は、その回転済み画像へ重ねます
- `*_annotation_overlay.png`: `--preview-at UL|UR|LL|LR`指定時に作る、N/E方位マークと太陽方向矢印だけの小型・透過RGBA PNG。元の画像サイズではなく、`--annotate-size`で指定した描画半径と保護余白だけを持つため、資料や投稿画像へ任意の位置に重ねられます
- `*_metcalf_saturation_warning.png`、`*_star_saturation_warning.png`: `--saturation-warning enable` 時だけ作る飽和警告PNG
- `*_star_left_metcalf_right_saturation_warning.png`: 星固定と移動天体固定の飽和警告を並べたPNG
- `*_shifts.csv`: 各フレームの星位置合わせ量と天体移動量
- `*_registration_diagnostics.csv`: 全フレームの位置合わせ診断表。基準フレームの選び直しや、採用枚数が少ない原因の確認に使います
- `*_summary.json`、`moving_target_pipeline_summary.json`: 再現用の処理記録

北を上にした表示画像が必要な場合は、次のように指定します。WCSが必要なため、プレートソルブまたは有効なWCSキャッシュが必要です。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --preview-north-up
```

太陽方向を左にしてダストテイルを右向きに表示したい場合は、`--preview-sun-pa-left`を使います。通常はJPL Horizonsから、基準フレーム時刻・対象座標・観測地に対する太陽の位置角を取得し、移動天体固定FITSへ`SUN_PA`（北から東回りの太陽方向）と`ASUN_PA`（反太陽方向）を記録します。通信できない場合でも通常のスタックは完走しますが、太陽方向を使う表示オプションは利用できません。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --preview-sun-pa-left
```

N/Eと太陽方向の注釈は既定で左上（`--preview-at UL`）、半径60pxで作ります。`--preview-at UR|LL|LR`で注釈付き表示PNGの角を、`--annotate-size 120`のように描画半径を変えられます。注釈を不要にする場合は`--preview-at none`を指定します。併せて出力される`*_annotation_overlay.png`は角指定に依存しない小型の透過PNGなので、画像編集ソフトや資料側で好きな場所に配置できます。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --preview-sun-pa-left --preview-at LR --annotate-size 60
```

位置合わせ診断表は、index、元ファイル名、基準フレームか、採用/除外、除外理由、FWHM、weighted FWHM、roundness、検出星数、初期対応星数、フィッティング後の対応星数、inlier率、背景星位置合わせのX/Y移動量・回転角・倍率を記録します。`fwhm_px`はSirilの代表FWHM、`weighted_fwhm_px`はSirilの星品質を考慮したweighted FWHMです。値が小さいほど星像は鋭く、roundnessは1に近いほど丸い星像です。

基準フレーム不良などで最終スタックまで進めなかった場合も、同じ内容の`registration_diagnostics.csv`を作業フォルダへ先に保存します。

スタック枚数が少ない場合は、まず`reason`と`fitted_matched_pairs`を確認してください。検出星数が多く、FWHMが小さく、roundnessが高いフレームが基準候補です。対応星数は現在選ばれている基準に対する値なので、別候補の良否はそのファイルを`--reference-frame-file`で指定して再実行して確認します。基準フレーム自身は他画像との対応付けを行わないため、対応星数は空欄になります。X/Y/回転角はSirilの変換行列から読み取った「各フレームを基準座標へ写す変換」です。

最終FITSは線形ADU値を保ち、中間計算は浮動小数点で行います。デフォルトのunsigned 16-bit出力は再スケールしません。補間後の小数値も直接残したい場合は `--output-bitpix float32` を使います。

Siril登録画像とメジアン用一時配列は成功後に削除します。調査のため残す場合は `--no-cleanup` を指定します。

## Horizonsで天体を特定できない場合

通常はFITSヘッダーの `OBJECT` を読み取り、Seestarで使われる名称からJPL Horizons用の検索候補を自動生成します。彗星・小惑星の名称表記がHorizonsの登録名と一致しない場合や、同じ彗星に複数の回帰軌道・分裂片が登録されている場合は、自動特定できないことがあります。

ログに次のような表示があれば、Horizonsの天体特定で停止しています。

```text
Target candidate did not resolve: ...
No matches found.
Horizons response did not contain $$SOE/$$EOE ephemeris markers
Could not identify target '...' in JPL Horizons.
```

複数候補の一覧が返る場合も、対象や軌道解を一意に選べていません。次の順番で復旧してください。

### 1. 正式名称・符号で上書きする

[JPL Horizons](https://ssd.jpl.nasa.gov/horizons/)または[Horizons Lookup API](https://ssd-api.jpl.nasa.gov/doc/horizons_lookup.html)で正式名称、彗星符号、小惑星番号を確認し、`--horizons-object`でFITSの `OBJECT` を上書きします。この指定でも名称の正規化と複数候補の検索を行います。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-object "C/2025 R2 (SWAN)"
```

### 2. HorizonsのCOMMANDを直接指定する

Horizonsで使える検索式やIDが分かっている場合は、`--horizons-command`でその値をそのまま渡します。これは名称の自動変換を行わないため、より確実です。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "DES=24P;CAP;NOFRAG"
```

- `DES=24P`: 正式符号24Pを検索
- `CAP`: 複数の回帰軌道から適切な近日点回帰の解を選択
- `NOFRAG`: `73P-A`のような分裂片を除外し、親彗星を選択

番号付き小惑星は、番号と末尾のセミコロンを指定できます。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "98943;"
```

検索結果に複数の軌道解が表示された場合は、目的のEpochに対応する `Record #` を直接指定できます。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "90001033;"
```

HorizonsのRecord番号は将来変わる可能性があります。通常は正式符号と `CAP` / `NOFRAG`を優先し、古い観測などで特定の歴史的軌道解が必要な場合だけRecord番号を使います。PowerShellではセミコロンがコマンド区切りになるため、COMMAND全体を必ず引用符で囲んでください。

### 3. 作成済みの座標CSVを使う

Horizonsで別途作成した時刻・赤経・赤緯のCSVがある場合は、検索処理を行わずそのファイルを使用できます。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --ephemeris-csv "C:\path\to\horizons.csv"
```

各サブフレームと完全に同じ時刻の座標を用意する必要はありません。本ツールはFITSの観測時刻ごとに、CSV内の前後2点から赤経・赤緯を時間に対して線形補間します。CSVの時刻範囲より前または後のフレームには、先頭または末尾の2点を使った線形外挿を行います。

地球への近接時など、見かけの運動が大きく曲がる場合を除けば、数時間の撮影では天体はほぼ等速直線に移動します。そのため、撮影開始以前と撮影終了以後の座標を含む最低2点を指定してください。CSVの2点が撮影期間をまたいでいれば全フレームを補間でき、範囲外の外挿による誤差を避けられます。近接通過など非線形性が無視できない場合は、撮影期間内の座標点を増やしてください。

指定の優先順位は、実在する `--ephemeris-csv`、`--horizons-command`、`--horizons-object`、FITSの `OBJECT` の順です。

### 解決できなかった天体名をお知らせください

自動検索で解決できなかった名称は、今後の名称変換ロジック改善に利用できます。[GitHub Issues](https://github.com/nisikazu/seestar-metcalf-stack/issues)または [@RollerRacers](https://twitter.com/RollerRacers) へ、次の情報をお知らせください。

- Seestar Metcalf Stackのバージョン
- FITSの `OBJECT` に記録されていた文字列
- 本来意図していた天体の正式名称・符号
- ログの `Trying Horizons target:` から最終エラーまで
- 成功した `--horizons-object`、`--horizons-command`、またはCSVがあればその指定内容

Astrometry.net APIキー、観測地点、個人情報、FITS本体を公開する必要はありません。ログを掲載する前に、それらが含まれていないことを確認してください。

## その他のオプション

Plate Solveの選択:

```bat
rem 既定: Sirilを先に試し、失敗時だけAstrometry.netを使う
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --plate-solver auto

rem ネットワークへ画像を送らずSirilだけで解く
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --plate-solver siril

rem Astrometry.netを明示的に使う
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --plate-solver astrometry
```

ファイル名に `_failed_` を含むSeestarフレームも使う:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --include-failed-frames
```

既存のAstrometry.net解を再利用する:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --astrometry-json "C:\path\to\solution.json"
```

観測地をHorizonsへ送らず地心座標を使う:

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-center geocenter
```

Windowsの `.cmd` に引用符付きパスを渡す場合、閉じ引用符直前の末尾バックスラッシュは付けないでください。`"C:\path\to\frames"` は正しく、`"C:\path\to\frames\"` は避けます。

## プライバシー

SirilのローカルPlate Solveに成功すれば、基準FITSをAstrometry.netへ送りません。フォールバックでAstrometry.netを使う場合は基準FITSを1枚送信し、その前に観測地を表すFITSカードを削除します。デフォルトではtopocentric座標を得るため、JPL HorizonsへFITSの観測地を送ります。送りたくない場合は `--horizons-center geocenter` または自分で用意した `--ephemeris-csv` を使ってください。

## ライセンスと作者

Seestar Metcalf StackはMIT Licenseで公開します。

Copyright (c) 2026 **Nishida Kazufumi**
([@RollerRacers](https://twitter.com/RollerRacers))

SirilはGPLv3ソフトウェアであり、本プロジェクトのMITライセンス部分とは別です。詳細は `THIRD-PARTY-NOTICES.md` を参照してください。
