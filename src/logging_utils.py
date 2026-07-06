from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.console_utils import console_line


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JsonlLogger:
    def __init__(self, project_root: Path, config: Dict[str, Any]) -> None:
        logging_cfg = config.get("logging", {})
        console_cfg = config.get("console", {})
        self.console = bool(logging_cfg.get("console", True))
        self.console_mode = str(console_cfg.get("mode", "raw")).lower()
        self.raw_console = self.console and self.console_mode == "raw"
        self.trade_log = project_root / logging_cfg.get("trade_log", "logs/trades.jsonl")
        self.decision_log = project_root / logging_cfg.get("decision_log", "logs/decisions.jsonl")
        self.system_log = project_root / logging_cfg.get("system_log", "logs/system.log")
        self._entry_context: Optional[Callable[[], Dict[str, Any]]] = None

    def set_entry_console_context(self, provider: Callable[[], Dict[str, Any]]) -> None:
        self._entry_context = provider

    def decision(self, event: Dict[str, Any]) -> None:
        self._safe_write(self._append_jsonl, self.decision_log, event)
        if self.raw_console:
            self._safe_console(self._print_decision, event)

    def trade(self, event: Dict[str, Any]) -> None:
        self._safe_write(self._append_jsonl, self.trade_log, event)
        if self.raw_console:
            self._safe_console(self._print_trade, event)

    def system(self, event: str, **fields: Any) -> None:
        payload = {"ts": now_iso(), "event": event, **fields}
        try:
            self.system_log.parent.mkdir(parents=True, exist_ok=True)
            with self.system_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception as exc:
            self._safe_console(print, console_line(f"[LOGGER_ERROR] system_log_write_failed error={exc}"), flush=True)
        if self.raw_console:
            self._safe_console(print, _format_system(event, fields), flush=True)

    @staticmethod
    def _safe_write(writer: Callable[..., None], *args: Any) -> None:
        try:
            writer(*args)
        except Exception as exc:
            print(console_line(f"[LOGGER_ERROR] jsonl_write_failed error={exc}"), flush=True)

    @staticmethod
    def _safe_console(func: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        try:
            func(*args, **kwargs)
        except Exception as exc:
            print(f"[LOGGER_ERROR] console_write_failed error={exc}", flush=True)

    @staticmethod
    def _append_jsonl(path: Path, event: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _print_decision(self, event: Dict[str, Any]) -> None:
        gate = event.get("gate")
        passed = "OK" if event.get("passed") else "BARRADO"
        near = " near-miss" if event.get("near_miss") else ""
        reason = event.get("reason", "")
        context = self._entry_context() if self._entry_context else {}
        details = _compact_details(
            {**context, **event},
            skip={"ts", "gate", "passed", "near_miss", "reason"},
            limit=8,
        )
        suffix = f" | {details}" if details else ""
        print(console_line(f"[ENTRY] gate={gate} {passed}{near} reason={reason}{suffix}"), flush=True)

    @staticmethod
    def _print_trade(event: Dict[str, Any]) -> None:
        pair_id = event.get("pair_id", "")
        position = event.get("position", "")
        event_name = event.get("event", "")
        engine = event.get("engine", "")
        price = _fmt_number(event.get("price"))
        pnl = _fmt_number(event.get("pnl_pct"))
        reason = event.get("exit_reason")
        reason_text = f" reason={reason}" if reason else ""
        print(
            console_line(
                f"[TRADE] pair={pair_id} pos={position} {event_name} {engine} "
                f"price={price} pnl={pnl}%{reason_text}"
            ),
            flush=True,
        )


def _format_system(message: str, fields: Dict[str, Any]) -> str:
    details = _compact_details(fields, skip=set(), limit=5)
    suffix = f" | {details}" if details else ""
    return console_line(f"[SYSTEM] {message}{suffix}")


def _compact_details(fields: Dict[str, Any], skip: set[str], limit: int) -> str:
    parts = []
    for key, value in fields.items():
        if key in skip or value is None:
            continue
        parts.append(f"{key}={_fmt_value(value)}")
        if len(parts) >= limit:
            break
    return " ".join(parts)


def _fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return _fmt_number(value)
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def _fmt_number(value: Any) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "n/a"
