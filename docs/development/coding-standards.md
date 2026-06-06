# コーディング・変更ルール

## 基本方針

- 実注文に影響する変更を小さく保ち、差分の理由を明確にする。
- 既存 payload、plain text alert、strategy fill alert、GUI 設定ファイルとの後方互換性を優先する。
- 設定、仕様、運用手順を変えた場合は docs を同時に更新する。
- secret、password、Discord webhook URL、実口座番号をサンプルやログに残さない。

## Python

- 既存の標準ライブラリ中心の構成を維持する。依存を追加する場合は `requirements.txt` と README に理由を書く。
- 型ヒントは既存の `dict[str, Any]`、`str | None` 形式に合わせる。
- `dataclass` に設定を追加したら、`AppConfig.load` と GUI の `_apply_config_to_ui()` / `_collect_config_from_ui()` を確認する。
- `except Exception` は GUI や外部 API 境界など、ユーザーへエラーを返す境界に限定する。握りつぶす場合は理由が分かるログまたは UI 表示を残す。
- 実注文に関わる分岐では暗黙の型変換を避け、数値・文字列・空値の扱いを明示する。

## bridge.py

- payload 解析、action 判定、profile 選択、MT5 発注を混ぜすぎない。
- `dry_run` と実発注で request の意味が変わらないようにする。
- `parse_action`、`parse_symbol_list`、`parse_plain_text_payload` の変更時は `docs/specs/webhook-payloads.md` を更新する。
- `MT5Trader` では initialize / login / shutdown のライフサイクルを崩さない。
- `strategy_id`、comment suffix、pending entry lock は strategy 別重複抑止の根幹なので、互換性を保つ。

## gui_app.py

- tkinter の UI 更新は main thread 側で行う。重い処理や MT5 接続確認は worker thread と queue の既存パターンに合わせる。
- 設定項目を追加したら、ロード、保存、Apply & Restart、テスト送信の各導線を確認する。
- `messagebox` を出す前に、実注文に繋がる操作かどうかを明確に判定する。
- GUI 文言は既存に合わせて短くし、危険操作では dry run / live の状態が分かるようにする。

## 設定変更

設定キーを増やす場合の更新対象:

- `config.example.json`
- `README.md`
- `docs/operations/runbook.md`
- `docs/specs/webhook-payloads.md` または `docs/architecture.md`
- GUI の該当フォーム
- `AGENTS.md` の注意点、必要な場合

`config.json` は実運用の秘密情報を含むため、サンプル以外に実値を書かない。

## 検証

最低限:

```bash
python -m py_compile bridge.py gui_app.py
```

dry run 例:

```bash
python bridge.py -c config.json
curl -X POST http://127.0.0.1:8181/webhook \
  -H 'Content-Type: application/json' \
  -d '{"secret":"CHANGE_ME","symbol":"USDJPY","action":"buy","lot":0.01}'
```

変更内容に応じた確認:

| 変更 | 確認 |
| --- | --- |
| payload 解析 | JSON、key-value、plain text、strategy fill alert |
| routing | payload profile 指定、symbol routing、strategy routing、default routing |
| lot | payload lot、profile lot、symbol lot、min/max clamp |
| symbol | alias、explicit_map、prefix/suffix、未解決時の fallback |
| GUI | 起動、ロード、保存、Apply & Restart、Webhook test |
| Discord | disabled 時に何もしない、enabled 時に失敗しても発注を妨げない |

## ドキュメント更新

- Webhook の受け付け項目、action、レスポンスを変えたら `docs/specs/webhook-payloads.md`。
- セットアップ、起動、障害対応、本番化手順を変えたら `docs/operations/runbook.md`。
- コンポーネント構成、処理フロー、責務を変えたら `docs/architecture.md`。
- セキュリティ、公開、secret、実注文リスクに関わる変更は `docs/security.md`。
