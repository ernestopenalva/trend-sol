"""Read-only historical comparison of REAL_A and the two current context shadows."""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.indicators.indicators import ema
from src.monitor.context_predicates import passes_dmi15_trajectory, passes_slow_ge45
from src.monitor.context_shadow import ContextGateEntryEngine
from src.monitor.entry_engine import EntryEngine
from src.monitor.market_context import MarketContextEngine
from tools.cohort_study import _load_config
from tools.ge_replay_study import SignalEvent, _kline_payload, load_ge_market_data, run_universe
from tools.market_bot_replay import MINUTE_MS, NullLogger, ReplayResult
from tools.market_selection_study import BinancePublicClient, MarketCandle

START = "2026-08-01T00:00:00-03:00"
END = "2026-08-26T00:00:00-03:00"
WARMUP_DAYS = 7
ARMS = ("REAL_A", "DMI_CONTEXT", "SLOW_GE_CONTEXT")
LOW_SAMPLE = 30


def main() -> None:
    args = _args()
    config = effective_config(_load_config(Path(args.config)))
    _validate(config)
    start, end = _ts(args.since), _ts(args.until)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
    data_start = start_ms - WARMUP_DAYS * 24 * 60 * MINUTE_MS
    client = BinancePublicClient(str(config["market_data"]["rest_url"]), args.http_timeout_seconds)
    cache = Path(args.cache_dir)
    candles = {tf: load_ge_market_data(client, str(config["symbol"]), tf, data_start, end_ms, cache, args.offline) for tf in ("1m", "5m", "15m")}
    print("TREND-SOL | historical EMA context diagnostic | READ-ONLY")
    print(f"window: {_brt(start)} -> {_brt(end)} (end exclusive) | data: 1m={len(candles['1m'])}, 5m={len(candles['5m'])}, 15m={len(candles['15m'])}")
    print("Prior expectation: REAL_A likely leads; shadows reduce volume; small-bucket inversions are diagnostic only.")
    contexts = _ema_contexts(candles["5m"], start_ms, end_ms)
    if not contexts:
        raise SystemExit("No stable EMA100 5m context inside the requested window.")
    print("EMA source: closed 5m candles; src.indicators.indicators.ema; EMA100 SMA seed then alpha=2/(101).")
    print("Predicates: GE15 5m[t]>5m[t-3]; DMI trajectory shared runtime predicate; SLOW_GE45 15m[t]>15m[t-3].")
    _print_time_fraction(contexts)
    signals = _signals(config, candles, start_ms, end_ms)
    results = {name: run_universe(name=name, lookback=3, config=config, signals=signals[name], execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=args.intrabar_path, round_trip_spread_bps=args.round_trip_spread_bps) for name in ARMS}
    entry_context = {name: {event.boundary_ms: _context_at(contexts, event.boundary_ms) for event in signals[name]} for name in ARMS}
    exit_context = {name: {trade.opened_ms: _context_at(contexts, trade.closed_ms) for trade in results[name].trades} for name in ARMS}
    print("\nAGGREGATE (closed trades only)")
    _print_rows(results, entry_context, key="ALL")
    print("\nANALYSIS A | STACK at entry")
    _print_rows(results, entry_context, key="stack")
    _print_deltas(results, entry_context, "stack")
    print("\nANALYSIS B | SCORE at entry")
    _print_rows(results, entry_context, key="score")
    _print_deltas(results, entry_context, "score")
    print("\nDIAGNOSTIC | exact EMA direction pattern at entry")
    _print_patterns(results, entry_context)
    print("\nDIAGNOSTIC | entry stack -> exit stack (exit context is not a future rule)")
    _print_exit_transitions(results, entry_context, exit_context)
    print("\nDIAGNOSTIC | score transition at entry (previous 5m score -> current)")
    _print_score_transitions(results, entry_context)
    _conclusion(results, entry_context)
    print("\nLIMITATIONS: deterministic OHLC HIGH_FIRST/LOW_FIRST replay comparison, not Testnet-fill reconstruction. No runtime/config/shadow state was written.")


def _signals(config: Dict[str, Any], candles: Dict[str, Sequence[MarketCandle]], start_ms: int, end_ms: int) -> dict[str, list[SignalEvent]]:
    symbol = str(config["symbol"])
    a = EntryEngine(symbol, config, NullLogger())  # type: ignore[arg-type]
    feed = EntryEngine(symbol, config, NullLogger())  # type: ignore[arg-type]
    context = MarketContextEngine(feed, config)
    dmi = ContextGateEntryEngine(symbol, config, NullLogger(), lambda _e, snapshot: passes_dmi15_trajectory(snapshot.get("tf_5m", {}) if isinstance(snapshot, dict) else {}), lambda _missing: None)  # type: ignore[arg-type]
    slow = ContextGateEntryEngine(symbol, config, NullLogger(), lambda engine, _snapshot: passes_slow_ge45(engine._candles_for("15m")), lambda _missing: None)  # type: ignore[arg-type]
    engines = {"REAL_A": a, "DMI_CONTEXT": dmi, "SLOW_GE_CONTEXT": slow}
    indices = {"5m": 0, "15m": 0}
    output = {name: [] for name in ARMS}
    for processed, candle in enumerate(candles["1m"], start=1):
        boundary = candle.boundary_ms
        if boundary > end_ms:
            break
        for tf in ("15m", "5m"):
            source = candles[tf]
            while indices[tf] < len(source) and source[indices[tf]].boundary_ms <= boundary:
                item, payload = source[indices[tf]], _kline_payload(source[indices[tf]])
                feed.on_kline(f"{symbol.lower()}@kline_{tf}", payload)
                snapshot = context.refresh() if tf == "5m" else context.latest
                dmi.set_context_snapshot(snapshot)
                for engine in engines.values():
                    engine.on_kline(f"{symbol.lower()}@kline_{tf}", payload)
                indices[tf] += 1
        snapshot = context.latest
        dmi.set_context_snapshot(snapshot)
        payload = _kline_payload(candle)
        for name, engine in engines.items():
            signal = engine.on_kline(f"{symbol.lower()}@kline_1m", payload)
            if signal is not None and start_ms <= boundary <= end_ms:
                output[name].append(SignalEvent(boundary, signal))
        if processed % 5_000 == 0:
            print(f"signal progress: {processed}/{len(candles['1m'])} 1m candles", flush=True)
    print("raw pipeline signals | " + " | ".join(f"{name}={len(output[name])}" for name in ARMS))
    return output


def _ema_contexts(candles: Sequence[MarketCandle], start_ms: int, end_ms: int) -> dict[int, dict[str, Any]]:
    closes = [item.close for item in candles]
    series = {period: ema(closes, period) for period in (20, 50, 100)}
    output: dict[int, dict[str, Any]] = {}
    previous_score: float | None = None
    for index, candle in enumerate(candles):
        if not start_ms <= candle.boundary_ms <= end_ms or index < 3:
            continue
        current = {period: series[period][index] for period in series}
        before = {period: series[period][index - 3] for period in series}
        if any(value is None for value in (*current.values(), *before.values())):
            continue
        directions = {period: "UP" if float(current[period]) > float(before[period]) else "DOWN" if float(current[period]) < float(before[period]) else "FLAT" for period in series}
        rising = sum(value == "UP" for value in directions.values())
        score, label = {0: (0.0, "FALLING"), 1: (3.3, "MOSTLY_FALLING"), 2: (6.7, "MOSTLY_RISING"), 3: (10.0, "RISING")}[rising]
        stack = "BULLISH_STACK" if current[20] > current[50] > current[100] else "BEARISH_STACK" if current[20] < current[50] < current[100] else "MIXED"
        output[candle.boundary_ms] = {"stack": stack, "score": score, "score_label": label, "previous_score": previous_score, "directions": directions, "ema": current, "ema_t3": before, "deltas_pct": {period: (float(current[period]) / float(before[period]) - 1) * 100 for period in series}}
        previous_score = score
    return output


def _context_at(contexts: dict[int, dict[str, Any]], timestamp: int) -> dict[str, Any] | None:
    choices = [value for at, value in contexts.items() if at <= timestamp]
    return choices[-1] if choices else None


def _print_time_fraction(contexts: dict[int, dict[str, Any]]) -> None:
    counts = Counter(item["stack"] for item in contexts.values())
    total = len(contexts)
    print("STACK time fraction | " + " | ".join(f"{key}={counts[key] / total:.1%}" for key in ("BULLISH_STACK", "MIXED", "BEARISH_STACK")))


def _print_rows(results: dict[str, ReplayResult], contexts: dict[str, dict[int, dict[str, Any] | None]], key: str) -> None:
    buckets = ["ALL"] if key == "ALL" else (["BULLISH_STACK", "MIXED", "BEARISH_STACK"] if key == "stack" else [0.0, 3.3, 6.7, 10.0])
    print("strategy | bucket | trades | gross total | gross/trade | net/trade | HS rate | PL rate | TRAIL rate | age mean/median h | sample")
    for arm in ARMS:
        for bucket in buckets:
            trades = [trade for trade in results[arm].trades if key == "ALL" or (contexts[arm].get(trade.opened_ms) or {}).get(key) == bucket]
            row = _metrics(trades)
            print(f"{arm} | {bucket} | {row['n']} | {row['gross']:+.3f}% | {row['gross_trade']:+.3f}% | {row['net_trade']:+.3f}% | {row['hs']:.1%} | {row['pl']:.1%} | {row['trail']:.1%} | {row['age_mean']:.2f}/{row['age_median']:.2f} | {'LOW SAMPLE' if row['n'] < LOW_SAMPLE else '30+'}")


def _print_deltas(results: dict[str, ReplayResult], contexts: dict[str, dict[int, dict[str, Any] | None]], key: str) -> None:
    buckets = ("BULLISH_STACK", "MIXED", "BEARISH_STACK") if key == "stack" else (0.0, 3.3, 6.7, 10.0)
    print("relative gross/trade vs REAL_A | bucket | DMI-REAL_A | SLOW_GE-REAL_A")
    for bucket in buckets:
        rows = {arm: _metrics([trade for trade in results[arm].trades if (contexts[arm].get(trade.opened_ms) or {}).get(key) == bucket]) for arm in ARMS}
        print(f"{bucket} | {rows['DMI_CONTEXT']['gross_trade'] - rows['REAL_A']['gross_trade']:+.3f}% | {rows['SLOW_GE_CONTEXT']['gross_trade'] - rows['REAL_A']['gross_trade']:+.3f}%")


def _print_patterns(results: dict[str, ReplayResult], contexts: dict[str, dict[int, dict[str, Any] | None]]) -> None:
    for arm in ARMS:
        grouped: dict[str, list[Any]] = {}
        for trade in results[arm].trades:
            item = contexts[arm].get(trade.opened_ms)
            if item:
                pattern = " | ".join(f"EMA{period} {'UP' if item['directions'][period] == 'UP' else 'DOWN' if item['directions'][period] == 'DOWN' else 'FLAT'}" for period in (20, 50, 100))
                grouped.setdefault(pattern, []).append(trade)
        for pattern, trades in sorted(grouped.items()):
            row = _metrics(trades)
            print(f"{arm} | {pattern} | n={row['n']} | gross/trade={row['gross_trade']:+.3f}% | HS={row['hs']:.1%} | TRAIL={row['trail']:.1%} | {'LOW SAMPLE' if row['n'] < LOW_SAMPLE else '30+'}")


def _print_exit_transitions(results: dict[str, ReplayResult], entry: dict[str, dict[int, dict[str, Any] | None]], exit_: dict[str, dict[int, dict[str, Any] | None]]) -> None:
    for arm in ARMS:
        counts = Counter(f"{(entry[arm].get(t.opened_ms) or {}).get('stack','n/a')} -> {(exit_[arm].get(t.opened_ms) or {}).get('stack','n/a')}" for t in results[arm].trades)
        print(arm + " | " + " | ".join(f"{key}: {value}" for key, value in sorted(counts.items())))


def _print_score_transitions(results: dict[str, ReplayResult], contexts: dict[str, dict[int, dict[str, Any] | None]]) -> None:
    for arm in ARMS:
        counts = Counter(f"{item.get('previous_score')} -> {item.get('score')}" for trade in results[arm].trades if (item := contexts[arm].get(trade.opened_ms)))
        print(arm + " | " + " | ".join(f"{key}: {value}" for key, value in sorted(counts.items())))


def _metrics(trades: Sequence[Any]) -> dict[str, float | int]:
    n = len(trades); gross = [item.gross_pct for item in trades]; net = [item.net_pct for item in trades]; reasons = Counter(item.exit_reason for item in trades); ages = [item.age_seconds / 3600 for item in trades]
    return {"n": n, "gross": sum(gross), "gross_trade": sum(gross) / n if n else 0.0, "net_trade": sum(net) / n if n else 0.0, "hs": reasons["HARD_STOP"] / n if n else 0.0, "pl": reasons["PROFIT_LOCK"] / n if n else 0.0, "trail": reasons["TRAILING"] / n if n else 0.0, "age_mean": statistics.fmean(ages) if ages else 0.0, "age_median": statistics.median(ages) if ages else 0.0}


def _conclusion(results: dict[str, ReplayResult], contexts: dict[str, dict[int, dict[str, Any] | None]]) -> None:
    print("\nFINAL ANSWERS (mechanical; any bucket with n<30 is LOW SAMPLE and cannot support promotion)")
    for key, buckets in (("stack", ("BULLISH_STACK", "MIXED", "BEARISH_STACK")), ("score", (0.0, 3.3, 6.7, 10.0))):
        supported = []
        for bucket in buckets:
            rows = {arm: _metrics([t for t in results[arm].trades if (contexts[arm].get(t.opened_ms) or {}).get(key) == bucket]) for arm in ARMS}
            for arm in ("DMI_CONTEXT", "SLOW_GE_CONTEXT"):
                if min(rows["REAL_A"]["n"], rows[arm]["n"]) >= LOW_SAMPLE and rows[arm]["gross_trade"] > rows["REAL_A"]["gross_trade"]:
                    supported.append(f"{arm}/{bucket}")
        print(f"specialization by {key}: " + (", ".join(supported) if supported else "none meeting predeclared 30+ rule"))
    print("No evidence here alone authorizes EMA->strategy switching or STACK×SCORE crossing.")


def _validate(config: Dict[str, Any]) -> None:
    gate = config.get("trend_gate", {})
    if str(config.get("entry", {}).get("timeframe")) != "1m" or str(gate.get("candle_interval")) != "5m" or int(gate.get("lookback_candles", 0)) != 3:
        raise SystemExit("Requires current intraday REAL_A GE15 configuration.")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only EMA-context backtest for the three current arms.")
    parser.add_argument("--since", default=START); parser.add_argument("--until", default=END)
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--cache-dir", default="data/studies/real_a_dmi15_trajectory_regime_backtest/klines")
    parser.add_argument("--offline", action="store_true", default=True)
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--intrabar-path", choices=("HIGH_FIRST", "LOW_FIRST"), default="HIGH_FIRST")
    parser.add_argument("--round-trip-spread-bps", type=float, default=5.0)
    return parser.parse_args()


def _ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def _brt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")


if __name__ == "__main__":
    main()
