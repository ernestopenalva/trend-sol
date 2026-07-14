from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from src.position.bot_full_engine import BotFullExitPosition


class TradeLedger:
    def __init__(self, project_root: Path) -> None:
        self.path = project_root / "data" / "trades" / "trades_B.jsonl"

    def append_closed_bot_trade(self, position: BotFullExitPosition, config: Dict[str, Any]) -> bool:
        if position.status != "CLOSED":
            return False
        if self._contains(position.pair_id, "BOT_EXIT"):
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(self._record(position, config), ensure_ascii=False) + "\n")
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

    def _record(self, position: BotFullExitPosition, config: Dict[str, Any]) -> Dict[str, Any]:
        entry_price = _float_or_none(position.entry_price)
        exit_price = _float_or_none(position.exit_price)
        qty = _float_or_none(position.quantity)
        realized_pct = position.pnl_pct(exit_price) if exit_price is not None else None
        estimated_fees_pct = _estimated_fees_pct(config)
        net_pct = realized_pct - estimated_fees_pct if realized_pct is not None else None
        return {
            "run_id": config.get("run_id"),
            "strategy_version": config.get("strategy_version"),
            "profile": config.get("active_profile"),
            "pair_id": position.pair_id,
            "position_id": position.position_id,
            "symbol": position.symbol,
            "position_type": "BOT_EXIT",
            "position_notional_usdt": _float_or_none(position.position_notional_usdt),
            "opened_at": position.open_ts,
            "closed_at": position.close_ts,
            "age_seconds": _age_seconds(position.open_ts, position.close_ts),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_price_source": "market_fill" if position.exit_order else None,
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
            "be_atr_stop": _float_or_none(getattr(position, "be_atr_stop", None)),
            "be_net_floor": _float_or_none(getattr(position, "be_net_floor", None)),
            "be_stop": _float_or_none(getattr(position, "be_stop", None)),
            "be_activation_price": _float_or_none(getattr(position, "be_activation_price", None)),
            "be_activation_buffer_atr": _float_or_none(getattr(position, "be_activation_buffer_atr", None)),
            "be_floor_source": getattr(position, "be_floor_source", None),
            "be_floor_absorbed_atr_stop": getattr(position, "be_floor_absorbed_atr_stop", None),
            "realized_pnl_pct": realized_pct,
            "gross_pnl_pct": realized_pct,
            "estimated_fees_pct": estimated_fees_pct,
            "net_pnl_pct": net_pct,
            "realized_pnl_abs": ((exit_price - entry_price) * qty) if None not in (entry_price, exit_price, qty) else None,
        }


def latest_trade(records: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    latest = None
    latest_ts = None
    for record in records:
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
