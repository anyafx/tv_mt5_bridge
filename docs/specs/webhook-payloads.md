# Webhook Payload 仕様

## 目的

`bridge.py` が受け付ける TradingView Webhook payload、plain text alert の推定ルール、注文解釈、レスポンス形式を明文化する。

## エンドポイント

```text
POST /webhook
```

`/webhook` 以外は `404` を返す。

## 認証

`config.json` の `webhook.secret` が空でない場合、以下のいずれかの `secret` と完全一致する必要がある。

1. JSON / key-value payload の `secret`
2. URL query の `?secret=...`
3. HTTP header の `X-Webhook-Secret`

```json
{
  "secret": "CHANGE_ME",
  "symbol": "USDJPY",
  "action": "buy"
}
```

一致しない場合:

```json
{"ok": false, "error": "Invalid secret"}
```

HTTP status は `403`。

plain text の TradingView strategy alert では本文に `secret` を入れにくいため、Webhook URL を以下の形にする。

```text
http://<host>:8181/webhook?secret=CHANGE_ME
```

## JSON payload

### 最小エントリー

```json
{
  "secret": "CHANGE_ME",
  "symbol": "USDJPY",
  "action": "buy"
}
```

### 複数銘柄

```json
{
  "secret": "CHANGE_ME",
  "symbols": ["USDJPY", "EURUSD", "NASDAQ"],
  "action": "sell"
}
```

### ロット・SL・TP

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

### 決済

```json
{
  "secret": "CHANGE_ME",
  "symbol": "USDJPY",
  "action": "close_all"
}
```

```json
{
  "secret": "CHANGE_ME",
  "symbol": "USDJPY",
  "action": "close_buy"
}
```

## フィールド

| フィールド | 型 | 必須 | 説明 |
| --- | --- | --- | --- |
| `secret` | string | `webhook.secret` 設定時に必須 | shared secret |
| `action` | string | 必須 | 注文種別。`side` でも代替可 |
| `side` | string | `action` がない場合に利用 | 注文方向または決済方向 |
| `symbol` | string | `symbols` がない場合に必須 | 単一シンボル。空白・カンマ区切りで複数指定も可 |
| `ticker` | string | `symbol` / `symbols` がない場合に利用 | TradingView の ticker 用 |
| `instrument` | string | `symbol` / `ticker` がない場合に利用 | 代替シンボルキー |
| `symbols` | array[string] | `symbol` がない場合に利用 | 複数シンボル |
| `lot` | number/string | 任意 | payload でロットを上書き |
| `sl` | number/string/null | 任意 | Stop Loss。空文字、`na`, `nan`, `null`, `none` は未指定扱い |
| `tp` | number/string/null | 任意 | Take Profit。空文字、`na`, `nan`, `null`, `none` は未指定扱い |
| `mt5_profile` | string | 任意 | `mt5_profiles` に定義した発注先 profile。`profile` / `account` / `route` でも代替可 |
| `mt5_profiles` | array[string] / string | 任意 | 同じ alert を複数 MT5 profile へ送る。`profiles` / `accounts` でも代替可 |
| `strategy_id` | string | 任意 | strategy alert 由来の場合に内部設定。JSON で明示指定も可 |
| `skip_scope` | string | 任意 | `strategy` の場合、同方向ポジション判定を strategy comment 単位に限定 |

### MT5 profile 指定

payload で `mt5_profile` を指定すると、`routing` より優先してその MT5 profile へ発注する。

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profile": "sub_account"
}
```

複数口座へ同じ注文を送る場合:

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profiles": ["default", "sub_account"]
}
```

登録済みprofile全部へ送る場合:

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profiles": "all"
}
```

payload 指定がない場合は以下の順で profile を選ぶ。

1. `routing.strategy_profiles[strategy_id]`
2. `routing.symbol_profiles[canonical_symbol]`
3. `routing.default_profile`
4. `default`

`routing.*` の値は string でも array[string] でもよい。array の場合は同じ alert を複数 profile へ順番に送る。
`all` または `*` は `default` と登録済み追加profile全てに展開する。
`routing.dedupe_same_terminal` が `true` の場合、同じ `terminal_path` の profile は1つにまとめ、同一MT5端末への二重発注を避ける。

## action

| action | 種別 | side |
| --- | --- | --- |
| `buy` | entry | `buy` |
| `long` | entry | `buy` |
| `sell` | entry | `sell` |
| `short` | entry | `sell` |
| `close` | close | 全方向 |
| `close_all` | close | 全方向 |
| `flat` | close | 全方向 |
| `close_buy` | close | buy のみ |
| `close_long` | close | buy のみ |
| `close_sell` | close | sell のみ |
| `close_short` | close | sell のみ |

未対応 action は `400` で `Unsupported action` を返す。

## symbol 解決

`parse_symbol_list` は以下の順でシンボルを取得する。

1. `symbols` が list なら、その各要素
2. `symbol`
3. `ticker`
4. `instrument`

`symbol` / `ticker` / `instrument` が文字列の場合、空白またはカンマ区切りで複数シンボルとして扱う。

例:

```json
{"symbol": "USDJPY,EURUSD NASDAQ", "action": "buy"}
```

### canonicalize

`symbols.aliases` で TradingView 側の別名を canonical symbol に寄せる。

例:

```json
{
  "aliases": {
    "NASDAQ": ["USTEC", "NAS100", "US100", "NAS"]
  }
}
```

`USTEC` を受け取ると canonical symbol は `NASDAQ` になる。

### explicit_map

`symbols.explicit_map` は最優先の固定変換。

例:

```json
{
  "explicit_map": {
    "NASDAQ": "USTEC.cash"
  }
}
```

この場合、`NASDAQ` やその alias は MT5 実シンボル `USTEC.cash` として扱う。

### prefix / suffix 探索

MT5 の `symbols_get()` から取得したシンボル一覧に対して、`prefixes` と `suffixes` を組み合わせて探索する。

例:

```json
{
  "prefixes": ["", "#", "m."],
  "suffixes": ["", ".m", "m", ".pro", "_pro", ".ecn", "-ecn", ".cash"]
}
```

完全一致、正規化一致、prefix/suffix 探索、類似スコア探索の順で `resolved_symbol` を決定する。見つからない場合は canonical symbol をそのまま返す。

## lot 解決

優先順:

1. payload の `lot` が正の数値なら採用
2. `risk.per_symbol[canonical_symbol]`
3. `risk.default_lot`

最後に `risk.min_lot` と `risk.max_lot` で clamp する。MT5 送信直前には `symbol_info().volume_min` / `volume_max` / `volume_step` に合わせて丸める。

## plain text payload

JSON として解析できない本文は plain text として扱う。

### strategy fill 形式

以下の7種類は、TradingView strategy fill alert として専用解析する。いずれも entry 専用で、bridge は TP / SL / 決済を送らない。

| alert | `strategy_id` | MT5 comment |
| --- | --- | --- |
| `【15m】REM BB Pullback Rider V3 ...` | `rem_bb_pullback_15m` | `tv-bridge-r15` |
| `【15mアクティブ】REM BB Pullback Rider V3 ...` | `rem_bb_pullback_15m_active` | `tv-bridge-r15a` |
| `【30m】REM BB Pullback Rider V3 ...` | `rem_bb_pullback_30m` | `tv-bridge-r30` |
| `【30mアクティブ】REM BB Pullback Rider V3 ...` | `rem_bb_pullback_30m_active` | `tv-bridge-r30a` |
| `REM BB Pullback Rider V3 ...` | `rem_bb_pullback_default` | `tv-bridge-rem` |
| `Wemof Strategy Original ...` | `wemof_original` | `tv-bridge-wem` |
| `Gate Breaker T-L ...` | `gate_breaker_tl` | `tv-bridge-gbtl` |

15m:

```text
【15m】REM BB Pullback Rider V3 (60, 75, 200, 20, 20, 2, 2, 20, 2.8): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

15m active:

```text
【15mアクティブ】REM BB Pullback Rider V3 (40, 75, 400, 50, 50, 2, 0.5, 20, 2.8, 4, 30, 9, 0): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

30m:

```text
【30m】REM BB Pullback Rider V3 (40, 100, 160, 10, 20, 2, 2, 20, 2.8): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

30m active:

```text
【30mアクティブ】REM BB Pullback Rider V3 (...): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

REM default:

```text
REM BB Pullback Rider V3 (60, 75, 200, 20, 20, 2, 2, 20, 2.8): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

Wemof:

```text
Wemof Strategy Original (20, 3, 100, 10, 200, 200, 5,000, 20, 5, 1.1): {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

Gate Breaker T-L:

```text
Gate Breaker T-L: {{ticker}} で {{strategy.order.action}} @ {{strategy.order.contracts}} の注文が約定しました。新しいストラテジーポジションは {{strategy.position_size}} です
```

`{{strategy.order.action}}` は `buy` / `sell` のほか、TradingView の表示言語によって `買い` / `売り` になっても解析する。

専用解析後の内部 payload 例:

```json
{
  "action": "buy",
  "symbol": "USDJPY",
  "strategy_id": "rem_bb_pullback_15m",
  "strategy_label": "15m",
  "strategy_entry_signal": true,
  "entry_only": true,
  "skip_scope": "strategy"
}
```

`entry_only: true` のため、payload に `sl` / `tp` が含まれても発注 request には入れない。
`strategy.position_size` が `0`、または `buy` なのにポジションが負、`sell` なのにポジションが正のように action とポジション方向が一致しない場合は `strategy_fill_not_entry` として skip する。決済や縮小は別 EA に任せる。

### key-value 形式

`|` 区切りで `key=value` が含まれる場合、payload object に変換する。

```text
secret=CHANGE_ME|symbol=USDJPY|action=buy|lot=0.01
```

`action` または `side` が取れた場合、そのまま通常 payload として処理する。

### 自然文推定

以下の語から action を推定する。

| 文字列 | action |
| --- | --- |
| `BUY` | `buy` |
| `SELL` | `sell` |
| `統合サイン↑` | `buy` |
| `統合サイン↓` | `sell` |

シンボル推定:

1. `OANDA:USDJPY` のような exchange prefix 付きなら、コロン後を採用
2. それ以外は大文字トークンから stop word を除外して最初の候補を採用

対応例:

```text
▼ SELL ... OANDA:USDJPY ...
```

```text
▲ BUY ... FX:EURUSD ...
```

```text
USDJPY 15 統合サイン↑ 150.123
```

推定できない場合:

```json
{
  "ok": false,
  "error": "Invalid payload: Could not infer action/symbol from plain text alert. Use JSON or include BUY/SELL and symbol in message."
}
```

HTTP status は `400`。

## エントリー処理

`action` が entry の場合:

1. シンボルを解決する
2. `entry.skip_same_side_position` が `true` なら、MT5 の既存ポジション数を取得する
3. 同方向ポジションがあれば `same_side_position_exists` で skip
4. ロットを解決する
5. `market_order` で成行注文を送る

strategy fill 形式では `skip_scope: strategy` が設定されるため、既存ポジション判定は同じ `strategy_id` の MT5 comment suffix を対象にする。たとえば `tv-bridge-r30a` と `tv-bridge-1-r30a` は同じ 30m active として扱う。同じ銘柄・同じ方向でも、15m / 15m active / 30m / 30m active / REM default / Wemof / Gate Breaker T-L は独立して発注できる。

strategy fill 形式で決済・縮小と判断した場合の skip レスポンス例:

```json
{
  "ok": true,
  "results": [
    {
      "skipped": true,
      "reason": "strategy_fill_not_entry",
      "symbol": "USDJPY",
      "action": "sell",
      "strategy_id": "rem_bb_pullback_15m",
      "strategy_position_size": "0"
    }
  ]
}
```

skip レスポンス例:

```json
{
  "raw_symbol": "USDJPY",
  "canonical_symbol": "USDJPY",
  "resolved_symbol": "USDJPY",
  "skipped": true,
  "reason": "same_side_position_exists",
  "side": "buy",
  "existing_positions": {"buy": 1, "sell": 0, "total": 1}
}
```

## 決済処理

`action` が close の場合:

1. シンボルを解決する
2. `close_positions` で対象ポジションを取得する
3. buy 決済は sell、sell 決済は buy の反対注文を送る

`close_buy` / `close_long` は buy ポジションのみ、`close_sell` / `close_short` は sell ポジションのみを対象にする。

## レスポンス

成功時:

```json
{
  "ok": true,
  "results": []
}
```

payload エラーまたは処理エラー:

```json
{
  "ok": false,
  "error": "..."
}
```

HTTP status:

| status | 意味 |
| --- | --- |
| `200` | 処理成功。銘柄ごとの skip は `results[]` 内で表現 |
| `400` | payload 不正、未対応 action、MT5 処理エラー |
| `403` | secret 不一致 |
| `404` | `/webhook` 以外 |

## TradingView alert message 例

JSON が最も安全。

```json
{"secret":"CHANGE_ME","symbol":"{{ticker}}","action":"buy","lot":0.01}
```

SignTool / indicator 由来の plain text を使う場合は、`BUY` / `SELL` とシンボルが本文に含まれるようにする。
