"""Read-only paired replay: REAL_A GE15 gate versus Shadow-C DMI15 gate.

This is a triage study.  It does not import or write runtime state, ledgers, or
production configuration.  Both variants share EntryEngine gates 2--4, 1m
admission, the GE replay execution path, fees, slots, and HIGH_FIRST/LOW_FIRST
modeling.  The DMI boolean is deliberately obtained by invoking the existing
Dmi15ShadowRegistry.on_closed_5m code path on a no-persistence probe.
"""
from __future__ import annotations

import argparse
import math
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.monitor.dmi15_shadow import Dmi15ShadowRegistry
from src.monitor.dmi15_trajectory_shadow import Dmi15TrajectoryShadowRegistry
from src.monitor.entry_engine import EntryEngine, EntrySignal
from src.monitor.market_context import MarketContextEngine
from tools.cohort_study import _load_config
from tools.ge_replay_study import (
    MINUTE_MS, SignalEvent, _kline_payload, _stamp, load_ge_market_data,
    parse_cli_datetime, run_universe,
)
from tools.market_bot_replay import NullLogger
from tools.market_selection_study import BinancePublicClient, MarketCandle, _floor_ms

WARMUP_CANDLES = 300
MATCH_TOLERANCE_MS = 90_000


class Dmi15Probe(Dmi15ShadowRegistry):
    """Runs the Shadow-C gate verbatim without positions, ledgers, or state writes."""

    def __init__(self, config: Dict[str, Any]) -> None:
        cfg = deepcopy(config)
        cfg.setdefault("instrumentation", {}).setdefault("dmi15_shadow", {})["enabled"] = True
        self.last_passed = False
        self.evaluations = 0
        super().__init__(PROJECT_ROOT, cfg, NullLogger())  # type: ignore[arg-type]

    def _load_state(self) -> None:
        return

    def _save_state(self) -> None:
        return

    def _open(self, **kwargs: Any) -> None:
        del kwargs

    def _event(self, event: str, bucket: int, **fields: Any) -> None:
        del bucket
        if event == "DMI15_EVALUATED":
            self.last_passed = bool(fields["passed"])
            self.evaluations += 1


class TrajectoryProbe(Dmi15TrajectoryShadowRegistry):
    """Runs Shadow-E's exact closed-5m trajectory path without persistence."""

    def __init__(self, config: Dict[str, Any]) -> None:
        cfg = deepcopy(config)
        cfg.setdefault("instrumentation", {}).setdefault("dmi15_trajectory_shadow", {})["enabled"] = True
        super().__init__(PROJECT_ROOT, cfg, NullLogger())  # type: ignore[arg-type]

    def _load_state(self) -> None:
        return

    def _save_state(self) -> None:
        return

    def _open(self, **kwargs: Any) -> None:
        del kwargs

    def _event(self, event: str, bucket: int, **fields: Any) -> None:
        del event, bucket, fields


class Dmi15GateEngine(EntryEngine):
    """EntryEngine whose Gate 1 is the latest closed-5m Shadow-C probe result."""

    def __init__(self, symbol: str, config: Dict[str, Any], probe: Dmi15Probe) -> None:
        super().__init__(symbol, config, NullLogger(), gate1_mode="dmi15")  # type: ignore[arg-type]
        self.probe = probe
        self.dmi_passed = False

    def refresh_dmi_gate(self, context: Dict[str, Any]) -> None:
        self.probe.on_closed_5m(context, entry_atr=1.0, atr_timeframe="1m", atr_period=14)
        self.dmi_passed = self.probe.last_passed

    def _gate_trend(self) -> bool:
        self._log_gate(1, self.dmi_passed, False, "dmi15_shadow_c_closed_5m")
        return self.dmi_passed


def generate_signals(
    config: Dict[str, Any], candles: Dict[str, Sequence[MarketCandle]], start_ms: int, end_ms: int
) -> tuple[dict[str, list[SignalEvent]], dict[str, Counter[str]], int, Counter[str], dict[str, Counter[str]], dict[int, bool], dict[int, bool]]:
    symbol = str(config["symbol"])
    ge = EntryEngine(symbol, config, NullLogger())  # type: ignore[arg-type]
    probe = Dmi15Probe(config)
    dmi = Dmi15GateEngine(symbol, config, probe)
    context = MarketContextEngine(dmi, config)
    signals = {"A_GE15": [], "B_DMI15": []}
    blocked = {"A_GE15": Counter(), "B_DMI15": Counter()}
    rsi70_total: Counter[str] = Counter()
    rsi70_funnel = {"A_GE15": Counter(), "B_DMI15": Counter()}
    rsi70_by_boundary: dict[int, bool] = {}
    trajectory_by_boundary: dict[int, bool] = {}
    latest_snapshot: Optional[Dict[str, Any]] = None
    indices = {"5m": 0, "15m": 0}
    for candle in candles["1m"]:
        boundary = candle.boundary_ms
        if boundary > end_ms:
            break
        for timeframe in ("15m", "5m"):
            source = candles[timeframe]
            while indices[timeframe] < len(source) and source[indices[timeframe]].boundary_ms <= boundary:
                current = source[indices[timeframe]]
                payload = _kline_payload(current)
                ge.on_kline(f"{symbol.lower()}@kline_{timeframe}", payload)
                dmi.on_kline(f"{symbol.lower()}@kline_{timeframe}", payload)
                if timeframe == "5m":
                    snapshot = context.refresh()
                    if isinstance(snapshot, dict):
                        dmi.refresh_dmi_gate(snapshot)
                        latest_snapshot = snapshot
                indices[timeframe] += 1
        tf_5m = (context.latest or {}).get("tf_5m", {})
        rsi_ma = tf_5m.get("rsi14_sma14") if isinstance(tf_5m, dict) else None
        above_rsi70 = isinstance(rsi_ma, (int, float)) and float(rsi_ma) > 70.0
        if above_rsi70 and boundary >= start_ms:
            rsi70_total["evaluations"] += 1
            rsi70_by_boundary[boundary] = True
        for name, engine in (("A_GE15", ge), ("B_DMI15", dmi)):
            signal = engine.on_kline(f"{symbol.lower()}@kline_1m", _kline_payload(candle))
            if boundary < start_ms:
                continue
            rejected = engine.last_diagnostic.get("last_rejected_gate")
            if rejected in {"trend", "pullback", "exhaustion", "recovery"}:
                blocked[name][str(rejected)] += 1
                if above_rsi70:
                    rsi70_funnel[name][str(rejected)] += 1
            if signal is not None:
                signals[name].append(SignalEvent(boundary, signal))
                if name == "B_DMI15" and latest_snapshot is not None:
                    trajectory_by_boundary[boundary] = TrajectoryProbe(config).on_closed_5m(
                        latest_snapshot, entry_atr=1.0, atr_timeframe="1m", atr_period=14
                    )
                if above_rsi70:
                    rsi70_funnel[name]["entry_stage"] += 1
    return signals, blocked, probe.evaluations, rsi70_total, rsi70_funnel, rsi70_by_boundary, trajectory_by_boundary


def _overlap(left: Sequence[SignalEvent], right: Sequence[SignalEvent]) -> tuple[int, int, int]:
    available = set(range(len(right)))
    common = 0
    for item in left:
        candidates = [index for index in available if abs(right[index].boundary_ms - item.boundary_ms) <= MATCH_TOLERANCE_MS]
        if candidates:
            available.remove(min(candidates, key=lambda index: abs(right[index].boundary_ms - item.boundary_ms)))
            common += 1
    return common, len(left) - common, len(right) - common


def _metrics(result: Any) -> Dict[str, Any]:
    closed = result.trades
    gross = [item.gross_pct for item in closed]
    net = [item.net_pct for item in closed]
    gains, losses = sum(value for value in net if value > 0), abs(sum(value for value in net if value < 0))
    reasons = Counter(item.exit_reason for item in closed)
    return {
        "trades": len(closed), "gross_trade": sum(gross) / len(gross) if gross else 0.0,
        "net_trade": sum(net) / len(net) if net else 0.0,
        "profit_factor": gains / losses if losses else (math.inf if gains else 0.0),
        "hard_stop_rate": reasons["HARD_STOP"] / len(closed) * 100 if closed else 0.0,
        "be": reasons["BREAKEVEN"], "pl": reasons["PROFIT_LOCK"], "trail": reasons["TRAILING"],
        "slots_full": result.full_slot_minutes / result.observed_minutes * 100 if result.observed_minutes else 0.0,
        "max_simultaneous": result.max_simultaneous_positions,
        "capacity": result.blocked_slots, "admission_1m": result.blocked_candle_limit,
        "spacing": result.blocked_spacing, "admitted": len(result.entries()),
        "open_end": len(result.open_positions),
    }


def main() -> None:
    args = _args()
    config = effective_config(_load_config(Path(args.config)))
    start = parse_cli_datetime(args.since)
    if start is None:
        raise SystemExit("--since is required")
    end = parse_cli_datetime(args.until) if args.until else datetime.now(timezone.utc)
    start_ms, end_ms = int(start.timestamp() * 1000), _floor_ms(int(end.timestamp() * 1000), MINUTE_MS) - 1
    warmup_ms = WARMUP_CANDLES * 15 * MINUTE_MS
    client = BinancePublicClient(str(config["market_data"]["rest_url"]), int(args.http_timeout_seconds))
    cache = Path(args.cache_dir)
    candles = {tf: load_ge_market_data(client, str(config["symbol"]), tf, start_ms - warmup_ms, end_ms, cache, args.offline) for tf in ("1m", "5m", "15m")}
    signals, gate_blocks, dmi_evaluations, rsi70_total, rsi70_funnel, rsi70_by_boundary, trajectory_by_boundary = generate_signals(config, candles, start_ms, end_ms)
    spread = float(config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0))
    results = {name: run_universe(name=name, lookback=3, config=config, signals=value, execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=args.intrabar_path.upper(), round_trip_spread_bps=spread) for name, value in signals.items()}
    print("TREND-SOL | GE15 versus Shadow-C DMI15 paired triage")
    print(f"period | {_stamp(start_ms)} to {_stamp(end_ms)} | warmup | {WARMUP_CANDLES}x15m | path | {args.intrabar_path.upper()} | modeled round-trip spread | {spread:.1f}bp")
    print(f"DMI15 probe evaluations via Dmi15ShadowRegistry.on_closed_5m | {dmi_evaluations}")
    print("metric | A GE15+G2+G3+G4 | B DMI15+G2+G3+G4")
    metrics = {name: _metrics(value) for name, value in results.items()}
    for key, label, pct in (("trades", "trades fechados", False), ("open_end", "open at end", False), ("admitted", "entries admitted", False), ("gross_trade", "gross/trade", True), ("net_trade", "net/trade", True), ("profit_factor", "profit factor", False), ("hard_stop_rate", "HARD_STOP rate", True), ("be", "BREAKEVEN", False), ("pl", "PROFIT_LOCK", False), ("trail", "TRAILING", False), ("slots_full", "slots full", True), ("max_simultaneous", "max simultaneous", False), ("trend", "blocked Gate 1", False), ("pullback", "blocked G2", False), ("exhaustion", "blocked G3", False), ("recovery", "blocked G4", False), ("capacity", "blocked capacity", False), ("admission_1m", "blocked admission 1m", False), ("spacing", "blocked spacing (1 ATR)", False)):
        values = [gate_blocks[name][key] if key in {"trend", "pullback", "exhaustion", "recovery"} else metrics[name][key] for name in ("A_GE15", "B_DMI15")]
        fmt = lambda value: f"{value:+.3f}%" if pct else ("inf" if isinstance(value, float) and math.isinf(value) else str(value))
        print(f"{label} | {fmt(values[0])} | {fmt(values[1])}")
    signal_overlap = _overlap(signals["A_GE15"], signals["B_DMI15"])
    trade_overlap = _overlap([SignalEvent(item.opened_ms, EntrySignal("", item.entry_price, "", 0, 0, "", 0)) for item in results["A_GE15"].entries()], [SignalEvent(item.opened_ms, EntrySignal("", item.entry_price, "", 0, 0, "", 0)) for item in results["B_DMI15"].entries()])
    print("signal overlap (90s) | common={} | A-only={} | B-only={}".format(*signal_overlap))
    print("opened trade overlap (90s) | common={} | A-only={} | B-only={}".format(*trade_overlap))
    print("RSI-MA14 >70 diagnostic before any gate | evaluations={}".format(rsi70_total["evaluations"]))
    print("RSI-MA14 >70 funnel | variant | blocked G1 | blocked G2 | blocked G3 | blocked G4 | reaches entry stage")
    for name in ("A_GE15", "B_DMI15"):
        values = rsi70_funnel[name]
        print("RSI-MA14 >70 funnel | {} | {} | {} | {} | {} | {}".format(name, values["trend"], values["pullback"], values["exhaustion"], values["recovery"], values["entry_stage"]))
    affected = [item for item in results["A_GE15"].trades if rsi70_by_boundary.get(item.opened_ms, False)]
    affected_reasons = Counter(item.exit_reason for item in affected)
    print("A trades with entry RSI-MA14 >70 | closed={} | HARD_STOP={} | BREAKEVEN={} | PROFIT_LOCK={} | TRAILING={} | gross aggregate={:+.3f}%".format(len(affected), affected_reasons["HARD_STOP"], affected_reasons["BREAKEVEN"], affected_reasons["PROFIT_LOCK"], affected_reasons["TRAILING"], sum(item.gross_pct for item in affected)))
    print("B closed trades split by exact Shadow-E trajectory at entry")
    for label, expected in (("TRUE", True), ("FALSE", False)):
        group = [item for item in results["B_DMI15"].trades if trajectory_by_boundary.get(item.opened_ms, False) is expected]
        reasons = Counter(item.exit_reason for item in group)
        gross = sum(item.gross_pct for item in group)
        hard_stop_rate = reasons["HARD_STOP"] / len(group) * 100 if group else 0.0
        print("trajectory={} | trades={} | gross total={:+.3f}% | gross/trade={:+.3f}% | HARD_STOP rate={:.2f}% | BE={} | PL={} | TRAIL={}".format(label, len(group), gross, gross / len(group) if group else 0.0, hard_stop_rate, reasons["BREAKEVEN"], reasons["PROFIT_LOCK"], reasons["TRAILING"]))
    print("NOTE | Relative replay comparison only; 1m OHLC exits are not forward PnL reconstruction.")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only paired GE15 versus Shadow-C DMI15 replay.")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    parser.add_argument("--intrabar-path", choices=["high_first", "low_first"], default="high_first")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "data/studies/gate1_dmi_ge_triage/klines"))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    return parser.parse_args()


if __name__ == "__main__":
    main()
