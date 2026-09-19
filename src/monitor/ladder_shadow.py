"""Order-free forward controls for isolated REAL_A ladder variants."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict

from src.logging_utils import JsonlLogger, now_iso
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.entry_engine import EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter


class LadderShadowPosition(BotFullExitPosition):
    """A phantom whose PL economic floor remains identical to REAL_A."""

    def _active_profit_lock_economic_floor(self) -> float | None:
        kind = self.shadow_kind
        try:
            self.shadow_kind = None
            return super()._active_profit_lock_economic_floor()
        finally:
            self.shadow_kind = kind


class RealALadderShadow(RealAContextShadow):
    """Shared approved signal, but fully independent admission and ladder."""

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger,
                 telemetry: TelemetryWriter | None, *, settings_key: str, strategy: str,
                 shadow_kind: str, pair_prefix: str, variant: str, cohort_started_at: str) -> None:
        self.variant = variant
        self.cohort_started_at = cohort_started_at
        settings = config.get("instrumentation", {}).get(settings_key, {})
        state_path = project_root / str((settings or {}).get("state_file"))
        ledger_path = project_root / str((settings or {}).get("ledger_file"))
        if bool((settings or {}).get("enabled", False)) and not state_path.exists() and ledger_path.exists() and ledger_path.stat().st_size:
            raise ValueError(f"{settings_key} ledger exists without its cohort state; archive before starting a new cohort")
        super().__init__(project_root, config, logger, telemetry, settings_key=settings_key,
                         strategy=strategy, shadow_kind=shadow_kind, pair_prefix=pair_prefix,
                         predicate=lambda _engine, _snapshot: True)
        self._restore_cohort_marker()

    def announce_cohort(self) -> None:
        self._event("COHORT_STARTED", cohort_started_at=self.cohort_started_at, variant=self.variant)
        self._save_state()

    def on_approved_real_a_signal(self, signal: EntrySignal, market_context: Dict[str, Any] | None) -> bool:
        self.latest_market_context = deepcopy(market_context) if market_context else self.latest_market_context
        return self.on_signal(signal)

    def on_kline(self, stream: str, payload: Dict[str, Any], snapshot: Dict[str, Any] | None) -> None:
        """No second EntryEngine: this shadow only receives the shared opportunity."""
        return None

    def _exit_config(self) -> Dict[str, Any]:
        value = deepcopy(super()._exit_config())
        if self.variant == "BE030":
            value.setdefault("ladder", {})["be_net_margin_pct"] = 0.10
        elif self.variant == "BE_OFF":
            value["breakeven"] = {"mode": "off"}
        else:
            raise ValueError(f"Unknown ladder shadow variant: {self.variant}")
        return value

    def _open(self, signal: EntrySignal, bucket: int) -> None:
        notional = float(self.config["capital"]["operational_balance_usdt"]) * float(self.config["capital"]["trade_size_pct"]) / 100
        client = PhantomExecutionClient(); client.set_price(signal.price)
        pair_id = f"{self.pair_prefix}-{signal.source_candle_open_time}"
        position = LadderShadowPosition(
            pair_id=pair_id, symbol=str(self.config["symbol"]), entry_price=float(signal.price),
            quantity=notional / float(signal.price), entry_order={"shadow": True}, open_ts=signal.ts,
            config=self._exit_config(), client=client, logger=self.logger, entry_atr=signal.entry_atr,
            atr_timeframe=signal.atr_timeframe, atr_period=signal.atr_period,
            source_candle_open_time=signal.source_candle_open_time, position_notional_usdt=notional,
            no_progress_enabled=False, no_progress_tolerance_seconds=None, no_progress_tolerance_source="DISABLED",
        )
        position.phantom, position.phantom_id, position.shadow_kind = True, pair_id, self.shadow_kind
        position.market_context_entry = deepcopy(self.latest_market_context)
        self.positions.append(position); self.entries_by_bucket[bucket] = self.entries_by_bucket.get(bucket, 0) + 1
        self.max_simultaneous_positions = max(self.max_simultaneous_positions, len(self.open_positions))
        self.logger.trade(position._trade_event("OPEN", signal.price, 0.0, None, price_source="signal"))
        self._event("OPEN", pair_id=pair_id, source_candle_open_time=signal.source_candle_open_time,
                    admission_bucket_open_time=bucket, variant=self.variant)
        self._emit_ema_entry(position); self._save_state()

    def on_tick(self, price: float, observed_at: str) -> None:
        if not self.enabled: return
        changed = False
        for position in list(self.open_positions):
            client = position.client
            if not isinstance(client, PhantomExecutionClient): continue
            client.set_price(price); event = position.on_tick(price, market_ts=observed_at)
            if not event or position.status != "CLOSED": continue
            position.market_context_exit = deepcopy(self.latest_market_context)
            self.ledger.append_closed_ladder_shadow_trade(position, self.config)
            self._event("CLOSE", pair_id=position.pair_id, reason=position.exit_reason, variant=self.variant)
            changed = True
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        if changed: self._save_state()

    def _event(self, event: str, **fields: Any) -> None:
        payload = {"ts": now_iso(), "strategy": self.strategy, "shadow_kind": self.shadow_kind,
                   "event": event, **fields}
        self.logger.decision(payload)
        if self.telemetry: self.telemetry.submit("ladder_shadow_event", payload)

    def _restore_cohort_marker(self) -> None:
        if not self.state_path.exists(): return
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8")).get("cohort_started_at")
            if value: self.cohort_started_at = str(value)
        except (OSError, ValueError, json.JSONDecodeError): pass

    def _load_state(self) -> None:
        super()._load_state()
        self.positions = [
            LadderShadowPosition.from_state(item.to_state(), self._exit_config(), item.client, self.logger)
            for item in self.positions
        ]

    def _save_state(self) -> None:
        super()._save_state()
        if not self.enabled or not self.state_path.exists(): return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8")); data["cohort_started_at"] = self.cohort_started_at; data["variant"] = self.variant
            temp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.state_path)
        except (OSError, ValueError, json.JSONDecodeError):
            raise
