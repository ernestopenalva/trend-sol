"""Diagnostic replay of adverse deterioration before BE_OFF_CB hard stops.

Read-only: this tool does not alter runtime configuration or shadow state.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.indicators.indicators import ema
from src.monitor.market_context import classify_ema_context, classify_macd_context
from tools.cohort_study import _load_config
from tools.ge_replay_study import (
    WARMUP_CANDLES,
    ReplayTrade,
    SignalEvent,
    generate_ge_signals,
    load_ge_market_data,
    run_universe,
)
from tools.market_bot_replay import MINUTE_MS, _round_trip_fees_pct
from tools.market_selection_study import BinancePublicClient, MarketCandle
from tools.real_a_circuit_breaker_replay import CircuitGuard, Rule

LEVELS = (0.50, 0.75, 1.00, 1.10, 1.20, 1.30)
WINDOWS = (5, 10, 15, 30)
VELOCITY_CUTS = (0.025, 0.050, 0.075, 0.100)
COMBO_RULE = Rule("COMBO_DD1P5_PNL4H0P5_MIN2", "COMBO", 1.5, 4, 2)


@dataclass(frozen=True)
class Crossing:
    path: str
    opened_ms: int
    crossed_ms: int
    level_pct: float
    minutes: float
    entry_price: float
    crossing_price: float
    entry_atr: float | None
    atr_distance: float | None
    atr_per_minute: float | None
    adverse: dict[int, float | None]
    velocity: dict[int, float | None]
    acceleration: dict[int, float | None]
    ema_context: str
    macd_context: str
    exit_reason: str
    net_pct: float


def _direction(current: float | None, previous: float | None) -> str:
    if current is None or previous is None:
        return "UNAVAILABLE"
    return "UP" if current > previous else "DOWN" if current < previous else "FLAT"


def build_context_index(candles: Sequence[MarketCandle]) -> list[tuple[int, str, str]]:
    closes = [item.close for item in candles]
    output: list[tuple[int, str, str]] = []
    for index, candle in enumerate(candles):
        # Match EntryEngine's bounded candle buffer: the runtime context is
        # recalculated from at most the latest 300 fully closed candles.
        window = closes[max(0, index - 299) : index + 1]
        series = {period: ema(window, period) for period in (12, 26, 50, 100, 200)}
        def value(period: int, offset: int = 0) -> float | None:
            at = len(window) - 1 - offset
            raw = series[period][at] if at >= 0 else None
            return float(raw) if raw is not None else None

        e50, e100, e200 = value(50), value(100), value(200)
        ema_context = classify_ema_context(
            e50, e100, e200,
            _direction(e50, value(50, 1)),
            _direction(e100, value(100, 1)),
            _direction(e200, value(200, 1)),
        )
        fast, slow = value(12), value(26)
        pfast, pslow = value(12, 1), value(26, 1)
        current = fast - slow if fast is not None and slow is not None else None
        previous = pfast - pslow if pfast is not None and pslow is not None else None
        output.append((candle.close_time_ms, ema_context, classify_macd_context(current, previous)))
    return output


def context_before(index: Sequence[tuple[int, str, str]], at_ms: int) -> tuple[str, str]:
    eligible = [item for item in index if item[0] < at_ms]
    return (eligible[-1][1], eligible[-1][2]) if eligible else ("UNAVAILABLE", "UNAVAILABLE")


def first_crossing(
    trade: ReplayTrade,
    level_pct: float,
    candles: Sequence[MarketCandle],
    intrabar_path: str,
) -> tuple[int, float] | None:
    target = trade.entry_price * (1 - level_pct / 100)
    if trade.trough_price > target:
        return None
    for candle in candles:
        if candle.boundary_ms < trade.opened_ms or candle.boundary_ms > trade.closed_ms:
            continue
        points = (candle.open, candle.high, candle.low, candle.close) if intrabar_path == "HIGH_FIRST" else (candle.open, candle.low, candle.high, candle.close)
        for point in points:
            if point <= target:
                return candle.boundary_ms, target
    return None


def _reference_close(candles_by_ms: dict[int, MarketCandle], at_ms: int) -> float | None:
    candle = candles_by_ms.get(at_ms)
    return candle.close if candle is not None else None


def crossing_metrics(
    *, trade: ReplayTrade, level_pct: float, crossed_ms: int, crossing_price: float,
    entry_atr: float | None, candles_by_ms: dict[int, MarketCandle],
    context_index: Sequence[tuple[int, str, str]], path: str,
) -> Crossing:
    adverse: dict[int, float | None] = {}
    velocity: dict[int, float | None] = {}
    acceleration: dict[int, float | None] = {}
    for window in WINDOWS:
        prior = _reference_close(candles_by_ms, crossed_ms - window * MINUTE_MS)
        older = _reference_close(candles_by_ms, crossed_ms - 2 * window * MINUTE_MS)
        adverse[window] = (crossing_price / prior - 1) * 100 if prior else None
        velocity[window] = adverse[window] / window if adverse[window] is not None else None
        previous_velocity = ((prior / older - 1) * 100 / window) if prior and older else None
        acceleration[window] = velocity[window] - previous_velocity if velocity[window] is not None and previous_velocity is not None else None
    minutes = max(0.0, (crossed_ms - trade.opened_ms) / MINUTE_MS)
    atr_distance = (trade.entry_price - crossing_price) / entry_atr if entry_atr and entry_atr > 0 else None
    ema_context, macd_context = context_before(context_index, crossed_ms)
    return Crossing(
        path, trade.opened_ms, crossed_ms, level_pct, minutes, trade.entry_price,
        crossing_price, entry_atr, atr_distance,
        atr_distance / minutes if atr_distance is not None and minutes > 0 else None,
        adverse, velocity, acceleration, ema_context, macd_context,
        normalize_exit(trade.exit_reason), trade.net_pct,
    )


def normalize_exit(value: str) -> str:
    upper = value.upper()
    if "HARD_STOP" in upper or upper == "HS": return "HS"
    if "PROFIT_LOCK" in upper or upper == "PL": return "PL"
    if "TRAIL" in upper: return "TRAIL"
    return upper


def select_threshold(rows: Sequence[Crossing], window: int, min_sample: int) -> tuple[float, float, int] | None:
    candidates: list[tuple[float, float, int]] = []
    for cut in VELOCITY_CUTS:
        selected = [row for row in rows if row.velocity[window] is not None and row.velocity[window] <= -cut]
        if len(selected) >= min_sample:
            rate = sum(row.exit_reason == "HS" for row in selected) / len(selected)
            candidates.append((cut, rate, len(selected)))
    dominant = [item for item in candidates if item[1] >= 0.60]
    return min(dominant, key=lambda item: item[0]) if dominant else None


def counterfactual(rows: Sequence[Crossing], exit_cost_pct: float, notional: float) -> dict[str, float | int]:
    if not rows:
        return {"triggered": 0, "hs_avoided": 0, "winners_sacrificed": 0, "delta": 0.0}
    level = rows[0].level_pct
    cf_net_pct = -level - exit_cost_pct
    return {
        "triggered": len(rows),
        "hs_avoided": sum(row.exit_reason == "HS" for row in rows),
        "winners_sacrificed": sum(row.exit_reason in {"PL", "TRAIL"} for row in rows),
        "delta": sum((cf_net_pct - row.net_pct) * notional / 100 for row in rows),
    }


def _month(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, BRASILIA_TZ).strftime("%Y-%m")


def print_report(rows: Sequence[Crossing], trades: Sequence[ReplayTrade], path: str, exit_cost_pct: float, notional: float, min_sample: int) -> None:
    print(f"\n{path} | admitted closed trades={len(trades)} | crossings={len(rows)}")
    print("level | reached | HS | HS rate | PL | TRAIL | median min | median ATR | median v5 %/min")
    for level in LEVELS:
        subset = [row for row in rows if row.level_pct == level]
        exits = Counter(row.exit_reason for row in subset)
        med = lambda values: f"{statistics.median(values):.4f}" if values else "N/A"
        print(f"-{level:.2f}% | {len(subset)} | {exits['HS']} | {exits['HS']/len(subset):.1%}" if subset else f"-{level:.2f}% | 0 | 0 | N/A", end="")
        print(f" | {exits['PL']} | {exits['TRAIL']} | {med([x.minutes for x in subset])} | {med([x.atr_distance for x in subset if x.atr_distance is not None])} | {med([x.velocity[5] for x in subset if x.velocity[5] is not None])}")

    print("\nVELOCITY STRATA | adverse velocity <= cut | window=5m")
    print("level | cut %/min | n | HS rate | HS avoided | PL/TRAIL sacrificed | delta $")
    for level in LEVELS:
        level_rows = [row for row in rows if row.level_pct == level]
        for cut in VELOCITY_CUTS:
            selected = [row for row in level_rows if row.velocity[5] is not None and row.velocity[5] <= -cut]
            if not selected: continue
            cf = counterfactual(selected, exit_cost_pct, notional)
            hs_rate = sum(row.exit_reason == "HS" for row in selected) / len(selected)
            print(f"-{level:.2f}% | -{cut:.3f} | {len(selected)} | {hs_rate:.1%} | {cf['hs_avoided']} | {cf['winners_sacrificed']} | {float(cf['delta']):+.4f}")

    print("\nFIRST EXPLORATORY HS-DOMINANT THRESHOLD (>=60% HS, minimum sample enforced)")
    for level in LEVELS:
        selected = select_threshold([row for row in rows if row.level_pct == level], 5, min_sample)
        text = "none" if selected is None else f"v5 <= -{selected[0]:.3f}%/min | HS={selected[1]:.1%} | n={selected[2]}"
        print(f"-{level:.2f}% | {text}")

    print("\nMONTH SPLIT")
    print("month | level | reached | HS rate | median v5 %/min")
    for month in ("2026-06", "2026-07", "2026-08"):
        for level in LEVELS:
            subset = [row for row in rows if _month(row.opened_ms) == month and row.level_pct == level]
            rate = f"{sum(x.exit_reason == 'HS' for x in subset)/len(subset):.1%}" if subset else "N/A"
            speeds = [x.velocity[5] for x in subset if x.velocity[5] is not None]
            print(f"{month} | -{level:.2f}% | {len(subset)} | {rate} | {statistics.median(speeds):.4f}" if speeds else f"{month} | -{level:.2f}% | {len(subset)} | {rate} | N/A")


def _signals(config: dict[str, Any], candles: dict[str, list[MarketCandle]], start_ms: int, end_ms: int) -> list[SignalEvent]:
    signals, _ = generate_ge_signals(config, candles["1m"], candles["5m"], candles["15m"], start_ms, end_ms, 0)
    return signals


def _signal_atr(signals: Iterable[SignalEvent]) -> dict[int, float | None]:
    return {item.boundary_ms: float(item.signal.entry_atr) if item.signal.entry_atr is not None else None for item in signals}


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default="2026-06-01T00:00:00-03:00")
    parser.add_argument("--until", default="2026-09-01T00:00:00-03:00")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--cache-dir", default="data/studies/be_off_cb_deterioration/klines")
    parser.add_argument("--paths", default="HIGH_FIRST,LOW_FIRST")
    parser.add_argument("--capital", type=float)
    parser.add_argument("--output", default="data/studies/be_off_cb_deterioration/crossings.jsonl")
    parser.add_argument("--round-trip-spread-bps", type=float)
    parser.add_argument("--min-sample", type=int, default=10)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--http-timeout-seconds", type=float, default=20.0)
    return parser.parse_args()


def _ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None: raise SystemExit("timestamps require an offset")
    return parsed.astimezone(timezone.utc)


def main() -> None:
    args = _args()
    start, end = _ts(args.since), _ts(args.until)
    if end <= start: raise SystemExit("--until must be after --since")
    raw = _load_config(Path(args.config)); config = deepcopy(effective_config(raw))
    config.setdefault("risk", {})["breakeven"] = {"mode": "off"}
    if args.capital is not None:
        config["capital"]["operational_balance_usdt"] = args.capital
    capital = float(config["capital"]["operational_balance_usdt"])
    spread = float(args.round_trip_spread_bps if args.round_trip_spread_bps is not None else config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0))
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
    data_start = start_ms - WARMUP_CANDLES * 15 * MINUTE_MS
    client = BinancePublicClient(str(config.get("market_data", {}).get("rest_url") or "https://api.binance.com"), args.http_timeout_seconds)
    cache = Path(args.cache_dir)
    candles = {interval: load_ge_market_data(client, str(config.get("symbol") or "SOLUSDT"), interval, data_start, end_ms, cache, args.offline) for interval in ("1m", "5m", "15m")}
    signals = _signals(config, candles, start_ms, end_ms)
    atrs = _signal_atr(signals)
    context_index = build_context_index(candles["5m"])
    minute_index = {item.boundary_ms: item for item in candles["1m"]}
    minute_boundaries = [item.boundary_ms for item in candles["1m"]]
    fees_pct = _round_trip_fees_pct(config)
    exit_cost_pct = fees_pct + spread / 2 / 100
    notional = capital * float(config["capital"]["trade_size_pct"]) / 100
    print("BE_OFF_CB DETERIORATION | DIAGNOSTIC FULL-ENGINE OHLC REPLAY | READ-ONLY")
    print(f"window BRT | {start.astimezone(BRASILIA_TZ).isoformat()} -> {end.astimezone(BRASILIA_TZ).isoformat()} (exclusive)")
    print("CB | realized DD >=1.5% AND closed PnL 4h <=-0.5% AND >=2 closes | cooldown=6h")
    print("crossing | ordered 1m OHLC path; counterfactual price is exact loss level; context is latest fully closed 5m")
    all_rows: list[Crossing] = []
    for path in tuple(item.strip().upper() for item in args.paths.split(",") if item.strip()):
        guard = CircuitGuard(COMBO_RULE, 6.0, capital, notional)
        replay = run_universe(name=f"BE_OFF_CB_{path}", lookback=0, config=config, signals=signals, execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=path, round_trip_spread_bps=spread, admission_guard=guard.allows)
        rows: list[Crossing] = []
        for trade in replay.trades:
            first = bisect.bisect_left(minute_boundaries, trade.opened_ms)
            last = bisect.bisect_right(minute_boundaries, trade.closed_ms)
            trade_candles = candles["1m"][first:last]
            for level in LEVELS:
                hit = first_crossing(trade, level, trade_candles, path)
                if hit is not None:
                    rows.append(crossing_metrics(trade=trade, level_pct=level, crossed_ms=hit[0], crossing_price=hit[1], entry_atr=atrs.get(trade.opened_ms), candles_by_ms=minute_index, context_index=context_index, path=path))
        all_rows.extend(rows)
        print_report(rows, replay.trades, path, exit_cost_pct, notional, args.min_sample)
        print(f"CB crises={guard.crises} | blocked admissions={replay.blocked_circuit} | open at end={len(replay.open_positions)}")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for row in all_rows:
            record = dict(row.__dict__)
            record["opened_at_brt"] = datetime.fromtimestamp(row.opened_ms / 1000, BRASILIA_TZ).isoformat()
            record["crossed_at_brt"] = datetime.fromtimestamp(row.crossed_ms / 1000, BRASILIA_TZ).isoformat()
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"\nDETAIL | {output} | rows={len(all_rows)}")
    print("LIMITS | exploratory in-sample OHLC replay; crossing time has 1-minute resolution; no MTM/CB state/runtime was changed.")


if __name__ == "__main__":
    main()
