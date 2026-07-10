from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional

from src.exchange.binance_client import BinanceClientError
from src.logging_utils import JsonlLogger, now_iso
from src.position.position_base import PositionBase

if TYPE_CHECKING:
    from src.exchange.binance_client import BinanceClient


class ServerSimpleTrailPosition(PositionBase):
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
        position_id: Optional[int] = None,
        source_candle_open_time: Optional[int] = None,
        position_notional_usdt: Optional[float] = None,
    ) -> None:
        super().__init__(
            pair_id=pair_id,
            label="A",
            engine="SERVER_SIMPLE_TRAIL",
            symbol=symbol,
            entry_price=entry_price,
            quantity=quantity,
            entry_order=entry_order,
            reserved_qty=quantity,
            open_ts=open_ts,
            position_id=position_id,
            source_candle_open_time=source_candle_open_time,
            position_notional_usdt=position_notional_usdt,
        )
        self.config = config
        self.client = client
        self.logger = logger
        self.trailing_order: Optional[Dict[str, Any]] = None

    @classmethod
    def from_state(
        cls,
        state: Dict[str, Any],
        config: Dict[str, Any],
        client: "BinanceClient",
        logger: JsonlLogger,
    ) -> "ServerSimpleTrailPosition":
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
        position.position_id = _optional_int(state.get("position_id", position.position_id))
        position.source_candle_open_time = _optional_int(
            state.get("source_candle_open_time", position.source_candle_open_time)
        )
        position.position_notional_usdt = _optional_float(
            state.get("position_notional_usdt", position.position_notional_usdt)
        )
        position.status = str(state.get("status", "OPEN"))
        position.exit_price = state.get("exit_price")
        position.exit_reason = state.get("exit_reason")
        position.close_ts = state.get("close_ts")
        position.exit_order = state.get("exit_order")
        position.highest_price = float(state.get("highest_price", position.entry_price))
        position.trailing_order = state.get("trailing_order")
        return position

    def post_trailing_order(self) -> Dict[str, Any]:
        if bool(self.config.get("use_stop_price", False)):
            raise BinanceClientError("exit_server_simple_trail.use_stop_price must stay false")

        trailing_delta = int(self.config["trailing_delta_bips"])
        preferred_type = str(self.config.get("preferred_order_type", "STOP_LOSS"))
        fallback_type = str(self.config.get("fallback_order_type", "STOP_LOSS_LIMIT"))
        self.client.validate_trailing_delta(self.symbol, trailing_delta)
        self.validate_sell_quantity(self.reserved_qty)

        try:
            order = self.client.trailing_sell(
                symbol=self.symbol,
                quantity=self.reserved_qty,
                trailing_delta_bips=trailing_delta,
                order_type=preferred_type,
                client_order_id=f"ts-{self.pair_id}-A-trail",
            )
        except BinanceClientError as exc:
            self.logger.system(
                "preferred_trailing_order_rejected",
                pair_id=self.pair_id,
                position="A",
                preferred_order_type=preferred_type,
                fallback_order_type=fallback_type,
                error=str(exc),
            )
            order = self.client.trailing_sell(
                symbol=self.symbol,
                quantity=self.reserved_qty,
                trailing_delta_bips=trailing_delta,
                order_type=fallback_type,
                client_order_id=f"ts-{self.pair_id}-A-trail-fallback",
                limit_price=self.entry_price * 0.95,
            )

        self.trailing_order = order
        self.logger.trade(self._trade_event("OPEN", self.entry_price, 0.0, None, self.entry_order))
        return order

    def poll_fill(self) -> Optional[Dict[str, Any]]:
        if self.status != "OPEN" or not self.trailing_order:
            return None
        order = self.client.get_order(
            self.symbol,
            order_id=str(self.trailing_order.get("orderId")),
            client_order_id=self.trailing_order.get("clientOrderId"),
        )
        if order.get("status") != "FILLED":
            return None

        price = _average_fill_price(order) or self.entry_price
        self.mark_closed(price, "TRAILING", now_iso(), order)
        event = self._trade_event("CLOSE", price, self.pnl_pct(price), "TRAILING", order)
        self.logger.trade(event)
        return event

    def _trade_event(
        self,
        event: str,
        price: float,
        pnl_pct: float,
        exit_reason: Optional[str],
        order: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        order = order or {}
        return {
            "ts": now_iso(),
            "pair_id": self.pair_id,
            "position_id": self.position_id,
            "position": self.label,
            "position_notional_usdt": self.position_notional_usdt,
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
        state["trailing_order"] = self.trailing_order
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


def _optional_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _commission(order: Dict[str, Any]) -> float:
    return sum(_float_or_zero(fill.get("commission")) for fill in order.get("fills", []) or [])
