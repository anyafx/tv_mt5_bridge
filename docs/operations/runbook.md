# TradingView -> MT5 Bridge 運用 Runbook

## 目的

TradingView Webhook を MT5 注文へ橋渡しするローカルブリッジのセットアップ、検証、本番化、障害調査、復旧手順をまとめる。

## 前提

- Python 3 が利用できる
- MT5 端末がインストール済み
- MT5 口座へログインできる
- Python パッケージ `MetaTrader5` が動作する環境で実行する
- `config.json` は `config.example.json` から作成済み

## 初期セットアップ

```bash
cd tv_mt5_bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

`config.json` で最低限確認する項目:

| キー | 確認内容 |
| --- | --- |
| `dry_run` | 初回は `true` |
| `mt5.terminal_path` | 実行したい MT5 端末の path |
| `mt5.login` / `password` / `server` | 任意。MT5 端末側でログイン済みなら空でよい |
| `mt5.filling_mode` | `auto` / `ioc` / `fok` / `return`。通常は `auto` |
| `mt5_profiles` | 複数 MT5 を使う場合の追加端末・口座設定 |
| `routing` | symbol / strategy ごとの発注先 profile |
| `webhook.secret` | TradingView payload と一致する shared secret |
| `webhook.host` / `port` | HTTP サーバーの bind 先 |
| `trading_pause` | 指標や市場オープン前後など、注文を止めるJST時間帯 |
| `news_filter` | Gaikaex 経済指標カレンダーで関連通貨の entry を止める設定 |
| `symbols.explicit_map` | ブローカー固有シンボルを固定したい場合に設定 |
| `risk.default_lot` / `per_symbol` | 想定ロット |
| `entry.skip_same_side_position` | 重複エントリー抑止の有無 |
| `entry.skip_same_side_across_strategies` | 全strategy横断で同一シンボル・同方向を1ポジに制限するか |

停止時間を使う場合:

```json
"trading_pause": {
  "enabled": true,
  "timezone": "Asia/Tokyo",
  "entry_only": true,
  "notify_on_skip": true,
  "windows": [
    {"start": "08:55", "end": "09:10", "label": "Tokyo open"},
    {"start": "21:25", "end": "21:40", "label": "Economic indicator"}
  ]
}
```

`HH:MM` は日本時間で指定する。`23:55` から `00:10` のような日跨ぎも指定できる。停止中のWebhookは `reason: "trading_pause"` でskipされ、MT5注文は送られない。

デフォルトでは entry / close の両方と Discord 通知を止める。`entry_only: true` なら新規エントリーだけ止めて決済は通す。`notify_on_skip: true` なら、止めたエントリーでも Discord 通知は送る。

経済指標カレンダーで止める場合:

```json
"news_filter": {
  "enabled": true,
  "minutes_before": 15,
  "minutes_after": 15,
  "impact_filter": "medium_high",
  "refresh_seconds": 300,
  "notify_on_skip": true,
  "skip_on_fetch_error": false
}
```

Gaikaex のカレンダーから当日の中・高重要度指標を取得し、USDJPY は USD/JPY、XAUUSD や NASDAQ 系は USD の指標前後で entry を `news_filter` としてスキップする。`notify_on_skip: true` の場合はDiscord通知自体は送り、embed内にニュース理由を追加する。`skip_on_fetch_error: false` では取得失敗時に発注を止めない。

## 起動

### CLI

```bash
source .venv/bin/activate
python bridge.py -c config.json
```

起動ログ:

```text
Webhook server started: http://0.0.0.0:8181/webhook
Dry run mode: True
```

### GUI

```bash
source .venv/bin/activate
python gui_app.py -c config.json
```

GUI 操作:

| 操作 | 用途 |
| --- | --- |
| `Scan/Select` | 起動中の MT5 端末候補の検出と選択。MT5 API には接続しない |
| `Check Selected` / `Check All` | 選択画面内で MT5 account info を取得し、login / server / balance / name を表示 |
| `Test MT5` | 選択した端末 path と入力中の MT5 接続情報で疎通確認。MT5 側でログイン済みなら login/password/server は空でよい |
| `TradingView Webhook URL` | TradingView に貼る URL を表示してコピー |
| `MT5 Profiles` | 追加MT5プロファイルを一覧から選択し、端末選択・接続テストを実行。`Default Order Targets` で通常時の発注先を複数選択 |
| `Send Webhook Test` | 現在のGUI設定をサーバーへ反映してからローカル Webhook へ HTTP POST を送って疎通確認。サーバー未起動なら起動確認を出す |
| `Send MT5 Test Order` | 現在の Dry Run 設定でテスト注文を実行。`Test MT5 Profile` で `all` を選ぶと全profileへ送る |
| `Save Config` | 入力内容を `config.json` へ保存 |
| `Start` / `Stop` | Webhook サーバー起動/停止 |
| `Apply & Restart` | 設定保存後にサーバー再起動 |

## dry run 検証

まず `dry_run: true` のまま疎通確認する。

ブラウザで ngrok からブリッジまでの到達だけ確認する場合:

```text
https://<ngrok-host>/webhook?test=1&secret=<YOUR_SECRET>
```

この GET テストは MT5 注文を送らない。TradingView と同じ注文経路を確認する場合は POST を使う。

```bash
curl -X POST http://127.0.0.1:8181/webhook \
  -H 'Content-Type: application/json' \
  -d '{"secret":"CHANGE_ME","symbol":"USDJPY","action":"buy","lot":0.01}'
```

期待レスポンス例:

```json
{
  "ok": true,
  "results": [
    {
      "raw_symbol": "USDJPY",
      "canonical_symbol": "USDJPY",
      "resolved_symbol": "USDJPY",
      "lot": 0.01,
      "existing_positions": {"buy": 0, "sell": 0, "total": 0},
      "result": {
        "dry_run": true,
        "request": {
          "action": "market",
          "symbol": "USDJPY",
          "side": "buy",
          "lot": 0.01
        }
      }
    }
  ]
}
```

確認観点:

- `ok: true` になっている
- `resolved_symbol` が MT5 で注文可能な名前になっている
- `lot` が想定どおり
- `existing_positions` が重複エントリー判定に使える値になっている
- `dry_run: true` の request が想定する side / lot / sl / tp になっている

### strategy fill alert の dry run

7種類の strategy fill alert は発注だけ行い、TP / SL / 決済は別 EA に任せる。

```bash
curl -X POST http://127.0.0.1:8181/webhook \
  -H 'Content-Type: text/plain' \
  --data '【15m】REM BB Pullback Rider V3 (60, 75, 200, 20, 20, 2, 2, 20, 2.8): USDJPY で buy @ 1 の注文が約定しました。新しいストラテジーポジションは 1 です'
```

確認観点:

- `strategy_id` が `rem_bb_pullback_15m` になる
- dry run request の `comment` が `tv-bridge-r15` になる
- `sl` / `tp` が `null` になる
- 同じ銘柄でも 15m / 15m active / 30m / 30m active / REM default / Wemof / Gate Breaker T-L で comment が分かれる
- `strategy.position_size` が `0` の決済 alert は `strategy_fill_not_entry` で skip される

## 本番化手順

1. デモ口座または最小ロットで検証する。
2. `explicit_map` や `aliases` を対象ブローカーに合わせる。
3. `risk.default_lot` / `risk.per_symbol` / `risk.max_lot` を最終確認する。
4. `webhook.secret` を推測しにくい値へ変更する。
5. `dry_run` を `false` に変更する。
6. CLI または GUI で再起動する。
7. 小さなテスト注文を送り、MT5 側の注文・約定・ポジションを確認する。

## TradingView 設定

Webhook URL:

```text
http://<公開先ホスト>:8181/webhook
```

GUI の `Host` が `0.0.0.0` の場合、`TradingView Webhook URL` 欄には `<YOUR_PUBLIC_HOST>` が表示される。TradingView に貼る時は、VPS のホスト名、固定IP、トンネルURL、リバースプロキシのURLなど、TradingView から到達できる公開先に置き換える。

plain text の strategy fill alert で `webhook.secret` を使う場合:

```text
http://<公開先ホスト>:8181/webhook?secret=<YOUR_SECRET>
```

TradingView からローカル PC へ直接到達できない場合は、以下のいずれかが必要。

- VPS 上でブリッジを実行
- VPN / トンネルでローカルへ転送
- HTTPS 終端するリバースプロキシを経由
- ルーターのポート転送と OS ファイアウォール許可

外部公開する場合は、`webhook.secret` だけに依存せず、送信元制限や認証付きプロキシを検討する。

## 複数 MT5 の使い分け

`mt5` は default profile として扱う。追加口座や別端末は `mt5_profiles` に定義する。
GUI では `MT5 Profiles` タブから profile を `New` で追加し、`Scan/Select` で端末を選ぶ。選択画面内の `Check Selected` / `Check All` で login / server / balance を見て、どの口座か確認してから `Apply Profile` する。複数追加する場合は、1つ目を `Apply Profile` した後に `New` を押す。編集中profileがある状態で `New` を押した場合も、現在のprofileを保存してから次のprofileを作る。通常時の発注先は `Default Order Targets` で `default` や追加profileを複数選択する。
profileごとの `Magic` / `Deviation` / `Comment` / `Fill Mode` は `Apply Profile` で画面内設定へ反映し、`Save Config` または `Apply & Restart` で `config.json` へ保存する。

```json
{
  "mt5_profiles": {
    "sub_account": {
      "terminal_path": "C:\\Program Files\\MetaTrader 5 EXNESS 2\\terminal64.exe",
      "login": null,
      "password": "",
      "server": "",
      "magic": 990002,
      "deviation": 20,
      "comment": "tv-bridge-sub",
      "filling_mode": "auto"
    }
  },
  "routing": {
    "default_profile": "default",
    "dedupe_same_terminal": true,
    "symbol_profiles": {
      "XAUUSD": ["default", "sub_account"]
    },
    "strategy_profiles": {
      "wemof_original": "sub_account"
    }
  }
}
```

payload 側で直接指定することもできる。

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profile": "sub_account"
}
```

同じ alert を複数口座へ飛ばす場合は `mt5_profiles` を配列で指定する。

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profiles": ["default", "sub_account"]
}
```

登録済みprofile全部へ飛ばす場合は `all` を指定できる。`all` は `default` と `mt5_profiles` に登録した追加profile全てに展開される。
`routing.dedupe_same_terminal` が `true` の場合、同じ `terminal_path` を指すprofileは1つにまとめる。これにより同じMT5端末へ二重発注しない。意図的に同一端末へ複数回発注したい場合だけ `false` にする。

```json
{
  "secret": "CHANGE_ME",
  "symbol": "XAUUSD",
  "action": "buy",
  "lot": 0.01,
  "mt5_profiles": "all"
}
```

同一プロセスでは MT5 API への接続を直列化するため、複数MT5へ完全同時発注はしない。高頻度で並列発注したい場合は、MT5ごとにブリッジを別プロセス・別ポートで起動する。

## 障害調査

### Webhook が 403 を返す

原因:

- payload の `secret` が `config.json` の `webhook.secret` と一致しない

対応:

1. `config.json` の `webhook.secret` を確認する。
2. TradingView alert message の JSON に同じ `secret` が入っているか確認する。
3. GUI 起動中の場合、設定保存後に `Apply & Restart` したか確認する。

### Webhook が 400 を返す

原因例:

- JSON が壊れている
- `action` が未対応
- `symbol` / `symbols` がない
- plain text から action / symbol を推定できない
- MT5 接続、シンボル選択、注文送信でエラー

対応:

1. レスポンスの `error` を読む。
2. JSON payload を [../specs/webhook-payloads.md](../specs/webhook-payloads.md) と照合する。
3. `dry_run: true` に戻して request を確認する。
4. MT5 端末が起動・ログイン済みか確認する。

### `same_side_position_exists` で注文されない

`entry.skip_same_side_position` が `true` の場合、同じ `resolved_symbol` で同方向ポジションがあると新規エントリーをスキップする。

strategy fill alert の場合は、同じ `resolved_symbol` かつ同じ strategy comment suffix を持つポジションだけを重複判定する。たとえば `tv-bridge-r30a` と `tv-bridge-1-r30a` は同じ 30m active として扱う。15m / 15m active / 30m / 30m active / REM default / Wemof / Gate Breaker T-L は独立して扱う。
`entry.skip_same_side_across_strategies` が `true` の場合はstrategy commentを区別せず、同じMT5口座・`resolved_symbol`・方向に1つでもポジションがあればスキップする。この設定は `entry.skip_same_side_position` が `false` でも有効になる。
`entry.skip_same_side_position` が `true` の場合、通常Webhookも同じMT5口座・`resolved_symbol`・方向で短時間の pending marker を取得する。同じ entry が複数アプリからほぼ同時に来ても、MT5 の positions 反映前に2件目以降を `duplicate_entry_pending` としてスキップする。strategy fill alert は同じstrategy単位で判定する。

対応:

- 意図どおりなら正常。
- 重複エントリーを許可したい場合は `entry.skip_same_side_position` と `entry.skip_same_side_across_strategies` を両方 `false` に変更して再起動する。
- 別シンボルとして扱われている可能性がある場合は `resolved_symbol` を確認する。

### シンボルが期待どおり解決されない

確認順:

1. レスポンスの `raw_symbol` / `canonical_symbol` / `resolved_symbol` を見る。
2. `symbols.explicit_map` に固定変換を追加する。
3. `symbols.aliases` に TradingView 側の別名を追加する。
4. `prefixes` / `suffixes` にブローカー固有の接頭辞・接尾辞を追加する。
5. `refresh_seconds` 経過後、または再起動後に再確認する。

ログに `Symbols refreshed: 0` が出る場合は、シンボル一覧取得時点で MT5 接続が成立していない可能性が高い。GUI の `Test MT5` で account info が取れるか確認し、ブリッジを再起動してから再テストする。

`symbol_select failed: XAUUSD` のように出る場合は、MT5 の実シンボル名と TradingView 側シンボル名が一致していない。EXNESS などで `XAUUSDm` のような接尾辞が付く場合は、`symbols.explicit_map` に固定変換を追加する。

```json
{
  "XAUUSD": "XAUUSDm"
}
```

### MT5 接続に失敗する

確認項目:

- MT5 端末 path が正しい
- MT5 端末が壊れていない
- 口座番号、パスワード、サーバー名が正しい
- Python 実行環境で `MetaTrader5` package が import できる
- GUI の `Test MT5` で account info が取れる

### 注文が MT5 側で拒否される

確認項目:

- シンボルが Market Watch に表示・取引可能
- ロットが `volume_min` / `volume_max` / `volume_step` に合っている
- SL / TP が近すぎない
- 取引時間内
- 口座の証拠金が足りている
- `deviation` が小さすぎない
- `Unsupported filling mode` の場合は profile の `Fill Mode` を `auto` にする。`auto` は発注時に `ioc` / `fok` / `return` を順に試し、成功したmodeをprofileごとに起動中メモリへ記憶する。

## レスポンスの読み方

| フィールド | 意味 |
| --- | --- |
| `ok` | ブリッジ処理が成功したか |
| `results[]` | 銘柄ごとの処理結果 |
| `raw_symbol` | payload から受け取った元シンボル |
| `canonical_symbol` | alias 正規化後のシンボル |
| `resolved_symbol` | MT5 に渡す実シンボル |
| `lot` | 実際に採用したロット |
| `existing_positions` | 同方向ポジション判定用の件数 |
| `skipped` | 注文をスキップしたか |
| `reason` | skip 理由 |
| `result` | MT5 order_send の結果または dry run request |

## 復旧手順

- 設定ミスが疑われる場合: `dry_run: true` に戻し、再起動して payload を確認する。
- 誤発注リスクがある場合: ブリッジ停止、TradingView alert 停止、MT5 側で手動確認の順に実施する。
- Webhook secret 流出時: `webhook.secret` を変更し、TradingView 側 payload も同時に更新する。
- シンボル解決ミス時: `explicit_map` で固定変換し、dry run で `resolved_symbol` を確認してから本番化する。
- GUI が固まった場合: CLI で起動できるか確認し、必要ならプロセスを停止して再起動する。

## 変更後チェックリスト

- `python -m py_compile bridge.py gui_app.py` が通る
- `dry_run: true` で `buy` / `sell` / `close_all` のテストが通る
- `resolved_symbol` と `lot` が想定どおり
- `webhook.secret` が実運用値に変更されている
- `dry_run: false` への切り替え前にデモ口座または最小ロットで検証した
