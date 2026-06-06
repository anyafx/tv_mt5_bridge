# TradingView -> MT5 Bridge エージェント向けガイド

> 対象: OpenAI Codex / GitHub Copilot / Cursor などのAIエージェントおよび支援を受ける開発者
> 目的: 実注文に影響するブリッジを、安全かつ小さな差分で保守する

## 1. TL;DR

- **実注文リスクを最優先**: `dry_run`、ロット、シンボル解決、発注先 profile、決済 action を変更する場合は必ず影響範囲を説明する。
- **秘密情報を残さない**: `config.json`、MT5 口座情報、Webhook secret、Discord webhook URL をコミット・共有用ドキュメントへ書かない。
- **既存仕様を壊さない**: TradingView alert、plain text 解析、strategy fill alert、GUI 操作の後方互換性を維持する。
- **検証は dry run から**: 変更後は `python -m py_compile bridge.py gui_app.py` と dry run の Webhook 疎通を優先して確認する。
- **ドキュメントを同期**: payload 変更は `docs/specs/webhook-payloads.md`、起動・復旧・設定変更は `docs/operations/runbook.md`、設計変更は `docs/architecture.md` を更新する。
- **単一ファイル肥大化に注意**: 現状は `bridge.py` と `gui_app.py` が中心。大きな機能追加では責務分離を検討し、既存動作を守る。
- **コミュニケーションは日本語**: ユーザーから明示がない限り、説明・レビュー・PR文案は日本語で書く。
- **コミット/プッシュ禁止**: ユーザーから明示的な依頼または許可があるまで、コミットとプッシュは実行しない。

## 2. プロジェクト概要

```text
bridge.py             TradingView Webhook サーバー、payload 解析、MT5 発注ロジック
gui_app.py            tkinter GUI、設定編集、起動停止、疎通テスト
config.example.json   設定例。実運用値は config.json に置く
requirements.txt      Python 依存
docs/                 運用、仕様、設計、開発ルール
```

このプロジェクトは TradingView の alert をローカル Python サーバーで受け取り、MetaTrader 5 Python API 経由で成行注文または決済注文を送る。MT5 端末・口座・ロット設定に依存し、設定ミスは実損につながる。

## 3. 開発環境

基本手順:

```bash
cd tv_mt5_bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.json config.json
```

Windows 運用では `.venv\Scripts\activate` を使う。`MetaTrader5` package と MT5 端末の組み合わせは環境依存が強いため、MT5 実接続が必要な検証はユーザー環境で行う前提にする。

## 4. 実装方針

- `bridge.py` の pure function は可能な限り副作用を増やさず、payload 解析・正規化・ルーティングを分離して扱う。
- `MT5Trader` 周辺は実注文に直結するため、`dry_run` のレスポンスと実発注 request の差分を明確に保つ。
- GUI は `BridgeGUI` の既存パターンに合わせ、設定保存時は `AppConfig` の dataclass 構造を崩さない。
- 新しい設定キーを追加する場合は、`AppConfig.load`、`config.example.json`、README、runbook、GUI の読み書きを同時に確認する。
- strategy fill alert の `strategy_id`、MT5 comment suffix、重複エントリー判定は互換性を崩さない。
- 外部サービスへの送信は Discord webhook のみ。失敗しても発注処理全体を巻き込まない設計を保つ。

## 5. セキュリティ・運用安全

- `webhook.secret` は最低限の共有 secret。外部公開時は IP 制限、認証付きリバースプロキシ、VPN、トンネル側の保護も検討する。
- `host: 0.0.0.0` は LAN または外部から到達可能になる。変更時は公開範囲を明記する。
- ログやエラーに `password`、secret、Discord webhook URL を出さない。
- `dry_run: false`、`risk.*`、`mt5_profiles`、`routing.*` の変更は実注文・複数口座発注・二重発注に影響する。
- `entry.skip_same_side_position` と `skip_scope: strategy` の仕様変更は、重複エントリー防止に直結する。

## 6. テスト・確認

最低限:

```bash
python -m py_compile bridge.py gui_app.py
```

可能なら dry run で以下を確認する。

```bash
python bridge.py -c config.json
curl -X POST http://127.0.0.1:8181/webhook \
  -H 'Content-Type: application/json' \
  -d '{"secret":"CHANGE_ME","symbol":"USDJPY","action":"buy","lot":0.01}'
```

確認観点:

- JSON payload、key-value payload、plain text alert の既存例が壊れていない。
- `buy` / `sell` / `close_all` / `close_buy` / `close_sell` の action が期待どおり処理される。
- `resolved_symbol`、`lot`、`mt5_profiles`、`dry_run` request が想定どおり。
- GUI 変更時は起動、設定ロード、保存、`Apply & Restart`、Webhook test の導線を確認する。

## 7. レビュー観点

- 実注文・決済・ロット・複数 profile への影響が説明されているか。
- 認証なしで注文できる経路が増えていないか。
- secret や口座情報がログ、例示、例外、ドキュメントに漏れていないか。
- 異常系で HTTP status と JSON error が既存仕様と整合しているか。
- MT5 API 接続後に `shutdown()` されるか、GUI スレッドをブロックしないか。
- strategy fill alert の決済・縮小を誤って entry として扱わないか。
- 設定追加時に `config.example.json` と docs が更新されているか。

## 8. 参照ドキュメント

- [README.md](README.md): 導入と主要機能
- [docs/README.md](docs/README.md): ドキュメント索引
- [docs/architecture.md](docs/architecture.md): 構成と処理フロー
- [docs/development/coding-standards.md](docs/development/coding-standards.md): コーディング・変更ルール
- [docs/security.md](docs/security.md): セキュリティと運用安全
- [docs/operations/runbook.md](docs/operations/runbook.md): 起動、検証、障害対応
- [docs/specs/webhook-payloads.md](docs/specs/webhook-payloads.md): Webhook payload 仕様
