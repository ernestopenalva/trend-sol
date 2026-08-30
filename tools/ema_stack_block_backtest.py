"""Read-only, path-dependent test of blocking REAL_A entries by EMA stack."""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from tools.cohort_study import _load_config
from tools.ema_context_historical_backtest import WARMUP_DAYS, _context_at, _ema_contexts, _signals, _ts
from tools.ge_replay_study import MATCH_TOLERANCE_MS, ReplayEntry, ReplayResult, entry_overlap, load_ge_market_data, run_universe
from tools.market_bot_replay import MINUTE_MS
from tools.market_selection_study import BinancePublicClient

START = "2026-08-01T00:00:00-03:00"
END = "2026-08-26T00:00:00-03:00"
ARMS = ("CONTROL", "BLOCK_MIXED", "BLOCK_BEARISH")
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
    contexts = _ema_contexts(candles["5m"], start_ms, end_ms)
    if not contexts:
        raise SystemExit("No stable EMA100 stack context inside the requested window.")
    print("TREND-SOL | independent REAL_A EMA STACK blocking test | READ-ONLY")
    print(f"window: {_brt(start)} -> {_brt(end)} (end exclusive); warmup: {WARMUP_DAYS}d; HIGH_FIRST; modeled spread={args.round_trip_spread_bps:.1f} bps")
    print("GE15: high_5m[t] > high_5m[t-3] AND low_5m[t] > low_5m[t-3].")
    print("STACK uses only closed 5m EMA values available at entry: BULLISH=EMA20>EMA50>EMA100; BEARISH=reverse; MIXED=otherwise.")
    print("Predeclared prediction: BLOCK_MIXED may improve; BLOCK_BEARISH is the negative control and is expected to worsen.")
    raw = _signals(config, candles, start_ms, end_ms)["REAL_A"]
    stack_for = {event.boundary_ms: (_context_at(contexts, event.boundary_ms) or {}).get("stack", "UNAVAILABLE") for event in raw}
    signals = {
        "CONTROL": raw,
        "BLOCK_MIXED": [event for event in raw if stack_for[event.boundary_ms] != "MIXED"],
        "BLOCK_BEARISH": [event for event in raw if stack_for[event.boundary_ms] != "BEARISH_STACK"],
    }
    stack_blocks = {
        "CONTROL": Counter(),
        "BLOCK_MIXED": Counter(stack_for[event.boundary_ms] for event in raw if stack_for[event.boundary_ms] == "MIXED"),
        "BLOCK_BEARISH": Counter(stack_for[event.boundary_ms] for event in raw if stack_for[event.boundary_ms] == "BEARISH_STACK"),
    }
    results = {
        arm: run_universe(name=arm, lookback=3, config=config, signals=signals[arm], execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=args.intrabar_path, round_trip_spread_bps=args.round_trip_spread_bps)
        for arm in ARMS
    }
    print("\nAGGREGATE (closed-trade PnL; open positions are separate)")
    print("strategy | raw signals | admitted | closed | open end | gross total | gross/trade | net total | net/trade | HS | HS rate | BE | PL | TRAIL | TRAIL rate | avg/median age h | max simultaneous | capacity blocks | same-5m | stack blocks")
    for arm in ARMS:
        row = _metrics(results[arm].trades)
        print(f"{arm} | {len(raw)} | {len(results[arm].entry_times)} | {row['n']} | {len(results[arm].open_positions)} | {row['gross']:+.3f}% | {row['gross_trade']:+.3f}% | {row['net']:+.3f}% | {row['net_trade']:+.3f}% | {row['hs']} | {row['hs_rate']:.1%} | {row['be']} | {row['pl']} | {row['trail']} | {row['trail_rate']:.1%} | {row['age_mean']:.2f}/{row['age_median']:.2f} | {results[arm].max_simultaneous_positions} | {results[arm].blocked_slots} | {results[arm].blocked_candle_limit} | {sum(stack_blocks[arm].values())}")
    _print_deltas(results)
    print("\nSTACK BLOCKS (pre-admission evaluation; a block is counted even if that arm was already full, so this is not a counterfactual capacity admission count)")
    for arm in ("BLOCK_MIXED", "BLOCK_BEARISH"):
        print(f"{arm} | blocked stack={dict(stack_blocks[arm])} | total={sum(stack_blocks[arm].values())}")
    print("\nADMITTED ENTRY COMPOSITION BY STACK")
    for arm in ARMS:
        composition = Counter((_context_at(contexts, opened) or {}).get("stack", "UNAVAILABLE") for opened, _ in results[arm].entry_times)
        print(f"{arm} | BULLISH={composition['BULLISH_STACK']} | MIXED={composition['MIXED']} | BEARISH={composition['BEARISH_STACK']} | unavailable={composition['UNAVAILABLE']}")
    _print_overlap("CONTROL vs BLOCK_MIXED", results["CONTROL"], results["BLOCK_MIXED"])
    _print_overlap("CONTROL vs BLOCK_BEARISH", results["CONTROL"], results["BLOCK_BEARISH"])
    _conclusion(results)
    if args.export_control_trades:
        _export_control_trades(Path(args.export_control_trades), results["CONTROL"], contexts)
    print("\nOOS CANDIDATE (not run): 05/06/2026 00:00 BRT -> 01/08/2026 00:00 BRT, 57 days; 7d warmup from 29/05 00:00 BRT is fully available in the continuous cache.")
    print("No runtime, configuration, shadow, ledger, or production decision was modified.")


def _metrics(trades: Sequence[Any]) -> dict[str, float | int]:
    gross = [item.gross_pct for item in trades]
    net = [item.net_pct for item in trades]
    reasons = Counter(item.exit_reason for item in trades)
    ages = [item.age_seconds / 3600 for item in trades]
    n = len(trades)
    return {
        "n": n, "gross": sum(gross), "gross_trade": sum(gross) / n if n else 0.0,
        "net": sum(net), "net_trade": sum(net) / n if n else 0.0,
        "hs": reasons["HARD_STOP"], "be": reasons["BREAKEVEN"], "pl": reasons["PROFIT_LOCK"], "trail": reasons["TRAILING"],
        "hs_rate": reasons["HARD_STOP"] / n if n else 0.0, "trail_rate": reasons["TRAILING"] / n if n else 0.0,
        "age_mean": statistics.fmean(ages) if ages else 0.0, "age_median": statistics.median(ages) if ages else 0.0,
    }


def _print_deltas(results: dict[str, ReplayResult]) -> None:
    control = _metrics(results["CONTROL"].trades)
    print("\nDELTAS VS CONTROL | arm | gross/trade | net/trade | HS rate | TRAIL rate | closed trades")
    for arm in ("BLOCK_MIXED", "BLOCK_BEARISH"):
        row = _metrics(results[arm].trades)
        print(f"{arm} | {float(row['gross_trade']) - float(control['gross_trade']):+.3f}pp | {float(row['net_trade']) - float(control['net_trade']):+.3f}pp | {float(row['hs_rate']) - float(control['hs_rate']):+.1%} | {float(row['trail_rate']) - float(control['trail_rate']):+.1%} | {int(row['n']) - int(control['n']):+d}")


def _print_overlap(title: str, first: ReplayResult, second: ReplayResult) -> None:
    pairs, only_first, only_second = entry_overlap(first.entries(), second.entries(), MATCH_TOLERANCE_MS)
    groups = (("common", [left for left, _ in pairs]), ("only CONTROL", only_first), ("only filtered arm", only_second))
    print(f"\nENTRY OVERLAP | {title} | nearest one-to-one match within {MATCH_TOLERANCE_MS // 1000}s")
    print("group | entries | closed | gross/trade | net/trade | HS rate | TRAIL rate")
    for label, entries in groups:
        closed = [item for item in entries if item.gross_pct is not None and item.net_pct is not None]
        row = _entry_metrics(closed)
        print(f"{label} | {len(entries)} | {row['n']} | {row['gross_trade']:+.3f}% | {row['net_trade']:+.3f}% | {row['hs_rate']:.1%} | {row['trail_rate']:.1%}")


def _entry_metrics(entries: Sequence[ReplayEntry]) -> dict[str, float | int]:
    gross = [float(item.gross_pct) for item in entries if item.gross_pct is not None]
    net = [float(item.net_pct) for item in entries if item.net_pct is not None]
    reasons = Counter(item.exit_reason for item in entries)
    n = len(gross)
    return {"n": n, "gross_trade": sum(gross) / n if n else 0.0, "net_trade": sum(net) / n if n else 0.0, "hs_rate": reasons["HARD_STOP"] / n if n else 0.0, "trail_rate": reasons["TRAILING"] / n if n else 0.0}


def _conclusion(results: dict[str, ReplayResult]) -> None:
    a, b, c = (_metrics(results[arm].trades) for arm in ARMS)
    promising = float(b["gross_trade"]) > float(a["gross_trade"]) and float(b["net_trade"]) > float(a["net_trade"]) and float(b["trail_rate"]) >= float(a["trail_rate"]) and float(b["hs_rate"]) <= float(a["hs_rate"])
    negative_control = float(c["gross_trade"]) < float(a["gross_trade"])
    print("\nDIRECT READING")
    print(f"BLOCK_MIXED passes the predeclared screen: {'YES' if promising else 'NO'}")
    print(f"BLOCK_BEARISH worsens gross/trade as the negative-control prediction: {'YES' if negative_control else 'NO'}")
    print("Path dependency must be read from the common/exclusive-entry tables above; no result here authorizes a runtime or shadow change.")


def _export_control_trades(path: Path, result: ReplayResult, contexts: dict[int, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="|")
        writer.writerow(("opened", "closed", "age", "entry", "peak", "trough", "exit", "gross", "net", "exit reason", "context na abertura", "context na saida"))
        for trade in sorted(result.trades, key=lambda item: item.opened_ms):
            entry_context = (_context_at(contexts, trade.opened_ms) or {}).get("stack", "UNAVAILABLE")
            exit_context = (_context_at(contexts, trade.closed_ms) or {}).get("stack", "UNAVAILABLE")
            writer.writerow((_stamp(trade.opened_ms), _stamp(trade.closed_ms), _age(trade.age_seconds), f"{trade.entry_price:.4f}", f"{trade.peak_price:.4f}", f"{trade.trough_price:.4f}", f"{trade.exit_price:.4f}", f"{trade.gross_pct:+.3f}%", f"{trade.net_pct:+.3f}%", trade.exit_reason, entry_context, exit_context))
    print(f"CONTROL trade export: {path} ({len(result.trades)} closed trades)")


def _stamp(value_ms: int) -> str:
    return datetime.fromtimestamp(value_ms / 1000, timezone.utc).astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S BRT")


def _age(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _validate(config: dict[str, Any]) -> None:
    gate = config.get("trend_gate", {})
    if str(config.get("entry", {}).get("timeframe")) != "1m" or str(gate.get("candle_interval")) != "5m" or int(gate.get("lookback_candles", 0)) != 3:
        raise SystemExit("Requires current intraday REAL_A GE15 configuration.")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only independent REAL_A EMA-stack blocking test.")
    parser.add_argument("--since", default=START); parser.add_argument("--until", default=END)
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--cache-dir", default="data/studies/real_a_dmi15_trajectory_regime_backtest/klines")
    parser.add_argument("--offline", action="store_true", default=True)
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--intrabar-path", choices=("HIGH_FIRST", "LOW_FIRST"), default="HIGH_FIRST")
    parser.add_argument("--round-trip-spread-bps", type=float, default=5.0)
    parser.add_argument("--export-control-trades", help="Pipe-delimited closed CONTROL/REAL_A trades, in BRT.")
    return parser.parse_args()


def _brt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")


if __name__ == "__main__":
    main()
