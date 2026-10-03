"""Systemic BE_OFF_CB vs fixed FAST_DROP+EMA replay; diagnostic only."""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.position.bot_full_engine import BotFullExitPosition
from src.monitor.fast_drop_semantics import (
    FAST_LOSS_PCT, FAST_VELOCITY_5M, FAST_EMA, closed_before,
    fast_drop_allowed, fast_drop_values, loss_reached, normal_stop_precedes_fast,
)
from tools.be_off_cb_deterioration_study import build_context_index, context_before
from tools.be_off_cb_fast_drop_audit import TrackingCircuitGuard, month_of
from tools.cohort_study import _load_config
from tools.ge_replay_study import (
    WARMUP_CANDLES, OpenPosition, ReplayResult, ReplayTrade, SignalEvent,
    ceil_ms, generate_ge_signals, load_ge_market_data,
)
from tools.market_bot_replay import (
    MINUTE_MS, NullLogger, ReplayExecutionClient, _bot_exit_config,
    _deduplicate, _passes_spacing, _round_trip_fees_pct,
)
from tools.market_selection_study import BinancePublicClient, MarketCandle

MONTHS = ("2026-06", "2026-07", "2026-08")


@dataclass
class SystemicRun:
    result: ReplayResult
    guard: TrackingCircuitGuard
    simultaneous_by_month: dict[str, int] = field(default_factory=dict)
    admission_audit: list[dict[str, Any]] = field(default_factory=list)
    hs_pause_boundaries: set[int] = field(default_factory=set)
    hs_pause_intervals: list[tuple[int, int]] = field(default_factory=list)


class PostHsPause:
    """Replay-only, fixed one-hour admission pause; never closes a position."""
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.cursor = 0
        self.until = -1
        self.intervals: list[tuple[int, int]] = []

    def update(self, trades: Sequence[ReplayTrade]) -> None:
        for trade in trades[self.cursor:]:
            if self.enabled and trade.exit_reason == 'HARD_STOP':
                end = trade.closed_ms + 60 * MINUTE_MS
                self.until = max(self.until, end)
                self.intervals.append((trade.closed_ms, end))
        self.cursor = len(trades)

    def active(self, at: int) -> bool:
        return self.enabled and at < self.until


def _iso(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, timezone.utc).isoformat()


def _fast_decision(
    position: BotFullExitPosition,
    boundary_ms: int,
    point: float,
    previous: float | None,
    minute_index: dict[int, MarketCandle],
    contexts: Sequence[tuple[int, str, str]],
    evaluated_at_ms: int | None = None,
) -> tuple[bool, float, str, float | None]:
    target, _ = fast_drop_values(position.entry_price, None)
    if not loss_reached(position.entry_price, point):
        return False, target, "UNAVAILABLE", None
    # OHLC has no intraminute tick timestamps. Freeze context at the minute's
    # opening instant for all modeled points: no candle closing during it is visible.
    evaluated_at_ms = boundary_ms-MINUTE_MS if evaluated_at_ms is None else evaluated_at_ms
    reference = minute_index.get(boundary_ms - 5 * MINUTE_MS)
    initial = reference.close if reference and closed_before(reference.close_time_ms, evaluated_at_ms) else None
    target, velocity = fast_drop_values(position.entry_price, initial)
    ema_context, _ = context_before(contexts, evaluated_at_ms)
    eligible = fast_drop_allowed(velocity, ema_context)
    return eligible, target, ema_context, velocity


def _append_trade(position: BotFullExitPosition, opened_ms: int, boundary_ms: int, fees_pct: float, trades: list[ReplayTrade]) -> None:
    exit_price = float(position.exit_price)
    gross = position.pnl_pct(exit_price)
    trades.append(ReplayTrade(opened_ms, boundary_ms, position.entry_price, exit_price, position.highest_price, position.trough_price, gross, gross - fees_pct, str(position.exit_reason)))


def process_candle_systemic(
    positions: Sequence[OpenPosition], trades: list[ReplayTrade], candle: MarketCandle,
    intrabar_path: str, fees_pct: float, fast_enabled: bool,
    fast_evaluated: set[str], minute_index: dict[int, MarketCandle],
    contexts: Sequence[tuple[int, str, str]],
) -> None:
    points = (candle.open, candle.high, candle.low, candle.close) if intrabar_path == "HIGH_FIRST" else (candle.open, candle.low, candle.high, candle.close)
    for replay_position in list(positions):
        position = replay_position.position
        if position.status != "OPEN":
            continue
        previous: float | None = None
        for point in _deduplicate(points):
            normal_stop = position.effective_stop
            stop_crossed = previous is not None and previous > normal_stop and point <= normal_stop
            fast = False
            target = position.entry_price * (1 - FAST_LOSS_PCT / 100)
            if fast_enabled and position.pair_id not in fast_evaluated:
                fast, target, _, velocity = _fast_decision(position, candle.boundary_ms, point, previous, minute_index, contexts,
                                                         evaluated_at_ms=candle.open_time_ms)
                if loss_reached(position.entry_price, point) and velocity is not None:
                    fast_evaluated.add(position.pair_id)
            # On a descending segment, the higher crossed threshold happens first.
            if fast and not normal_stop_precedes_fast(normal_stop, target):
                replay_position.client.current_price = target
                position.on_tick(target, _iso(candle.boundary_ms))
                if position.status == "OPEN":
                    position._close_at_market(target, "FAST_DROP", _iso(candle.boundary_ms), target)
                if position.status == "CLOSED":
                    _append_trade(position, replay_position.opened_ms, candle.boundary_ms, fees_pct, trades)
                    break
            tick = normal_stop if stop_crossed else point
            replay_position.client.current_price = tick
            event = position.on_tick(tick, _iso(candle.boundary_ms))
            previous = point
            if event is not None and position.status == "CLOSED":
                _append_trade(position, replay_position.opened_ms, candle.boundary_ms, fees_pct, trades)
                break


def run_systemic(
    *, name: str, config: dict[str, Any], signals: Sequence[SignalEvent],
    candles: Sequence[MarketCandle], contexts: Sequence[tuple[int, str, str]],
    start_ms: int, end_ms: int, path: str, spread_bps: float, fast_enabled: bool,
    hs_pause_minutes: int = 0,
) -> SystemicRun:
    if hs_pause_minutes not in (0, 60):
        raise ValueError('Only the prespecified 60-minute HS pause is supported')
    groups: dict[int, list[SignalEvent]] = {}
    for event in signals:
        groups.setdefault(event.boundary_ms, []).append(event)
    candle_index = {item.boundary_ms: item for item in candles}
    result = ReplayResult(name=name, lookback=0, signals=len(signals))
    positions: list[OpenPosition] = []
    max_positions = int(config["capital"]["max_open_positions"])
    capital = float(config["capital"]["operational_balance_usdt"])
    notional = capital * float(config["capital"]["trade_size_pct"]) / 100
    max_per_candle = int(config.get("entry", {}).get("max_entries_per_candle", 1))
    entry_cost_bps = exit_cost_bps = spread_bps / 2
    fees_pct = _round_trip_fees_pct(config)
    exit_config = _bot_exit_config(config)
    logger = NullLogger()
    guard = TrackingCircuitGuard(capital, notional)
    fast_evaluated: set[str] = set()
    simultaneous: dict[str, int] = {}
    pause = PostHsPause(hs_pause_minutes == 60)
    paused_boundaries: set[int] = set()
    audit: list[dict[str, Any]] = []
    sequence = 0
    boundary = ceil_ms(start_ms, MINUTE_MS)
    while boundary <= end_ms:
        result.observed_minutes += 1
        candle = candle_index.get(boundary)
        if candle is not None:
            process_candle_systemic(positions, result.trades, candle, path, fees_pct, fast_enabled, fast_evaluated, candle_index, contexts)
        positions[:] = [item for item in positions if item.position.status == "OPEN"]
        if len(positions) >= max_positions:
            result.full_slot_minutes += 1
        admission_allowed = guard.allows(boundary, result)
        pause.update(result.trades)
        pause_active = pause.active(boundary)
        if pause_active:
            paused_boundaries.add(boundary)
        admitted = 0
        for event in groups.get(boundary, []):
            decision = {'at_ms': boundary, 'source_candle': event.signal.source_candle_open_time,
                        'cb_active': not admission_allowed, 'hs_pause_active': pause_active}
            audit.append(decision)
            if not admission_allowed:
                decision['decision'] = 'CB'
                result.blocked_circuit += 1
                continue
            if pause_active:
                decision['decision'] = 'HS_PAUSE'
                decision['otherwise_admissible'] = (len(positions) < max_positions and
                    admitted < max_per_candle and _passes_spacing(config, event.signal, positions))
                continue
            if len(positions) >= max_positions:
                decision['decision'] = 'CAPACITY'
                result.blocked_slots += 1
                continue
            if admitted >= max_per_candle:
                decision['decision'] = 'CANDLE_LIMIT'
                result.blocked_candle_limit += 1
                continue
            if not _passes_spacing(config, event.signal, positions):
                decision['decision'] = 'SPACING'
                result.blocked_spacing += 1
                continue
            sequence += 1
            entry_price = event.signal.price * (1 + entry_cost_bps / 10_000)
            quantity = notional / entry_price
            client = ReplayExecutionClient(exit_cost_bps)
            position = BotFullExitPosition(
                pair_id=f"{name.lower()}-{sequence}", symbol=event.signal.symbol,
                entry_price=entry_price, quantity=quantity, entry_order={"replay": True},
                open_ts=_iso(boundary), config=exit_config, client=client, logger=logger,
                entry_atr=event.signal.entry_atr, atr_timeframe=event.signal.atr_timeframe,
                atr_period=event.signal.atr_period, position_id=sequence,
                source_candle_open_time=event.signal.source_candle_open_time,
                position_notional_usdt=notional,
            )
            positions.append(OpenPosition(position, client, boundary, notional))
            result.entry_times.append((boundary, entry_price))
            decision['decision'] = 'ADMITTED'
            admitted += 1
            result.max_simultaneous_positions = max(result.max_simultaneous_positions, len(positions))
        month = month_of(boundary)
        simultaneous[month] = max(simultaneous.get(month, 0), len(positions))
        boundary += MINUTE_MS
    result.open_positions = positions
    return SystemicRun(result, guard, simultaneous, audit, paused_boundaries, pause.intervals)


def _exit_bucket(reason: str) -> str:
    upper = reason.upper()
    if "FAST_DROP" in upper: return "FAST_DROP"
    if "HARD_STOP" in upper: return "HS"
    if "PROFIT_LOCK" in upper: return "PL"
    if "TRAIL" in upper: return "TRAIL"
    return upper


def metrics(run: SystemicRun, notional: float, month: str | None) -> dict[str, Any]:
    trades = [item for item in run.result.trades if month is None or month_of(item.closed_ms) == month]
    trades.sort(key=lambda item: item.closed_ms)
    values = [item.net_pct * notional / 100 for item in trades]
    equity = peak = dd = 0.0
    for value in values:
        equity += value; peak = max(peak, equity); dd = max(dd, peak - equity)
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    exits = Counter(_exit_bucket(item.exit_reason) for item in trades)
    paused = [item for item in run.guard.paused_boundaries if month is None or month_of(item) == month]
    crises = [item for item in run.guard.crisis_starts if month is None or month_of(item) == month]
    blocked = sum(1 for boundary in paused if boundary in _SIGNAL_BOUNDARIES)
    ages = [(item.closed_ms - item.opened_ms) / 60_000 for item in trades]
    return {
        "closed": len(trades), "net": sum(values), "net_trade": sum(values) / len(values) if values else None,
        "pf": gains / losses if losses else math.inf if gains else None, "dd": dd,
        "HS": exits["HS"], "PL": exits["PL"], "TRAIL": exits["TRAIL"], "FAST": exits["FAST_DROP"],
        "crises": len(crises), "cooldown": len(paused) / 60, "blocked": blocked,
        "max_sim": run.result.max_simultaneous_positions if month is None else run.simultaneous_by_month.get(month, 0),
        "median_age": statistics.median(ages) if ages else None,
    }


_SIGNAL_BOUNDARIES: set[int] = set()


def _fmt(value: float | None, digits: int = 4) -> str:
    if value is None: return "N/A"
    if math.isinf(value): return "inf"
    return f"{value:.{digits}f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--cache-dir", default="data/studies/be_off_cb_deterioration/klines")
    parser.add_argument("--offline", action="store_true", default=True)
    args = parser.parse_args()
    config = deepcopy(effective_config(_load_config(Path(args.config))))
    config.setdefault("risk", {})["breakeven"] = {"mode": "off"}
    capital = float(config["capital"]["operational_balance_usdt"])
    notional = capital * float(config["capital"]["trade_size_pct"]) / 100
    spread = float(config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0))
    start = datetime.fromisoformat("2026-06-01T00:00:00-03:00").astimezone(timezone.utc)
    end = datetime.fromisoformat("2026-09-01T00:00:00-03:00").astimezone(timezone.utc)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
    client = BinancePublicClient(str(config.get("market_data", {}).get("rest_url") or "https://api.binance.com"), 20)
    cache = Path(args.cache_dir)
    candles = {interval: load_ge_market_data(client, str(config.get("symbol") or "SOLUSDT"), interval, start_ms - WARMUP_CANDLES * 15 * MINUTE_MS, end_ms, cache, True) for interval in ("1m", "5m", "15m")}
    signals, _ = generate_ge_signals(config, candles["1m"], candles["5m"], candles["15m"], start_ms, end_ms, 0)
    global _SIGNAL_BOUNDARIES
    _SIGNAL_BOUNDARIES = {item.boundary_ms for item in signals}
    contexts = build_context_index(candles["5m"])
    print("BE_OFF_CB vs BE_OFF_CB_FAST_DROP_EMA | SYSTEMIC FULL-ENGINE REPLAY | READ-ONLY")
    print("FAST_DROP fixed | loss<=-0.50% | adverse velocity 5m<=-0.10%/min | EMA in {SHO,BEA}")
    print("arm | window | closed | net $ | net/trade $ | PF | DD $ | HS | PL | TRAIL | FAST | CB crises | cooldown h | blocked CB | max sim | median age min")
    for path in ("HIGH_FIRST", "LOW_FIRST"):
        runs = (
            ("BE_OFF_CB", run_systemic(name=f"CONTROL_{path}", config=config, signals=signals, candles=candles["1m"], contexts=contexts, start_ms=start_ms, end_ms=end_ms, path=path, spread_bps=spread, fast_enabled=False)),
            ("FAST_DROP_EMA", run_systemic(name=f"FAST_{path}", config=config, signals=signals, candles=candles["1m"], contexts=contexts, start_ms=start_ms, end_ms=end_ms, path=path, spread_bps=spread, fast_enabled=True)),
        )
        print(f"\n{path}")
        for arm, run in runs:
            for window in (*MONTHS, "AGG"):
                row = metrics(run, notional, None if window == "AGG" else window)
                print(f"{arm} | {window} | {row['closed']} | {row['net']:+.4f} | {_fmt(row['net_trade'])} | {_fmt(row['pf'])} | {row['dd']:.4f} | {row['HS']} | {row['PL']} | {row['TRAIL']} | {row['FAST']} | {row['crises']} | {row['cooldown']:.2f} | {row['blocked']} | {row['max_sim']} | {_fmt(row['median_age'], 1)}")
    print("\nLIMITS | in-sample OHLC replay, 1-minute path sensitivity; FAST_DROP is systemic inside each arm. Runtime/config/state were not modified.")


if __name__ == "__main__":
    main()
