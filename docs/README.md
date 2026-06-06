# docs ガイド

## 目的

`docs/` には、TradingView -> MT5 Bridge の運用手順、Webhook 仕様、設計、開発ルール、セキュリティ方針を置く。README は導入用、`docs/` は調査・保守・設定変更時に参照する詳細資料として管理する。

## ディレクトリ構成

```text
docs/
├── README.md
├── architecture.md
├── security.md
├── development/
│   └── coding-standards.md
├── operations/
│   └── runbook.md
└── specs/
    └── webhook-payloads.md
```

## 文書一覧

- [architecture.md](architecture.md): コンポーネント構成、設定モデル、Webhook / MT5 / GUI の処理フロー
- [development/coding-standards.md](development/coding-standards.md): Python / GUI / 設定変更 / 検証 / docs 更新のルール
- [security.md](security.md): Webhook 公開、secret、dry run、本番化、誤発注時の安全方針
- [operations/runbook.md](operations/runbook.md): セットアップ、起動、疎通確認、本番化、障害調査、復旧手順
- [specs/webhook-payloads.md](specs/webhook-payloads.md): JSON / プレーンテキスト payload、action、symbol、lot、レスポンス仕様

## 更新ルール

- 起動・停止・復旧手順を変えたら `operations/` を更新する。
- payload の受け付け項目や解釈を変えたら `specs/` を更新する。
- 設定項目を増やしたら README と `operations/runbook.md` の両方を更新する。
- コンポーネント責務や処理フローを変えたら `architecture.md` を更新する。
- 実注文リスク、公開方法、secret、ロット、複数 profile に関わる変更は `security.md` を更新する。
- 開発・検証・レビュー手順を変えたら `development/coding-standards.md` とルートの `AGENTS.md` を更新する。
