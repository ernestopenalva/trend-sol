from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class PositionBase:
    pair_id: str
    label: str
    engine: str
    symbol: str
    entry_price: float
    quantity: float
    entry_order: Dict[str, Any]
    reserved_qty: float
    open_ts: str
    status: str = "OPEN"
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    close_ts: Optional[str] = None
    exit_order: Optional[Dict[str, Any]] = None
    highest_price: float = field(init=False)

    def __post_init__(self) -> None:
        self.highest_price = self.entry_price

    def pnl_pct(self, price: float) -> float:
        return ((price / self.entry_price) - 1) * 100

    def validate_sell_quantity(self, quantity: float) -> None:
        if quantity <= 0:
            raise ValueError("sell quantity must be positive")
        if quantity > self.reserved_qty:
            raise ValueError(
                f"sell quantity exceeds reserved quantity for {self.pair_id}/{self.label}: "
                f"{quantity} > {self.reserved_qty}"
            )

    def mark_closed(self, price: float, reason: str, ts: str, order: Dict[str, Any]) -> None:
        self.status = "CLOSED"
        self.exit_price = price
        self.exit_reason = reason
        self.close_ts = ts
        self.exit_order = order
        self.reserved_qty = 0.0

    def to_state(self) -> Dict[str, Any]:
        return {
            "pair_id": self.pair_id,
            "label": self.label,
            "engine": self.engine,
            "symbol": self.symbol,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "entry_order": self.entry_order,
            "reserved_qty": self.reserved_qty,
            "open_ts": self.open_ts,
            "status": self.status,
            "exit_price": self.exit_price,
            "exit_reason": self.exit_reason,
            "close_ts": self.close_ts,
            "exit_order": self.exit_order,
            "highest_price": self.highest_price,
        }
