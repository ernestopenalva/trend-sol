from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from src.position.bot_full_engine import BotFullExitPosition


class TradeLedger:
    def __init__(self, project_root: Path, path: Path | None = None) -> None:
        self.path = path or (project_root / "data" / "trades" / "trades_B.jsonl")

    def append_closed_bot_trade(self, position: BotFullExitPosition, config: Dict[str, Any]) -> bool:
        return self._append_closed(position, config, "BOT_EXIT")

    def append_closed_phantom_trade(self, position: BotFullExitPosition, config: Dict[str, Any]) -> bool:
        return self._append_closed(position, config, "PHANTOM")

    def append_closed_market_shadow_trade(
        self,
        position: BotFullExitPosition,
        config: Dict[str, Any],
    ) -> bool:
        return self._append_closed(position, config, "MARKET_SHADOW")

    def append_closed_gcr_shadow_trade(
        self,
        position: BotFullExitPosition,
        config: Dict[str, Any],
    ) -> bool:
        return self._append_closed(position, config, "GCR_SHADOW")

    def append_closed_dmi15_shadow_trade(
        self,
        position: BotFullExitPosition,
        config: Dict[str, Any],
    ) -> bool:
        return self._append_closed(position, config, "DMI15_SHADOW")

    def append_closed_dmi15_spread_shadow_trade(
        self,
        position: BotFullExitPosition,
        config: Dict[str, Any],
    ) -> bool:
        return self._append_closed(position, config, "DMI15_SPREAD_SHADOW")

    def append_closed_dmi15_trajectory_shadow_trade(
        self, position: BotFullExitPosition, config: Dict[str, Any]
    ) -> bool:
        return self._append_closed(position, config, "DMI15_TRAJECTORY_SHADOW")

    def append_closed_dmi15_rsi70_shadow_trade(
        self, position: BotFullExitPosition, config: Dict[str, Any]
    ) -> bool:
        return self._append_closed(position, config, "DMI15_RSI70_SHADOW")

    def append_closed_dmi15_combined_shadow_trade(
        self, position: BotFullExitPosition, config: Dict[str, Any]
    ) -> bool:
        return self._append_closed(position, config, "DMI15_COMBINED_SHADOW")

    def append_closed_context_shadow_trade(
        self, position: BotFullExitPosition, config: Dict[str, Any]
    ) -> bool:
        return self._append_closed(position, config, "CONTEXT_SHADOW")

    def append_closed_h2_exposure_shadow_trade(
        self,
        position: BotFullExitPosition,
        config: Dict[str, Any],
        entry_metadata: Dict[str, Any],
    ) -> bool:
        """Append H2-only dollar fields without changing other ledger schemas."""
        if position.status != "CLOSED" or self._contains(position.pair_id, "H2_EXPOSURE_SHADOW"):
            return False
        record = self._record(position, config, "H2_EXPOSURE_SHADOW")
        gross = _float_or_none(record.get("realized_pnl_abs")) or 0.0
        notional = _float_or_none(record.get("position_notional_usdt")) or 0.0
        fees = notional * (_float_or_none(record.get("estimated_fees_pct")) or 0.0) / 100
        record.update({
            "shadow_kind": "H2_EXPOSURE_SHADOW",
            "h2": entry_metadata,
            "gross_pnl_usdt": gross,
            "estimated_fees_usdt": fees,
            "net_pnl_usdt": gross - fees,
        })
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True

    def _append_closed(
        self,
        position: BotFullExitPosition,
        config: Dict[str, Any],
        position_type: str,
    ) -> bool:
        if position.status != "CLOSED":
            return False
        if self._contains(position.pair_id, position_type):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._record(position, config, position_type), ensure_ascii=False) + "\n")
        return True

    def load(self) -> list[Dict[str, Any]]:
        if not self.path.exists():
            return []
        records = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return records

    def _contains(self, pair_id: str, position_type: str) -> bool:
        return any(
            str(record.get("pair_id")) == pair_id and str(record.get("position_type")) == position_type
            for record in self.load()
        )

    def _record(
        self,
        position: BotFullExitPosition,
        config: Dict[str, Any],
        position_type: str = "BOT_EXIT",
    ) -> Dict[str, Any]:
        entry_price = _float_or_none(position.entry_price)
        exit_price = _float_or_none(position.exit_price)
        qty = _float_or_none(position.quantity)
        realized_pct = position.pnl_pct(exit_price) if exit_price is not None else None
        estimated_fees_pct = _estimated_fees_pct(config)
        net_pct = realized_pct - estimated_fees_pct if realized_pct is not None else None
        phantom = position_type == "PHANTOM" or bool(getattr(position, "phantom", False))
        return {
            "run_id": config.get("run_id"),
            "strategy_version": config.get("strategy_version"),
            "profile": config.get("active_profile"),
            "pair_id": position.pair_id,
            "position_id": position.position_id,
            "source_candle_open_time": position.source_candle_open_time,
            "phantom": phantom,
            "phantom_id": getattr(position, "phantom_id", None),
            "shadow_kind": getattr(position, "shadow_kind", None),
            "shadow_selection_epoch_ms": getattr(
                position,
                "shadow_selection_epoch_ms",
                None,
            ),
            "shadow_selection_rank": getattr(
                position,
                "shadow_selection_rank",
                None,
            ),
            "shadow_selection_snapshot": getattr(
                position,
                "shadow_selection_snapshot",
                None,
            ),
            "symbol": position.symbol,
            "position_type": position_type,
            "position_notional_usdt": _float_or_none(position.position_notional_usdt),
            "opened_at": position.open_ts,
            "closed_at": position.close_ts,
            "age_seconds": _age_seconds(position.open_ts, position.close_ts),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_price_source": "phantom_tick" if phantom else ("market_fill" if position.exit_order else None),
            "exit_trigger_price": _float_or_none(getattr(position, "exit_trigger_price", None)),
            "exit_trigger_price_source": getattr(position, "exit_trigger_price_source", None),
            "qty": qty,
            "entry_atr": _float_or_none(position.entry_atr),
            "peak_price": _float_or_none(position.highest_price),
            "peak_atr": position.peak_atr(),
            "hard_stop_enabled": position.hard_stop_enabled,
            "hard_stop_pct": _float_or_none(position.hard_stop_pct),
            "hard_stop_price": _float_or_none(position.hard_stop_price),
            "hard_stop_applied_on_restore": position.hard_stop_applied_on_restore,
            "trough_price": _float_or_none(position.trough_price),
            "trough_pct": position.trough_pct(),
            "trough_atr": position.trough_atr(),
            "trough_at": position.trough_at,
            "time_to_trough_seconds": position.time_to_trough_seconds(),
            "trough_tracking_complete": position.trough_tracking_complete,
            "trough_tracking_started_at": position.trough_tracking_started_at,
            "stop_hit": _float_or_none(position.effective_stop),
            "exit_slippage_pct": _float_or_none(getattr(position, "exit_slippage_pct", None)),
            "exit_reason": position.exit_reason,
            "final_step": _final_step(position),
            "profit_lock_step": getattr(position, "profit_lock_step", None),
            "profit_lock_raw_stop": _float_or_none(getattr(position, "profit_lock_raw_stop", None)),
            "profit_lock_economic_floor": _float_or_none(
                getattr(position, "profit_lock_economic_floor", None)
            ),
            "profit_lock_floor_sufficient": getattr(position, "profit_lock_floor_sufficient", None),
            "profit_lock_action": getattr(position, "profit_lock_action", None),
            "profit_lock_trigger_atr": _float_or_none(
                getattr(position, "profit_lock_trigger_atr", None)
            ),
            "profit_lock_lock_atr": _float_or_none(getattr(position, "profit_lock_lock_atr", None)),
            "profit_lock_raw_trigger": _float_or_none(
                getattr(position, "profit_lock_raw_trigger", None)
            ),
            "profit_lock_effective_trigger": _float_or_none(
                getattr(position, "profit_lock_effective_trigger", None)
            ),
            "profit_lock_floor_absorbed": getattr(position, "profit_lock_floor_absorbed", None),
            "profit_lock_net_margin_pct": _float_or_none(
                getattr(position, "profit_lock_net_margin_pct", None)
            ),
            "be_atr_stop": _float_or_none(getattr(position, "be_atr_stop", None)),
            "be_net_floor": _float_or_none(getattr(position, "be_net_floor", None)),
            "be_stop": _float_or_none(getattr(position, "be_stop", None)),
            "be_activation_price": _float_or_none(getattr(position, "be_activation_price", None)),
            "be_activation_buffer_atr": _float_or_none(getattr(position, "be_activation_buffer_atr", None)),
            "be_floor_source": getattr(position, "be_floor_source", None),
            "be_floor_absorbed_atr_stop": getattr(position, "be_floor_absorbed_atr_stop", None),
            "be_armed_at": getattr(position, "be_armed_at", None),
            "time_to_be_seconds": _float_or_none(getattr(position, "time_to_be_seconds", None)),
            "no_progress_enabled": bool(getattr(position, "no_progress_enabled", False)),
            "no_progress_tolerance_seconds": _float_or_none(
                getattr(position, "no_progress_tolerance_seconds", None)
            ),
            "no_progress_tolerance_source": getattr(position, "no_progress_tolerance_source", None),
            "market_context_entry": getattr(position, "market_context_entry", None),
            "market_context_exit": getattr(position, "market_context_exit", None),
            "pl_shadow_enabled": getattr(position, "pl_shadow_enabled", False),
            "pl_shadow_status": getattr(position, "pl_shadow_status", None),
            "pl_shadow_step": getattr(position, "pl_shadow_step", None),
            "pl_shadow_raw_stop": _float_or_none(getattr(position, "pl_shadow_raw_stop", None)),
            "pl_shadow_net_floor": _float_or_none(getattr(position, "pl_shadow_net_floor", None)),
            "pl_shadow_stop": _float_or_none(getattr(position, "pl_shadow_stop", None)),
            "pl_shadow_activation_price": _float_or_none(
                getattr(position, "pl_shadow_activation_price", None)
            ),
            "pl_shadow_activation_buffer_atr": _float_or_none(
                getattr(position, "pl_shadow_activation_buffer_atr", None)
            ),
            "pl_shadow_net_margin_pct": _float_or_none(
                getattr(position, "pl_shadow_net_margin_pct", None)
            ),
            "pl_shadow_floor_absorbed": getattr(position, "pl_shadow_floor_absorbed", None),
            "pl_shadow_active_step": getattr(position, "pl_shadow_active_step", None),
            "pl_shadow_active_stop": _float_or_none(getattr(position, "pl_shadow_active_stop", None)),
            "pl_shadow_activated_at": getattr(position, "pl_shadow_activated_at", None),
            "pl_shadow_close_price": _float_or_none(getattr(position, "pl_shadow_close_price", None)),
            "pl_shadow_closed_at": getattr(position, "pl_shadow_closed_at", None),
            "pl_shadow_censored_by_real_exit": bool(
                getattr(position, "pl_shadow_enabled", False)
                and getattr(position, "pl_shadow_status", None) != "CLOSED"
            ),
            "realized_pnl_pct": realized_pct,
            "gross_pnl_pct": realized_pct,
            "estimated_fees_pct": estimated_fees_pct,
            "net_pnl_pct": net_pct,
            "realized_pnl_abs": ((exit_price - entry_price) * qty) if None not in (entry_price, exit_price, qty) else None,
        }


def latest_trade(
    records: Iterable[Dict[str, Any]],
    include_phantoms: bool = False,
) -> Optional[Dict[str, Any]]:
    latest = None
    latest_ts = None
    for record in records:
        if not include_phantoms and (
            bool(record.get("phantom", False)) or str(record.get("position_type") or "") == "PHANTOM"
        ):
            continue
        closed = _parse_ts(record.get("closed_at"))
        if closed is not None and (latest_ts is None or closed > latest_ts):
            latest = record
            latest_ts = closed
    return latest


def _final_step(position: BotFullExitPosition) -> str:
    if position.trailing_active:
        return "TRAIL"
    peak = position.peak_atr()
    if peak is None:
        return "NONE"
    steps = [
        ("PL3", 12),
        ("PL2", 8),
        ("PL1", 5),
        ("BE", 3),
    ]
    for name, trigger in steps:
        if peak >= trigger:
            return name
    return "NONE"


def _age_seconds(opened_at: Any, closed_at: Any) -> Optional[int]:
    opened = _parse_ts(opened_at)
    closed = _parse_ts(closed_at)
    if opened is None or closed is None:
        return None
    return max(0, int((closed - opened).total_seconds()))


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _float_or_none(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _estimated_fees_pct(config: Dict[str, Any]) -> float:
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    if not fees or not bool(fees.get("enabled", False)):
        return 0.0
    taker_fee_pct = _float_or_none(fees.get("taker_fee_pct")) or 0.0
    if bool(fees.get("use_bnb_discount", False)):
        taker_fee_pct *= 0.75
    return taker_fee_pct * 2
