# セキュリティ・運用安全ガイド

## 目的

TradingView -> MT5 Bridge を外部 alert と実注文の間に置く際のリスク、設定、公開方法、復旧時の注意点をまとめる。

## 主なリスク

| リスク | 影響 |
| --- | --- |
| Webhook URL / secret の漏えい | 第三者が注文・決済 payload を送れる |
| `dry_run: false` の誤設定 | テスト alert が実注文になる |
| シンボル解決ミス | 意図しない銘柄へ注文する |
| ロット設定ミス | 想定以上のポジションを持つ |
| 複数 profile routing ミス | 複数口座へ二重発注する |
| 決済 action の誤解釈 | 既存ポジションを意図せず閉じる |
| 外部公開の過剰許可 | LAN 外から攻撃・誤送信を受ける |

## 秘密情報

以下はドキュメント、ログ、チャット、コミットに残さない。

- `config.json`
- `webhook.secret`
- MT5 の login / password / server
- Discord webhook URL
- 実口座番号、残高、個人名

例示には `CHANGE_ME`、`<YOUR_SECRET>`、`sub_account` などのダミー値を使う。

## Webhook 保護

`webhook.secret` は最低限の共有 secret。外部公開する場合は、可能な限り追加保護を置く。

- VPN や private tunnel を使う。
- リバースプロキシで HTTPS 終端する。
- 送信元 IP 制限を設定する。
- Basic 認証やトンネル側認証を使う。
- OS firewall で不要な inbound を閉じる。

`host: 0.0.0.0` は全 interface で待ち受ける。ローカル検証だけなら `127.0.0.1` を使う。

## dry run と本番化

本番化前の順序:

1. `dry_run: true` で Webhook 到達を確認する。
2. `resolved_symbol` が MT5 の実シンボル名と一致することを確認する。
3. `lot` が想定どおりで、`risk.max_lot` が安全な上限になっていることを確認する。
4. `mt5_profiles` と `routing` が意図した口座だけを指すことを確認する。
5. デモ口座または最小ロットで検証する。
6. `dry_run: false` に切り替え、最小限の alert から始める。

`dry_run: false` のときは GUI の `Send Webhook Test` や `Send MT5 Test Order` も実注文になり得る。操作前の確認ダイアログを維持する。

## シンボルとロット

- ブローカー固有 suffix がある銘柄は `symbols.explicit_map` で固定する。
- alias を追加する場合は、別銘柄へ寄らないか確認する。
- `risk.per_symbol`、`risk.per_profile`、`risk.per_profile_symbol` は優先順位を理解して変更する。
- `risk.max_lot` は事故時の上限として機能するため、安易に大きくしない。

## 複数 MT5 profile

複数 profile は二重発注リスクがある。

- `routing.default_profile` に `all` を設定すると全 profile へ送る。
- payload の `mt5_profiles: "all"` も全 profile へ送る。
- `routing.dedupe_same_terminal: true` は同じ terminal identity への二重発注を抑止する。
- 意図的に同一端末へ複数回発注する場合以外、`dedupe_same_terminal` は `true` を維持する。

## 障害・インシデント対応

誤発注や不審な Webhook を疑う場合:

1. ブリッジを停止する。
2. TradingView alert を停止する。
3. MT5 側でポジションと注文履歴を確認する。
4. `webhook.secret` を変更する。
5. `dry_run: true` に戻して再起動する。
6. payload、routing、lot、symbol 解決を確認してから本番へ戻す。

secret 漏えい時は、TradingView 側の alert message または URL query も同時に更新する。片方だけ更新すると 403 が発生する。

## ログ

ログに残してよい情報:

- action
- raw / canonical / resolved symbol
- lot
- profile name
- dry run request
- MT5 retcode とエラー概要

ログに残さない情報:

- password
- secret
- Discord webhook URL
- 実口座の個人情報
