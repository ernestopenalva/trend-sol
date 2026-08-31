"""Order-free H2 shadow: REAL_A timing with variable, capped exposure."""
from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.exchange.binance_client import SymbolFilters
from src.logging_utils import JsonlLogger, now_iso
from src.monitor.entry_engine import EntrySignal
from src.no_progress import resolved_no_progress_tolerance
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter
from src.trade_ledger import TradeLedger


class H2ExposureShadow:
    """Independent phantom arm that changes only notional sizing.

    It deliberately consumes the already-approved REAL_A signal rather than
    duplicating gates.  It never calls a buy endpoint.
    """

    def __init__(
        self,
        project_root: Path,
        config: Dict[str, Any],
        logger: JsonlLogger,
        telemetry: Optional[TelemetryWriter],
        filters_provider: Callable[[str], SymbolFilters],
    ) -> None:
        self.project_root, self.config, self.logger, self.telemetry = project_root, config, logger, telemetry
        settings = config.get("instrumentation", {}).get("h2_exposure_shadow", {})
        self.settings = settings if isinstance(settings, dict) else {}
        self.enabled = bool(self.settings.get("enabled", False))
        self.accept_new_entries = bool(self.settings.get("accept_new_entries", True))
        self.sizing_version = str(self.settings.get("sizing_version", "v2_reducer"))
        self.state_path = project_root / str(self.settings.get("state_file", "data/state/h2_exposure_shadow.json"))
        self.ledger = TradeLedger(project_root, project_root / str(self.settings.get("ledger_file", "data/trades/trades_h2_exposure_shadow.jsonl")))
        self.filters_provider = filters_provider
        self.positions: list[BotFullExitPosition] = []
        self.entries_by_bucket: dict[int, int] = {}
        self.entry_metadata: dict[str, dict[str, Any]] = {}
        self.blocked_capacity = 0
        self.blocked_same_5m = 0
        self.blocked_spacing = 0
        self.blocked_min_notional_h2 = 0
        self.blocked_symbol_filters_unavailable = 0
        self.max_simultaneous_positions = 0
        if self.enabled:
            self._load_state()

    @property
    def capital_max_usdt(self) -> float:
        configured = self.settings.get("capital_max_usdt")
        if configured is None:
            configured = self.config.get("capital", {}).get("operational_balance_usdt", 0)
        return float(configured)

    @property
    def base_notional_usdt(self) -> float:
        capital = self.config.get("capital", {})
        return float(capital.get("operational_balance_usdt", 0)) * float(capital.get("trade_size_pct", 0)) / 100

    @property
    def open_positions(self) -> list[BotFullExitPosition]:
        return [item for item in self.positions if item.status == "OPEN"]

    def on_signal(self, signal: EntrySignal) -> bool:
        """Apply only REAL_A's existing admission checks plus H2 sizing."""
        if not self.enabled or not self.accept_new_entries:
            return False
        bucket = _bucket_5m(signal.source_candle_open_time)
        if self.entries_by_bucket.get(bucket, 0) >= int(self.config.get("entry", {}).get("max_entries_per_candle", 1)):
            self.blocked_same_5m += 1
            return self._block("ENTRY_BLOCKED_SAME_5M_CANDLE", signal, bucket)
        if len(self.open_positions) >= int(self.settings.get("max_open_positions", self.config.get("capital", {}).get("max_open_positions", 5))):
            self.blocked_capacity += 1
            return self._block("ENTRY_BLOCKED_H2_CAPACITY", signal, bucket)
        if signal.entry_atr is None or signal.entry_atr <= 0:
            return self._block("ENTRY_BLOCKED_ATR_UNAVAILABLE", signal, bucket)
        minimum_distance = float(self.config.get("entry", {}).get("entry_spacing_atr", 0)) * float(signal.entry_atr)
        if minimum_distance > 0 and any(abs(float(signal.price) - item.entry_price) < minimum_distance for item in self.open_positions):
            self.blocked_spacing += 1
            return self._block("ENTRY_BLOCKED_SPACING", signal, bucket, required_distance=minimum_distance)
        try:
            filters = self.filters_provider(str(self.config["symbol"]))
            sizing = self._sizing_for(signal.price, filters)
        except Exception as exc:
            self.blocked_symbol_filters_unavailable += 1
            return self._block("ENTRY_BLOCKED_SYMBOL_FILTERS_UNAVAILABLE", signal, bucket, error=str(exc))
        if sizing is None:
            self.blocked_min_notional_h2 += 1
            return self._block("ENTRY_BLOCKED_MIN_NOTIONAL_H2", signal, bucket)
        self._open(signal, bucket, sizing)
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
            metadata = self.entry_metadata.get(position.pair_id, {})
            self.ledger.append_closed_h2_exposure_shadow_trade(position, self.config, metadata)
            self._event("CLOSE", pair_id=position.pair_id, exit_reason=position.exit_reason, **metadata)
            self.entry_metadata.pop(position.pair_id, None)
            changed = True
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        if changed:
            self._save_state()

    def _sizing_for(self, price: float, filters: SymbolFilters) -> Optional[dict[str, Any]]:
        capital = Decimal(str(self.capital_max_usdt))
        price_d = Decimal(str(price))
        if capital <= 0 or price_d <= 0 or filters.step_size <= 0:
            return None
        minimum_qty = _ceil_to_step(max(filters.min_qty, filters.min_notional / price_d), filters.step_size)
        minimum_notional = minimum_qty * price_d
        base = Decimal(str(self.base_notional_usdt))
        if minimum_notional > capital or base <= 0:
            return None
        uncovered = self._uncovered_positions()
        step_index = min(len(uncovered), 4) + 1
        candidate = base / Decimal(step_index)
        committed = sum(Decimal(str(item.position_notional_usdt or 0)) for item in self.open_positions)
        available = max(Decimal(0), capital - committed)
        effective = min(candidate, available, base)
        quantity = _floor_to_step(effective / price_d, filters.step_size)
        actual_notional = quantity * price_d
        if quantity < minimum_qty or actual_notional > available or actual_notional <= 0:
            return None
        return {
            "uncovered_count": len(uncovered),
            "h2_step": step_index,
            "sizing_version": self.sizing_version,
            "base_notional": float(base),
            "candidate_notional": float(candidate),
            "available_capital": float(available),
            "effective_notional": float(actual_notional),
            "quantity": float(quantity),
            "minimum_operational_notional": float(minimum_notional),
            "filters": {"min_qty": str(filters.min_qty), "step_size": str(filters.step_size), "min_notional": str(filters.min_notional)},
        }

    def _uncovered_positions(self) -> list[BotFullExitPosition]:
        return [item for item in self.open_positions if item.current_step() == "NONE"]

    def _open(self, signal: EntrySignal, bucket: int, sizing: dict[str, Any]) -> None:
        client = PhantomExecutionClient()
        client.set_price(signal.price)
        pair_id = f"h2-{uuid.uuid4().hex[:12]}"
        no_progress = self._no_progress()
        position = BotFullExitPosition(
            pair_id=pair_id, symbol=str(self.config["symbol"]), entry_price=float(signal.price), quantity=float(sizing["quantity"]),
            entry_order={"shadow": True, "h2": True}, open_ts=now_iso(), config=self._exit_config(), client=client, logger=self.logger,
            entry_atr=signal.entry_atr, atr_timeframe=signal.atr_timeframe, atr_period=signal.atr_period,
            source_candle_open_time=signal.source_candle_open_time, position_notional_usdt=float(sizing["effective_notional"]),
            no_progress_enabled=bool(no_progress.get("enabled", False)),
            no_progress_tolerance_seconds=no_progress.get("seconds"),
            no_progress_tolerance_source=no_progress.get("source"),
        )
        # Do not set position.shadow_kind: BotFullExitPosition uses that field
        # to disable REAL_A's economic profit-lock floor.
        position.phantom, position.phantom_id = True, pair_id
        prior = [self._protection_state(item) for item in self.open_positions]
        metadata = {
            **sizing,
            "opened_positions_before": len(self.open_positions),
            "protected_positions_before": prior,
            "capital_committed_after": float(sum((item.position_notional_usdt or 0) for item in self.open_positions) + position.position_notional_usdt),
            "initial_stop": position.effective_stop,
        }
        self.positions.append(position)
        self.entry_metadata[pair_id] = metadata
        self.entries_by_bucket[bucket] = self.entries_by_bucket.get(bucket, 0) + 1
        self.max_simultaneous_positions = max(self.max_simultaneous_positions, len(self.open_positions))
        self.logger.trade(position._trade_event("OPEN", signal.price, 0.0, None, price_source="signal"))
        self._event("OPEN", pair_id=pair_id, admission_bucket_open_time=bucket, entry_price=signal.price, **metadata)
        self._save_state()

    def announce_sizing_version(self) -> None:
        """Persist an auditable boundary without touching restored positions."""
        if self.enabled:
            self._event(
                "H2_SIZING_VERSION_ACTIVATED",
                sizing_version=self.sizing_version,
                base_notional=self.base_notional_usdt,
                restored_open_positions=len(self.open_positions),
            )

    def _protection_state(self, position: BotFullExitPosition) -> dict[str, Any]:
        step = position.current_step()
        return {"pair_id": position.pair_id, "step": step, "uncovered": step == "NONE", "effective_stop": position.effective_stop}

    def _no_progress(self) -> dict[str, Any]:
        settings = self.config.get("risk", {}).get("no_progress", {})
        if not isinstance(settings, dict) or not bool(settings.get("enabled", False)):
            return {"enabled": False, "seconds": None, "source": "DISABLED"}
        records = TradeLedger(self.project_root).load()
        closed = [item for item in records if not bool(item.get("phantom", False)) and str(item.get("position_type")) == "BOT_EXIT"]
        result = resolved_no_progress_tolerance(closed, settings)
        return {"enabled": True, "seconds": result.get("seconds"), "source": result.get("source")}

    def _exit_config(self) -> Dict[str, Any]:
        return {**self.config.get("risk", {}), "fees": self.config.get("fees", {}), "ladder": self.config.get("ladder", {})}

    def _block(self, event: str, signal: EntrySignal, bucket: int, **fields: Any) -> bool:
        self._event(event, price=signal.price, source_candle_open_time=signal.source_candle_open_time, admission_bucket_open_time=bucket, **fields)
        self._save_state()
        return False

    def _event(self, event: str, **fields: Any) -> None:
        payload = {"ts": now_iso(), "strategy": "H2_EXPOSURE_SHADOW", "shadow_kind": "H2_EXPOSURE_SHADOW", "event": event, **fields}
        self.logger.decision(payload)
        if self.telemetry:
            self.telemetry.submit("h2_exposure_shadow_event", payload)

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.entries_by_bucket = {int(key): int(value) for key, value in (data.get("entries_by_bucket") or {}).items()}
            self.entry_metadata = {str(key): value for key, value in (data.get("entry_metadata") or {}).items() if isinstance(value, dict)}
            for name in ("blocked_capacity", "blocked_same_5m", "blocked_spacing", "blocked_symbol_filters_unavailable", "max_simultaneous_positions"):
                setattr(self, name, int(data.get(name, 0)))
            self.blocked_min_notional_h2 = int(
                data.get("blocked_min_notional_h2", data.get("blocked_min_notional_or_capital", 0))
            )
            for item in data.get("positions", []):
                if item.get("status") == "OPEN":
                    position = BotFullExitPosition.from_state(item, self._exit_config(), PhantomExecutionClient(), self.logger)  # type: ignore[arg-type]
                    position.phantom, position.phantom_id = True, position.pair_id
                    self.positions.append(position)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.logger.system("h2_exposure_shadow_restore_failed", error=str(exc))

    def _save_state(self) -> None:
        if not self.enabled:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        latest = max(self.entries_by_bucket, default=0)
        payload = {
            "updated_at": now_iso(),
            "entries_by_bucket": {str(key): value for key, value in self.entries_by_bucket.items() if key >= latest - 86_400_000},
            "positions": [item.to_state() for item in self.open_positions],
            "entry_metadata": self.entry_metadata,
        }
        payload.update({name: getattr(self, name) for name in ("blocked_capacity", "blocked_same_5m", "blocked_spacing", "blocked_min_notional_h2", "blocked_symbol_filters_unavailable", "max_simultaneous_positions")})
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)


def _bucket_5m(timestamp_ms: int) -> int:
    return int(timestamp_ms) - int(timestamp_ms) % 300_000


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step
