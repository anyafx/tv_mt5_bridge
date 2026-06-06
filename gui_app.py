#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    import psutil
except ImportError:
    psutil = None

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk
except Exception as exc:  # noqa: BLE001
    raise RuntimeError(f"tkinter is required for GUI mode: {exc}") from exc

from bridge import AppConfig, DiscordConfig, LotManager, MT5Config, MT5Trader, RoutingConfig, SymbolResolver, build_discord_embed_payload, create_handler, discord_request_headers, mt5, normalize_profile_targets, normalize_symbol, select_mt5_profile_names


class QueueLogHandler(logging.Handler):
    def __init__(self, out_queue: queue.Queue[str]) -> None:
        super().__init__()
        self.out_queue = out_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.out_queue.put(msg)
        except Exception:
            pass


def discover_terminals() -> list[str]:
    """Return already-running terminal executable paths without touching MT5 API."""
    found: list[str] = []
    seen: set[str] = set()

    if psutil:
        for proc in psutil.process_iter(attrs=["name", "exe", "cmdline"]):
            try:
                name = (proc.info.get("name") or "").lower()
                exe = proc.info.get("exe") or ""
                cmdline = " ".join(proc.info.get("cmdline") or []).lower()
                exe_l = exe.lower()
                is_mt5_like = (
                    name == "terminal64.exe"
                    or name == "metatrader5.exe"
                    or "metaeditor64.exe" not in name and "metatrader 5" in cmdline
                    or "metaeditor64.exe" not in exe_l and "metatrader 5" in exe_l
                )
                if is_mt5_like and exe and exe not in seen:
                    seen.add(exe)
                    found.append(exe)
            except Exception:
                continue

    return sorted(found)


def probe_terminal(path: str, login: int | None = None, password: str = "", server: str = "") -> dict[str, Any]:
    info: dict[str, Any] = {
        "exe": path,
        "login": "",
        "server": "",
        "balance": "",
        "currency": "",
        "name": "",
        "status": "unverified",
    }
    if mt5 is None:
        info["status"] = "MetaTrader5 package missing"
        return info

    try:
        ok = mt5.initialize(path=path)
        if not ok:
            info["status"] = f"init failed: {mt5.last_error()}"
            return info

        if login is not None:
            mt5.login(login=login, password=password or "", server=server or "")

        acc = mt5.account_info()
        term = mt5.terminal_info()
        if acc:
            info.update(
                {
                    "login": str(getattr(acc, "login", "")),
                    "server": str(getattr(acc, "server", "")),
                    "balance": f"{float(getattr(acc, 'balance', 0.0)):.2f}",
                    "currency": str(getattr(acc, "currency", "")),
                    "name": str(getattr(acc, "name", "")),
                    "status": "ok",
                }
            )
        else:
            info["status"] = "connected (no account_info)"
        if term and not info.get("name"):
            info["name"] = str(getattr(term, "name", ""))
    except Exception as exc:  # noqa: BLE001
        info["status"] = f"error: {exc}"
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass
    return info


def choose_terminal_dialog(master: tk.Misc, login: int | None = None, password: str = "", server: str = "") -> str | None:
    win = tk.Toplevel(master)
    win.title("Select MT5 Terminal")
    win.grab_set()
    win.geometry("1100x420")

    columns = ("exe", "login", "server", "balance", "currency", "name", "status")
    tree = ttk.Treeview(win, columns=columns, show="headings", height=12)
    widths = (390, 90, 200, 90, 70, 150, 140)
    for col, width in zip(columns, widths):
        tree.heading(col, text=col)
        tree.column(col, width=width, anchor="w")

    yscroll = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=yscroll.set)
    tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
    yscroll.grid(row=0, column=1, sticky="ns", pady=8)
    win.rowconfigure(0, weight=1)
    win.columnconfigure(0, weight=1)

    selected: dict[str, str | None] = {"path": None}
    scan_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
    item_by_exe: dict[str, str] = {}
    scan_state: dict[str, Any] = {"running": False, "placeholder": None}

    def _row_values(row: dict[str, Any]) -> tuple[str, str, str, str, str, str, str]:
        return (
            str(row.get("exe", "")),
            str(row.get("login", "")),
            str(row.get("server", "")),
            str(row.get("balance", "")),
            str(row.get("currency", "")),
            str(row.get("name", "")),
            str(row.get("status", "")),
        )

    def _set_buttons_scanning(scanning: bool) -> None:
        scan_state["running"] = scanning
        reload_button.configure(state=tk.DISABLED if scanning else tk.NORMAL)
        check_selected_button.configure(state=tk.DISABLED if scanning else tk.NORMAL)
        check_all_button.configure(state=tk.DISABLED if scanning else tk.NORMAL)

    def _scan_worker() -> None:
        try:
            terminals = discover_terminals()
            if not terminals:
                scan_queue.put(("message", "No running MT5 terminal64.exe candidates found. Use Browse... if needed."))
            for terminal in terminals:
                scan_queue.put(("candidate", terminal))
        except Exception as exc:  # noqa: BLE001
            scan_queue.put(("message", f"scan error: {exc}"))
        finally:
            scan_queue.put(("done", None))

    def _drain_scan_queue() -> None:
        try:
            while True:
                kind, payload = scan_queue.get_nowait()
                if kind == "candidate":
                    placeholder = scan_state.get("placeholder")
                    if placeholder:
                        try:
                            tree.delete(str(placeholder))
                        except tk.TclError:
                            pass
                        scan_state["placeholder"] = None
                    exe = str(payload)
                    if exe not in item_by_exe:
                        item = tree.insert(
                            "",
                            tk.END,
                            values=(exe, "", "", "", "", "", "running"),
                        )
                        item_by_exe[exe] = item
                        if not tree.selection():
                            tree.selection_set(item)
                elif kind == "probe":
                    row = dict(payload)
                    exe = str(row.get("exe", ""))
                    item = item_by_exe.get(exe)
                    if item:
                        tree.item(item, values=_row_values(row))
                elif kind == "message":
                    tree.insert("", tk.END, values=("", "", "", "", "", "", str(payload)))
                elif kind == "done":
                    _set_buttons_scanning(False)
        except queue.Empty:
            pass
        if scan_state["running"]:
            win.after(100, _drain_scan_queue)

    def _set_row_status(path: str, status: str) -> None:
        item = item_by_exe.get(path)
        if not item:
            return
        values = list(tree.item(item, "values"))
        while len(values) < len(columns):
            values.append("")
        values[-1] = status
        tree.item(item, values=values)

    def _selected_terminal_paths() -> list[str]:
        paths: list[str] = []
        for item in tree.selection():
            values = tree.item(item, "values")
            if values and values[0]:
                paths.append(str(values[0]))
        return paths

    def _probe_worker(paths: list[str]) -> None:
        try:
            for path in paths:
                scan_queue.put(("probe", probe_terminal(path, login=login, password=password, server=server)))
        except Exception as exc:  # noqa: BLE001
            scan_queue.put(("message", f"check error: {exc}"))
        finally:
            scan_queue.put(("done", None))

    def _check_paths(paths: list[str]) -> None:
        if scan_state["running"]:
            return
        if not paths:
            messagebox.showwarning("No terminal", "MT5 terminalを選択してください。", parent=win)
            return
        _set_buttons_scanning(True)
        for path in paths:
            _set_row_status(path, "checking...")
        threading.Thread(target=_probe_worker, args=(paths,), name="mt5-terminal-check", daemon=True).start()
        win.after(100, _drain_scan_queue)

    def _check_selected() -> None:
        _check_paths(_selected_terminal_paths())

    def _check_all() -> None:
        _check_paths(list(item_by_exe.keys()))

    def _reload() -> None:
        if scan_state["running"]:
            return
        for item in tree.get_children():
            tree.delete(item)
        item_by_exe.clear()
        _set_buttons_scanning(True)
        scan_state["placeholder"] = tree.insert(
            "",
            tk.END,
            values=("", "", "", "", "", "", "Scanning running MT5 terminals..."),
        )
        threading.Thread(target=_scan_worker, name="mt5-terminal-scan", daemon=True).start()
        win.after(100, _drain_scan_queue)

    def _use_selected() -> None:
        items = tree.selection()
        if not items:
            messagebox.showwarning("No selection", "MT5 terminalを選択してください。", parent=win)
            return
        path = str(tree.item(items[0], "values")[0])
        if not path:
            messagebox.showwarning("No terminal", "MT5 terminalを選択してください。", parent=win)
            return
        selected["path"] = path
        win.destroy()

    def _browse() -> None:
        path = filedialog.askopenfilename(parent=win, title="Select MT5 executable")
        if not path:
            return
        selected["path"] = path
        win.destroy()

    button_row = ttk.Frame(win)
    button_row.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
    reload_button = ttk.Button(button_row, text="Reload", command=_reload)
    reload_button.pack(side=tk.LEFT)
    ttk.Button(button_row, text="Browse...", command=_browse).pack(side=tk.LEFT, padx=6)
    check_selected_button = ttk.Button(button_row, text="Check Selected", command=_check_selected)
    check_selected_button.pack(side=tk.LEFT, padx=6)
    check_all_button = ttk.Button(button_row, text="Check All", command=_check_all)
    check_all_button.pack(side=tk.LEFT)
    ttk.Button(button_row, text="Use", command=_use_selected).pack(side=tk.RIGHT)
    tree.bind("<Double-1>", lambda _event: _use_selected())

    _reload()
    win.wait_window()
    return selected["path"]


class BridgeService:
    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._trader: MT5Trader | None = None
        self._config: AppConfig | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def status_text(self) -> str:
        cfg = self._config
        if self.running and cfg:
            return f"RUNNING  http://{cfg.webhook.host}:{cfg.webhook.port}/webhook  dry_run={cfg.dry_run}"
        return "STOPPED"

    def start(self, config: AppConfig) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("Server is already running")

            trader = MT5Trader(config.mt5, config.dry_run, self.logger)
            resolver = SymbolResolver(config.symbols, self.logger)
            lot_manager = LotManager(config.risk)
            handler = create_handler(config, trader, resolver, lot_manager, self.logger)
            server = ThreadingHTTPServer((config.webhook.host, config.webhook.port), handler)

            def _run() -> None:
                self.logger.info("Webhook server started: http://%s:%s/webhook", config.webhook.host, config.webhook.port)
                self.logger.info("Dry run mode: %s", config.dry_run)
                try:
                    server.serve_forever()
                except Exception as exc:  # noqa: BLE001
                    self.logger.exception("Server loop error: %s", exc)
                finally:
                    server.server_close()
                    trader.shutdown()
                    self.logger.info("Webhook server stopped")

            thread = threading.Thread(target=_run, name="tv-mt5-webhook", daemon=True)
            thread.start()

            self._config = config
            self._trader = trader
            self._server = server
            self._thread = thread

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            trader = self._trader
            self._server = None
            self._thread = None
            self._trader = None

        if server:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
        if thread:
            thread.join(timeout=5)
        if trader:
            trader.shutdown()


class BridgeGUI(tk.Tk):
    def __init__(self, config_path: Path, logger: logging.Logger) -> None:
        super().__init__()
        self.title("TradingView -> MT5 Bridge")
        self.geometry("1050x850")
        self.minsize(1000, 760)
        self.config_path = config_path
        self.logger = logger
        self.service = BridgeService(logger)
        self.mt5_profiles_ui: dict[str, dict[str, Any]] = {}
        self.profile_lots_ui: dict[str, float] = {}
        self.profile_symbol_lots_ui: dict[str, dict[str, float]] = {}

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.log_handler = QueueLogHandler(self.log_queue)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self.logger.addHandler(self.log_handler)

        self._build_vars()
        self._build_layout()
        self._load_or_default()
        self._refresh_status()
        self._drain_logs()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_vars(self) -> None:
        self.var_terminal = tk.StringVar()
        self.var_login = tk.StringVar()
        self.var_password = tk.StringVar()
        self.var_server = tk.StringVar()
        self.var_magic = tk.StringVar(value="990001")
        self.var_deviation = tk.StringVar(value="20")
        self.var_comment = tk.StringVar(value="tv-bridge")
        self.var_fill_mode = tk.StringVar(value="auto")

        self.var_host = tk.StringVar(value="0.0.0.0")
        self.var_port = tk.StringVar(value="8181")
        self.var_secret = tk.StringVar()
        self.var_webhook_url = tk.StringVar()
        self.var_dry_run = tk.BooleanVar(value=True)
        self.var_skip_same_side = tk.BooleanVar(value=True)
        self.var_discord_enabled = tk.BooleanVar(value=False)
        self.var_discord_webhook_url = tk.StringVar()
        self.var_discord_username = tk.StringVar(value="半裁量アラート")
        self.var_discord_avatar_url = tk.StringVar()

        self.var_default_lot = tk.StringVar(value="0.1")
        self.var_test_symbol = tk.StringVar(value="USDJPY")
        self.var_test_side = tk.StringVar(value="buy")
        self.var_test_lot = tk.StringVar(value="0.01")
        self.var_test_profile = tk.StringVar(value="")
        self.var_profile_name = tk.StringVar()
        self.var_profile_terminal = tk.StringVar()
        self.var_profile_login = tk.StringVar()
        self.var_profile_password = tk.StringVar()
        self.var_profile_server = tk.StringVar()
        self.var_profile_magic = tk.StringVar(value="990002")
        self.var_profile_deviation = tk.StringVar(value="20")
        self.var_profile_comment = tk.StringVar(value="tv-bridge-sub")
        self.var_profile_fill_mode = tk.StringVar(value="auto")
        self.var_profile_lot = tk.StringVar()
        self.var_dedupe_same_terminal = tk.BooleanVar(value=True)
        self.var_min_lot = tk.StringVar(value="0.01")
        self.var_max_lot = tk.StringVar(value="10.0")
        self.var_refresh = tk.StringVar(value="300")

        self.var_prefixes = tk.StringVar(value="")
        self.var_suffixes = tk.StringVar(value=", .m, m, .pro, _pro, .ecn, -ecn, .cash")
        self.var_status = tk.StringVar(value="STOPPED")
        self.current_profile_name: str | None = None

        for var in (self.var_host, self.var_port, self.var_secret):
            var.trace_add("write", lambda *_args: self._update_webhook_url())

    def _add_row(self, parent: tk.Misc, row: int, label: str, widget: tk.Widget) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
        widget.grid(row=row, column=1, sticky="ew", padx=6, pady=4)

    def _build_layout(self) -> None:
        body_container = ttk.Frame(self)
        body_container.pack(fill="both", expand=True)
        body_container.rowconfigure(0, weight=1)
        body_container.columnconfigure(0, weight=1)

        self.body_canvas = tk.Canvas(body_container, highlightthickness=0)
        body_scrollbar = ttk.Scrollbar(body_container, orient="vertical", command=self.body_canvas.yview)
        self.body_canvas.configure(yscrollcommand=body_scrollbar.set)
        self.body_canvas.grid(row=0, column=0, sticky="nsew")
        body_scrollbar.grid(row=0, column=1, sticky="ns")

        body = ttk.Frame(self.body_canvas)
        self.body_window = self.body_canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind(
            "<Configure>",
            lambda _event: self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all")),
        )
        self.body_canvas.bind(
            "<Configure>",
            lambda event: self.body_canvas.itemconfigure(self.body_window, width=event.width),
        )
        self.body_canvas.bind_all("<MouseWheel>", self._on_body_mousewheel)
        self.body_canvas.bind_all("<Button-4>", self._on_body_mousewheel)
        self.body_canvas.bind_all("<Button-5>", self._on_body_mousewheel)

        top = ttk.Frame(body)
        top.pack(fill="x", padx=8, pady=8)
        top.columnconfigure(1, weight=1)

        term_row = ttk.Frame(top)
        term_row.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        term_row.columnconfigure(0, weight=1)
        ttk.Entry(term_row, textvariable=self.var_terminal).grid(row=0, column=0, sticky="ew")
        ttk.Button(term_row, text="Scan/Select", command=self._select_terminal).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(term_row, text="Test MT5", command=self._test_terminal).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(top, text="MT5 Terminal").grid(row=0, column=0, sticky="w", padx=6, pady=4)

        self._add_row(top, 1, "Login (optional)", ttk.Entry(top, textvariable=self.var_login))
        self._add_row(top, 2, "Password (optional)", ttk.Entry(top, textvariable=self.var_password, show="*"))
        self._add_row(top, 3, "Server (optional)", ttk.Entry(top, textvariable=self.var_server))
        self._add_row(top, 4, "Magic", ttk.Entry(top, textvariable=self.var_magic))
        self._add_row(top, 5, "Deviation", ttk.Entry(top, textvariable=self.var_deviation))
        self._add_row(top, 6, "Comment", ttk.Entry(top, textvariable=self.var_comment))
        self._add_row(top, 7, "Fill Mode", ttk.Combobox(top, textvariable=self.var_fill_mode, values=("auto", "ioc", "fok", "return"), state="readonly"))
        self._add_row(top, 8, "Host", ttk.Entry(top, textvariable=self.var_host))
        self._add_row(top, 9, "Port", ttk.Entry(top, textvariable=self.var_port))
        self._add_row(top, 10, "Webhook Secret", ttk.Entry(top, textvariable=self.var_secret))
        webhook_row = ttk.Frame(top)
        webhook_row.grid(row=11, column=1, sticky="ew", padx=6, pady=4)
        webhook_row.columnconfigure(0, weight=1)
        ttk.Entry(webhook_row, textvariable=self.var_webhook_url, state="readonly").grid(row=0, column=0, sticky="ew")
        ttk.Button(webhook_row, text="Copy", command=self._copy_webhook_url).grid(row=0, column=1, padx=(6, 0))
        ttk.Label(top, text="TradingView Webhook URL").grid(row=11, column=0, sticky="w", padx=6, pady=4)

        dry_frame = ttk.Frame(top)
        dry_frame.grid(row=12, column=1, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(dry_frame, text="Dry Run", variable=self.var_dry_run).pack(side=tk.LEFT)
        ttk.Checkbutton(dry_frame, text="Skip Same-Side Position", variable=self.var_skip_same_side).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(top, text="Mode").grid(row=12, column=0, sticky="w", padx=6, pady=4)

        notebook = ttk.Notebook(body)
        notebook.pack(fill="both", expand=True, padx=8, pady=6)

        risk_tab = ttk.Frame(notebook)
        symbol_tab = ttk.Frame(notebook)
        route_tab = ttk.Frame(notebook)
        discord_tab = ttk.Frame(notebook)
        logs_tab = ttk.Frame(notebook)
        notebook.add(risk_tab, text="Risk")
        notebook.add(symbol_tab, text="Symbols")
        notebook.add(route_tab, text="MT5 Profiles")
        notebook.add(discord_tab, text="Discord")
        test_tab = ttk.Frame(notebook)
        notebook.add(test_tab, text="Test")
        notebook.add(logs_tab, text="Logs")

        risk_tab.columnconfigure(1, weight=1)
        self._add_row(risk_tab, 0, "Default Lot", ttk.Entry(risk_tab, textvariable=self.var_default_lot))
        self._add_row(risk_tab, 1, "Min Lot", ttk.Entry(risk_tab, textvariable=self.var_min_lot))
        self._add_row(risk_tab, 2, "Max Lot", ttk.Entry(risk_tab, textvariable=self.var_max_lot))
        ttk.Label(risk_tab, text="Per Symbol Lot (JSON)").grid(row=3, column=0, sticky="nw", padx=6, pady=4)
        self.txt_per_symbol = scrolledtext.ScrolledText(risk_tab, height=12)
        self.txt_per_symbol.grid(row=3, column=1, sticky="nsew", padx=6, pady=4)
        risk_tab.rowconfigure(3, weight=1)

        symbol_tab.columnconfigure(1, weight=1)
        self._add_row(symbol_tab, 0, "Refresh Sec", ttk.Entry(symbol_tab, textvariable=self.var_refresh))
        self._add_row(symbol_tab, 1, "Prefixes (comma)", ttk.Entry(symbol_tab, textvariable=self.var_prefixes))
        self._add_row(symbol_tab, 2, "Suffixes (comma)", ttk.Entry(symbol_tab, textvariable=self.var_suffixes))
        ttk.Label(symbol_tab, text="Aliases (JSON)").grid(row=3, column=0, sticky="nw", padx=6, pady=4)
        self.txt_aliases = scrolledtext.ScrolledText(symbol_tab, height=10)
        self.txt_aliases.grid(row=3, column=1, sticky="nsew", padx=6, pady=4)
        ttk.Label(symbol_tab, text="Explicit Map (JSON)").grid(row=4, column=0, sticky="nw", padx=6, pady=4)
        self.txt_explicit = scrolledtext.ScrolledText(symbol_tab, height=8)
        self.txt_explicit.grid(row=4, column=1, sticky="nsew", padx=6, pady=4)
        symbol_tab.rowconfigure(3, weight=1)
        symbol_tab.rowconfigure(4, weight=1)

        route_tab.columnconfigure(1, weight=1)
        route_tab.rowconfigure(0, weight=1)
        profile_outer = ttk.Frame(route_tab)
        profile_outer.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=6, pady=4)
        profile_outer.columnconfigure(1, weight=1)
        profile_outer.rowconfigure(0, weight=1)

        left_profiles = ttk.Frame(profile_outer)
        left_profiles.grid(row=0, column=0, sticky="nsw", padx=(0, 8))
        ttk.Label(left_profiles, text="Profiles").pack(anchor="w")
        self.lst_mt5_profiles = tk.Listbox(left_profiles, height=11, exportselection=False)
        self.lst_mt5_profiles.pack(fill="y", expand=True)
        self.lst_mt5_profiles.bind("<<ListboxSelect>>", lambda _event: self._on_profile_select())
        profile_buttons = ttk.Frame(left_profiles)
        profile_buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(profile_buttons, text="New", command=self._new_profile).pack(side=tk.LEFT)
        ttk.Button(profile_buttons, text="Delete", command=self._delete_profile).pack(side=tk.LEFT, padx=6)

        profile_form = ttk.Frame(profile_outer)
        profile_form.grid(row=0, column=1, sticky="nsew")
        profile_form.columnconfigure(1, weight=1)
        self._add_row(profile_form, 0, "Profile Name", ttk.Entry(profile_form, textvariable=self.var_profile_name))
        profile_term_row = ttk.Frame(profile_form)
        profile_term_row.columnconfigure(0, weight=1)
        ttk.Entry(profile_term_row, textvariable=self.var_profile_terminal).grid(row=0, column=0, sticky="ew")
        ttk.Button(profile_term_row, text="Scan/Select", command=self._select_profile_terminal).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(profile_term_row, text="Test MT5", command=self._test_profile_terminal).grid(row=0, column=2, padx=(6, 0))
        self._add_row(profile_form, 1, "MT5 Terminal", profile_term_row)
        self._add_row(profile_form, 2, "Login (optional)", ttk.Entry(profile_form, textvariable=self.var_profile_login))
        self._add_row(profile_form, 3, "Password (optional)", ttk.Entry(profile_form, textvariable=self.var_profile_password, show="*"))
        self._add_row(profile_form, 4, "Server (optional)", ttk.Entry(profile_form, textvariable=self.var_profile_server))
        self._add_row(profile_form, 5, "Magic", ttk.Entry(profile_form, textvariable=self.var_profile_magic))
        self._add_row(profile_form, 6, "Deviation", ttk.Entry(profile_form, textvariable=self.var_profile_deviation))
        self._add_row(profile_form, 7, "Comment", ttk.Entry(profile_form, textvariable=self.var_profile_comment))
        self._add_row(profile_form, 8, "Fill Mode", ttk.Combobox(profile_form, textvariable=self.var_profile_fill_mode, values=("auto", "ioc", "fok", "return"), state="readonly"))
        self._add_row(profile_form, 9, "Profile Lot", ttk.Entry(profile_form, textvariable=self.var_profile_lot))
        ttk.Label(profile_form, text="Symbol Lots (JSON)").grid(row=10, column=0, sticky="nw", padx=6, pady=4)
        self.txt_profile_symbol_lots = scrolledtext.ScrolledText(profile_form, height=5)
        self.txt_profile_symbol_lots.grid(row=10, column=1, sticky="nsew", padx=6, pady=4)
        profile_form.rowconfigure(10, weight=1)
        ttk.Button(profile_form, text="Apply Profile", command=self._apply_profile_form).grid(row=11, column=1, sticky="w", padx=6, pady=8)

        ttk.Checkbutton(
            route_tab,
            text="Dedupe Same Terminal",
            variable=self.var_dedupe_same_terminal,
        ).grid(row=1, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(route_tab, text="Default Order Targets").grid(row=2, column=0, sticky="nw", padx=6, pady=4)
        self.lst_default_targets = tk.Listbox(route_tab, height=4, selectmode=tk.MULTIPLE, exportselection=False)
        self.lst_default_targets.grid(row=2, column=1, sticky="ew", padx=6, pady=4)
        ttk.Label(route_tab, text="Routing (JSON)").grid(row=3, column=0, sticky="nw", padx=6, pady=4)
        self.txt_routing = scrolledtext.ScrolledText(route_tab, height=10)
        self.txt_routing.grid(row=3, column=1, sticky="nsew", padx=6, pady=4)
        route_tab.rowconfigure(3, weight=1)

        discord_tab.columnconfigure(1, weight=1)
        ttk.Checkbutton(discord_tab, text="Enable Discord Notify", variable=self.var_discord_enabled).grid(
            row=0,
            column=1,
            sticky="w",
            padx=6,
            pady=4,
        )
        ttk.Label(discord_tab, text="Mode").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self._add_row(discord_tab, 1, "Discord Webhook URL", ttk.Entry(discord_tab, textvariable=self.var_discord_webhook_url, show="*"))
        self._add_row(discord_tab, 2, "Bot Name", ttk.Entry(discord_tab, textvariable=self.var_discord_username))
        self._add_row(discord_tab, 3, "Avatar URL", ttk.Entry(discord_tab, textvariable=self.var_discord_avatar_url))
        discord_buttons = ttk.Frame(discord_tab)
        discord_buttons.grid(row=4, column=1, sticky="w", padx=6, pady=8)
        ttk.Button(discord_buttons, text="Send Discord Test", command=self._send_discord_test).pack(side=tk.LEFT)

        test_tab.columnconfigure(1, weight=1)
        self._add_row(test_tab, 0, "Test Symbol", ttk.Entry(test_tab, textvariable=self.var_test_symbol))
        side_combo = ttk.Combobox(test_tab, textvariable=self.var_test_side, values=("buy", "sell"), state="readonly")
        self._add_row(test_tab, 1, "Test Side", side_combo)
        self._add_row(test_tab, 2, "Test Lot", ttk.Entry(test_tab, textvariable=self.var_test_lot))
        self.cmb_test_profile = ttk.Combobox(test_tab, textvariable=self.var_test_profile, values=("", "default", "all"), state="readonly")
        self._add_row(test_tab, 3, "Test MT5 Profile", self.cmb_test_profile)
        test_buttons = ttk.Frame(test_tab)
        test_buttons.grid(row=4, column=1, sticky="w", padx=6, pady=8)
        ttk.Button(test_buttons, text="Send Webhook Test", command=self._send_webhook_test).pack(side=tk.LEFT)
        ttk.Button(test_buttons, text="Send MT5 Test Order", command=self._send_mt5_test_order).pack(side=tk.LEFT, padx=8)
        ttk.Label(
            test_tab,
            text="Webhook Test can auto-start the local server. Test orders use the current Dry Run setting.",
        ).grid(row=5, column=1, sticky="w", padx=6, pady=4)

        self.txt_logs = scrolledtext.ScrolledText(logs_tab, state="disabled")
        self.txt_logs.pack(fill="both", expand=True, padx=6, pady=6)

        footer = ttk.Frame(self)
        footer.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(footer, text="Load Config", command=self._load_or_default).pack(side=tk.LEFT)
        ttk.Button(footer, text="Save Config", command=self._save_config).pack(side=tk.LEFT, padx=6)
        ttk.Button(footer, text="Start", command=self._start_server).pack(side=tk.LEFT, padx=6)
        ttk.Button(footer, text="Stop", command=self._stop_server).pack(side=tk.LEFT, padx=6)
        ttk.Button(footer, text="Apply & Restart", command=self._apply_restart).pack(side=tk.LEFT, padx=6)
        ttk.Label(footer, textvariable=self.var_status).pack(side=tk.RIGHT)

    def _on_body_mousewheel(self, event: tk.Event) -> None:
        widget = self.focus_get()
        if widget is not None:
            widget_class = str(widget.winfo_class())
            if widget_class in {"Text", "Listbox", "Treeview"}:
                return
        if getattr(event, "num", None) == 4:
            self.body_canvas.yview_scroll(-3, "units")
        elif getattr(event, "num", None) == 5:
            self.body_canvas.yview_scroll(3, "units")
        else:
            delta = int(getattr(event, "delta", 0))
            if delta:
                self.body_canvas.yview_scroll(int(-1 * (delta / 120)), "units")

    def _read_json_text(self, widget: scrolledtext.ScrolledText, field_name: str) -> dict[str, Any]:
        raw = widget.get("1.0", tk.END).strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} JSON parse error: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{field_name} must be a JSON object")
        return data

    def _set_json_text(self, widget: scrolledtext.ScrolledText, value: dict[str, Any]) -> None:
        widget.delete("1.0", tk.END)
        widget.insert("1.0", json.dumps(value, ensure_ascii=False, indent=2))

    def _profile_value(self, profile: dict[str, Any], key: str, default: Any = "") -> Any:
        value = profile.get(key)
        return default if value is None else value

    def _profile_form_values(self) -> tuple[str, dict[str, Any]]:
        name = self.var_profile_name.get().strip()
        if not name:
            raise ValueError("Profile Name is required")
        if name == "default":
            raise ValueError("default profile is edited in the top MT5 settings, not in MT5 Profiles")
        login_raw = self.var_profile_login.get().strip()
        return name, {
            "terminal_path": self.var_profile_terminal.get().strip() or None,
            "login": int(login_raw) if login_raw else None,
            "password": self.var_profile_password.get(),
            "server": self.var_profile_server.get().strip() or None,
            "magic": int(self.var_profile_magic.get().strip() or "990002"),
            "deviation": int(self.var_profile_deviation.get().strip() or "20"),
            "comment": self.var_profile_comment.get().strip() or "tv-bridge-sub",
            "filling_mode": self.var_profile_fill_mode.get().strip() or "auto",
        }

    def _profile_lot_values(self, name: str) -> tuple[float | None, dict[str, float]]:
        lot_raw = self.var_profile_lot.get().strip()
        profile_lot = float(lot_raw) if lot_raw else None
        if profile_lot is not None and profile_lot <= 0:
            raise ValueError("Profile Lot must be greater than 0")

        symbol_lots_raw = self._read_json_text(self.txt_profile_symbol_lots, f"{name} symbol_lots")
        symbol_lots = {str(symbol).upper(): float(lot) for symbol, lot in symbol_lots_raw.items()}
        for symbol, lot in symbol_lots.items():
            if lot <= 0:
                raise ValueError(f"Symbol lot for {symbol} must be greater than 0")
        return profile_lot, symbol_lots

    def _set_profile_form(self, name: str, profile: dict[str, Any]) -> None:
        self.var_profile_name.set(name)
        self.var_profile_terminal.set(str(self._profile_value(profile, "terminal_path", "")))
        self.var_profile_login.set("" if profile.get("login") is None else str(profile.get("login")))
        self.var_profile_password.set(str(self._profile_value(profile, "password", "")))
        self.var_profile_server.set(str(self._profile_value(profile, "server", "")))
        self.var_profile_magic.set(str(self._profile_value(profile, "magic", "990002")))
        self.var_profile_deviation.set(str(self._profile_value(profile, "deviation", "20")))
        self.var_profile_comment.set(str(self._profile_value(profile, "comment", "tv-bridge-sub")))
        self.var_profile_fill_mode.set(str(self._profile_value(profile, "filling_mode", "auto")))
        profile_lot = self.profile_lots_ui.get(name)
        self.var_profile_lot.set("" if profile_lot is None else str(profile_lot))
        if hasattr(self, "txt_profile_symbol_lots"):
            self._set_json_text(self.txt_profile_symbol_lots, self.profile_symbol_lots_ui.get(name, {}))
        self.current_profile_name = name or None

    def _refresh_profile_list(self, select_name: str | None = None) -> None:
        self.lst_mt5_profiles.delete(0, tk.END)
        names = sorted(self.mt5_profiles_ui.keys())
        for name in names:
            self.lst_mt5_profiles.insert(tk.END, name)
        if hasattr(self, "cmb_test_profile"):
            self.cmb_test_profile.configure(values=("", "default", "all", *names))
        if hasattr(self, "lst_default_targets"):
            selected = self._get_default_target_selection()
            self.lst_default_targets.delete(0, tk.END)
            for name in ("default", *names):
                self.lst_default_targets.insert(tk.END, name)
            self._set_default_target_selection(selected or ["default"])
        if select_name and select_name in names:
            idx = names.index(select_name)
            self.lst_mt5_profiles.selection_set(idx)
            self.lst_mt5_profiles.see(idx)

    def _get_default_target_selection(self) -> list[str]:
        if not hasattr(self, "lst_default_targets"):
            return []
        return [str(self.lst_default_targets.get(i)) for i in self.lst_default_targets.curselection()]

    def _set_default_target_selection(self, targets: Any) -> None:
        if not hasattr(self, "lst_default_targets"):
            return
        target_list = normalize_profile_targets(targets)
        if not target_list:
            target_list = ["default"]
        if any(t.lower() in {"all", "*"} for t in target_list):
            target_list = ["default"] + sorted(self.mt5_profiles_ui.keys())
        target_set = set(target_list)
        self.lst_default_targets.selection_clear(0, tk.END)
        for idx in range(self.lst_default_targets.size()):
            if str(self.lst_default_targets.get(idx)) in target_set:
                self.lst_default_targets.selection_set(idx)

    def _on_profile_select(self) -> None:
        items = self.lst_mt5_profiles.curselection()
        if not items:
            return
        name = str(self.lst_mt5_profiles.get(items[0]))
        profile = self.mt5_profiles_ui.get(name)
        if profile:
            self._set_profile_form(name, profile)

    def _new_profile(self) -> None:
        if self.current_profile_name:
            if not self._apply_profile_form(silent=True, allow_rename=True):
                return
        idx = 1
        while f"account_{idx}" in self.mt5_profiles_ui:
            idx += 1
        name = f"account_{idx}"
        self.lst_mt5_profiles.selection_clear(0, tk.END)
        self._set_profile_form(
            name,
            {
                "terminal_path": "",
                "login": None,
                "password": "",
                "server": "",
                "magic": 990001 + idx,
                "deviation": 20,
                "comment": f"tv-bridge-{idx}",
                "filling_mode": "auto",
            },
        )

    def _apply_profile_form(self, silent: bool = False, allow_rename: bool = True) -> bool:
        try:
            name, profile = self._profile_form_values()
            old_name = self.current_profile_name
            existing = name in self.mt5_profiles_ui
            if old_name and old_name != name:
                if not allow_rename:
                    raise ValueError("Profile rename is not allowed in this operation")
                if existing and not messagebox.askyesno("Overwrite MT5 Profile", f"Profile '{name}' already exists. Overwrite it?"):
                    return False
                if old_name in self.mt5_profiles_ui:
                    self.mt5_profiles_ui.pop(old_name, None)
                if old_name in self.profile_lots_ui:
                    self.profile_lots_ui[name] = self.profile_lots_ui.pop(old_name)
                if old_name in self.profile_symbol_lots_ui:
                    self.profile_symbol_lots_ui[name] = self.profile_symbol_lots_ui.pop(old_name)
            elif not old_name and existing and not messagebox.askyesno("Overwrite MT5 Profile", f"Profile '{name}' already exists. Overwrite it?"):
                return False
            profile_lot, symbol_lots = self._profile_lot_values(name)
            self.mt5_profiles_ui[name] = profile
            if profile_lot is None:
                self.profile_lots_ui.pop(name, None)
            else:
                self.profile_lots_ui[name] = profile_lot
            if symbol_lots:
                self.profile_symbol_lots_ui[name] = symbol_lots
            else:
                self.profile_symbol_lots_ui.pop(name, None)
            self.current_profile_name = name
            self._refresh_profile_list(name)
            self.logger.info("MT5 profile applied: %s", name)
            if not silent:
                messagebox.showinfo("MT5 Profile", f"Applied: {name}")
            return True
        except Exception as exc:  # noqa: BLE001
            if not silent:
                messagebox.showerror("MT5 Profile", str(exc))
            return False

    def _delete_profile(self) -> None:
        items = self.lst_mt5_profiles.curselection()
        if not items:
            messagebox.showwarning("MT5 Profile", "削除するprofileを選択してください。")
            return
        name = str(self.lst_mt5_profiles.get(items[0]))
        if not messagebox.askyesno("Delete MT5 Profile", f"Delete profile '{name}'?"):
            return
        self.mt5_profiles_ui.pop(name, None)
        self.profile_lots_ui.pop(name, None)
        self.profile_symbol_lots_ui.pop(name, None)
        self._refresh_profile_list()
        self._set_profile_form("", {})
        self.current_profile_name = None

    def _select_profile_terminal(self) -> None:
        try:
            login = int(self.var_profile_login.get().strip()) if self.var_profile_login.get().strip() else None
        except ValueError:
            login = None
        selected = choose_terminal_dialog(
            self,
            login=login,
            password=self.var_profile_password.get(),
            server=self.var_profile_server.get(),
        )
        if selected:
            self.var_profile_terminal.set(selected)
            self.logger.info("Profile terminal selected: %s", selected)

    def _test_profile_terminal(self) -> None:
        path = self.var_profile_terminal.get().strip()
        if not path:
            messagebox.showwarning("No terminal", "まずprofileのMT5 terminal pathを選択してください。")
            return
        try:
            login = int(self.var_profile_login.get().strip()) if self.var_profile_login.get().strip() else None
        except ValueError:
            messagebox.showerror("Invalid login", "Loginは数値で入力してください。")
            return
        password = self.var_profile_password.get()
        server = self.var_profile_server.get()

        def _probe() -> str:
            row = probe_terminal(path, login=login, password=password, server=server)
            self.logger.info("MT5 profile test result: %s", row.get("status", ""))
            return "\n".join(
                [
                    f"status: {row.get('status', '')}",
                    f"login: {row.get('login', '')}",
                    f"server: {row.get('server', '')}",
                    f"balance: {row.get('balance', '')}",
                    f"currency: {row.get('currency', '')}",
                    f"name: {row.get('name', '')}",
                ]
            )

        self._run_worker("MT5 Profile Test", _probe)

    def _apply_config_to_ui(self, cfg: AppConfig) -> None:
        self.var_terminal.set(cfg.mt5.terminal_path or "")
        self.var_login.set("" if cfg.mt5.login is None else str(cfg.mt5.login))
        self.var_password.set(cfg.mt5.password or "")
        self.var_server.set(cfg.mt5.server or "")
        self.var_magic.set(str(cfg.mt5.magic))
        self.var_deviation.set(str(cfg.mt5.deviation))
        self.var_comment.set(cfg.mt5.comment)
        self.var_fill_mode.set(str(cfg.mt5.filling_mode or "auto"))

        self.var_host.set(cfg.webhook.host)
        self.var_port.set(str(cfg.webhook.port))
        self.var_secret.set(cfg.webhook.secret)
        self.var_dry_run.set(cfg.dry_run)
        self.var_skip_same_side.set(cfg.entry.skip_same_side_position)
        self.var_discord_enabled.set(bool(cfg.discord.enabled))
        self.var_discord_webhook_url.set(cfg.discord.webhook_url)
        self.var_discord_username.set(cfg.discord.username)
        self.var_discord_avatar_url.set(cfg.discord.avatar_url)

        self.var_default_lot.set(str(cfg.risk.default_lot))
        self.var_min_lot.set(str(cfg.risk.min_lot))
        self.var_max_lot.set(str(cfg.risk.max_lot))
        self._set_json_text(self.txt_per_symbol, cfg.risk.per_symbol)
        self.profile_lots_ui = {str(name): float(lot) for name, lot in cfg.risk.per_profile.items()}
        self.profile_symbol_lots_ui = {
            str(profile): {str(symbol).upper(): float(lot) for symbol, lot in symbols.items()}
            for profile, symbols in cfg.risk.per_profile_symbol.items()
            if isinstance(symbols, dict)
        }

        self.var_refresh.set(str(cfg.symbols.refresh_seconds))
        self.var_prefixes.set(", ".join(cfg.symbols.prefixes))
        self.var_suffixes.set(", ".join(cfg.symbols.suffixes))
        self._set_json_text(self.txt_aliases, cfg.symbols.aliases)
        self._set_json_text(self.txt_explicit, cfg.symbols.explicit_map)
        self.mt5_profiles_ui = {name: asdict(profile) for name, profile in cfg.mt5_profiles.items()}
        self._refresh_profile_list()
        if self.mt5_profiles_ui:
            first_name = sorted(self.mt5_profiles_ui.keys())[0]
            self._refresh_profile_list(first_name)
            self._set_profile_form(first_name, self.mt5_profiles_ui[first_name])
        else:
            self._set_profile_form("", {})
        self._set_json_text(self.txt_routing, asdict(cfg.routing))
        self.var_dedupe_same_terminal.set(bool(cfg.routing.dedupe_same_terminal))
        self._set_default_target_selection(cfg.routing.default_profile)
        self._update_webhook_url()

    def _collect_config_from_ui(self) -> AppConfig:
        per_symbol = self._read_json_text(self.txt_per_symbol, "per_symbol")
        aliases = self._read_json_text(self.txt_aliases, "aliases")
        explicit_map = self._read_json_text(self.txt_explicit, "explicit_map")
        routing = self._read_json_text(self.txt_routing, "routing")
        if self.var_profile_name.get().strip():
            if not self._apply_profile_form(silent=True):
                raise ValueError("MT5 profile form is invalid")

        login_raw = self.var_login.get().strip()
        mt5_login = int(login_raw) if login_raw else None
        prefixes = [x.strip() for x in self.var_prefixes.get().split(",")] if self.var_prefixes.get().strip() else [""]
        suffixes = [x.strip() for x in self.var_suffixes.get().split(",")] if self.var_suffixes.get().strip() else [""]

        cfg = AppConfig.load(self.config_path) if self.config_path.exists() else AppConfig()

        cfg.mt5.terminal_path = self.var_terminal.get().strip() or None
        cfg.mt5.login = mt5_login
        cfg.mt5.password = self.var_password.get()
        cfg.mt5.server = self.var_server.get().strip() or None
        cfg.mt5.magic = int(self.var_magic.get().strip())
        cfg.mt5.deviation = int(self.var_deviation.get().strip())
        cfg.mt5.comment = self.var_comment.get().strip() or "tv-bridge"
        cfg.mt5.filling_mode = self.var_fill_mode.get().strip() or "auto"

        cfg.webhook.host = self.var_host.get().strip() or "0.0.0.0"
        cfg.webhook.port = int(self.var_port.get().strip())
        cfg.webhook.secret = self.var_secret.get().strip()
        cfg.discord.enabled = bool(self.var_discord_enabled.get())
        cfg.discord.webhook_url = self.var_discord_webhook_url.get().strip()
        cfg.discord.username = self.var_discord_username.get().strip() or "半裁量アラート"
        cfg.discord.avatar_url = self.var_discord_avatar_url.get().strip()
        cfg.dry_run = bool(self.var_dry_run.get())
        cfg.entry.skip_same_side_position = bool(self.var_skip_same_side.get())

        cfg.risk.default_lot = float(self.var_default_lot.get().strip())
        cfg.risk.min_lot = float(self.var_min_lot.get().strip())
        cfg.risk.max_lot = float(self.var_max_lot.get().strip())
        cfg.risk.per_symbol = {str(k).upper(): float(v) for k, v in per_symbol.items()}
        cfg.risk.per_profile = {str(k): float(v) for k, v in self.profile_lots_ui.items()}
        cfg.risk.per_profile_symbol = {
            str(profile): {str(symbol).upper(): float(lot) for symbol, lot in symbols.items()}
            for profile, symbols in self.profile_symbol_lots_ui.items()
        }

        cfg.symbols.refresh_seconds = int(self.var_refresh.get().strip())
        cfg.symbols.prefixes = prefixes
        cfg.symbols.suffixes = suffixes
        cfg.symbols.aliases = {str(k).upper(): [str(i).upper() for i in v] for k, v in aliases.items()}
        cfg.symbols.explicit_map = {str(k).upper(): str(v) for k, v in explicit_map.items()}
        cfg.mt5_profiles = {
            str(name): MT5Config(**{**asdict(cfg.mt5), **profile})
            for name, profile in self.mt5_profiles_ui.items()
            if isinstance(profile, dict)
        }
        cfg.routing = RoutingConfig(**routing)
        cfg.routing.dedupe_same_terminal = bool(self.var_dedupe_same_terminal.get())
        target_profiles = self._get_default_target_selection()
        cfg.routing.default_profile = target_profiles[0] if len(target_profiles) == 1 else (target_profiles or ["default"])
        cfg.routing.symbol_profiles = {normalize_symbol(str(k)): v for k, v in cfg.routing.symbol_profiles.items()}
        cfg.routing.strategy_profiles = {str(k): v for k, v in cfg.routing.strategy_profiles.items()}
        return cfg

    def _save_config(self) -> None:
        try:
            cfg = self._collect_config_from_ui()
            self.config_path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
            self.logger.info("Config saved: %s", self.config_path)
            messagebox.showinfo("Saved", f"Saved: {self.config_path}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save failed", str(exc))

    def _webhook_url(self, local: bool = False) -> str:
        host = self.var_host.get().strip() or "0.0.0.0"
        if local and host in {"0.0.0.0", "::"}:
            host = "127.0.0.1"
        elif not local and host in {"0.0.0.0", "::"}:
            host = "<YOUR_PUBLIC_HOST>"
        port = self.var_port.get().strip() or "8181"
        url = f"http://{host}:{port}/webhook"
        secret = self.var_secret.get().strip()
        if secret:
            url += "?" + urllib.parse.urlencode({"secret": secret})
        return url

    def _update_webhook_url(self) -> None:
        try:
            self.var_webhook_url.set(self._webhook_url(local=False))
        except Exception:
            pass

    def _copy_webhook_url(self) -> None:
        url = self.var_webhook_url.get().strip() or self._webhook_url(local=False)
        self.clipboard_clear()
        self.clipboard_append(url)
        self.logger.info("Webhook URL copied: %s", url)

    def _load_or_default(self) -> None:
        try:
            if self.config_path.exists():
                cfg = AppConfig.load(self.config_path)
            else:
                cfg = AppConfig()
            self._apply_config_to_ui(cfg)
            self.logger.info("Config loaded: %s", self.config_path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Load failed", str(exc))

    def _select_terminal(self) -> None:
        try:
            login = int(self.var_login.get().strip()) if self.var_login.get().strip() else None
        except ValueError:
            login = None
        selected = choose_terminal_dialog(self, login=login, password=self.var_password.get(), server=self.var_server.get())
        if selected:
            self.var_terminal.set(selected)
            self.logger.info("Terminal selected: %s", selected)

    def _test_terminal(self) -> None:
        path = self.var_terminal.get().strip()
        if not path:
            messagebox.showwarning("No terminal", "まずMT5 terminal pathを選択してください。")
            return
        try:
            login = int(self.var_login.get().strip()) if self.var_login.get().strip() else None
        except ValueError:
            messagebox.showerror("Invalid login", "Loginは数値で入力してください。")
            return
        password = self.var_password.get()
        server = self.var_server.get()

        def _probe() -> str:
            row = probe_terminal(path, login=login, password=password, server=server)
            self.logger.info("MT5 test result: %s", row.get("status", ""))
            return "\n".join(
                [
                    f"status: {row.get('status', '')}",
                    f"login: {row.get('login', '')}",
                    f"server: {row.get('server', '')}",
                    f"balance: {row.get('balance', '')}",
                    f"currency: {row.get('currency', '')}",
                    f"name: {row.get('name', '')}",
                ]
            )

        self._run_worker("MT5 Test", _probe)

    def _run_worker(self, title: str, fn: Any) -> None:
        def _worker() -> None:
            try:
                message = fn()
                self.after(0, lambda msg=message: messagebox.showinfo(title, msg))
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
                self.after(0, lambda msg=error: messagebox.showerror(title, msg))

        threading.Thread(target=_worker, name=f"tv-mt5-{title.lower().replace(' ', '-')}", daemon=True).start()

    def _send_discord_test(self) -> None:
        webhook_url = self.var_discord_webhook_url.get().strip()
        if not webhook_url:
            messagebox.showwarning("Discord Test", "Discord Webhook URLを入力してください。")
            return

        symbol = self.var_test_symbol.get().strip() or "XAUUSD"
        side = self.var_test_side.get().strip().lower() or "buy"
        config = DiscordConfig(
            enabled=True,
            webhook_url=webhook_url,
            username=self.var_discord_username.get().strip() or "半裁量アラート",
            avatar_url=self.var_discord_avatar_url.get().strip(),
        )

        def _send() -> str:
            payload = {
                "symbol": symbol,
                "action": side,
                "strategy_id": "rem_bb_pullback_15m",
                "strategy_label": "15m",
                "strategy_name": "Discord Test",
            }
            body = json.dumps(build_discord_embed_payload(config, symbol.upper(), side, payload), ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=body,
                headers=discord_request_headers(),
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as res:
                    text = res.read().decode("utf-8", errors="replace")
                    self.logger.info("Discord test response: HTTP %s %s", res.status, text)
                    return f"Discord test sent.\nHTTP {res.status}"
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", errors="replace")
                self.logger.error("Discord test failed: %s %s", exc.code, text)
                return f"Discord test failed.\nHTTP {exc.code}\n\n{text}"
            except urllib.error.URLError as exc:
                reason = str(exc.reason) if getattr(exc, "reason", None) else str(exc)
                self.logger.error("Discord test connection failed: %s", reason)
                return f"Discord test connection failed.\n\n{reason}"

        self._run_worker("Discord Test", _send)

    def _send_webhook_test(self) -> None:
        try:
            symbol = self.var_test_symbol.get().strip() or "USDJPY"
            side = self.var_test_side.get().strip().lower() or "buy"
            lot = float(self.var_test_lot.get().strip() or "0.01")
            profile = self.var_test_profile.get().strip()
            secret = self.var_secret.get().strip()
            url = self._webhook_url(local=True)
            dry_run = bool(self.var_dry_run.get())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Webhook Test", str(exc))
            return

        if not dry_run:
            ok = messagebox.askyesno(
                "Confirm Webhook Test",
                "Dry Run is OFF.\n\nSend Webhook Test can place a real MT5 market order through the running server.\n\n"
                f"Continue?\n\nsymbol={symbol}\nside={side.upper()}\nlot={lot}",
            )
            if not ok:
                return

        try:
            cfg = self._collect_config_from_ui()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Webhook Test", str(exc))
            return

        auto_started = False
        if self.service.running:
            try:
                self.service.stop()
                self.service.start(cfg)
                auto_started = True
                self.logger.info("Server restarted with current GUI settings for webhook test")
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Webhook Test", f"Could not apply current settings:\n{exc}")
                return

        if not self.service.running:
            ok = messagebox.askyesno(
                "Webhook Server Not Running",
                "Webhook server is not running.\n\nStart it with the current settings and send the test?",
            )
            if not ok:
                return
            try:
                self.service.start(cfg)
                auto_started = True
                self.logger.info("Server auto-started for webhook test")
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("Webhook Test", f"Could not start webhook server:\n{exc}")
                return

        def _send() -> str:
            if auto_started:
                time.sleep(0.2)
            payload = {
                "symbol": symbol,
                "action": side,
                "lot": lot,
            }
            if profile:
                if profile.lower() in {"all", "*"}:
                    payload["mt5_profiles"] = "all"
                else:
                    payload["mt5_profile"] = profile
            if secret:
                payload["secret"] = secret
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as res:
                    text = res.read().decode("utf-8", errors="replace")
                    self.logger.info("Webhook test response: %s", text)
                    return f"POST {url}\n\n{text}"
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", errors="replace")
                self.logger.error("Webhook test failed: %s %s", exc.code, text)
                return f"HTTP {exc.code}\nPOST {url}\n\n{text}"
            except urllib.error.URLError as exc:
                reason = str(exc.reason) if getattr(exc, "reason", None) else str(exc)
                self.logger.error("Webhook test connection failed: %s", reason)
                return (
                    f"Could not connect to local webhook server.\n\nPOST {url}\n\n"
                    "Check that the server is running and that Host/Port match the current GUI settings.\n\n"
                    f"Details: {reason}"
                )

        self._run_worker("Webhook Test", _send)

    def _send_mt5_test_order(self) -> None:
        try:
            symbol = self.var_test_symbol.get().strip() or "USDJPY"
            side = self.var_test_side.get().strip().lower() or "buy"
            lot_raw = self.var_test_lot.get().strip() or "0.01"
            profile = self.var_test_profile.get().strip()
            float(lot_raw)
            cfg = self._collect_config_from_ui()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("MT5 Test Order", str(exc))
            return
        if not self.var_dry_run.get():
            ok = messagebox.askyesno(
                "Confirm MT5 Test Order",
                f"Dry Run is OFF.\n\nSend a real MT5 {side.upper()} market order?\n\nsymbol={symbol}\nlot={lot_raw}",
            )
            if not ok:
                return

        def _send() -> str:
            payload = {}
            if profile:
                if profile.lower() in {"all", "*"}:
                    payload["mt5_profiles"] = "all"
                else:
                    payload["mt5_profile"] = profile
            target_profiles = select_mt5_profile_names(cfg, payload, symbol, symbol)
            results = []
            for target_profile in target_profiles:
                profile_name, profile_cfg = cfg.mt5_profile_config(target_profile)
                resolver = SymbolResolver(cfg.symbols, self.logger)
                lot_manager = LotManager(cfg.risk)
                trader = MT5Trader(profile_cfg, cfg.dry_run, self.logger)
                try:
                    with trader._lock:
                        if mt5 is not None:
                            trader.connect()
                        resolved_symbol, canonical = resolver.resolve(symbol)
                        lot = lot_manager.resolve_lot(canonical, float(lot_raw), profile_name)
                        result = trader.market_order(
                            symbol=resolved_symbol,
                            side=side,
                            lot=lot,
                            sl=None,
                            tp=None,
                        )
                    results.append(
                        {
                            "raw_symbol": symbol,
                            "canonical_symbol": canonical,
                            "resolved_symbol": resolved_symbol,
                            "mt5_profile": profile_name,
                            "side": side,
                            "lot": lot,
                            "result": result,
                        }
                    )
                finally:
                    trader.shutdown()
            text = json.dumps(
                {
                    "dry_run": cfg.dry_run,
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
            self.logger.info("MT5 test order result: %s", text)
            return text

        self._run_worker("MT5 Test Order", _send)

    def _start_server(self) -> None:
        try:
            cfg = self._collect_config_from_ui()
            self.service.start(cfg)
            self.logger.info("Server start requested")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Start failed", str(exc))
            self.logger.error("Start failed: %s", exc)

    def _stop_server(self) -> None:
        try:
            self.service.stop()
            self.logger.info("Server stop requested")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Stop failed", str(exc))
            self.logger.error("Stop failed: %s", exc)

    def _apply_restart(self) -> None:
        try:
            cfg = self._collect_config_from_ui()
            self.config_path.write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
            self.service.stop()
            self.service.start(cfg)
            self.logger.info("Applied config and restarted server")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Apply failed", str(exc))
            self.logger.error("Apply failed: %s", exc)

    def _refresh_status(self) -> None:
        self.var_status.set(self.service.status_text())
        self.after(500, self._refresh_status)

    def _drain_logs(self) -> None:
        lines: list[str] = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.txt_logs.configure(state="normal")
            for line in lines:
                self.txt_logs.insert(tk.END, line + "\n")
            self.txt_logs.see(tk.END)
            self.txt_logs.configure(state="disabled")
        self.after(200, self._drain_logs)

    def _on_close(self) -> None:
        self.service.stop()
        try:
            self.logger.removeHandler(self.log_handler)
        except Exception:
            pass
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description="TradingView -> MT5 bridge GUI")
    parser.add_argument("-c", "--config", default="config.json", help="Path to config.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("tv-mt5-bridge")

    app = BridgeGUI(Path(args.config).resolve(), logger)
    app.mainloop()


if __name__ == "__main__":
    main()
