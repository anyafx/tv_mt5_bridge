#!/usr/bin/env python3
"""Gaikaex economic calendar Discord notifier with Tkinter GUI.

Python port of gaikaex_calendar_notifier.gs.  The GAS script remains untouched;
this app is a desktop alternative with GUI, tray residency, daily scheduling,
and 15-minute reminder notifications.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta
from html import unescape
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover - dependency/environment fallback
    pystray = None
    Image = None
    ImageDraw = None

import tkinter as tk
from tkinter import StringVar, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
STATE_PATH = APP_DIR / "notification_state.json"
LOG_PATH = APP_DIR / "news_bot.log"
BASE_URL = "https://www.gaikaex.com/gaikaex/mark/calendar/"
JST = ZoneInfo("Asia/Tokyo")
RUN_AT = datetime_time(hour=8, minute=30, tzinfo=JST)
REQUEST_TIMEOUT = 30
DEFAULT_RESULT_POLL_INTERVAL_MINUTES = 2
DEFAULT_RESULT_POLL_ATTEMPTS = 5
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

MAJOR_COUNTRIES = [
    "米国",
    "日本",
    "ユーロ",
    "ドイツ",
    "フランス",
    "スペイン",
    "イタリア",
    "英国",
    "イギリス",
    "オーストラリア",
    "豪",
    "ニュージーランド",
    "NZ",
    "カナダ",
    "スイス",
]


@dataclass(frozen=True)
class CalendarEvent:
    date: str
    time: str
    country: str
    currency: str
    impact: int
    name: str
    forecast: str
    actual: str
    previous: str
    is_yesterday: bool = False

    @property
    def impact_label(self) -> str:
        return "高" if self.impact >= 3 else "中"

    @property
    def reminder_key(self) -> str:
        event_dt = self.event_datetime
        date_time_key = event_dt.isoformat() if event_dt else f"{self.date}T{self.time}"
        return "|".join([date_time_key, self.country, self.name])

    @property
    def event_datetime(self) -> Optional[datetime]:
        if self.time == "--:--":
            return None
        try:
            hour, minute = [int(part) for part in self.time.split(":", 1)]
            y, m, d = [int(part) for part in self.date.split("-", 2)]
            day_offset, normalized_hour = divmod(hour, 24)
            return datetime(y, m, d, normalized_hour, minute, tzinfo=JST) + timedelta(days=day_offset)
        except ValueError:
            return None

    @property
    def has_actual_result(self) -> bool:
        return bool(self.actual and self.actual.strip() not in {"-", "--", "－"})


@dataclass
class CalendarBundle:
    today: str
    today_label: str
    yesterday: str
    yesterday_label: str
    past_medium: List[CalendarEvent]
    past_high: List[CalendarEvent]
    upcoming_medium: List[CalendarEvent]
    upcoming_high: List[CalendarEvent]
    is_weekend: bool

    @property
    def all_events(self) -> List[CalendarEvent]:
        return self.past_medium + self.past_high + self.upcoming_medium + self.upcoming_high


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: "queue.Queue[str]") -> None:
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


def setup_logger(log_queue: "queue.Queue[str]") -> logging.Logger:
    logger = logging.getLogger("news_bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    queue_handler = QueueLogHandler(log_queue)
    queue_handler.setFormatter(formatter)
    logger.addHandler(queue_handler)
    return logger


def load_config() -> Dict[str, object]:
    if not CONFIG_PATH.exists():
        return {
            "webhook_url": "",
            "window_geometry": "1050x720",
            "run_at": "08:30",
            "impact_filter": "medium_high",
            "result_poll_interval_minutes": DEFAULT_RESULT_POLL_INTERVAL_MINUTES,
            "result_poll_attempts": DEFAULT_RESULT_POLL_ATTEMPTS,
        }
    with CONFIG_PATH.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        return {}
    return data


def save_config(config: Dict[str, object]) -> None:
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        json.dump(config, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
    tmp_path.replace(CONFIG_PATH)


def load_notification_state() -> Dict[str, object]:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_notification_state(state: Dict[str, object]) -> None:
    tmp_path = STATE_PATH.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2, sort_keys=True)
        fp.write("\n")
    tmp_path.replace(STATE_PATH)


def format_md(date_value: date) -> str:
    return f"{date_value.month}/{date_value.day}"


def yyyymmdd(date_value: date) -> str:
    return date_value.strftime("%Y%m%d")


def iso_date(date_value: date) -> str:
    return date_value.strftime("%Y-%m-%d")


def country_to_code(country: str) -> str:
    if "米" in country:
        return "USD"
    if "日本" in country:
        return "JPY"
    if any(name in country for name in ["ユーロ", "ドイツ", "フランス", "スペイン", "イタリア"]):
        return "EUR"
    if "英国" in country or "イギリス" in country:
        return "GBP"
    if "オーストラリア" in country or "豪" in country:
        return "AUD"
    if "ニュージーランド" in country or "NZ" in country:
        return "NZD"
    if "カナダ" in country:
        return "CAD"
    if "スイス" in country:
        return "CHF"
    return ""


def event_matches_impact(event: CalendarEvent, impact_filter: str) -> bool:
    if impact_filter == "high":
        return event.impact >= 3
    if impact_filter == "medium":
        return event.impact == 2
    return event.impact >= 2


def filter_events_by_impact(events: Sequence[CalendarEvent], impact_filter: str) -> List[CalendarEvent]:
    return [event for event in events if event_matches_impact(event, impact_filter)]


def filter_bundle_by_impact(bundle: CalendarBundle, impact_filter: str) -> CalendarBundle:
    return CalendarBundle(
        today=bundle.today,
        today_label=bundle.today_label,
        yesterday=bundle.yesterday,
        yesterday_label=bundle.yesterday_label,
        past_medium=bundle.past_medium if impact_filter in {"medium", "medium_high"} else [],
        past_high=bundle.past_high if impact_filter in {"high", "medium_high"} else [],
        upcoming_medium=bundle.upcoming_medium if impact_filter in {"medium", "medium_high"} else [],
        upcoming_high=bundle.upcoming_high if impact_filter in {"high", "medium_high"} else [],
        is_weekend=bundle.is_weekend,
    )


def is_major_country(country: str) -> bool:
    return any(name in country for name in MAJOR_COUNTRIES)


def clean_text(value: str) -> str:
    return " ".join(unescape(value).replace("\xa0", " ").split())


def extract_status_value(container: Tag, label: str) -> str:
    for span in container.select("span.status"):
        if clean_text(span.get_text()) != label:
            continue
        parent = span.parent
        if not isinstance(parent, Tag):
            continue
        text = clean_text(parent.get_text(" ", strip=True))
        if text.startswith(label):
            text = text[len(label):].strip()
        return text or "-"
    return "-"


def first_text(select_result: Sequence[Tag]) -> str:
    if not select_result:
        return ""
    return clean_text(select_result[0].get_text(" ", strip=True))


def fetch_html(url: str) -> str:
    response = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def get_day_blocks(html: str) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    blocks: List[Tuple[str, str]] = []
    for date_title in soup.select("li.date_title"):
        label = clean_text(date_title.get_text())
        parts = [str(date_title)]
        for sibling in date_title.next_siblings:
            if isinstance(sibling, Tag) and "date_title" in sibling.get("class", []):
                break
            parts.append(str(sibling))
        blocks.append((label, "".join(parts)))
    return blocks


def extract_day_block(html: str, date_label: str) -> str:
    for label, block in get_day_blocks(html):
        if label.startswith(f"{date_label}（") or label.startswith(f"{date_label}("):
            return block
    return ""


def parse_events_from_block(block_html: str, event_date: str, is_yesterday: bool) -> List[CalendarEvent]:
    if not block_html:
        return []
    soup = BeautifulSoup(block_html, "html.parser")
    events: List[CalendarEvent] = []

    for box in soup.select("li.data_box"):
        country = first_text(box.select("p.flag"))
        time_value = ""
        for p_tag in box.find_all("p", recursive=True):
            text = clean_text(p_tag.get_text(" ", strip=True))
            if text == "--:--" or len(text) == 5 and text[2] == ":" and text[:2].isdigit() and text[3:].isdigit():
                time_value = text
                break

        impact = 0
        for p_tag in box.find_all("p", recursive=True):
            text = clean_text(p_tag.get_text("", strip=True))
            if "重要度" in text:
                impact = text.count("★")
                break

        name = first_text(box.select("p.index_name"))
        forecast = extract_status_value(box, "予想")
        actual = extract_status_value(box, "結果")
        previous = extract_status_value(box, "前回")

        if not country or not name or impact < 2 or not is_major_country(country):
            continue

        events.append(
            CalendarEvent(
                date=event_date,
                time=time_value or "--:--",
                country=country,
                currency=country_to_code(country),
                impact=impact,
                name=name,
                forecast=forecast,
                actual=actual,
                previous=previous,
                is_yesterday=is_yesterday,
            )
        )
    return events


def split_past_upcoming(events: Iterable[CalendarEvent], now: datetime) -> Tuple[List[CalendarEvent], List[CalendarEvent]]:
    past: List[CalendarEvent] = []
    upcoming: List[CalendarEvent] = []
    for event in events:
        event_dt = event.event_datetime
        if event.is_yesterday or event_dt is None or event_dt < now:
            past.append(event)
        else:
            upcoming.append(event)
    return past, upcoming


def fetch_calendar_bundle(now: Optional[datetime] = None) -> CalendarBundle:
    now = now or datetime.now(JST)
    today_date = now.date()
    is_weekend = now.weekday() >= 5

    today_label = format_md(today_date)
    today_iso = iso_date(today_date)

    html_today = fetch_html(BASE_URL)

    today_block = "" if is_weekend else extract_day_block(html_today, today_label)

    today_events = parse_events_from_block(today_block, today_iso, False)
    past_today, upcoming_today = split_past_upcoming(today_events, now)

    past_medium = [event for event in past_today if event.impact == 2]
    past_high = [event for event in past_today if event.impact >= 3]
    upcoming_medium = [event for event in upcoming_today if event.impact == 2]
    upcoming_high = [event for event in upcoming_today if event.impact >= 3]

    return CalendarBundle(
        today=today_iso,
        today_label=today_label,
        yesterday="",
        yesterday_label="",
        past_medium=past_medium,
        past_high=past_high,
        upcoming_medium=upcoming_medium,
        upcoming_high=upcoming_high,
        is_weekend=is_weekend,
    )


def chunked(items: Sequence[CalendarEvent], size: int) -> Iterable[Sequence[CalendarEvent]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def event_to_field(event: CalendarEvent) -> Dict[str, object]:
    return {
        "name": f"{event.time} / {event.country} / 重要度:{event.impact_label}",
        "value": f"📌 **{event.name}**",
        "inline": False,
    }


def build_daily_payloads(bundle: CalendarBundle, now: Optional[datetime] = None) -> List[Dict[str, object]]:
    now = now or datetime.now(JST)
    payloads: List[Dict[str, object]] = []

    def add_embed(title: str, color: int, events: Sequence[CalendarEvent]) -> None:
        for index, group in enumerate(chunked(events, 25)):
            if not group:
                continue
            payloads.append(
                {
                    "embeds": [
                        {
                            "title": f"{title}{'(続き)' if index > 0 else ''}",
                            "color": color,
                            "fields": [event_to_field(event) for event in group],
                            "timestamp": now.isoformat(),
                        }
                    ]
                }
            )

    if bundle.is_weekend:
        return payloads

    add_embed(
        f"📋 本日の経済指標（重要度・中） {bundle.today_label}",
        0x00BFFF,
        bundle.past_medium,
    )
    add_embed(
        f"📋 本日の経済指標（重要度・高） {bundle.today_label}",
        0x00BFFF,
        bundle.past_high,
    )
    add_embed(f"⏳ 本日の経済指標（これから・中） {bundle.today_label}", 0xFFD700, bundle.upcoming_medium)
    add_embed(f"⏳ 本日の経済指標（これから・高） {bundle.today_label}", 0xFF0000, bundle.upcoming_high)

    if not payloads:
        payloads.append({"content": f"⚠️ 本日({bundle.today_label}) 重要度中以上指標はありませんでした"})
        return payloads

    high_watch = sorted({event.currency for event in bundle.upcoming_high if event.currency})
    payloads.append(
        {
            "embeds": [
                {
                    "title": f"🎯 本日の警戒通貨（高重要度） {bundle.today_label}",
                    "color": 0xFF0000,
                    "fields": [{"name": "通貨", "value": ", ".join(high_watch) if high_watch else "特になし", "inline": False}],
                    "timestamp": now.isoformat(),
                }
            ]
        }
    )
    return payloads


def build_reminder_payload(event: CalendarEvent) -> Dict[str, object]:
    return {
        "embeds": [
            {
                "title": f"⚠️ 経済指標 15分前 {event.time}（{event.country}）",
                "description": (
                    f"📌 **{event.name}**\n"
                    f"🔥 重要度: {event.impact_label}\n"
                    f"📈 予想: {event.forecast}\n"
                    f"📉 前回: {event.previous}"
                ),
                "color": 0xFFD700 if event.impact == 2 else 0xFF0000,
                "timestamp": datetime.now(JST).isoformat(),
            }
        ]
    }


def build_result_payload(event: CalendarEvent) -> Dict[str, object]:
    return {
        "embeds": [
            {
                "title": f"✅ 経済指標 結果 {event.time}（{event.country}）",
                "description": (
                    f"📌 **{event.name}**\n"
                    f"🔥 重要度: {event.impact_label}\n"
                    f"📈 予想: {event.forecast}\n"
                    f"✅ 結果: {event.actual}\n"
                    f"📉 前回: {event.previous}"
                ),
                "color": 0x00BFFF if event.impact == 2 else 0xFF0000,
                "timestamp": datetime.now(JST).isoformat(),
            }
        ]
    }


def post_discord(webhook_url: str, payload: Dict[str, object]) -> None:
    if not webhook_url:
        raise RuntimeError("Discord Webhook URLが未設定です")
    response = requests.post(webhook_url, json=payload, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()


class NewsBotApp:
    def __init__(self) -> None:
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.ui_queue: "queue.Queue[Tuple[str, object]]" = queue.Queue()
        self.logger = setup_logger(self.log_queue)
        self.config = load_config()
        self.notification_state = load_notification_state()
        self.events: List[CalendarEvent] = []
        self.last_fetch_date: Optional[str] = None
        self.sent_daily_dates: set[str] = set(self._state_list("sent_daily_dates"))
        self.sent_reminder_keys: set[str] = set(self._state_list("sent_reminder_keys"))
        self.sent_result_keys: set[str] = set(self._state_list("sent_result_keys"))
        self._prune_notification_state(datetime.now(JST).date())
        self.result_poll_state: Dict[str, Dict[str, object]] = {}
        self.stop_event = threading.Event()
        self.worker_thread: Optional[threading.Thread] = None
        self.tray_icon = None

        self.root = tk.Tk()
        self.root.title("News Bot")
        self.root.geometry(str(self.config.get("window_geometry") or "1050x720"))

        self.webhook_var = StringVar(value=str(self.config.get("webhook_url") or ""))
        self.impact_filter_var = StringVar(value=str(self.config.get("impact_filter") or "medium_high"))
        self.result_poll_interval_var = StringVar(
            value=str(self.config.get("result_poll_interval_minutes") or DEFAULT_RESULT_POLL_INTERVAL_MINUTES)
        )
        self.result_poll_attempts_var = StringVar(
            value=str(self.config.get("result_poll_attempts") or DEFAULT_RESULT_POLL_ATTEMPTS)
        )
        self.status_var = StringVar(value="停止中")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.root.bind("<Unmap>", self._on_unmap)
        self.root.after(200, self._drain_queues)
        self.start_background_worker()

    def _state_list(self, key: str) -> List[str]:
        value = self.notification_state.get(key)
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]

    def _prune_notification_state(self, today: date) -> None:
        cutoff = today - timedelta(days=7)

        def keep_date(value: str) -> bool:
            try:
                return date.fromisoformat(value) >= cutoff
            except ValueError:
                return False

        def keep_event_key(value: str) -> bool:
            try:
                return date.fromisoformat(value[:10]) >= cutoff
            except ValueError:
                return False

        self.sent_daily_dates = {value for value in self.sent_daily_dates if keep_date(value)}
        self.sent_reminder_keys = {value for value in self.sent_reminder_keys if keep_event_key(value)}
        self.sent_result_keys = {value for value in self.sent_result_keys if keep_event_key(value)}
        self._save_notification_state()

    def _save_notification_state(self) -> None:
        self.notification_state = {
            "sent_daily_dates": sorted(self.sent_daily_dates),
            "sent_reminder_keys": sorted(self.sent_reminder_keys),
            "sent_result_keys": sorted(self.sent_result_keys),
            "updated_at": datetime.now(JST).isoformat(),
        }
        save_notification_state(self.notification_state)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        settings = ttk.LabelFrame(outer, text="設定", padding=10)
        settings.pack(fill="x")
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="Discord Webhook URL").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings, textvariable=self.webhook_var, show="*").grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(settings, text="保存", command=self.save_current_config).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(settings, text="最新取得", command=self.fetch_latest_async).grid(row=0, column=3, padx=(0, 8))

        ttk.Label(settings, text="状態").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Label(settings, textvariable=self.status_var).grid(row=1, column=1, sticky="w", pady=(8, 0))

        ttk.Label(settings, text="重要度").grid(row=2, column=0, sticky="w", pady=(8, 0))
        impact_combo = ttk.Combobox(
            settings,
            textvariable=self.impact_filter_var,
            values=("medium_high", "high", "medium"),
            width=14,
            state="readonly",
        )
        impact_combo.grid(row=2, column=1, sticky="w", pady=(8, 0), padx=(8, 8))
        ttk.Label(settings, text="medium_high=中以上 / high=高のみ / medium=中のみ").grid(
            row=2, column=2, columnspan=3, sticky="w", pady=(8, 0)
        )

        ttk.Label(settings, text="結果再取得").grid(row=3, column=0, sticky="w", pady=(8, 0))
        result_frame = ttk.Frame(settings)
        result_frame.grid(row=3, column=1, columnspan=4, sticky="w", pady=(8, 0), padx=(8, 0))
        ttk.Label(result_frame, text="間隔(分)").pack(side="left")
        ttk.Entry(result_frame, textvariable=self.result_poll_interval_var, width=5).pack(side="left", padx=(6, 14))
        ttk.Label(result_frame, text="回数").pack(side="left")
        ttk.Entry(result_frame, textvariable=self.result_poll_attempts_var, width=5).pack(side="left", padx=(6, 0))

        test_frame = ttk.Frame(settings)
        test_frame.grid(row=4, column=0, columnspan=5, sticky="w", pady=(10, 0))
        ttk.Label(test_frame, text="テスト送信").pack(side="left", padx=(0, 8))
        ttk.Button(test_frame, text="一覧", command=self.test_daily_send_async).pack(side="left", padx=(0, 8))
        ttk.Button(test_frame, text="事前告知", command=self.test_reminder_send_async).pack(side="left", padx=(0, 8))
        ttk.Button(test_frame, text="結果", command=self.test_result_send_async).pack(side="left")

        table_frame = ttk.LabelFrame(outer, text="本日取得した経済指標", padding=10)
        table_frame.pack(fill="both", expand=True, pady=(12, 8))
        columns = ("date", "time", "country", "currency", "impact", "name", "forecast", "actual", "previous")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=16)
        headings = {
            "date": "日付",
            "time": "時刻",
            "country": "国",
            "currency": "通貨",
            "impact": "重要度",
            "name": "指標名",
            "forecast": "予想",
            "actual": "結果",
            "previous": "前回",
        }
        widths = {
            "date": 95,
            "time": 70,
            "country": 110,
            "currency": 60,
            "impact": 70,
            "name": 330,
            "forecast": 100,
            "actual": 100,
            "previous": 100,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w", stretch=column == "name")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)

        log_frame = ttk.LabelFrame(outer, text="リアルタイムログ", padding=10)
        log_frame.pack(fill="both", expand=True)
        self.log_text = ScrolledText(log_frame, height=10, state="disabled")
        self.log_text.pack(fill="both", expand=True)

    def save_current_config(self) -> None:
        self.config["webhook_url"] = self.webhook_var.get().strip()
        self.config["window_geometry"] = self.root.geometry()
        self.config["run_at"] = "08:30"
        self.config["impact_filter"] = self.impact_filter_var.get()
        self.config["result_poll_interval_minutes"] = self._result_poll_interval_minutes()
        self.config["result_poll_attempts"] = self._result_poll_attempts()
        save_config(self.config)
        self.logger.info("設定を保存しました: %s", CONFIG_PATH)

    def _impact_filter(self) -> str:
        value = self.impact_filter_var.get()
        return value if value in {"medium_high", "high", "medium"} else "medium_high"

    def _result_poll_interval_minutes(self) -> int:
        try:
            value = int(self.result_poll_interval_var.get())
        except ValueError:
            return DEFAULT_RESULT_POLL_INTERVAL_MINUTES
        return max(1, value)

    def _result_poll_attempts(self) -> int:
        try:
            value = int(self.result_poll_attempts_var.get())
        except ValueError:
            return DEFAULT_RESULT_POLL_ATTEMPTS
        return max(1, value)

    def start_background_worker(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        self.stop_event.clear()
        self.worker_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self.worker_thread.start()
        self.logger.info("バックグラウンド監視を開始しました")

    def _scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                now = datetime.now(JST)
                self.ui_queue.put(("status", f"監視中: {now.strftime('%Y-%m-%d %H:%M:%S')} JST"))
                if now.weekday() >= 5:
                    time.sleep(60)
                    continue

                if now.hour == 8 and now.minute == 30 and now.strftime("%Y-%m-%d") not in self.sent_daily_dates:
                    self.logger.info("8:30定期送信を開始します")
                    self._send_daily_from_worker(now)

                if self.last_fetch_date != now.strftime("%Y-%m-%d") and not self._has_pending_future_events(now):
                    self._refresh_events_from_worker(now)

                self._send_due_reminders(now)
                self._poll_due_results(now)
            except Exception:
                self.logger.error("バックグラウンド処理でエラー:\n%s", traceback.format_exc())
            self.stop_event.wait(60)

    def _has_pending_future_events(self, now: datetime) -> bool:
        for event in self.events:
            if not event_matches_impact(event, self._impact_filter()):
                continue
            event_dt = event.event_datetime
            if event_dt and event_dt > now:
                return True
        return False

    def _refresh_events_from_worker(self, now: Optional[datetime] = None) -> Optional[CalendarBundle]:
        self.logger.info("経済指標データを取得します")
        bundle = fetch_calendar_bundle(now)
        self.events = bundle.all_events
        self.last_fetch_date = (now or datetime.now(JST)).strftime("%Y-%m-%d")
        filtered_events = filter_events_by_impact(self.events, self._impact_filter())
        self.ui_queue.put(("events", filtered_events))
        self.logger.info("データ取得成功: %d件（表示対象:%d件）", len(self.events), len(filtered_events))
        return bundle

    def _send_daily_from_worker(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now(JST)
        if now.weekday() >= 5:
            self.logger.info("土日のため定期送信をスキップしました")
            return
        bundle = self._refresh_events_from_worker(now)
        if bundle is None:
            return
        bundle = filter_bundle_by_impact(bundle, self._impact_filter())
        webhook = self.webhook_var.get().strip()
        for payload in build_daily_payloads(bundle, now):
            post_discord(webhook, payload)
            time.sleep(0.5)
        self.sent_daily_dates.add(now.strftime("%Y-%m-%d"))
        self._save_notification_state()
        self.logger.info("Discord定期送信完了")

    def _send_due_reminders(self, now: datetime) -> None:
        webhook = self.webhook_var.get().strip()
        if not webhook:
            return
        for event in self.events:
            if not event_matches_impact(event, self._impact_filter()):
                continue
            event_dt = event.event_datetime
            if event_dt is None or event.is_yesterday:
                continue
            reminder_at = event_dt - timedelta(minutes=15)
            if reminder_at <= now < reminder_at + timedelta(minutes=1) and event.reminder_key not in self.sent_reminder_keys:
                post_discord(webhook, build_reminder_payload(event))
                self.sent_reminder_keys.add(event.reminder_key)
                self._save_notification_state()
                self.logger.info("15分前通知を送信しました: %s %s %s", event.time, event.country, event.name)

    def _register_result_poll_targets(self, now: datetime) -> None:
        max_poll_window = timedelta(
            minutes=self._result_poll_interval_minutes() * self._result_poll_attempts() + 1
        )
        for event in self.events:
            if event.is_yesterday or not event_matches_impact(event, self._impact_filter()):
                continue
            event_dt = event.event_datetime
            if event_dt is None or event_dt > now or event.reminder_key in self.sent_result_keys:
                continue
            if now > event_dt + max_poll_window:
                continue
            if event.reminder_key not in self.result_poll_state:
                self.result_poll_state[event.reminder_key] = {
                    "event": event,
                    "attempts": 0,
                    "next_poll": now,
                }

    def _poll_due_results(self, now: datetime) -> None:
        webhook = self.webhook_var.get().strip()
        if not webhook:
            return
        self._register_result_poll_targets(now)
        due_keys = [
            key
            for key, state in self.result_poll_state.items()
            if state.get("next_poll") and state["next_poll"] <= now
        ]
        if not due_keys:
            return

        bundle = fetch_calendar_bundle(now)
        self.events = bundle.all_events
        self.last_fetch_date = now.strftime("%Y-%m-%d")
        self.ui_queue.put(("events", filter_events_by_impact(self.events, self._impact_filter())))
        current_by_key = {event.reminder_key: event for event in self.events}
        interval = timedelta(minutes=self._result_poll_interval_minutes())
        max_attempts = self._result_poll_attempts()

        for key in due_keys:
            state = self.result_poll_state.get(key)
            if not state:
                continue
            state["attempts"] = int(state.get("attempts") or 0) + 1
            current_event = current_by_key.get(key)
            if current_event and current_event.has_actual_result:
                post_discord(webhook, build_result_payload(current_event))
                self.sent_result_keys.add(key)
                self._save_notification_state()
                del self.result_poll_state[key]
                self.logger.info("指標結果を送信しました: %s %s %s", current_event.time, current_event.country, current_event.name)
                continue

            if int(state["attempts"]) >= max_attempts:
                del self.result_poll_state[key]
                original = state.get("event")
                if isinstance(original, CalendarEvent):
                    self.logger.info("結果再取得を終了しました（結果未反映）: %s %s", original.time, original.name)
                continue

            state["next_poll"] = now + interval

    def fetch_latest_async(self) -> None:
        threading.Thread(target=self._fetch_latest_task, daemon=True).start()

    def _fetch_latest_task(self) -> None:
        try:
            self._refresh_events_from_worker(datetime.now(JST))
        except Exception:
            self.logger.error("最新取得に失敗しました:\n%s", traceback.format_exc())

    def test_daily_send_async(self) -> None:
        threading.Thread(target=self._test_daily_send_task, daemon=True).start()

    def _test_daily_send_task(self) -> None:
        try:
            now = datetime.now(JST)
            bundle = self._refresh_events_from_worker(now)
            if bundle is None:
                return
            bundle = filter_bundle_by_impact(bundle, self._impact_filter())
            webhook = self.webhook_var.get().strip()
            payloads = build_daily_payloads(bundle, now)
            if not payloads:
                payloads = [{"content": "News Bot test message"}]
            post_discord(webhook, payloads[0])
            self.logger.info("Discord一覧テスト送信完了")
        except Exception:
            self.logger.error("一覧テスト送信に失敗しました:\n%s", traceback.format_exc())

    def test_reminder_send_async(self) -> None:
        threading.Thread(target=self._test_reminder_send_task, daemon=True).start()

    def _test_reminder_send_task(self) -> None:
        try:
            event = self._select_test_event(prefer_upcoming=True)
            webhook = self.webhook_var.get().strip()
            post_discord(webhook, build_reminder_payload(event))
            self.logger.info("Discord事前告知テスト送信完了")
        except Exception:
            self.logger.error("事前告知テスト送信に失敗しました:\n%s", traceback.format_exc())

    def test_result_send_async(self) -> None:
        threading.Thread(target=self._test_result_send_task, daemon=True).start()

    def _test_result_send_task(self) -> None:
        try:
            event = self._select_test_event(prefer_upcoming=False)
            if not event.has_actual_result:
                event = CalendarEvent(
                    date=event.date,
                    time=event.time,
                    country=event.country,
                    currency=event.currency,
                    impact=event.impact,
                    name=event.name,
                    forecast=event.forecast,
                    actual="テスト結果",
                    previous=event.previous,
                    is_yesterday=event.is_yesterday,
                )
            webhook = self.webhook_var.get().strip()
            post_discord(webhook, build_result_payload(event))
            self.logger.info("Discord結果テスト送信完了")
        except Exception:
            self.logger.error("結果テスト送信に失敗しました:\n%s", traceback.format_exc())

    def test_send_async(self) -> None:
        self.test_daily_send_async()

    def _select_test_event(self, prefer_upcoming: bool) -> CalendarEvent:
        now = datetime.now(JST)
        if not self.events or self.last_fetch_date != now.strftime("%Y-%m-%d"):
            self._refresh_events_from_worker(now)
        candidates = [
            event for event in self.events
            if not event.is_yesterday and event_matches_impact(event, self._impact_filter())
        ]
        if prefer_upcoming:
            upcoming = [event for event in candidates if event.event_datetime and event.event_datetime >= now]
            if upcoming:
                return upcoming[0]
        if candidates:
            return candidates[0]
        return CalendarEvent(
            date=now.strftime("%Y-%m-%d"),
            time=(now + timedelta(minutes=15)).strftime("%H:%M"),
            country="米国",
            currency="USD",
            impact=3,
            name="テスト指標",
            forecast="1.0%",
            actual="-",
            previous="0.8%",
        )

    def _set_events(self, events: List[CalendarEvent]) -> None:
        self.tree.delete(*self.tree.get_children())
        for event in events:
            if event.is_yesterday:
                continue
            self.tree.insert(
                "",
                "end",
                values=(
                    event.date,
                    event.time,
                    event.country,
                    event.currency,
                    event.impact_label,
                    event.name,
                    event.forecast,
                    event.actual,
                    event.previous,
                ),
            )

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_queues(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
                self._append_log(message)
            except queue.Empty:
                break
        while True:
            try:
                kind, value = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "events":
                self._set_events(value)  # type: ignore[arg-type]
            elif kind == "status":
                self.status_var.set(str(value))
        self.root.after(200, self._drain_queues)

    def _on_unmap(self, _event: object) -> None:
        if self.root.state() == "iconic":
            self.hide_to_tray()

    def hide_to_tray(self) -> None:
        self.save_current_config()
        if pystray is None or Image is None or ImageDraw is None:
            self.logger.info("pystray/Pillowが利用できないため、通常の最小化にします")
            self.root.iconify()
            return
        self.root.withdraw()
        self._ensure_tray_icon()
        self.logger.info("タスクトレイへ格納しました")

    def _ensure_tray_icon(self) -> None:
        if self.tray_icon:
            return
        image = Image.new("RGBA", (64, 64), (24, 24, 28, 255))
        draw = ImageDraw.Draw(image)
        draw.rectangle((12, 12, 52, 52), fill=(0, 191, 255, 255))
        draw.text((24, 22), "N", fill=(255, 255, 255, 255))

        self.tray_icon = pystray.Icon(
            "News Bot",
            image,
            "News Bot",
            menu=pystray.Menu(
                pystray.MenuItem("表示", lambda _icon, _item: self.root.after(0, self.show_window)),
                pystray.MenuItem("一覧テスト送信", lambda _icon, _item: self.root.after(0, self.test_daily_send_async)),
                pystray.MenuItem("事前告知テスト送信", lambda _icon, _item: self.root.after(0, self.test_reminder_send_async)),
                pystray.MenuItem("結果テスト送信", lambda _icon, _item: self.root.after(0, self.test_result_send_async)),
                pystray.MenuItem("終了", lambda _icon, _item: self.root.after(0, self.quit_app)),
            ),
        )
        self.tray_icon.run_detached()

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.logger.info("メインウィンドウを表示しました")

    def quit_app(self) -> None:
        if messagebox.askokcancel("終了", "News Botを終了しますか？"):
            self.save_current_config()
            self.stop_event.set()
            if self.tray_icon:
                self.tray_icon.stop()
            self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    app = NewsBotApp()
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
