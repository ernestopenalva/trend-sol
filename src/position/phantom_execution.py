from __future__ import annotations

from typing import Any, Dict


class PhantomExecutionClient:
    """In-memory fill adapter used only by synthetic positions."""

    def __init__(self) -> None:
        self._price: float | None = None

    def set_price(self, price: float) -> None:
        self._price = float(price)

    def market_sell(self, symbol: str, quantity: float, client_order_id: str) -> Dict[str, Any]:
        if self._price is None or self._price <= 0:
            raise RuntimeError("phantom execution price is unavailable")
        return {
            "clientOrderId": client_order_id,
            "executedQty": str(quantity),
            "cummulativeQuoteQty": str(quantity * self._price),
            "fills": [{"price": str(self._price), "qty": str(quantity), "commission": "0"}],
            "phantom": True,
        }
