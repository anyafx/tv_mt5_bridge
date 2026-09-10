# アーキテクチャ概要

## 目的

TradingView -> MT5 Bridge の主要コンポーネント、設定、Webhook 処理フロー、MT5 発注フローを整理する。

## コンポーネント

| ファイル / クラス | 役割 |
| --- | --- |
| `bridge.py` | CLI Webhook サーバー、payload 解析、シンボル解決、ロット解決、MT5 発注 |
| `gui_app.py` | tkinter GUI、設定編集、MT5 端末選択、接続テスト、サーバー起動停止 |
| `AppConfig` | `config.json` を dataclass に読み込む設定ルート |
| `SymbolResolver` | TradingView 側シンボルを MT5 実シンボルへ変換 |
| `LotManager` | payload / profile / symbol / default の優先順でロットを決める |
| `MT5Trader` | MetaTrader5 API の initialize、symbol 選択、注文、決済、ポジション確認 |
| `create_handler` | `/webhook` と health/test 系 GET を提供する HTTP handler を生成 |
| `BridgeService` | GUI から Webhook server を起動・停止する薄いサービス |

## 設定モデル

`config.json` の主なセクション:

| セクション | 役割 |
| --- | --- |
| `dry_run` | `true` の場合、MT5 へ注文せず request 内容だけ返す |
| `mt5` | default profile の端末、口座、magic、deviation、comment、filling mode |
| `mt5_profiles` | 追加 profile。複数口座・複数端末への発注に使う |
| `routing` | strategy / symbol / default の順で発注先 profile を選ぶ |
| `webhook` | bind host、port、shared secret |
| `discord` | 約定 alert の Discord 通知設定 |
| `trading_pause` | JST時間帯による注文停止設定。必要に応じて entry のみ停止、停止中通知を選べる |
| `news_filter` | 経済指標カレンダーによる entry 停止設定 |
| `symbols` | alias、明示 map、prefix/suffix 探索、refresh 間隔 |
| `risk` | default lot、symbol/profile 別 lot、min/max lot |
| `entry` | 同方向ポジション重複エントリーの抑止 |

設定キーを増やす場合は、`AppConfig.load`、GUI の読み書き、`config.example.json`、README、runbook を合わせて更新する。

## Webhook 処理フロー

1. `ThreadingHTTPServer` が `POST /webhook` を受ける。
2. body を `parse_webhook_payload` で JSON、JSON string、key-value、plain text の順に解釈する。
3. `webhook.secret` が設定されている場合、payload、query、`X-Webhook-Secret` のいずれかと照合する。
4. strategy fill alert が決済・縮小と判断された場合は `strategy_fill_not_entry` で skip する。
5. `parse_action` と `parse_symbol_list` で action と対象 symbol を決める。
6. `trading_pause` のJST時間帯に入っている場合は `trading_pause` で skip する。`entry_only: true` なら close は通し、`notify_on_skip: true` なら停止した entry の Discord 通知は送る。
7. `news_filter` が有効で関連通貨の重要指標前後に入っている場合は、entry を `news_filter` で skip する。close は止めない。
8. `SymbolResolver.canonicalize` で canonical symbol を決め、`routing` または payload 指定から発注先 profile を選ぶ。
9. profile ごとに `SymbolResolver.resolve` で MT5 実シンボルを決める。
10. entry の場合、必要に応じて同方向ポジションを確認し、ロットを決め、`MT5Trader.market_order` を呼ぶ。
11. close の場合、`MT5Trader.close_positions` を呼ぶ。
12. 銘柄・profile ごとの結果を `results[]` に積んで JSON で返す。

## strategy fill alert

TradingView strategy fill alert は専用の正規表現で解析される。対応 strategy は `STRATEGY_ALERT_PROFILES` と `DISCORD_STRATEGY_LABELS` が正本。

重要な前提:

- bridge は entry だけ送る。
- TP、SL、決済、縮小は別 EA に任せる。
- `entry_only: true` のため、SL / TP は発注 request に入れない。
- `skip_scope: strategy` の場合、重複判定は MT5 comment suffix 単位で行う。
- `skip_same_side_across_strategies` が有効な場合は `skip_scope: strategy` より優先し、同一シンボル・方向を全strategy横断で判定する。
- `skip_same_side_position` が有効な通常Webhookと同一 strategy のほぼ同時 alert は、一時 lock file で短時間抑止する。

## MT5 発注フロー

`MT5Trader` は profile ごとに作成される。実発注時の大まかな流れ:

1. `MetaTrader5.initialize()` を端末 path 付き、または path なしで呼ぶ。
2. `login` が設定されている場合は `mt5.login()` を呼ぶ。
3. `symbol_select()` で対象シンボルを有効化する。
4. `symbol_info()` の `volume_min` / `volume_max` / `volume_step` に合わせて lot を丸める。
5. `filling_mode` が `auto` の場合は利用可能な mode を試し、成功した mode を起動中メモリに保持する。
6. `order_send()` を呼び、retcode と request をレスポンスに含める。
7. finally で `mt5.shutdown()` を呼ぶ。

## GUI

GUI は `BridgeGUI` がフォーム状態を持ち、`_collect_config_from_ui()` で `AppConfig` に変換する。`Save Config` と `Apply & Restart` は JSON を保存するため、GUI に設定項目を追加した場合はロード、保存、再起動の3経路を確認する。

MT5 端末検出は `psutil` がある場合だけ起動中プロセスから候補を拾う。`Scan/Select` は MT5 API に接続せず、`Check Selected` / `Check All` で account info を取得する。

## 変更時の注意

- `parse_plain_text_payload` は TradingView の自由文に依存するため、推定ルールの変更は既存 alert を壊しやすい。
- `normalize_symbol` は alias、routing、dedupe の共通前提。変更は広範囲に影響する。
- `select_mt5_profile_names` の優先順位は payload 指定、strategy routing、symbol routing、default routing の順。
- `routing.dedupe_same_terminal` は同じ terminal identity への二重発注防止に使う。
