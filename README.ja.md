[English](README.md) | **日本語**

# oscilloscope-mcp

AIエージェント（Claude Code / Cursor / 自作）が、実機のベンチオシロスコープを
ネットワーク経由で操作するための MCP サーバーです。

使い方は2通りあります。

**1. 逐次利用（あなたが依頼し、AIがオシロを操作する）。**
本体のつまみを回したりメニューを辿ったりする代わりに、観たいことを自然言語で伝えます。
「*CH1の立下り2回目でトリガー*」「*CH1〜CH3を見せて*」「*周波数は?*」。AIがそれを実機に
設定し、読み戻して、その場で操作できる波形ビューを返します。

**2. AIエージェントに開発を任せたときのフィードバックとして。**
FPGA の開発を AIエージェントに任せたとき、実機での動作確認のフィードバックに使えます。
エージェントが実機の信号を取得し、その仕様が RTL シミュレーションと一致しているかを**自動で**
確認します（人間がオシロを目視して *「RTL sim PASS / HW FAIL」* を切り分ける作業を、AIが
代わりに行います）。
また、さまざまな機器の状態を AI に入力し、それをもとに何かを行わせる用途にも使えます。

結果は用途に合った形で返ります。エージェントがそのまま次の入力に戻せる**構造化データ**
（測定値や注意点、エッジ / ラン列）、人間がさっと確認できる **PNG 画像**、測定したサンプルを
ブラウザ画面に描画する**自己完結型の HTML ビューア**の3つです。

*オシロの制御は内部で標準の SCPI インターフェース越しに行いますが、利用者が SCPI を書くことも、
その存在を知ることもありません。*

![4チャンネル取得デモ。エージェント駆動の設定と RIGOL DS1104Z のスクリーンショット](docs/images/hero-4ch-stacked-demo.png)

## このMCPの特徴

- **機器の能力をまるごとエージェントに見せます。薄く包んだだけの道具ではありません。**
  能力の全体（例：DS1000Z の*全15種*のトリガーと約95個のパラメータ）を機械可読な
  プロファイルとして宣言しています。公式プログラミングガイドから定義し、実機で検証しました。
  エージェントは *「ここで何を設定できる?」* と問い合わせれば、実際に設定できるパラメータ一式が
  返ります。ツール作者がたまたま想定した数個の操作に縛られません。

- **自然言語で入れて、検証済みの設定が出ます。** すべての依頼は機器が実際に対応する範囲に
  照合され、対応しない設定はハードウェアに届く*前に*はじかれます。おかしな依頼は黙って
  素通りせず、はっきり失敗します。

- **すべての操作は、再現できる型付きコマンドでもあります。** 逐次で依頼したのと同じことを、
  スクリプトにも共有にも CI 実行にも回せます。

- **構造化された事実と、人間が確認できるスクリーンショット。** エージェントは数値と注意点
  （`caveats[]`）を抽出し、PNG スクリーンショットを人間レビュー用に返します。中で何をしたかは
  いつでも確認できます。

- **自己完結型の HTML ビューア。** 単一ファイル、依存ゼロ、どのブラウザでも開くだけ。
  オシロから測定した RAW サンプルを取得し、ブラウザ画面に描画します（最大でオシロのバッファ
  容量まで）。操作系はオシロと同じで、チャンネル別 V/div、GND のドラッグ移動、サンプル点に
  吸い付くカーソル、CH の表示 ON/OFF を備えます。

- **sim と実機の差分。** リファレンスのエッジ列（例：RTL シミュレーションの出力）を渡すと、
  オシロが実測した結果と突き合わせ、構造化した *added / missing / shifted*（追加 / 欠落 / ずれ）
  エッジを返します。

- **プラグイン設計。** 新しいメーカーや機種は2ファイルで足せます。SCPI 方言のドライバと、能力を
  書いたプロファイル YAML です。能力はコードではなく宣言データです。

- **テスト済み。** 約560のユニットテストに加え、実機に対するライブ conformance テスト。

> ドライバは **RIGOL DS1000Z シリーズ**に対応。実機検証済みは今のところ **DS1104Z** のみです。
> 他の機種は同じコードパス + プロファイルで動きますが、conformance テストは未実施です。

## インストール

```bash
git clone https://github.com/masahiro-999/oscilloscope-mcp.git
cd oscilloscope-mcp
pip install -e .
```

## MCP サーバーとして起動

```bash
# オシロのネットワークアドレスを設定。
export SCOPE_MCP_HOST=<your-scope-ip>     # 例: 192.168.1.42

# サーバー起動（既定は stdio トランスポート）。
python -m oscilloscope_mcp

# もしくはコンソールスクリプト。
oscilloscope-mcp
```

## Claude Code への登録

```bash
claude mcp add oscilloscope -- python -m oscilloscope_mcp
```

これで、このMCPサーバーに接続したセッションから
`scope_query` / `scope_screenshot` / `scope_trigger` / `scope_measure` /
`scope_measure_stat` / `scope_channel` / `scope_timebase` / `scope_waveform` /
`scope_acquire` / `scope_capture` / `scope_compare` / `scope_viewer` の各ツールが
使えるようになります。

## 提供ツール

| MCP ツール | 役割 |
|---|---|
| `scope_query` | 生 SCPI コマンドのパススルー。応答に加え、この経路では観測限界が自動チェックされない旨の `caveats[]` 警告を返す。 |
| `scope_screenshot` | 現在のオシロ画面の PNG を人間レビュー用に保存。任意で手動カーソル（Δt / ΔV）やチャンネルラベル（DIR / STP / NXT / CLK）を配置して画像を読みやすくできる。 |
| `scope_trigger` | トリガーの読み出しと設定。機器が対応する**全**トリガー種別を扱う。引数なしで呼ぶと現在状態を**読み出す**（`mode`、大域設定、そのモードの `params`、設定可能項目を示す `accepted_params` を返す）。**設定**するには `mode` および/または大域の `sweep` / `coupling` / `holdoff_s` を渡し、種別固有のパラメータはすべて `params` オブジェクトに入れる（例：Nth-edge トリガーなら `{"source":"CHAN1","slope":"NEG","nth_edge":2,"idle_s":2e-3}`）。`mode` はプロファイルの全種別リスト（DS1000Z の標準6種 + オプションライセンス9種）に照合して検証し、各 `params` のキー/値もそのモードのスキーマ（列挙値・数値範囲・チャンネルソース）に照合して検証する。非対応の種別/パラメータは有効リスト付きで拒否、オプションライセンス種別は `caveats[]` 警告を出す。`action` を渡すと取得を制御する。`RUN` / `STOP` / `SINGLE`（単発アーム）/ `FORCE`（強制トリガ。NORMAL/SINGLE sweep のみ）。`status`（TD/WAIT/RUN/AUTO/STOP）に結果が反映される。 |
| `scope_measure` | オシロ側の自動測定（`:MEAS:ITEM?`）を `source` チャンネルの `items` リストについて読み出す。単一ソース項目：`VMAX VMIN VPP VTOP VBASE VAMP VAVG VRMS OVERSHOOT PRESHOOT PERIOD FREQUENCY RTIME FTIME PWIDTH NWIDTH PDUTY NDUTY`、2ソース項目（`source2` 必須）：`RDELAY FDELAY RPHASE FPHASE`。測定不能値は caveat 付きで `null` を返す。 |
| `scope_measure_stat` | 測定**統計**（`:MEASure:STATistic:ITEM`）を1つ以上の項目について読み出す。単一の瞬時値ではなく、累積された current/max/min/average/deviation/count。`stat_types` は既定で全6種（`CURRent MAXimum MINimum AVERages DEViation COUNt`）、部分指定でクエリを絞れる。`mode` で統計モード（`DIFFerence` / `EXTRemum`）を任意設定。`reset=True` でクエリ前に累積データをクリア。`{source, statistics: [{item, current, max, min, avg, dev, count}], caveats}` を返す。 |
| `scope_channel` | アナログチャンネルの**垂直**設定の読み出し / 設定。`channel`（1始まり）は必須。`scale_v_per_div`, `offset_v`, `coupling`（AC/DC/GND）, `display`, `probe`, `bw_limit`（20M/OFF）, `invert`, `units` の任意サブセットを渡す。プロファイルに照合して検証、20MHz 帯域制限時は caveat を出す。 |
| `scope_timebase` | **水平**（タイムベース）設定の読み出し / 設定。`s_per_div`, `offset_s`, `mode`（MAIN/XY/ROLL）の任意サブセットを渡す。プロファイルに照合して検証、長いタイムベースではメモリ降格 caveat を出す。 |
| `scope_waveform` | 1チャンネルを取得し、生 ADC サンプルではなくコンパクトな**しきい値量子化イベント列**を返す。生電圧をシュミットトリガコンパレータ（`threshold_v` ± `hysteresis_v`/2）に通して 0/1 ストリームにし、ランレングス符号化で `runs` `[(t_us, level, dur_us)]` と `edges` `[(t_us, channel, RISE/FALL)]` にする（時刻は µs、トリガが t=0）。ヒステリシスは必須。ノイズのあるエッジに単一しきい値だと無用なマイクロパルスに分裂する。`hysteresis_v` が信号 peak-to-peak の 20% を超えると検出ミスの `caveats[]` 警告、予算超過のストリームは caveat 付きで切り詰める。引数：`source`（既定 `CHAN1`）、`mode`（`NORMal` または `RAW`。RAW は ~1200点の画面間引きではなく**全取得メモリ**を最大24M点まで読む。事前にオシロを停止すること）、`threshold_v`（1.5）、`hysteresis_v`（0.1）。 |
| `scope_acquire` | **取得モード**の読み出しと設定。`type`（NORMal/AVERages/PEAK/HRESolution）、`averages`（2..1024 の2の冪）、`memory_depth`（AUTO または数値、有効チャンネル数に応じて検証）。引数なしで**読み出す**（type, averages, memory_depth, sample_rate を返す）。AVERages 選択時は更新レートが下がる旨の `caveats[]` 警告を出す。 |
| `scope_capture` | **宣言的シングルショット取得**。設定・アーム・待機・読み出しを1コールで。trigger/channel/timebase/acquire 設定を束ね、`:SINGle` でアームし、トリガまで（または `timeout_s` まで）ポーリングし、チャンネル毎の波形（`runs`/`edges`/`bus_runs` に量子化）、オシロ側 `measurements`、任意のスクリーンショットを読む。`sweep='AUTO'` または `'NORMAL'` でアーム+ポーリングを省略し現在の画面メモリを即読み。`waveform_mode='RAW'` でトリガ後に全取得メモリを読む（SINGLE 後はオシロは既に停止状態）。`{triggered, channels, waveform, bus_runs, measurements, screenshot_path, caveats}` を返す。 |
| `scope_compare` | **sim と実機のリファレンス差分**。プロジェクトの中核目標。RTL シミュレータからの golden なエッジ/run リストを取り、実機（ライブ取得または与えたデータ）と突き合わせる。`{matched, shifted[{ref_t_us, hw_t_us, delta_us, kind}], missing[{t_us, kind}], added[{t_us, kind}], first_divergence_us, summary, caveats}` を返す。2モード：*live*（`hw_runs`/`hw_edges` 未指定ならオシロから取得）または *offline*（取得済みデータを直接渡す。オシロ接続不要）。`tolerance_us`（既定 0.05 = 50ns）で同種2エッジが一致とみなされる近さを設定。信号非依存：SPI / I2C / UART / ULPI など任意のデジタル信号で動く。 |
| `scope_viewer` | **自己完結型の HTML** 波形ビューアを生成。RAW 波形データを読み（オシロは停止状態が必要）、測定したアナログ電圧サンプルを HTML ファイルに埋め込む。機能：チャンネル別 ON/OFF トグル、チャンネル別 V/div ドロップダウン（10mV〜100V）、ドラッグ可能な GND オフセットマーカー（▶）、スクロールズームで 1-2-5 自動ステップする T/div ドロップダウン、ドラッグでパン、サンプル点にスナップする縦カーソルと固定電圧リードアウト、トリガ位置マーカー（t=0 に ▼T、`:TRIG:POS?` から導出）。`depth` でトリガを中心とした観測窓を制御：`"low"`（30k点、~120µs、転送 ~1秒）、`"mid"`（300k、~1.2ms、~3秒）、`"high"`（3M+、~12ms、~17秒）。深いほど転送が遅くなるため、通常はトリガ前後の一部を取得します（上限はオシロのバッファ容量）。ブラウザはズームアウト時にピクセル単位 min/max エンベロープで数百万点を描画し、ズームイン時は個々のサンプル + ドットを描画。 |

## 構成

```
oscilloscope-mcp/                 # リポジトリ（kebab-case）
├── README.md
├── LICENSE
├── pyproject.toml
├── src/oscilloscope_mcp/         # Python パッケージ（snake_case）
│   ├── __init__.py
│   ├── __main__.py           # `python -m oscilloscope_mcp` エントリ
│   ├── server.py             # FastMCP サーバー：ツール登録 + ディスパッチ
│   ├── transport/
│   │   └── scpi_lan.py       # 生 TCP SCPI クライアント（サードパーティ依存ゼロ）
│   ├── instruments/
│   │   ├── _base.py          # Scope ABC、メーカー非依存インターフェース
│   │   ├── __init__.py       # MODEL_REGISTRY + open_scope() ディスパッチ
│   │   ├── rigol_ds1000z.py  # RIGOL DS1054Z / DS1104Z ドライバ
│   │   └── profiles/
│   │       ├── _ds1000z_family.yaml  # 共有トリガー + 取得スキーマ
│   │       ├── rigol_ds1104z.yaml
│   │       └── rigol_ds1054z.yaml
│   └── helpers/
│       ├── caveat_calc.py    # 能力 + 現在設定 → caveats[]
│       ├── trigger.py        # トリガープロファイル：検証 / 正規化 / caveat
│       ├── measure.py        # 測定プロファイル：検証 / 正規化 / パース
│       ├── acquisition.py    # チャンネル + タイムベースのプロファイル検証
│       ├── quantize.py       # 生電圧 → 0/1 ストリーム（シュミットヒステリシス）
│       ├── rle.py            # デジタルストリーム → runs[(t_us, level, dur_us)]
│       ├── edges.py          # runs → edges[(t_us, ch, RISE/FALL)]
│       ├── bus.py            # マルチチャンネル runs → bus_runs[(t_us, value, dur_us)]
│       ├── reference_diff.py # sim vs 実機エッジ差分（プロジェクトの存在理由）
│       ├── viewer.py         # 自己完結型 HTML 波形ビューア生成
│       ├── glitch_list.py    # min_width 未満の runs（ラント/グリッチ検出）
│       ├── edge_interval_stats.py  # ジッター / クロック安定性の統計
│       ├── pattern_search.py # RLE ビットパターンのテンプレートマッチ
│       ├── voltage_histogram.py    # 電圧分布 / bimodal 検出
│       ├── fft_peaks.py      # 上位N周波数ピーク（EMI / スイッチングノイズ）
│       ├── causality_check.py      # ch間の「BはAの後N µs以内」
│       └── envelope_downsample.py  # 可視化用 min/max 間引き
└── tests/                    # ユニット + ライブ conformance
```

## 環境変数

| 環境変数 | 必須 | 意味 |
|---|---|---|
| `SCOPE_MCP_HOST` | はい | オシロの IP またはホスト名 |
| `SCOPE_MCP_PORT` | いいえ（既定 5555） | SCPI TCP ポート |
| `SCOPE_MCP_MODEL` | いいえ | 機種キー（例 `RIGOL_DS1104Z`）。未設定なら `*IDN?` をパースして各プロファイルの `idn_match` 正規表現に照合 |

環境変数のプレフィックス `SCOPE_MCP_*` はプロジェクト固有なので、無関係なツールを
動かしている他のシェルと衝突しません。

## 新しい機器の追加

2つの新規ファイル + レジストリ1行：

1. **プロファイル**（`src/oscilloscope_mcp/instruments/profiles/<model>.yaml`）：
   能力（アナログ帯域、チャンネル数ごとのサンプルレート、メモリ深度、`idn_match` 正規表現）。
2. **ドライバ**（`src/oscilloscope_mcp/instruments/<vendor>_<series>.py`）：
   `oscilloscope_mcp.instruments._base.Scope` を継承し、各抽象メソッドをそのメーカーの
   SCPI 方言で実装。
3. **レジストリ1行**。`src/oscilloscope_mcp/instruments/__init__.py` の `MODEL_REGISTRY`
   にエントリを追加。
4. **conformance テスト**を実行：

   ```bash
   SCOPE_MCP_HOST=<ip> \
   SCOPE_MCP_CONFORMANCE_MODEL=<MODEL_KEY> \
   pytest tests/test_instrument_conformance.py -v
   ```

他のモジュールの変更は不要です。

## 限界（ツールの警告）

すべての構造化出力に `caveats[]` フィールドが含まれます。プロファイルが宣言する限界の外で
動作すると（例：DS1000Z の4チャンネルモードは 250 MSa/s を強制し、実用観測を ~25MHz まで
下げる）caveat が自動的に出ます。エージェントはどの数値を信じる前にも `caveats[]` を
読まねばなりません。

## 実機での確認状況

以下は実機 RIGOL **DS1104Z** に対して検証済みです（`tests/test_instrument_conformance.py`）：
接続 / IDN、生 SCPI、スクリーンショット、チャンネル / タイムベース / 取得設定の読み書き、
測定と統計、波形取得（**NORMal と RAW（全メモリ）**）、宣言的 `scope_capture`、取得制御、
全15種トリガーの**モード切替**、各種バリデーション（拒否）経路。

以下は**公式プログラミングガイドを基に実装・ユニットテスト済みだが、実機での確認は未了**です
（確認できるまでは仕様ベースの実装として扱ってください）：

- **EDGE 以外のトリガー種別固有パラメータ**（pulse width、slope time、RS232 / IIC / SPI の各
  フィールド…）。各種別のパラメータ範囲・列挙値はガイド由来で、送信前に検証し、**モード切替**は
  全15種を実機確認していますが、非 EDGE 種別の個々のパラメータは実機で逐一は通していません。
- **`NORMal` 以外の取得モード**（AVERages / PEAK / HRESolution）と明示的な `memory_depth` 値。
- **実シミュレーション出力との `scope_compare`。** 差分エンジンはユニットテスト済みで、実機
  キャプチャを既知量ずらしたデータでは検証済みですが、本物の *RTL シミュレーション出力 vs
  実機キャプチャ* の実行はまだ行っていません。
- **DS1104Z 以外の機種**（DS1054Z / DS1074Z）：同一コードパス + プロファイルですが、
  conformance テスト未実施。

## セキュリティ / 信頼モデル

これは**ローカル専用の stdio MCP サーバー**です。親プロセスの stdin/stdout を通じて MCP
クライアントとやり取りし、ネットワークポートは一切開きません。クライアント（Claude Code /
Cursor / 自作エージェント）がサブプロセスとして起動し、信頼境界はローカルユーザーアカウント
です。

その境界の中では：

- `scope_query` はコマンド検証なしの**生 SCPI パススルー**です。クライアントが送る SCPI は
  そのままオシロに転送されます。これは意図的です。ベンチオシロの SCPI は圧倒的に読み出し
  中心で、破壊的な設定変更（タイムベース、チャンネルスケール、トリガ）でさえ簡単に元に
  戻せます。ただし、このMCPサーバーを有効にしたエージェントは、あなたのオシロを完全に
  再設定でき、画面上のすべてを読めます。承知の上で使ってください。
- オシロへの SCPI 接続自体は、オシロの LAN ポート上の**認証なし TCP**です（RIGOL DS1000Z は
  5555 を使用）。同じネットワークにいる機器なら、すでにオシロと通信できます。このMCPサーバーを
  入れてもそこは変わらず、あなたが既に持っているのと同じアクセスを AIエージェントに渡すだけです。

オシロをさらに固めたいなら、この層ではなくオシロのネットワーク（VLAN、ファイアウォール）で
やってください。MCPサーバーは、同じマシン上のエージェントがオシロに何を頼めるかを取り締まる
適切な場所ではありません。

## テスト

```bash
# ユニットテスト（ハードウェア不要）。
pytest tests/ -v

# 実機に対するライブ conformance。
SCOPE_MCP_HOST=<your-scope-ip> \
  pytest tests/test_instrument_conformance.py -v
```

## 対応機器の追加

**制御方法が公開されている**オシロスコープ（SCPI でも、それ以外でも）なら対応できます。
上記のプラグイン設計のおかげで、新しいメーカーや機種はドライバのサブクラスと能力プロファイル
だけで済み、コア部分に手を入れる必要はありません。*新しい機器の追加* を参照してください。

**対応機器を追加する Fork を歓迎します。** 対応してほしい機器の要望も歓迎です。issue を
立ててください（プログラミングガイドへのリンクがあると助かります）。検討します。
