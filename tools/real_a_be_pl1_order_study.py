"""Read-only BE follow-through study using REAL_A ledger records and raw aggTrades.

The primary result never substitutes OHLC replay for missing ticks; optional OHLC
HIGH_FIRST/LOW_FIRST output is explicitly labelled sensitivity only.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.position.bot_full_engine import BotFullExitPosition
from tools.cohort_study import _load_config
from tools.real_a_exit_simulator import Tick, _NoopClient, _NoopLogger, _as_float, _exit_config, _parse_timestamp, iter_aggtrade_files
from tools.market_selection_study import MarketCandle, load_candle_cache

START = "2026-08-01T00:00:00-03:00"
END = "2026-08-26T00:00:00-03:00"
REFERENCE_PCT = 0.52


@dataclass(frozen=True)
class BeSeed:
    pair_id: str
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    entry_atr: float
    peak_price: float
    trough_price: float

@dataclass
class FollowThrough:
    seed: BeSeed
    pl1_price: float
    hard_stop_price: float
    reference_price: float
    first_tick: datetime | None = None
    last_tick: datetime | None = None
    resolved_at: datetime | None = None
    resolution: str | None = None
    coverage_error: str | None = None

    def record(self, tick: Tick, max_gap_seconds: float) -> None:
        if self.resolution is not None:
            return
        if self.first_tick is None:
            if (tick.timestamp - self.seed.opened_at).total_seconds() > max_gap_seconds:
                self.coverage_error = "starts too late"
            self.first_tick = tick.timestamp
        elif self.last_tick is not None and (tick.timestamp - self.last_tick).total_seconds() > max_gap_seconds:
            self.coverage_error = "internal gap exceeds limit"
        self.last_tick = tick.timestamp
        if tick.price >= self.pl1_price:
            self.resolution, self.resolved_at = "PL1_FIRST", tick.timestamp
        elif tick.price <= self.hard_stop_price:
            self.resolution, self.resolved_at = "HARD_STOP_FIRST", tick.timestamp

    def finish_at_data_end(self) -> None:
        if self.first_tick is None:
            self.coverage_error = "no ticks"

    @property
    def coverage_complete(self) -> bool:
        return self.coverage_error is None

    def outcome(self) -> str:
        if not self.coverage_complete:
            return "COVERAGE_INCOMPLETE"
        return self.resolution or "UNRESOLVED_AT_DATA_END"


def main() -> None:
    args = _args()
    config = effective_config(_load_config(args.config))
    start, end = _timestamp(args.since), _timestamp(args.until)
    seeds = _load_be_seeds(args.ledger, start, end)
    if not seeds:
        raise SystemExit("No REAL_A BREAKEVEN ledger records found in the opened_at window.")
    states, data_end = _follow_through(seeds, iter_aggtrade_files(args.aggtrades), config, args.max_gap_seconds)
    _print_report(seeds, states, args, data_end)
    if any(not item.coverage_complete for item in states) and args.fallback_1m_cache:
        _print_ohlc_sensitivity(seeds, config, args.fallback_1m_cache)
    if args.output:
        _write_details(args.output, states)
        print(f"detailed CSV: {args.output}")


def _load_be_seeds(ledger: Path, start: datetime, end: datetime) -> list[BeSeed]:
    output: list[BeSeed] = []
    with ledger.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or item.get("phantom") or item.get("shadow_kind"):
                continue
            if item.get("position_type") != "BOT_EXIT" or str(item.get("exit_reason") or "") != "BREAKEVEN":
                continue
            opened = _parse_timestamp(item.get("opened_at")); closed = _parse_timestamp(item.get("closed_at"))
            entry = _as_float(item.get("entry_price")); atr = _as_float(item.get("entry_atr"))
            peak = _as_float(item.get("peak_price")); trough = _as_float(item.get("trough_price"))
            if opened is None or closed is None or not start <= opened < end or None in (entry, atr, peak, trough):
                continue
            if entry <= 0 or atr <= 0 or peak <= 0 or trough <= 0:
                continue
            output.append(BeSeed(str(item.get("pair_id") or "unknown"), opened, closed, entry, atr, peak, trough))
    return sorted(output, key=lambda seed: seed.opened_at)


def _follow_through(seeds: list[BeSeed], ticks: Iterable[Tick], config: dict[str, Any], max_gap_seconds: float) -> tuple[list[FollowThrough], datetime | None]:
    pending = iter(seeds)
    next_seed = next(pending, None)
    active: dict[str, FollowThrough] = {}
    finished: list[FollowThrough] = []
    previous: datetime | None = None
    processed = 0
    for tick in ticks:
        processed += 1
        if previous is not None and tick.timestamp < previous:
            raise ValueError("aggTrade inputs are not chronological after merge.")
        previous = tick.timestamp
        while next_seed is not None and next_seed.opened_at <= tick.timestamp:
            active[next_seed.pair_id] = _state(next_seed, config)
            next_seed = next(pending, None)
        for pair_id, state in list(active.items()):
            state.record(tick, max_gap_seconds)
            if state.resolution is not None:
                finished.append(state)
                del active[pair_id]
        if processed % 100_000 == 0:
            print(f"tick progress: {processed} | active={len(active)} | completed={len(finished)}", flush=True)
    for state in active.values():
        state.finish_at_data_end()
        finished.append(state)
    while next_seed is not None:
        state = _state(next_seed, config)
        state.finish_at_data_end()
        finished.append(state)
        next_seed = next(pending, None)
    return sorted(finished, key=lambda item: item.seed.opened_at), previous


def _state(seed: BeSeed, config: dict[str, Any]) -> FollowThrough:
    """Ask the same position class used at runtime for effective PL1/HS prices."""
    position = BotFullExitPosition(
        pair_id=seed.pair_id, symbol="SOLUSDT", entry_price=seed.entry_price, quantity=1.0,
        entry_order={}, open_ts=seed.opened_at.isoformat(), config=_exit_config(config),
        client=_NoopClient(), logger=_NoopLogger(), entry_atr=seed.entry_atr,
        atr_timeframe="1m", atr_period=14,
    )
    plans = position._profit_lock_candidates(999.0, 999.0)  # Uses runtime formula, including economic floor.
    if not plans or position.hard_stop_price is None:
        raise ValueError(f"Could not derive effective PL1/hard stop for {seed.pair_id}.")
    return FollowThrough(seed, float(plans[0]["effective_trigger"]), float(position.hard_stop_price), seed.entry_price * (1 + REFERENCE_PCT / 100))


def _print_report(seeds: list[BeSeed], states: list[FollowThrough], args: argparse.Namespace, data_end: datetime | None) -> None:
    print("REAL_A BREAKEVEN -> PL1 / HARD_STOP order study | LEDGER + AGGTRADE | READ-ONLY")
    print(f"opened_at window: {_brt(_timestamp(args.since))} -> {_brt(_timestamp(args.until))} (end exclusive)")
    print(f"counterfactual observation: opened_at -> first effective PL1 / hard-stop touch, or end of supplied aggTrade data ({_brt_optional(data_end)}).")
    print(f"Hard stop and PL1 prices are derived through BotFullExitPosition; reference +{REFERENCE_PCT:.2f}% remains ledger-only diagnostic.")
    print(f"BE ledger seeds: {len(seeds)}")
    _print_peak_buckets(seeds)
    complete = [item for item in states if item.coverage_complete]
    incomplete = [item for item in states if not item.coverage_complete]
    print(f"\nAGGTRADE COVERAGE | complete={len(complete)} | incomplete={len(incomplete)}")
    if incomplete:
        print("coverage failures: " + " | ".join(f"{key}={value}" for key, value in sorted(Counter(item.coverage_error for item in incomplete).items())))
        print("Tick-order conclusions below exclude incomplete records and are NOT decisive for the full seed set.")
    _print_resolution(complete)
    _print_resolution_time_buckets(complete)
    print("PREDECLARED READING: the primary count ratio is PL1_FIRST / HARD_STOP_FIRST among resolved, complete paths. Unresolved-at-data-end records do not vote; no result authorizes a runtime change.")


def _print_peak_buckets(seeds: list[BeSeed]) -> None:
    buckets = Counter(_peak_bucket(_pct(seed.peak_price, seed.entry_price)) for seed in seeds)
    above = sum(1 for seed in seeds if _pct(seed.peak_price, seed.entry_price) >= REFERENCE_PCT)
    print("\nLEDGER PEAK DISTRIBUTION (real extrema during the recorded trade life)")
    print(" | ".join(f"{name}={buckets[name]}" for name in ("<0.25%", "0.25-0.52%", "0.52-1.00%", "1.00-2.00%", ">=2.00%")))
    print(f"ledger peak >= +{REFERENCE_PCT:.2f}%: {above}/{len(seeds)}")


def _print_resolution(states: list[FollowThrough]) -> None:
    outcomes = Counter(item.outcome() for item in states)
    pl1, hard = outcomes["PL1_FIRST"], outcomes["HARD_STOP_FIRST"]
    print(f"\nTICK FIRST-TOUCH | {dict(outcomes)}")
    print(f"PL1_FIRST / HARD_STOP_FIRST: {pl1}/{hard} = {pl1 / hard:.3f}" if hard else "PL1_FIRST / HARD_STOP_FIRST: n/a (no hard-stop-first paths)")
    print(f"unresolved at supplied data end: {outcomes['UNRESOLVED_AT_DATA_END']}")


def _print_resolution_time_buckets(states: list[FollowThrough]) -> None:
    labels = ("<=2h", "2-6h", "6-24h", ">24h")
    grouped: dict[str, list[FollowThrough]] = {label: [] for label in labels}
    for item in states:
        if item.resolved_at is None or not item.coverage_complete:
            continue
        hours = (item.resolved_at - item.seed.opened_at).total_seconds() / 3600
        label = "<=2h" if hours <= 2 else "2-6h" if hours <= 6 else "6-24h" if hours <= 24 else ">24h"
        grouped[label].append(item)
    print("\nRESOLUTION TIME | bucket | PL1_FIRST | HARD_STOP_FIRST | PL1/HS")
    for label in labels:
        outcomes = Counter(item.outcome() for item in grouped[label]); pl1, hard = outcomes["PL1_FIRST"], outcomes["HARD_STOP_FIRST"]
        ratio = f"{pl1 / hard:.3f}" if hard else "n/a"
        print(f"{label} | {pl1} | {hard} | {ratio}")


def _write_details(path: Path, states: list[FollowThrough]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("pair_id", "opened_brt", "closed_brt", "entry", "entry_atr", "ledger_peak", "ledger_peak_pct", "ledger_trough", "peak_bucket", "hard_stop_effective", "effective_pl1", "reference_052", "coverage", "resolved_brt", "outcome", "hours_to_resolution"))
        for item in states:
            seed = item.seed
            hours = (item.resolved_at - seed.opened_at).total_seconds() / 3600 if item.resolved_at else None
            writer.writerow((seed.pair_id, _brt(seed.opened_at), _brt(seed.closed_at), f"{seed.entry_price:.8f}", f"{seed.entry_atr:.8f}", f"{seed.peak_price:.8f}", f"{_pct(seed.peak_price, seed.entry_price):+.6f}", f"{seed.trough_price:.8f}", _peak_bucket(_pct(seed.peak_price, seed.entry_price)), f"{item.hard_stop_price:.8f}", f"{item.pl1_price:.8f}", f"{item.reference_price:.8f}", "COMPLETE" if item.coverage_complete else item.coverage_error, _brt_optional(item.resolved_at), item.outcome(), f"{hours:.6f}" if hours is not None else ""))


def _print_ohlc_sensitivity(seeds: list[BeSeed], config: dict[str, Any], cache_path: Path) -> None:
    candles = load_candle_cache(cache_path)
    if not candles:
        print(f"OHLC FALLBACK NOT RUN: no 1m candles in {cache_path}")
        return
    high_first = Counter(_candle_order(seed, config, candles, high_first=True) for seed in seeds)
    low_first = Counter(_candle_order(seed, config, candles, high_first=False) for seed in seeds)
    print("\n1M OHLC SENSITIVITY ONLY — NOT A SUBSTITUTE FOR INCOMPLETE AGGTRADE")
    print(f"cache: {cache_path}; convention HIGH_FIRST: {dict(high_first)}")
    print(f"cache: {cache_path}; convention LOW_FIRST: {dict(low_first)}")
    if high_first == low_first:
        print("Sensitivity result: conventions agree, but the tick result remains incomplete.")
    else:
        print("Sensitivity result: conventions differ; the order conclusion is INDETERMINATE without complete aggTrades.")


def _candle_order(seed: BeSeed, config: dict[str, Any], candles: list[MarketCandle], *, high_first: bool) -> str:
    state = _state(seed, config)
    opened_ms = int(seed.opened_at.timestamp() * 1000)
    relevant = [item for item in candles if item.close_time_ms >= opened_ms]
    if not relevant:
        return "CANDLE_COVERAGE_INCOMPLETE"
    for candle in relevant:
        points = (candle.open, candle.high, candle.low, candle.close) if high_first else (candle.open, candle.low, candle.high, candle.close)
        for price in points:
            if price >= state.pl1_price:
                return "PL1_FIRST"
            if price <= state.hard_stop_price:
                return "HARD_STOP_FIRST"
    return "UNRESOLVED_AT_DATA_END"


def _peak_bucket(value: float) -> str:
    if value < 0.25: return "<0.25%"
    if value < 0.52: return "0.25-0.52%"
    if value < 1.0: return "0.52-1.00%"
    if value < 2.0: return "1.00-2.00%"
    return ">=2.00%"


def _pct(price: float, entry: float) -> float:
    return (price / entry - 1) * 100


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else float("nan")


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def _timestamp(value: str) -> datetime:
    parsed = _parse_timestamp(value)
    if parsed is None: raise ValueError(f"Invalid timestamp: {value}")
    return parsed


def _brt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S BRT")


def _brt_optional(value: datetime | None) -> str:
    return _brt(value) if value else ""


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only REAL_A BREAKEVEN PL1 vs hard-stop order study from ledger and raw aggTrades.")
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "config.yaml")
    parser.add_argument("--aggtrades", type=Path, required=True, nargs="+", help="One or more chronological aggTrade JSONLs; files are merge-streamed.")
    parser.add_argument("--since", default=START); parser.add_argument("--until", default=END)
    parser.add_argument("--max-gap-seconds", type=float, default=30.0)
    parser.add_argument("--fallback-1m-cache", type=Path,
                        help="Optional 1m candle cache: if aggTrade coverage is incomplete, print HIGH_FIRST and LOW_FIRST sensitivity only.")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "real_a_be_pl1_order_details.csv")
    return parser.parse_args()


if __name__ == "__main__":
    main()
