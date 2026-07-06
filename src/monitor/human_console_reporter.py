from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from src.monitor.cycle_manager import CycleManager
from src.monitor.entry_engine import EntryEngine
from src.monitor.position_registry import PositionRegistry
from src.console_utils import console_line


class HumanConsoleReporter:
    def __init__(
        self,
        entry_engine: EntryEngine,
        registry: PositionRegistry,
        cycle_manager: CycleManager,
        ws_status: Callable[[], str],
        uptime_seconds: Callable[[], float],
        last_tick_age_seconds: Callable[[], Optional[float]],
        last_price: Callable[[], Optional[float]],
        interval_seconds: int,
        max_market_data_age_seconds: int,
    ) -> None:
        self.entry_engine = entry_engine
        self.registry = registry
        self.cycle_manager = cycle_manager
        self.ws_status = ws_status
        self.uptime_seconds = uptime_seconds
        self.last_tick_age_seconds = last_tick_age_seconds
        self.last_price = last_price
        self.interval_seconds = max(1, int(interval_seconds))
        self.max_market_data_age_seconds = int(max_market_data_age_seconds)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        for line in self.lines():
            print(console_line(line), flush=True)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            for line in self.lines():
                print(console_line(line), flush=True)

    def lines(self) -> list[str]:
        return [self._entry_line(), self._positions_line(), self._system_line()]

    def _entry_line(self) -> str:
        cycle_total = self.cycle_manager.pairs_per_cycle
        cycle_done = cycle_total if self.cycle_manager.single_cycle_complete else self.cycle_manager.closed_pairs_in_current_cycle
        if self.cycle_manager.single_cycle_complete:
            return f"[ENTRY] PAUSED cycle_complete | cycle={cycle_done}/{cycle_total}"
        if self.registry.review_required:
            return f"[ENTRY] PAUSED needs_review | cycle={cycle_done}/{cycle_total}"
        if self.registry.capacity_full:
            return f"[ENTRY] PAUSED capacity {self.registry.open_pair_count}/{self.registry.max_open_pairs} | cycle={cycle_done}/{cycle_total}"
        diagnostic = self.entry_engine.last_diagnostic
        reason = diagnostic.get("last_reason", "waiting")
        gates = _gate_status(diagnostic.get("gates", {}))
        return f"[ENTRY] gates={gates} last={reason} | cycle={cycle_done}/{cycle_total}"

    def _positions_line(self) -> str:
        summary = self.registry.position_summary(self.last_price())
        if summary["pairs"] == 0:
            return "[POSITIONS] none"
        pnl = "n/a"
        if summary["bot_pnl_min"] is not None and summary["bot_pnl_max"] is not None:
            pnl = f"{summary['bot_pnl_min']:.2f}%..{summary['bot_pnl_max']:.2f}%"
        return (
            f"[POSITIONS] pairs={summary['pairs']} "
            f"A_open={summary['server_open']} B_open={summary['bot_open']} "
            f"B_pnl={pnl} needs_review={summary['needs_review']}"
        )

    def _system_line(self) -> str:
        age = self.last_tick_age_seconds()
        stale = age is not None and age > self.max_market_data_age_seconds
        stale_text = " market_data_stale=true" if stale else ""
        return (
            f"[SYSTEM] ws={self.ws_status()} uptime={_fmt_duration(self.uptime_seconds())} "
            f"tick_age={_fmt_age(age)} state_saved=true{stale_text}"
        )


def _gate_status(gates: dict) -> str:
    labels = [("trend", "T"), ("pullback", "P"), ("exhaustion", "E"), ("recovery", "R")]
    return "".join(f"{short}{_gate_mark((gates.get(name) or {}).get('passed'))}" for name, short in labels)


def _gate_mark(value: object) -> str:
    if value is True:
        return "+"
    if value is False:
        return "-"
    return "."


def _fmt_age(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f}s"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"
