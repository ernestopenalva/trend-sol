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
from src.state_manager import StateManager
from src.trade_ledger import TradeLedger


class PositionRegistry:
    def __init__(
        self,
        config: Dict[str, Any],
        client: BinanceClient,
        logger: JsonlLogger,
        cycle_manager: CycleManager,
        state_manager: StateManager,
        trade_ledger: TradeLedger | None = None,
    ) -> None:
        self.config = config
        self.symbol = str(config["symbol"])
        self.client = client
        self.logger = logger
        self.cycle_manager = cycle_manager
        self.state_manager = state_manager
        self.trade_ledger = trade_ledger
        self.positions: List[PositionBase] = []
        self.entries_paused = False
        self.review_required = False
        self._blocked_candles: set[int] = set()
        self._last_entry_candle_open_time: Optional[int] = None
        self._entries_by_candle: dict[int, int] = {}
        self._last_admission_details: Dict[str, Any] = {}
        self._next_position_id = 1
        self.load_state()
        self.reconcile_with_binance()

    def open_pair(self, signal: EntrySignal) -> None:
        blocked_reason = self._admission_block_reason(signal)
        if blocked_reason:
            self._log_blocked_signal(signal, blocked_reason)
            return
        if self.review_required:
            self.logger.system("entry_paused_needs_review", price=signal.price)
            return

        capital_cfg = self.config["capital"]
        quote_per_position = (
            float(capital_cfg["operational_balance_usdt"])
            * float(capital_cfg["trade_size_pct"])
            / 100
        )
        pair_id = uuid.uuid4().hex[:12]
        opened: List[PositionBase] = []

        for label in self._position_labels():
            position_id = self._allocate_position_id()
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
                    position_id=position_id,
                    source_candle_open_time=signal.source_candle_open_time,
                    position_notional_usdt=quote_per_position,
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
                    self._bot_exit_config(),
                    self.client,
                    self.logger,
                    entry_atr=signal.entry_atr,
                    atr_timeframe=signal.atr_timeframe,
                    atr_period=signal.atr_period,
                    position_id=position_id,
                    source_candle_open_time=signal.source_candle_open_time,
                    position_notional_usdt=quote_per_position,
                )
                self.logger.trade(position._trade_event("OPEN", entry_price, 0.0, None, order))

            opened.append(position)
            self.positions.append(position)
            self._last_entry_candle_open_time = signal.source_candle_open_time
            self.save_state()

        self._entries_by_candle[signal.source_candle_open_time] = (
            self._entries_by_candle.get(signal.source_candle_open_time, 0) + 1
        )
        if len(opened) == 2:
            slippage_pp = abs(opened[0].entry_price - opened[1].entry_price) / opened[0].entry_price * 100
            self.logger.system(
                "pair_opened",
                pair_id=pair_id,
                entry_A=opened[0].entry_price,
                entry_B=opened[1].entry_price,
                fill_divergence_pct=slippage_pp,
            )
        else:
            self.logger.system("bot_position_opened", pair_id=pair_id, entry=opened[0].entry_price)
        self.save_state()

    def on_tick(self, price: float) -> None:
        for position in list(self.positions):
            if isinstance(position, ServerSimpleTrailPosition):
                event = position.poll_fill()
            elif isinstance(position, BotFullExitPosition):
                event = position.on_tick(price)
            else:
                event = None
            if event:
                if self._bot_exit_only and isinstance(position, BotFullExitPosition):
                    if self.trade_ledger:
                        self.trade_ledger.append_closed_bot_trade(position, self.config)
                    self.cycle_manager.on_position_closed(position)
                    self.save_state()
                    continue
                self.cycle_manager.on_position_closed(position)
                self.save_state()
            if position.status == "NEEDS_REVIEW":
                self.review_required = True
        if self._bot_exit_only:
            self._purge_closed_bot_positions()
        else:
            self._purge_closed_pairs()
        self.save_state()

    def load_state(self) -> None:
        restored: List[PositionBase] = []
        for item in self.state_manager.load_open_positions():
            try:
                if self._bot_exit_only and item.get("label") == "A":
                    continue
                if item.get("label") == "A":
                    restored.append(
                        ServerSimpleTrailPosition.from_state(
                            item,
                            self.config["exit_server_simple_trail"],
                            self.client,
                            self.logger,
                        )
                    )
                elif item.get("label") == "B":
                    restored.append(
                        BotFullExitPosition.from_state(
                            item,
                            self._bot_exit_config(),
                            self.client,
                            self.logger,
                        )
                    )
            except Exception as exc:
                item["status"] = "NEEDS_REVIEW"
                self.review_required = True
                self.logger.system("position_restore_failed", pair_id=item.get("pair_id"), error=str(exc))
        self.positions = restored
        self._last_entry_candle_open_time = _latest_candle_open_time(restored)
        self._entries_by_candle = _entries_by_candle(restored)
        self._next_position_id = _next_position_id(restored)
        if restored:
            self.logger.system("positions_restored", positions=len(restored), open_pairs=self.open_pair_count)

    def save_state(self) -> None:
        self.state_manager.save_open_positions([position.to_state() for position in self.positions])

    def reconcile_with_binance(self) -> None:
        try:
            open_orders = self.client.open_orders(self.symbol)
            all_orders = self.client.all_orders(self.symbol, limit=100)
        except Exception as exc:
            if self.positions:
                self.review_required = True
                self._mark_all_open_needs_review(f"binance_reconcile_failed: {exc}")
                self.save_state()
            return

        open_client_ids = {str(order.get("clientOrderId")) for order in open_orders}
        all_client_ids = {str(order.get("clientOrderId")) for order in all_orders}
        if not self.positions:
            stale_bot_orders = sorted(
                client_id
                for client_id in open_client_ids
                if client_id.startswith("ts-") and not (self._bot_exit_only and "-A-" in client_id)
            )
            if stale_bot_orders:
                self.review_required = True
                self.logger.system(
                    "binance_orders_without_local_state",
                    orders=",".join(stale_bot_orders[:10]),
                    count=len(stale_bot_orders),
                )
            return

        for position in self.positions:
            if not isinstance(position, ServerSimpleTrailPosition) or position.status != "OPEN":
                continue
            trailing_order = position.trailing_order or {}
            client_order_id = str(trailing_order.get("clientOrderId") or "")
            if client_order_id and client_order_id not in open_client_ids and client_order_id not in all_client_ids:
                position.status = "NEEDS_REVIEW"
                self.review_required = True
                self.logger.system(
                    "position_needs_review",
                    pair_id=position.pair_id,
                    position=position.label,
                    reason="trailing_order_not_found_on_binance",
                    client_order_id=client_order_id,
                )
        if self.review_required:
            self.save_state()

    def _mark_all_open_needs_review(self, reason: str) -> None:
        for position in self.positions:
            if position.status == "OPEN":
                position.status = "NEEDS_REVIEW"
        self.logger.system("positions_need_review", reason=reason)

    @property
    def open_pair_count(self) -> int:
        return len({position.pair_id for position in self.positions if position.status == "OPEN"})

    @property
    def max_open_pairs(self) -> int:
        return self.max_open_positions

    @property
    def max_open_positions(self) -> int:
        capital = self.config.get("capital", {})
        return int(capital.get("max_open_positions", capital.get("max_open_pairs", 1)))

    @property
    def capacity_full(self) -> bool:
        return self.open_pair_count >= self.max_open_positions

    def position_summary(self, current_price: Optional[float] = None) -> Dict[str, Any]:
        open_positions = [position for position in self.positions if position.status == "OPEN"]
        pair_ids = {position.pair_id for position in open_positions}
        bot_positions = [position for position in open_positions if position.label == "B"]
        pnl_values = [
            position.pnl_pct(current_price)
            for position in bot_positions
            if current_price is not None and position.entry_price > 0
        ]
        return {
            "pairs": len(pair_ids),
            "server_open": sum(1 for position in open_positions if position.label == "A"),
            "bot_open": len(bot_positions),
            "needs_review": sum(1 for position in self.positions if position.status == "NEEDS_REVIEW"),
            "bot_pnl_min": min(pnl_values) if pnl_values else None,
            "bot_pnl_max": max(pnl_values) if pnl_values else None,
            **(_bot_position_details(bot_positions[0], current_price) if bot_positions else {}),
        }

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

    def _purge_closed_bot_positions(self) -> None:
        self.positions = [
            position
            for position in self.positions
            if not (isinstance(position, BotFullExitPosition) and position.status == "CLOSED")
        ]

    def _bot_exit_config(self) -> Dict[str, Any]:
        config = dict(self.config.get("risk") or self.config["exit_bot_full_engine"])
        config["fees"] = self.config.get("fees", {})
        config["ladder"] = self.config.get("ladder", {})
        return config

    def _position_labels(self) -> tuple[str, ...]:
        return ("B",) if self._bot_exit_only else ("A", "B")

    @property
    def _bot_exit_only(self) -> bool:
        return str(self.config.get("position_mode", "paired_ab")) == "bot_exit_only"

    def _admission_block_reason(self, signal: EntrySignal) -> Optional[str]:
        self._last_admission_details = {}
        if self.open_pair_count >= self.max_open_positions:
            return "BLOCKED_MAX_POSITIONS"
        max_entries = int(self.config.get("entry", {}).get("max_entries_per_candle", 1))
        if max_entries <= 0:
            return "BLOCKED_CANDLE_LIMIT"
        entries_this_candle = self._entries_by_candle.get(signal.source_candle_open_time, 0)
        if entries_this_candle >= max_entries:
            return "BLOCKED_CANDLE_LIMIT"
        spacing_atr = float(self.config.get("entry", {}).get("entry_spacing_atr", 0))
        if spacing_atr <= 0:
            return None
        if signal.entry_atr is None or signal.entry_atr <= 0:
            return "BLOCKED_SPACING"
        minimum_distance = spacing_atr * float(signal.entry_atr)
        for position in self.positions:
            if position.status != "OPEN":
                continue
            actual_distance = abs(float(signal.price) - float(position.entry_price))
            if actual_distance < minimum_distance:
                self._last_admission_details = {
                    "current_price": signal.price,
                    "blocked_against_position_id": position.position_id,
                    "blocked_against_pair_id": position.pair_id,
                    "existing_entry_price": position.entry_price,
                    "new_signal_atr": signal.entry_atr,
                    "required_distance": minimum_distance,
                    "actual_distance": actual_distance,
                }
                return "BLOCKED_SPACING"
        return None

    def _log_blocked_signal(self, signal: EntrySignal, reason: str) -> None:
        if signal.source_candle_open_time in self._blocked_candles:
            return
        self._blocked_candles.add(signal.source_candle_open_time)
        self.logger.decision(
            {
                "ts": now_iso(),
                "gate": 6,
                "passed": False,
                "near_miss": False,
                "reason": reason,
                "price": signal.price,
                "entry_atr": signal.entry_atr,
                "atr_timeframe": signal.atr_timeframe,
                "atr_period": signal.atr_period,
                "source_candle_open_time": signal.source_candle_open_time,
                "open_positions": self.open_pair_count,
                "max_open_positions": self.max_open_positions,
                **self._last_admission_details,
            }
        )

    def _allocate_position_id(self) -> int:
        position_id = self._next_position_id
        self._next_position_id += 1
        return position_id


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


def _next_position_id(positions: List[PositionBase]) -> int:
    ids = [position.position_id for position in positions if position.position_id is not None]
    return (max(ids) + 1) if ids else 1


def _latest_candle_open_time(positions: List[PositionBase]) -> Optional[int]:
    values = [
        position.source_candle_open_time
        for position in positions
        if position.source_candle_open_time is not None
    ]
    return max(values) if values else None


def _entries_by_candle(positions: List[PositionBase]) -> dict[int, int]:
    counts: dict[int, set[str]] = {}
    for position in positions:
        if position.source_candle_open_time is None:
            continue
        counts.setdefault(position.source_candle_open_time, set()).add(position.pair_id)
    return {candle: len(pair_ids) for candle, pair_ids in counts.items()}


def _bot_position_details(position: PositionBase, current_price: Optional[float]) -> Dict[str, Any]:
    pnl_atr = None
    if current_price is not None and hasattr(position, "pnl_atr"):
        pnl_atr = position.pnl_atr(current_price)  # type: ignore[attr-defined]
    return {
        "bot_entry": position.entry_price,
        "current_price": current_price,
        "bot_pnl_atr": pnl_atr,
        "bot_effective_stop": getattr(position, "effective_stop", None),
        "bot_stop_type": getattr(position, "stop_type", None),
        "bot_trail_status": "active" if bool(getattr(position, "trailing_active", False)) else "inactive",
    }
