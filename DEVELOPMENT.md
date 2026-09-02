# Seestar Metcalf Stack 開発・引き継ぎノート

利用者に影響する変更は[変更履歴](CHANGELOG.md)と[改訂内容とトラブルシュート](TROUBLESHOOTING.md)にまとめています。この文書は実装判断、検証、引き継ぎを目的とした開発者向け資料です。

## 2026-09-02: registration座標に基づくexpanded output canvas

- `--output-region`に`reference`、`union`、整数の枚数、`%`付きの割合を受け付ける。`reference`は完全互換の既定、`union`は全採用footprintの外接矩形、整数は指定枚数、割合は採用枚数に対する指定割合（切り上げ）を満たす画素群の外接矩形である。
- Siril行列のY反転変換は不変の`registration_shape`で行い、選択後の`StackCanvas(shape, origin_x, origin_y)`を混ぜない。source四隅を登録座標へprojective変換し、coverage prepassは候補canvas上のfootprintを整数count mapへラスタライズする。異常な変換による過大canvasと、available RAMの半分を超えるcoverage mapは明示エラーにする。
- moving modeでは星固定と`T_motion @ H_star`によるMetcalf固定のfootprintを別々に評価する。左右比較FITSの両半分を同じshapeに保つため、どちらかの生成物でcoverage条件を満たす領域の和集合に対する外接矩形を共通canvasとする。fixed modeは星固定footprintだけを使う。
- resampler、mean accumulator、median/rankfit row tile、valid mask、飽和maskは既存の独立canvas APIをそのまま使用する。WCSは登録基準画像のheaderを作ってからcanvas originだけ`CRPIX1/2`へ反映するため、CD/PC/SIP係数は変えない。FITSの`OUTREG`/`OUTORGX`/`OUTORGY`/`OUTFRMS`/`OUTCOV`/`OUTRAT`とsummaryのcandidate/selected canvasへ判断を記録する。
- coverageは登録に成功して実際のstack taskになったフレームだけを数える。外接矩形内には、回転したfootprintの角や、条件を満たす画素群の非矩形形状により、寄与0または閾値未満の画素が残り得る。実際の平均除算・median標本選択は従来どおり画素単位のvalid maskを使う。
- 220Pの実データ20枚で回帰した。`reference`は1080x1920・原点(0,0)、`union`は1105x1963・原点(-3,-20)となり、共通領域を切り出したstar/Metcalf FITSはどちらも`reference`と全画素完全一致（最大差0 ADU）した。`--output-region 10`と`--output-region 50%`はどちらも1094x1925・原点(0,-5)・実効閾値10/20となり、star/Metcalfの全画素が完全一致した。5枚の`median --median-tile-rows 256 --output-region union`も1086x1926・原点(-3,-2)の拡張canvasを8個の全幅行タイルで処理し、5/5枚を完走した。自動テストは213件すべて成功。
- SharpCap StackLog経路は、現在`transform_image()`で基準shapeの`r_*.fit`を先に生成するため、失われた外側を後段で復元できない。非`reference`指定は黙って不完全画像を作らず、理由と回避方法を表示して終了する。将来はSharpCap行列をsourceへ直接合成する経路へ変更すれば同じcanvas policyを利用できる。
- この機能は同一登録座標系内の拡張であり、天球上の離れたフレームを連結するモザイクではない。将来のモザイクは各フレームWCSの共通投影面への変換、歪み、背景・photometric normalization、接続グラフを別レイヤーとして追加する。

## 2026-09-01: median/rankfitのbounded row-tile処理

- 従来の`MedianAccumulator`は全採用フレーム×全画面の`float32` memmapを作り、ディスク容量は抑えられず、OS cache次第では物理RAMも大きく消費した。productionの`median`/`rankfit`は二段階の全幅行タイル方式へ変更し、cubeをディスクへ書き出さずRAM上だけで処理する。旧クラスは数値回帰用の参照実装としてだけ残す。
- 第1段階は各採用FITSを1回読み、ADU復元、valid mask、RGB背景面fit、飽和レベルと該当画素数を確定する。背景係数とフレーム評価を保持し、画素キューブは保持しない。第2段階は`StackCanvas`を同じ登録座標系の全幅行タイルへ分け、各FITSを再読込して背景面を適用し、星固定とMetcalf固定をそのタイルへ直接resampleする。
- 各タイルは`(frame, channel, tile-row, width)`の`float32`キューブとチャンネル別`uint32`有効標本数を持つ。moving modeでは星固定とMetcalf固定の2キューブを同時に予算化する。`np.ndarray.sort(axis=0)`でキューブをin-placeに並べ、別のcube-size配列を作らずmedian/rankfitを得る。rankfitの中央標本matrix積も16 MiB単位の画素チャンクへ制限し、タイルとは別の巨大なadvanced-index配列を作らない。coverageは従来どおり2次元の実寄与フレーム数であり、0除外時のチャンネル別標本数とは分離する。
- `--median-tile-rows auto|N`を追加した。値`N`の単位は分割数ではなく1タイルに含める縦方向のピクセル行数であり、各タイルは画像の全幅を持つ。例えば高さ1920ピクセルで`N=720`なら720行ずつの3タイルになる。`auto`はavailable RAMの50%を作業キューブ上限とし、`frame_count * channels * width * sizeof(float32) * cube_count`と標本数mapを1行当たり容量として最大行数を選ぶ。RAM不明時は保守的な固定行数を使い、明示値は画像高までに制限して優先する。計画と実績はsummaryの`median_tile_plan`へ記録する。
- `MemoryError`時はまだglobal出力へcommitしていないタイルを破棄し、行数を半分にして同じ`row_start`から再実行する。1行でも確保できなければ、他アプリを閉じるか入力規模/RAMを見直す利用者向けエラーにする。frame workerはglobal出力へ書かないため、worker数低減とタイル行数低減の両方でrollbackを必要としない。
- row tileは`StackCanvas(shape, origin_x, origin_y)`として作り、registration座標系とoutput canvasを混同しない。現行はreference footprintだが、実装は`reference.shape == canvas.shape`を要求せず、将来のexpanded canvasにも同じorigin付きタイルを適用できる。zero-shiftかつ整数originの直接cropだけは、bilinearの4近傍要件で最終行/列を失わない専用経路を持つ。
- 5枚RGB実データでは128行×15タイルと1920行×1タイルの星固定・Metcalf FITSがともに全画素でbit-identicalだった。前者はstack 19.862秒・peak RSS約740 MiB、後者は8.565秒・約854 MiBで、分割を細かくするほどRAMは減り再読込コストが増えることを確認した。
- 247枚RGB moving実データではavailable RAM 8.76 GiBから720行×3タイル、2キューブ合計4.31 GiBを自動選択した。fallback 0、peak RSS 4.98 GiB、stack 215.547秒、pipeline 268.475秒で完走した。全画面2キューブなら約11.5 GiB必要な入力である。詳細は[median row tiling results](developer-tools/stack-performance-analysis/RESULTS-20260901-MEDIAN-TILING.md)を参照する。

## 2026-08-31: Seestar SMBストレージランチャー

- Windows向け`seestar-open-storage.cmd`は、PowerShell実装`seestar-open-storage.ps1`を呼ぶだけの薄いランチャーである。Metcalf Stack本体やPEM通信には依存しない。
- 探索順は、利用者指定、`SEESTAR_HOST`、`seestar.local`/`seestar`のIPv4、ホストモード`10.0.0.1`、代替`192.168.4.1`、ローカル/24のTCP 4700並列探索である。`.local`がIPv6も返す環境でSMBを安定して開くため、UNCには解決済みIPv4を使う。
- 共有パスは既存Seestarツールと同じ`\\<IPv4>\EMMC Images\MyWorks`とした。共有列挙の`net view`は判定に使わず、候補IPv4ごとにTCP 445と直接UNCを確認する。STA側失敗時にもAP側`10.0.0.1`を試す。
- 実機では`seestar.local`から`192.168.0.23`を解決しTCP 4700/445とも到達したが、直接UNCは`UnauthorizedAccess (0x80070005)`となった。管理者権限で確認したWindows SMB設定は`EnableInsecureGuestLogons=False`、`RequireSecuritySignature=False`、`RequireEncryption=False`であり、この環境ではguest拒否が原因と判断できた。`445 open + net view error 53`をSeestar側共有OFFとは解釈しない。
- ヘルパーは有効なSMB設定を読める場合はその値、読めない場合はLanmanWorkstationレジストリを表示する。必要に応じて`EnableInsecureGuestLogons`、`RequireSecuritySignature`、`RequireEncryption`の変更コマンドを案内するが、PC全体のSMBクライアント保護を弱めるため自動変更しない。低レベルSMBライブラリは新依存とWindows実効ポリシーとの差を増やすため、現段階ではTCP・直接UNC・OSポリシーの組み合わせを採用した。
- `-FindOnly`はExplorerやSMBへ触れず、選択したIPv4とUNCを表示する診断・オフラインテスト用経路である。通常利用者はCMDをダブルクリックするか、第一引数へ既知IPを指定する。

## 2026-08-29: 固定天体スタック経路（次期0.9.x）

- 共通CLIへ`--target-mode moving|fixed`を追加し、Windowsの`seestar-fixed-stack.cmd`は`fixed`を付加するだけの薄いランチャーとした。背景補正、保存形式、飽和警告などの既存オプションはそのまま受け渡す。
- 固定モードはHorizons ephemerisとSUN_PA取得を省略する。解いたWCS中心を診断用の一定座標として使い、全フレームのMetcalf追加量を0とする。
- workerは星登録後の配列を固定像へ直接返し、Metcalf用の2回目resampling・accumulation・比較像を作らない。出力は`*_fixed_stack.fit`、表示PNG、既定ONの飽和警告PNG、登録診断CSV、summary JSONである。
- FITSへ`TARGMODE=fixed`、`FIXEDSTK=T`、`STARSTK=T`、`MTSTACK=F`、`NCOMBINE`を記録する。既定は`mean`、`float32`、`quadratic`、飽和警告ONだが、すべて利用者指定を優先する。
- 220P/McNaught実データ2枚を既存Astrometry JSONとSiril登録で処理し、Horizons通信なし、2/2枚採用、Metcalf処理時間0、固定FITS・プレビュー・飽和警告・WCS・固定用summaryの生成を確認した。
- M 33の空白を含む基準フレーム名で、v0.7.1以来`calibrate_single`およびplate solve用`load`/`save`が未引用だった問題を修正した。Siril強制指定の実データ2枚試験で1x scaleのローカル解決と2/2枚の固定スタック完走を確認した。
- `WcsModel.to_fits_header()`はSIPを3次に固定せず、`A/B/AP/BP_ORDER`、各多項式係数、`*_DMAX`を正規表現で動的に継承する。output canvasの変更は`CRPIX1/2`だけを再基準化し、SIPのpixel-minus-CRPIX座標と係数は変更しない。
- 共通FITSメタデータとして`CREATOR`、`SWVER`、`PLTSOLVR`、`TIMESYS=UTC`、`DATE-BEG`、`DATE-END`、`NCOMBINE`を記録する。全採用フレームの露光時間が既知なら、露光中央時刻の露光時間加重平均を`DATE-AVG`と`MJD-AVG`、開始から終了までを`TELAPSE`、露光時間総和を`TOTEXP`へ記録する。1枚でも露光時間が不明なら推定せず、この4項目を省略する。`DATE-END`は最後の採用フレームの`DATE-OBS`にその露光時間を加えた終了時刻であり、基準フレームの`DATE-OBS`と`EXPTIME`は保持する。
- M33のSiril WCS実データで44個のSIPカードを44個すべて最終fixed FITSへ継承した。20秒露光2枚の実行では`TOTEXP=40.0`、`PLTSOLVR=Siril`、`HISTORY Fixed stack generated by Seestar Metcalf Stack v0.9.4`を確認した。
- `run-release-tests.ps1`を追加し、全187ユニットテスト、主要Pythonの構文検査、`git diff --check`、同一WCS除去FITSに対するSiril/Astrometry.net各1回の実ソルブ、両WCS FITSの実体検査を公開前に一括実行する。220P/McNaught実データではSiril 0.47秒、Astrometry.net 24.07秒で成功し、両方のWCS FITSを確認した。ネットワーク依存試験は通常CIへ混ぜず、FITS送信を明示承認するリリース検証に限定する。

## 2026-08-24: v0.9.3 registration matrix統合と1回resampling

- 通常のproduction経路は`moving_target_pipeline.main()`から`moving_target_stack.main()`へ入り、Siril前処理、`register <sequence>_ -2pass`、Python stackの順に進む。`-2pass`は星対応と変換行列を`.seq`へ記録するだけで、`r_*.fit`を生成しない。完全なSharpCap StackLog経路は今回の変更対象外であり、記録されたX/Y/rotationを適用した登録画像を従来どおり一時生成する。
- Sirilの`H`行列はsourceからreferenceへ写す下原点座標系である。配列座標へはsource/referenceそれぞれのY反転行列を使い、`H_array = F_reference @ H_siril @ F_source`として変換する。`-2pass`が自動選択した基準と利用者指定基準が異なる場合は、指定基準の行列の逆行列を左から掛け、全行列を指定基準へ再基準化する。
- 移動天体固定像では、背景星登録行列`H_star`とHorizonsから求めたMetcalf平行移動`T_motion`を`H_output = T_motion @ H_star`として合成する。星固定像は`H_star`、移動天体固定像は`H_output`を、それぞれ同じ前処理済みsourceへ1回だけ適用する。旧版のSiril既定Lanczos-4登録とPython bilinear shiftによる2回補間は行わない。
- 一般affine resamplingはPillowのfloat32 bilinearを使う。Pillowのpixel-center規約に合わせ、逆変換へ`0.5 - 0.5 * row_sum(linear_part)`のtranslation補正を加える。valid maskは逆写像した各出力中心について4近傍すべてがsource範囲内かを独立に計算し、NaN、zero padding、saturation maskへ同じ幾何変換を適用する。pure translationは既存の高速slice経路を維持する。
- `StackCanvas(shape, origin_x, origin_y)`をregistration座標系から独立させ、resamplerはsource shapeとoutput shape/originが異なる場合も処理する。v0.9.3のpolicyは従来どおりreference footprintだが、将来のN枚以上/M%以上のexpanded canvasはcanvas policyだけを差し替えられる。
- cleanup有効時はsource staging後に約0.94 GiB、Siril前処理ピークで約7.48 GiB、前処理cleanup後に約5.61 GiBとなり、登録後も登録FITSを作らないため約5.61 GiBを維持する。採用済み入力を順次削除し、最終的に画像中間ファイルを0へ戻す。220P 242枚では旧推定peak約13.23 GiBから7.48 GiBへ43.5%削減し、登録FITSは242枚から0枚になった。
- 242枚のexact-common-inputでvalid footprint 2,073,600画素は旧版と完全一致した。旧版とのMetcalf対象開口差は半径6画素のRGBで`-0.28%～+0.37%`、半径10画素で`-0.03%～+0.32%`だった。半径14画素では背景環の影響を受け`-1.66%～-0.13%`となるため、測光比較では開口と背景環を固定する。星像中心差は概ね0.03～0.05画素である。旧版との画素完全一致は、補間kernelと回数を意図的に変更したため要件にしない。
- 同じ入力星5個に対する単一変換の開口測光では、新bilinearのRGB平均偏差は`-0.2167%、+0.0478%、-0.0864%`、旧Siril Lanczos-4は`+0.4450%、+0.9127%、+1.0683%`だった。Siril linearとも平均0.09%以内であり、1回bilinearは旧2回補間より入力光量をよく保存する。
- exact 242枚のv0.9.3実測はend-to-end `174.15/197.60/176.11秒`、registration `9.85/10.38/10.08秒`、stack `100.41/111.40/99.38秒`、auto=4、fallback 0、Python peak RSS約1.20～1.23 GiBだった。温キャッシュ比較ではv0.9.0の182.81秒から176.11秒へ3.7%短縮したが、cold/warm filesystem cacheにより全体時間は変動するため速度改善を保証値として扱わない。
- 行列座標、再基準化、正負・整数・小数shift、dx/dy=0、mono/RGB、画像端、valid/saturation mask、NaN/zero padding、独立canvas shape/originを含む全170テストが成功した。詳細と再現用CSVは[RESULTS-20260824-v0.9.3.md](developer-tools/stack-performance-analysis/RESULTS-20260824-v0.9.3.md)を参照する。

## 2026-08-24: v0.9.0 production stack path高速化（履歴）

- production経路は`moving_target_pipeline.main()`から`moving_target_stack.main()`へ入り、Sirilの前処理・Reference registration後、登録済みFITSを背景補正、星固定加算、Metcalf pure translation、移動天体固定加算の順に処理する。従来は背景統計の全フレームpassとスタックpassで登録FITSを2回読んでいたが、fit・適用・スタックを1回の読込へ統合した。
- pure translationはfull-frame座標gridを毎RGB面で作らず、共通のsource/output sliceと一定のbilinear weightを使う。一次・二次背景面の適用も1次元X/Y項と必要な交差項だけで評価し、星固定画像は現行Reference footprintならzero-shift resamplingせず直接加算する。平均加算は`np.add(..., out=..., where=mask)`を使う。
- `StackCanvas(shape, origin_x, origin_y)`はregistration座標系とoutput canvasを分離する。現行のcanvas policyは従来どおりReference frame footprintだが、resampler、valid mask、accumulator、WCS `CRPIX`再基準化は任意shape/originを受け取る。将来のN枚以上/M%以上のexpanded canvasはcanvas policyの追加として実装し、translation本体を置換しない。
- frame workerはFITS読込、背景fit・適用、Metcalf shiftまでを行い、global sum/countへ書かない。main threadは、バッチ内の全workerが成功した後だけlocal resultを入力順に決定的に加算する。既定の`--stack-workers auto`はavailable RAM、source shape、独立したstack canvas shapeから固定配列とworker配列を見積もり、RAMの25%または512 MiBを予約して最大4 workerから`4/2/1`を選ぶ。明示した`--stack-workers 1|2|4`は初期選択に優先する。
- workerで`MemoryError`が起きた場合はpending futureをcancelし、executorを`shutdown(wait=True)`して全worker停止を待つ。成功した兄弟resultを含む未確定バッチ全体とtraceback参照を破棄してから、同じバッチ構成を`4→2→1`で最初から再実行する。バッチ成功前にはglobal accumulatorへ一切反映しないためrollbackは不要で、既に確定した過去バッチも再処理しない。
- summary JSONの`stack_timing_seconds`へ`fits_read`、`background_fit`、`background_apply`、`star_resample`、`star_accumulation`、`metcalf_shift`、`metcalf_accumulation`、`saturation`、`total_stacking_wall`を記録する。worker側項目はCPU時間の合計であり、wall timeとは一致しない。
- cleanup有効時は、前処理成功後にsource copy・変換像・staged calibrationを、登録成功後に最終前処理画像を削除し、各登録FITSは加算と診断行の確定後に削除する。242枚実測ディレクトリ換算では、登録中peakを約13.23 GiBから約11.33 GiBへ、スタック開始時を約5.61 GiBへ下げる。登録世代自体をなくすには、将来Siril `register -2pass`のmatrix統合が必要である。
- 実画像で旧slice前実装とのfull valid mask一致、最大画素差`8.10623e-6 ADU`、中心天体・基準星開口の最大差`1.3e-6 ADU`以下を確認した。最終コードの20枚productionは`1/2/4 worker = 12.35/8.29/6.15秒`で、Metcalf・星固定・左右比較の3出力は全worker条件で全画素一致した。早期cleanup前後も最大差`0 ADU`である。242枚をauto=4で2回処理したstack wall timeは`84.510/73.418秒`、3種類のfloat32 FITSは2回ともSHA-256まで一致した。同じ計測範囲のend-to-end `quadratic`ベンチマークは旧`736.298秒`から新平均`181.470秒`へ約4.06倍高速化した。MemoryErrorとcleanupのfailure injectionを含む全162テストが成功した。詳細は[stack performance results](developer-tools/stack-performance-analysis/RESULTS-20260823.md)を参照する。

## 2026-08-22: FITSプレビュー実験ツール

- `scripts/fits_preview.py`へ表示PNGの伸長・回転処理を集約し、スタッカーと開発者ツールが共有する。科学用FITS、WCS、スタック配列を変更する処理は置かない。
- `developer-tools/fits-preview/create_fits_preview.py`は1枚のFITSを同じ既定の`-1 sigma`〜`+3 sigma`伸長でPNGへ変換する最小CLIである。N/E方位マーカーや太陽方向などの表示専用オーバーレイはこの場所で実験する。
- `--preview-at UL|UR|LL|LR`では、注釈を合成した表示PNGに加え、`*_annotation_overlay.png`をRGBAで出力する。既定は`UL`で、`none`なら注釈を作らない。後者は`--annotate-size`の半径で描いた小型の独立スプライトで、角指定に依存しない。合成先の配置は利用者側が自由に決める。描画ロジックは`_draw_annotation()`だけに置き、合成PNGと透過PNGで方位・太陽方向・線幅がずれないようにする。

## 2026-08-20: 時間変化する背景の面モデル補正（開発中）

- `--background-normalization`に`plane`と`quadratic`を追加した。`offset`、`plane`、`quadratic`の全モードで、各フレームの推定背景を演算中に完全に差し引いて0付近へ置く。スタック後には、採用フレームのRGB局所DC値を算術平均した一定値だけを加える。これは負値を保存形式へ渡さないための表現レンジ対策であり、共通背景面ではない。
- 面モデルは登録済みの有効画素を50x50タイルへ分割し、各タイルを4画素間隔でサンプリングしてRGBごとのsigma-clipped medianを得る。全画素をタイルごとにソートすると長時間観測で実用的でないため、面の大域形状を保てるこのサンプリングを採用した。
- 一次は`[1, x, y]`、二次は`[1, x, y, x^2, xy, y^2]`を、画像中心を原点とする`[-1, 1]`正規化座標で重み付き最小二乗フィットする。初回残差のmedian/MADから3 sigmaを超えるタイルを一回だけ除外して再フィットする。反復乱数・手作業の閾値調整は用いない。
- 2026-08-21に220P McNaughtの最新セッション（247入力、242採用）で速度測定を行った。背景補正なしのウォームキャッシュ基準579.40秒に対して、`offset`は713.34秒、`plane`は729.83秒、`quadratic`は736.30秒だった。面モデルだけの`offset`からの増分は約16〜23秒である。詳細は`developer-tools/background-normalization-benchmark/RESULTS-20260821.md`を参照。
- 各フレームは自身のfit面を有効画素だけから完全に差し引き、背景を0付近にする。最終画像へは採用フレームのRGB局所DC値の算術平均だけを一定値として加える。これは符号付き演算結果を保存形式へ収めるrange safeguardであり、傾斜面を戻す処理ではない。
- `BGCn_m`、`BGTILER`、`BGTILEC`、`BGTSTEP`、`BGOUTSIG`を出力FITSへ、モデル係数・除外タイル数・残差RMSをshifts CSVへ記録する。
- 通常の小天体向けであり、視野の大半を占める彗星やDSOを保護する対象マスクは実装しない。そのような対象では`none`または`offset`を選ぶ。
- 合成面と単一の破損タイルを使う単体試験、および220P/McNaughtの20枚実写で`plane`/`quadratic`各20/20枚の完走、出力FITSヘッダー、shifts CSVを確認した。

## 2026-08-21: v0.8.0の既定値

- 実写220P/McNaughtで、星を外れ値として除いたロバストなプレビューσ伸長は背景ノイズと色むらを過大に表示した。プレビューの`--preview-stretch sigma`は、星を残した各RGBの単純平均・標準偏差による`-1σ`〜`+3σ`を既定とする。ロバストなsigma-clipped median/MADは背景面モデルだけで使う。
- 高度変化に伴う視野端の明暗差を避ける目的と220P実写の比較結果を踏まえ、`--background-normalization quadratic`を既定とする。視野の大半を占める天体には`none`または`offset`を利用者が明示する。
- `--preview-north-up`はWCSで表示PNGだけを北上に回転する。FITSデータ、WCS、通常プレビューを変えないため、科学データと表示用画像の役割を分けられる。

## 2026-08-22: north-up PNG の座標規約修正（v0.8.1 再公開）

- `WcsModel.world_to_pixel()`は、WCSのYをスタック用NumPy配列の行座標へそのまま返す。通常プレビューも同じ配列を上下反転せずにPNGへ渡すため、north-up計算もこのYを反転してはならない。
- Pillowの`Image.rotate(+angle)`は画面上で反時計回りに回転する。そのため、表示座標での北ベクトル角を`current`とすると、北を上 `-90°` に置くために渡す回転角は`current + 90°`である。
- 220P McNaughtの実WCS（CD行列）では北上回転は`-50.8207789°`となる。通常プレビューで右上に伸びる尾は、この回転後に右下へ向く。この実WCSを回帰テストに追加する。

## 2026-08-18: 外部サービス障害の再試行とエラー保持（v0.7.3）

- VizieRのHTTP 503は画像・画角の失敗と区別し、同じ倍率を3回まで指数バックオフで再試行する。HTTP 400や星照合不成立ではこの再試行を行わない。
- 近距離倍率探索の全条件が503で尽き、Astrometry.net APIキーもない場合は、無意味な広域探索へ進まずAPIキー設定方法を示して終了する。
- Astrometry.netの全HTTP処理、Horizons ephemeris、SBDB名称検索は一時的な通信失敗を再試行し、最終例外にサービス名と試行回数を残す。
- `run()`は子プロセスのstdout/stderrをリアルタイム表示しながら蓄積する。非ゼロ終了時は`CalledProcessError.output`へ保持し、子の`ERROR:`を最終的な利用者向けメッセージへ引き継ぐ。
- Windows CMDは異常終了時だけ`pause`する。処理本体や内部子プロセスへpauseを入れてはならない。
- ベンチマークの`cold-each`は通常のSirilキャッシュから隔離した短い一時ディレクトリに`.config/siril/download_cache`等を作り、試行後に削除する。結果フォルダ配下はWindowsのパス長制限に達するため使用しない。
- 10P基準FITSの1.00倍を空キャッシュから実測し、VizieRからNOMAD 1898星を取得して4.92秒で解決した。
- EXEビルド用PyInstallerはバージョン付き隔離先へ固定し、既存`--target`を`pip --upgrade`で再帰削除して長時間停止する経路を避ける。

## 2026-08-17: Siril前処理・Plate Solve優先経路（実装済み、0.7.x）

基準フレームのプレートソルブを高速化し、Astrometry.netのAPIやネットワークが利用できない場合にも復旧できるよう、次の順序で実装した。

1. FITSヘッダまたは`--pixel-scale-arcsec`から画角を得られる場合は、その値から換算した焦点距離でSiril内蔵ソルバーを最初に実行する。
2. Sirilが検出した星数が`--registration-minpairs`未満なら、画角を変えても後続の位置合わせに使えないため、必要数と検出数を示して終了する。
3. 星数が足りており、失敗理由がカタログ星との位置合わせ不成立なら、指定焦点距離に次の倍率を掛けて近距離探索する。

   ```text
   1.00, 0.70, 1.40, 0.50, 2.00
   ```

4. 近距離探索でも解けなければAstrometry.netへフォールバックする。概略中心座標は使うが、画角は制限しないか十分広い範囲を与え、誤ったFITS画角に再び拘束されないようにする。
5. APIキー欠落、認証失敗、通信障害、サービス障害などでAstrometry.netを利用できない場合に限り、Siril探索を次の順で拡張する。

   ```text
   1.00, 0.70, 1.40, 0.50, 2.00, 0.35, 2.80, 0.25, 4.00
   ```

   この系列は、実際の焦点距離がヘッダ値の約0.21～5.0倍にある範囲を、Sirilの実測許容幅を考慮してほぼ隙間なく覆う。これを超えた場合は探索を続けず、焦点距離、ピクセルピッチ、ビニング、中心座標の確認と明示指定を求める。

倍率系列は等比約1.4倍とする。2026-08-16にSiril 1.4.1とSharpCapの10P FITSで測定したところ、正しいピクセル画角の0.84～1.25倍で3/3回成功し、0.83倍と1.26倍では0/3回だった。焦点距離に直すと約0.80～1.19倍であり、概ね±20%の照合許容幅に相当する。隣接試行倍率の比を理論上の上限`1.2 / 0.8 = 1.5`未満にし、画像差に備えて1.4とした。`0.41, 0.65, 1.00`のような系列は隣接比が1.5を超える箇所があり、探索漏れを作るため採用しない。

失敗は少なくとも次に分類し、利用者へ別の復旧方法を示す。

- 検出星不足: 画角再試行をせず終了し、基準フレーム変更や不良フレーム除外を案内する。
- 星数十分・カタログ星との照合失敗: 焦点距離倍率探索を行う。
- 星表取得失敗: 同じ倍率の反復ではなく、通信・CA証明書・Sirilキャッシュを確認する。
- Astrometry.net API利用不能: 理由を認証、通信、サーバー応答に分け、Siril広域探索へ移る。
- 中心座標不良の疑い: Sirilの画角探索では直らないことを明示する。Astrometry.netも利用不能ならRA/Decの明示指定を求める。

Sirilのオンライン部分星表は中心座標、画角、限界等級に応じてディスクキャッシュされる。通常実行では自動再利用し、同じ候補を再取得しない。検証にはソースリポジトリの`developer-tools/plate-solve-benchmark/run-siril-scale-tolerance.cmd`を使い、毎回の初回取得を測る場合だけ`--siril-cache-mode cold-each`で通常キャッシュから隔離する。実測CSVは配布物へ含めず、設計根拠は[プレートソルブ・ベンチマーク](https://github.com/nisikazu/seestar-metcalf-stack/tree/main/developer-tools/plate-solve-benchmark)に記録する。

SharpCap入力では`*.CameraSettings.txt`を正規化して`PreprocessingPlan`を作る。master dark/flatはCLI明示を最優先し、次にCameraSettingsの記録パス、移動後はbasenameと近隣`darks`/`flats`を探索する。要求されたmasterが見つからない場合は未補正で続行せず停止する。`Hot Pixel Sensitivity != 0`はホット補正ONの信号としてのみ使い、SharpCapの尺度をSiril sigmaへ換算しない。既定sigmaは3である。

全フレームをSirilでdark/flat・cosmetic correction後にデベイヤする。SharpCap StackLogの変換が完全なら、デベイヤ済み画像へ記録済みX/Y/rotationを適用し、Sirilの星登録だけを省略する。masterファイルは`registration_images/calibration`へ分離し、Sirilのライト列`convert`へ混入させない。

2026-08-17にSharpCap 4.1.13800.0の10P RAW16 FITS 3枚で統合確認した。Sirilはライト3枚だけを変換し、master dark subtraction、master-dark由来のhot pixel 1314点補正、RGGBデベイヤを実施した。続いてStackLog位置合わせで3/3枚を採用し、メトカーフ、星固定、左右比較のFITS/PNGを生成した。基準フレームは同データでSiril 1.4.1により約0.52秒で解決できた。

## 2026-08-13: SharpCap Live Stackログによる星登録省略

- `stacklog.csv`が入力フォルダ内またはその1つ上にある場合、SharpCap 4.1.10745以降のLive Stackセッションとして自動認識する。raw frameフォルダ名を`rawframes`へ固定しない。
- CSVは列番号ではなくヘッダ名で読み、`Raw frame file`のbasenameをコピー先へ照合する。入力フォルダ内の同名画像をCSVの旧絶対パスより優先し、複数候補はエラーにする。既定では`Frame Stacked? = 1`だけを採用する。
- FITSは`DATE-AVG`、次に`DATE-OBS + EXPTIME/2`を露光中央時刻に使う。PNG/TIFFはStackLog時刻からCameraSettingsのExposure/2を引く。
- `LiveStack.AlignFrames=True`かつ全採用行にX/Y offsetとrotationがある場合、Sirilで補正・デベイヤした画像へPythonが中心回転・平行移動を適用して登録FITSを生成する。Sirilの星登録は起動しない。
- SharpCap offsetは実データとの相関比較で、そのまま画像へ加える符号が基準像と一致することを確認した。任意基準フレームではStackLog基準からの相対変換へ合成する。
- offset不完全、alignment OFF、失敗行を明示的に含める場合は従来Siril経路へフォールバックする。
- 非FITSのSharpCap入力では、対象を`--horizons-object`/`--horizons-command`/既存CSVのいずれかで、画素スケールを`--pixel-scale-arcsec`で明示させる。観測地は任意で、欠落時はgeocenterを使う。
- 下処理済み画像へ差し替える場合もStackLog変換を有効に保つため、ファイル名、寸法、向き、切り抜き範囲を維持する。CameraSettingsはバージョン検査とPNG/TIFFのExposure補正に必要である。
- 初期実装ではPythonデベイヤでSiril全体を省略したが、0.7.xで補正とデベイヤをSirilへ統一した。StackLog変換の利用条件と3/3枚スタック結果は維持されている。
- 時刻仕様・バージョン差・parser検証項目は`SHARPCAP-TIMESTAMPS.md`を正本とする。

## 2026-08-04: 登録失敗時の全フレーム診断

- Sirilの並列登録ログでは`Found N stars`行に画像番号がなく、登録が全面失敗すると`.seq`にも品質情報が残らないため、ログだけから星数を各FITSへ対応付けることはできない。
- 登録失敗時だけ、作成済みのデベイヤ画像へSiril `findstar`を逐次実行する。Horizons、Astrometry.net、コピー、デベイヤは再実行しない。
- 各フレームの検出星数、FWHM、星ごとのFWHMx/FWHMyから求めたroundness中央値を`registration_diagnostics.csv`へ保存する。一時星カタログは解析後に削除する。
- 169P/NEATの28枚で全項目が28/28枚取得でき、追加診断は約1.55秒だった。

## 2026-07-31: 基準フレームと登録失敗の扱い

- 実行前に予見不能なフィルタ後の番号指定 `--reference-frame-index` を廃止し、`--reference-frame-file` で選択済みセッション内のFITSファイル名を指定する方式へ変更した。
- 基準フレームが `--registration-minpairs` を満たさない場合は、検出星数と必要数を表示して停止する。基準が崩れるとWCS・星位置合わせ・移動補正の座標基準がすべて不正になるためである。
- 非基準フレームの登録失敗は雲・遮蔽物・導入ずれで通常に起こる。失敗理由を `*_shifts.csv` に残し、当該フレームだけを除外してスタックする。
- 完了時に `Stacked used/total frames; skipped excluded` を表示する。`used_frames`、出力名、summary JSONも実使用枚数を記録する。
- Sirilが登録途中で非ゼロ終了した場合も、出力の `Found N stars in reference` を解析して基準星不足を明示する。
- CLIではPython Tracebackを直接表示せず、原因と復旧操作を示す短い`Error:`を表示する。Tracebackは公開版ランチャーの実行ログだけに残し、子処理の`ERROR:`を親処理が一度だけ表示する。

## 2026-08-05: 一般FITSとSharpCap対応

- 入力の既定パターンを`*.fit*`とし、実際には拡張子が`.fit`または`.fits`の通常ファイルだけを採用する。これによりSharpCapの`.fits`を扱いつつ、`.fits.invalid`などを除外する。
- Horizonsの観測地はCLIの`--site-longitude`/`--site-latitude`を優先し、次にFITSの`SITELONG`/`SITELAT`を使う。どちらもない場合はgeocenterへフォールバックする。
- Astrometry.netへ送る画素スケールはFITSの焦点距離・画素サイズから推定し、欠落時は`--pixel-scale-arcsec`を使う。Astrometry側の検索範囲だけに使い、画像の実データを変換しない。
- WCSダウンロードは`SIMPLE`と`END`カードを確認してから保存する。HTMLログインページなどが返った場合も、JSON calibrationで処理を継続できる。

最終更新: 2026-08-24

この文書は、Seestar Metcalf Stackを改造する人、保守する人、または開発を
引き継ぐ人のための技術記録です。一般利用者向けの操作方法は`README.md`、
macOSの導入方法は`README-macOS.md`、リリース作業は[GitHub上のPUBLISHING.md](https://github.com/nisikazu/seestar-metcalf-stack/blob/main/PUBLISHING.md)を参照して
ください。

## 現在の状態

- 最新の公開Releaseは`v0.7.3`です。
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

- Siril: dark/flat・hot/cold pixel補正、デベイヤ、基準フレームのPlate Solve、必要時の背景星登録を行う
- Astrometry.net: Sirilで解決できない場合だけ基準フレームのWCSを得る
- JPL Horizons: 各露光時刻の天体の赤経・赤緯を取得する
- Python/NumPy: 天体移動量を画素移動へ変換し、最終的な画素結合とFITS出力を行う

Sirilは最終スタックを行いません。Sirilが生成した星位置合わせ済みフレームを
Pythonが読み、メトカーフスタックと星固定スタックを同じ画素結合方式で作ります。

## 処理の流れ

1. FITSの`DATE-OBS`を読み、既定では60分を超える空白でセッションを分割します。
2. 指定がなければ最新セッションを選びます。ファイル名に`_failed_`を含む
   フレームは既定で除外します。
3. FITSの`OBJECT`からHorizons検索候補を作り、各フレーム時刻のtopocentric座標を
   取得します。明示CSV、COMMAND、天体名の上書きもできます。
4. SharpCap入力ではCameraSettingsから補正計画を作り、Sirilで基準フレームを補正・デベイヤします。
5. Sirilで基準フレームをPlate Solveします。近距離画角探索で解けなければAstrometry.netへフォールバックし、それも利用不能ならSirilの画角探索を拡張します。
6. Sirilで全CFA画像を同じ設定で補正・デベイヤします。StackLog変換が完全ならそのX/Y/rotationを使い、不完全ならSirilの`similarity`登録で平行移動、回転、等方倍率を推定します。
7. WCSと各時刻の天体座標から、基準フレームに対する天体の追加移動量を求めます。
8. 登録済みフレームを双線形補間でサブピクセル移動し、メトカーフ基準と
   星固定基準を同じ結合方式でスタックします。
9. 線形FITS、表示用PNG、フレーム別シフトCSV、処理要約JSON、ログを出力します。
10. 成功時は大きな中間FITSを削除し、成果物フォルダを開きます。

## 主要ファイル

| ファイル | 責務 |
| --- | --- |
| `scripts/moving_target_pipeline.py` | 引数、セッション選択、作業ディレクトリ、外部処理の順序、ログ、キャッシュ、EXE内部ディスパッチ |
| `scripts/horizons_ephemeris.py` | FITS時刻・観測地の読出し、天体名候補、SBDBフォールバック、Horizons CSV生成 |
| `scripts/astrometry_solve.py` | APIログイン、FITS送信、再試行、ジョブ待機、WCS/JSON取得、submission再開 |
| `scripts/siril_preprocessing.py` | CameraSettingsとCLIから補正計画を解決し、master配置とSiril補正・デベイヤscriptを生成 |
| `scripts/moving_target_stack.py` | FITS入出力、WCS、Siril登録、画素シフト、平均・メジアン・ランクフィット、成果物生成 |
| `tests/` | 名称解決、セッション、スタック方式、プレビュー、キャッシュ、Siril失敗判定、OS差、開発ツールの単体テスト |
| `developer-tools/plate-solve-benchmark/` | Astrometry.netとSirilのPlate Solve性能・画角許容範囲を測る開発専用ツール。Release ZIPには含めない |
| `build-release-packages.ps1` | 通常版とSiril同梱版の生成・内容検証・SHA-256作成 |
| `release-package-manifest.psd1` | 両配布ZIPに含めるファイルとSiril検証条件の唯一の定義 |
| `verify-release-packages.ps1` | 完成したZIPを開き直して配布範囲とSiril実体を検証 |
| `build-seestar-metcalf-stack-exe.ps1` | PyInstallerによるWindows one-file EXE作成 |

`developer-tools/legacy/README-Siril-CLI.md`は開発初期の実験記録です。現在は存在しない補助スクリプトや
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

- `mean`: 有効画素ごとの算術平均。加算は`float64`、寄与枚数は`uint32`です。デフォルトの`--padding-policy valid`ではSiril登録後の全チャンネル0 paddingを検出し、メトカーフの小数画素シフトでは補間4近傍がすべて有効な画素だけを整数countへ加えます。`legacy`は旧挙動との比較用です。
- `median`: 画素ごとの中央値。登録・シフトで生じる厳密な0をデフォルトで欠損として除外します。`--zero-sample-policy include`で0を母集団へ戻せますが、低重複領域ではpaddingの0が中央値となって真っ黒な領域を作るため、旧版との比較以外には非推奨です。
- `rankfit`: 0を除外して明るさ順に並べ、中央の指定割合へ5次多項式を当て、
  順位中央の値を返します。標本が少ない画素は中央値へフォールバックします。

メジアンとランクフィットのproduction経路は、背景面fitとフレーム評価を先に確定し、
全幅の行タイルごとにbounded RAM cubeを構築します。全画面×全フレームのmemmapは
作りません。小さい`--median-tile-rows`はRAMを減らす代わりにFITS再読込回数を増やします。
プレビューPNGは非線形な表示用ストレッチであり、測光には使えません。

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
| 開発中 / 2026-09-01 | median/rankfitを全幅行タイル方式へ変更。available RAMの約半分を上限に自動分割し、全フレーム×全画面キューブを不要化。明示行数、MemoryError時の未確定タイル再試行、計画・実測ログを追加 |
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
| v0.5.4 / 2026-08-02 | 有効画素だけを画素別枚数で正規化する平均スタック、登録診断CSV、利用者向けエラー表示を追加 |
| v0.5.5 / 2026-08-04 | 登録が全面失敗した場合も全デベイヤ画像を個別に星検出し、診断CSVへ検出星数、FWHM、roundnessを残す処理を追加 |
| v0.6.0 / 2026-08-05 | SharpCapなどの一般FITS対応、観測地と画素スケールのCLI指定、geocenterフォールバック、Astrometry.net不正WCS応答の検証を追加 |

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
- 平均の有効画素countはuint32です。
- 実機データで主に確認したのはSeestar S30です。S50、S30 Pro、将来firmwareが
  出力するFITSカードや画像形状は追加検証が必要です。
- FITSの上下方向は表示ソフトによって見え方が異なります。内部配列を既定では反転
  せず、通常プレビューもスタック配列の向きをそのまま使います。

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
5. `*_shifts.csv`、`*_registration_diagnostics.csv`、`*_summary.json`のフレーム数、時刻、基準フレームが一致する
6. uint16 `none`とfloat32の画素値が想定したADU関係を保つ
7. 成功時cleanupと`--no-cleanup`の両方を確認する
8. 通信中断後にAstrometry submissionを再開できる
9. `--saturation-warning enable`で専用PNGだけが生成され、通常PNGとFITSが変わらない
10. `*_shifts.csv`とsummary JSONの飽和件数が、確認した元フレームと一致する
11. 登録診断CSVのFWHM・weighted FWHM・roundness・検出星数がSiril `.seq`と一致し、対応星数がSirilログと一致する
12. slice translationの正負・整数/小数・片軸0、mono/RGB、画像端、NaN/0 padding、valid/saturation maskを旧実装と比較する
13. `--stack-workers 1|2|4`の出力FITSが全画素一致し、summaryのoperation timingとworker数が一致する
14. 非Reference shape/originの`StackCanvas`で画素配置、valid mask、WCS `CRPIX`再基準化が一致する
15. median/rankfitの全画面と複数行タイルで画素、coverage、valid mask、saturation maskが一致し、タイルMemoryError再試行で未確定結果が混ざらない

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
powershell -NoProfile -ExecutionPolicy Bypass -File .\build-release-packages.ps1 -Version X.Y.Z
```

両パッケージには利用者向け文書、`DEVELOPMENT.md`、実行・セットアップ・再構築に必要なファイルだけを含めます。`developer-tools/`、`tests/`、`.github/`、`PUBLISHING.md`、パッケージ生成・検証スクリプトはソースリポジトリだけに置きます。
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
