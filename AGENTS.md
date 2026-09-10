# TradingView -> MT5 Bridge エージェント向けガイド

> 対象: OpenAI Codex / GitHub Copilot / Cursor などのAIエージェントおよび支援を受ける開発者
> 目的: 実注文に影響するブリッジを、安全かつ小さな差分で保守する

## 0. 設計思想：3つの軸

個別のエラー修正・テストPASSだけを追って全体の目的（実注文を壊さない安全な改修）を見失わないための判断基準（詳細はワークスペース共通のルート `AGENTS.md` 2章）。能力や探索の深さを一律に下げることでこれを防ぐのではなく、次の3つの軸を判断基準として固定し、目的から外れた瞬間だけ引き戻す。

- **成果の軸**: 完了は「実際に受け取れる成果」で判定する。テストPASS・エラーなしを成果の代用にしない
- **証拠の軸**: 単一の確認手段に頼らない。dry runの結果に加えて実際のpayload・発注requestなど複数経路で裏付ける
- **進行の軸**: 調査（原因の特定）→実装（修正）→検証のフェーズを混在させない。原因を特定する前に直さない。直した後は、直した箇所自体を疑う目でもう一度見る

さらに（objective-integrity-framework由来の補強、詳細はルート`AGENTS.md` 2.1章）:
- **証拠と解釈の分離**: 生ログ・観測事実と原因仮説を分けて扱う。仮説だけで実装に着手しない
- **コンポーネント/システムの区別**: 個別テストのPASSは、それが証明すべき上位のクレーム（全体挙動）まで再確認しないと証明にならない
- **正常系の温存**: ガード・安全チェック追加時は、既存の正常な成功パスを塞いでいないか確認する
- **是正権限の分離**: ユーザーが明示していない仕様・条件は、類似パターンが見つかってもAI判断だけで変更しない
さらに（objective-integrity-framework由来、大きな実装作業のスコープ管理。詳細はルート`AGENTS.md` 2.2章）:
- **BUILD**: 複数箇所にまたがる実装は、部分的に動いただけで満足せず、依頼された連携部分まで一通り作ってから次に進む
- **SWEEP**: 実装した候補全体を一旦フリーズし、最初の失敗箇所だけで手を止めず、影響範囲全体を洗い出す
- **REPAIR**: 洗い出した問題は原因ごとにまとめて直す。対症療法の場当たり修正を繰り返さない
- **ACCEPT**: 個々のテストPASSではなく、依頼の本来の範囲全体に対して受け入れを判定する

### 作業前に確定すること

3ステップ以上または設計判断を伴う変更では、着手前に次を確定する。

- 依頼の本来の目的と、最低限これがないと失敗と言える成果
- リスク分類（下記）と、それに応じた確認の厚さ
- 触ってよい範囲（「5. セキュリティ・運用安全」を含む）
- 完了をどう確認するか

**リスク分類**: 低=調査・ドキュメント確認／通常=`dry_run`環境での修正確認／高=`dry_run: false`・`risk.*`・`mt5_profiles`・`routing.*`の変更、Webhook公開範囲（`host: 0.0.0.0`等）の変更。高リスクほど実注文への影響を明示してからユーザーへ確認する。

### 作業種別ごとの進め方

- **新規実装**（新しいaction・payload形式を追加する）: 存在しない不具合の「原因」を探さない。要求を先に固め、既存のJSON/key-value/plain text解析を壊さないことを設計段階で確認してから実装する
- **既存不具合修正**（誤発注・重複エントリーを直す）: 下記「根本原因の特定」を経てから直す。「症状を消すこと」と「原因を直すこと」は別物として扱う
- **保守**（依存更新・GUIリファクタなど挙動を変えない変更）: 変更前後でpayload解析結果とMT5発注requestが変わらないことを受け入れ条件にする

### 根本原因の特定

- 一つ目のエラーだけを見て修正に移らない。同じ原因が他にも波及していないか確認してから直す
- 症状を隠す場当たり的な例外処理・リトライ追加をしない。修正は最小差分で、根本原因そのものを直す
- **検証手段自体を疑う**: `py_compile`と1回のdry runが通っても、JSON/key-value/plain textの3種のpayload形式すべてで壊れていないかは別。1形式だけの確認を全体の検証完了と扱わない

### 実装後の確認

- 変更前のdry run結果（payload解析・発注request）を、直す前に把握しておく
- 完了報告前に、変更した箇所自体を批判的に見直す
- 高リスク操作は、実行直前に`config.json`の設定が計画時点と変わっていないか再確認する

### スコープドリフトの扱い

作業中に見つけた別の不具合は、今回の依頼と同一原因のものだけその場で含める。それ以外はユーザーへ報告し、無断で着手しない。

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
