from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.logging_utils import JsonlLogger, now_iso
from src.monitor.entry_engine import EntrySignal
from src.no_progress import resolved_no_progress_tolerance
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter
from src.trade_ledger import TradeLedger


class GcrShadowRegistry:
    """Independent, order-free counterfactual for Logic B (A + GCR)."""

    def __init__(
        self,
        project_root: Path,
        config: Dict[str, Any],
        logger: JsonlLogger,
        telemetry_writer: Optional[TelemetryWriter] = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.telemetry_writer = telemetry_writer
        settings = config.get("instrumentation", {}).get("gcr_shadow", {})
        self.settings = settings if isinstance(settings, dict) else {}
        self.enabled = bool(self.settings.get("enabled", False))
        self.state_path = project_root / str(
            self.settings.get("state_file", "data/state/gcr_shadow.json")
        )
        self.ledger = TradeLedger(
            project_root,
            project_root / str(
                self.settings.get("ledger_file", "data/trades/trades_gcr_shadow.jsonl")
            ),
        )
        self.positions: list[BotFullExitPosition] = []
        self.entries_by_bucket: dict[int, int] = {}
        self.blocked_same_5m = 0
        self.blocked_gcr = 0
        self.max_simultaneous_positions = 0
        self.latest_market_context: Optional[Dict[str, Any]] = None
        if self.enabled:
            self._load_state()

    def on_signal(
        self,
        signal: EntrySignal,
        market_context: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not self.enabled:
            return False
        bucket = _bucket_5m(signal.source_candle_open_time)
        maximum = int(self.config.get("entry", {}).get("max_entries_per_candle", 1))
        if self.entries_by_bucket.get(bucket, 0) >= maximum:
            self.blocked_same_5m += 1
            self._blocked("ENTRY_BLOCKED_SAME_5M_CANDLE", signal, bucket)
            self._save_state()
            return False
        open_positions = self.open_positions
        cap = int(self.settings.get("max_open_positions", self.config.get("capital", {}).get("max_open_positions", 5)))
        if len(open_positions) >= cap:
            self._blocked("ENTRY_BLOCKED_SHADOW_CAPACITY", signal, bucket)
            return False
        latest = max(open_positions, key=lambda item: item.open_ts) if open_positions else None
        if latest is not None and latest.be_armed_at is None and latest.breakeven_stop is None:
            self.blocked_gcr += 1
            self._blocked("ENTRY_BLOCKED_GCR", signal, bucket, previous_pair_id=latest.pair_id)
            self._save_state()
            return False
        spacing_atr = float(self.config.get("entry", {}).get("entry_spacing_atr", 0))
        if spacing_atr > 0 and signal.entry_atr and signal.entry_atr > 0:
            minimum = spacing_atr * signal.entry_atr
            if any(abs(signal.price - item.entry_price) < minimum for item in open_positions):
                self._blocked("ENTRY_BLOCKED_SPACING", signal, bucket)
                return False
        self._open(signal, bucket, market_context)
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
            if event and position.status == "CLOSED":
                position.market_context_exit = deepcopy(self.latest_market_context)
                if self.telemetry_writer and position.market_context_exit:
                    self.telemetry_writer.submit(
                        "market_context",
                        {
                            "ts": position.close_ts,
                            "strategy": "GCR_SHADOW_B",
                            "shadow_kind": "GCR_SHADOW",
                            "pair_id": position.pair_id,
                            "phase": "EXIT",
                            "market_context": position.market_context_exit,
                        },
                    )
                try:
                    self.ledger.append_closed_gcr_shadow_trade(position, self.config)
                except Exception as exc:
                    self.logger.system(
                        "gcr_shadow_ledger_failed", pair_id=position.pair_id, error=str(exc)
                    )
                next_tolerance = self._current_tolerance()
                self.logger.system(
                    "gcr_shadow_closed",
                    report_strategy="GCR_SHADOW_B",
                    pair_id=position.pair_id,
                    reason=position.exit_reason,
                    next_no_progress_tolerance_seconds=next_tolerance.get("seconds"),
                    next_no_progress_tolerance_source=next_tolerance.get("source"),
                    next_no_progress_sample_size=next_tolerance.get("sample_size"),
                )
                changed = True
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        if changed:
            self._save_state()

    @property
    def open_positions(self) -> list[BotFullExitPosition]:
        return [item for item in self.positions if item.status == "OPEN"]

    def record_market_context(self, snapshot: Optional[Dict[str, Any]]) -> None:
        if not snapshot:
            return
        self.latest_market_context = deepcopy(snapshot)
        if not self.telemetry_writer:
            return
        for position in self.open_positions:
            self.telemetry_writer.submit(
                "market_context",
                {
                    "ts": snapshot.get("captured_at"),
                    "strategy": "GCR_SHADOW_B",
                    "shadow_kind": "GCR_SHADOW",
                    "pair_id": position.pair_id,
                    "phase": "DURING",
                    "market_context": snapshot,
                },
            )

    def _open(
        self,
        signal: EntrySignal,
        bucket: int,
        market_context: Optional[Dict[str, Any]],
    ) -> None:
        notional = (
            float(self.config["capital"]["operational_balance_usdt"])
            * float(self.config["capital"]["trade_size_pct"])
            / 100
        )
        client = PhantomExecutionClient()
        client.set_price(signal.price)
        tolerance = self._current_tolerance()
        pair_id = f"gcr-{uuid.uuid4().hex[:12]}"
        position = BotFullExitPosition(
            pair_id=pair_id,
            symbol=str(self.config["symbol"]),
            entry_price=float(signal.price),
            quantity=notional / float(signal.price),
            entry_order={"shadow": True},
            open_ts=now_iso(),
            config=self._exit_config(),
            client=client,  # type: ignore[arg-type]
            logger=self.logger,
            entry_atr=signal.entry_atr,
            atr_timeframe=signal.atr_timeframe,
            atr_period=signal.atr_period,
            source_candle_open_time=signal.source_candle_open_time,
            position_notional_usdt=notional,
            no_progress_enabled=bool(tolerance.get("enabled", False)),
            no_progress_tolerance_seconds=tolerance.get("seconds"),
            no_progress_tolerance_source=tolerance.get("source"),
        )
        position.phantom = True
        position.phantom_id = pair_id
        position.shadow_kind = "GCR_SHADOW"
        position.market_context_entry = deepcopy(market_context)
        self.positions.append(position)
        self.entries_by_bucket[bucket] = self.entries_by_bucket.get(bucket, 0) + 1
        self.max_simultaneous_positions = max(
            self.max_simultaneous_positions, len(self.open_positions)
        )
        self.logger.trade(position._trade_event("OPEN", signal.price, 0.0, None, price_source="signal"))
        self.logger.system(
            "gcr_shadow_opened",
            report_strategy="GCR_SHADOW_B",
            pair_id=pair_id,
            admission_bucket_open_time=bucket,
            no_progress_tolerance_seconds=position.no_progress_tolerance_seconds,
            no_progress_tolerance_source=position.no_progress_tolerance_source,
        )
        if self.telemetry_writer and market_context:
            self.telemetry_writer.submit(
                "market_context",
                {
                    "ts": market_context.get("captured_at"),
                    "strategy": "GCR_SHADOW_B",
                    "shadow_kind": "GCR_SHADOW",
                    "pair_id": pair_id,
                    "phase": "ENTRY",
                    "market_context": market_context,
                },
            )
        self._save_state()

    def _blocked(self, reason: str, signal: EntrySignal, bucket: int, **extra: Any) -> None:
        event = {
            "ts": now_iso(),
            "strategy": "GCR_SHADOW_B",
            "shadow_kind": "GCR_SHADOW",
            "event": reason,
            "price": signal.price,
            "source_candle_open_time": signal.source_candle_open_time,
            "admission_bucket_open_time": bucket,
            **extra,
        }
        self.logger.decision(event)
        if self.telemetry_writer:
            self.telemetry_writer.submit("gcr_shadow_event", event)

    def _current_tolerance(self) -> Dict[str, Any]:
        settings = self.config.get("risk", {}).get("no_progress", {})
        if not isinstance(settings, dict) or not bool(settings.get("enabled", False)):
            return {"enabled": False, "seconds": None, "source": "DISABLED"}
        result = resolved_no_progress_tolerance(self.ledger.load(), settings)
        result["enabled"] = True
        return result

    def _exit_config(self) -> Dict[str, Any]:
        result = dict(self.config.get("risk", {}))
        result["fees"] = self.config.get("fees", {})
        result["ladder"] = self.config.get("ladder", {})
        return result

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.entries_by_bucket = {
            int(key): int(value) for key, value in (data.get("entries_by_bucket") or {}).items()
        }
        self.blocked_same_5m = int(data.get("blocked_same_5m", 0))
        self.blocked_gcr = int(data.get("blocked_gcr", 0))
        self.max_simultaneous_positions = int(data.get("max_simultaneous_positions", 0))
        for item in data.get("positions", []):
            if item.get("status") != "OPEN":
                continue
            try:
                client = PhantomExecutionClient()
                position = BotFullExitPosition.from_state(
                    item, self._exit_config(), client, self.logger  # type: ignore[arg-type]
                )
                position.phantom = True
                position.phantom_id = position.pair_id
                position.shadow_kind = "GCR_SHADOW"
                self.positions.append(position)
            except Exception as exc:
                self.logger.system("gcr_shadow_restore_failed", error=str(exc))

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        latest = max(self.entries_by_bucket) if self.entries_by_bucket else 0
        cutoff = latest - 86_400_000
        payload = {
            "updated_at": now_iso(),
            "entries_by_bucket": {
                str(key): value for key, value in self.entries_by_bucket.items() if key >= cutoff
            },
            "blocked_same_5m": self.blocked_same_5m,
            "blocked_gcr": self.blocked_gcr,
            "max_simultaneous_positions": self.max_simultaneous_positions,
            "positions": [item.to_state() for item in self.open_positions],
        }
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)


def _bucket_5m(timestamp_ms: int) -> int:
    value = int(timestamp_ms)
    return value - value % 300_000
