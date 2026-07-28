from __future__ import annotations

import argparse
import bisect
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.monitor.entry_engine import EntryEngine, EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from tools.cohort_study import _load_config
from tools.market_selection_study import (
    HOUR_MS,
    BinancePublicClient,
    CurrentMarket,
    MarketCandle,
    _floor_ms,
    _group_snapshots,
    build_candidate_snapshots,
    fetch_current_markets,
    load_candle_cache,
    load_universe_snapshot,
    merge_candles,
    missing_candle_ranges,
    save_candle_cache,
    save_universe_snapshot,
)


MINUTE_MS = 60_000
DAY_MS = 24 * HOUR_MS


@dataclass(frozen=True)
class SignalEvent:
    boundary_ms: int
    symbol: str
    signal: EntrySignal


@dataclass(frozen=True)
class ReplayPolicy:
    name: str
    top_count: int
    max_positions_per_symbol: int
    sol_only: bool = False


@dataclass
class OpenReplayPosition:
    position: BotFullExitPosition
    client: "ReplayExecutionClient"
    opened_ms: int
    notional_usdt: float


@dataclass(frozen=True)
class ReplayTrade:
    symbol: str
    opened_ms: int
    closed_ms: int
    entry_price: float
    exit_price: float
    gross_pct: float
    net_pct: float
    net_usdt: float
    exit_reason: str


@dataclass
class ReplayResult:
    policy: str
    intrabar_path: str
    trades: list[ReplayTrade]
    open_positions: list[OpenReplayPosition]
    signals: int
    eligible_signals: int
    blocked_slots: int
    blocked_symbol_cap: int
    blocked_candle_limit: int
    blocked_spacing: int
    full_slot_minutes: int
    observed_minutes: int
    open_mtm_usdt: float = 0.0

    @property
    def realized_net_usdt(self) -> float:
        return sum(item.net_usdt for item in self.trades)

    @property
    def combined_net_usdt(self) -> float:
        return self.realized_net_usdt + self.open_mtm_usdt


@dataclass(frozen=True)
class RearmReplay:
    quiet_minutes: int
    raw_signals: int
    rearmed_signals: int
    result: ReplayResult


class NullLogger:
    def decision(self, event: Dict[str, Any]) -> None:
        del event

    def trade(self, event: Dict[str, Any]) -> None:
        del event

    def system(self, event: str, **fields: Any) -> None:
        del event, fields


class ReplayExecutionClient:
    def __init__(self, exit_cost_bps: float) -> None:
        self.current_price = 0.0
        self.exit_cost_bps = exit_cost_bps

    def market_sell(
        self,
        symbol: str,
        quantity: float,
        client_order_id: str,
    ) -> Dict[str, Any]:
        executed_price = self.current_price * (1 - self.exit_cost_bps / 10_000)
        return {
            "orderId": f"replay-{client_order_id}",
            "clientOrderId": client_order_id,
            "symbol": symbol,
            "executedQty": str(quantity),
            "cummulativeQuoteQty": str(quantity * executed_price),
            "fills": [{"price": str(executed_price), "qty": str(quantity)}],
        }


class SelectionTimeline:
    def __init__(
        self,
        selections: Dict[int, Sequence[str]],
        ranks: Dict[int, Dict[str, int]],
    ) -> None:
        self.selections = {key: tuple(value) for key, value in selections.items()}
        self.ranks = ranks
        self.boundaries = sorted(selections)

    def selected(self, boundary_ms: int, top_count: int) -> tuple[str, ...]:
        decision = self._latest_boundary(boundary_ms)
        if decision is None:
            return ()
        return self.selections.get(decision, ())[:top_count]

    def rank(self, boundary_ms: int, symbol: str) -> int:
        decision = self._latest_boundary(boundary_ms)
        if decision is None:
            return 1_000_000
        return self.ranks.get(decision, {}).get(symbol, 1_000_000)

    def _latest_boundary(self, boundary_ms: int) -> Optional[int]:
        index = bisect.bisect_right(self.boundaries, boundary_ms) - 1
        return self.boundaries[index] if index >= 0 else None


def main() -> None:
    args = _parse_args()
    raw_config = _load_config(Path(args.config))
    config = effective_config(raw_config)
    study = _study_config(config)
    lookback_days = int(args.lookback_days or study.get("lookback_days", 30))
    warmup_days = int(study.get("warmup_days", 7))
    decision_hours = int(study.get("decision_interval_hours", 4))
    min_quote_volume = float(study.get("min_quote_volume_usdt", 10_000_000))
    top_count = int(study.get("top_count", 5))
    max_symbols = int(study.get("max_universe_symbols", 50))
    round_trip_spread_bps = float(study.get("round_trip_spread_bps", 5.0))
    max_entries_per_minute = int(study.get("max_entries_per_minute", 1))
    if lookback_days < 7 or warmup_days < 7:
        raise ValueError("market_bot_replay requires at least 7 replay and warmup days")
    if top_count < 1 or round_trip_spread_bps < 0 or max_entries_per_minute < 1:
        raise ValueError("invalid market_bot_replay settings")

    base_url = str(
        args.market_data_url
        or (config.get("market_data") or {}).get("rest_url")
        or "https://api.binance.com"
    )
    client = BinancePublicClient(base_url, int(args.http_timeout_seconds))
    cache_dir = Path(args.cache_dir)
    selection_cache_dir = Path(args.selection_cache_dir)
    markets = _load_or_fetch_universe(
        client,
        Path(args.universe_snapshot),
        config,
        min_quote_volume,
        max_symbols,
        args.offline,
    )
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end_ms = _floor_ms(now_ms, MINUTE_MS) - 1
    replay_start_ms = end_ms - lookback_days * DAY_MS + 1
    data_start_ms = replay_start_ms - warmup_days * DAY_MS

    print("TREND-SOL | selected-market full bot replay")
    print(
        f"Preparing {lookback_days}d replay + {warmup_days}d warmup | "
        f"universe={len(markets)} | Top {top_count}"
    )
    hourly = _load_market_data(
        client,
        markets,
        "1h",
        data_start_ms,
        end_ms,
        selection_cache_dir,
        args.offline,
    )
    timeline = build_selection_timeline(
        hourly,
        decision_hours,
        min_quote_volume,
        top_count,
        replay_start_ms,
        end_ms,
    )
    selected_symbols = sorted(
        {
            symbol
            for boundary in timeline.boundaries
            for symbol in timeline.selected(boundary, top_count)
        }
        | {"SOLUSDT"}
    )
    selected_markets = [item for item in markets if item.symbol in selected_symbols]
    print(
        f"Historical selector touched {len(selected_markets)} symbols: "
        + ", ".join(item.symbol for item in selected_markets)
    )
    trend_timeframe = str(config["trend"]["timeframe"])
    entry_timeframe = str(config["entry"]["timeframe"])
    execution_timeframe = str(args.execution_timeframe or entry_timeframe)
    execution = _load_market_data(
        client,
        selected_markets,
        execution_timeframe,
        data_start_ms,
        end_ms,
        cache_dir,
        args.offline,
    )
    entry = (
        execution
        if execution_timeframe == entry_timeframe
        else _load_market_data(
            client,
            selected_markets,
            entry_timeframe,
            data_start_ms,
            end_ms,
            cache_dir,
            args.offline,
        )
    )
    trend = _load_market_data(
        client,
        selected_markets,
        trend_timeframe,
        data_start_ms,
        end_ms,
        cache_dir,
        args.offline,
    )
    complete_symbols = set(execution) & set(entry) & set(trend)
    execution = {key: value for key, value in execution.items() if key in complete_symbols}
    entry = {key: value for key, value in entry.items() if key in complete_symbols}
    trend = {key: value for key, value in trend.items() if key in complete_symbols}
    signals = generate_pipeline_signals(
        config, entry, trend, replay_start_ms, end_ms
    )
    print(
        f"Pipeline approved {len(signals)} signals across "
        f"{len({item.symbol for item in signals})} symbols."
    )

    policies = [
        ReplayPolicy("SOL_MAX5", 0, 5, sol_only=True),
        ReplayPolicy("TOP5_ONE_EACH", top_count, 1),
        ReplayPolicy("TOP5_MAX2", top_count, 2),
    ]
    results = []
    for intrabar_path in ("LOW_FIRST", "HIGH_FIRST"):
        for policy in policies:
            results.append(
                run_replay(
                    config=config,
                    policy=policy,
                    intrabar_path=intrabar_path,
                    signals=signals,
                    candles_by_symbol=execution,
                    timeline=timeline,
                    replay_start_ms=replay_start_ms,
                    end_ms=end_ms,
                    round_trip_spread_bps=round_trip_spread_bps,
                    max_entries_per_minute=max_entries_per_minute,
                )
            )
    rearm_quiet_minutes = [
        int(value)
        for value in study.get("rearm_quiet_minutes", [1, 3, 5, 15])
    ]
    rearm_results = []
    for quiet_minutes in rearm_quiet_minutes:
        rearmed = filter_signals_by_quiet_period(signals, quiet_minutes)
        for intrabar_path in ("LOW_FIRST", "HIGH_FIRST"):
            for policy in (policies[0], policies[1]):
                rearm_results.append(
                    RearmReplay(
                        quiet_minutes=quiet_minutes,
                        raw_signals=len(signals),
                        rearmed_signals=len(rearmed),
                        result=run_replay(
                            config=config,
                            policy=policy,
                            intrabar_path=intrabar_path,
                            signals=rearmed,
                            candles_by_symbol=execution,
                            timeline=timeline,
                            replay_start_ms=replay_start_ms,
                            end_ms=end_ms,
                            round_trip_spread_bps=round_trip_spread_bps,
                            max_entries_per_minute=max_entries_per_minute,
                        ),
                    )
                )
    _print_report(
        results,
        config,
        lookback_days,
        decision_hours,
        min_quote_volume,
        round_trip_spread_bps,
        replay_start_ms,
        end_ms,
        entry_timeframe,
        execution_timeframe,
    )
    _print_signal_rearm_report(signals, results, rearm_results)


def build_selection_timeline(
    candles_by_symbol: Dict[str, Sequence[MarketCandle]],
    decision_interval_hours: int,
    min_quote_volume_usdt: float,
    top_count: int,
    start_ms: int,
    end_ms: int,
) -> SelectionTimeline:
    snapshots = build_candidate_snapshots(
        candles_by_symbol,
        decision_interval_hours,
        min_quote_volume_usdt,
        require_positive_24h=True,
        require_positive_7d=True,
    )
    grouped = _group_snapshots(snapshots)
    selections: Dict[int, Sequence[str]] = {}
    ranks: Dict[int, Dict[str, int]] = {}
    first = _floor_ms(start_ms, HOUR_MS)
    if datetime.fromtimestamp(first / 1000, tz=timezone.utc).hour % decision_interval_hours:
        first += HOUR_MS
    for boundary in range(first, end_ms + 1, HOUR_MS):
        hour = datetime.fromtimestamp(boundary / 1000, tz=timezone.utc).hour
        if hour % decision_interval_hours:
            continue
        candidates = sorted(
            grouped.get(boundary, []),
            key=lambda item: (-item.change_24h_pct, -item.quote_volume_24h, item.symbol),
        )
        selected = tuple(item.symbol for item in candidates[:top_count])
        selections[boundary] = selected
        ranks[boundary] = {symbol: index for index, symbol in enumerate(selected)}
    return SelectionTimeline(selections, ranks)


def generate_pipeline_signals(
    config: Dict[str, Any],
    minute_by_symbol: Dict[str, Sequence[MarketCandle]],
    trend_by_symbol: Dict[str, Sequence[MarketCandle]],
    replay_start_ms: int,
    end_ms: int,
) -> list[SignalEvent]:
    logger = NullLogger()
    output = []
    trend_timeframe = str(config["trend"]["timeframe"])
    entry_timeframe = str(config["entry"]["timeframe"])
    for symbol, entry_candles in minute_by_symbol.items():
        engine = EntryEngine(symbol, config, logger)  # type: ignore[arg-type]
        trend_candles = trend_by_symbol.get(symbol, ())
        trend_index = 0
        for candle in entry_candles:
            boundary = candle.boundary_ms
            if boundary > end_ms:
                break
            while (
                trend_index < len(trend_candles)
                and trend_candles[trend_index].boundary_ms <= boundary
            ):
                trend_candle = trend_candles[trend_index]
                engine.on_kline(
                    f"{symbol.lower()}@kline_{trend_timeframe}",
                    _kline_payload(trend_candle),
                )
                trend_index += 1
            signal = engine.on_kline(
                f"{symbol.lower()}@kline_{entry_timeframe}",
                _kline_payload(candle),
            )
            if signal is not None and replay_start_ms <= boundary <= end_ms:
                output.append(SignalEvent(boundary, symbol, signal))
    return sorted(output, key=lambda item: (item.boundary_ms, item.symbol))


def filter_signals_by_quiet_period(
    signals: Sequence[SignalEvent],
    quiet_minutes: int,
) -> list[SignalEvent]:
    if quiet_minutes < 1:
        raise ValueError("quiet_minutes must be at least 1")
    output = []
    last_raw_by_symbol: Dict[str, int] = {}
    for signal in sorted(signals, key=lambda item: (item.boundary_ms, item.symbol)):
        previous = last_raw_by_symbol.get(signal.symbol)
        missing_candles = (
            math.inf
            if previous is None
            else max(0, (signal.boundary_ms - previous) // MINUTE_MS - 1)
        )
        if missing_candles >= quiet_minutes:
            output.append(signal)
        last_raw_by_symbol[signal.symbol] = signal.boundary_ms
    return output


def signal_repetition_stats(signals: Sequence[SignalEvent]) -> Dict[str, float]:
    gaps = []
    by_symbol: Dict[str, list[int]] = {}
    for signal in signals:
        by_symbol.setdefault(signal.symbol, []).append(signal.boundary_ms)
    for boundaries in by_symbol.values():
        ordered = sorted(boundaries)
        gaps.extend(
            (current - previous) / MINUTE_MS
            for previous, current in zip(ordered, ordered[1:])
        )
    return {
        "comparisons": float(len(gaps)),
        "consecutive": float(sum(gap == 1 for gap in gaps)),
        "within_5m": float(sum(gap <= 5 for gap in gaps)),
        "within_15m": float(sum(gap <= 15 for gap in gaps)),
        "median_gap_minutes": statistics.median(gaps) if gaps else 0.0,
    }


def run_replay(
    *,
    config: Dict[str, Any],
    policy: ReplayPolicy,
    intrabar_path: str,
    signals: Sequence[SignalEvent],
    candles_by_symbol: Dict[str, Sequence[MarketCandle]],
    timeline: SelectionTimeline,
    replay_start_ms: int,
    end_ms: int,
    round_trip_spread_bps: float,
    max_entries_per_minute: int,
) -> ReplayResult:
    if intrabar_path not in {"LOW_FIRST", "HIGH_FIRST"}:
        raise ValueError("intrabar_path must be LOW_FIRST or HIGH_FIRST")
    signal_groups: Dict[int, list[SignalEvent]] = {}
    for item in signals:
        signal_groups.setdefault(item.boundary_ms, []).append(item)
    candle_indexes = {
        symbol: {item.boundary_ms: item for item in candles}
        for symbol, candles in candles_by_symbol.items()
    }
    result = ReplayResult(
        policy=policy.name,
        intrabar_path=intrabar_path,
        trades=[],
        open_positions=[],
        signals=len(signals),
        eligible_signals=0,
        blocked_slots=0,
        blocked_symbol_cap=0,
        blocked_candle_limit=0,
        blocked_spacing=0,
        full_slot_minutes=0,
        observed_minutes=0,
    )
    open_positions: list[OpenReplayPosition] = []
    max_positions = int(config["capital"]["max_open_positions"])
    notional = (
        float(config["capital"]["operational_balance_usdt"])
        * float(config["capital"]["trade_size_pct"])
        / 100
    )
    entry_cost_bps = round_trip_spread_bps / 2
    exit_cost_bps = round_trip_spread_bps / 2
    fees_pct = _round_trip_fees_pct(config)
    bot_exit_config = _bot_exit_config(config)
    logger = NullLogger()
    position_sequence = 0

    for boundary in range(
        _floor_ms(replay_start_ms, MINUTE_MS) + MINUTE_MS,
        end_ms + 1,
        MINUTE_MS,
    ):
        result.observed_minutes += 1
        for symbol in {item.position.symbol for item in open_positions}:
            candle = candle_indexes.get(symbol, {}).get(boundary)
            if candle is None:
                continue
            _process_candle(
                open_positions,
                result.trades,
                symbol,
                candle,
                intrabar_path,
                fees_pct,
            )
        open_positions[:] = [
            item for item in open_positions if item.position.status == "OPEN"
        ]
        if len(open_positions) >= max_positions:
            result.full_slot_minutes += 1

        selected = (
            ("SOLUSDT",)
            if policy.sol_only
            else timeline.selected(boundary, policy.top_count)
        )
        candidates = [
            item
            for item in signal_groups.get(boundary, [])
            if item.symbol in selected
        ]
        candidates.sort(key=lambda item: (timeline.rank(boundary, item.symbol), item.symbol))
        result.eligible_signals += len(candidates)
        admitted_this_minute = 0
        for event in candidates:
            same_symbol = [
                item
                for item in open_positions
                if item.position.symbol == event.symbol
            ]
            if len(open_positions) >= max_positions:
                result.blocked_slots += 1
                continue
            if len(same_symbol) >= policy.max_positions_per_symbol:
                result.blocked_symbol_cap += 1
                continue
            if admitted_this_minute >= max_entries_per_minute:
                result.blocked_candle_limit += 1
                continue
            if not _passes_spacing(config, event.signal, same_symbol):
                result.blocked_spacing += 1
                continue
            position_sequence += 1
            entry_price = event.signal.price * (1 + entry_cost_bps / 10_000)
            quantity = notional / entry_price
            replay_client = ReplayExecutionClient(exit_cost_bps)
            position = BotFullExitPosition(
                pair_id=f"{policy.name.lower()}-{intrabar_path.lower()}-{position_sequence}",
                symbol=event.symbol,
                entry_price=entry_price,
                quantity=quantity,
                entry_order={"replay": True},
                open_ts=_iso(boundary),
                config=bot_exit_config,
                client=replay_client,  # type: ignore[arg-type]
                logger=logger,  # type: ignore[arg-type]
                entry_atr=event.signal.entry_atr,
                atr_timeframe=event.signal.atr_timeframe,
                atr_period=event.signal.atr_period,
                position_id=position_sequence,
                source_candle_open_time=event.signal.source_candle_open_time,
                position_notional_usdt=notional,
            )
            open_positions.append(
                OpenReplayPosition(position, replay_client, boundary, notional)
            )
            admitted_this_minute += 1

    result.open_positions = open_positions
    result.open_mtm_usdt = _open_mtm(
        open_positions,
        candle_indexes,
        end_ms,
        exit_cost_bps,
        fees_pct,
    )
    return result


def _process_candle(
    open_positions: list[OpenReplayPosition],
    trades: list[ReplayTrade],
    symbol: str,
    candle: MarketCandle,
    intrabar_path: str,
    fees_pct: float,
) -> None:
    points = (
        (candle.open, candle.low, candle.high, candle.close)
        if intrabar_path == "LOW_FIRST"
        else (candle.open, candle.high, candle.low, candle.close)
    )
    for replay_position in list(open_positions):
        position = replay_position.position
        if position.symbol != symbol or position.status != "OPEN":
            continue
        previous: Optional[float] = None
        for point in _deduplicate(points):
            tick = point
            if (
                previous is not None
                and previous > position.effective_stop
                and point <= position.effective_stop
            ):
                tick = position.effective_stop
            replay_position.client.current_price = tick
            event = position.on_tick(tick, _iso(candle.boundary_ms))
            previous = point
            if event is not None:
                exit_price = float(position.exit_price)
                gross_pct = position.pnl_pct(exit_price)
                net_pct = gross_pct - fees_pct
                trades.append(
                    ReplayTrade(
                        symbol=symbol,
                        opened_ms=replay_position.opened_ms,
                        closed_ms=candle.boundary_ms,
                        entry_price=position.entry_price,
                        exit_price=exit_price,
                        gross_pct=gross_pct,
                        net_pct=net_pct,
                        net_usdt=replay_position.notional_usdt * net_pct / 100,
                        exit_reason=str(position.exit_reason),
                    )
                )
                break


def _passes_spacing(
    config: Dict[str, Any],
    signal: EntrySignal,
    positions: Sequence[OpenReplayPosition],
) -> bool:
    spacing_atr = float(config.get("entry", {}).get("entry_spacing_atr", 0))
    if spacing_atr <= 0:
        return True
    if signal.entry_atr is None or signal.entry_atr <= 0:
        return False
    required = spacing_atr * signal.entry_atr
    return all(
        abs(signal.price - item.position.entry_price) >= required
        for item in positions
    )


def _open_mtm(
    positions: Sequence[OpenReplayPosition],
    candle_indexes: Dict[str, Dict[int, MarketCandle]],
    end_ms: int,
    exit_cost_bps: float,
    fees_pct: float,
) -> float:
    total = 0.0
    for item in positions:
        candles = candle_indexes.get(item.position.symbol, {})
        boundaries = [value for value in candles if value <= end_ms]
        if not boundaries:
            continue
        price = candles[max(boundaries)].close * (1 - exit_cost_bps / 10_000)
        net_pct = item.position.pnl_pct(price) - fees_pct
        total += item.notional_usdt * net_pct / 100
    return total


def _load_market_data(
    client: BinancePublicClient,
    markets: Sequence[CurrentMarket],
    interval: str,
    start_ms: int,
    end_ms: int,
    cache_dir: Path,
    offline: bool,
) -> Dict[str, list[MarketCandle]]:
    interval_ms = {
        "1m": MINUTE_MS,
        "15m": 15 * MINUTE_MS,
        "1h": HOUR_MS,
        "4h": 4 * HOUR_MS,
        "1d": DAY_MS,
    }[interval]
    output = {}
    for index, market in enumerate(markets, start=1):
        path = cache_dir / f"{market.symbol}_{interval}.jsonl"
        cached = load_candle_cache(path)
        missing = missing_candle_ranges(cached, start_ms, end_ms, interval_ms)
        if missing and offline:
            continue
        downloaded = [
            candle
            for range_start, range_end in missing
            for candle in client.klines(
                market.symbol,
                interval,
                range_start,
                range_end,
            )
        ]
        candles = merge_candles(cached, downloaded)
        if downloaded:
            save_candle_cache(path, candles)
        remaining = missing_candle_ranges(candles, start_ms, end_ms, interval_ms)
        if remaining:
            print(
                f"  [{index}/{len(markets)}] {market.symbol} {interval}: "
                "excluded (incomplete history)"
            )
            continue
        output[market.symbol] = [
            item
            for item in candles
            if item.open_time_ms >= _floor_ms(start_ms, interval_ms)
            and item.close_time_ms <= end_ms
        ]
        status = "downloaded" if downloaded else "cache"
        print(
            f"  [{index}/{len(markets)}] {market.symbol} {interval}: "
            f"{len(output[market.symbol])} candles ({status})"
        )
    return output


def _load_or_fetch_universe(
    client: BinancePublicClient,
    path: Path,
    config: Dict[str, Any],
    min_quote_volume: float,
    max_symbols: int,
    offline: bool,
) -> list[CurrentMarket]:
    markets = load_universe_snapshot(path)
    if markets:
        return markets[:max_symbols]
    if offline:
        raise SystemExit("Offline mode requires the market-selection universe cache.")
    selection = (
        config.get("instrumentation", {}).get("market_selection_study", {})
        if isinstance(config.get("instrumentation"), dict)
        else {}
    )
    excluded = {str(value).upper() for value in selection.get("excluded_base_assets", [])}
    markets = fetch_current_markets(
        client,
        excluded,
        min_quote_volume,
        max_symbols,
    )
    save_universe_snapshot(path, markets)
    return markets


def _print_report(
    results: Sequence[ReplayResult],
    config: Dict[str, Any],
    lookback_days: int,
    decision_hours: int,
    min_quote_volume: float,
    spread_bps: float,
    start_ms: int,
    end_ms: int,
    entry_timeframe: str,
    execution_timeframe: str,
) -> None:
    fees_pct = _round_trip_fees_pct(config)
    print()
    print("TREND-SOL | selected-market full bot replay")
    print(
        f"Period: {_date(start_ms)} to {_date(end_ms)} | {lookback_days}d | "
        f"selector every {decision_hours}h | entry={entry_timeframe} | execution={execution_timeframe}"
    )
    print(
        f"Costs: taker round-trip={fees_pct:.3f}% | modeled spread/slippage="
        f"{spread_bps:.1f}bp round-trip"
    )
    print(
        f"Selector: 24h>0 and 7d>0 | historical quote volume>="
        f"{min_quote_volume:,.0f} USDT"
    )
    print(
        "Fidelity: actual EntryEngine and BotFullExitPosition; 1m OHLC cannot reveal "
        "the true high/low order, so both paths are reported."
    )
    print(
        "Universe uses currently listed liquid markets (survivorship bias); historical "
        "spread is modeled, not observed."
    )
    print()
    print(
        f"{'path':11} {'policy':15} {'trades':>6} {'win':>7} {'HS':>4} "
        f"{'net USDT':>10} {'open MTM':>10} {'combined':>10} {'PF':>7} {'full':>7}"
    )
    for result in results:
        wins = sum(item.net_pct > 0 for item in result.trades)
        hard_stops = sum(item.exit_reason == "HARD_STOP" for item in result.trades)
        gains = sum(max(0.0, item.net_usdt) for item in result.trades)
        losses = -sum(min(0.0, item.net_usdt) for item in result.trades)
        profit_factor = gains / losses if losses > 0 else math.inf
        winrate = wins / len(result.trades) if result.trades else 0.0
        full = (
            result.full_slot_minutes / result.observed_minutes
            if result.observed_minutes
            else 0.0
        )
        print(
            f"{result.intrabar_path:11} {result.policy:15} {len(result.trades):6d} "
            f"{winrate:7.1%} {hard_stops:4d} {result.realized_net_usdt:+10.4f} "
            f"{result.open_mtm_usdt:+10.4f} {result.combined_net_usdt:+10.4f} "
            f"{_fmt_pf(profit_factor):>7} {full:7.1%}"
        )
    print()
    print("Admission diagnostics:")
    print(
        f"{'path':11} {'policy':15} {'signals':>7} {'eligible':>8} {'slots':>7} "
        f"{'symbol':>7} {'candle':>7} {'spacing':>8} {'open':>5}"
    )
    for result in results:
        print(
            f"{result.intrabar_path:11} {result.policy:15} {result.signals:7d} "
            f"{result.eligible_signals:8d} {result.blocked_slots:7d} "
            f"{result.blocked_symbol_cap:7d} {result.blocked_candle_limit:7d} "
            f"{result.blocked_spacing:8d} {len(result.open_positions):5d}"
        )
    print()
    print("LOW_FIRST is favorable to a long bot; HIGH_FIRST is conservative when a")
    print("single minute both activates a ladder step and returns through its stop.")
    print("Open MTM assumes immediate liquidation at the replay end and includes fees.")


def _print_signal_rearm_report(
    signals: Sequence[SignalEvent],
    baseline_results: Sequence[ReplayResult],
    rearm_results: Sequence[RearmReplay],
) -> None:
    stats = signal_repetition_stats(signals)
    comparisons = int(stats["comparisons"])
    print()
    print("Signal repetition audit:")
    print(
        f"raw approvals={len(signals)} | comparable gaps={comparisons} | "
        f"consecutive minute={_ratio(stats['consecutive'], comparisons):.1%} | "
        f"within 5m={_ratio(stats['within_5m'], comparisons):.1%} | "
        f"within 15m={_ratio(stats['within_15m'], comparisons):.1%} | "
        f"median gap={stats['median_gap_minutes']:.1f}m"
    )
    print(
        "A quiet-period rearm emits only the first approval after the symbol spent "
        "the configured number of complete candles without all four gates passing."
    )
    print()
    print(
        f"{'path':11} {'policy':15} {'quiet':>6} {'signals':>8} {'trades':>7} "
        f"{'HS':>5} {'net USDT':>10} {'delta':>10} {'PF':>7}"
    )
    baselines = {
        (item.intrabar_path, item.policy): item
        for item in baseline_results
    }
    for item in rearm_results:
        result = item.result
        baseline = baselines[(result.intrabar_path, result.policy)]
        hard_stops = sum(trade.exit_reason == "HARD_STOP" for trade in result.trades)
        gains = sum(max(0.0, trade.net_usdt) for trade in result.trades)
        losses = -sum(min(0.0, trade.net_usdt) for trade in result.trades)
        profit_factor = gains / losses if losses > 0 else math.inf
        print(
            f"{result.intrabar_path:11} {result.policy:15} "
            f"{item.quiet_minutes:5d}m {item.rearmed_signals:8d} "
            f"{len(result.trades):7d} {hard_stops:5d} "
            f"{result.combined_net_usdt:+10.4f} "
            f"{result.combined_net_usdt - baseline.combined_net_usdt:+10.4f} "
            f"{_fmt_pf(profit_factor):>7}"
        )
    print()
    for path in ("LOW_FIRST", "HIGH_FIRST"):
        baseline = next(
            (
                item
                for item in baseline_results
                if item.intrabar_path == path and item.policy == "TOP5_ONE_EACH"
            ),
            None,
        )
        if baseline is None:
            continue
        gaps = _reentry_gap_counts(baseline.trades)
        print(
            f"{path} TOP5_ONE_EACH reentries after a prior close: "
            f"<=5m={gaps['5m']} | <=15m={gaps['15m']} | <=60m={gaps['60m']} | "
            f"after HARD_STOP <=60m={gaps['hard_stop_60m']}"
        )


def _reentry_gap_counts(trades: Sequence[ReplayTrade]) -> Dict[str, int]:
    output = {"5m": 0, "15m": 0, "60m": 0, "hard_stop_60m": 0}
    by_symbol: Dict[str, list[ReplayTrade]] = {}
    for trade in trades:
        by_symbol.setdefault(trade.symbol, []).append(trade)
    for symbol_trades in by_symbol.values():
        ordered = sorted(symbol_trades, key=lambda item: item.opened_ms)
        prior_closes = []
        for trade in ordered:
            eligible = [
                item for item in prior_closes if item.closed_ms <= trade.opened_ms
            ]
            if eligible:
                previous = max(eligible, key=lambda item: item.closed_ms)
                gap_minutes = (trade.opened_ms - previous.closed_ms) / MINUTE_MS
                if gap_minutes <= 5:
                    output["5m"] += 1
                if gap_minutes <= 15:
                    output["15m"] += 1
                if gap_minutes <= 60:
                    output["60m"] += 1
                    if previous.exit_reason == "HARD_STOP":
                        output["hard_stop_60m"] += 1
            prior_closes.append(trade)
    return output


def _ratio(value: float, total: int) -> float:
    return value / total if total else 0.0


def _bot_exit_config(config: Dict[str, Any]) -> Dict[str, Any]:
    output = dict(config.get("risk") or config["exit_bot_full_engine"])
    output["fees"] = config.get("fees", {})
    output["ladder"] = config.get("ladder", {})
    return output


def _round_trip_fees_pct(config: Dict[str, Any]) -> float:
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    if not fees or not bool(fees.get("enabled", False)):
        return 0.0
    taker = float(fees.get("taker_fee_pct", 0))
    if bool(fees.get("use_bnb_discount", False)):
        taker *= 0.75
    return taker * 2


def _kline_payload(candle: MarketCandle) -> Dict[str, Any]:
    return {
        "k": {
            "t": candle.open_time_ms,
            "T": candle.close_time_ms,
            "o": str(candle.open),
            "h": str(candle.high),
            "l": str(candle.low),
            "c": str(candle.close),
            "v": str(candle.quote_volume),
            "x": True,
        }
    }


def _deduplicate(values: Iterable[float]) -> list[float]:
    output = []
    for value in values:
        if not output or value != output[-1]:
            output.append(value)
    return output


def _iso(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def _date(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _fmt_pf(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.2f}"


def _study_config(config: Dict[str, Any]) -> Dict[str, Any]:
    instrumentation = (
        config.get("instrumentation")
        if isinstance(config.get("instrumentation"), dict)
        else {}
    )
    value = instrumentation.get("market_bot_replay")
    return value if isinstance(value, dict) else {}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Replay offline do pipeline real de entrada e saida sobre mercados "
            "historicamente selecionados; nunca envia ordens."
        )
    )
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument(
        "--execution-timeframe",
        help="Candles used for exit simulation; defaults to the entry timeframe.",
    )
    parser.add_argument("--market-data-url")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument(
        "--cache-dir",
        default=str(PROJECT_ROOT / "data/market_bot_replay/klines"),
    )
    parser.add_argument(
        "--selection-cache-dir",
        default=str(PROJECT_ROOT / "data/market_selection/klines"),
    )
    parser.add_argument(
        "--universe-snapshot",
        default=str(PROJECT_ROOT / "data/market_selection/universe.jsonl"),
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    return parser.parse_args()


if __name__ == "__main__":
    main()
