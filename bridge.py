#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, request
from urllib.parse import parse_qs, urlparse

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


def normalize_symbol(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def parse_symbol_list(payload: dict[str, Any]) -> list[str]:
    raw_symbols = payload.get("symbols")
    if isinstance(raw_symbols, list):
        return [str(s).strip() for s in raw_symbols if str(s).strip()]

    single = payload.get("symbol") or payload.get("ticker") or payload.get("instrument")
    if isinstance(single, str):
        return [s for s in re.split(r"[\s,]+", single) if s]
    if single is None:
        return []
    return [str(single).strip()]


def to_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "na", "nan", "null", "none"}:
        return None
    return float(value)


STRATEGY_COMMENT_MAX_LEN = 31
PENDING_ENTRY_SECONDS = 10
STRATEGY_ALERT_PROFILES = {
    "rem_bb_pullback_15m": {"label": "15m", "comment": "r15"},
    "rem_bb_pullback_15m_active": {"label": "15m_active", "comment": "r15a"},
    "rem_bb_pullback_30m": {"label": "30m", "comment": "r30"},
    "rem_bb_pullback_30m_active": {"label": "30m_active", "comment": "r30a"},
    "rem_bb_pullback_default": {"label": "default", "comment": "rem"},
    "wemof_original": {"label": "wemof", "comment": "wem"},
    "gate_breaker_tl": {"label": "gate_breaker_tl", "comment": "gbtl"},
}
DISCORD_STRATEGY_LABELS = {
    "rem_bb_pullback_15m": "15mレベル",
    "rem_bb_pullback_15m_active": "15m攻め",
    "rem_bb_pullback_30m": "30mレベル",
    "rem_bb_pullback_30m_active": "30m攻め",
    "rem_bb_pullback_default": "5mレベル",
    "wemof_original": "Wemof",
    "gate_breaker_tl": "Gate Breaker T-L",
}
TIPS_LIST = [
    "🌸 上位足の方向には逆らわないのがコツだよ！",
    "✨ 損切りは『次のチャンスへの入場料』、怖くないよ。",
    "🎀 分からない時は『何もしない』のも立派なトレードだよ。",
    "🌙 深夜の無理なエントリーは、お肌にも資金にも優しくないよ。",
    "💎 利益を追うより、リスクを管理する方がずっと大事だよ。",
    "🌸 監視足の確定を見てから執行足でタイミングを測るのが王道だね！",
    "✨ チャートに張り付くより、心に余裕がある時の方が勝てるかも？",
    "🎯 『勝つこと』より『負けないこと』を意識すると結果がついてくるよ。",
    "🧭 自分のトレードスタイルを持ってる人が最終的に一番強いよ。",
    "🪴 トレードは短距離走じゃなくてマラソン。ゆっくり育てていこうね。",
    "🛡 1回のトレードでリスクは資金の2%以内が安心だよ。",
    "📏 ロットは『負けても平気な量』で入るのが長生きのコツだよ。",
    "⚖️ リスクリワード1:2以上を意識するだけで、勝率50%でも利益が残るよ。",
    "🚫 ナンピンは計画的に。感情のナンピンは資金が溶けるよ…。",
    "💰 含み益は利益じゃないよ。確定して初めてお金になるんだよ。",
    "🧮 勝率よりも期待値。10回中3回でも大きく勝てればプラスだよ。",
    "🪤 全額投入は一発退場の入り口だよ。余力は常に残してね。",
    "📉 最大ドローダウンを想定しておくと、暴落でもパニックにならないよ。",
    "🎯 エントリーの根拠を言葉にできないなら、それはギャンブルかも？",
    "⏰ 指標発表の前後はスプレッドが広がるから気をつけてね。",
    "📊 レンジ相場を無理にトレードしなくていいよ。トレンドを待とう！",
    "🔍 押し目・戻りを待てる人が最終的に勝つよ。焦らないでね。",
    "🏁 利確も技術のうち。欲張りすぎると建値で返されるよ。",
    "🚪 エントリーする前に『どこで逃げるか』を決めておくのが鉄則だよ。",
    "🎣 チャンスは待ってれば来る。飛び乗りエントリーは火傷のもとだよ。",
    "⛳ 『ここで入らなきゃ』は幻想だよ。見送ったチャンスは損失じゃないよ。",
    "🔔 アラートを使えば、チャートを見続けなくてもタイミングを逃さないよ。",
    "🧩 複数の根拠が重なるポイントほど、勝率が高くなるよ。",
    "📈 トレンド初動より、トレンドの途中に乗る方が安全だよ。",
    "🎪 髭で狩られたくなければ、損切り位置に少し余裕を持たせてね。",
    "🧘 負けた後すぐリベンジトレードするのは一番やっちゃダメなパターンだよ。",
    "📓 トレード日記をつけると、自分のクセが見えてくるよ。",
    "☕ 連敗したら一回休憩。相場は明日もあるよ。",
    "🌈 勝ちトレードより、ルール通りにできたトレードを褒めてあげてね。",
    "💤 睡眠不足の判断力は酔っ払いと同じって言われてるよ。ちゃんと寝てね。",
    "🪞 他人のトレードと比べないで。自分のペースが一番大事だよ。",
    "🎭 感情でポジションサイズを変えるのは危険サインだよ。",
    "🫧 SNSの爆益報告は生存者バイアスだよ。惑わされないでね。",
    "🧸 大きく負けた日は、チャートを閉じてお気に入りの動画でも見ようね。",
    "🏔 勝てるようになるまでの道のりは長いけど、続けた人だけがたどり着くよ。",
    "🎵 音楽聴きながらのトレードもアリだよ。リラックスが大事。",
    "📵 ポジション持ったまま寝落ちは危険だよ。逆指値は必ず入れてね。",
    "📅 月曜と金曜はダマシが多いから慎重にね。",
    "🌍 ロンドン時間とNY時間の重なる21〜24時はボラが高まるよ。",
    "🔄 トレンドの転換は一瞬じゃなくて、レンジを経由することが多いよ。",
    "📐 水平線は多くの人が見てるから、それだけで強い根拠になるよ。",
    "🐢 コツコツ積み上げた利益を、一発で飛ばさない仕組みが大事だよ。",
    "🏦 中央銀行の発言は相場を大きく動かすよ。要人発言カレンダーは要チェック。",
    "🌊 相場には波があるよ。波に逆らわず、波に乗る意識を持とうね。",
    "📆 月末・四半期末はリバランスの流れで普段と違う動きが出やすいよ。",
    "🔗 通貨の相関を意識すると、ダマシを減らせるよ。",
    "⛽ ゴールドはリスクオフで買われやすいよ。株が下がった時は注目してね。",
    "🗞 噂で買って事実で売る。織り込み済みの材料で逆に動くこともあるよ。",
    "🧊 ボラが低い時は無理に入らなくていいよ。嵐の前の静けさかもしれないけどね。",
    "📉 下落トレンドは上昇より速いよ。ショートは利確タイミングに注意してね。",
    "🌐 ドルインデックスを見ておくと、ドルストレート全体の方向感が掴めるよ。",
    "🔭 大きな足で方向を見て、小さな足でタイミングを取るのが基本だよ。",
    "🗺 日足で迷ったら週足を見てみて。景色が全然違うよ。",
    "⏳ 5分足で振り回されてたら、一度15分足に切り替えてみて。落ち着くよ。",
    "🎶 MTF分析は上位足→下位足の順番が大事。下から見ると迷子になるよ。",
    "🔬 執行足だけ見てると木を見て森を見ずになるよ。環境認識足も忘れずにね。",
]


def normalize_action(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"buy", "long"} or "買" in raw:
        return "buy"
    if raw in {"sell", "short"} or "売" in raw:
        return "sell"
    return raw


def strategy_comment(base_comment: str, strategy_id: str | None) -> str:
    if not strategy_id:
        return base_comment[:STRATEGY_COMMENT_MAX_LEN]
    suffix = str(STRATEGY_ALERT_PROFILES.get(strategy_id, {}).get("comment") or strategy_id)
    return f"{base_comment}-{suffix}"[:STRATEGY_COMMENT_MAX_LEN]


def strategy_comment_suffix(strategy_id: str | None) -> str | None:
    if not strategy_id:
        return None
    suffix = str(STRATEGY_ALERT_PROFILES.get(strategy_id, {}).get("comment") or strategy_id).strip()
    if not suffix:
        return None
    return f"-{suffix}"


def pending_entry_key(config: "MT5Config", symbol: str, side: str, strategy_id: str | None) -> str | None:
    suffix = strategy_comment_suffix(strategy_id)
    if not suffix:
        return None
    parts = [
        str(config.terminal_path or "").strip().lower(),
        "" if config.login is None else str(config.login),
        str(config.server or "").strip().lower(),
        normalize_symbol(symbol),
        side.lower(),
        suffix,
    ]
    raw = "\0".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def acquire_pending_entry(config: "MT5Config", symbol: str, side: str, strategy_id: str | None) -> Path | None:
    key = pending_entry_key(config, symbol, side, strategy_id)
    if not key:
        return None

    directory = Path(tempfile.gettempdir()) / "tv_mt5_bridge_pending_entries"
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{key}.lock"
    now = time.time()

    try:
        stat = marker.stat()
        if now - stat.st_mtime <= PENDING_ENTRY_SECONDS:
            return None
        marker.unlink()
    except FileNotFoundError:
        pass

    try:
        fd = os.open(str(marker), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"symbol": symbol, "side": side, "strategy_id": strategy_id, "created_at": now}))
    return marker


def release_pending_entry(marker: Path | None) -> None:
    if marker is None:
        return
    try:
        marker.unlink()
    except FileNotFoundError:
        pass


def resolve_strategy_profile(timeframe_label: str, strategy_name: str) -> tuple[str, str] | None:
    label = timeframe_label.lower()
    name = strategy_name.strip()
    if name.startswith("REM BB Pullback Rider V3"):
        if label in {"15mアクティブ", "15m active", "15m_active"}:
            return "rem_bb_pullback_15m_active", "15m_active"
        if label == "15m":
            return "rem_bb_pullback_15m", "15m"
        if label in {"30mアクティブ", "30m active", "30m_active"}:
            return "rem_bb_pullback_30m_active", "30m_active"
        if label == "30m":
            return "rem_bb_pullback_30m", "30m"
        return "rem_bb_pullback_default", "default"
    if name.startswith("Wemof Strategy Original"):
        return "wemof_original", "wemof"
    if name.startswith("Gate Breaker T-L"):
        return "gate_breaker_tl", "gate_breaker_tl"
    return None


def parse_strategy_fill_payload(message: str) -> dict[str, Any] | None:
    match = re.match(
        r"^(?:【(?P<label>[^】]+)】\s*)?"
        r"(?P<strategy>[^:：]+?)\s*[:：]\s*"
        r"(?P<symbol>\S+)\s*で\s*"
        r"(?P<action>[^@\s]+)\s*@\s*"
        r"(?P<contracts>[+-]?\d[\d,]*(?:\.\d+)?)\s*"
        r"の注文が約定しました。?\s*"
        r"新しいストラテジー?の?ポジションは\s*"
        r"(?P<position>[+-]?\d[\d,]*(?:\.\d+)?)\s*です?\s*$",
        message.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None

    strategy_name = match.group("strategy").strip()
    profile = resolve_strategy_profile(match.group("label") or "", strategy_name)
    if profile is None:
        return None

    action = normalize_action(match.group("action"))
    if action not in {"buy", "sell"}:
        raise ValueError(f"Unsupported strategy order action: {match.group('action')!r}")

    position_size = float(match.group("position").replace(",", ""))
    is_entry = (action == "buy" and position_size > 0) or (action == "sell" and position_size < 0)
    strategy_id, strategy_label = profile
    return {
        "raw_message": message,
        "action": action,
        "symbol": match.group("symbol").strip(),
        "strategy_id": strategy_id,
        "strategy_label": strategy_label,
        "strategy_name": strategy_name,
        "strategy_order_contracts": match.group("contracts").replace(",", ""),
        "strategy_position_size": match.group("position").replace(",", ""),
        "strategy_entry_signal": is_entry,
        "entry_only": True,
        "skip_scope": "strategy",
    }


@dataclass
class MT5Config:
    terminal_path: str | None = None
    login: int | None = None
    password: str | None = None
    server: str | None = None
    magic: int = 990001
    deviation: int = 20
    comment: str = "tv-bridge"
    filling_mode: str | int | None = "auto"


@dataclass
class WebhookConfig:
    host: str = "0.0.0.0"
    port: int = 8181
    secret: str = ""


@dataclass
class DiscordConfig:
    enabled: bool = False
    webhook_url: str = ""
    username: str = "半裁量アラート"
    avatar_url: str = ""


@dataclass
class SymbolConfig:
    aliases: dict[str, list[str]] = field(default_factory=dict)
    explicit_map: dict[str, str] = field(default_factory=dict)
    prefixes: list[str] = field(default_factory=lambda: [""])
    suffixes: list[str] = field(default_factory=lambda: ["", ".m", "m", ".pro", "_pro", ".ecn", "-ecn", ".cash"])
    refresh_seconds: int = 300


@dataclass
class RiskConfig:
    default_lot: float = 0.1
    per_symbol: dict[str, float] = field(default_factory=dict)
    per_profile: dict[str, float] = field(default_factory=dict)
    per_profile_symbol: dict[str, dict[str, float]] = field(default_factory=dict)
    min_lot: float = 0.01
    max_lot: float = 100.0


@dataclass
class EntryConfig:
    skip_same_side_position: bool = True


@dataclass
class RoutingConfig:
    default_profile: str | list[str] = "default"
    symbol_profiles: dict[str, str | list[str]] = field(default_factory=dict)
    strategy_profiles: dict[str, str | list[str]] = field(default_factory=dict)
    dedupe_same_terminal: bool = True


@dataclass
class AppConfig:
    mt5: MT5Config = field(default_factory=MT5Config)
    mt5_profiles: dict[str, MT5Config] = field(default_factory=dict)
    routing: RoutingConfig = field(default_factory=RoutingConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    symbols: SymbolConfig = field(default_factory=SymbolConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    dry_run: bool = False

    @staticmethod
    def load(path: Path) -> "AppConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        mt5_cfg = MT5Config(**raw.get("mt5", {}))
        profiles_cfg = {
            str(name): MT5Config(**{**raw.get("mt5", {}), **profile})
            for name, profile in raw.get("mt5_profiles", {}).items()
            if isinstance(profile, dict)
        }
        routing_cfg = RoutingConfig(**raw.get("routing", {}))
        routing_cfg.symbol_profiles = {normalize_symbol(str(k)): v for k, v in routing_cfg.symbol_profiles.items()}
        routing_cfg.strategy_profiles = {str(k): v for k, v in routing_cfg.strategy_profiles.items()}
        webhook_cfg = WebhookConfig(**raw.get("webhook", {}))
        discord_cfg = DiscordConfig(**raw.get("discord", {}))
        symbols_cfg = SymbolConfig(**raw.get("symbols", {}))
        risk_cfg = RiskConfig(**raw.get("risk", {}))
        entry_cfg = EntryConfig(**raw.get("entry", {}))
        return AppConfig(
            mt5=mt5_cfg,
            mt5_profiles=profiles_cfg,
            routing=routing_cfg,
            webhook=webhook_cfg,
            discord=discord_cfg,
            symbols=symbols_cfg,
            risk=risk_cfg,
            entry=entry_cfg,
            dry_run=bool(raw.get("dry_run", False)),
        )

    def mt5_profile_config(self, profile_name: str | None) -> tuple[str, MT5Config]:
        name = str(profile_name or "").strip() or self.routing.default_profile or "default"
        if name == "default":
            return "default", self.mt5
        cfg = self.mt5_profiles.get(name)
        if cfg is None:
            raise ValueError(f"Unknown mt5 profile: {name}")
        return name, cfg

    def mt5_terminal_identity(self, profile_name: str, config: MT5Config) -> str:
        terminal = str(config.terminal_path or "").strip().lower()
        login = "" if config.login is None else str(config.login)
        server = str(config.server or "").strip().lower()
        if terminal:
            return f"path:{terminal}"
        if login or server:
            return f"account:{login}:{server}"
        return f"profile:{profile_name}"


def parse_plain_text_payload(text: str) -> dict[str, Any]:
    message = text.strip()
    if not message:
        raise ValueError("Empty webhook payload")

    out: dict[str, Any] = {"raw_message": message}
    if "|" in message and "=" in message:
        for part in message.split("|"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            k = key.strip().lower()
            if k:
                out[k] = value.strip()
        if "symbol" in out:
            out["symbol"] = str(out["symbol"])
        if "action" in out or "side" in out:
            return out

    strategy_payload = parse_strategy_fill_payload(message)
    if strategy_payload:
        return strategy_payload

    upper = message.upper()
    if re.search(r"\bBUY\b", upper) or "統合サイン↑" in message:
        out["action"] = "buy"
    elif re.search(r"\bSELL\b", upper) or "統合サイン↓" in message:
        out["action"] = "sell"

    match_exchange = re.search(r"\b[A-Z0-9_]+:([A-Z0-9._/-]{2,20})\b", upper)
    if match_exchange:
        out["symbol"] = match_exchange.group(1)
    elif "symbol" not in out:
        tokens = re.findall(r"\b[A-Z][A-Z0-9._/-]{2,20}\b", upper)
        stop_words = {
            "BUY",
            "SELL",
            "LONG",
            "SHORT",
            "TP",
            "SL",
            "M1",
            "M5",
            "M15",
            "M30",
            "H1",
            "H4",
            "D1",
            "W1",
            "AI_ENV_BOOT",
            "AI_ENV_UPDATE",
        }
        for token in tokens:
            if token in stop_words:
                continue
            if re.fullmatch(r"[MHDW]\d+", token):
                continue
            if token.startswith("HTTP"):
                continue
            out["symbol"] = token
            break

    if "action" not in out or "symbol" not in out:
        raise ValueError(
            "Could not infer action/symbol from plain text alert. "
            "Use JSON or include BUY/SELL and symbol in message."
        )
    return out


def parse_webhook_payload(raw_body: bytes) -> dict[str, Any]:
    text = raw_body.decode("utf-8").strip()
    if not text:
        raise ValueError("Empty webhook body")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return parse_plain_text_payload(text)

    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        return parse_plain_text_payload(payload)
    raise ValueError("JSON payload must be an object")


class SymbolResolver:
    def __init__(self, config: SymbolConfig, logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger
        self._alias_lookup = self._build_alias_lookup(config.aliases)
        self._explicit_lookup = {normalize_symbol(k): v for k, v in config.explicit_map.items()}
        self._symbols_cache: list[str] = []
        self._symbols_norm: dict[str, str] = {}
        self._last_refresh = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def _build_alias_lookup(aliases: dict[str, list[str]]) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for canonical, values in aliases.items():
            can = canonical.upper()
            lookup[normalize_symbol(can)] = can
            for alias in values:
                lookup[normalize_symbol(alias)] = can
        return lookup

    def canonicalize(self, raw_symbol: str) -> str:
        return self._alias_lookup.get(normalize_symbol(raw_symbol), raw_symbol.upper())

    def _refresh_symbols(self, force: bool = False) -> list[str]:
        with self._lock:
            now = time.time()
            if self._symbols_cache and not force and now - self._last_refresh < self.config.refresh_seconds:
                return self._symbols_cache

            if mt5 is None:
                return self._symbols_cache

            symbols = mt5.symbols_get() or []
            names = [s.name for s in symbols if getattr(s, "name", None)]
            self._symbols_cache = names
            self._symbols_norm = {name: normalize_symbol(name) for name in names}
            self._last_refresh = now
            self.logger.info("Symbols refreshed: %s", len(names))
            return names

    def _score(self, avail_norm: str, candidate_norm: str) -> int:
        if not candidate_norm:
            return 0
        if avail_norm == candidate_norm:
            return 1000
        if avail_norm.startswith(candidate_norm):
            return 900 - (len(avail_norm) - len(candidate_norm))
        if candidate_norm in avail_norm:
            return 700 - abs(len(avail_norm) - len(candidate_norm))
        if candidate_norm.startswith(avail_norm):
            return 500 - (len(candidate_norm) - len(avail_norm))
        return 0

    def resolve(self, raw_symbol: str) -> tuple[str, str]:
        raw = raw_symbol.strip()
        canonical = self.canonicalize(raw)

        explicit = self._explicit_lookup.get(normalize_symbol(raw)) or self._explicit_lookup.get(normalize_symbol(canonical))
        if explicit:
            return explicit, canonical

        names = self._refresh_symbols()
        if not names:
            return canonical, canonical

        names_set = set(names)
        norm_to_name = {normalize_symbol(name): name for name in names}

        candidates = [raw, canonical]
        aliases = self.config.aliases.get(canonical, [])
        candidates.extend(aliases)

        for base in candidates:
            if base in names_set:
                return base, canonical
            direct = norm_to_name.get(normalize_symbol(base))
            if direct:
                return direct, canonical

        for base in candidates:
            for pre in self.config.prefixes:
                for suf in self.config.suffixes:
                    probe = f"{pre}{base}{suf}"
                    if probe in names_set:
                        return probe, canonical
                    direct = norm_to_name.get(normalize_symbol(probe))
                    if direct:
                        return direct, canonical

        best_name = ""
        best_score = -1
        for avail_name, avail_norm in self._symbols_norm.items():
            for candidate in candidates:
                score = self._score(avail_norm, normalize_symbol(candidate))
                if score > best_score or (score == best_score and avail_name < best_name):
                    best_score = score
                    best_name = avail_name

        if best_name and best_score > 0:
            return best_name, canonical
        return raw, canonical


class LotManager:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config
        self._per_symbol = {k.upper(): float(v) for k, v in config.per_symbol.items()}
        self._per_profile = {str(k): float(v) for k, v in config.per_profile.items()}
        self._per_profile_symbol = {
            str(profile): {str(symbol).upper(): float(lot) for symbol, lot in symbols.items()}
            for profile, symbols in config.per_profile_symbol.items()
            if isinstance(symbols, dict)
        }

    def resolve_lot(self, canonical_symbol: str, payload_lot: Any, profile_name: str | None = None) -> float:
        if payload_lot is not None:
            try:
                lot = float(payload_lot)
                if lot > 0:
                    return self._clamp(lot)
            except (TypeError, ValueError):
                pass

        symbol = canonical_symbol.upper()
        profile = str(profile_name or "").strip()
        lot = self.config.default_lot
        if symbol in self._per_symbol:
            lot = self._per_symbol[symbol]
        if profile in self._per_profile:
            lot = self._per_profile[profile]
        if profile in self._per_profile_symbol and symbol in self._per_profile_symbol[profile]:
            lot = self._per_profile_symbol[profile][symbol]
        return self._clamp(float(lot))

    def _clamp(self, lot: float) -> float:
        return max(self.config.min_lot, min(self.config.max_lot, lot))


class MT5Trader:
    _global_lock = threading.RLock()
    _fill_mode_memory: dict[str, int] = {}

    def __init__(self, config: MT5Config, dry_run: bool, logger: logging.Logger) -> None:
        self.config = config
        self.dry_run = dry_run
        self.logger = logger
        self._lock = MT5Trader._global_lock
        self._connected = False

    def _fill_memory_key(self, symbol: str) -> str:
        terminal = str(self.config.terminal_path or "").strip().lower()
        login = "" if self.config.login is None else str(self.config.login)
        server = str(self.config.server or "").strip().lower()
        return "::".join([terminal, login, server, str(self.config.magic), symbol])

    def _configured_filling_mode(self) -> int | None:
        raw = self.config.filling_mode
        if raw is None:
            return None
        if isinstance(raw, int):
            return raw
        text = str(raw).strip().lower()
        if not text or text == "auto":
            return None
        if text.isdigit():
            return int(text)
        mapping = {
            "fok": int(getattr(mt5, "ORDER_FILLING_FOK", 0)),
            "ioc": int(getattr(mt5, "ORDER_FILLING_IOC", 1)),
            "return": int(getattr(mt5, "ORDER_FILLING_RETURN", 2)),
        }
        return mapping.get(text)

    def connect(self) -> None:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package is not installed. Run: pip install MetaTrader5")
        if self._connected:
            return

        ok = mt5.initialize(path=self.config.terminal_path) if self.config.terminal_path else mt5.initialize()
        if not ok:
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")

        if self.config.login is not None:
            login_ok = mt5.login(
                login=self.config.login,
                password=self.config.password or "",
                server=self.config.server or "",
            )
            if not login_ok:
                raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")

        acc = mt5.account_info()
        self.logger.info("MT5 connected: login=%s server=%s", getattr(acc, "login", None), getattr(acc, "server", None))
        self._connected = True

    def shutdown(self) -> None:
        if mt5 is not None and self._connected:
            mt5.shutdown()
            self._connected = False

    def _fit_volume(self, symbol: str, requested_lot: float) -> float:
        info = mt5.symbol_info(symbol)
        if info is None:
            return requested_lot

        minimum = float(getattr(info, "volume_min", requested_lot))
        maximum = float(getattr(info, "volume_max", requested_lot))
        step = float(getattr(info, "volume_step", 0.01))
        if step <= 0:
            step = 0.01

        lot = max(minimum, min(maximum, requested_lot))
        steps = round(lot / step)
        lot = steps * step
        lot = max(minimum, min(maximum, lot))
        return float(f"{lot:.4f}")

    def _filling_modes(self, symbol: str) -> list[int]:
        info = mt5.symbol_info(symbol)
        memory_key = self._fill_memory_key(symbol)
        remembered = MT5Trader._fill_mode_memory.get(memory_key)
        configured = self._configured_filling_mode()
        fallback = [
            int(getattr(mt5, "ORDER_FILLING_IOC", 1)),
            int(getattr(mt5, "ORDER_FILLING_FOK", 0)),
            int(getattr(mt5, "ORDER_FILLING_RETURN", 2)),
        ]
        mode = getattr(info, "filling_mode", None)
        modes: list[int] = []
        for preferred in (configured, remembered):
            if preferred is not None and preferred not in modes:
                modes.append(preferred)
        if configured is None and isinstance(mode, int):
            constants = [
                int(getattr(mt5, "ORDER_FILLING_FOK", 0)),
                int(getattr(mt5, "ORDER_FILLING_IOC", 1)),
                int(getattr(mt5, "ORDER_FILLING_RETURN", 2)),
            ]
            if mode in constants:
                modes.append(mode)
            for constant in constants:
                if mode & constant and constant not in modes:
                    modes.append(constant)
        for fallback_mode in fallback:
            if fallback_mode not in modes:
                modes.append(fallback_mode)
        return modes

    def _send_order_with_fill_retry(self, req: dict[str, Any], symbol: str) -> dict[str, Any]:
        unsupported_fill = {10030}
        attempts: list[dict[str, Any]] = []
        for filling_mode in self._filling_modes(symbol):
            req["type_filling"] = filling_mode
            result = mt5.order_send(req)
            if result is None:
                attempts.append({"type_filling": filling_mode, "error": str(mt5.last_error())})
                continue
            as_dict = result._asdict() if hasattr(result, "_asdict") else {"retcode": getattr(result, "retcode", None)}
            as_dict["type_filling"] = filling_mode
            retcode = int(as_dict.get("retcode") or 0)
            if retcode not in unsupported_fill:
                MT5Trader._fill_mode_memory[self._fill_memory_key(symbol)] = filling_mode
                if str(self.config.filling_mode or "").strip().lower() == "auto":
                    self.config.filling_mode = filling_mode
                if attempts:
                    as_dict["fill_attempts"] = attempts
                return as_dict
            attempts.append({"type_filling": filling_mode, "retcode": retcode, "comment": as_dict.get("comment", "")})
        if attempts:
            last = attempts[-1].copy()
            last["fill_attempts"] = attempts
            return last
        raise RuntimeError(f"order_send returned None: {mt5.last_error()}")

    def get_position_counts(self, symbol: str, strategy_id: str | None = None) -> dict[str, int]:
        if mt5 is None:
            return {"buy": 0, "sell": 0, "total": 0}

        with self._lock:
            self.connect()
            positions = mt5.positions_get(symbol=symbol) or []
            buy = 0
            sell = 0
            total = 0
            expected_comment = strategy_comment(self.config.comment, strategy_id)
            expected_suffix = strategy_comment_suffix(strategy_id)
            inspected: list[dict[str, Any]] = []
            for pos in positions:
                if strategy_id:
                    inspected.append(
                        {
                            "comment": str(getattr(pos, "comment", "") or ""),
                            "magic": getattr(pos, "magic", None),
                            "type": getattr(pos, "type", None),
                            "volume": getattr(pos, "volume", None),
                        }
                    )
                if strategy_id:
                    comment = str(getattr(pos, "comment", "") or "")
                    if comment != expected_comment and not (expected_suffix and comment.endswith(expected_suffix)):
                        continue
                total += 1
                p_type = int(getattr(pos, "type", -1))
                if p_type == int(mt5.POSITION_TYPE_BUY):
                    buy += 1
                elif p_type == int(mt5.POSITION_TYPE_SELL):
                    sell += 1
            if strategy_id:
                self.logger.info(
                    "Position check: symbol=%s strategy_id=%s expected_comment=%s expected_suffix=%s matched=%s inspected=%s",
                    symbol,
                    strategy_id,
                    expected_comment,
                    expected_suffix,
                    {"buy": buy, "sell": sell, "total": total},
                    inspected[:20],
                )
            return {"buy": buy, "sell": sell, "total": total}

    def market_order(
        self,
        symbol: str,
        side: str,
        lot: float,
        sl: float | None,
        tp: float | None,
        strategy_id: str | None = None,
    ) -> dict[str, Any]:
        comment = strategy_comment(self.config.comment, strategy_id)
        if self.dry_run and mt5 is None:
            return {
                "dry_run": True,
                "request": {
                    "action": "market",
                    "symbol": symbol,
                    "side": side,
                    "lot": lot,
                    "sl": sl,
                    "tp": tp,
                    "comment": comment,
                    "strategy_id": strategy_id,
                },
            }

        with self._lock:
            self.connect()
            if not mt5.symbol_select(symbol, True):
                raise RuntimeError(f"symbol_select failed: {symbol}, last_error={mt5.last_error()}")

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                raise RuntimeError(f"No tick for symbol: {symbol}")

            side_l = side.lower()
            order_type = mt5.ORDER_TYPE_BUY if side_l in {"buy", "long"} else mt5.ORDER_TYPE_SELL
            price = float(tick.ask) if order_type == mt5.ORDER_TYPE_BUY else float(tick.bid)
            volume = self._fit_volume(symbol, lot)

            req: dict[str, Any] = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": order_type,
                "price": price,
                "deviation": self.config.deviation,
                "magic": self.config.magic,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
            }
            if sl is not None and sl > 0:
                req["sl"] = float(sl)
            if tp is not None and tp > 0:
                req["tp"] = float(tp)

            if self.dry_run:
                return {"dry_run": True, "request": req}

            return self._send_order_with_fill_retry(req, symbol)

    def close_positions(self, symbol: str, side_filter: str | None = None) -> list[dict[str, Any]]:
        if self.dry_run and mt5 is None:
            return [
                {
                    "dry_run": True,
                    "request": {
                        "action": "close",
                        "symbol": symbol,
                        "side_filter": side_filter,
                    },
                }
            ]

        with self._lock:
            self.connect()
            positions = mt5.positions_get(symbol=symbol) or []
            out: list[dict[str, Any]] = []
            for pos in positions:
                p_type = int(getattr(pos, "type", -1))
                if side_filter == "buy" and p_type != int(mt5.POSITION_TYPE_BUY):
                    continue
                if side_filter == "sell" and p_type != int(mt5.POSITION_TYPE_SELL):
                    continue

                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    continue

                req = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "position": int(pos.ticket),
                    "volume": float(pos.volume),
                    "type": mt5.ORDER_TYPE_SELL if p_type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                    "price": float(tick.bid) if p_type == mt5.POSITION_TYPE_BUY else float(tick.ask),
                    "deviation": self.config.deviation,
                    "magic": self.config.magic,
                    "comment": f"{self.config.comment}-close",
                    "type_time": mt5.ORDER_TIME_GTC,
                }

                if self.dry_run:
                    out.append({"dry_run": True, "request": req})
                    continue

                out.append(self._send_order_with_fill_retry(req, symbol))
            return out


def parse_action(payload: dict[str, Any]) -> tuple[str, str | None]:
    raw = normalize_action(payload.get("action") or payload.get("side") or "")
    if raw in {"buy", "long"}:
        return "entry", "buy"
    if raw in {"sell", "short"}:
        return "entry", "sell"
    if raw in {"close", "close_all", "flat"}:
        return "close", None
    if raw in {"close_buy", "close_long"}:
        return "close", "buy"
    if raw in {"close_sell", "close_short"}:
        return "close", "sell"
    raise ValueError(f"Unsupported action: {raw!r}")


def normalize_profile_targets(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [item.strip() for item in re.split(r"[\s,]+", text) if item.strip()]


def expand_profile_targets(app_config: AppConfig, targets: list[str]) -> list[str]:
    expanded: list[str] = []
    all_profiles = ["default"] + sorted(app_config.mt5_profiles.keys())
    for target in targets:
        if target.lower() in {"all", "*"}:
            expanded.extend(all_profiles)
        else:
            expanded.append(target)

    deduped: list[str] = []
    seen: set[str] = set()
    for name in expanded:
        if name in seen:
            continue
        seen.add(name)
        deduped.append(name)
    if not app_config.routing.dedupe_same_terminal:
        return deduped

    terminal_deduped: list[str] = []
    seen_terminal: set[str] = set()
    for name in deduped:
        try:
            profile_name, cfg = app_config.mt5_profile_config(name)
            terminal_key = app_config.mt5_terminal_identity(profile_name, cfg)
        except ValueError:
            terminal_key = f"profile:{name}"
        if terminal_key in seen_terminal:
            continue
        seen_terminal.add(terminal_key)
        terminal_deduped.append(name)
    return terminal_deduped


def select_mt5_profile_names(app_config: AppConfig, payload: dict[str, Any], raw_symbol: str, canonical_symbol: str) -> list[str]:
    explicit_multi = payload.get("mt5_profiles") or payload.get("profiles") or payload.get("accounts")
    targets = normalize_profile_targets(explicit_multi)
    if targets:
        return expand_profile_targets(app_config, targets)

    explicit = (
        payload.get("mt5_profile")
        or payload.get("profile")
        or payload.get("account")
        or payload.get("route")
    )
    targets = normalize_profile_targets(explicit)
    if targets:
        return expand_profile_targets(app_config, targets)

    strategy_id = str(payload.get("strategy_id") or "").strip()
    if strategy_id:
        routed = app_config.routing.strategy_profiles.get(strategy_id)
        targets = normalize_profile_targets(routed)
        if targets:
            return expand_profile_targets(app_config, targets)

    for symbol in (raw_symbol, canonical_symbol):
        routed = app_config.routing.symbol_profiles.get(normalize_symbol(str(symbol)))
        targets = normalize_profile_targets(routed)
        if targets:
            return expand_profile_targets(app_config, targets)

    targets = normalize_profile_targets(app_config.routing.default_profile)
    return expand_profile_targets(app_config, targets or ["default"])


def select_mt5_profile_name(app_config: AppConfig, payload: dict[str, Any], raw_symbol: str, canonical_symbol: str) -> str:
    return select_mt5_profile_names(app_config, payload, raw_symbol, canonical_symbol)[0]


def discord_strategy_display(payload: dict[str, Any]) -> str:
    strategy_id = str(payload.get("strategy_id") or "").strip()
    if strategy_id:
        return DISCORD_STRATEGY_LABELS.get(strategy_id, strategy_id)
    strategy_label = str(payload.get("strategy_label") or "").strip()
    if strategy_label:
        return strategy_label
    strategy_name = str(payload.get("strategy_name") or "").strip()
    if strategy_name:
        return strategy_name
    return "アラート"


def build_discord_embed_payload(
    config: DiscordConfig,
    symbol: str,
    side: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    side_label = side.upper()
    side_emoji = "📈" if side.lower() in {"buy", "long"} else "📉"
    title = f"{symbol} {side_label} {side_emoji}"
    strategy = discord_strategy_display(payload)
    tip = random.choice(TIPS_LIST)
    now = jst_now()
    now_label = now.strftime("今日 %H:%M JST")
    color = 0x2ECC71 if side.lower() in {"buy", "long"} else 0xE74C3C

    description = "\n".join(
        [
            f"Tips: {tip}",
            "",
            f"**※必ずご自身でもチャートを確認してください。 | {now_label}**",
        ]
    )
    message: dict[str, Any] = {
        "content": f"[{symbol}] {side_label} {side_emoji} ※{strategy}\n@everyone",
        "allowed_mentions": {"parse": ["everyone"]},
        "embeds": [
            {
                "title": title,
                "description": description,
                "color": color,
                "timestamp": now.isoformat(),
            }
        ],
    }
    if config.username:
        message["username"] = config.username
    if config.avatar_url:
        message["avatar_url"] = config.avatar_url
    return message


def discord_request_headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "tv-mt5-bridge/1.0 (Discord Webhook)",
    }


DISCORD_RETRY_DELAYS = [10, 30, 60, 180, 300]
JST = timezone(timedelta(hours=9), "JST")


def jst_now() -> datetime:
    return datetime.now(JST)


def discord_retry_delay(exc: Exception, fallback_delay: int) -> float:
    if not isinstance(exc, error.HTTPError) or exc.code != 429:
        return float(fallback_delay)

    retry_after_header = exc.headers.get("Retry-After")
    if retry_after_header:
        try:
            return max(float(retry_after_header), 1.0)
        except ValueError:
            pass

    try:
        text = exc.read().decode("utf-8", errors="replace")
        data = json.loads(text)
        retry_after = float(data.get("retry_after", fallback_delay))
        if data.get("global"):
            retry_after += 1.0
        return max(retry_after, 1.0)
    except Exception:
        return float(fallback_delay)


def post_discord_webhook(webhook_url: str, body: bytes) -> None:
    req = request.Request(
        webhook_url,
        data=body,
        headers=discord_request_headers(),
        method="POST",
    )
    with request.urlopen(req, timeout=10) as res:
        res.read()


def send_discord_alert(
    config: DiscordConfig,
    symbol: str,
    side: str,
    payload: dict[str, Any],
    logger: logging.Logger,
) -> None:
    if not config.enabled or not config.webhook_url.strip():
        return

    message = build_discord_embed_payload(config, symbol, side, payload)
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    webhook_url = config.webhook_url.strip()

    for attempt in range(1, len(DISCORD_RETRY_DELAYS) + 2):
        try:
            post_discord_webhook(webhook_url, body)
            if attempt == 1:
                logger.info("Discord alert sent: %s %s", symbol, side.upper())
            else:
                logger.info("Discord alert sent after retry %s: %s %s", attempt - 1, symbol, side.upper())
            return
        except Exception as exc:  # noqa: BLE001
            if attempt > len(DISCORD_RETRY_DELAYS):
                logger.warning("Discord alert failed after %s retries: %s", len(DISCORD_RETRY_DELAYS), exc)
                return
            delay = discord_retry_delay(exc, DISCORD_RETRY_DELAYS[attempt - 1])
            logger.warning(
                "Discord alert failed; retry %s/%s in %.0fs: %s",
                attempt,
                len(DISCORD_RETRY_DELAYS),
                delay,
                exc,
            )
            time.sleep(delay)


def create_handler(
    app_config: AppConfig,
    trader: MT5Trader,
    resolver: SymbolResolver,
    lot_manager: LotManager,
    logger: logging.Logger,
) -> type[BaseHTTPRequestHandler]:
    class WebhookHandler(BaseHTTPRequestHandler):
        def _check_secret(self, payload: dict[str, Any], query: dict[str, list[str]]) -> bool:
            query_secret = query.get("secret", [""])[0]
            header_secret = self.headers.get("X-Webhook-Secret", "")
            secret = str(payload.get("secret") or query_secret or header_secret or "")
            return not app_config.webhook.secret or secret == app_config.webhook.secret

        def _respond(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            query = parse_qs(parsed_path.query)

            if parsed_path.path == "/health":
                if not self._check_secret({}, query):
                    self._respond(403, {"ok": False, "error": "Invalid secret"})
                    return
                self._respond(
                    200,
                    {
                        "ok": True,
                        "service": "tv_mt5_bridge",
                        "webhook_path": "/webhook",
                        "dry_run": app_config.dry_run,
                        "mt5_profiles": ["default"] + sorted(app_config.mt5_profiles.keys()),
                        "message": "Bridge is running. TradingView must POST to /webhook.",
                    },
                )
                return

            if parsed_path.path == "/webhook":
                if not self._check_secret({}, query):
                    self._respond(403, {"ok": False, "error": "Invalid secret"})
                    return
                if query.get("test", [""])[0] in {"1", "true", "yes"}:
                    self._respond(
                        200,
                        {
                            "ok": True,
                            "test": True,
                            "safe": True,
                            "message": "Browser GET test reached the bridge. No MT5 order was sent.",
                            "post_url": "/webhook",
                            "dry_run": app_config.dry_run,
                        },
                    )
                    return
                self._respond(
                    200,
                    {
                        "ok": True,
                        "message": "Use POST /webhook for TradingView alerts. Add ?test=1 for a browser-safe reachability test.",
                        "dry_run": app_config.dry_run,
                    },
                )
                return

            self._respond(404, {"ok": False, "error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            if parsed_path.path != "/webhook":
                self._respond(404, {"ok": False, "error": "Not found"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw_body = self.rfile.read(length)
                payload = parse_webhook_payload(raw_body)
            except Exception as exc:  # noqa: BLE001
                self._respond(400, {"ok": False, "error": f"Invalid payload: {exc}"})
                return

            query = parse_qs(parsed_path.query)
            if not self._check_secret(payload, query):
                self._respond(403, {"ok": False, "error": "Invalid secret"})
                return

            try:
                if payload.get("entry_only") and not payload.get("strategy_entry_signal", False):
                    self._respond(
                        200,
                        {
                            "ok": True,
                            "results": [
                                {
                                    "skipped": True,
                                    "reason": "strategy_fill_not_entry",
                                    "symbol": payload.get("symbol"),
                                    "action": payload.get("action"),
                                    "strategy_id": payload.get("strategy_id"),
                                    "strategy_label": payload.get("strategy_label"),
                                    "strategy_position_size": payload.get("strategy_position_size"),
                                }
                            ],
                        },
                    )
                    return

                action_kind, action_side = parse_action(payload)
                symbols = parse_symbol_list(payload)
                if not symbols:
                    raise ValueError("Missing symbol/symbols field")

                strategy_id = str(payload.get("strategy_id") or "").strip() or None
                strategy_label = str(payload.get("strategy_label") or "").strip()
                entry_only = bool(payload.get("entry_only", False))
                skip_scope = str(payload.get("skip_scope") or "").strip().lower()
                sl = None if entry_only else payload.get("sl")
                tp = None if entry_only else payload.get("tp")
                results: list[dict[str, Any]] = []

                for raw_symbol in symbols:
                    canonical_guess = resolver.canonicalize(raw_symbol)
                    profile_names = select_mt5_profile_names(app_config, payload, raw_symbol, canonical_guess)
                    for requested_profile_name in profile_names:
                        profile_name, profile_cfg = app_config.mt5_profile_config(requested_profile_name)
                        selected_trader = MT5Trader(profile_cfg, app_config.dry_run, logger)
                        selected_resolver = SymbolResolver(app_config.symbols, logger)
                        try:
                            with selected_trader._lock:
                                if mt5 is not None:
                                    selected_trader.connect()
                                resolved_symbol, canonical = selected_resolver.resolve(raw_symbol)
                                if action_kind == "entry":
                                    side = action_side or "buy"
                                    counts = {"buy": 0, "sell": 0, "total": 0}
                                    if app_config.entry.skip_same_side_position:
                                        counts_strategy_id = strategy_id if skip_scope == "strategy" else None
                                        counts = selected_trader.get_position_counts(resolved_symbol, strategy_id=counts_strategy_id)
                                        same_count = counts["buy"] if side in {"buy", "long"} else counts["sell"]

                                        if same_count > 0:
                                            item = {
                                                "raw_symbol": raw_symbol,
                                                "canonical_symbol": canonical,
                                                "resolved_symbol": resolved_symbol,
                                                "mt5_profile": profile_name,
                                                "skipped": True,
                                                "reason": "same_side_position_exists",
                                                "side": side,
                                                "existing_positions": counts,
                                            }
                                            if strategy_id:
                                                item["strategy_id"] = strategy_id
                                                item["strategy_label"] = strategy_label
                                                item["position_scope"] = "strategy"
                                            results.append(item)
                                            continue

                                    lot = lot_manager.resolve_lot(canonical, payload.get("lot"), profile_name)
                                    pending_marker: Path | None = None
                                    if strategy_id and skip_scope == "strategy" and not app_config.dry_run:
                                        pending_marker = acquire_pending_entry(profile_cfg, resolved_symbol, side, strategy_id)
                                        if pending_marker is None:
                                            item = {
                                                "raw_symbol": raw_symbol,
                                                "canonical_symbol": canonical,
                                                "resolved_symbol": resolved_symbol,
                                                "mt5_profile": profile_name,
                                                "skipped": True,
                                                "reason": "duplicate_entry_pending",
                                                "side": side,
                                                "existing_positions": counts,
                                                "strategy_id": strategy_id,
                                                "strategy_label": strategy_label,
                                                "position_scope": "strategy",
                                            }
                                            results.append(item)
                                            continue

                                    try:
                                        result = selected_trader.market_order(
                                            symbol=resolved_symbol,
                                            side=side,
                                            lot=lot,
                                            sl=to_optional_float(sl),
                                            tp=to_optional_float(tp),
                                            strategy_id=strategy_id,
                                        )
                                    except Exception:
                                        release_pending_entry(pending_marker)
                                        raise
                                    retcode = int(result.get("retcode") or 0) if isinstance(result, dict) else 0
                                    if pending_marker is not None and retcode not in {10008, 10009}:
                                        release_pending_entry(pending_marker)
                                    item = {
                                        "raw_symbol": raw_symbol,
                                        "canonical_symbol": canonical,
                                        "resolved_symbol": resolved_symbol,
                                        "mt5_profile": profile_name,
                                        "lot": lot,
                                        "existing_positions": counts,
                                        "result": result,
                                    }
                                    if strategy_id:
                                        item["strategy_id"] = strategy_id
                                        item["strategy_label"] = strategy_label
                                        item["position_scope"] = "strategy"
                                    results.append(item)
                                else:
                                    close_results = selected_trader.close_positions(
                                        symbol=resolved_symbol,
                                        side_filter=action_side,
                                    )
                                    results.append(
                                        {
                                            "raw_symbol": raw_symbol,
                                            "canonical_symbol": canonical,
                                            "resolved_symbol": resolved_symbol,
                                            "mt5_profile": profile_name,
                                            "closed": len(close_results),
                                            "result": close_results,
                                        }
                                    )
                        finally:
                            selected_trader.shutdown()

                if action_kind == "entry":
                    side = action_side or "buy"
                    for raw_symbol in symbols:
                        discord_thread = threading.Thread(
                            target=send_discord_alert,
                            args=(app_config.discord, resolver.canonicalize(raw_symbol), side, payload, logger),
                            name="tv-mt5-discord-alert",
                            daemon=True,
                        )
                        discord_thread.start()

                self._respond(200, {"ok": True, "results": results})
            except Exception as exc:  # noqa: BLE001
                logger.exception("Webhook processing failed")
                self._respond(400, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            logger.info("HTTP %s - %s", self.address_string(), format % args)

    return WebhookHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingView -> MT5 bridge")
    parser.add_argument(
        "-c",
        "--config",
        default="config.json",
        help="Path to config.json",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("tv-mt5-bridge")

    cfg_path = Path(args.config).resolve()
    if not cfg_path.exists():
        raise FileNotFoundError(f"Config not found: {cfg_path}")

    app_config = AppConfig.load(cfg_path)
    trader = MT5Trader(app_config.mt5, app_config.dry_run, logger)
    resolver = SymbolResolver(app_config.symbols, logger)
    lot_manager = LotManager(app_config.risk)

    handler = create_handler(app_config, trader, resolver, lot_manager, logger)
    server = ThreadingHTTPServer((app_config.webhook.host, app_config.webhook.port), handler)
    logger.info("Webhook server started: http://%s:%s/webhook", app_config.webhook.host, app_config.webhook.port)
    logger.info("Dry run mode: %s", app_config.dry_run)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping server")
    finally:
        server.server_close()
        trader.shutdown()


if __name__ == "__main__":
    main()
