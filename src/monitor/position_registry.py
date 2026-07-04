from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from src.exchange.binance_client import BinanceClient
from src.logging_utils import JsonlLogger, now_iso
from src.monitor.cycle_manager import CycleManager
from src.monitor.entry_engine import EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from src.position.position_base import PositionBase
from src.position.server_simple_trail import ServerSimpleTrailPosition


class PositionRegistry:
    def __init__(
        self,
        config: Dict[str, Any],
        client: BinanceClient,
        logger: JsonlLogger,
        cycle_manager: CycleManager,
    ) -> None:
        self.config = config
        self.symbol = str(config["symbol"])
        self.client = client
        self.logger = logger
        self.cycle_manager = cycle_manager
        self.positions: List[PositionBase] = []

    def open_pair(self, signal: EntrySignal) -> None:
        if self.open_pair_count >= int(self.config["capital"]["max_open_pairs"]):
            self.logger.system("max_open_pairs reached; signal ignored", price=signal.price)
            return

        capital_cfg = self.config["capital"]
        quote_per_position = (
            float(capital_cfg["operational_balance_usdt"])
            * float(capital_cfg["trade_size_pct"])
            / 100
        )
        pair_id = uuid.uuid4().hex[:12]
        opened: List[PositionBase] = []

        for label in ("A", "B"):
            client_order_id = f"ts-{pair_id}-{label}-buy"
            order = self.client.market_buy_quote(self.symbol, quote_per_position, client_order_id)
            entry_price = _average_fill_price(order)
            quantity = _float_or_zero(order.get("executedQty"))
            if entry_price <= 0 or quantity <= 0:
                raise RuntimeError(f"invalid market buy fill for position {label}: {order}")
            self.client.validate_notional(self.symbol, quantity, entry_price)
            open_ts = now_iso()

            if label == "A":
                position = ServerSimpleTrailPosition(
                    pair_id,
                    self.symbol,
                    entry_price,
                    quantity,
                    order,
                    open_ts,
                    self.config["exit_server_simple_trail"],
                    self.client,
                    self.logger,
                )
                position.post_trailing_order()
            else:
                position = BotFullExitPosition(
                    pair_id,
                    self.symbol,
                    entry_price,
                    quantity,
                    order,
                    open_ts,
                    self.config["exit_bot_full_engine"],
                    self.client,
                    self.logger,
                )
                self.logger.trade(position._trade_event("OPEN", entry_price, 0.0, None, order))

            opened.append(position)

        slippage_pp = abs(opened[0].entry_price - opened[1].entry_price) / opened[0].entry_price * 100
        self.logger.system(
            "pair opened",
            pair_id=pair_id,
            entry_A=opened[0].entry_price,
            entry_B=opened[1].entry_price,
            fill_divergence_pct=slippage_pp,
        )
        self.positions.extend(opened)

    def on_tick(self, price: float) -> None:
        for position in list(self.positions):
            if isinstance(position, ServerSimpleTrailPosition):
                event = position.poll_fill()
            elif isinstance(position, BotFullExitPosition):
                event = position.on_tick(price)
            else:
                event = None
            if event:
                self.cycle_manager.on_position_closed(position)
        self._purge_closed_pairs()

    @property
    def open_pair_count(self) -> int:
        return len({position.pair_id for position in self.positions if position.status == "OPEN"})

    def reserved_qty(self, pair_id: Optional[str] = None) -> float:
        return sum(
            position.reserved_qty
            for position in self.positions
            if position.status == "OPEN" and (pair_id is None or position.pair_id == pair_id)
        )

    def _purge_closed_pairs(self) -> None:
        keep: List[PositionBase] = []
        for position in self.positions:
            pair_positions = [item for item in self.positions if item.pair_id == position.pair_id]
            if len(pair_positions) == 2 and all(item.status == "CLOSED" for item in pair_positions):
                self.cycle_manager.on_pair_closed(pair_positions)
                continue
            keep.append(position)
        seen = set()
        self.positions = []
        for position in keep:
            key = (position.pair_id, position.label)
            if key not in seen:
                seen.add(key)
                self.positions.append(position)


def _average_fill_price(order: Dict[str, Any]) -> float:
    quote = _float_or_zero(order.get("cummulativeQuoteQty"))
    qty = _float_or_zero(order.get("executedQty"))
    if quote > 0 and qty > 0:
        return quote / qty
    fills = order.get("fills") or []
    total_qty = sum(_float_or_zero(fill.get("qty")) for fill in fills)
    total_quote = sum(_float_or_zero(fill.get("price")) * _float_or_zero(fill.get("qty")) for fill in fills)
    return total_quote / total_qty if total_qty > 0 else 0.0


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
