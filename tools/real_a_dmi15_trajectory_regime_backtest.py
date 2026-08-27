"""Read-only independent REAL_A / Shadow-C / Shadow-E replay over fixed regimes."""
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
from typing import Any, Dict, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.monitor.dmi15_shadow import Dmi15ShadowRegistry
from src.monitor.dmi15_trajectory_shadow import Dmi15TrajectoryShadowRegistry
from src.monitor.entry_engine import EntryEngine, EntrySignal
from src.monitor.market_context import MarketContextEngine
from src.trade_ledger import TradeLedger
from tools.cohort_study import _load_config
from tools.ge_replay_study import (
    MATCH_TOLERANCE_MS, WARMUP_CANDLES, SignalEvent, _kline_payload,
    generate_ge_signals, load_ge_market_data, real_bot_b_records, run_universe,
    validate_replay,
)
from tools.market_bot_replay import MINUTE_MS, NullLogger, ReplayResult, _round_trip_fees_pct
from tools.market_selection_study import BinancePublicClient, MarketCandle


@dataclass(frozen=True)
class Regime:
    name: str
    start: datetime
    end: datetime  # exclusive
    in_sample: bool


def _brt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


REGIMES = (
    Regime("LATERAL", _brt("2026-08-10T00:00:00-03:00"), _brt("2026-08-16T00:00:00-03:00"), False),
    Regime("ALTA", _brt("2026-08-16T00:00:00-03:00"), _brt("2026-08-26T00:00:00-03:00"), True),
    Regime("BAIXA", _brt("2026-06-01T00:00:00-03:00"), _brt("2026-06-08T00:00:00-03:00"), False),
)
ARMS = ("A", "C", "E")


class _ProbeMixin:
    """Executes the real shadow gate path but suppresses persistence and positions."""

    def _load_state(self) -> None:
        return

    def _save_state(self) -> None:
        return

    def _open(self, **kwargs: Any) -> None:
        del kwargs

    def _event(self, event: str, bucket: int, **fields: Any) -> None:
        del bucket
        if event == "DMI15_EVALUATED":
            self.passed = bool(fields.get("passed", False))
        elif event == "ENTRY_BLOCKED_DMI_TRAJECTORY":
            self.blocked_trajectory_events += 1


class _CProbe(_ProbeMixin, Dmi15ShadowRegistry):
    def __init__(self, config: Dict[str, Any]) -> None:
        cfg = deepcopy(config)
        cfg.setdefault("instrumentation", {}).setdefault("dmi15_shadow", {})["enabled"] = True
        self.passed = False
        self.blocked_trajectory_events = 0
        super().__init__(PROJECT_ROOT, cfg, NullLogger())  # type: ignore[arg-type]


class _EProbe(_ProbeMixin, Dmi15TrajectoryShadowRegistry):
    def __init__(self, config: Dict[str, Any]) -> None:
        cfg = deepcopy(config)
        cfg.setdefault("instrumentation", {}).setdefault("dmi15_trajectory_shadow", {})["enabled"] = True
        self.passed = False
        self.blocked_trajectory_events = 0
        super().__init__(PROJECT_ROOT, cfg, NullLogger())  # type: ignore[arg-type]


def main() -> None:
    args = _args()
    config = effective_config(_load_config(Path(args.config)))
    _validate_context(config)
    path = args.intrabar_path.upper()
    spread = _spread(args, config)
    selected = tuple(item for item in REGIMES if args.regimes == "all" or item.name.lower() in args.regimes.split(","))
    if not selected:
        raise SystemExit("--regimes must select lateral, alta, baixa, or all")
    validation_a_start, validation_a_end = _timestamp(args.validation_a_since), _timestamp(args.validation_a_until)
    validation_shadow_start, validation_shadow_end = _timestamp(args.validation_shadow_since), _timestamp(args.validation_shadow_until)
    warmup_ms = WARMUP_CANDLES * 15 * MINUTE_MS
    data_start = min([int(item.start.timestamp() * 1000) for item in selected] + [int(validation_a_start.timestamp() * 1000), int(validation_shadow_start.timestamp() * 1000)]) - warmup_ms
    data_end = max([int(item.end.timestamp() * 1000) - 1 for item in selected] + [int(validation_a_end.timestamp() * 1000) - 1, int(validation_shadow_end.timestamp() * 1000) - 1])
    client = BinancePublicClient(str(args.market_data_url or config["market_data"]["rest_url"]), args.http_timeout_seconds)
    cache = Path(args.cache_dir)
    symbol = str(config["symbol"])
    candles = {tf: load_ge_market_data(client, symbol, tf, data_start, data_end, cache, args.offline) for tf in ("1m", "5m", "15m")}

    print("TREND-SOL | independent REAL_A GE15 vs Shadow-C DMI15 vs Shadow-E trajectory replay")
    print("A: GE15+G2+G3+G4, 1m admission. C/E: exact registry gate on closed 5m, no G2/G3/G4, own admission/slots/spacing.")
    print(f"Execution: 1m OHLC | intrabar={path} | modeled round-trip spread={spread:.1f}bp | fees={_round_trip_fees_pct(config):.3f}%")
    print(f"Candles: 1m={len(candles['1m'])} | 5m={len(candles['5m'])} | 15m={len(candles['15m'])}; incomplete histories are rejected.")

    if args.skip_forward_validation:
        print("FORWARD VALIDATION SKIPPED by explicit local diagnostic option.")
    else:
        _print_validations(args, config, candles, validation_a_start, validation_a_end, validation_shadow_start, validation_shadow_end, path, spread)
    all_results: dict[str, dict[str, ReplayResult]] = {}
    all_signals: dict[str, dict[str, list[SignalEvent]]] = {}
    all_e_blocks: dict[str, int] = {}
    for regime in selected:
        start_ms, end_ms = int(regime.start.timestamp() * 1000), int(regime.end.timestamp() * 1000) - 1
        print(f"Generating {regime.name} raw signals...", flush=True)
        signals, e_blocks = _signals(config, candles, start_ms, end_ms, label=regime.name)
        results: dict[str, ReplayResult] = {}
        for arm in ARMS:
            print(f"Replaying {regime.name} arm {arm} ({len(signals[arm])} raw signals)...", flush=True)
            results[arm] = run_universe(name=f"{regime.name}_{arm}", lookback=3, config=config, signals=signals[arm], execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=path, round_trip_spread_bps=spread)
        all_results[regime.name], all_signals[regime.name], all_e_blocks[regime.name] = results, signals, e_blocks
        _print_regime(regime, results, signals, e_blocks)
    _print_final(selected, all_results)
    _print_limits()


def _signals(config: Dict[str, Any], candles: Dict[str, Sequence[MarketCandle]], start_ms: int, end_ms: int, *, label: str = "") -> tuple[dict[str, list[SignalEvent]], int]:
    warmup_start = start_ms - WARMUP_CANDLES * 15 * MINUTE_MS
    scoped = {tf: [item for item in values if warmup_start <= item.boundary_ms <= end_ms] for tf, values in candles.items()}
    a_signals, _ = generate_ge_signals(config, scoped["1m"], scoped["5m"], scoped["15m"], start_ms, end_ms, 0)
    symbol = str(config["symbol"])
    atr_engine = EntryEngine(symbol, config, NullLogger())  # type: ignore[arg-type]
    context = MarketContextEngine(atr_engine, config)
    c_probe, e_probe = _CProbe(config), _EProbe(config)
    output = {"A": a_signals, "C": [], "E": []}
    indices = {"5m": 0, "15m": 0}
    e_blocks_before_window: int | None = None
    for processed, candle in enumerate(scoped["1m"], start=1):
        boundary = candle.boundary_ms
        if boundary > end_ms:
            break
        if boundary >= start_ms and e_blocks_before_window is None:
            e_blocks_before_window = e_probe.blocked_trajectory_events
        for timeframe in ("15m", "5m"):
            source = scoped[timeframe]
            while indices[timeframe] < len(source) and source[indices[timeframe]].boundary_ms <= boundary:
                current = source[indices[timeframe]]
                atr_engine.on_kline(f"{symbol.lower()}@kline_{timeframe}", _kline_payload(current))
                if timeframe == "5m":
                    snapshot = context.refresh()
                    entry_atr = atr_engine._current_entry_atr()
                    for arm, probe in (("C", c_probe), ("E", e_probe)):
                        if probe.on_closed_5m(snapshot, entry_atr, atr_engine.entry_timeframe, int(config["entry"]["atr_period"])) and boundary >= start_ms:
                            tf_5m = snapshot.get("tf_5m", {}) if isinstance(snapshot, dict) else {}
                            price = tf_5m.get("close") if isinstance(tf_5m, dict) else None
                            bucket = tf_5m.get("latest_open_at_ms") if isinstance(tf_5m, dict) else None
                            if isinstance(price, (int, float)) and isinstance(bucket, int):
                                output[arm].append(SignalEvent(boundary, EntrySignal(symbol, float(price), _iso(boundary), bucket, entry_atr, atr_engine.entry_timeframe, int(config["entry"]["atr_period"]))))
                indices[timeframe] += 1
        atr_engine.on_kline(f"{symbol.lower()}@kline_1m", _kline_payload(candle))
        if label and processed % 5_000 == 0:
            print(f"signal progress {label}: {processed}/{len(scoped['1m'])} 1m candles", flush=True)
    return output, e_probe.blocked_trajectory_events - (e_blocks_before_window or 0)


def _print_validations(args: argparse.Namespace, config: Dict[str, Any], candles: Dict[str, Sequence[MarketCandle]], a_start: datetime, a_end: datetime, shadow_start: datetime, shadow_end: datetime, path: str, spread: float) -> None:
    print("Running forward entry validation A/C/E...", flush=True)
    windows = {"A": (a_start, a_end), "C": (shadow_start, shadow_end), "E": (shadow_start, shadow_end)}
    ledgers = {"A": Path(args.ledger_a), "C": _shadow_ledger(config, "dmi15_shadow", "data/trades/trades_dmi15_shadow.jsonl"), "E": _shadow_ledger(config, "dmi15_trajectory_shadow", "data/trades/trades_dmi15_trajectory_shadow.jsonl")}
    for arm in ARMS:
        start, end = windows[arm]
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
        print(f"validating {arm} signal reproduction...", flush=True)
        signals, _e_blocks = _signals(config, candles, start_ms, end_ms, label=f"validation-{arm}")
        replay = run_universe(name=f"VALIDATION_{arm}", lookback=3, config=config, signals=signals[arm], execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=path, round_trip_spread_bps=spread)
        records = TradeLedger(PROJECT_ROOT, ledgers[arm]).load()
        if arm == "A":
            observed = [item for item in real_bot_b_records(records, args.profile) if start_ms <= _record_ms(item) < end_ms]
        else:
            observed = [item for item in records if start_ms <= _record_ms(item) < end_ms]
        validation = validate_replay(replay.entries(), observed, MATCH_TOLERANCE_MS)
        print(f"validation {arm} | forward entries={validation.observed} | replay entries={validation.replayed} | matched={validation.matched}/{validation.observed} ({validation.match_rate:.1%}) | timing MAE={_seconds(validation.entry_time_abs_error_seconds)} | fidelity={validation.level}")
        if validation.match_rate < 0.60:
            print(f"WARNING {arm}: low entry coincidence; historical results for this arm require caution.")


def _shadow_ledger(config: Dict[str, Any], key: str, default: str) -> Path:
    settings = config.get("instrumentation", {}).get(key, {})
    value = settings.get("ledger_file", default) if isinstance(settings, dict) else default
    return PROJECT_ROOT / str(value)


def _print_regime(regime: Regime, results: dict[str, ReplayResult], signals: dict[str, list[SignalEvent]], e_blocks: int) -> None:
    scope = "IN-SAMPLE" if regime.in_sample else "OUT-OF-SAMPLE"
    print(f"\nREGIME {regime.name} - {scope}")
    print(f"{_stamp(regime.start)} -> {_stamp(regime.end)} (end exclusive)")
    print("ARM | raw signals | admitted | raw/admitted | closed | gross total | gross/trade | net total | net/trade | PF(net) | winrate(net) | HS | HS rate | BE | PL | TRAIL | age mean/median min | slots full min/% | max sim | blocked capacity | winner | loser")
    for arm in ARMS:
        row = _metrics(results[arm])
        admitted = len(results[arm].entries())
        ratio = len(signals[arm]) / admitted if admitted else math.inf
        print(f"{arm} | {len(signals[arm])} | {admitted} | {_number(ratio)} | {row['closed']} | {row['gross']:+.3f}% | {row['gross_trade']:+.3f}% | {row['net']:+.3f}% | {row['net_trade']:+.3f}% | {_number(row['pf'])} | {row['winrate']:.1%} | {row['HARD_STOP']} | {row['hs_rate']:.1%} | {row['BREAKEVEN']} | {row['PROFIT_LOCK']} | {row['TRAILING']} | {row['age_mean']:.1f}/{row['age_median']:.1f} | {results[arm].full_slot_minutes}/{_full(results[arm]):.1%} | {results[arm].max_simultaneous_positions} | {results[arm].blocked_slots} | {row['winner']:+.3f}% | {row['loser']:+.3f}%")
    print(f"E blocked trajectory: {e_blocks}")
    for left, right in (("A", "C"), ("A", "E"), ("C", "E")):
        raw = _overlap(signals[left], signals[right])
        admitted = _overlap(_entry_events(results[left]), _entry_events(results[right]))
        print(f"overlap {left}/{right} raw (90s): common={raw[0]} | {left}-only={raw[1]} | {right}-only={raw[2]}")
        print(f"overlap {left}/{right} admitted (90s): common={admitted[0]} | {left}-only={admitted[1]} | {right}-only={admitted[2]}")


def _metrics(result: ReplayResult) -> dict[str, float | int]:
    trades = result.trades
    gross = [item.gross_pct for item in trades]
    net = [item.net_pct for item in trades]
    reasons = Counter(item.exit_reason for item in trades)
    gains, losses = sum(value for value in net if value > 0), -sum(value for value in net if value < 0)
    ages = [(item.closed_ms - item.opened_ms) / 60_000 for item in trades]
    return {"closed": len(trades), "gross": sum(gross), "gross_trade": sum(gross) / len(gross) if gross else 0.0, "net": sum(net), "net_trade": sum(net) / len(net) if net else 0.0, "pf": gains / losses if losses else math.inf, "winrate": sum(value > 0 for value in net) / len(net) if net else 0.0, "HARD_STOP": reasons["HARD_STOP"], "hs_rate": reasons["HARD_STOP"] / len(trades) if trades else 0.0, "BREAKEVEN": reasons["BREAKEVEN"], "PROFIT_LOCK": reasons["PROFIT_LOCK"], "TRAILING": reasons["TRAILING"], "age_mean": statistics.fmean(ages) if ages else 0.0, "age_median": statistics.median(ages) if ages else 0.0, "winner": max(net, default=0.0), "loser": min(net, default=0.0)}


def _overlap(left: Sequence[SignalEvent], right: Sequence[SignalEvent]) -> tuple[int, int, int]:
    available = set(range(len(right)))
    common = 0
    for item in left:
        candidates = [index for index in available if abs(right[index].boundary_ms - item.boundary_ms) <= MATCH_TOLERANCE_MS]
        if candidates:
            available.remove(min(candidates, key=lambda index: abs(right[index].boundary_ms - item.boundary_ms)))
            common += 1
    return common, len(left) - common, len(right) - common


def _entry_events(result: ReplayResult) -> list[SignalEvent]:
    return [SignalEvent(item.opened_ms, EntrySignal("", item.entry_price, "", 0, 0, "", 0)) for item in result.entries()]


def _print_final(regimes: Sequence[Regime], all_results: dict[str, dict[str, ReplayResult]]) -> None:
    print("\nFINAL - net/trade is the primary economic comparison")
    print("regime | A net/trade | C net/trade | E net/trade | best observed")
    for regime in regimes:
        values = {arm: _metrics(all_results[regime.name][arm]) for arm in ARMS}
        best = max(ARMS, key=lambda arm: float(values[arm]["net_trade"]))
        print(f"{regime.name} ({'IN-SAMPLE' if regime.in_sample else 'OUT-OF-SAMPLE'}) | {values['A']['net_trade']:+.3f}% | {values['C']['net_trade']:+.3f}% | {values['E']['net_trade']:+.3f}% | {best}")


def _validate_context(config: Dict[str, Any]) -> None:
    gate = config.get("trend_gate") if isinstance(config.get("trend_gate"), dict) else {}
    if str(config.get("entry", {}).get("timeframe")) != "1m" or str(config.get("trend", {}).get("timeframe")) != "15m":
        raise SystemExit("This study requires the current intraday 1m/15m REAL_A architecture.")
    if str(gate.get("mode", "")).lower() != "ge30" or str(gate.get("candle_interval")) != "5m":
        raise SystemExit("This study requires the current closed-5m GE15 REAL_A gate configuration.")


def _spread(args: argparse.Namespace, config: Dict[str, Any]) -> float:
    value = args.round_trip_spread_bps if args.round_trip_spread_bps is not None else config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0)
    if float(value) < 0:
        raise SystemExit("--round-trip-spread-bps cannot be negative")
    return float(value)


def _timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise SystemExit("timestamps must include an offset")
    return result.astimezone(timezone.utc)


def _record_ms(record: Dict[str, Any]) -> int:
    value = record.get("opened_at")
    if not value:
        return -1
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return -1
    return int((parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed).timestamp() * 1000)


def _kline_payload(candle: MarketCandle) -> Dict[str, Any]:
    return {"k": {"t": candle.open_time_ms, "T": candle.close_time_ms, "o": str(candle.open), "h": str(candle.high), "l": str(candle.low), "c": str(candle.close), "v": str(candle.quote_volume), "x": True}}


def _iso(boundary_ms: int) -> str:
    return datetime.fromtimestamp(boundary_ms / 1000, tz=timezone.utc).isoformat()


def _stamp(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")


def _number(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.3f}"


def _seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}s"


def _full(result: ReplayResult) -> float:
    return result.full_slot_minutes / result.observed_minutes if result.observed_minutes else 0.0


def _print_limits() -> None:
    print("\nLIMITATIONS")
    print("- ALTA is IN-SAMPLE; LATERAL and BAIXA are out-of-sample relative to the recent DMI hypotheses.")
    print("- Regime labels are fixed as supplied and are not a real-time regime classifier.")
    print("- All arms use the same OHLC path and cost model; outputs are relative replay comparisons, not Testnet PnL reconstruction.")
    print("- No runtime/configuration/shadow state is written; only the study candle cache may be populated.")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only independent REAL_A/Shadow-C/Shadow-E regime backtest.")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--ledger-a", default=str(PROJECT_ROOT / "data/trades/trades_B.jsonl"))
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument("--validation-a-since", default="2026-08-19T01:05:00-03:00")
    parser.add_argument("--validation-a-until", default="2026-08-26T00:00:00-03:00")
    parser.add_argument("--validation-shadow-since", default="2026-08-22T00:00:00-03:00")
    parser.add_argument("--validation-shadow-until", default="2026-08-26T00:00:00-03:00")
    parser.add_argument("--skip-forward-validation", action="store_true", help="Local diagnostic only; default performs the required A/C/E ledger validation.")
    parser.add_argument("--regimes", default="all")
    parser.add_argument("--intrabar-path", choices=["high_first", "low_first"], default="high_first")
    parser.add_argument("--round-trip-spread-bps", type=float)
    parser.add_argument("--market-data-url")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "data/studies/real_a_dmi15_trajectory_regime_backtest/klines"))
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
