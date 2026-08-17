# 改訂内容とトラブルシュート

この文書は、Seestar Metcalf Stack の実行中に起こりやすい問題と、結果の確認方法をまとめたものです。通常の導入手順は [README](README.md) を参照してください。

## 2026-07-31 の改訂

- `--reference-frame-index` は廃止しました。セッション・時刻フィルタ後の番号は実行前に予見できないためです。
- 任意の基準画像は `--reference-frame-file` にサブフレームのファイル名を指定します。選択されたセッション内に同名ファイルが必要です。
- 基準フレームで背景星が不足する場合は、基準画像・検出星数・必要数を表示して、スタックを作らず終了します。
- 基準以外のフレームは、雲・障害物・導入ずれなどで星位置合わせに失敗しても異常ではありません。そのフレームだけを除外し、残りをスタックします。
- 完了時には必ず `Stacked 使用枚数/対象枚数; skipped 除外枚数` を表示します。出力FITSのファイル名に入るフレーム数も実際に使った枚数です。

## 基準フレームを指定する

通常は最初のフレームが基準です。長時間セッションでは時刻中央のフレームを選べます。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame middle
```

任意の画像を指定する場合は、フォルダ全体のパスではなく、そのフォルダ内のFITSファイル名を渡します。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --reference-frame-file "Light_C2025 R2 (SWAN)_20.0s_IRCUT_20251103-185613.fit"
```

ファイル名またはフォルダ名に空白がある場合は、必ず引用符で囲みます。Windowsでは、引用符の直前にフォルダ末尾の `\` を置かないでください。

```bat
rem Correct
.\seestar-metcalf-stack.cmd "C:\data\C2025 R2 (SWAN)_sub"

rem Avoid: the trailing backslash can consume the closing quote in some shells
.\seestar-metcalf-stack.cmd "C:\data\C2025 R2 (SWAN)_sub\"
```

## 背景星の登録と除外

`--registration-minpairs` は、Sirilが各フレームを背景星で位置合わせする際に必要な最小星対数です。既定値は6です。

基準フレームでは、この条件を満たせないと正しい座標基準を作れないため停止します。まず雲のない、星像が十分に写ったフレームを基準に選び直してください。値をむやみに下げると誤った位置合わせを受け入れるおそれがあるため、通常は既定値のまま使います。

基準以外のフレームで失敗した場合は、処理を継続します。出力フォルダの `*_shifts.csv` を開き、次を確認してください。

- `used=true`: スタックに使用したフレームです。
- `used=false`: 除外したフレームです。`reason` に未登録、星対数不足、変換行列の欠落などの理由が残ります。
- `star_pairs`: Sirilが使った背景星対数です。

例えば `Stacked 53/64 frames; skipped 11` と表示された場合、64枚を対象にし53枚を使用、11枚を除外したことを示します。雲や建物による一時的な劣化では正常な結果です。除外が多すぎるときは、その観測区間をフォルダから外すか、`--session-index` または `--session-at` で別セッションを選んでください。

## よくある問題

### ダウンロードした`.cmd`や`.exe`を実行できない

Windowsはインターネットから取得したファイルにダウンロード元を示す情報を付けるため、`.cmd`、`.exe`、または内部から呼び出すPowerShellスクリプトの実行を止める場合があります。[GitHubの公式Release](https://github.com/nisikazu/seestar-metcalf-stack/releases)から取得したZIPであることを確認し、**展開前のZIP**を右クリックして`プロパティ`を開きます。`全般`タブの下部に`許可する`が表示された場合はチェックを入れて`OK`を押し、その後でZIPを展開してください。表示がなければ解除操作は不要です。

すでに展開している場合は、展開したフォルダを削除し、元のZIPを`許可する`にしてから再度展開するのが確実です。`seestar-metcalf-stack.cmd`だけを許可しても、同梱EXEや内部スクリプトにブロック情報が残る可能性があります。配布元を確認できないファイルでは解除しないでください。

### PowerShellで「コマンドとして認識されません」と表示される

PowerShellは安全上、現在のフォルダを自動的には実行ファイルの検索対象にしません。エクスプローラーの`ターミナルで開く`から実行するときは、現在のフォルダを表す`.\`を付けます。

```powershell
.\set-astrometry-api-key.cmd YOUR_API_KEY
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --list-sessions
```

コマンドプロンプトでは`.\`なしでも動きますが、READMEのWindows例はPowerShellでも確実に動く`.\`付きで統一しています。

サブフレームフォルダを`seestar-metcalf-stack.cmd`へドラッグ&ドロップする基本操作では、ターミナル入力は不要です。SharpCap PNG/TIFFのように天体名や画素スケールを指定する場合は、READMEの例に従ってターミナルから実行します。

### SharpCapの`stacklog.csv`またはraw frameが見つからない

本ツールは、指定したフォルダ内の`stacklog.csv`を最初に探し、なければ1つ上を探します。`stacklog.csv`自体を第1引数に指定した場合は、その親フォルダを処理対象にします。CSVと同じセッションの`*.CameraSettings.txt`も、指定したフォルダまたはその親に置いてください。

CSV内の`Raw frame file`がコピー前の絶対パスでも、ドロップしたフォルダ内に同名画像があればそちらを優先します。下処理後にファイル名を変えた場合は対応付けできません。元と同じ名前へ戻してください。同名ファイルが複数のサブフォルダにある場合は曖昧性エラーになるため、処理対象だけを1フォルダへ整理します。

### SharpCap PNG/TIFFで対象天体または画素スケールを要求される

PNG/TIFFにはHorizons検索に使える天体名や、プレートソルブの画素スケールが確実には入りません。`--horizons-object`または`--horizons-command`と、`--pixel-scale-arcsec`を指定してください。作成済みの座標CSVがある場合は、天体名の代わりに`--ephemeris-csv`を使えます。

観測地は`--site-longitude`と`--site-latitude`で指定できますが、省略時は地心座標へフォールバックします。地球へ近接中の天体では視差が大きくなるため省略しないでください。

### SharpCap Live Stack入力でもSirilが起動する

0.7.xではSirilをdark/flat補正、ホット・クールピクセル補正、デベイヤ、Plate Solveに使うため、StackLogの位置合わせが完全でもSirilは起動します。`Using SharpCap StackLog alignment after Siril preprocessing`と表示されれば、Sirilで前処理した後の背景星位置合わせにはStackLogのX/Y offsetとrotationを使用しています。

### CameraSettingsのmaster darkまたはflatが見つからない

CameraSettingsに記録された元の絶対パスと、コピー先周辺の同名ファイルを探します。見つからない場合は未補正で黙って続行しません。masterをコピーして`--dark-file`または`--flat-file`で指定するか、その補正を意図的に使わない場合だけ`--dark-correction disable`または`--flat-correction disable`を指定してください。

すでに補正済み画像へ差し替えた場合は、二重補正を避けるため`--preprocessing disable`を指定します。

### `--reference-frame-file was not found`

指定したファイルが、選択されたセッションまたは時刻範囲に含まれていません。`--list-sessions` で対象セッションを確認し、拡張子を含む正確なファイル名を指定してください。

```bat
.\seestar-metcalf-stack.cmd "C:\path\to\frames" --list-sessions
```

### `The selected reference frame has insufficient background stars`

基準画像の星が少なすぎます。雲、薄明、ピント不良、障害物、または視野移動直後の画像が候補です。`--reference-frame middle` または星が明瞭なファイル名を指定して再実行してください。

処理が停止しても、作業フォルダの`registration_diagnostics.csv`には全フレームの`detected_stars`、`fwhm_px`、`roundness`が記録されます。まず`detected_stars`が`--registration-minpairs`以上のフレームを絞り、その中からFWHMが小さくroundnessが大きいものを`--reference-frame-file`で指定してください。

### 使用枚数が少ない、またはスタックが作られない

`*_shifts.csv` の `reason` を確認してください。雲や障害物で失敗したフレームは除外されます。良好な連続区間だけを別フォルダにして再実行しても構いません。使用可能フレームが0ならスタックは作られずエラーで終了します。基準フレームまで失敗している場合は、基準を変更してください。

### `No files matching *.fit`

Seestarの最終スタック画像ではなく、サブフレームフォルダを指定してください。通常は末尾が `_sub` のフォルダです。拡張子が `.fits` のデータでは `--pattern "*.fits"` を指定します。

### Sirilが見つからない、または途中で失敗する

Siril同梱版を使うか、Sirilをインストールして `siril-cli` をPATHへ追加してください。`Not enough free disk space` は出力先に十分な空き容量がないことを示します。原因調査では `--no-cleanup` を付けると、Siril登録後の中間FITSを残せます。

### Windowsで出力時に `FileNotFoundError` になる

展開先、入力フォルダ、出力フォルダ、対象名を連結したパスがWindowsのパス長制限を超えている可能性があります。空白は問題ありませんが、フォルダ階層を浅くしてください。たとえば `C:\Seestar Metcalf Stack` のような短い展開先を使い、深いOneDrive配下や長い対象名の入れ子は避けます。

### Astrometry.netで停止する

既定の`--plate-solver auto`はSirilを先に試すため、通常のSeestar FITSではAstrometry.netへ到達しません。Astrometry.netへ進んだ場合は、Sirilで解決できなかった理由を直前のログで確認し、続いてAPIキーとネットワーク接続を確認してください。ローカル処理だけに限定するには`--plate-solver siril`を使います。

成功済みの基準フレームはソースフォルダの`*_siril_wcs.fits`、`*_astrometry.json`、`*_wcs.fits`を再利用します。別の基準を選ぶと、そのファイル用に新しい解が必要です。

### Horizonsで天体名が見つからない

READMEの「Horizonsで天体を特定できない場合」を参照し、`--horizons-object` または `--horizons-command` を指定してください。作成済みの座標CSVがあれば `--ephemeris-csv` も使えます。

## 問題報告に添えるもの

再現や原因調査には、次の情報が役立ちます。APIキーや個人情報は含めないでください。

- 実行したコマンドとコンソール出力
- `metcalf-*.log`
- `*_shifts.csv` と `*_summary.json`
- 使用したSeestar機種、ファームウェア、Sirilの版
- 問題が起きたサブフレーム数と、可能なら該当FITS数枚
