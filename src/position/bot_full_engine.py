from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from src.logging_utils import JsonlLogger, now_iso
from src.position.position_base import PositionBase

if TYPE_CHECKING:
    from src.exchange.binance_client import BinanceClient


class BotFullExitPosition(PositionBase):
    def __init__(
        self,
        pair_id: str,
        symbol: str,
        entry_price: float,
        quantity: float,
        entry_order: Dict[str, Any],
        open_ts: str,
        config: Dict[str, Any],
        client: "BinanceClient",
        logger: JsonlLogger,
    ) -> None:
        super().__init__(
            pair_id=pair_id,
            label="B",
            engine="BOT_FULL_EXIT_ENGINE",
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            entry_order=entry_order,
            reserved_qty=quantity,
            open_ts=open_ts,
        )
        self.config = config
        self.client = client
        self.logger = logger
        self.stop_price = entry_price * (1 - float(config["stop_loss_pct"]) / 100)
        self.breakeven_steps = sorted(config.get("breakeven", []), key=lambda item: item["trigger_pct"])
        self.applied_steps: set[float] = set()
        trailing_cfg = config.get("trailing", {})
        self.trailing_activation_pct = float(trailing_cfg.get("activation_pct", 10))
        self.trailing_gap_pct = float(trailing_cfg.get("gap_pct", 4))
        self.trailing_active = False

    @classmethod
    def from_state(
        cls,
        state: Dict[str, Any],
        config: Dict[str, Any],
        client: "BinanceClient",
        logger: JsonlLogger,
    ) -> "BotFullExitPosition":
        position = cls(
            pair_id=str(state["pair_id"]),
            symbol=str(state["symbol"]),
            entry_price=float(state["entry_price"]),
            quantity=float(state["quantity"]),
            entry_order=state.get("entry_order") or {},
            open_ts=str(state["open_ts"]),
            config=config,
            client=client,
            logger=logger,
        )
        position.reserved_qty = float(state.get("reserved_qty", position.quantity))
        position.status = str(state.get("status", "OPEN"))
        position.exit_price = state.get("exit_price")
        position.exit_reason = state.get("exit_reason")
        position.close_ts = state.get("close_ts")
        position.exit_order = state.get("exit_order")
        position.highest_price = float(state.get("highest_price", position.entry_price))
        position.stop_price = float(state.get("stop_price", position.stop_price))
        position.applied_steps = {float(item) for item in state.get("applied_steps", [])}
        position.trailing_active = bool(state.get("trailing_active", False))
        return position

    def on_tick(self, price: float) -> Optional[Dict[str, Any]]:
        if self.status != "OPEN":
            return None

        ts = now_iso()
        if price > self.highest_price:
            self.highest_price = price

        pnl_pct = self.pnl_pct(price)
        for index, step in enumerate(self.breakeven_steps, start=1):
            trigger = float(step["trigger_pct"])
            stop_to = float(step["stop_to_pct"])
            if pnl_pct >= trigger and trigger not in self.applied_steps:
                new_stop = self.entry_price * (1 + stop_to / 100)
                if new_stop > self.stop_price:
                    self.stop_price = new_stop
                self.applied_steps.add(trigger)
                self.logger.trade(
                    self._trade_event(
                        event=f"BREAKEVEN_{index}",
                        price=price,
                        pnl_pct=pnl_pct,
                        exit_reason=None,
                    )
                )

        if pnl_pct >= self.trailing_activation_pct and not self.trailing_active:
            self.trailing_active = True
            self.logger.trade(
                self._trade_event(
                    event="TRAILING_ACTIVATED",
                    price=price,
                    pnl_pct=pnl_pct,
                    exit_reason=None,
                )
            )

        trailing_stop = None
        if self.trailing_active:
            trailing_stop = self.highest_price * (1 - self.trailing_gap_pct / 100)

        reason = None
        if price <= self.stop_price:
            reason = "BREAKEVEN_FLOOR" if self.applied_steps else "STOP_LOSS"
        elif trailing_stop is not None and price <= trailing_stop:
            reason = "TRAILING"

        if reason is None:
            return None

        client_order_id = f"ts-{self.pair_id}-B-close"
        self.validate_sell_quantity(self.reserved_qty)
        order = self.client.market_sell(self.symbol, self.reserved_qty, client_order_id)
        executed_price = _average_fill_price(order) or price
        self.mark_closed(executed_price, reason, ts, order)
        event = self._trade_event("CLOSE", executed_price, self.pnl_pct(executed_price), reason, order)
        self.logger.trade(event)
        return event

    def _trade_event(
        self,
        event: str,
        price: float,
        pnl_pct: float,
        exit_reason: Optional[str],
        order: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        order = order or {}
        return {
            "ts": now_iso(),
            "pair_id": self.pair_id,
            "position": self.label,
            "engine": self.engine,
            "event": event,
            "price": price,
            "pnl_pct": pnl_pct,
            "exit_reason": exit_reason,
            "order_id": order.get("orderId"),
            "client_order_id": order.get("clientOrderId"),
            "executed_qty": _float_or_zero(order.get("executedQty")),
            "cummulative_quote_qty": _float_or_zero(order.get("cummulativeQuoteQty")),
            "commission": _commission(order),
        }

    def to_state(self) -> Dict[str, Any]:
        state = super().to_state()
        state.update(
            {
                "stop_price": self.stop_price,
                "applied_steps": sorted(self.applied_steps),
                "trailing_active": self.trailing_active,
                "trailing_activation_pct": self.trailing_activation_pct,
                "trailing_gap_pct": self.trailing_gap_pct,
            }
        )
        return state


def _average_fill_price(order: Dict[str, Any]) -> Optional[float]:
    quote = _float_or_zero(order.get("cummulativeQuoteQty"))
    qty = _float_or_zero(order.get("executedQty"))
    if quote > 0 and qty > 0:
        return quote / qty
    fills = order.get("fills") or []
    if fills:
        total_qty = sum(_float_or_zero(fill.get("qty")) for fill in fills)
        total_quote = sum(_float_or_zero(fill.get("price")) * _float_or_zero(fill.get("qty")) for fill in fills)
        if total_qty > 0:
            return total_quote / total_qty
    return None


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _commission(order: Dict[str, Any]) -> float:
    return sum(_float_or_zero(fill.get("commission")) for fill in order.get("fills", []) or [])
