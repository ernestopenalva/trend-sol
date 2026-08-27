"""Read-only, independent A/B/C REAL_A ladder replay across pre-labelled regimes."""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger
from tools.cohort_study import _load_config
from tools.ge_replay_study import (
    MATCH_TOLERANCE_MS,
    WARMUP_CANDLES,
    generate_ge_signals,
    load_ge_market_data,
    real_bot_b_records,
    validate_replay,
)
from tools.market_bot_replay import MINUTE_MS, ReplayResult, _round_trip_fees_pct
from tools.market_selection_study import BinancePublicClient
from tools.ge_replay_study import run_universe


@dataclass(frozen=True)
class Regime:
    name: str
    start: datetime
    end: datetime  # exclusive
    in_sample: bool


ARMS = ("A", "B", "C")


def main() -> None:
    args = _parse_args()
    raw = _load_config(Path(args.config))
    config = effective_config(raw)
    _validate_context(config)
    path = args.intrabar_path.upper()
    spread_bps = _spread_bps(args, config)
    validation_start = _timestamp(args.validation_since)
    validation_end = _timestamp(args.validation_until)
    active_regimes = tuple(regime for regime in REGIMES if args.regimes == "all" or regime.name.lower() in args.regimes.split(","))
    if not active_regimes:
        raise SystemExit("--regimes must select at least one of lateral, alta, baixa")
    if validation_end <= validation_start:
        raise SystemExit("--validation-until must be after --validation-since")

    warmup = WARMUP_CANDLES * 15 * MINUTE_MS
    earliest = min(int(regime.start.timestamp() * 1000) for regime in active_regimes)
    latest = max(int(regime.end.timestamp() * 1000) - 1 for regime in active_regimes)
    data_start_ms = min(earliest, int(validation_start.timestamp() * 1000)) - warmup
    data_end_ms = max(latest, int(validation_end.timestamp() * 1000) - 1)
    client = BinancePublicClient(
        str(args.market_data_url or config.get("market_data", {}).get("rest_url") or "https://api.binance.com"),
        int(args.http_timeout_seconds),
    )
    symbol = str(config.get("symbol") or "SOLUSDT")
    cache_dir = Path(args.cache_dir)
    candles = {
        interval: load_ge_market_data(client, symbol, interval, data_start_ms, data_end_ms, cache_dir, bool(args.offline))
        for interval in ("1m", "5m", "15m")
    }
    arm_configs = _arm_configs(config)

    print("TREND-SOL | REAL_A independent A/B/C ladder backtest (read-only)")
    print("Instrument: same EntryEngine + BotFullExitPosition replay for every regime and arm.")
    print(f"Execution: 1m OHLC | intrabar={path} | modeled round-trip spread={spread_bps:.1f}bp | fees={_round_trip_fees_pct(config):.3f}%")
    print(f"Candles: 1m={len(candles['1m'])} | 5m={len(candles['5m'])} | 15m={len(candles['15m'])}; required gaps rejected.")
    print("A=current BE buffer 0.5 ATR | B=BE buffer 2.9 ATR | C=BREAKEVEN off. All arms have independent state, five slots, spacing and admission.")

    print("Running forward validation A...", flush=True)
    validation = _validate_forward(args, arm_configs["A"], candles, validation_start, validation_end, path, spread_bps)
    _print_validation(validation, validation_start, validation_end)

    all_results: dict[str, dict[str, ReplayResult]] = {}
    for regime in active_regimes:
        start_ms, end_ms = int(regime.start.timestamp() * 1000), int(regime.end.timestamp() * 1000) - 1
        print(f"Generating {regime.name} signals...", flush=True)
        signals = _generate_signals(config, candles, start_ms, end_ms)
        results = {}
        for arm, variant in arm_configs.items():
            print(f"Replaying {regime.name} arm {arm} ({len(signals)} raw signals)...", flush=True)
            results[arm] = run_universe(
                name=f"{regime.name}_{arm}", lookback=0, config=variant, signals=signals,
                execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms,
                intrabar_path=path, round_trip_spread_bps=spread_bps,
            )
        all_results[regime.name] = results
        _print_regime(regime, results)
    _print_final(all_results, active_regimes)
    _print_limitations()


def _brt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


REGIMES = (
    Regime("LATERAL", _brt("2026-08-10T00:00:00-03:00"), _brt("2026-08-16T00:00:00-03:00"), False),
    Regime("ALTA", _brt("2026-08-16T00:00:00-03:00"), _brt("2026-08-26T00:00:00-03:00"), True),
    Regime("BAIXA", _brt("2026-06-01T00:00:00-03:00"), _brt("2026-06-08T00:00:00-03:00"), False),
)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SystemExit("timestamps must include an offset, e.g. -03:00")
    return parsed.astimezone(timezone.utc)


def _arm_configs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    current = deepcopy(config)
    late = deepcopy(config)
    late.setdefault("ladder", {})["be_activation_buffer_atr"] = 2.9
    disabled = deepcopy(config)
    disabled.setdefault("risk", {})["breakeven"] = {"mode": "off"}
    return {"A": current, "B": late, "C": disabled}


def _validate_context(config: dict[str, Any]) -> None:
    if str(config.get("entry", {}).get("timeframe")) != "1m":
        raise SystemExit("This REAL_A replay requires the runtime 1m entry timeframe.")
    if str(config.get("trend", {}).get("timeframe")) != "15m":
        raise SystemExit("This REAL_A replay requires the runtime 15m auxiliary timeframe.")
    gate = config.get("trend_gate") if isinstance(config.get("trend_gate"), dict) else {}
    if str(gate.get("mode", "")).lower() != "ge30" or str(gate.get("candle_interval")) != "5m":
        raise SystemExit("This backtest is scoped to the current REAL_A closed-5m GE15 entry architecture.")


def _spread_bps(args: argparse.Namespace, config: dict[str, Any]) -> float:
    value = args.round_trip_spread_bps
    if value is None:
        value = config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0)
    if float(value) < 0:
        raise SystemExit("--round-trip-spread-bps cannot be negative")
    return float(value)


def _validate_forward(
    args: argparse.Namespace, config: dict[str, Any], candles: dict[str, list[Any]], start: datetime, end: datetime,
    path: str, spread_bps: float,
) -> dict[str, Any]:
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
    signals = _generate_signals(config, candles, start_ms, end_ms)
    replay = run_universe(
        name="FORWARD_A", lookback=0, config=config, signals=signals, execution_candles=candles["1m"],
        start_ms=start_ms, end_ms=end_ms, intrabar_path=path, round_trip_spread_bps=spread_bps,
    )
    records = TradeLedger(PROJECT_ROOT, Path(args.ledger)).load()
    observed = [
        item for item in real_bot_b_records(records, args.profile)
        if start_ms <= _record_time_ms(item, "opened_at") < end_ms
    ]
    comparison = validate_replay(replay.entries(), observed, MATCH_TOLERANCE_MS)
    return {"replay": replay, "comparison": comparison, "observed": len(observed)}


def _record_time_ms(item: dict[str, Any], field: str) -> int:
    value = item.get(field)
    if not value:
        return -1
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        parsed = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        return int(parsed.timestamp() * 1000)
    except ValueError:
        return -1


def _generate_signals(config: dict[str, Any], candles: dict[str, list[Any]], start_ms: int, end_ms: int) -> list[Any]:
    """Use only the required 15m warmup plus the target window, not the whole cache."""
    warmup_start = start_ms - WARMUP_CANDLES * 15 * MINUTE_MS
    scoped = {
        interval: [item for item in values if warmup_start <= item.boundary_ms <= end_ms]
        for interval, values in candles.items()
    }
    signals, _decisions = generate_ge_signals(
        config, scoped["1m"], scoped["5m"], scoped["15m"], start_ms, end_ms, 0,
    )
    return signals


def _print_validation(value: dict[str, Any], start: datetime, end: datetime) -> None:
    comparison = value["comparison"]
    print("\nFORWARD VALIDATION - A replay vs REAL_A ledger (available architecture-comparable interval)")
    print(f"Period: {_stamp(start)} -> {_stamp(end)} (end exclusive)")
    print(f"observed forward entries: {comparison.observed} | replay entries: {comparison.replayed}")
    print(f"matched entries within 90s: {comparison.matched}/{comparison.observed} ({comparison.match_rate:.1%})")
    print(f"mean entry timing error: {_seconds(comparison.entry_time_abs_error_seconds)} | matched exit reasons: {comparison.reason_matches}/{comparison.matched}")
    print(f"mean absolute net difference: {_pct(comparison.pnl_abs_error)} | fidelity: {comparison.level}")
    print("Relevant differences: replay uses 1m OHLC plus declared HIGH_FIRST/LOW_FIRST path and modeled fills; forward uses real tick flow and Testnet fills.")


def _print_regime(regime: Regime, results: dict[str, ReplayResult]) -> None:
    print(f"\nREGIME {regime.name}{' - IN-SAMPLE (hypothesis discovery window)' if regime.in_sample else ' - OUT-OF-SAMPLE relative to discovery'}")
    print(f"{_stamp(regime.start)} -> {_stamp(regime.end)} (end exclusive)")
    print("ARM | trades | gross total | gross/trade | net total | net/trade | PF(net) | winrate(net) | HS | BE | PL | TRAIL | age mean/median min | slots full min/% | max sim | blocked capacity | winner | loser")
    for arm in ARMS:
        result = results[arm]
        row = _metrics(result)
        print(
            f"{arm} | {row['trades']} | {row['gross']:+.3f}% | {row['gross_trade']:+.3f}% | {row['net']:+.3f}% | {row['net_trade']:+.3f}% | "
            f"{_pf(row['pf'])} | {row['winrate']:.1%} | {row['HARD_STOP']} | {row['BREAKEVEN']} | {row['PROFIT_LOCK']} | {row['TRAILING']} | "
            f"{row['age_mean']:.1f}/{row['age_median']:.1f} | {result.full_slot_minutes}/{_full(result):.1%} | {result.max_simultaneous_positions} | {result.blocked_slots} | "
            f"{row['winner']:+.3f}% | {row['loser']:+.3f}%"
        )
    a, b, c = (_metrics(results[arm]) for arm in ARMS)
    print(f"delta B-A: net/trade={b['net_trade'] - a['net_trade']:+.3f} pp | gross/trade={b['gross_trade'] - a['gross_trade']:+.3f} pp")
    print(f"delta C-A: net/trade={c['net_trade'] - a['net_trade']:+.3f} pp | gross/trade={c['gross_trade'] - a['gross_trade']:+.3f} pp")
    _print_path(results)


def _metrics(result: ReplayResult) -> dict[str, float | int]:
    trades = result.trades
    gross = sum(item.gross_pct for item in trades)
    net = sum(item.net_pct for item in trades)
    net_values = [item.net_pct for item in trades]
    gains = sum(value for value in net_values if value > 0)
    losses = -sum(value for value in net_values if value < 0)
    ages = [(item.closed_ms - item.opened_ms) / 60_000 for item in trades]
    reasons = Counter(item.exit_reason for item in trades)
    return {
        "trades": len(trades), "gross": gross, "gross_trade": gross / len(trades) if trades else 0.0,
        "net": net, "net_trade": net / len(trades) if trades else 0.0, "pf": gains / losses if losses else math.inf,
        "winrate": sum(value > 0 for value in net_values) / len(trades) if trades else 0.0,
        "HARD_STOP": reasons["HARD_STOP"], "BREAKEVEN": reasons["BREAKEVEN"], "PROFIT_LOCK": reasons["PROFIT_LOCK"], "TRAILING": reasons["TRAILING"],
        "age_mean": statistics.fmean(ages) if ages else 0.0, "age_median": statistics.median(ages) if ages else 0.0,
        "winner": max(net_values, default=0.0), "loser": min(net_values, default=0.0),
    }


def _print_path(results: dict[str, ReplayResult]) -> None:
    entries = {arm: {item.opened_ms for item in results[arm].entries()} for arm in ARMS}
    common = entries["A"] & entries["B"] & entries["C"]
    only = {arm: entries[arm] - set().union(*(entries[other] for other in ARMS if other != arm)) for arm in ARMS}
    print(f"entry path: common A/B/C={len(common)} | A-only={len(only['A'])} | B-only={len(only['B'])} | C-only={len(only['C'])}")
    print(f"pairwise common: A/B={len(entries['A'] & entries['B'])} | A/C={len(entries['A'] & entries['C'])} | B/C={len(entries['B'] & entries['C'])}")


def _print_final(all_results: dict[str, dict[str, ReplayResult]], regimes: Sequence[Regime]) -> None:
    print("\nFINAL - net/trade is the primary economic comparison")
    print("regime | A net/trade | B net/trade | C net/trade | best observed")
    for regime in regimes:
        metrics = {arm: _metrics(all_results[regime.name][arm]) for arm in ARMS}
        best = max(ARMS, key=lambda arm: float(metrics[arm]["net_trade"]))
        marker = "IN-SAMPLE" if regime.in_sample else "OUT-OF-SAMPLE"
        print(f"{regime.name} ({marker}) | {metrics['A']['net_trade']:+.3f}% | {metrics['B']['net_trade']:+.3f}% | {metrics['C']['net_trade']:+.3f}% | {best}")


def _print_limitations() -> None:
    print("\nLIMITATIONS")
    print("- Regime labels are fixed from the user-provided 1h price structure; no indicator reclassification is performed.")
    print("- ALTA is IN-SAMPLE. LATERAL and BAIXA are the discovery out-of-sample checks, not proof of real-time regime identification.")
    print("- Every arm is independently admitted from the same raw signals; exits change slots, spacing and later admission naturally within that arm.")
    print("- Historical fills/exits are modeled from 1m OHLC and declared intrabar path, so PnL is comparative within this replay rather than a Testnet-fill reproduction.")
    print("- No runtime/configuration/shadow state was read or written; cache files may be populated only under the study cache directory.")


def _stamp(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def _pf(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.3f}"


def _full(result: ReplayResult) -> float:
    return result.full_slot_minutes / result.observed_minutes if result.observed_minutes else 0.0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only independent REAL_A A/B/C exit-ladder replay over fixed regimes.")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--ledger", default=str(PROJECT_ROOT / "data/trades/trades_B.jsonl"))
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument("--validation-since", default="2026-08-19T01:05:00-03:00")
    parser.add_argument("--validation-until", default="2026-08-26T00:00:00-03:00")
    parser.add_argument("--regimes", default="all", help="all or a comma-separated subset: lateral,alta,baixa")
    parser.add_argument("--intrabar-path", choices=["high_first", "low_first"], default="high_first")
    parser.add_argument("--round-trip-spread-bps", type=float)
    parser.add_argument("--market-data-url")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "data/studies/real_a_ladder_regime_backtest/klines"))
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
