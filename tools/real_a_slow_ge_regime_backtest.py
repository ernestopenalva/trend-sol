"""Read-only REAL_A versus a single slow-GE context gate over fixed regimes."""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.monitor.entry_engine import EntryEngine, EntrySignal
from src.trade_ledger import TradeLedger
from tools.cohort_study import _load_config
from tools.ge_replay_study import MATCH_TOLERANCE_MS, WARMUP_CANDLES, SignalEvent, _kline_payload, load_ge_market_data, real_bot_b_records, run_universe, validate_replay
from tools.market_bot_replay import MINUTE_MS, NullLogger, ReplayResult, _round_trip_fees_pct
from tools.market_selection_study import BinancePublicClient, MarketCandle


@dataclass(frozen=True)
class Regime:
    name: str
    start: datetime
    end: datetime
    in_sample: bool


def _brt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


REGIMES = (
    Regime("LATERAL", _brt("2026-08-10T00:00:00-03:00"), _brt("2026-08-16T00:00:00-03:00"), False),
    Regime("ALTA", _brt("2026-08-16T00:00:00-03:00"), _brt("2026-08-26T00:00:00-03:00"), True),
    Regime("BAIXA", _brt("2026-06-01T00:00:00-03:00"), _brt("2026-06-08T00:00:00-03:00"), False),
)


class SlowGeEntryEngine(EntryEngine):
    """Normal REAL_A pipeline, but the slow 15m geometry is evaluated first."""

    def __init__(self, symbol: str, config: Dict[str, Any]) -> None:
        super().__init__(symbol, config, NullLogger())  # type: ignore[arg-type]
        self.slow_ge = False

    def _gate_trend(self) -> bool:
        if not self.slow_ge:
            self._log_gate(1, False, False, "slow_ge15m_45m_block")
            return False
        return super()._gate_trend()


def main() -> None:
    args = _args()
    config = effective_config(_load_config(Path(args.config)))
    _validate_context(config)
    selected = tuple(item for item in REGIMES if args.regimes == "all" or item.name.lower() in args.regimes.split(","))
    if not selected:
        raise SystemExit("--regimes must select lateral, alta, baixa, or all")
    path, spread = args.intrabar_path.upper(), _spread(args, config)
    validation_start, validation_end = _timestamp(args.validation_since), _timestamp(args.validation_until)
    warmup_ms = WARMUP_CANDLES * 15 * MINUTE_MS
    data_start = min([int(item.start.timestamp() * 1000) for item in selected] + [int(validation_start.timestamp() * 1000)]) - warmup_ms
    data_end = max([int(item.end.timestamp() * 1000) - 1 for item in selected] + [int(validation_end.timestamp() * 1000) - 1])
    client = BinancePublicClient(str(args.market_data_url or config["market_data"]["rest_url"]), args.http_timeout_seconds)
    cache = Path(args.cache_dir)
    candles = {tf: load_ge_market_data(client, str(config["symbol"]), tf, data_start, data_end, cache, args.offline) for tf in ("1m", "5m", "15m")}
    _header(config, path, spread, candles)
    if not args.skip_forward_validation:
        _validate_a(args, config, candles, validation_start, validation_end, path, spread)
    else:
        print("FORWARD VALIDATION SKIPPED by explicit local diagnostic option.")

    all_results: dict[str, dict[str, ReplayResult]] = {}
    all_data: dict[str, dict[str, Any]] = {}
    for regime in selected:
        start_ms, end_ms = int(regime.start.timestamp() * 1000), int(regime.end.timestamp() * 1000) - 1
        print(f"Generating {regime.name} signals and slow-GE duty cycle...", flush=True)
        signals, states = _signals(config, candles, start_ms, end_ms, regime.name)
        results = {}
        for arm in ("A", "B"):
            print(f"Replaying {regime.name} arm {arm} ({len(signals[arm])} raw signals)...", flush=True)
            results[arm] = run_universe(name=f"{regime.name}_{arm}", lookback=3, config=config, signals=signals[arm], execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=path, round_trip_spread_bps=spread)
        all_results[regime.name], all_data[regime.name] = results, {"signals": signals, "states": states}
        _print_regime(regime, results, signals, states)
    _print_final(selected, all_results, all_data)
    print("\nLIMITATIONS: ALTA is IN-SAMPLE; labels are fixed and not a real-time regime classifier. Outputs are relative OHLC replay comparisons, not Testnet-fill PnL reconstruction. No runtime/configuration/shadow state is written.")


def _header(config: Dict[str, Any], path: str, spread: float, candles: Dict[str, Sequence[MarketCandle]]) -> None:
    print("TREND-SOL | REAL_A vs slow GE context gate (read-only)")
    print("CURRENT GE15: high_5m[t] > high_5m[t-3] AND low_5m[t] > low_5m[t-3], all candles closed.")
    print("SLOW GE45:   high_15m[t] > high_15m[t-3] AND low_15m[t] > low_15m[t-3], all candles closed.")
    print("B evaluates SLOW GE45 first; only PASS continues to the unchanged GE15 + G2 + G3 + G4 pipeline.")
    print(f"Execution: 1m OHLC | intrabar={path} | modeled round-trip spread={spread:.1f}bp | fees={_round_trip_fees_pct(config):.3f}%")
    print(f"Candles: 1m={len(candles['1m'])} | 5m={len(candles['5m'])} | 15m={len(candles['15m'])}; incomplete histories are rejected.")


def _signals(config: Dict[str, Any], candles: Dict[str, Sequence[MarketCandle]], start_ms: int, end_ms: int, label: str) -> tuple[dict[str, list[SignalEvent]], dict[int, bool]]:
    warmup_start = start_ms - WARMUP_CANDLES * 15 * MINUTE_MS
    scoped = {tf: [item for item in values if warmup_start <= item.boundary_ms <= end_ms] for tf, values in candles.items()}
    symbol = str(config["symbol"])
    a, b = EntryEngine(symbol, config, NullLogger()), SlowGeEntryEngine(symbol, config)  # type: ignore[arg-type]
    indices = {"5m": 0, "15m": 0}
    output = {"A": [], "B": []}
    states: dict[int, bool] = {}
    for processed, candle in enumerate(scoped["1m"], start=1):
        boundary = candle.boundary_ms
        if boundary > end_ms:
            break
        for timeframe in ("15m", "5m"):
            source = scoped[timeframe]
            while indices[timeframe] < len(source) and source[indices[timeframe]].boundary_ms <= boundary:
                current = source[indices[timeframe]]
                payload = _kline_payload(current)
                a.on_kline(f"{symbol.lower()}@kline_{timeframe}", payload)
                b.on_kline(f"{symbol.lower()}@kline_{timeframe}", payload)
                if timeframe == "15m":
                    b.slow_ge = _slow_ge(b._candles_for("15m"))
                indices[timeframe] += 1
        signal_a = a.on_kline(f"{symbol.lower()}@kline_1m", _kline_payload(candle))
        signal_b = b.on_kline(f"{symbol.lower()}@kline_1m", _kline_payload(candle))
        if boundary >= start_ms:
            states[boundary] = b.slow_ge
            if signal_a is not None:
                output["A"].append(SignalEvent(boundary, signal_a))
            if signal_b is not None:
                output["B"].append(SignalEvent(boundary, signal_b))
        if processed % 5_000 == 0:
            print(f"signal progress {label}: {processed}/{len(scoped['1m'])} 1m candles", flush=True)
    return output, states


def _slow_ge(candles: Sequence[Any]) -> bool:
    if len(candles) < 4:
        return False
    latest, reference = candles[-1], candles[-4]
    return latest.high > reference.high and latest.low > reference.low


def _validate_a(args: argparse.Namespace, config: Dict[str, Any], candles: Dict[str, Sequence[MarketCandle]], start: datetime, end: datetime, path: str, spread: float) -> None:
    print("Running forward validation A...", flush=True)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
    signals, _states = _signals(config, candles, start_ms, end_ms, "validation-A")
    replay = run_universe(name="VALIDATION_A", lookback=3, config=config, signals=signals["A"], execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=path, round_trip_spread_bps=spread)
    records = TradeLedger(PROJECT_ROOT, Path(args.ledger_a)).load()
    observed = [item for item in real_bot_b_records(records, args.profile) if start_ms <= _record_ms(item) < end_ms]
    value = validate_replay(replay.entries(), observed, MATCH_TOLERANCE_MS)
    print(f"validation A | forward entries={value.observed} | replay entries={value.replayed} | matched={value.matched}/{value.observed} ({value.match_rate:.1%}) | timing MAE={_seconds(value.entry_time_abs_error_seconds)} | fidelity={value.level}")


def _print_regime(regime: Regime, results: dict[str, ReplayResult], signals: dict[str, list[SignalEvent]], states: dict[int, bool]) -> None:
    print(f"\nREGIME {regime.name} - {'IN-SAMPLE' if regime.in_sample else 'OUT-OF-SAMPLE'}")
    print(f"{_stamp(regime.start)} -> {_stamp(regime.end)} (end exclusive)")
    print("ARM | A raw | blocked slow | pipeline | admitted | gross total | gross/trade | net total | net/trade | PF(net) | winrate(net) | HS | HS rate | BE | PL | TRAIL | age mean/median min | slots full min/% | max sim | blocked capacity | winner | loser")
    a_raw, b_raw = len(signals["A"]), len(signals["B"])
    for arm in ("A", "B"):
        row, admitted = _metrics(results[arm]), len(results[arm].entries())
        blocked = a_raw - b_raw if arm == "B" else 0
        pipeline = b_raw if arm == "B" else a_raw
        print(f"{arm} | {a_raw} | {blocked} | {pipeline} | {admitted} | {row['gross']:+.3f}% | {row['gross_trade']:+.3f}% | {row['net']:+.3f}% | {row['net_trade']:+.3f}% | {_num(row['pf'])} | {row['winrate']:.1%} | {row['HARD_STOP']} | {row['hs_rate']:.1%} | {row['BREAKEVEN']} | {row['PROFIT_LOCK']} | {row['TRAILING']} | {row['age_mean']:.1f}/{row['age_median']:.1f} | {results[arm].full_slot_minutes}/{_full(results[arm]):.1%} | {results[arm].max_simultaneous_positions} | {results[arm].blocked_slots} | {row['winner']:+.3f}% | {row['loser']:+.3f}%")
    _quality(results["A"], states)
    _overlap(results, signals)
    _duty_and_delay(states, results["A"])


def _quality(a: ReplayResult, states: dict[int, bool]) -> None:
    blocked = [item for item in a.trades if not states.get(item.opened_ms, False)]
    preserved = [item for item in a.trades if states.get(item.opened_ms, False)]
    print("A closed trades classified by slow-GE state at entry")
    for name, items in (("blocked", blocked), ("preserved", preserved)):
        row = _trade_metrics(items)
        print(f"{name} | trades={row['closed']} | gross total={row['gross']:+.3f}% | gross/trade={row['gross_trade']:+.3f}% | net total={row['net']:+.3f}% | net/trade={row['net_trade']:+.3f}% | HS={row['HARD_STOP']} | HS rate={row['hs_rate']:.1%} | BE={row['BREAKEVEN']} | PL={row['PROFIT_LOCK']} | TRAIL={row['TRAILING']}")


def _overlap(results: dict[str, ReplayResult], signals: dict[str, list[SignalEvent]]) -> None:
    raw = _match(signals["A"], signals["B"])
    trades = _match(_entry_events(results["A"]), _entry_events(results["B"]))
    print(f"overlap raw signals (90s): common={raw[0]} | A-only={raw[1]} | B-only={raw[2]}")
    print(f"overlap admitted trades (90s): common={trades[0]} | A-only={trades[1]} | B-only={trades[2]}")


def _duty_and_delay(states: dict[int, bool], a: ReplayResult) -> None:
    ordered = sorted(states.items())
    runs: list[tuple[bool, int, int]] = []
    for timestamp, value in ordered:
        if not runs or runs[-1][0] != value:
            runs.append((value, timestamp, timestamp))
        else:
            runs[-1] = (value, runs[-1][1], timestamp)
    total = len(ordered)
    true_minutes = sum(1 for _at, value in ordered if value)
    def durations(flag: bool) -> list[float]: return [(end - start + MINUTE_MS) / MINUTE_MS for value, start, end in runs if value == flag]
    true_runs, false_runs = durations(True), durations(False)
    up = [start for value, start, _end in runs if value and start != ordered[0][0]]
    down = [start for value, start, _end in runs if not value and start != ordered[0][0]]
    entries = sorted(item.opened_ms for item in a.entries())
    delays = [(next((entry - transition for entry in entries if entry >= transition), None)) for transition in up]
    delay_minutes = [value / MINUTE_MS for value in delays if value is not None]
    false_entries = sum(1 for entry in entries if not states.get(entry, False))
    print(f"slow GE duty: TRUE={true_minutes / total:.1%} | FALSE={(total - true_minutes) / total:.1%} | TRUE run mean/median={_mean(true_runs):.1f}/{_median(true_runs):.1f}m | FALSE run mean/median={_mean(false_runs):.1f}/{_median(false_runs):.1f}m | FALSE->TRUE={len(up)} | TRUE->FALSE={len(down)}")
    print(f"slow GE operational delay: FALSE->TRUE transitions with later A admission={len(delay_minutes)}/{len(up)} | delay mean/median={_mean(delay_minutes):.1f}/{_median(delay_minutes):.1f}m | A admitted while FALSE before next TRUE={false_entries}")


def _metrics(result: ReplayResult) -> dict[str, float | int]:
    return _trade_metrics(result.trades)


def _trade_metrics(trades: Sequence[Any]) -> dict[str, float | int]:
    gross, net = [item.gross_pct for item in trades], [item.net_pct for item in trades]
    reasons = Counter(item.exit_reason for item in trades)
    gains, losses = sum(value for value in net if value > 0), -sum(value for value in net if value < 0)
    ages = [(item.closed_ms - item.opened_ms) / MINUTE_MS for item in trades]
    return {"closed": len(trades), "gross": sum(gross), "gross_trade": sum(gross) / len(gross) if gross else 0.0, "net": sum(net), "net_trade": sum(net) / len(net) if net else 0.0, "pf": gains / losses if losses else math.inf, "winrate": sum(value > 0 for value in net) / len(net) if net else 0.0, "HARD_STOP": reasons["HARD_STOP"], "hs_rate": reasons["HARD_STOP"] / len(trades) if trades else 0.0, "BREAKEVEN": reasons["BREAKEVEN"], "PROFIT_LOCK": reasons["PROFIT_LOCK"], "TRAILING": reasons["TRAILING"], "age_mean": _mean(ages), "age_median": _median(ages), "winner": max(net, default=0.0), "loser": min(net, default=0.0)}


def _print_final(regimes: Sequence[Regime], results: dict[str, dict[str, ReplayResult]], data: dict[str, dict[str, Any]]) -> None:
    print("\nFINAL - selection quality before volume reduction")
    print("regime | A gross/trade | B gross/trade | A net/trade | B net/trade | preserved gross/trade | blocked gross/trade | signals blocked | slow TRUE")
    for regime in regimes:
        a, b = results[regime.name]["A"], results[regime.name]["B"]
        signals, states = data[regime.name]["signals"], data[regime.name]["states"]
        blocked = [item for item in a.trades if not states.get(item.opened_ms, False)]
        preserved = [item for item in a.trades if states.get(item.opened_ms, False)]
        ar, br, block, keep = _metrics(a), _metrics(b), _trade_metrics(blocked), _trade_metrics(preserved)
        pct_blocked = (len(signals["A"]) - len(signals["B"])) / len(signals["A"]) if signals["A"] else 0.0
        duty = sum(states.values()) / len(states) if states else 0.0
        print(f"{regime.name} | {ar['gross_trade']:+.3f}% | {br['gross_trade']:+.3f}% | {ar['net_trade']:+.3f}% | {br['net_trade']:+.3f}% | {keep['gross_trade']:+.3f}% | {block['gross_trade']:+.3f}% | {pct_blocked:.1%} | {duty:.1%}")


def _validate_context(config: Dict[str, Any]) -> None:
    gate = config.get("trend_gate") if isinstance(config.get("trend_gate"), dict) else {}
    if str(config.get("entry", {}).get("timeframe")) != "1m" or str(config.get("trend", {}).get("timeframe")) != "15m":
        raise SystemExit("Requires the current intraday 1m/15m REAL_A profile.")
    if str(gate.get("mode", "")).lower() != "ge30" or str(gate.get("candle_interval")) != "5m" or int(gate.get("lookback_candles", 0)) != 3:
        raise SystemExit("Requires the current GE15 configuration: three closed 5m candles of lookback.")


def _spread(args: argparse.Namespace, config: Dict[str, Any]) -> float:
    value = args.round_trip_spread_bps if args.round_trip_spread_bps is not None else config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0)
    return float(value)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None: raise SystemExit("timestamps must include offset")
    return parsed.astimezone(timezone.utc)


def _record_ms(record: Dict[str, Any]) -> int:
    try:
        parsed = datetime.fromisoformat(str(record.get("opened_at") or "").replace("Z", "+00:00"))
        return int((parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp() * 1000)
    except ValueError: return -1


def _match(left: Sequence[SignalEvent], right: Sequence[SignalEvent]) -> tuple[int, int, int]:
    available, common = set(range(len(right))), 0
    for item in left:
        choices = [index for index in available if abs(right[index].boundary_ms - item.boundary_ms) <= MATCH_TOLERANCE_MS]
        if choices:
            available.remove(min(choices, key=lambda index: abs(right[index].boundary_ms - item.boundary_ms)))
            common += 1
    return common, len(left) - common, len(right) - common


def _entry_events(result: ReplayResult) -> list[SignalEvent]:
    return [SignalEvent(item.opened_ms, EntrySignal("", item.entry_price, "", 0, 0, "", 0)) for item in result.entries()]


def _mean(values: Sequence[float]) -> float: return statistics.fmean(values) if values else 0.0
def _median(values: Sequence[float]) -> float: return statistics.median(values) if values else 0.0
def _num(value: float) -> str: return "inf" if math.isinf(value) else f"{value:.3f}"
def _seconds(value: float | None) -> str: return "n/a" if value is None else f"{value:.1f}s"
def _full(result: ReplayResult) -> float: return result.full_slot_minutes / result.observed_minutes if result.observed_minutes else 0.0
def _stamp(value: datetime) -> str: return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only REAL_A vs one slow 15m/45m GE context gate.")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--ledger-a", default=str(PROJECT_ROOT / "data/trades/trades_B.jsonl"))
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument("--validation-since", default="2026-08-19T01:05:00-03:00")
    parser.add_argument("--validation-until", default="2026-08-26T00:00:00-03:00")
    parser.add_argument("--regimes", default="all")
    parser.add_argument("--intrabar-path", choices=["high_first", "low_first"], default="high_first")
    parser.add_argument("--round-trip-spread-bps", type=float)
    parser.add_argument("--market-data-url")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "data/studies/real_a_ladder_regime_backtest/klines"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--skip-forward-validation", action="store_true")
    return parser.parse_args()


if __name__ == "__main__": main()
