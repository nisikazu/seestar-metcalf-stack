# 変更履歴

この文書は、利用者に影響する変更を記録します。開発上の判断、既知の制約、引き継ぎ情報は[DEVELOPMENT.md](DEVELOPMENT.md)を参照してください。

形式は[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)を参考にしています。

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
