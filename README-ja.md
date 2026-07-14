# Seestar Metcalf Stack

Seestar のサブフレームFITSから、彗星や小惑星を追跡したメトカーフスタックを作るWindows向けツールです。同じフレームから背景星固定スタックと、両者を左右に並べた比較FITSも作成します。

これは撮影後の画像処理専用ツールです。Seestar本体は制御せず、Seestar通信用のPEMや秘密キーも必要ありません。

## なぜ外部ツールが必要なのか

準備するものが複数あるのは、生のSeestarサブフレームだけでは移動天体を正確に止めるための情報が揃わないためです。

- **Astrometry.net** は基準フレームをプレートソルブし、その画像が空のどこを、どの画角と向きで撮影したかを確定します。これにより天体の赤経・赤緯を画像上の画素位置へ変換できます。アカウントとAPIキーが必要ですが、同梱の `set-astrometry-api-key.cmd` で設定できます。
- **JPL Horizons** は各露光時刻における対象天体の赤経・赤緯を返します。この固有運動から、フレームごとに追加すべき移動量を求めます。JPLのAPIキーは不要です。
- **Siril** は背景星を検出し、各フレームの平行移動・回転・倍率を基準フレームに対して推定します。本ツールはその結果にHorizons由来の天体移動量を加え、最後の画素スタックを行います。
- **Python、NumPy、Pillow** は処理全体、座標とシフトの計算、線形FITSの出力、確認用PNGの作成に使います。
- **Python** はAstrometry.netへのアップロード、完了待ち、解決結果の取得、再開用チェックポイント保存も行います。

つまり、Astrometry.netは「画像がどこを向いているか」、Horizonsは「対象がどう動いたか」、Sirilは「背景星の写り方がフレーム間でどうずれたか」を担当します。APIキーやSirilの準備には意味があり、どれも別の役割です。

## 必要なものと配布版の違い

- Windows 10/11
- Astrometry.netとJPL Horizonsへ接続できるネットワーク
- Astrometry.net APIキー
- Siril 1.4以降

標準版 `seestar-metcalf-stack-vX.Y.Z.zip` は `seestar-metcalf-stack.exe` を含むため、通常の実行にPythonの別途インストールは不要です。Sirilは同梱しないため、別途インストールし、`siril-cli.exe` にPATHを通すか、環境変数 `SIRIL_CLI` にフルパスを設定します。

容量の大きい `seestar-metcalf-stack-siril-vX.Y.Z.zip` は、Sirilと実行用EXEを含むWindows便利版です。Pythonを別途インストールする必要はありません。同梱されるSiril部分にはGPLv3が適用されます。

Pythonコードを改造した場合は、古いEXEが優先実行されないよう `seestar-metcalf-stack.exe` を削除するか、`build-seestar-metcalf-stack-exe.ps1` でEXEを再生成してください。

## 初回セットアップ

1. Sirilをインストールします。Siril同梱版では不要です。
2. Pythonコードを実行・改造する場合は、展開したフォルダで依存パッケージを準備します。

   ```bat
   setup-python-deps.cmd
   ```

3. [Astrometry.net](https://nova.astrometry.net/) にログインし、[API help](https://nova.astrometry.net/api_help) からAPIキーを取得します。
4. 同梱コマンドでAPIキーを保存します。

   ```bat
   set-astrometry-api-key.cmd YOUR_API_KEY
   ```

キーはツールと同じフォルダの `.astrometry_api_key` に保存されます。このファイルはGit管理や配布に含めないでください。

## 最初にセッションを確認する

まずサブフレームフォルダ内の撮影セッションを一覧表示できます。この操作はローカルだけで完結し、Astrometry.net、Horizons、Sirilを呼びません。

```bat
seestar-metcalf-stack.cmd "C:\path\to\98943 Torifune_sub" --list-sessions
```

一覧には1から始まるセッション番号、フレーム数、ローカル時刻とUTCの開始・終了時刻が表示されます。連続するFITSの間隔が60分を超えたところで別セッションになります。何も指定しなければ最新セッションを処理します。

一覧の番号で選ぶ場合:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --session-index 2
```

指定したローカル日時以後に開始する最初のセッションを選ぶ場合:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --session-at 20260709-195000
```

`--session-at` は `YYYYMMDD` または `YYYYMMDD-hhmmss` 形式です。時刻はPCのローカル時刻として解釈されます。省略した時刻桁は `00`、時分秒の1桁指定や範囲外値も `00`、範囲外の月日は `01` として扱います。

## スタックを実行する

最新セッションを平均処理し、先頭フレームを基準にする基本実行:

```bat
seestar-metcalf-stack.cmd "C:\path\to\C2025 R2 (SWAN)_sub"
```

サブフレームフォルダは `seestar-metcalf-stack.cmd` に直接ドラッグ&ドロップして実行できます。成功すると出力フォルダが開きます。

処理はHorizons座標取得、基準フレームのプレートソルブ、Sirilによる背景星位置合わせ、最終スタックまで自動で進みます。出力先は `metcalf_output\<target>_<処理方式>-YYYYMMDD-HHMMSS` です。方式部分は `mean`、`median`、または `rankfit5_p50` のようになります。

### プレートソルブ結果のキャッシュ

最初に解決した結果は、サブフレームのソースフォルダへ基準FITS名を使って保存します。

- `<基準FITSのstem>_astrometry.json`
- `<基準FITSのstem>_wcs.fits`
- 送信途中または再開用の `<基準FITSのstem>_astrometry_submission.json`

次回以降はWCSまたはJSON calibrationの内容を検証し、正常ならFITSをAstrometry.netへ再送せず利用します。アップロード後の結果待ち中に処理が中断した場合も、保存されたsubmission IDから既存ジョブを再開します。`--reference-frame`によって別の基準FITSが選ばれれば、そのFITS専用の別キャッシュになります。ソースフォルダ以外へ永続キャッシュを置きたい場合だけ `--solve-dir` を指定します。

### 平均、メジアン、ランクフィット

デフォルトの平均は、入力が良好なら一般に最も高いS/Nを得やすい方式です。

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method mean
```

画素ごとのメジアンは、人工衛星、飛行機、ホットピクセルなど少数フレームだけに現れる外れ値に強い方式です。メジアンはメトカーフスタッキング像において星の軌跡を低減し、彗星光度の精度向上を図ります。一方で平均より遅く、大きなディスク上の一時配列を使い、統計的な効率も通常は平均より低くなります。メジアンでは登録・シフト境界の完全な0を常に母集団から除外します。

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method median
```

ランクフィットは、各画素の非0サンプルを明るさ順に並べ、中央の指定割合を採用し、正規化順位に対する明るさを5次多項式でフィットして中央値順位での関数値を返します。既定の採用率は50%です。

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --stack-method rankfit --rankfit-fraction 50
```

`--rankfit-fraction` は1〜100の整数です。出力名と実行フォルダには `rankfit5_p50` のように採用率を記録します。中央候補が7点未満の画素は非0メジアンへフォールバックします。

出力名には `_mean_`、`_median_`、または `_rankfit5_pNN_` が入り、FITSヘッダーの `STKMODE` に方式を記録します。ランクフィットでは `RFFRAC` と `RFDEG` に採用率と次数も記録します。

### 先頭または時刻中間の基準フレーム

デフォルトは先頭フレームです。長時間セッションでは、撮影開始と終了の時刻中間に最も近いフレームを基準にすると、最大の位置合わせ量や天体シフト量を抑えられます。

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame middle
```

選ばれたフレームがAstrometry.netへ送られ、Sirilの位置合わせ基準にも明示設定されます。最終FITSの `DATE-OBS` とWCS座標はこの基準フレームを反映します。`REFMODE`、`REFINDEX`、`MTREFRA`、`MTREFDEC` にも基準情報を残します。

## 出力

ファイル名には対象、露光時間、フィルター、UTCの開始・終了時刻、使用枚数、平均/メジアン方式が入ります。

例: `C2025_R2_SWAN_20.0s_IRCUT_20251103T095234Z-20251103T105620Z_90frames_median_metcalf_stack.fit`

- `*_metcalf_stack.fit`: 移動天体固定の線形FITS
- `*_star_stack.fit`: 同じ採用フレームによる背景星固定の線形FITS
- `*_star_left_metcalf_right.fit`: 左に星固定、右に移動天体固定を並べたFITS。WCSは左半分に有効
- `*_metcalf_preview.png`、`*_star_preview.png`: 表示用ストレッチ画像。測光には使用しません
- `*_shifts.csv`: 各フレームの星位置合わせ量と天体移動量
- `*_summary.json`、`moving_target_pipeline_summary.json`: 再現用の処理記録

最終FITSは線形ADU値を保ち、中間計算は浮動小数点で行います。デフォルトのunsigned 16-bit出力は再スケールしません。補間後の小数値も直接残したい場合は `--output-bitpix float32` を使います。

Siril登録画像とメジアン用一時配列は成功後に削除します。調査のため残す場合は `--no-cleanup` を指定します。

## その他のオプション

ファイル名に `_failed_` を含むSeestarフレームも使う:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --include-failed-frames
```

既存のHorizons CSVまたはAstrometry.net解を再利用する:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --ephemeris-csv "C:\path\to\ephemeris.csv"
seestar-metcalf-stack.cmd "C:\path\to\frames" --astrometry-json "C:\path\to\solution.json"
```

観測地をHorizonsへ送らず地心座標を使う:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-center geocenter
```

Windowsの `.cmd` に引用符付きパスを渡す場合、閉じ引用符直前の末尾バックスラッシュは付けないでください。`"C:\path\to\frames"` は正しく、`"C:\path\to\frames\"` は避けます。

## プライバシー

Astrometry.netへは基準FITSを1枚送ります。送信前に観測地を表すFITSカードを削除します。デフォルトではtopocentric座標を得るため、JPL HorizonsへFITSの観測地を送ります。送りたくない場合は `--horizons-center geocenter` または自分で用意した `--ephemeris-csv` を使ってください。

## ライセンスと作者

Seestar Metcalf StackはMIT Licenseで公開します。

Copyright (c) 2026 **Nishida Kazufumi**
([@RollerRacers](https://twitter.com/RollerRacers))

SirilはGPLv3ソフトウェアであり、本プロジェクトのMITライセンス部分とは別です。詳細は `THIRD-PARTY-NOTICES.md` を参照してください。
