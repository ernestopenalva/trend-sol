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
        entry_atr: Optional[float] = None,
        atr_timeframe: Optional[str] = None,
        atr_period: Optional[int] = None,
        position_id: Optional[int] = None,
        source_candle_open_time: Optional[int] = None,
        position_notional_usdt: Optional[float] = None,
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
            position_id=position_id,
            source_candle_open_time=source_candle_open_time,
            position_notional_usdt=position_notional_usdt,
        )
        self.config = config
        self.client = client
        self.logger = logger
        self.entry_atr = _optional_float(entry_atr)
        self.atr_timeframe = atr_timeframe
        self.atr_period = int(atr_period) if atr_period is not None else None
        self.review_stop_pct = float(config.get("review_stop_pct", config.get("stop_loss_pct", 30)))
        self.review_stop = entry_price * (1 - self.review_stop_pct / 100)
        breakeven_cfg = config.get("breakeven", [])
        self.breakeven_mode = str(_section_value(breakeven_cfg, "mode", "pct")).lower()
        self.breakeven_trigger_pct = _optional_float(_section_value(breakeven_cfg, "trigger_pct"))
        self.breakeven_stop_to_pct = _optional_float(_section_value(breakeven_cfg, "stop_to_pct"))
        self.breakeven_trigger_atr = _optional_float(_section_value(breakeven_cfg, "trigger_atr"))
        self.breakeven_offset_atr = _optional_float(_section_value(breakeven_cfg, "offset_atr"))
        self.breakeven_stop: Optional[float] = None
        profit_lock_cfg = config.get("profit_lock", {})
        self.profit_lock_mode = str(
            profit_lock_cfg.get("mode", config.get("profit_lock_mode", "pct"))
        ).lower()
        self.profit_lock_pct_steps = sorted(
            profit_lock_cfg.get("steps_pct", config.get("profit_lock_pct", _legacy_breakeven_steps(breakeven_cfg))),
            key=lambda item: item["trigger_pct"],
        )
        self.profit_lock_atr_steps = sorted(
            profit_lock_cfg.get("steps", config.get("profit_lock_atr", [])),
            key=lambda item: item["trigger_atr"],
        )
        self.applied_steps: set[str] = set()
        self.profit_lock_stop: Optional[float] = None
        trailing_cfg = config.get("trailing", {})
        self.trailing_mode = str(trailing_cfg.get("mode", "pct")).lower()
        self.trailing_activation_pct = float(trailing_cfg.get("activation_pct", 10))
        self.trailing_gap_pct = float(trailing_cfg.get("gap_pct", 4))
        self.trailing_activation_atr = float(trailing_cfg.get("activation_atr", 5))
        self.trailing_gap_atr = float(trailing_cfg.get("gap_atr", 3))
        self.trailing_active = False
        self.trailing_stop: Optional[float] = None
        self.effective_stop = self.review_stop
        self.stop_price = self.effective_stop
        self.stop_type = "review"
        self.be_atr_stop: Optional[float] = None
        self.be_net_floor: Optional[float] = None
        self.be_stop: Optional[float] = None
        self.be_activation_price: Optional[float] = None
        self.be_activation_buffer_atr = self._be_activation_buffer_atr()
        self.be_floor_source: Optional[str] = None
        self.be_floor_absorbed_atr_stop: Optional[bool] = None
        self.exit_trigger_price: Optional[float] = None
        self.exit_trigger_price_source: Optional[str] = None
        self.exit_slippage_pct: Optional[float] = None

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
        position.entry_atr = _optional_float(state.get("entry_atr", position.entry_atr))
        position.atr_timeframe = state.get("atr_timeframe", position.atr_timeframe)
        position.atr_period = int(state["atr_period"]) if state.get("atr_period") is not None else position.atr_period
        position.profit_lock_mode = str(state.get("profit_lock_mode", position.profit_lock_mode)).lower()
        position.breakeven_mode = str(state.get("breakeven_mode", position.breakeven_mode)).lower()
        position.trailing_mode = str(state.get("trailing_mode", position.trailing_mode)).lower()
        position.review_stop = float(state.get("review_stop", state.get("review_stop_price", position.review_stop)))
        position.breakeven_stop = _optional_float(state.get("breakeven_stop", position.breakeven_stop))
        position.be_atr_stop = _optional_float(state.get("be_atr_stop", position.be_atr_stop))
        position.be_net_floor = _optional_float(state.get("be_net_floor", position.be_net_floor))
        position.be_stop = _optional_float(state.get("be_stop", position.be_stop))
        position.be_activation_price = _optional_float(state.get("be_activation_price", position.be_activation_price))
        position.be_activation_buffer_atr = _optional_float(
            state.get("be_activation_buffer_atr", position.be_activation_buffer_atr)
        ) or 0.0
        position.be_floor_source = state.get("be_floor_source", position.be_floor_source)
        position.be_floor_absorbed_atr_stop = state.get(
            "be_floor_absorbed_atr_stop", position.be_floor_absorbed_atr_stop
        )
        position.profit_lock_stop = _optional_float(state.get("profit_lock_stop", position.profit_lock_stop))
        position.trailing_stop = _optional_float(state.get("trailing_stop", position.trailing_stop))
        position.effective_stop = float(state.get("effective_stop", state.get("stop_price", position.effective_stop)))
        position.stop_price = position.effective_stop
        position.stop_type = str(state.get("stop_type", position.stop_type))
        position.applied_steps = {str(item) for item in state.get("applied_steps", [])}
        position.trailing_active = bool(state.get("trailing_active", False))
        return position

    def on_tick(self, price: float) -> Optional[Dict[str, Any]]:
        if self.status != "OPEN":
            return None

        ts = now_iso()
        if price > self.highest_price:
            self.highest_price = price

        if self._atr_required() and not self._valid_entry_atr():
            self.status = "NEEDS_REVIEW"
            self.logger.system(
                "position_needs_review",
                pair_id=self.pair_id,
                position=self.label,
                reason="missing_entry_atr_for_atr_exit",
                profit_lock_mode=self.profit_lock_mode,
                trailing_mode=self.trailing_mode,
            )
            return None

        pnl_pct = self.pnl_pct(price)
        pnl_atr = self.pnl_atr(price)
        self._apply_breakeven(pnl_pct, pnl_atr, price)
        for index, new_stop in self._profit_lock_candidates(pnl_pct, pnl_atr):
            step_key = f"{self.profit_lock_mode}:{index}"
            if step_key not in self.applied_steps:
                if self.profit_lock_stop is None or new_stop > self.profit_lock_stop:
                    self.profit_lock_stop = new_stop
                self.applied_steps.add(step_key)
                self._refresh_effective_stop()
                self.logger.trade(
                    self._trade_event(
                        event=f"PROFIT_LOCK_{self.profit_lock_mode.upper()}_{index}",
                        price=price,
                        pnl_pct=pnl_pct,
                        exit_reason=None,
                    )
                )

        if self._should_activate_trailing(pnl_pct, pnl_atr) and not self.trailing_active:
            self.trailing_active = True
            self._update_trailing_stop()
            self._refresh_effective_stop()
            self.logger.trade(
                self._trade_event(
                    event="TRAILING_ACTIVATED",
                    price=price,
                    pnl_pct=pnl_pct,
                    exit_reason=None,
                )
            )

        if self.trailing_active:
            self._update_trailing_stop()

        self._refresh_effective_stop()

        reason = None
        if price <= self.effective_stop:
            reason = {
                "review": "REVIEW_STOP",
                "breakeven": "BREAKEVEN",
                "profit_lock": "PROFIT_LOCK",
                "trailing": "TRAILING",
            }.get(
                self.stop_type,
                "REVIEW_STOP",
            )

        if reason is None:
            return None

        client_order_id = f"ts-{self.pair_id}-B-close"
        self.validate_sell_quantity(self.reserved_qty)
        trigger_price = price
        trigger_stop = self.effective_stop
        order = self.client.market_sell(self.symbol, self.reserved_qty, client_order_id)
        executed_price = _average_fill_price(order) or price
        self.exit_trigger_price = trigger_price
        self.exit_trigger_price_source = "aggTrade"
        self.exit_slippage_pct = _slippage_pct(executed_price, trigger_stop)
        self.mark_closed(executed_price, reason, ts, order)
        event = self._trade_event(
            "CLOSE",
            executed_price,
            self.pnl_pct(executed_price),
            reason,
            order,
            price_source="market_fill",
            trigger_price=trigger_price,
            trigger_price_source="aggTrade",
        )
        self.logger.trade(event)
        return event

    def _trade_event(
        self,
        event: str,
        price: float,
        pnl_pct: float,
        exit_reason: Optional[str],
        order: Optional[Dict[str, Any]] = None,
        price_source: str = "aggTrade",
        trigger_price: Optional[float] = None,
        trigger_price_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        order = order or {}
        estimated_fees_pct = self._estimated_round_trip_fees_pct()
        stop_hit = self.effective_stop if event == "CLOSE" else None
        return {
            "ts": now_iso(),
            "pair_id": self.pair_id,
            "position_id": self.position_id,
            "position": self.label,
            "position_notional_usdt": self.position_notional_usdt,
            "engine": self.engine,
            "event": event,
            "price": price,
            "price_source": price_source,
            "trigger_price": trigger_price,
            "trigger_price_source": trigger_price_source,
            "pnl_pct": pnl_pct,
            "gross_pnl_pct": pnl_pct,
            "estimated_fees_pct": estimated_fees_pct,
            "net_pnl_pct": pnl_pct - estimated_fees_pct,
            "pnl_atr": self.pnl_atr(price),
            "entry_atr": self.entry_atr,
            "breakeven_mode": self.breakeven_mode,
            "profit_lock_mode": self.profit_lock_mode,
            "trailing_mode": self.trailing_mode,
            "breakeven_stop": self.breakeven_stop,
            "be_atr_stop": self.be_atr_stop,
            "be_net_floor": self.be_net_floor,
            "be_stop": self.be_stop,
            "be_activation_price": self.be_activation_price,
            "be_activation_buffer_atr": self.be_activation_buffer_atr,
            "be_floor_source": self.be_floor_source,
            "be_floor_absorbed_atr_stop": self.be_floor_absorbed_atr_stop,
            "profit_lock_stop": self.profit_lock_stop,
            "trailing_stop": self.trailing_stop,
            "effective_stop": self.effective_stop,
            "stop_hit": stop_hit,
            "exit_slippage_pct": self.exit_slippage_pct if event == "CLOSE" else None,
            "stop_type": self.stop_type,
            "exit_reason": exit_reason,
            "order_id": order.get("orderId"),
            "client_order_id": order.get("clientOrderId"),
            "executed_qty": _float_or_zero(order.get("executedQty")),
            "cummulative_quote_qty": _float_or_zero(order.get("cummulativeQuoteQty")),
            "commission": _commission(order),
        }

    def pnl_atr(self, price: float) -> Optional[float]:
        if not self._valid_entry_atr():
            return None
        return (price - self.entry_price) / float(self.entry_atr)

    def peak_atr(self) -> Optional[float]:
        if not self._valid_entry_atr():
            return None
        return (self.highest_price - self.entry_price) / float(self.entry_atr)

    def _profit_lock_candidates(self, pnl_pct: float, pnl_atr: Optional[float]) -> list[tuple[int, float]]:
        candidates: list[tuple[int, float]] = []
        if self.profit_lock_mode == "atr":
            if pnl_atr is None:
                return candidates
            for index, step in enumerate(self.profit_lock_atr_steps, start=1):
                if _gte(pnl_atr, float(step["trigger_atr"])):
                    candidates.append((index, self.entry_price + float(step["lock_atr"]) * float(self.entry_atr)))
            return candidates

        for index, step in enumerate(self.profit_lock_pct_steps, start=1):
            if _gte(pnl_pct, float(step["trigger_pct"])):
                candidates.append((index, self.entry_price * (1 + float(step["stop_to_pct"]) / 100)))
        return candidates

    def _apply_breakeven(self, pnl_pct: float, pnl_atr: Optional[float], price: float) -> None:
        plan = self._breakeven_plan()
        if plan is None:
            return
        new_stop = float(plan["be_stop"])
        trigger_hit = _gte(price, float(plan["be_activation_price"]))
        if not trigger_hit:
            return
        if self.breakeven_stop is None or new_stop > self.breakeven_stop:
            self.breakeven_stop = new_stop
            self.applied_steps.add(f"breakeven:{self.breakeven_mode}")
            self._refresh_effective_stop()
            self.logger.trade(
                self._trade_event(
                    event=f"BREAKEVEN_{self.breakeven_mode.upper()}",
                    price=price,
                    pnl_pct=pnl_pct,
                    exit_reason=None,
                )
            )

    def _should_activate_trailing(self, pnl_pct: float, pnl_atr: Optional[float]) -> bool:
        if self.trailing_mode == "atr":
            return pnl_atr is not None and _gte(pnl_atr, self.trailing_activation_atr)
        return _gte(pnl_pct, self.trailing_activation_pct)

    def _net_breakeven_floor(self) -> float:
        fees = self.config.get("fees") if isinstance(self.config.get("fees"), dict) else {}
        if not fees or not bool(fees.get("enabled", False)):
            return self.entry_price
        taker_fee_pct = _float_or_zero(fees.get("taker_fee_pct"))
        if bool(fees.get("use_bnb_discount", False)):
            taker_fee_pct *= 0.75
        ladder = self.config.get("ladder") if isinstance(self.config.get("ladder"), dict) else {}
        margin_pct = _float_or_zero(ladder.get("be_net_margin_pct"))
        return self.entry_price * (1 + (2 * taker_fee_pct / 100) + (margin_pct / 100))

    def _breakeven_plan(self) -> Optional[Dict[str, float | str | bool | None]]:
        if self.breakeven_mode == "atr":
            if self.breakeven_trigger_atr is None or self.breakeven_offset_atr is None:
                return None
            if not self._valid_entry_atr():
                return None
            be_atr_stop: Optional[float] = self.entry_price + self.breakeven_offset_atr * float(self.entry_atr)
            trigger_price = self.entry_price + self.breakeven_trigger_atr * float(self.entry_atr)
        elif self.breakeven_trigger_pct is not None and self.breakeven_stop_to_pct is not None:
            be_atr_stop = None
            trigger_price = self.entry_price * (1 + self.breakeven_trigger_pct / 100)
            pct_stop = self.entry_price * (1 + self.breakeven_stop_to_pct / 100)
        else:
            return None

        net_floor = self._net_breakeven_floor_or_none()
        base_stop = be_atr_stop if be_atr_stop is not None else pct_stop
        candidates = [base_stop]
        if net_floor is not None:
            candidates.append(net_floor)
        be_stop = max(candidates)
        floor_source = "NET_FLOOR" if net_floor is not None and net_floor >= base_stop else "ATR"
        absorbed = bool(net_floor is not None and net_floor > base_stop)
        buffer_abs = self.be_activation_buffer_atr * float(self.entry_atr) if self._valid_entry_atr() else 0.0
        activation_price = max(trigger_price, be_stop + buffer_abs)

        self.be_atr_stop = be_atr_stop
        self.be_net_floor = net_floor
        self.be_stop = be_stop
        self.be_activation_price = activation_price
        self.be_activation_buffer_atr = self._be_activation_buffer_atr()
        self.be_floor_source = floor_source
        self.be_floor_absorbed_atr_stop = absorbed
        return {
            "be_atr_stop": be_atr_stop,
            "be_net_floor": net_floor,
            "be_stop": be_stop,
            "be_activation_price": activation_price,
            "be_floor_source": floor_source,
            "be_floor_absorbed_atr_stop": absorbed,
        }

    def _net_breakeven_floor_or_none(self) -> Optional[float]:
        fees = self.config.get("fees") if isinstance(self.config.get("fees"), dict) else {}
        if not fees or not bool(fees.get("enabled", False)):
            return None
        return self._net_breakeven_floor()

    def _be_activation_buffer_atr(self) -> float:
        ladder = self.config.get("ladder") if isinstance(self.config.get("ladder"), dict) else {}
        return _float_or_zero(ladder.get("be_activation_buffer_atr"))

    def _estimated_round_trip_fees_pct(self) -> float:
        fees = self.config.get("fees") if isinstance(self.config.get("fees"), dict) else {}
        if not fees or not bool(fees.get("enabled", False)):
            return 0.0
        taker_fee_pct = _float_or_zero(fees.get("taker_fee_pct"))
        if bool(fees.get("use_bnb_discount", False)):
            taker_fee_pct *= 0.75
        return taker_fee_pct * 2

    def _current_trailing_stop(self) -> Optional[float]:
        if self.trailing_mode == "atr":
            if not self._valid_entry_atr():
                return None
            return self.highest_price - self.trailing_gap_atr * float(self.entry_atr)
        return self.highest_price * (1 - self.trailing_gap_pct / 100)

    def _update_trailing_stop(self) -> None:
        trailing_stop = self._current_trailing_stop()
        if trailing_stop is None:
            return
        if self.trailing_stop is None or trailing_stop > self.trailing_stop:
            self.trailing_stop = trailing_stop

    def _refresh_effective_stop(self) -> None:
        stops = [
            ("current", self.effective_stop),
            ("review", self.review_stop),
            ("breakeven", self.breakeven_stop),
            ("profit_lock", self.profit_lock_stop),
            ("trailing", self.trailing_stop),
        ]
        stop_type, self.effective_stop = max(
            ((name, value) for name, value in stops if value is not None),
            key=lambda item: float(item[1]),
        )
        if stop_type != "current":
            self.stop_type = stop_type
        self.stop_price = self.effective_stop

    def _atr_required(self) -> bool:
        return self.breakeven_mode == "atr" or self.profit_lock_mode == "atr" or self.trailing_mode == "atr"

    def _valid_entry_atr(self) -> bool:
        return self.entry_atr is not None and self.entry_atr > 0

    def to_state(self) -> Dict[str, Any]:
        state = super().to_state()
        state.update(
            {
                "stop_price": self.stop_price,
                "entry_atr": self.entry_atr,
                "atr_timeframe": self.atr_timeframe,
                "atr_period": self.atr_period,
                "breakeven_mode": self.breakeven_mode,
                "profit_lock_mode": self.profit_lock_mode,
                "trailing_mode": self.trailing_mode,
                "review_stop": self.review_stop,
                "breakeven_stop": self.breakeven_stop,
                "be_atr_stop": self.be_atr_stop,
                "be_net_floor": self.be_net_floor,
                "be_stop": self.be_stop,
                "be_activation_price": self.be_activation_price,
                "be_activation_buffer_atr": self.be_activation_buffer_atr,
                "be_floor_source": self.be_floor_source,
                "be_floor_absorbed_atr_stop": self.be_floor_absorbed_atr_stop,
                "profit_lock_stop": self.profit_lock_stop,
                "trailing_stop": self.trailing_stop,
                "effective_stop": self.effective_stop,
                "stop_type": self.stop_type,
                "applied_steps": sorted(self.applied_steps),
                "trailing_active": self.trailing_active,
                "trailing_activation_pct": self.trailing_activation_pct,
                "trailing_gap_pct": self.trailing_gap_pct,
                "trailing_activation_atr": self.trailing_activation_atr,
                "trailing_gap_atr": self.trailing_gap_atr,
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


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _section_value(section: Any, key: str, default: Any = None) -> Any:
    if isinstance(section, dict):
        return section.get(key, default)
    return default


def _legacy_breakeven_steps(section: Any) -> list[Dict[str, Any]]:
    if isinstance(section, list):
        return section
    return []


def _gte(left: float, right: float) -> bool:
    return left >= right or abs(left - right) <= 1e-9


def _commission(order: Dict[str, Any]) -> float:
    return sum(_float_or_zero(fill.get("commission")) for fill in order.get("fills", []) or [])


def _slippage_pct(exit_price: float, stop_hit: Optional[float]) -> Optional[float]:
    if stop_hit is None or stop_hit <= 0:
        return None
    return ((exit_price / stop_hit) - 1) * 100
