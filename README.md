# Seestar Metcalf Stack

[English](README-en.md) | [macOSセットアップ](README-macOS.md)

Seestar のサブフレームFITSから、彗星や小惑星を追跡したメトカーフスタックを作るWindows/macOS向けツールです。同じフレームから背景星固定スタックと、両者を左右に並べた比較FITSも作成します。

これは撮影後の画像処理専用ツールです。Seestar本体は制御せず、Seestar通信用のPEMや秘密キーも必要ありません。

## このソフトを使う流れ

このツールは、Seestarで撮影したサブフレームを後から処理するソフトです。まず彗星や小惑星をSeestarで観測し、元の1枚ごとの画像を保存しておきます。

1. Seestarアプリで彗星または小惑星を選び、観測を開始します。
2. 撮影設定で**サブフレーム保存をON**にします。保存されていないスタック済み画像だけでは、このツールでフレームごとの移動を計算できません。
3. 観測終了後、サブフレームのフォルダをPCへコピーします。USB経由で本体のファイルを取得する方法、またはSeestarをSTAモードにしてネットワークファイル共有経由で取得する方法があります。フォルダ名は通常 `*_sub` で、内部に `.fit` または `.fits` ファイルが入ります。
4. Windowsでは `seestar-metcalf-stack.cmd`、macOSではセットアップ時に作る `Seestar Metcalf Stack.app` へサブフレームフォルダをドラッグ&ドロップするか、コマンドで処理します。

## 必要な外部ツール一覧

サブフレームを用意しただけでは、画像が空のどこを向いているか、撮影中に天体がどこへ動いたか、背景星をどう重ねるかが分かりません。次のツールがそれぞれ別の役割を担います。

- **Astrometry.net** は基準フレームをプレートソルブし、その画像が空のどこを、どの画角と向きで撮影したかを確定します。これにより天体の赤経・赤緯を画像上の画素位置へ変換できます。アカウントとAPIキーが必要ですが、同梱の `set-astrometry-api-key.cmd` で設定できます。
- **JPL Horizons** は各露光時刻における対象天体の赤経・赤緯を返します。この固有運動から、フレームごとに追加すべき移動量を求めます。JPLのAPIキーは不要です。
- **Siril** は背景星を検出し、各フレームの平行移動・回転・倍率を基準フレームに対して推定します。本ツールはその結果にHorizons由来の天体移動量を加え、最後の画素スタックを行います。
- **Python、NumPy、Pillow** はソースコードを実行・改造する場合に必要です。配布版の `seestar-metcalf-stack.exe` には実行に必要なPythonランタイムが含まれているため、通常の利用者はPythonやライブラリを別途インストールする必要はありません。

処理の分担は、Astrometry.netが「画像がどこを向いているか」、Horizonsが「対象がどう動いたか」、Sirilが「背景星の写り方がフレーム間でどうずれたか」を決めます。Sirilは背景星の検出とフレーム間の平行移動・回転・倍率の推定を担当します。最終的なメトカーフスタック、星固定スタック、線形FITSの書き出しはPython側で行います。

## 必要なものと配布版の違い

- Windows 10/11、またはPythonソース版を実行するmacOS 13以降
- Astrometry.netとJPL Horizonsへ接続できるネットワーク
- Astrometry.net APIキー
- Siril 1.4以降

Sirilをまだインストールしていない利用者には、容量の大きい `seestar-metcalf-stack-siril-vX.Y.Z.zip` を標準版として推奨します。Sirilと実行用EXEを含むため、PythonやSirilを別途インストールする必要がありません。同梱されるSiril部分にはGPLv3が適用されます。

すでにSirilをインストール済みの場合や、配布サイズを小さくしたい場合は `seestar-metcalf-stack-vX.Y.Z.zip` を使います。この版も `seestar-metcalf-stack.exe` を含むため、通常の実行にPythonの別途インストールは不要です。Sirilは別途インストールし、`siril-cli.exe` にPATHを通すか、環境変数 `SIRIL_CLI` にフルパスを設定します。

バージョンアップ時は、Sirilなし版を展開して新しいファイルへ更新できます。旧版から次のものを新しいフォルダへコピーすると、SirilやAPIキー、過去の出力を引き継げます。

- `tools` フォルダ（Siril同梱版を使っていた場合）
- `.astrometry_api_key`
- `metcalf_output` フォルダ

Siril同梱版から更新する場合も、同じ3つを新しいSirilなし版へ移せます。Sirilを別途インストールしていない場合は、引き続きSiril同梱版を使用してください。

Pythonコードを改造した場合は、古いEXEが優先実行されないよう `seestar-metcalf-stack.exe` を削除するか、`build-seestar-metcalf-stack-exe.ps1` でEXEを再生成してください。初回ビルド時はPyInstallerを `.build` へ自動導入するため、ネットワーク接続が必要です。

## 初回セットアップ

1. Siril未導入なら、Siril同梱版を展開します。通常のEXE実行だけなら、これでPython依存パッケージのインストールは不要です。
2. Sirilを別途利用する場合は、Sirilをインストールし、`siril-cli.exe` をPATHへ追加するか `SIRIL_CLI` を設定します。Sirilなし版を使う場合も同じです。
3. Pythonコードを実行・改造する場合だけ、展開したフォルダで依存パッケージを準備します。

   ```bat
   setup-python-deps.cmd
   ```

4. [Astrometry.net](https://nova.astrometry.net/) にログインし、[API help](https://nova.astrometry.net/api_help) からAPIキーを取得します。
5. 同梱コマンドでAPIキーを保存します。

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

詳細表示はCMD、シェル、EXE、Pythonのどの入口でも標準で有効です。最初に全セッションと選択されたセッションを表示し、その後は処理段階、Sirilの出力、スタック方式、`現在枚数/総枚数`を表示します。同じ内容が実行中から `metcalf_output\metcalf-YYYYMMDD-HHMMSS.log` へ追記されます。正常終了時には成果物の出力フォルダをExplorerまたはFinderで開きます。詳細表示を抑制する場合は `--no-verbose`、成果物フォルダを開かない場合は `--no-open-output` を指定してください。macOSの準備とFinderドラッグ&ドロップについては [macOSセットアップ](README-macOS.md) を参照してください。

### 大規模セッションの空き容量

Sirilの背景星位置合わせでは、デベイヤ済み画像と登録済み画像を一時的に保存します。数百枚のセッションでは、元FITSの合計より大きな空き容量が必要です。Sirilが `Not enough free disk space` を表示した場合は、空き容量を増やす、`--work-root D:\metcalf_output` のように別ドライブを使う、または `--count 400` のように処理枚数を減らしてください。登録失敗時の中間FITSはデフォルトで自動削除されます。`--no-cleanup`を指定した場合は残ります。

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
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-object "C/2025 R2 (SWAN)"
```

### 2. HorizonsのCOMMANDを直接指定する

Horizonsで使える検索式やIDが分かっている場合は、`--horizons-command`でその値をそのまま渡します。これは名称の自動変換を行わないため、より確実です。

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "DES=24P;CAP;NOFRAG"
```

- `DES=24P`: 正式符号24Pを検索
- `CAP`: 複数の回帰軌道から適切な近日点回帰の解を選択
- `NOFRAG`: `73P-A`のような分裂片を除外し、親彗星を選択

番号付き小惑星は、番号と末尾のセミコロンを指定できます。

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "98943;"
```

検索結果に複数の軌道解が表示された場合は、目的のEpochに対応する `Record #` を直接指定できます。

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --horizons-command "90001033;"
```

HorizonsのRecord番号は将来変わる可能性があります。通常は正式符号と `CAP` / `NOFRAG`を優先し、古い観測などで特定の歴史的軌道解が必要な場合だけRecord番号を使います。PowerShellではセミコロンがコマンド区切りになるため、COMMAND全体を必ず引用符で囲んでください。

### 3. 作成済みの座標CSVを使う

Horizonsで別途作成した時刻・赤経・赤緯のCSVがある場合は、検索処理を行わずそのファイルを使用できます。

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --ephemeris-csv "C:\path\to\horizons.csv"
```

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

ファイル名に `_failed_` を含むSeestarフレームも使う:

```bat
seestar-metcalf-stack.cmd "C:\path\to\frames" --include-failed-frames
```

既存のAstrometry.net解を再利用する:

```bat
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
