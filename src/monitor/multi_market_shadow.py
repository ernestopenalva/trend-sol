from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from src.exchange.binance_market_data import BinanceMarketDataClient
from src.logging_utils import JsonlLogger, now_iso
from src.monitor.entry_engine import EntryEngine, EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter
from src.trade_ledger import TradeLedger


HOUR_MS = 3_600_000


@dataclass(frozen=True)
class SelectedMarket:
    symbol: str
    base_asset: str
    rank: int
    change_24h_pct: float
    change_7d_pct: float
    quote_volume_24h: float
    spread_bps: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "base_asset": self.base_asset,
            "rank": self.rank,
            "change_24h_pct": self.change_24h_pct,
            "change_7d_pct": self.change_7d_pct,
            "quote_volume_24h": self.quote_volume_24h,
            "spread_bps": self.spread_bps,
        }


class ShadowMarketSelector:
    def __init__(
        self,
        client: BinanceMarketDataClient,
        settings: Dict[str, Any],
    ) -> None:
        self.client = client
        self.settings = settings

    def select(self) -> list[SelectedMarket]:
        excluded = {
            str(value).upper()
            for value in self.settings.get("excluded_base_assets", [])
        }
        info = self.client.exchange_info()
        eligible = {
            str(item.get("symbol")): str(item.get("baseAsset"))
            for item in info.get("symbols", [])
            if isinstance(item, dict)
            and item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
            and _eligible_base_asset(str(item.get("baseAsset") or ""), excluded)
        }
        tickers = {
            str(item.get("symbol")): item
            for item in self.client.tickers_24h()
            if str(item.get("symbol")) in eligible
        }
        min_volume = float(self.settings.get("min_quote_volume_usdt", 10_000_000))
        max_universe = int(self.settings.get("max_universe_symbols", 50))
        liquid = sorted(
            (
                symbol
                for symbol in eligible
                if _number(tickers.get(symbol, {}).get("quoteVolume")) >= min_volume
            ),
            key=lambda symbol: _number(tickers[symbol].get("quoteVolume")),
            reverse=True,
        )[:max_universe]
        rolling = {
            str(item.get("symbol")): item
            for item in self.client.rolling_tickers(liquid, "7d")
        }
        books = {
            str(item.get("symbol")): item
            for item in self.client.book_tickers()
            if str(item.get("symbol")) in liquid
        }
        max_spread = float(self.settings.get("max_spread_bps", 10))
        require_positive_24h = bool(self.settings.get("require_positive_24h", True))
        require_positive_7d = bool(self.settings.get("require_positive_7d", True))
        candidates = []
        for symbol in liquid:
            change_24h = _number(tickers[symbol].get("priceChangePercent"))
            change_7d = _number(rolling.get(symbol, {}).get("priceChangePercent"))
            spread = _spread_bps(books.get(symbol, {}))
            if spread is None or spread > max_spread:
                continue
            if require_positive_24h and change_24h <= 0:
                continue
            if require_positive_7d and change_7d <= 0:
                continue
            candidates.append(
                (
                    symbol,
                    eligible[symbol],
                    change_24h,
                    change_7d,
                    _number(tickers[symbol].get("quoteVolume")),
                    spread,
                )
            )
        candidates.sort(key=lambda item: (-item[2], -item[3], -item[4], item[0]))
        top_count = int(self.settings.get("top_count", 3))
        return [
            SelectedMarket(
                symbol=item[0],
                base_asset=item[1],
                rank=index,
                change_24h_pct=item[2],
                change_7d_pct=item[3],
                quote_volume_24h=item[4],
                spread_bps=item[5],
            )
            for index, item in enumerate(candidates[:top_count], start=1)
        ]


class ShadowLogger:
    def __init__(
        self,
        symbol: str,
        telemetry: TelemetryWriter,
        system_logger: JsonlLogger,
    ) -> None:
        self.symbol = symbol
        self.telemetry = telemetry
        self.system_logger = system_logger

    def decision(self, event: Dict[str, Any]) -> None:
        self._submit("GATE", event)

    def trade(self, event: Dict[str, Any]) -> None:
        self._submit("TRADE", event)

    def system(self, event: str, **fields: Any) -> None:
        self._submit("SYSTEM", {"event": event, **fields})

    def _submit(self, event_type: str, event: Dict[str, Any]) -> None:
        payload = {
            "ts": event.get("ts") or now_iso(),
            "event_type": event_type,
            "shadow_kind": "TOP3_MARKET",
            "symbol": self.symbol,
            **event,
        }
        if not self.telemetry.submit("market_shadow_event", payload):
            self.system_logger.system(
                "market_shadow_telemetry_dropped",
                symbol=self.symbol,
                event_type=event_type,
            )


class MultiMarketShadow:
    def __init__(
        self,
        project_root: Path,
        config: Dict[str, Any],
        market_client: BinanceMarketDataClient,
        logger: JsonlLogger,
        telemetry: TelemetryWriter,
        on_streams_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        instrumentation = (
            config.get("instrumentation")
            if isinstance(config.get("instrumentation"), dict)
            else {}
        )
        settings = instrumentation.get("multi_market_shadow")
        self.settings = settings if isinstance(settings, dict) else {}
        self.enabled = bool(
            instrumentation.get("enabled", False)
            and self.settings.get("enabled", False)
        )
        self.project_root = project_root
        self.config = config
        self.market_client = market_client
        self.logger = logger
        self.telemetry = telemetry
        self.on_streams_changed = on_streams_changed
        self.selector = ShadowMarketSelector(market_client, self.settings)
        self.state_path = project_root / str(
            self.settings.get(
                "state_file",
                "data/state/multi_market_shadow.json",
            )
        )
        ledger_path = project_root / str(
            self.settings.get(
                "ledger_file",
                "data/trades/trades_shadow_top3.jsonl",
            )
        )
        self.ledger = TradeLedger(project_root, ledger_path)
        self.selected: Dict[str, SelectedMarket] = {}
        self.engines: Dict[str, EntryEngine] = {}
        self.positions: Dict[str, list[BotFullExitPosition]] = {}
        self.clients: Dict[str, PhantomExecutionClient] = {}
        self.entries_by_candle: Dict[str, Dict[int, int]] = {}
        self.epoch_entries: Dict[str, int] = {}
        self.quarantined_until_ms: Dict[str, int] = {}
        self.pending_hard_stop_recheck: set[str] = set()
        self.last_prices: Dict[str, float] = {}
        self.epoch_started_ms: Optional[int] = None
        self.last_scheduled_boundary_ms: Optional[int] = None
        self.last_clock_boundary_ms: Optional[int] = None
        self.next_position_id = 1

    def start(self) -> None:
        if not self.enabled:
            return
        self._load_state()
        now_ms = _now_ms()
        reset_epoch = (
            self.epoch_started_ms is None
            or _selection_epoch(self.epoch_started_ms, self._reevaluate_hours())
            != _selection_epoch(now_ms, self._reevaluate_hours())
        )
        self._evaluate_selection("STARTUP", now_ms, reset_epoch=reset_epoch)
        self.logger.system(
            "multi_market_shadow_started",
            selected=sorted(self.selected),
            open_positions=self.open_position_count,
            streams=self.required_streams(),
        )

    def stop(self) -> None:
        if self.enabled:
            self.save_state()

    @property
    def open_position_count(self) -> int:
        return sum(
            1
            for positions in self.positions.values()
            for position in positions
            if position.status == "OPEN"
        )

    def required_streams(self) -> list[str]:
        if not self.enabled:
            return []
        entry_timeframe = str(self.config["entry"]["timeframe"])
        trend_timeframe = str(self.config["trend"]["timeframe"])
        open_symbols = {
            symbol
            for symbol, positions in self.positions.items()
            if any(position.status == "OPEN" for position in positions)
        }
        symbols = sorted(set(self.selected) | open_symbols)
        streams = []
        for symbol in symbols:
            lower = symbol.lower()
            streams.append(f"{lower}@aggTrade")
            if symbol in self.selected:
                streams.extend(
                    [
                        f"{lower}@kline_{entry_timeframe}",
                        f"{lower}@kline_{trend_timeframe}",
                    ]
                )
        return streams

    def on_ws_event(self, stream: str, payload: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self._dispatch_ws_event(stream, payload)
        except Exception as exc:
            self.logger.system(
                "multi_market_shadow_event_failed",
                stream=stream,
                error=str(exc),
            )

    def _dispatch_ws_event(self, stream: str, payload: Dict[str, Any]) -> None:
        symbol = _stream_symbol(stream)
        if not symbol:
            return
        if stream.endswith("@aggTrade"):
            self._on_tick(symbol, float(payload["p"]), _market_timestamp(payload))
            return
        if "@kline_" not in stream:
            return
        kline = payload.get("k") or {}
        if not bool(kline.get("x")):
            return
        boundary_ms = int(kline.get("T", 0)) + 1
        self._maybe_reevaluate(boundary_ms)
        if symbol not in self.selected or self._is_quarantined(symbol, boundary_ms):
            return
        engine = self.engines.get(symbol)
        if engine is None:
            return
        signal = engine.on_kline(stream, payload)
        if signal is not None:
            self._admit_signal(symbol, signal, boundary_ms)

    def save_state(self) -> None:
        if not self.enabled:
            return
        data = {
            "updated_at": now_iso(),
            "selected": [item.to_dict() for item in self.selected.values()],
            "epoch_started_ms": self.epoch_started_ms,
            "last_scheduled_boundary_ms": self.last_scheduled_boundary_ms,
            "epoch_entries": self.epoch_entries,
            "quarantined_until_ms": self.quarantined_until_ms,
            "pending_hard_stop_recheck": sorted(self.pending_hard_stop_recheck),
            "next_position_id": self.next_position_id,
            "positions": [
                self._position_state(position)
                for positions in self.positions.values()
                for position in positions
                if position.status == "OPEN"
            ],
        }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    @staticmethod
    def _position_state(position: BotFullExitPosition) -> Dict[str, Any]:
        state = position.to_state()
        for field in (
            "shadow_kind",
            "shadow_selection_epoch_ms",
            "shadow_selection_rank",
            "shadow_selection_snapshot",
        ):
            state[field] = getattr(position, field, None)
        return state

    def _evaluate_selection(
        self,
        reason: str,
        boundary_ms: int,
        reset_epoch: bool,
    ) -> None:
        try:
            selected = self.selector.select()
        except Exception as exc:
            self.logger.system(
                "multi_market_shadow_selection_failed",
                reason=reason,
                error=str(exc),
            )
            return
        previous = set(self.selected)
        self.selected = {item.symbol: item for item in selected}
        if reset_epoch:
            self.epoch_started_ms = boundary_ms
            self.epoch_entries = {symbol: 0 for symbol in self.selected}
            self.last_scheduled_boundary_ms = boundary_ms
        for symbol in self.selected:
            if symbol not in self.engines or symbol not in previous:
                try:
                    self._load_engine(symbol)
                except Exception as exc:
                    self.logger.system(
                        "multi_market_shadow_history_failed",
                        symbol=symbol,
                        reason=reason,
                        error=str(exc),
                    )
        self.selected = {
            symbol: market
            for symbol, market in self.selected.items()
            if symbol in self.engines
        }
        self.epoch_entries = {
            symbol: self.epoch_entries.get(symbol, 0)
            for symbol in self.selected
        }
        for symbol in list(self.engines):
            if symbol not in self.selected:
                del self.engines[symbol]
        for symbol in list(self.pending_hard_stop_recheck):
            until = self.quarantined_until_ms.get(symbol, 0)
            if boundary_ms < until:
                continue
            self.pending_hard_stop_recheck.discard(symbol)
            if symbol in self.selected:
                self.quarantined_until_ms.pop(symbol, None)
        self._submit_event(
            "SELECTION",
            {
                "reason": reason,
                "boundary_ms": boundary_ms,
                "reset_epoch": reset_epoch,
                "selected": [item.to_dict() for item in self.selected.values()],
                "previous_symbols": sorted(previous),
            },
        )
        self.save_state()
        if self.on_streams_changed:
            self.on_streams_changed()

    def _load_engine(self, symbol: str) -> None:
        shadow_logger = ShadowLogger(symbol, self.telemetry, self.logger)
        engine = EntryEngine(symbol, self.config, shadow_logger)  # type: ignore[arg-type]
        limits = self.config.get("market_data", {}).get("historical_klines_limit", {})
        now_ms = _now_ms()
        for timeframe in (
            str(self.config["trend"]["timeframe"]),
            str(self.config["entry"]["timeframe"]),
        ):
            klines = self.market_client.klines(
                symbol,
                timeframe,
                int(limits.get(timeframe, 300)),
            )
            engine.load_history(timeframe, klines, now_ms)
        self.engines[symbol] = engine

    def _maybe_reevaluate(self, boundary_ms: int) -> None:
        if boundary_ms <= 0 or boundary_ms == self.last_clock_boundary_ms:
            return
        self.last_clock_boundary_ms = boundary_ms
        reevaluate_hours = self._reevaluate_hours()
        boundary = datetime.fromtimestamp(boundary_ms / 1000, timezone.utc)
        scheduled = (
            boundary.minute == 0
            and boundary.hour % reevaluate_hours == 0
            and (
                self.last_scheduled_boundary_ms is None
                or boundary_ms > self.last_scheduled_boundary_ms
            )
        )
        due_hard_stop = any(
            boundary.minute == 0
            and boundary_ms >= self.quarantined_until_ms.get(symbol, math.inf)
            for symbol in self.pending_hard_stop_recheck
        )
        if scheduled:
            self._evaluate_selection("SCHEDULED", boundary_ms, reset_epoch=True)
        elif due_hard_stop:
            self._evaluate_selection("HARD_STOP_RECHECK", boundary_ms, reset_epoch=False)

    def _admit_signal(
        self,
        symbol: str,
        signal: EntrySignal,
        boundary_ms: int,
    ) -> None:
        positions = [
            item
            for item in self.positions.get(symbol, [])
            if item.status == "OPEN"
        ]
        max_open = int(self.settings.get("max_open_positions_per_symbol", 5))
        epoch_cap = int(self.settings.get("max_entries_per_selection_epoch", 5))
        max_per_candle = int(self.config.get("entry", {}).get("max_entries_per_candle", 1))
        reason = None
        if len(positions) >= max_open:
            reason = "BLOCKED_MAX_POSITIONS"
        elif self.epoch_entries.get(symbol, 0) >= epoch_cap:
            reason = "BLOCKED_EPOCH_ENTRY_CAP"
        elif self.entries_by_candle.setdefault(symbol, {}).get(
            signal.source_candle_open_time,
            0,
        ) >= max_per_candle:
            reason = "BLOCKED_CANDLE_LIMIT"
        elif not self._passes_spacing(signal, positions):
            reason = "BLOCKED_SPACING"
        if reason:
            self._submit_event(
                "ADMISSION_BLOCKED",
                {
                    "symbol": symbol,
                    "reason": reason,
                    "price": signal.price,
                    "entry_atr": signal.entry_atr,
                    "open_positions": len(positions),
                    "epoch_entries": self.epoch_entries.get(symbol, 0),
                },
            )
            return

        notional = (
            float(self.config["capital"]["operational_balance_usdt"])
            * float(self.config["capital"]["trade_size_pct"])
            / 100
        )
        quantity = notional / float(signal.price)
        client = self.clients.setdefault(symbol, PhantomExecutionClient())
        position = BotFullExitPosition(
            pair_id=f"ms-{uuid.uuid4().hex[:12]}",
            symbol=symbol,
            entry_price=float(signal.price),
            quantity=quantity,
            entry_order={"market_shadow": True},
            open_ts=signal.ts,
            config=_bot_exit_config(self.config),
            client=client,  # type: ignore[arg-type]
            logger=ShadowLogger(symbol, self.telemetry, self.logger),  # type: ignore[arg-type]
            entry_atr=signal.entry_atr,
            atr_timeframe=signal.atr_timeframe,
            atr_period=signal.atr_period,
            position_id=self.next_position_id,
            source_candle_open_time=signal.source_candle_open_time,
            position_notional_usdt=notional,
        )
        position.phantom = True
        position.phantom_id = position.pair_id
        position.shadow_kind = "TOP3_MARKET"
        position.shadow_selection_epoch_ms = self.epoch_started_ms
        position.shadow_selection_rank = self.selected[symbol].rank
        position.shadow_selection_snapshot = self.selected[symbol].to_dict()
        self.next_position_id += 1
        self.positions.setdefault(symbol, []).append(position)
        self.entries_by_candle[symbol][signal.source_candle_open_time] = (
            self.entries_by_candle[symbol].get(signal.source_candle_open_time, 0) + 1
        )
        self.epoch_entries[symbol] = self.epoch_entries.get(symbol, 0) + 1
        ShadowLogger(symbol, self.telemetry, self.logger).trade(
            position._trade_event(
                "OPEN",
                position.entry_price,
                0.0,
                None,
                price_source="shadow_signal_close",
            )
        )
        self._submit_event(
            "POSITION_OPENED",
            {
                "symbol": symbol,
                "pair_id": position.pair_id,
                "position_id": position.position_id,
                "selection_rank": position.shadow_selection_rank,
                "entry_price": position.entry_price,
                "entry_atr": position.entry_atr,
                "epoch_entries": self.epoch_entries[symbol],
                "open_positions": len(positions) + 1,
            },
        )
        self.save_state()

    def _on_tick(self, symbol: str, price: float, market_ts: str) -> None:
        self.last_prices[symbol] = price
        client = self.clients.setdefault(symbol, PhantomExecutionClient())
        client.set_price(price)
        changed = False
        for position in list(self.positions.get(symbol, [])):
            if position.status != "OPEN":
                continue
            event = position.on_tick(price, market_ts)
            if event is None:
                continue
            changed = True
            self.ledger.append_closed_market_shadow_trade(position, self.config)
            self._submit_event(
                "POSITION_CLOSED",
                {
                    "symbol": symbol,
                    "pair_id": position.pair_id,
                    "position_id": position.position_id,
                    "exit_reason": position.exit_reason,
                    "entry_price": position.entry_price,
                    "exit_price": position.exit_price,
                    "gross_pnl_pct": position.pnl_pct(float(position.exit_price)),
                    "selection_rank": getattr(position, "shadow_selection_rank", None),
                },
            )
            if position.exit_reason == "HARD_STOP":
                self._quarantine_after_hard_stop(symbol, market_ts)
        if changed:
            self.positions[symbol] = [
                item
                for item in self.positions.get(symbol, [])
                if item.status == "OPEN"
            ]
            self.save_state()

    def _quarantine_after_hard_stop(self, symbol: str, market_ts: str) -> None:
        timestamp = _parse_timestamp_ms(market_ts)
        next_hour = ((timestamp // HOUR_MS) + 1) * HOUR_MS
        self.quarantined_until_ms[symbol] = max(
            next_hour,
            self.quarantined_until_ms.get(symbol, 0),
        )
        self.pending_hard_stop_recheck.add(symbol)
        self._submit_event(
            "HARD_STOP_QUARANTINE",
            {
                "symbol": symbol,
                "market_ts": market_ts,
                "quarantined_until_ms": self.quarantined_until_ms[symbol],
            },
        )

    def _is_quarantined(self, symbol: str, boundary_ms: int) -> bool:
        return (
            symbol in self.pending_hard_stop_recheck
            or boundary_ms < self.quarantined_until_ms.get(symbol, 0)
        )

    def _reevaluate_hours(self) -> int:
        return int(self.settings.get("reevaluate_hours", 4))

    def _passes_spacing(
        self,
        signal: EntrySignal,
        positions: Sequence[BotFullExitPosition],
    ) -> bool:
        spacing_atr = float(self.config.get("entry", {}).get("entry_spacing_atr", 0))
        if spacing_atr <= 0:
            return True
        if signal.entry_atr is None or signal.entry_atr <= 0:
            return False
        minimum = spacing_atr * signal.entry_atr
        return all(
            abs(float(signal.price) - float(position.entry_price)) >= minimum
            for position in positions
        )

    def _submit_event(self, event: str, fields: Dict[str, Any]) -> None:
        self.telemetry.submit(
            "market_shadow_event",
            {
                "ts": now_iso(),
                "event_type": event,
                "shadow_kind": "TOP3_MARKET",
                **fields,
            },
        )

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self.epoch_started_ms = _optional_int(data.get("epoch_started_ms"))
        self.last_scheduled_boundary_ms = _optional_int(
            data.get("last_scheduled_boundary_ms")
        )
        self.epoch_entries = {
            str(key): int(value)
            for key, value in (data.get("epoch_entries") or {}).items()
        }
        self.quarantined_until_ms = {
            str(key): int(value)
            for key, value in (data.get("quarantined_until_ms") or {}).items()
        }
        self.pending_hard_stop_recheck = {
            str(value) for value in data.get("pending_hard_stop_recheck", [])
        }
        self.next_position_id = int(data.get("next_position_id", 1))
        for state in data.get("positions", []):
            if not isinstance(state, dict) or state.get("status") != "OPEN":
                continue
            symbol = str(state.get("symbol") or "")
            if not symbol:
                continue
            client = self.clients.setdefault(symbol, PhantomExecutionClient())
            position = BotFullExitPosition.from_state(
                state,
                _bot_exit_config(self.config),
                client,  # type: ignore[arg-type]
                ShadowLogger(symbol, self.telemetry, self.logger),  # type: ignore[arg-type]
            )
            position.phantom = True
            position.phantom_id = str(state.get("phantom_id") or position.pair_id)
            for field in (
                "shadow_kind",
                "shadow_selection_epoch_ms",
                "shadow_selection_rank",
                "shadow_selection_snapshot",
            ):
                if field in state:
                    setattr(position, field, state[field])
            self.positions.setdefault(symbol, []).append(position)


def _bot_exit_config(config: Dict[str, Any]) -> Dict[str, Any]:
    output = dict(config.get("risk") or config["exit_bot_full_engine"])
    output["fees"] = config.get("fees", {})
    output["ladder"] = config.get("ladder", {})
    return output


def _eligible_base_asset(value: str, excluded: set[str]) -> bool:
    upper = value.upper()
    return bool(
        upper
        and upper not in excluded
        and not upper.endswith(("UP", "DOWN", "BULL", "BEAR"))
    )


def _spread_bps(value: Dict[str, Any]) -> Optional[float]:
    bid = _optional_number(value.get("bidPrice"))
    ask = _optional_number(value.get("askPrice"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    midpoint = (bid + ask) / 2
    return (ask - bid) / midpoint * 10_000 if midpoint > 0 else None


def _number(value: Any) -> float:
    parsed = _optional_number(value)
    return parsed if parsed is not None else 0.0


def _optional_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _stream_symbol(stream: str) -> str:
    return stream.split("@", 1)[0].upper() if "@" in stream else ""


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _selection_epoch(timestamp_ms: int, hours: int) -> int:
    width = max(1, int(hours)) * HOUR_MS
    return timestamp_ms // width


def _market_timestamp(payload: Dict[str, Any]) -> str:
    raw = payload.get("T", payload.get("E"))
    try:
        return datetime.fromtimestamp(
            float(raw) / 1000,
            timezone.utc,
        ).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OSError):
        return now_iso()


def _parse_timestamp_ms(value: str) -> int:
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return _now_ms()
