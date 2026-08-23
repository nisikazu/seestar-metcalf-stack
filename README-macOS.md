# Seestar Metcalf Stack: macOSセットアップ

[メインREADME](README.md) | [English README](README-en.md) | [トラブルシュート](TROUBLESHOOTING.md)

macOSではPythonソース版を使用します。Windows版と共通のPython プログラムで処理を行い、
`seestar-metcalf-stack.sh`は実行環境を判定するランチャーです。
セットアップ後は、Finderでサブフレームフォルダを
`Seestar Metcalf Stack.app`へドロップしてスタックを実行できます。処理中はTerminalでログを表示し、
Windows版と同様にセッション一覧、処理段階、Siril出力、枚数進捗を確認できます。

## 必要なもの

- macOS 13以降を推奨
- Python 3.10以降
- Siril 1.4以降
- Astrometry.netとJPL Horizonsへ接続できるネットワーク
- Astrometry.net APIキー

Pythonは[python.orgのmacOS版](https://www.python.org/downloads/macos/)または
Homebrewでインストールできます。Sirilは
[公式macOSインストール手順](https://siril.readthedocs.io/en/stable/installation/macos.html)
に従ってアプリケーションフォルダへインストールしてください。Homebrewを使う場合は
次のコマンドでも導入できます。

```sh
brew install --cask siril
```

## 初回セットアップ

GitHubからソースを取得して展開し、Terminalでそのフォルダへ移動します。
次のスクリプトは`.venv`を作り、必要なPythonライブラリをそこへインストールし、
Finderドロップ用アプリもローカルで生成します。

```sh
cd /path/to/seestar-metcalf-stack
sh setup-macos.sh
```

続いてAstrometry.netのAPIキーを次の手順で取得します。

1. ブラウザで[Astrometry.netのログイン画面](https://nova.astrometry.net/signin)を開きます。
2. Googleアカウントなど、画面に表示される外部認証を使ってログイン、または新規登録します。
3. ログイン後、画面上部の`API`または`API Help`を開きます。[API Helpを直接開く](https://nova.astrometry.net/api_help)こともできます。
4. ページに表示される`Your API key is xxxxxx...`の英数字部分をコピーします。

Terminalで`YOUR_API_KEY`をコピーした文字列に置き換えて実行します。

```sh
./set-astrometry-api-key.sh YOUR_API_KEY
```

キーはプロジェクト直下の`.astrometry_api_key`へ保存されます。

## 実行方法

### Finderから実行

基本的な使い方は、処理したいサブフレームフォルダを
`Seestar Metcalf Stack.app`へドラッグ&ドロップするだけです。セッション、
処理状況を表示するTerminalが開き、正常終了すると成果物フォルダもFinderで
開きます。処理後も結果を確認できるよう、Returnキーを押すまでTerminalを
閉じません。セッション、処理方式、天体名などを指定する場合は、次の
「Terminalから実行」の方法でオプションを付けて実行してください。

### Terminalから実行

```sh
./seestar-metcalf-stack.sh "/path/to/C2025 R2 (SWAN)_sub"
```

セッション一覧だけを表示する場合:

```sh
./seestar-metcalf-stack.sh "/path/to/Target_sub" --list-sessions
```

## Dual Alignment Composite (Comet + Stars)

Finderの `Seestar Metcalf Stack.app` は、lightsフォルダをドロップすると
`--dual-stack` を付けて実行します。従来のMetcalf stackに加えて、target maskで
彗星を除外した恒星基準マスター、移動する恒星をrobustにrejectした彗星基準マスター、
および両者を同じ観測データからDSS風に合成した `*_comet_stars_subtractive.fit` を生成します。
Terminalから同じ機能を使う場合は次のように指定してください。

```sh
./seestar-metcalf-stack.sh "/path/to/Target_sub" --dual-stack
```

マスク半径は必要に応じてピクセル単位で指定できます。省略時は登録診断のFWHMを参考に
自動決定します。合成は空間的なブレンド境界ではなく、彗星モデルを各star-registered frameから
減算して恒星masterを作り、reference位置の彗星モデルを一度だけ加えるsubtractive方式です。

```sh
./seestar-metcalf-stack.sh "/path/to/Target_sub" --dual-stack \
  --comet-mask-radius-px 12
```

この機能はexperimentalな実験版MVPです。Dual Alignment Compositeは、同一観測データから
作成したstar-aligned画像とcomet-aligned画像の合成です。既知の制約は次のとおりです。

- clean comet masterには淡い恒星trailが残る場合があります。
- automatic tail mask extensionは保守的で、検出に失敗した場合はcore circular maskへfallbackします。
- composite画像をphotometry、astrometryなどの科学測定に直接使用しないでください。元サブフレーム、
  または目的に適した個別stackを使用してください。

生成物には、star master、clean comet master、composite mask、個別preview、同一stretchの比較preview、
恒星masterの`*_stars_contribution_count.png`、再現用metadata、diagnosticsが含まれます。

既定のDual Alignment Compositeはexperimentalなsubtractive方式で、clean cometは`median`
（`--stack-method rankfit`時は`rankfit`）、composite maskは安定した円形maskです。恒星trailの除去を追加検証する場合は、
既存のnumpy処理だけでMADベースのsigma rejectionを選択できます。

```sh
./seestar-metcalf-stack.sh "/path/to/Target_sub" --dual-stack \
  --comet-clean-method sigma --comet-sigma-low 3 --comet-sigma-high 3
```

長い尾を試す場合は、彗星masterと恒星masterの差分を低周波化し、coreに連結した構造だけを
optionalにmaskへ追加できます。恒星trailがcoreに接続している場合などは誤検出し得るため、
`circle`を既定値のまま残し、`tail`のmask previewとdiagnosticsを確認してください。

```sh
./seestar-metcalf-stack.sh "/path/to/Target_sub" --dual-stack \
  --composite-mask-method tail --composite-tail-length-px 256
```

`*_comparison_stars.png`、`*_comparison_comet.png`、`*_comparison_composite.png`は、
3画像に共通のdisplay stretchを適用した評価用PNGです。FITSの科学データにはstretchを
適用しません。diagnostics JSONには、恒星masterの全体／target-regionのcontribution count、
target周辺とのbackground median差、tail検出情報、比較previewのstretch値を記録します。

詳細な進行表示は標準で有効です。抑制するときは`--no-verbose`、正常終了時に
Finderを開かないときは`--no-open-output`を追加します。

FITSの天体名をJPL Horizonsで特定できない場合は、メインREADMEの
[「Horizonsで天体を特定できない場合」](README.md#horizonsで天体を特定できない場合)
を参照してください。macOSでは例の`seestar-metcalf-stack.cmd`を
`./seestar-metcalf-stack.sh`へ読み替えて、同じオプションを指定できます。

## Sirilを見つけられない場合

CLIはPATHと標準的な`/Applications/Siril.app`または
`/Applications/SiriL.app`を自動検索します。別の場所へ入れた場合は、環境変数
`SIRIL_CLI`または`--siril`で実行ファイルを指定します。

```sh
SIRIL_CLI="/custom/path/siril" ./seestar-metcalf-stack.sh "/path/to/Target_sub"
```

## ZIP展開後に実行権限がない場合

```sh
chmod +x seestar-metcalf-stack.sh setup-macos.sh set-astrometry-api-key.sh macos/build-droplet.sh
```

その後、`sh setup-macos.sh`をもう一度実行してください。

## 現在のmacOS配布方針

現時点では、署名済みmacOSバイナリは配布せず、Pythonソースとローカル生成する
Finderドロップ用アプリを提供します。Python CLIへ機能を集約しているため、将来
macOSバイナリを追加してもランチャーや操作方法を変えずに利用できます。

## プライバシー

プレートソルブのため基準FITS 1枚をAstrometry.netへ送信します。JPL Horizonsで
topocentric座標を得る場合はFITSに記録された観測地点も送信します。観測地点を
送信したくない場合は`--horizons-center geocenter`を使用してください。
