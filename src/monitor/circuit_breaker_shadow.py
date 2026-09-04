"""Independent, order-free REAL_A shadow with the frozen circuit-breaker rule."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from src.logging_utils import JsonlLogger, now_iso
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.entry_engine import EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter
from src.trade_ledger import TradeLedger


class CircuitBreakerShadow(RealAContextShadow):
    """REAL_A clone. Only admission is suspended by its own realized results."""

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger, telemetry: TelemetryWriter | None) -> None:
        super().__init__(
            project_root, config, logger, telemetry,
            settings_key="circuit_breaker_shadow", strategy="REAL_A_CB_SHADOW",
            shadow_kind="REAL_A_CB_SHADOW", pair_prefix="cb", predicate=lambda _engine, _snapshot: True,
        )
        self.capital = float(self.settings.get("initial_capital_usdt", config["capital"]["operational_balance_usdt"]))
        self.equity = self.peak_equity = self.capital
        self.closed_history: list[tuple[datetime, float]] = []
        self.circuit_breaker_active = False
        self.circuit_breaker_started_at: str | None = None
        self.circuit_breaker_until: str | None = None
        self.crises_triggered = self.blocked_circuit_breaker = 0
        self.market_points: list[tuple[datetime, float]] = []
        self.cohort_started_at: str | None = None
        self.cohort_start_price: float | None = None
        if self.enabled:
            self._load_cb_state()

    def on_signal(self, signal: EntrySignal) -> bool:
        if not self.enabled or not self.accept_new_entries:
            return False
        self._release_if_due(_parse_ts(signal.ts) or datetime.now(timezone.utc), signal.price)
        if self.circuit_breaker_active:
            self.blocked_circuit_breaker += 1
            return self._block("ENTRY_BLOCKED_CIRCUIT_BREAKER", signal, _bucket(signal.source_candle_open_time), breaker_until=self.circuit_breaker_until)
        return super().on_signal(signal)

    def on_tick(self, price: float, observed_at: str) -> None:
        if not self.enabled:
            return
        moment = _parse_ts(observed_at) or datetime.now(timezone.utc)
        market_changed = self._record_market_point(moment, price)
        self._release_if_due(moment, price)
        changed = False
        for position in list(self.open_positions):
            client = position.client
            if not isinstance(client, PhantomExecutionClient):
                continue
            client.set_price(price)
            event = position.on_tick(price, market_ts=observed_at)
            if not event or position.status != "CLOSED":
                continue
            position.market_context_exit = deepcopy(self.latest_market_context)
            self.ledger.append_closed_circuit_breaker_shadow_trade(position, self.config)
            net_dollars = float(position.position_notional_usdt) * (position.pnl_pct(position.exit_price or position.entry_price) - self._fee_pct()) / 100
            self.equity += net_dollars
            self.peak_equity = max(self.peak_equity, self.equity)
            self.closed_history.append((moment, net_dollars))
            self._evaluate_after_close(moment, price)
            self.logger.system("circuit_breaker_shadow_closed", pair_id=position.pair_id, reason=position.exit_reason)
            changed = True
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        if changed or market_changed:
            self._save_state()

    def _evaluate_after_close(self, moment: datetime, price: float) -> None:
        cutoff = moment - timedelta(hours=4)
        self.closed_history = [(ts, pnl) for ts, pnl in self.closed_history if ts > cutoff]
        rolling_pct = sum(pnl for _, pnl in self.closed_history) / self.capital * 100
        drawdown_pct = (self.peak_equity - self.equity) / self.capital * 100
        true = drawdown_pct >= 1.5 and rolling_pct <= -0.5 and len(self.closed_history) >= 2
        self._event_at(moment, "CIRCUIT_BREAKER_EVALUATED", realized_equity=self.equity, realized_peak=self.peak_equity, realized_dd_pct=drawdown_pct, rolling_net_4h_pct=rolling_pct, min2_count=len(self.closed_history), detector_true=true, circuit_breaker_active=self.circuit_breaker_active, circuit_breaker_until=self.circuit_breaker_until)
        if true and not self.circuit_breaker_active:
            until = moment + timedelta(hours=6)
            self.circuit_breaker_active = True
            self.circuit_breaker_started_at, self.circuit_breaker_until = _iso(moment), _iso(until)
            self.crises_triggered += 1
            self._event_at(moment, "CIRCUIT_BREAKER_TRIGGERED", trigger_time=_iso(moment), price=price, realized_dd_pct=drawdown_pct, rolling_net_4h_pct=rolling_pct, min2_count=len(self.closed_history), open_positions=len(self.open_positions), cooldown_until=self.circuit_breaker_until, sol_return_1h_pct=self._return_ago(moment, price, 1), sol_return_4h_pct=self._return_ago(moment, price, 4), sol_return_12h_pct=self._return_ago(moment, price, 12))

    def _release_if_due(self, moment: datetime, price: float) -> None:
        until = _parse_ts(self.circuit_breaker_until)
        if not self.circuit_breaker_active or until is None or moment < until:
            return
        started = _parse_ts(self.circuit_breaker_started_at)
        self._event_at(moment, "CIRCUIT_BREAKER_RELEASED", trigger_time=self.circuit_breaker_started_at, release_time=_iso(moment), price=price, cooldown_until=self.circuit_breaker_until, sol_return_cooldown_pct=self._return_since(started, price) if started else None)
        self.circuit_breaker_active = False
        self.circuit_breaker_started_at = self.circuit_breaker_until = None
        self._save_state()

    def _record_market_point(self, moment: datetime, price: float) -> bool:
        if self.cohort_started_at is None:
            self.cohort_started_at, self.cohort_start_price = _iso(moment), price
        added = not self.market_points or (moment - self.market_points[-1][0]).total_seconds() >= 60
        if added:
            self.market_points.append((moment, price))
        cutoff = moment - timedelta(hours=13)
        self.market_points = [(ts, item) for ts, item in self.market_points if ts >= cutoff]
        return added

    def _return_ago(self, moment: datetime, price: float, hours: int) -> float | None:
        target = moment - timedelta(hours=hours)
        candidates = [(ts, value) for ts, value in self.market_points if ts <= target]
        return None if not candidates else (price / candidates[-1][1] - 1) * 100

    def _return_since(self, since: datetime, price: float) -> float | None:
        candidates = [(ts, value) for ts, value in self.market_points if ts >= since]
        return None if not candidates else (price / candidates[0][1] - 1) * 100

    def _fee_pct(self) -> float:
        fees = self.config.get("fees", {})
        if not fees.get("enabled"):
            return 0.0
        taker = float(fees.get("taker_fee_pct", 0))
        return taker * (0.75 if fees.get("use_bnb_discount") else 1.0) * 2

    def _event_at(self, moment: datetime, event: str, **fields: Any) -> None:
        payload = {"ts": _iso(moment), "strategy": self.strategy, "shadow_kind": self.shadow_kind, "event": event, **fields}
        self.logger.decision(payload)
        if self.telemetry:
            self.telemetry.submit("circuit_breaker_shadow_event", payload)

    def _event(self, event: str, **fields: Any) -> None:
        """Route inherited admission events to the CB audit stream, not context telemetry."""
        self._event_at(datetime.now(timezone.utc), event, **fields)

    def _load_cb_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return
        self.equity = float(data.get("realized_equity", self.capital)); self.peak_equity = float(data.get("realized_peak_equity", self.capital))
        self.closed_history = [(_parse_ts(item[0]), float(item[1])) for item in data.get("rolling_closed_history", []) if _parse_ts(item[0])]
        for name in ("circuit_breaker_active", "circuit_breaker_started_at", "circuit_breaker_until", "crises_triggered", "blocked_circuit_breaker", "cohort_started_at", "cohort_start_price"):
            setattr(self, name, data.get(name, getattr(self, name)))
        self.market_points = [(_parse_ts(item[0]), float(item[1])) for item in data.get("market_points", []) if _parse_ts(item[0])]

    def _save_state(self) -> None:
        if not self.enabled:
            return
        latest = max(self.entries_by_bucket, default=0)
        payload: Dict[str, Any] = {
            "updated_at": now_iso(), "entries_by_bucket": {str(k): v for k, v in self.entries_by_bucket.items() if k >= latest - 86_400_000}, "positions": [item.to_state() for item in self.open_positions],
            "realized_equity": self.equity, "realized_peak_equity": self.peak_equity, "rolling_closed_history": [[_iso(ts), pnl] for ts, pnl in self.closed_history], "circuit_breaker_active": self.circuit_breaker_active, "circuit_breaker_started_at": self.circuit_breaker_started_at, "circuit_breaker_until": self.circuit_breaker_until, "crises_triggered": self.crises_triggered, "blocked_circuit_breaker": self.blocked_circuit_breaker, "cohort_started_at": self.cohort_started_at, "cohort_start_price": self.cohort_start_price, "market_points": [[_iso(ts), price] for ts, price in self.market_points],
        }
        for name in ("blocked_context", "blocked_context_unavailable", "blocked_capacity", "blocked_same_5m", "blocked_spacing", "max_simultaneous_positions"):
            payload[name] = getattr(self, name)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp"); tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"); os.replace(tmp, self.state_path)


def _parse_ts(value: Any) -> datetime | None:
    if not value: return None
    try:
        item = datetime.fromisoformat(str(value).replace("Z", "+00:00")); return item.replace(tzinfo=item.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError: return None

def _iso(value: datetime) -> str: return value.astimezone(timezone.utc).isoformat()
def _bucket(value: int) -> int: return int(value) - int(value) % 300_000
