from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.monitor.entry_engine import EntryEngine, EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from src.trade_ledger import TradeLedger
from tools.cohort_study import _load_config
from tools.market_bot_replay import (
    MINUTE_MS,
    NullLogger,
    ReplayExecutionClient,
    _bot_exit_config,
    _deduplicate,
    _kline_payload,
    _passes_spacing,
    _round_trip_fees_pct,
)
from tools.market_selection_study import (
    BinancePublicClient,
    MarketCandle,
    _floor_ms,
    load_candle_cache,
    merge_candles,
    missing_candle_ranges,
    save_candle_cache,
)


MATCH_TOLERANCE_MS = 90_000
WARMUP_CANDLES = 300


@dataclass(frozen=True)
class GateDecision:
    boundary_ms: int
    lookback: int
    passed: bool
    high_now: Optional[float]
    low_now: Optional[float]
    high_reference: Optional[float]
    low_reference: Optional[float]
    full_signal: bool = False


@dataclass(frozen=True)
class SignalEvent:
    boundary_ms: int
    signal: EntrySignal


@dataclass
class OpenPosition:
    position: BotFullExitPosition
    client: ReplayExecutionClient
    opened_ms: int
    notional_usdt: float


@dataclass(frozen=True)
class ReplayTrade:
    opened_ms: int
    closed_ms: int
    entry_price: float
    exit_price: float
    peak_price: float
    trough_price: float
    gross_pct: float
    net_pct: float
    exit_reason: str

    @property
    def age_seconds(self) -> float:
        return max(0.0, (self.closed_ms - self.opened_ms) / 1000)


@dataclass(frozen=True)
class ReplayEntry:
    opened_ms: int
    entry_price: float
    closed_ms: Optional[int]
    exit_price: Optional[float]
    gross_pct: Optional[float]
    net_pct: Optional[float]
    exit_reason: str


@dataclass
class ReplayResult:
    name: str
    lookback: int
    signals: int
    trades: list[ReplayTrade] = field(default_factory=list)
    open_positions: list[OpenPosition] = field(default_factory=list)
    entry_times: list[tuple[int, float]] = field(default_factory=list)
    blocked_slots: int = 0
    blocked_spacing: int = 0
    blocked_candle_limit: int = 0
    full_slot_minutes: int = 0
    observed_minutes: int = 0
    max_simultaneous_positions: int = 0

    def entries(self) -> list[ReplayEntry]:
        closed = {
            (item.opened_ms, round(item.entry_price, 12)): item
            for item in self.trades
        }
        output = []
        for opened_ms, entry_price in self.entry_times:
            trade = closed.get((opened_ms, round(entry_price, 12)))
            if trade is None:
                output.append(ReplayEntry(opened_ms, entry_price, None, None, None, None, "OPEN"))
            else:
                output.append(
                    ReplayEntry(
                        trade.opened_ms,
                        trade.entry_price,
                        trade.closed_ms,
                        trade.exit_price,
                        trade.gross_pct,
                        trade.net_pct,
                        trade.exit_reason,
                    )
                )
        return sorted(output, key=lambda item: item.opened_ms)


@dataclass(frozen=True)
class Validation:
    observed: int
    replayed: int
    matched: int
    unmatched_observed: tuple[Dict[str, Any], ...]
    unmatched_replay: tuple[ReplayEntry, ...]
    reason_matches: int
    pnl_abs_error: Optional[float]
    entry_time_abs_error_seconds: Optional[float]

    @property
    def match_rate(self) -> float:
        return self.matched / self.observed if self.observed else 0.0

    @property
    def level(self) -> str:
        if self.observed < 5:
            return "INSUFFICIENT_SAMPLE"
        if self.match_rate >= 0.80:
            return "HIGH"
        if self.match_rate >= 0.60:
            return "MEDIUM"
        return "LOW"


class CaptureLogger(NullLogger):
    def __init__(self, lookback: int) -> None:
        self.lookback = lookback
        self.boundary_ms = 0
        self.decisions: Dict[int, GateDecision] = {}

    def decision(self, event: Dict[str, Any]) -> None:
        if int(event.get("gate", 0) or 0) != 1:
            return
        self.decisions[self.boundary_ms] = GateDecision(
            boundary_ms=self.boundary_ms,
            lookback=self.lookback,
            passed=bool(event.get("passed", False)),
            high_now=_optional_float(event.get("high_now")),
            low_now=_optional_float(event.get("low_now")),
            high_reference=_optional_float(event.get("high_lookback")),
            low_reference=_optional_float(event.get("low_lookback")),
        )


def main() -> None:
    args = _parse_args()
    raw_config = _load_config(Path(args.config))
    config = effective_config(raw_config)
    if args.profile != "all" and str(config.get("active_profile")) != args.profile:
        raise SystemExit(
            f"Configured active_profile is {config.get('active_profile')}, not {args.profile}. "
            "The replay does not mutate profile configuration."
        )
    _validate_replay_context(config)
    lookbacks = parse_lookbacks(args.compare_ge)
    if len(lookbacks) != 2:
        raise SystemExit("--compare-ge must contain exactly two minute horizons")
    configured_lookback = int(config.get("trend_gate", {}).get("lookback_candles", 0))
    if lookbacks[0] != configured_lookback:
        raise SystemExit(
            "The first --compare-ge horizon must match the observed runtime GE "
            f"({configured_lookback * 5} minutes) so validation is meaningful."
        )
    requested_start = parse_cli_datetime(args.since)
    if requested_start is None:
        raise SystemExit("--since is required")
    now_ms = _floor_ms(int(datetime.now(timezone.utc).timestamp() * 1000), MINUTE_MS) - 1
    parsed_until = parse_cli_datetime(args.until)
    end_ms = min(now_ms, int(parsed_until.timestamp() * 1000) - 1) if parsed_until else now_ms
    if end_ms <= int(requested_start.timestamp() * 1000):
        raise SystemExit("Replay end must be after --since")

    ledger_path = Path(args.ledger) if args.ledger else None
    ledger_records = TradeLedger(PROJECT_ROOT, ledger_path).load()
    real_records = real_bot_b_records(ledger_records, args.profile)
    state_records = read_open_state(Path(args.state))
    actual_start_ms, initial_fidelity = find_equivalent_flat_start(
        int(requested_start.timestamp() * 1000),
        end_ms,
        real_records,
        state_records,
    )
    if actual_start_ms is None:
        raise SystemExit(
            "Could not find a later minute with an observed empty Bot B state. "
            "Replay stopped instead of inventing empty slots."
        )

    interval_ms = {"1m": MINUTE_MS, "5m": 5 * MINUTE_MS, "15m": 15 * MINUTE_MS}
    warmup_ms = max(WARMUP_CANDLES * value for value in interval_ms.values())
    data_start_ms = actual_start_ms - warmup_ms
    base_url = str(args.market_data_url or config.get("market_data", {}).get("rest_url") or "https://api.binance.com")
    client = BinancePublicClient(base_url, int(args.http_timeout_seconds))
    symbol = str(config.get("symbol") or "SOLUSDT")
    cache_dir = Path(args.cache_dir)
    candles: Dict[str, list[MarketCandle]] = {}
    for interval in ("1m", "5m", "15m"):
        candles[interval] = load_ge_market_data(
            client,
            symbol,
            interval,
            data_start_ms,
            end_ms,
            cache_dir,
            bool(args.offline),
        )

    variant_configs = {
        lookback: ge_variant_config(config, lookback)
        for lookback in lookbacks
    }
    generated = {
        lookback: generate_ge_signals(
            variant_configs[lookback],
            candles["1m"],
            candles["5m"],
            candles["15m"],
            actual_start_ms,
            end_ms,
            lookback,
        )
        for lookback in lookbacks
    }
    spread_bps = float(
        args.round_trip_spread_bps
        if args.round_trip_spread_bps is not None
        else config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0)
    )
    if spread_bps < 0:
        raise SystemExit("--round-trip-spread-bps cannot be negative")
    path = args.intrabar_path.upper()
    results = {
        lookback: run_universe(
            name=f"GE{lookback * 5}",
            lookback=lookback,
            config=variant_configs[lookback],
            signals=generated[lookback][0],
            execution_candles=candles["1m"],
            start_ms=actual_start_ms,
            end_ms=end_ms,
            intrabar_path=path,
            round_trip_spread_bps=spread_bps,
        )
        for lookback in lookbacks
    }
    observed_source = [*real_records, *open_state_as_observed(state_records, args.profile)]
    observed = [
        item
        for item in observed_source
        if (_ts_ms(item.get("opened_at")) or -1) >= actual_start_ms
        and (_ts_ms(item.get("opened_at")) or end_ms + 1) <= end_ms
    ]
    baseline = results[lookbacks[0]]
    validation = validate_replay(baseline.entries(), observed, MATCH_TOLERANCE_MS)
    _print_report(
        requested_start_ms=int(requested_start.timestamp() * 1000),
        start_ms=actual_start_ms,
        end_ms=end_ms,
        initial_fidelity=initial_fidelity,
        config=config,
        lookbacks=lookbacks,
        results=results,
        validation=validation,
        generated=generated,
        candle_counts={key: len(value) for key, value in candles.items()},
        path=path,
        spread_bps=spread_bps,
        detail=bool(args.detail),
        decision_detail=bool(args.decision_detail),
    )


def load_ge_market_data(
    client: BinancePublicClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    cache_dir: Path,
    offline: bool,
) -> list[MarketCandle]:
    interval_sizes = {
        "1m": MINUTE_MS,
        "5m": 5 * MINUTE_MS,
        "15m": 15 * MINUTE_MS,
    }
    if interval not in interval_sizes:
        raise ValueError(f"Unsupported GE replay interval: {interval}")
    interval_ms = interval_sizes[interval]
    path = cache_dir / f"{symbol}_{interval}.jsonl"
    cached = load_candle_cache(path)
    missing = missing_candle_ranges(cached, start_ms, end_ms, interval_ms)
    if missing and offline:
        raise SystemExit(
            f"Offline cache incomplete for {symbol}/{interval}: {len(missing)} missing range(s)"
        )
    downloaded = [
        candle
        for range_start, range_end in missing
        for candle in fetch_ge_klines(
            client,
            symbol,
            interval,
            range_start,
            range_end,
            interval_ms,
        )
    ]
    candles = merge_candles(cached, downloaded)
    if downloaded:
        save_candle_cache(path, candles)
    remaining = missing_candle_ranges(candles, start_ms, end_ms, interval_ms)
    if remaining:
        raise SystemExit(
            f"Complete historical candles unavailable for {symbol}/{interval}: "
            f"{len(remaining)} missing range(s)"
        )
    return [
        candle
        for candle in candles
        if candle.open_time_ms >= _floor_ms(start_ms, interval_ms)
        and candle.close_time_ms <= end_ms
    ]


def fetch_ge_klines(
    client: BinancePublicClient,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    interval_ms: int,
) -> list[MarketCandle]:
    cursor = _floor_ms(start_ms, interval_ms)
    indexed: Dict[int, MarketCandle] = {}
    while cursor <= end_ms:
        data = client.get(
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(data, list):
            raise RuntimeError(f"Unexpected kline response for {symbol}/{interval}")
        page = [ge_candle_from_binance(item) for item in data if isinstance(item, list)]
        if not page:
            break
        for candle in page:
            indexed[candle.open_time_ms] = candle
        next_cursor = max(candle.open_time_ms for candle in page) + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError(f"Kline pagination did not advance for {symbol}/{interval}")
        cursor = next_cursor
        if len(page) < 1000:
            break
    return [
        indexed[key]
        for key in sorted(indexed)
        if indexed[key].open_time_ms >= _floor_ms(start_ms, interval_ms)
        and indexed[key].close_time_ms <= end_ms
    ]


def ge_candle_from_binance(value: Sequence[Any]) -> MarketCandle:
    if len(value) < 9:
        raise ValueError("Binance kline must have at least 9 fields")
    return MarketCandle(
        open_time_ms=int(value[0]),
        close_time_ms=int(value[6]),
        open=float(value[1]),
        high=float(value[2]),
        low=float(value[3]),
        close=float(value[4]),
        quote_volume=float(value[7]),
        trades=int(value[8]),
    )


def ge_variant_config(config: Dict[str, Any], lookback_candles: int) -> Dict[str, Any]:
    if lookback_candles < 1:
        raise ValueError("GE lookback must be positive")
    output = deepcopy(config)
    output.setdefault("trend_gate", {})
    output["trend_gate"]["mode"] = "ge30"
    output["trend_gate"]["candle_interval"] = "5m"
    output["trend_gate"]["lookback_candles"] = lookback_candles
    return output


def parse_lookbacks(value: str) -> tuple[int, ...]:
    horizons = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    if any(item <= 0 or item % 5 for item in horizons):
        raise SystemExit("GE horizons must be positive multiples of 5 minutes")
    return tuple(item // 5 for item in horizons)


def generate_ge_signals(
    config: Dict[str, Any],
    entry_candles: Sequence[MarketCandle],
    gate_candles: Sequence[MarketCandle],
    trend_candles: Sequence[MarketCandle],
    start_ms: int,
    end_ms: int,
    lookback: int,
) -> tuple[list[SignalEvent], Dict[int, GateDecision]]:
    logger = CaptureLogger(lookback)
    symbol = str(config.get("symbol") or "SOLUSDT")
    engine = EntryEngine(symbol, config, logger)  # type: ignore[arg-type]
    gate_index = trend_index = 0
    signals: list[SignalEvent] = []
    for candle in entry_candles:
        boundary = candle.boundary_ms
        if boundary > end_ms:
            break
        while trend_index < len(trend_candles) and trend_candles[trend_index].boundary_ms <= boundary:
            current = trend_candles[trend_index]
            engine.on_kline(f"{symbol.lower()}@kline_15m", _kline_payload(current))
            trend_index += 1
        while gate_index < len(gate_candles) and gate_candles[gate_index].boundary_ms <= boundary:
            current = gate_candles[gate_index]
            engine.on_kline(f"{symbol.lower()}@kline_5m", _kline_payload(current))
            gate_index += 1
        logger.boundary_ms = boundary
        signal = engine.on_kline(f"{symbol.lower()}@kline_1m", _kline_payload(candle))
        if signal is not None and start_ms <= boundary <= end_ms:
            signals.append(SignalEvent(boundary, signal))
            decision = logger.decisions.get(boundary)
            if decision is not None:
                logger.decisions[boundary] = GateDecision(**{**decision.__dict__, "full_signal": True})
    return signals, logger.decisions


def run_universe(
    *,
    name: str,
    lookback: int,
    config: Dict[str, Any],
    signals: Sequence[SignalEvent],
    execution_candles: Sequence[MarketCandle],
    start_ms: int,
    end_ms: int,
    intrabar_path: str,
    round_trip_spread_bps: float,
) -> ReplayResult:
    if intrabar_path not in {"HIGH_FIRST", "LOW_FIRST"}:
        raise ValueError("intrabar path must be HIGH_FIRST or LOW_FIRST")
    signal_groups: Dict[int, list[SignalEvent]] = {}
    for event in signals:
        signal_groups.setdefault(event.boundary_ms, []).append(event)
    candle_index = {item.boundary_ms: item for item in execution_candles}
    result = ReplayResult(name=name, lookback=lookback, signals=len(signals))
    open_positions: list[OpenPosition] = []
    max_positions = int(config["capital"]["max_open_positions"])
    notional = float(config["capital"]["operational_balance_usdt"]) * float(config["capital"]["trade_size_pct"]) / 100
    max_per_candle = int(config.get("entry", {}).get("max_entries_per_candle", 1))
    entry_cost_bps = exit_cost_bps = round_trip_spread_bps / 2
    fees_pct = _round_trip_fees_pct(config)
    exit_config = _bot_exit_config(config)
    logger = NullLogger()
    sequence = 0
    boundary = ceil_ms(start_ms, MINUTE_MS)
    while boundary <= end_ms:
        result.observed_minutes += 1
        candle = candle_index.get(boundary)
        if candle is not None:
            process_candle(open_positions, result.trades, candle, intrabar_path, fees_pct)
        open_positions[:] = [item for item in open_positions if item.position.status == "OPEN"]
        if len(open_positions) >= max_positions:
            result.full_slot_minutes += 1
        admitted = 0
        for event in signal_groups.get(boundary, []):
            if len(open_positions) >= max_positions:
                result.blocked_slots += 1
                continue
            if admitted >= max_per_candle:
                result.blocked_candle_limit += 1
                continue
            if not _passes_spacing(config, event.signal, open_positions):
                result.blocked_spacing += 1
                continue
            sequence += 1
            entry_price = event.signal.price * (1 + entry_cost_bps / 10_000)
            quantity = notional / entry_price
            replay_client = ReplayExecutionClient(exit_cost_bps)
            position = BotFullExitPosition(
                pair_id=f"{name.lower()}-{sequence}",
                symbol=event.signal.symbol,
                entry_price=entry_price,
                quantity=quantity,
                entry_order={"replay": True},
                open_ts=_iso(boundary),
                config=exit_config,
                client=replay_client,  # type: ignore[arg-type]
                logger=logger,  # type: ignore[arg-type]
                entry_atr=event.signal.entry_atr,
                atr_timeframe=event.signal.atr_timeframe,
                atr_period=event.signal.atr_period,
                position_id=sequence,
                source_candle_open_time=event.signal.source_candle_open_time,
                position_notional_usdt=notional,
            )
            open_positions.append(OpenPosition(position, replay_client, boundary, notional))
            result.entry_times.append((boundary, entry_price))
            admitted += 1
            result.max_simultaneous_positions = max(result.max_simultaneous_positions, len(open_positions))
        boundary += MINUTE_MS
    result.open_positions = open_positions
    return result


def process_candle(
    positions: Sequence[OpenPosition],
    trades: list[ReplayTrade],
    candle: MarketCandle,
    intrabar_path: str,
    fees_pct: float,
) -> None:
    points = (
        (candle.open, candle.high, candle.low, candle.close)
        if intrabar_path == "HIGH_FIRST"
        else (candle.open, candle.low, candle.high, candle.close)
    )
    for replay_position in list(positions):
        position = replay_position.position
        if position.status != "OPEN":
            continue
        previous: Optional[float] = None
        for point in _deduplicate(points):
            tick = point
            if previous is not None and previous > position.effective_stop and point <= position.effective_stop:
                tick = position.effective_stop
            replay_position.client.current_price = tick
            event = position.on_tick(tick, _iso(candle.boundary_ms))
            previous = point
            if event is None:
                continue
            exit_price = float(position.exit_price)
            gross = position.pnl_pct(exit_price)
            trades.append(
                ReplayTrade(
                    opened_ms=replay_position.opened_ms,
                    closed_ms=candle.boundary_ms,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    peak_price=position.highest_price,
                    trough_price=position.trough_price,
                    gross_pct=gross,
                    net_pct=gross - fees_pct,
                    exit_reason=str(position.exit_reason),
                )
            )
            break


def find_equivalent_flat_start(
    requested_ms: int,
    end_ms: int,
    ledger_records: Sequence[Dict[str, Any]],
    state_records: Sequence[Dict[str, Any]],
) -> tuple[Optional[int], str]:
    intervals: list[tuple[int, int]] = []
    for record in ledger_records:
        opened = _ts_ms(record.get("opened_at"))
        closed = _ts_ms(record.get("closed_at"))
        if opened is not None and closed is not None and closed > opened:
            intervals.append((opened, closed))
    for record in state_records:
        if bool(record.get("phantom")) or str(record.get("status") or "OPEN") != "OPEN":
            continue
        opened = _ts_ms(record.get("open_ts") or record.get("opened_at"))
        if opened is not None:
            intervals.append((opened, end_ms + MINUTE_MS))
    if not intervals and not ledger_records and not state_records:
        return requested_ms, "UNVERIFIED_EMPTY: ledger/state contain no reconstructable positions"
    boundary = ceil_ms(requested_ms, MINUTE_MS)
    while boundary <= end_ms:
        if not any(opened <= boundary < closed for opened, closed in intervals):
            if boundary == requested_ms:
                return boundary, "EXACT_OBSERVED_EMPTY"
            return boundary, "SHIFTED_TO_FIRST_OBSERVED_EMPTY_MINUTE"
        boundary += MINUTE_MS
    return None, "NO_OBSERVED_EMPTY_MINUTE"


def validate_replay(
    replay_entries: Sequence[ReplayEntry],
    observed_records: Sequence[Dict[str, Any]],
    tolerance_ms: int,
) -> Validation:
    observed = [item for item in observed_records if _ts_ms(item.get("opened_at")) is not None]
    pairs, missing_left, missing_right = match_by_time(
        observed,
        replay_entries,
        lambda item: int(_ts_ms(item.get("opened_at")) or 0),
        lambda item: item.opened_ms,
        tolerance_ms,
    )
    reason_matches = 0
    pnl_errors = []
    time_errors = []
    for real, replay in pairs:
        real_reason = _observed_reason(real)
        if real_reason == replay.exit_reason:
            reason_matches += 1
        time_errors.append(abs(int(_ts_ms(real.get("opened_at")) or 0) - replay.opened_ms) / 1000)
        real_net = _optional_float(real.get("net_pnl_pct"))
        if real_net is None:
            gross = _optional_float(real.get("gross_pnl_pct"))
            fees = _optional_float(real.get("estimated_fees_pct"))
            real_net = gross - fees if gross is not None and fees is not None else None
        if real_net is not None and replay.net_pct is not None:
            pnl_errors.append(abs(real_net - replay.net_pct))
    return Validation(
        observed=len(observed),
        replayed=len(replay_entries),
        matched=len(pairs),
        unmatched_observed=tuple(missing_left),
        unmatched_replay=tuple(missing_right),
        reason_matches=reason_matches,
        pnl_abs_error=statistics.fmean(pnl_errors) if pnl_errors else None,
        entry_time_abs_error_seconds=statistics.fmean(time_errors) if time_errors else None,
    )


def match_by_time(
    left: Sequence[Any],
    right: Sequence[Any],
    left_time: Any,
    right_time: Any,
    tolerance_ms: int,
) -> tuple[list[tuple[Any, Any]], list[Any], list[Any]]:
    available = set(range(len(right)))
    pairs = []
    unmatched_left = []
    for item in sorted(left, key=left_time):
        candidates = [
            index
            for index in available
            if abs(right_time(right[index]) - left_time(item)) <= tolerance_ms
        ]
        if not candidates:
            unmatched_left.append(item)
            continue
        selected = min(candidates, key=lambda index: (abs(right_time(right[index]) - left_time(item)), index))
        available.remove(selected)
        pairs.append((item, right[selected]))
    return pairs, unmatched_left, [right[index] for index in sorted(available)]


def real_bot_b_records(records: Sequence[Dict[str, Any]], profile: str) -> list[Dict[str, Any]]:
    output = []
    for record in records:
        if bool(record.get("phantom")) or str(record.get("position_type") or "") == "PHANTOM":
            continue
        if record.get("shadow_kind") or str(record.get("position_type") or "BOT_EXIT") != "BOT_EXIT":
            continue
        if profile != "all" and str(record.get("profile") or "") != profile:
            continue
        output.append(record)
    return output


def open_state_as_observed(records: Sequence[Dict[str, Any]], profile: str) -> list[Dict[str, Any]]:
    output = []
    for record in records:
        if bool(record.get("phantom")) or str(record.get("status") or "OPEN") != "OPEN":
            continue
        if record.get("shadow_kind"):
            continue
        if profile != "all" and record.get("profile") not in (None, "", profile):
            continue
        opened_at = record.get("open_ts") or record.get("opened_at")
        if not opened_at:
            continue
        output.append(
            {
                "opened_at": opened_at,
                "closed_at": None,
                "entry_price": record.get("entry_price"),
                "exit_reason": "OPEN",
                "profile": record.get("profile") or profile,
            }
        )
    return output


def read_open_state(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        positions = value.get("positions")
        if isinstance(positions, list):
            return [item for item in positions if isinstance(item, dict)]
    return []


def _print_report(
    *,
    requested_start_ms: int,
    start_ms: int,
    end_ms: int,
    initial_fidelity: str,
    config: Dict[str, Any],
    lookbacks: Sequence[int],
    results: Dict[int, ReplayResult],
    validation: Validation,
    generated: Dict[int, tuple[list[SignalEvent], Dict[int, GateDecision]]],
    candle_counts: Dict[str, int],
    path: str,
    spread_bps: float,
    detail: bool,
    decision_detail: bool,
) -> None:
    names = [results[item].name for item in lookbacks]
    fee_pct = _round_trip_fees_pct(config)
    economic = config.get("risk", {}).get("profit_lock", {}).get("economic_floor", {})
    economic_text = (
        f"adaptive Profit Lock economic floor, net margin={float(economic.get('net_margin_pct', 0)):.3f}%"
        if bool(economic.get("enabled", False))
        else "standard Profit Lock (economic floor disabled)"
    )
    print("TREND-SOL | GE counterfactual replay")
    print(f"Requested period: {_stamp(requested_start_ms)} to {_stamp(end_ms)}")
    print(f"Effective period: {_stamp(start_ms)} to {_stamp(end_ms)}")
    print(f"Profile: {config.get('active_profile')}")
    print("Market data source: Binance public historical klines | 1m execution, 5m GE, 15m auxiliary")
    print(f"Candles: 1m={candle_counts['1m']} | 5m={candle_counts['5m']} | 15m={candle_counts['15m']}")
    print("Candle gaps in required range: 0 (a timeframe is rejected if its history is incomplete)")
    print(f"Replay resolution: 1m OHLC | intrabar={path} | modeled spread/slippage={spread_bps:.1f}bp round-trip")
    print(f"Exit logic version: current code | {economic_text}")
    print(f"Initial-state fidelity: {initial_fidelity}")
    print("Same-boundary ordering: closed 15m, then closed 5m, then 1m entry evaluation.")
    print()
    print(f"1. {names[0]} REPLAY VS OBSERVED VALIDATION")
    print(f"observed trades | {validation.observed}")
    print(f"replay entries | {validation.replayed}")
    print(f"matched entries (within {MATCH_TOLERANCE_MS // 1000}s) | {validation.matched}")
    print(f"entry match rate | {validation.match_rate:.1%}")
    print(f"mean absolute entry-time difference | {_fmt_seconds(validation.entry_time_abs_error_seconds)}")
    print(f"matched exit reasons | {validation.reason_matches}/{validation.matched}")
    print(f"mean absolute net difference | {_fmt_pct(validation.pnl_abs_error)}")
    print(f"fidelity level | {validation.level}")
    if validation.level in {"LOW", "INSUFFICIENT_SAMPLE"}:
        print("WARNING | Counterfactual comparison is diagnostic and must not be treated as reliable promotion evidence.")
    _print_validation_mismatches(validation)

    first, second = (results[item] for item in lookbacks)
    print()
    print(f"2. {first.name} VS {second.name} SUMMARY")
    print(f"metric | {first.name} | {second.name} | delta ({second.name}-{first.name})")
    _print_metric("closed trades", len(first.trades), len(second.trades), integer=True)
    _print_metric("open at end", len(first.open_positions), len(second.open_positions), integer=True)
    _print_metric("full-pipeline signals", first.signals, second.signals, integer=True)
    _print_metric("blocked by slots", first.blocked_slots, second.blocked_slots, integer=True)
    _print_metric("blocked by spacing", first.blocked_spacing, second.blocked_spacing, integer=True)
    _print_metric("gross total", sum(item.gross_pct for item in first.trades), sum(item.gross_pct for item in second.trades), pct=True)
    _print_metric("net total", sum(item.net_pct for item in first.trades), sum(item.net_pct for item in second.trades), pct=True)
    _print_metric("avg gross", _mean([item.gross_pct for item in first.trades]), _mean([item.gross_pct for item in second.trades]), pct=True)
    _print_metric("avg net", _mean([item.net_pct for item in first.trades]), _mean([item.net_pct for item in second.trades]), pct=True)
    _print_metric("gross winrate", _winrate(first.trades, False), _winrate(second.trades, False), pct=True)
    _print_metric("net winrate", _winrate(first.trades, True), _winrate(second.trades, True), pct=True)
    _print_metric("gross profit factor", _profit_factor(first.trades, False), _profit_factor(second.trades, False))
    _print_metric("net profit factor", _profit_factor(first.trades, True), _profit_factor(second.trades, True))
    _print_metric("fees", len(first.trades) * fee_pct, len(second.trades) * fee_pct, pct=True)
    _print_metric("avg age minutes", _mean([item.age_seconds / 60 for item in first.trades]), _mean([item.age_seconds / 60 for item in second.trades]))
    _print_metric("median age minutes", _median([item.age_seconds / 60 for item in first.trades]), _median([item.age_seconds / 60 for item in second.trades]))
    _print_metric("slots full time", _full_pct(first), _full_pct(second), pct=True)
    _print_metric("max simultaneous", first.max_simultaneous_positions, second.max_simultaneous_positions, integer=True)

    print()
    print("3. EXIT REASONS")
    print(f"reason | {first.name} | {second.name} | delta")
    first_reasons = Counter(item.exit_reason for item in first.trades)
    second_reasons = Counter(item.exit_reason for item in second.trades)
    for reason in sorted(set(first_reasons) | set(second_reasons)):
        _print_metric(reason, first_reasons[reason], second_reasons[reason], integer=True)

    overlap, only_first, only_second = entry_overlap(first.entries(), second.entries(), MATCH_TOLERANCE_MS)
    print()
    print("4. ENTRY OVERLAP / EXCLUSIVES")
    print(f"matching rule | nearest entry within {MATCH_TOLERANCE_MS // 1000}s, one-to-one")
    print(f"common entries | {len(overlap)}")
    print(f"{first.name}-only | {len(only_first)}")
    print(f"{second.name}-only | {len(only_second)}")
    _print_exclusives(f"{first.name}-only", only_first)
    _print_exclusives(f"{second.name}-only", only_second)

    hs_first = [item for item in first.entries() if item.exit_reason == "HARD_STOP"]
    hs_second = [item for item in second.entries() if item.exit_reason == "HARD_STOP"]
    hs_common, hs_only_first, hs_only_second = entry_overlap(hs_first, hs_second, MATCH_TOLERANCE_MS)
    print()
    print("5. HARD STOP COMPARISON")
    print(f"HARD_STOP {first.name} total | {len(hs_first)}")
    print(f"HARD_STOP {second.name} total | {len(hs_second)}")
    print(f"HARD_STOP common | {len(hs_common)}")
    print(f"HARD_STOP exclusive {first.name} | {len(hs_only_first)}")
    print(f"HARD_STOP exclusive {second.name} | {len(hs_only_second)}")

    if detail:
        print()
        print("6. DETAILED TRADES")
        _print_trades(first)
        _print_trades(second)
    if decision_detail:
        print()
        print("7. DIVERGENT GE DECISIONS")
        _print_decision_differences(generated[lookbacks[0]][1], generated[lookbacks[1]][1], names)
    print()
    print("LIMITATIONS")
    print("- Historical entries use the 1m candle close plus modeled half-spread, not the observed market fill.")
    print("- Runtime exits consume aggTrade ticks; this replay uses a declared OHLC path and cannot know true intraminute order.")
    print("- Current entry/exit configuration is applied uniformly; historical patch changes are intentionally not replayed.")
    print("- Operational pauses, API failures and exchange-side balance anomalies are not replayed.")
    print("- Cache writes, when needed, are confined to the study cache directory; runtime state and ledgers are read-only.")
    print("- Entry comparison is trustworthy only in proportion to the validation level shown above.")


def entry_overlap(
    first: Sequence[ReplayEntry],
    second: Sequence[ReplayEntry],
    tolerance_ms: int,
) -> tuple[list[tuple[ReplayEntry, ReplayEntry]], list[ReplayEntry], list[ReplayEntry]]:
    return match_by_time(first, second, lambda item: item.opened_ms, lambda item: item.opened_ms, tolerance_ms)


def _print_exclusives(title: str, entries: Sequence[ReplayEntry]) -> None:
    print(f"{title}:")
    print("opened | entry | closed | exit | gross | net | reason")
    if not entries:
        print("none")
        return
    for item in entries:
        print(_entry_line(item))


def _print_validation_mismatches(validation: Validation) -> None:
    if not validation.unmatched_observed and not validation.unmatched_replay:
        return
    print("principal unmatched entries (max 10 each):")
    for item in validation.unmatched_observed[:10]:
        opened = _ts_ms(item.get("opened_at"))
        print(
            f"  observed-only | {_stamp(opened) if opened is not None else 'n/a'} | "
            f"entry={_fmt_price(_optional_float(item.get('entry_price')))} | reason={_observed_reason(item)}"
        )
    for item in validation.unmatched_replay[:10]:
        print(f"  replay-only | {_stamp(item.opened_ms)} | entry={item.entry_price:.4f} | reason={item.exit_reason}")


def _print_trades(result: ReplayResult) -> None:
    print(f"{result.name}:")
    print("opened | closed | age | entry | peak | trough | exit | gross | net | reason")
    if not result.trades:
        print("none")
    for item in result.trades:
        print(
            f"{_stamp(item.opened_ms)} | {_stamp(item.closed_ms)} | {item.age_seconds / 60:.0f}m | "
            f"{item.entry_price:.4f} | {item.peak_price:.4f} | {item.trough_price:.4f} | "
            f"{item.exit_price:.4f} | {item.gross_pct:+.3f}% | {item.net_pct:+.3f}% | {item.exit_reason}"
        )
    for item in result.entries():
        if item.closed_ms is None:
            print(f"{_stamp(item.opened_ms)} | OPEN | n/a | {item.entry_price:.4f} | n/a | n/a | n/a | n/a | n/a | OPEN")


def _print_decision_differences(
    first: Dict[int, GateDecision],
    second: Dict[int, GateDecision],
    names: Sequence[str],
) -> None:
    print(f"timestamp | high_now | low_now | {names[0]} ref H/L | {names[0]} GE/full | {names[1]} ref H/L | {names[1]} GE/full")
    count = 0
    for boundary in sorted(set(first) & set(second)):
        left, right = first[boundary], second[boundary]
        if left.passed == right.passed and left.full_signal == right.full_signal:
            continue
        count += 1
        print(
            f"{_stamp(boundary)} | {_fmt_price(left.high_now)} | {_fmt_price(left.low_now)} | "
            f"{_fmt_price(left.high_reference)}/{_fmt_price(left.low_reference)} | "
            f"{_pass(left.passed)}/{_pass(left.full_signal)} | "
            f"{_fmt_price(right.high_reference)}/{_fmt_price(right.low_reference)} | "
            f"{_pass(right.passed)}/{_pass(right.full_signal)}"
        )
    if not count:
        print("none")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay contrafactual completo e independente para horizontes GE; nao envia ordens."
    )
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument("--compare-ge", default="30,15")
    parser.add_argument("--intrabar-path", choices=["high_first", "low_first"], default="high_first")
    parser.add_argument("--round-trip-spread-bps", type=float)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--decision-detail", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--market-data-url")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "data/studies/ge_replay/klines"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--ledger")
    parser.add_argument("--state", default=str(PROJECT_ROOT / "data/state/open_positions.json"))
    return parser.parse_args()


def parse_cli_datetime(value: Optional[str], reference: Optional[datetime] = None) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    current = reference or datetime.now(BRASILIA_TZ)
    try:
        if len(text) == 11 and text[2] == "/" and text[5] == " " and text[8] == ":":
            parsed = datetime.strptime(f"{text}/{current.year}", "%d/%m %H:%M/%Y").replace(tzinfo=BRASILIA_TZ)
        elif len(text) == 10:
            parsed = datetime.combine(date.fromisoformat(text), time.min)
        else:
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        raise SystemExit(f"Invalid date/time: {value}. Use DD/MM HH:MM or ISO 8601.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA_TZ)
    return parsed.astimezone(timezone.utc)


def ceil_ms(value: int, interval_ms: int) -> int:
    return value if value % interval_ms == 0 else value + interval_ms - value % interval_ms


def _ts_ms(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _observed_reason(record: Dict[str, Any]) -> str:
    reason = str(record.get("exit_reason") or "OPEN")
    if reason == "REVIEW_STOP" and str(record.get("final_step") or "") == "BE":
        return "BREAKEVEN"
    return reason


def _iso(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


def _stamp(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


def _entry_line(item: ReplayEntry) -> str:
    return (
        f"{_stamp(item.opened_ms)} | {item.entry_price:.4f} | "
        f"{_stamp(item.closed_ms) if item.closed_ms is not None else 'OPEN'} | "
        f"{_fmt_price(item.exit_price)} | {_fmt_pct(item.gross_pct)} | {_fmt_pct(item.net_pct)} | {item.exit_reason}"
    )


def _print_metric(label: str, first: Any, second: Any, *, pct: bool = False, integer: bool = False) -> None:
    delta = second - first if math.isfinite(float(first)) and math.isfinite(float(second)) else None
    if integer:
        print(f"{label} | {int(first)} | {int(second)} | {int(delta or 0):+d}")
    elif pct:
        print(f"{label} | {_fmt_pct(first)} | {_fmt_pct(second)} | {_fmt_pct(delta)}")
    else:
        delta_text = "n/a" if delta is None else _fmt_number(delta, signed=True)
        print(f"{label} | {_fmt_number(first)} | {_fmt_number(second)} | {delta_text}")


def _validate_replay_context(config: Dict[str, Any]) -> None:
    gate = config.get("trend_gate") if isinstance(config.get("trend_gate"), dict) else {}
    if str(config.get("entry", {}).get("timeframe")) != "1m":
        raise SystemExit("GE replay currently requires the intraday entry timeframe 1m")
    if str(config.get("trend", {}).get("timeframe")) != "15m":
        raise SystemExit("GE replay currently requires the intraday auxiliary timeframe 15m")
    if str(gate.get("mode", "")).lower() != "ge30" or str(gate.get("candle_interval")) != "5m":
        raise SystemExit("GE replay requires the observed runtime GE mode on closed 5m candles")


def _mean(values: Sequence[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def _median(values: Sequence[float]) -> float:
    return statistics.median(values) if values else 0.0


def _winrate(trades: Sequence[ReplayTrade], net: bool) -> float:
    if not trades:
        return 0.0
    return sum((item.net_pct if net else item.gross_pct) > 0 for item in trades) / len(trades) * 100


def _profit_factor(trades: Sequence[ReplayTrade], net: bool) -> float:
    values = [item.net_pct if net else item.gross_pct for item in trades]
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    return gains / losses if losses else (math.inf if gains else 0.0)


def _full_pct(result: ReplayResult) -> float:
    return result.full_slot_minutes / result.observed_minutes * 100 if result.observed_minutes else 0.0


def _fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


def _fmt_price(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _fmt_number(value: float, signed: bool = False) -> str:
    if math.isinf(value):
        return "inf"
    return f"{value:+.3f}" if signed else f"{value:.3f}"


def _fmt_seconds(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def _pass(value: bool) -> str:
    return "PASS" if value else "FAIL"


if __name__ == "__main__":
    main()
