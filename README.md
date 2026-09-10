# TradingView -> MT5 Bridge

> TradingView の Webhook シグナルをローカル Python サーバーで受け取り、MetaTrader 5 (MT5) へ成行注文または決済注文を送るブリッジです。

## 目次

- [概要](#概要)
- [できること](#できること)
- [ディレクトリ構成](#ディレクトリ構成)
- [クイックスタート](#クイックスタート)
- [設定](#設定)
- [起動方法](#起動方法)
- [Webhook 仕様](#webhook-仕様)
- [運用](#運用)
- [注意点](#注意点)
- [関連ドキュメント](#関連ドキュメント)

## 概要

このプロジェクトは、`bridge.py` の CLI サーバーと `gui_app.py` の GUI から構成されます。

- `POST /webhook`: TradingView からの JSON またはプレーンテキスト alert を受信
- `SymbolResolver`: TradingView 側の `NASDAQ` / `USTEC` / `NAS100` などを MT5 の実シンボルへ解決
- `LotManager`: payload 指定ロット、銘柄別ロット、デフォルトロットを優先順に解決
- `MT5Trader`: MT5 Python API 経由で成行注文、同方向ポジション確認、ポジション決済を実行
- `dry_run`: MT5 注文を出さずに request 内容だけ返す検証モード

## できること

- 1つの Webhook エンドポイントで単一銘柄・複数銘柄を処理
- `symbol` / `ticker` / `instrument` / `symbols` を受け付け
- `buy` / `sell` / `close_all` / `close_buy` / `close_sell` などの action を処理
- ブローカー固有の prefix / suffix を MT5 の `symbols_get()` から自動探索
- `explicit_map` で特定銘柄を固定変換
- 同一シンボルで同方向ポジション保有中の重複エントリーをスキップ
- GUI から設定編集、MT5 端末選択、接続テスト、起動/停止、適用再起動
- 複数 MT5 プロファイルを設定し、symbol / strategy / payload 指定で発注先を切り替え
- JSON だけでなく `BUY` / `SELL` や `統合サイン↑/↓` を含むプレーンテキスト alert も受信
- TradingView strategy fill alert 7種類を `strategy_id` で識別し、同一銘柄でも独立して重複エントリー判定

## ディレクトリ構成

```text
.
├── bridge.py                       # CLI サーバーと MT5 注文ロジック
├── gui_app.py                      # tkinter GUI
├── config.example.json             # 設定ファイル例
├── requirements.txt                # Python 依存
├── AGENTS.md                       # AIエージェント向け開発・運用安全ガイド
├── README.md                       # この文書
└── docs
    ├── README.md                   # docs 配下の索引
    ├── architecture.md             # 構成と処理フロー
    ├── security.md                 # セキュリティ・運用安全
    ├── development
    │   └── coding-standards.md     # コーディング・変更ルール
    ├── operations
    │   └── runbook.md              # セットアップ、起動、検証、障害調査
    └── specs
        └── webhook-payloads.md     # Webhook payload と解釈仕様
```

## クイックスタート

### 1. 依存インストール

```bash
cd tv_mt5_bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows で運用する場合は PowerShell などで `.venv\Scripts\activate` を使ってください。

### 2. 設定ファイル作成

```bash
cp config.example.json config.json
```

`config.json` は認証情報を含むため、共有・コミットしないでください。MT5 端末側でログイン済みなら、`mt5.login` / `mt5.password` / `mt5.server` は空のままで構いません。

### 3. dry run で起動

`config.json` の `dry_run` を `true` のまま起動します。

```bash
python bridge.py -c config.json
```

起動後の Webhook URL:

```text
http://<サーバーIP>:8181/webhook
```

plain text の TradingView alert で `webhook.secret` を使う場合は、Webhook URL 側に付けます。

```text
http://<サーバーIP>:8181/webhook?secret=CHANGE_ME
```

### 4. 疎通テスト

ブラウザからは安全な疎通確認だけできます。MT5 注文は送られません。

```text
http://127.0.0.1:8181/health?secret=CHANGE_ME
```

ngrok を使う場合:

```text
https://xxxx.ngrok-free.dev/webhook?test=1&secret=CHANGE_ME
```

TradingView と同じ POST テストは `curl` または GUI の `Send Webhook Test` を使います。

```bash
curl -X POST http://127.0.0.1:8181/webhook \
  -H 'Content-Type: application/json' \
  -d '{"secret":"CHANGE_ME","symbol":"USDJPY","action":"buy","lot":0.01}'
```

## 設定

設定の正本は `config.json` です。構造は [config.example.json](config.example.json) を参照してください。

| セクション | 主なキー | 説明 |
| --- | --- | --- |
| `dry_run` | `true` / `false` | `true` の場合は MT5 へ注文を出さず request だけ返す |
| `mt5` | `terminal_path`, `login`, `password`, `server`, `magic`, `deviation`, `comment`, `filling_mode` | default MT5 接続と注文 request の基本設定。`login/password/server` は任意 |
| `mt5_profiles` | profile名ごとの `mt5` 設定 | 複数 MT5 端末・口座を使う場合の追加プロファイル |
| `routing` | `default_profile`, `dedupe_same_terminal`, `symbol_profiles`, `strategy_profiles` | payload / symbol / strategy ごとの発注先 profile |
| `webhook` | `host`, `port`, `secret` | HTTP サーバーの bind 先と shared secret |
| `trading_pause` | `enabled`, `timezone`, `entry_only`, `notify_on_skip`, `windows` | JST指定の時間帯に注文をスキップする |
| `news_filter` | `enabled`, `minutes_before`, `minutes_after`, `impact_filter`, `notify_on_skip` | Gaikaex 経済指標カレンダーを取得し、関連通貨の指標前後だけ entry をスキップする |
| `symbols` | `aliases`, `explicit_map`, `prefixes`, `suffixes`, `refresh_seconds` | TradingView シンボルから MT5 シンボルへの変換ルール |
| `risk` | `default_lot`, `per_symbol`, `min_lot`, `max_lot` | ロット解決と上下限 |
| `entry` | `skip_same_side_position`, `skip_same_side_across_strategies` | 同方向ポジションの重複をstrategy単位または全strategy横断でスキップするか |

`trading_pause.windows` は `HH:MM` のJSTで指定する。時間帯に入っている間は `/webhook` は `reason: "trading_pause"` の skip レスポンスを返し、MT5注文を送らない。デフォルトでは entry / close の両方と Discord 通知を止める。`entry_only: true` にすると新規エントリーだけ止めて決済は通し、`notify_on_skip: true` にすると止めたエントリーの Discord 通知は送る。`start` が `end` より後の指定は日跨ぎとして扱う。

`news_filter.enabled: true` の場合、Gaikaex の経済指標カレンダーを `refresh_seconds` ごとに取得し、`impact_filter` に一致する中・高重要度イベントの前後 `minutes_before` / `minutes_after` 分だけ entry を `reason: "news_filter"` でスキップする。対象通貨は USDJPY なら USD/JPY、XAUUSD や NASDAQ 系は USD として判定する。`notify_on_skip: true` なら、スキップ時も Discord embed にニュース理由を追加して通知する。取得失敗時はデフォルトでは発注を止めない。止めたい場合は `skip_on_fetch_error: true` にする。

## 起動方法

### CLI

```bash
python bridge.py -c config.json
```

### GUI

```bash
python gui_app.py -c config.json
```

GUI でできること:

- `Scan/Select`: 起動中の MT5 端末候補を一覧表示して選択。MT5 API には接続しない
- `Check Selected` / `Check All`: 選択画面内で MT5 account info を取得し、login / server / balance / name を表示
- `Test MT5`: 選択した端末 path と入力中の接続情報で接続確認。MT5 側でログイン済みなら login/password/server は空でOK
- `TradingView Webhook URL`: TradingView に貼る URL を表示してコピー。`host=0.0.0.0` の場合は `<YOUR_PUBLIC_HOST>` を実際の公開ホストに置換
- `Test` タブ: Webhook 疎通テストと MT5 テスト注文を実行
- `MT5 Profiles` タブ: 追加MT5プロファイルを一覧から選択し、単一MT5設定と同じように端末選択・接続テスト。`Default Order Targets` で通常時の発注先を複数選択
- `Trading Pause` タブ: 現在JSTを確認し、Entry Only / Notify Skipped Entry と停止時間を設定
- `News Filter` タブ: 経済指標フィルタの有効化、前後停止分数、重要度、取得テスト、Test Symbol の現在停止判定を確認
- `Send Webhook Test`: サーバー未起動なら現在設定で起動確認を出してから送信。`Dry Run` OFF の場合は実注文確認を出す
- `Save Config`: 設定を `config.json` に保存
- `Start/Stop`: Webhook サーバー制御
- `Apply & Restart`: 設定保存後にサーバー再起動

## Webhook 仕様

買いエントリー:

```json
{
  "secret": "CHANGE_ME",
  "symbol": "NASDAQ",
  "action": "buy"
}
```

複数銘柄エントリー:

```json
{
  "secret": "CHANGE_ME",
  "symbols": ["USDJPY", "EURUSD", "USTEC"],
  "action": "sell"
}
```

特定の MT5 プロファイルへ送る:

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profile": "sub_account"
}
```

同じ alert を複数 MT5 プロファイルへ送る:

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profiles": ["default", "sub_account"]
}
```

登録済みprofile全部へ送る:

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profiles": "all"
}
```

`routing.dedupe_same_terminal` が `true` の場合、同じ `terminal_path` のprofileは1つだけにまとめて二重発注を避けます。

ロット・SL・TP 指定:

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.03,
  "sl": 2300.0,
  "tp": 2350.0
}
```

全決済:

```json
{
  "secret": "CHANGE_ME",
  "symbol": "NASDAQ",
  "action": "close_all"
}
```

プレーンテキスト alert も一部対応しています。

```text
USDJPY 15 統合サイン↑ 150.123
```

詳細は [docs/specs/webhook-payloads.md](docs/specs/webhook-payloads.md) を参照してください。

### 対応 strategy fill alert

以下の7種類は plain text のまま受け取り、発注だけ行います。TP / SL / 決済は別 EA 側で管理する前提です。

```text
【15m】REM BB Pullback Rider V3 (60, 75, 200, 20, 20, 2, 2, 20, 2.8): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

```text
【15mアクティブ】REM BB Pullback Rider V3 (40, 75, 400, 50, 50, 2, 0.5, 20, 2.8, 4, 30, 9, 0): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

```text
【30m】REM BB Pullback Rider V3 (40, 100, 160, 10, 20, 2, 2, 20, 2.8): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

```text
【30mアクティブ】REM BB Pullback Rider V3 (...): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

```text
REM BB Pullback Rider V3 (60, 75, 200, 20, 20, 2, 2, 20, 2.8): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

```text
Wemof Strategy Original (20, 3, 100, 10, 200, 200, 5,000, 20, 5, 1.1): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

```text
Gate Breaker T-L: {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

各 alert は MT5 order comment を `tv-bridge-r15` / `tv-bridge-r15a` / `tv-bridge-r30` / `tv-bridge-r30a` / `tv-bridge-rem` / `tv-bridge-wem` / `tv-bridge-gbtl` に分けます。`entry.skip_same_side_position` が `true` の場合は、この comment 単位で同方向ポジションを判定します。`entry.skip_same_side_across_strategies` が `true` の場合は comment に関係なく、同一MT5口座・シンボル・方向で1ポジに制限します。

### 玉暴威アラート

玉暴威インジケーターの alert は `【予告】` `【候補】` `【確定】` `【取消】` `【包足】` のいずれかのタグで始まるプレーンテキストです。`【確定】` 以外はスキップし、`【確定】` のみ本文の `BUY` / `SELL` から action を推定して発注します。symbol は本文にシンボル情報が含まれないため `XAUUSD` 固定です。詳細は [docs/specs/webhook-payloads.md](docs/specs/webhook-payloads.md#玉暴威アラートの確定フィルタ) を参照してください。

## 運用

- まず `dry_run: true` で JSON payload とレスポンスを確認してください。
- GUI の `Send Webhook Test` は現在のGUI設定をサーバーへ反映してから、ローカル Webhook へ HTTP POST を送ります。サーバー未起動なら起動確認を出します。
- GUI の `Send MT5 Test Order` は現在の `Dry Run` 設定に従います。`Test MT5 Profile` で `all` を選ぶと全profileへテスト注文します。`Dry Run` OFF では実注文前に確認ダイアログを出します。
- 本番注文前に MT5 の対象口座、サーバー、シンボル名、最小ロット、最大ロットを確認してください。
- 外部ネットワークから受ける場合は、OS ファイアウォール、ルーター、リバースプロキシなどで公開範囲を限定してください。
- TradingView から直接ローカル PC へ到達できない環境では、トンネル、VPS、リバースプロキシなど別経路が必要です。
- 障害調査と復旧手順は [docs/operations/runbook.md](docs/operations/runbook.md) を参照してください。

## 注意点

- このブリッジは売買注文を実行できます。設定ミスは実損につながります。
- `webhook.secret` は必ず推測しにくい値に変更してください。
- `dry_run: false` に切り替える前に、少額ロットかデモ口座で検証してください。
- 対応 strategy fill alert では bridge は発注のみ行い、TP / SL / 決済注文は送りません。
- `symbol_select failed` が出る場合は MT5 の実シンボル名を確認してください。例: `XAUUSD` ではなく `XAUUSDm` の場合は `symbols.explicit_map` に固定変換を追加します。
- `Unsupported filling mode` が出る場合は、各MT5 profileの `Fill Mode` を `auto` にしてください。`auto` は発注時に `ioc` / `fok` / `return` を試し、成功したmodeをprofileごとに起動中メモリへ記憶します。
- `MetaTrader5` Python パッケージは実行環境・OS・MT5 端末状態に依存します。MT5 端末がログイン済みか確認してください。
- GUI は `tkinter` が使える環境でのみ動作します。
- `__pycache__/` や `config.json` は運用生成物として扱い、配布物には含めないでください。

## 関連ドキュメント

- [docs/README.md](docs/README.md): ドキュメント索引
- [AGENTS.md](AGENTS.md): AIエージェント向け開発・運用安全ガイド
- [docs/architecture.md](docs/architecture.md): アーキテクチャ概要
- [docs/development/coding-standards.md](docs/development/coding-standards.md): コーディング・変更ルール
- [docs/security.md](docs/security.md): セキュリティ・運用安全ガイド
- [docs/operations/runbook.md](docs/operations/runbook.md): 運用 Runbook
- [docs/specs/webhook-payloads.md](docs/specs/webhook-payloads.md): Webhook payload 仕様
