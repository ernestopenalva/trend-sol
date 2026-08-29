"""Independent REAL_A shadows gated by a passive market-context predicate."""
from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.logging_utils import JsonlLogger, now_iso
from src.monitor.entry_engine import EntryEngine, EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter
from src.trade_ledger import TradeLedger


ContextPredicate = Callable[[EntryEngine, Optional[Dict[str, Any]]], bool | None]


class ContextGateEntryEngine(EntryEngine):
    """The normal REAL_A engine with a context authorization before Gate 1."""

    def __init__(
        self,
        symbol: str,
        config: Dict[str, Any],
        logger: JsonlLogger,
        predicate: ContextPredicate,
        blocked: Callable[[bool], None],
    ) -> None:
        super().__init__(symbol, config, logger)
        self._context_snapshot: Optional[Dict[str, Any]] = None
        self._context_predicate = predicate
        self._context_blocked = blocked

    def set_context_snapshot(self, snapshot: Optional[Dict[str, Any]]) -> None:
        self._context_snapshot = deepcopy(snapshot) if snapshot else None

    def _gate_trend(self) -> bool:
        result = self._context_predicate(self, self._context_snapshot)
        if result is not True:
            self._context_blocked(result is None)
            self._log_gate(
                0,
                False,
                False,
                "context_indicator_unavailable" if result is None else "context_blocked",
            )
            return False
        return super()._gate_trend()


class RealAContextShadow:
    """Order-free shadow whose only delta from REAL_A is a context predicate."""

    def __init__(
        self,
        project_root: Path,
        config: Dict[str, Any],
        logger: JsonlLogger,
        telemetry: Optional[TelemetryWriter],
        *,
        settings_key: str,
        strategy: str,
        shadow_kind: str,
        pair_prefix: str,
        predicate: ContextPredicate,
    ) -> None:
        self.project_root, self.config, self.logger, self.telemetry = project_root, config, logger, telemetry
        self.settings_key, self.strategy, self.shadow_kind, self.pair_prefix = settings_key, strategy, shadow_kind, pair_prefix
        settings = config.get("instrumentation", {}).get(settings_key, {})
        self.settings = settings if isinstance(settings, dict) else {}
        self.enabled = bool(self.settings.get("enabled", False))
        self.accept_new_entries = bool(self.settings.get("accept_new_entries", True))
        self.state_path = project_root / str(self.settings.get("state_file"))
        self.ledger = TradeLedger(project_root, project_root / str(self.settings.get("ledger_file")))
        self.positions: list[BotFullExitPosition] = []
        self.entries_by_bucket: dict[int, int] = {}
        self.blocked_context = 0
        self.blocked_context_unavailable = 0
        self.blocked_capacity = 0
        self.blocked_same_5m = 0
        self.blocked_spacing = 0
        self.max_simultaneous_positions = 0
        self.latest_market_context: Optional[Dict[str, Any]] = None
        self.engine = ContextGateEntryEngine(str(config["symbol"]), config, logger, predicate, self._record_context_block)
        if self.enabled:
            self._load_state()

    def required_timeframes(self) -> list[str]:
        return self.engine.required_timeframes() if self.enabled else []

    def on_kline(self, stream: str, payload: Dict[str, Any], snapshot: Optional[Dict[str, Any]]) -> None:
        if not self.enabled:
            return
        self.latest_market_context = deepcopy(snapshot) if snapshot else self.latest_market_context
        self.engine.set_context_snapshot(self.latest_market_context)
        signal = self.engine.on_kline(stream, payload)
        if signal is not None:
            self.on_signal(signal)

    def on_signal(self, signal: EntrySignal) -> bool:
        if not self.enabled or not self.accept_new_entries:
            return False
        bucket = _bucket_5m(signal.source_candle_open_time)
        if self.entries_by_bucket.get(bucket, 0) >= int(self.config.get("entry", {}).get("max_entries_per_candle", 1)):
            self.blocked_same_5m += 1
            return self._block("ENTRY_BLOCKED_SAME_5M_CANDLE", signal, bucket)
        if len(self.open_positions) >= int(self.settings.get("max_open_positions", self.config.get("capital", {}).get("max_open_positions", 5))):
            self.blocked_capacity += 1
            return self._block("ENTRY_BLOCKED_SHADOW_CAPACITY", signal, bucket)
        if signal.entry_atr is None or signal.entry_atr <= 0:
            return self._block("ENTRY_BLOCKED_ATR_UNAVAILABLE", signal, bucket)
        minimum = float(self.config.get("entry", {}).get("entry_spacing_atr", 0)) * signal.entry_atr
        if minimum > 0 and any(abs(signal.price - item.entry_price) < minimum for item in self.open_positions):
            self.blocked_spacing += 1
            return self._block("ENTRY_BLOCKED_SPACING", signal, bucket, required_distance=minimum)
        self._open(signal, bucket)
        return True

    def on_tick(self, price: float, observed_at: str) -> None:
        if not self.enabled:
            return
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
            self.ledger.append_closed_context_shadow_trade(position, self.config)
            self.logger.system(f"{self.settings_key}_closed", report_strategy=self.strategy, pair_id=position.pair_id, reason=position.exit_reason)
            changed = True
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        if changed:
            self._save_state()

    @property
    def open_positions(self) -> list[BotFullExitPosition]:
        return [item for item in self.positions if item.status == "OPEN"]

    def _record_context_block(self, unavailable: bool) -> None:
        if unavailable:
            self.blocked_context_unavailable += 1
            event = "ENTRY_BLOCKED_CONTEXT_UNAVAILABLE"
        else:
            self.blocked_context += 1
            event = "ENTRY_BLOCKED_CONTEXT"
        self._event(event)
        self._save_state()

    def _open(self, signal: EntrySignal, bucket: int) -> None:
        notional = float(self.config["capital"]["operational_balance_usdt"]) * float(self.config["capital"]["trade_size_pct"]) / 100
        client = PhantomExecutionClient()
        client.set_price(signal.price)
        pair_id = f"{self.pair_prefix}-{uuid.uuid4().hex[:12]}"
        position = BotFullExitPosition(
            pair_id=pair_id, symbol=str(self.config["symbol"]), entry_price=float(signal.price), quantity=notional / float(signal.price),
            entry_order={"shadow": True}, open_ts=now_iso(), config=self._exit_config(), client=client, logger=self.logger,
            entry_atr=signal.entry_atr, atr_timeframe=signal.atr_timeframe, atr_period=signal.atr_period,
            source_candle_open_time=signal.source_candle_open_time, position_notional_usdt=notional,
            no_progress_enabled=False, no_progress_tolerance_seconds=None, no_progress_tolerance_source="DISABLED",
        )
        position.phantom, position.phantom_id, position.shadow_kind = True, pair_id, self.shadow_kind
        position.market_context_entry = deepcopy(self.latest_market_context)
        self.positions.append(position)
        self.entries_by_bucket[bucket] = self.entries_by_bucket.get(bucket, 0) + 1
        self.max_simultaneous_positions = max(self.max_simultaneous_positions, len(self.open_positions))
        self.logger.trade(position._trade_event("OPEN", signal.price, 0.0, None, price_source="signal"))
        self._event("OPEN", pair_id=pair_id, admission_bucket_open_time=bucket)
        self._emit_ema_entry(position)
        self._save_state()

    def _block(self, event: str, signal: EntrySignal, bucket: int, **fields: Any) -> bool:
        self._event(event, price=signal.price, source_candle_open_time=signal.source_candle_open_time, admission_bucket_open_time=bucket, **fields)
        self._save_state()
        return False

    def _event(self, event: str, **fields: Any) -> None:
        payload = {"ts": now_iso(), "strategy": self.strategy, "shadow_kind": self.shadow_kind, "event": event, **fields}
        self.logger.decision(payload)
        if self.telemetry:
            self.telemetry.submit("context_shadow_event", payload)

    def _emit_ema_entry(self, position: BotFullExitPosition) -> None:
        if not self.telemetry or not self.latest_market_context:
            return
        values = _ema_entry_values(self.latest_market_context)
        self.telemetry.submit("ema_entry", {"ts": position.open_ts, "symbol": position.symbol, "strategy": self.strategy, "shadow_kind": self.shadow_kind, "trade_id": position.pair_id, "entry_price": position.entry_price, **values})

    def _exit_config(self) -> Dict[str, Any]:
        return {**self.config.get("risk", {}), "fees": self.config.get("fees", {}), "ladder": self.config.get("ladder", {})}

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.entries_by_bucket = {int(key): int(value) for key, value in (data.get("entries_by_bucket") or {}).items()}
            for name in ("blocked_context", "blocked_context_unavailable", "blocked_capacity", "blocked_same_5m", "blocked_spacing", "max_simultaneous_positions"):
                setattr(self, name, int(data.get(name, 0)))
            for item in data.get("positions", []):
                if item.get("status") == "OPEN":
                    client = PhantomExecutionClient()
                    position = BotFullExitPosition.from_state(item, self._exit_config(), client, self.logger)  # type: ignore[arg-type]
                    position.phantom, position.phantom_id, position.shadow_kind = True, position.pair_id, self.shadow_kind
                    self.positions.append(position)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.logger.system(f"{self.settings_key}_restore_failed", error=str(exc))

    def _save_state(self) -> None:
        if not self.enabled:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        latest = max(self.entries_by_bucket, default=0)
        payload = {"updated_at": now_iso(), "entries_by_bucket": {str(key): value for key, value in self.entries_by_bucket.items() if key >= latest - 86_400_000}, "positions": [item.to_state() for item in self.open_positions]}
        payload.update({name: getattr(self, name) for name in ("blocked_context", "blocked_context_unavailable", "blocked_capacity", "blocked_same_5m", "blocked_spacing", "max_simultaneous_positions")})
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)


def _ema_entry_values(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    values = snapshot.get("tf_5m") if isinstance(snapshot.get("tf_5m"), dict) else {}
    return {key: values.get(key) for key in ("ema20", "ema20_t_minus_3", "ema50", "ema50_t_minus_3", "ema100", "ema100_t_minus_3", "ema20_delta_pct", "ema50_delta_pct", "ema100_delta_pct", "ema20_rising", "ema50_rising", "ema100_rising", "ema_trend_score", "ema_trend_label")}


def _bucket_5m(timestamp_ms: int) -> int:
    return int(timestamp_ms) - int(timestamp_ms) % 300_000
