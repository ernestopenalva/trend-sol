from __future__ import annotations

import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from src.logging_utils import JsonlLogger, now_iso
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter
from src.trade_ledger import TradeLedger


class Dmi15ShadowRegistry:
    """Order-free DMI15 entry shadow with isolated positions and persistence."""

    STRATEGY = "DMI15_SHADOW_C"
    SHADOW_KIND = "DMI15_SHADOW"

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
        settings = config.get("instrumentation", {}).get("dmi15_shadow", {})
        self.settings = settings if isinstance(settings, dict) else {}
        self.enabled = bool(self.settings.get("enabled", False))
        self.state_path = project_root / str(
            self.settings.get("state_file", "data/state/dmi15_shadow.json")
        )
        self.ledger = TradeLedger(
            project_root,
            project_root / str(
                self.settings.get("ledger_file", "data/trades/trades_dmi15_shadow.jsonl")
            ),
        )
        self.positions: list[BotFullExitPosition] = []
        self.entries_by_bucket: dict[int, int] = {}
        self.evaluated_buckets: set[int] = set()
        self.blocked_same_5m = 0
        self.blocked_capacity = 0
        self.signals_passed = 0
        self.max_simultaneous_positions = 0
        self.latest_market_context: Optional[Dict[str, Any]] = None
        if self.enabled:
            self._load_state()

    def on_closed_5m(
        self,
        market_context: Optional[Dict[str, Any]],
        entry_atr: Optional[float],
        atr_timeframe: str,
        atr_period: int,
    ) -> bool:
        if not self.enabled or not isinstance(market_context, dict):
            return False
        self.latest_market_context = deepcopy(market_context)
        snapshot = market_context.get("tf_5m")
        if not isinstance(snapshot, dict):
            return False
        bucket = _integer(snapshot.get("latest_open_at_ms"))
        if bucket is None:
            return False
        if bucket in self.evaluated_buckets:
            return False
        self.evaluated_buckets.add(bucket)

        values = _dmi_values(snapshot)
        if values is None:
            self._event("ENTRY_SKIPPED_DMI_UNAVAILABLE", bucket)
            self._save_state()
            return False
        plus_now, plus_previous, minus_now, minus_previous = values
        passed = (
            plus_now > plus_previous
            and minus_now < minus_previous
            and plus_now > minus_now
        )
        self._event(
            "DMI15_EVALUATED",
            bucket,
            passed=passed,
            plus_di_now=plus_now,
            plus_di_15m_ago=plus_previous,
            minus_di_now=minus_now,
            minus_di_15m_ago=minus_previous,
        )
        if not passed:
            self._save_state()
            return False
        if entry_atr is None or entry_atr <= 0:
            self._event("ENTRY_SKIPPED_ATR_UNAVAILABLE", bucket)
            self._save_state()
            return False

        maximum = int(self.config.get("entry", {}).get("max_entries_per_candle", 1))
        if self.entries_by_bucket.get(bucket, 0) >= maximum:
            self.blocked_same_5m += 1
            self._event("ENTRY_BLOCKED_SAME_5M_CANDLE", bucket)
            self._save_state()
            return False
        cap = int(
            self.settings.get(
                "max_open_positions",
                self.config.get("capital", {}).get("max_open_positions", 5),
            )
        )
        if len(self.open_positions) >= cap:
            self.blocked_capacity += 1
            self._event("ENTRY_BLOCKED_SHADOW_CAPACITY", bucket, cap=cap)
            self._save_state()
            return False

        close = _number(snapshot.get("close"))
        if close is None or close <= 0:
            self._event("ENTRY_SKIPPED_PRICE_UNAVAILABLE", bucket)
            self._save_state()
            return False
        self._open(
            price=close,
            bucket=bucket,
            opened_at=_iso_from_ms(_integer(snapshot.get("latest_closed_at_ms"))),
            entry_atr=entry_atr,
            atr_timeframe=atr_timeframe,
            atr_period=atr_period,
            market_context=market_context,
        )
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
            self._market_context_event(position, "EXIT", position.market_context_exit)
            try:
                self.ledger.append_closed_dmi15_shadow_trade(position, self.config)
            except Exception as exc:
                self.logger.system(
                    "dmi15_shadow_ledger_failed",
                    pair_id=position.pair_id,
                    error=str(exc),
                )
            self.logger.system(
                "dmi15_shadow_closed",
                report_strategy=self.STRATEGY,
                pair_id=position.pair_id,
                reason=position.exit_reason,
            )
            changed = True
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        if changed:
            self._save_state()

    @property
    def open_positions(self) -> list[BotFullExitPosition]:
        return [item for item in self.positions if item.status == "OPEN"]

    def record_market_context(self, snapshot: Optional[Dict[str, Any]]) -> None:
        if snapshot:
            self.latest_market_context = deepcopy(snapshot)

    def _open(
        self,
        price: float,
        bucket: int,
        opened_at: str,
        entry_atr: float,
        atr_timeframe: str,
        atr_period: int,
        market_context: Dict[str, Any],
    ) -> None:
        notional = (
            float(self.config["capital"]["operational_balance_usdt"])
            * float(self.config["capital"]["trade_size_pct"])
            / 100
        )
        client = PhantomExecutionClient()
        client.set_price(price)
        pair_id = f"dmi15-{uuid.uuid4().hex[:12]}"
        position = BotFullExitPosition(
            pair_id=pair_id,
            symbol=str(self.config["symbol"]),
            entry_price=price,
            quantity=notional / price,
            entry_order={"shadow": True},
            open_ts=opened_at,
            config=self._exit_config(),
            client=client,  # type: ignore[arg-type]
            logger=self.logger,
            entry_atr=entry_atr,
            atr_timeframe=atr_timeframe,
            atr_period=atr_period,
            source_candle_open_time=bucket,
            position_notional_usdt=notional,
            no_progress_enabled=False,
            no_progress_tolerance_seconds=None,
            no_progress_tolerance_source="DISABLED",
        )
        position.phantom = True
        position.phantom_id = pair_id
        position.shadow_kind = self.SHADOW_KIND
        position.market_context_entry = deepcopy(market_context)
        self.positions.append(position)
        self.entries_by_bucket[bucket] = self.entries_by_bucket.get(bucket, 0) + 1
        self.signals_passed += 1
        self.max_simultaneous_positions = max(
            self.max_simultaneous_positions,
            len(self.open_positions),
        )
        self.logger.trade(
            position._trade_event("OPEN", price, 0.0, None, price_source="closed_5m")
        )
        self.logger.system(
            "dmi15_shadow_opened",
            report_strategy=self.STRATEGY,
            pair_id=pair_id,
            admission_bucket_open_time=bucket,
        )
        self._market_context_event(position, "ENTRY", market_context)
        self._save_state()

    def _event(self, event: str, bucket: int, **fields: Any) -> None:
        payload = {
            "ts": now_iso(),
            "strategy": self.STRATEGY,
            "shadow_kind": self.SHADOW_KIND,
            "event": event,
            "source_candle_open_time": bucket,
            **fields,
        }
        self.logger.decision(payload)
        if self.telemetry_writer:
            self.telemetry_writer.submit("dmi15_shadow_event", payload)

    def _market_context_event(
        self,
        position: BotFullExitPosition,
        phase: str,
        context: Optional[Dict[str, Any]],
    ) -> None:
        if not self.telemetry_writer or not context:
            return
        self.telemetry_writer.submit(
            "market_context",
            {
                "ts": context.get("captured_at"),
                "strategy": self.STRATEGY,
                "shadow_kind": self.SHADOW_KIND,
                "pair_id": position.pair_id,
                "phase": phase,
                "market_context": context,
            },
        )

    def _exit_config(self) -> Dict[str, Any]:
        result = deepcopy(self.config.get("risk", {}))
        result["fees"] = self.config.get("fees", {})
        result["ladder"] = self.config.get("ladder", {})
        no_progress = result.get("no_progress")
        if isinstance(no_progress, dict):
            no_progress["enabled"] = False
        return result

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.entries_by_bucket = {
            int(key): int(value)
            for key, value in (data.get("entries_by_bucket") or {}).items()
        }
        self.evaluated_buckets = {
            int(value) for value in (data.get("evaluated_buckets") or [])
        }
        self.blocked_same_5m = int(data.get("blocked_same_5m", 0))
        self.blocked_capacity = int(data.get("blocked_capacity", 0))
        self.signals_passed = int(data.get("signals_passed", 0))
        self.max_simultaneous_positions = int(data.get("max_simultaneous_positions", 0))
        for item in data.get("positions", []):
            if item.get("status") != "OPEN":
                continue
            try:
                client = PhantomExecutionClient()
                position = BotFullExitPosition.from_state(
                    item,
                    self._exit_config(),
                    client,  # type: ignore[arg-type]
                    self.logger,
                )
                position.phantom = True
                position.phantom_id = position.pair_id
                position.shadow_kind = self.SHADOW_KIND
                self.positions.append(position)
            except Exception as exc:
                self.logger.system("dmi15_shadow_restore_failed", error=str(exc))

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        latest = max(self.evaluated_buckets) if self.evaluated_buckets else 0
        cutoff = latest - 86_400_000
        payload = {
            "updated_at": now_iso(),
            "entries_by_bucket": {
                str(key): value
                for key, value in self.entries_by_bucket.items()
                if key >= cutoff
            },
            "evaluated_buckets": sorted(
                key for key in self.evaluated_buckets if key >= cutoff
            ),
            "blocked_same_5m": self.blocked_same_5m,
            "blocked_capacity": self.blocked_capacity,
            "signals_passed": self.signals_passed,
            "max_simultaneous_positions": self.max_simultaneous_positions,
            "positions": [item.to_state() for item in self.open_positions],
        }
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)


def _dmi_values(snapshot: Dict[str, Any]) -> Optional[tuple[float, float, float, float]]:
    values = (
        _number(snapshot.get("plus_di14")),
        _number(snapshot.get("plus_di14_15m_ago")),
        _number(snapshot.get("minus_di14")),
        _number(snapshot.get("minus_di14_15m_ago")),
    )
    if any(value is None for value in values):
        return None
    return values  # type: ignore[return-value]


def _number(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso_from_ms(value: Optional[int]) -> str:
    if value is None:
        return now_iso()
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
