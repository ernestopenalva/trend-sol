"""Isolated BE_OFF + CB forward experiments. Never submits Binance orders."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from src.logging_utils import JsonlLogger
from src.monitor.circuit_breaker_shadow import CircuitBreakerPosition, CircuitBreakerShadow, _bucket, _iso, _parse_ts
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter


class PolicyShadow(CircuitBreakerShadow):
    """BE_OFF + frozen CB with an isolated context admission policy."""

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger,
                 telemetry: TelemetryWriter | None, *, settings_key: str, strategy: str,
                 pair_prefix: str, policy: str, cohort_started_at: str) -> None:
        self.policy = policy
        super().__init__(project_root, config, logger, telemetry, settings_key=settings_key,
                         strategy=strategy, shadow_kind=strategy, pair_prefix=pair_prefix,
                         be_off=True, cohort_started_at=cohort_started_at)

    def _entry_policy(self, context: Dict[str, Any]) -> tuple[bool, str]:
        ema_context = context.get("ema_context")
        macd_context = context.get("macd_context")
        if self.policy == "MACD_BU_MINUS":
            if macd_context in (None, "UNAVAILABLE"):
                return False, "ENTRY_BLOCKED_MACD_UNAVAILABLE"
            return (False, "ENTRY_BLOCKED_MACD_BU_MINUS") if macd_context == "BU-" else (True, "ENTRY_ACCEPTED_MACD")
        if ema_context in (None, "UNAVAILABLE") or macd_context in (None, "UNAVAILABLE"):
            return False, "ENTRY_BLOCKED_CONTEXT_UNAVAILABLE"
        allowed = ema_context in ("LON", "BUL", "BEA") and macd_context in ("BU+", "BE+")
        return (True, "ENTRY_ACCEPTED_EMA_MACD") if allowed else (False, f"ENTRY_BLOCKED_EMA_MACD_{ema_context}_{macd_context}")


class ElasticPosition(CircuitBreakerPosition):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hs_elastic = False
        self.hs_elastic_started_at = None
        self.hs_original_at = None
        self.hs_original_pnl_pct = None
        self.hs_original_context = None
        self.hs_elastic_worst_pnl_pct = None
        self.hs_elastic_returned_entry_at = None
        self.hs_elastic_returned_economic_be_at = None
        self.hs_elastic_extra_seconds = 0.0
        self._elastic_hard_stop_price = self.hard_stop_price

    def to_state(self):
        value = super().to_state()
        for name in (
            "hs_elastic", "hs_elastic_started_at", "hs_original_at", "hs_original_pnl_pct",
            "hs_original_context", "hs_elastic_worst_pnl_pct", "hs_elastic_returned_entry_at",
            "hs_elastic_returned_economic_be_at", "hs_elastic_extra_seconds", "_elastic_hard_stop_price",
        ):
            value[name] = getattr(self, name)
        return value

    @classmethod
    def from_state(cls, state, config, client, logger):
        value = super().from_state(state, config, client, logger)
        for name in (
            "hs_elastic", "hs_elastic_started_at", "hs_original_at", "hs_original_pnl_pct",
            "hs_original_context", "hs_elastic_worst_pnl_pct", "hs_elastic_returned_entry_at",
            "hs_elastic_returned_economic_be_at", "hs_elastic_extra_seconds", "_elastic_hard_stop_price",
        ):
            if name in state:
                setattr(value, name, state[name])
        return value


class ExperimentalRiskShadow(CircuitBreakerShadow):
    """One of the three mutually exclusive exit-risk hypotheses."""

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger,
                 telemetry: TelemetryWriter | None, *, settings_key: str, strategy: str,
                 pair_prefix: str, experiment: str, cohort_started_at: str) -> None:
        self.experiment = experiment
        super().__init__(project_root, config, logger, telemetry, settings_key=settings_key,
                         strategy=strategy, shadow_kind=strategy, pair_prefix=pair_prefix,
                         be_off=True, cohort_started_at=cohort_started_at)

    def _position_type(self):
        return ElasticPosition if self.experiment == "HS_BULL_ELASTIC" else CircuitBreakerPosition

    def on_closed_5m(self, snapshot: Dict[str, Any] | None) -> None:
        if not self.enabled or not snapshot:
            return
        self.latest_market_context = deepcopy(snapshot)
        if self.experiment != "HS_BULL_ELASTIC":
            self._save_state()
            return
        context = self._context_fields()
        close_price = (snapshot.get("tf_5m") or {}).get("close")
        observed_at = (snapshot.get("tf_5m") or {}).get("latest_closed_at_ms")
        if close_price is None:
            return
        stamp = datetime.fromtimestamp(float(observed_at) / 1000, timezone.utc).isoformat() if observed_at else None
        for position in list(self.open_positions):
            if not isinstance(position, ElasticPosition) or not position.hs_elastic or context.get("ema_context") == "LON":
                continue
            original = float(position._elastic_hard_stop_price)
            if float(close_price) <= original:
                self._finish_elastic_clock(position, stamp)
                self._event("HS_ELASTIC_EXIT_CONTEXT_LOST", pair_id=position.pair_id, price=close_price, **context)
                self._close_and_record(position, float(close_price), stamp, "HARD_STOP_ELASTIC_CONTEXT_LOST", original)
            else:
                self._finish_elastic_clock(position, stamp)
                position.hs_elastic = False
                position.hard_stop_price = original
                position._refresh_effective_stop()
                self._event("HS_ELASTIC_ENDED_RECOVERED", pair_id=position.pair_id, price=close_price, **context)
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        self._save_state()

    def _process_tick(self, price: float, observed_at: str) -> None:
        prior_breaker = self.circuit_breaker_active
        prior = list(self.open_positions)
        context = self._context_fields()
        if self.experiment == "HS_BULL_ELASTIC":
            for position in prior:
                if not isinstance(position, ElasticPosition):
                    continue
                original = float(position._elastic_hard_stop_price)
                pnl = position.pnl_pct(price)
                if not position.hs_elastic and price <= original and context.get("ema_context") == "LON":
                    position.hs_elastic = True
                    position.hs_original_at = observed_at
                    position.hs_elastic_started_at = observed_at
                    position.hs_original_pnl_pct = pnl
                    position.hs_original_context = deepcopy(context)
                    position.hs_elastic_worst_pnl_pct = pnl
                    position.hard_stop_price = None
                    position.effective_stop = position.review_stop
                    position.stop_type = "review"
                    position._refresh_effective_stop()
                    self._event("HS_ELASTIC_STARTED", pair_id=position.pair_id, price=price, pnl_pct=pnl, **context)
                if position.hs_elastic:
                    position.hs_elastic_worst_pnl_pct = min(float(position.hs_elastic_worst_pnl_pct or pnl), pnl)
                    if price >= position.entry_price and not position.hs_elastic_returned_entry_at:
                        position.hs_elastic_returned_entry_at = observed_at
                    economic_be = position.entry_price * (1 + self._fee_pct() / 100)
                    if price >= economic_be and not position.hs_elastic_returned_economic_be_at:
                        position.hs_elastic_returned_economic_be_at = observed_at
        super()._process_tick(price, observed_at)
        for position in prior:
            if isinstance(position, ElasticPosition) and position.status == "CLOSED" and position.hs_elastic_started_at:
                self._finish_elastic_clock(position, position.close_ts or observed_at)
                for record in self.closed_records:
                    if record.get("pair_id") == position.pair_id:
                        record.update({
                            "hs_elastic": True,
                            "hs_original_at": position.hs_original_at,
                            "hs_original_pnl_pct": position.hs_original_pnl_pct,
                            "hs_original_context": position.hs_original_context,
                            "hs_elastic_started_at": position.hs_elastic_started_at,
                            "hs_elastic_worst_pnl_pct": position.hs_elastic_worst_pnl_pct,
                            "hs_elastic_returned_entry_at": position.hs_elastic_returned_entry_at,
                            "hs_elastic_returned_economic_be_at": position.hs_elastic_returned_economic_be_at,
                            "hs_elastic_extra_seconds": position.hs_elastic_extra_seconds,
                            "control_pair_id": f"beoffcb-{position.source_candle_open_time}",
                            "control_exit_pending": True,
                        })
        hard_stops = [item for item in prior if item.status == "CLOSED" and item.exit_reason == "HARD_STOP"]
        if self.experiment == "HS_BEAR_CLUSTER_EXIT" and hard_stops and context.get("ema_context") == "SHO":
            victims = [item for item in self.open_positions if item.pnl_pct(price) < 0]
            self._event("HS_BEAR_CLUSTER_TRIGGERED", trigger_pair_ids=[item.pair_id for item in hard_stops],
                        victim_pair_ids=[item.pair_id for item in victims], price=price, **context)
            for position in victims:
                self._close_and_record(position, price, observed_at, "HS_BEAR_CLUSTER_EXIT", price)
            self.positions = [item for item in self.positions if item.status == "OPEN"]
        if self.experiment == "CB_EXIT_ALL" and not prior_breaker and self.circuit_breaker_active:
            victims = list(self.open_positions)
            self._event("CB_EXIT_ALL_TRIGGERED", victim_pair_ids=[item.pair_id for item in victims], price=price, **context)
            for position in victims:
                self._close_and_record(position, price, observed_at, "CIRCUIT_BREAKER_EXIT_ALL", price)
            self.positions = [item for item in self.positions if item.status == "OPEN"]
        self._save_state()

    @staticmethod
    def _finish_elastic_clock(position: ElasticPosition, ended_at: str | None) -> None:
        start = _parse_ts(position.hs_elastic_started_at)
        end = _parse_ts(ended_at)
        if start and end:
            position.hs_elastic_extra_seconds = max(0.0, (end - start).total_seconds())

    def _process_signal(self, signal):
        prior = self.circuit_breaker_active
        result = super()._process_signal(signal)
        if self.experiment == "CB_EXIT_ALL" and not prior and self.circuit_breaker_active:
            for position in list(self.open_positions):
                self._close_and_record(position, signal.price, signal.ts, "CIRCUIT_BREAKER_EXIT_ALL", signal.price)
            self.positions = [item for item in self.positions if item.status == "OPEN"]
            self._save_state()
        return result

    def _close_and_record(self, position, price: float, observed_at: str | None, reason: str, reference: float) -> None:
        if position.status != "OPEN":
            return
        position.market_context_exit = deepcopy(self.latest_market_context)
        position._cb_market_ts = observed_at
        event = position._close_at_market(price, reason, observed_at or _iso(datetime.now(timezone.utc)), reference)
        if not event or position.status != "CLOSED":
            return
        record = self.ledger._record(position, self.config, "CIRCUIT_BREAKER_SHADOW")
        record["control_pair_id"] = f"beoffcb-{position.source_candle_open_time}"
        record["control_exit_pending"] = True
        self.closed_records.append(record)
        moment = _parse_ts(position.close_ts) or datetime.now(timezone.utc)
        stamp = int(moment.timestamp() * 1000)
        boundary = stamp - stamp % 60_000 + 60_000
        net = float(record["net_pnl_pct"]) * float(position.position_notional_usdt) / 100
        self.pending_closes.append({"boundary": boundary, "net": net, "pair_id": position.pair_id})
        self._event("EXPERIMENTAL_CLOSE", pair_id=position.pair_id, reason=reason,
                    net_pnl_pct=record.get("net_pnl_pct"), control_exit_pending=True,
                    control_pair_id=f"beoffcb-{position.source_candle_open_time}",
                    source_candle_open_time=position.source_candle_open_time)
