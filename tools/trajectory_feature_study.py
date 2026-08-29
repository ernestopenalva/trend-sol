"""Read-only 5m DMI/EMA trajectory study for 120-minute SOL returns."""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.indicators.indicators import dmi_adx, ema
from tools.ge_replay_study import load_ge_market_data
from tools.market_selection_study import BinancePublicClient, MarketCandle


STEP = 3  # 3 closed 5m candles = 15 minutes
HORIZON = 24  # 24 closed 5m candles = 120 minutes


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
FEATURES = ("F1", "F2", "F3", "F4", "F5")


def main() -> None:
    args = _args()
    selected = tuple(item for item in REGIMES if args.regimes == "all" or item.name.lower() in args.regimes.split(","))
    if not selected:
        raise SystemExit("--regimes must select lateral, alta, baixa, or all")
    start_ms = min(int(item.start.timestamp() * 1000) for item in selected) - 300 * 5 * 60_000
    end_ms = max(int(item.end.timestamp() * 1000) - 1 for item in selected)
    client = BinancePublicClient(args.market_data_url, args.http_timeout_seconds)
    candles = load_ge_market_data(client, "SOLUSDT", "5m", start_ms, end_ms, Path(args.cache_dir), args.offline)
    rows = _rows(candles)
    print("TREND-SOL | read-only DMI/EMA short-trajectory feature study")
    print("Closed 5m only | DMI Wilder/14 | EMA20/EMA50 | trajectory=3 candles (15m) | target=24 candles (120m)")
    print("F1=+DI--DI | F2=spread[t]-spread[t-3] | F3=(EMA20[t]-EMA20[t-3])/close[t] | F4=(EMA50[t]-EMA50[t-3])/close[t] | F5=((EMA20-EMA50)[t]-(EMA20-EMA50)[t-3])/close[t]")
    print("Quartiles are descriptive only: 5m observations overlap strongly over the 120m target horizon.")
    results: dict[str, list[dict[str, float]]] = {}
    for regime in selected:
        subset = _within(rows, regime)
        results[regime.name] = subset
        _report_base(regime.name, subset)
        for feature in FEATURES:
            _report_feature(regime.name, feature, subset)
        _report_crosses(regime.name, subset)
    aggregate = [row for regime in selected for row in results[regime.name]]
    _report_base("AGGREGATE", aggregate)
    for feature in FEATURES:
        _report_feature("AGGREGATE", feature, aggregate)
    _summary(selected, results, aggregate)
    print("\nLIMITATIONS: regime labels are fixed and retrospective; observations overlap and are not independent tests; no gate, score, shadow, runtime or configuration was changed.")


def _rows(candles: Sequence[MarketCandle]) -> list[dict[str, float]]:
    highs, lows, closes = [item.high for item in candles], [item.low for item in candles], [item.close for item in candles]
    plus, minus, _adx = dmi_adx(highs, lows, closes, 14)
    ema20, ema50 = ema(closes, 20), ema(closes, 50)
    output = []
    for index, candle in enumerate(candles):
        if index < STEP or index + HORIZON >= len(candles):
            continue
        values = (plus[index], minus[index], plus[index - STEP], minus[index - STEP], ema20[index], ema20[index - STEP], ema50[index], ema50[index - STEP])
        if any(value is None for value in values):
            continue
        spread = float(plus[index]) - float(minus[index])
        old_spread = float(plus[index - STEP]) - float(minus[index - STEP])
        gap = float(ema20[index]) - float(ema50[index])
        old_gap = float(ema20[index - STEP]) - float(ema50[index - STEP])
        close = candle.close
        output.append({"time": float(candle.boundary_ms), "close": close, "future": closes[index + HORIZON] / close - 1, "F1": spread, "F2": spread - old_spread, "F3": (float(ema20[index]) - float(ema20[index - STEP])) / close, "F4": (float(ema50[index]) - float(ema50[index - STEP])) / close, "F5": (gap - old_gap) / close, "pre15": close / closes[index - 3] - 1 if index >= 3 else math.nan, "pre30": close / closes[index - 6] - 1 if index >= 6 else math.nan, "post15": closes[index + 3] / close - 1, "post30": closes[index + 6] / close - 1, "post60": closes[index + 12] / close - 1, "post120": closes[index + 24] / close - 1})
    return output


def _within(rows: Sequence[dict[str, float]], regime: Regime) -> list[dict[str, float]]:
    start, end = int(regime.start.timestamp() * 1000), int(regime.end.timestamp() * 1000)
    return [row for row in rows if start <= row["time"] < end and row["time"] + HORIZON * 5 * 60_000 < end]


def _report_base(name: str, rows: Sequence[dict[str, float]]) -> None:
    future = [row["future"] for row in rows]
    nominal, nonoverlap = len(rows), _nonoverlap(rows)
    print(f"\nBASE {name} | nominal observations={nominal} | non-overlapping 120m windows={nonoverlap} | future 2h mean={_pct(_mean(future))} | median={_pct(_median(future))} | >0={_ratio(sum(value > 0 for value in future), nominal)} | <0={_ratio(sum(value < 0 for value in future), nominal)}")


def _report_feature(name: str, feature: str, rows: Sequence[dict[str, float]]) -> None:
    baseline = [row["future"] for row in rows]
    corr = _correlation([row[feature] for row in rows], baseline)
    print(f"{name} {feature} | n={len(rows)} | corr_2h={corr:+.3f} | future mean={_pct(_mean(baseline))} | median={_pct(_median(baseline))}")
    ordered = sorted(rows, key=lambda row: row[feature])
    for number, group in enumerate(_quartiles(ordered), start=1):
        values, future = [row[feature] for row in group], [row["future"] for row in group]
        print(f"  Q{number} range=[{_num(min(values))},{_num(max(values))}] n={len(group)} mean={_pct(_mean(future))} delta_mean={_pct(_mean(future)-_mean(baseline))} median={_pct(_median(future))} delta_median={_pct(_median(future)-_median(baseline))} >0={_ratio(sum(value>0 for value in future),len(group))} <0={_ratio(sum(value<0 for value in future),len(group))}")


def _report_crosses(name: str, rows: Sequence[dict[str, float]]) -> None:
    print(f"{name} sign-cross latency (F2-F5; rows require 30m prior and 120m future inside the base)")
    for feature in ("F2", "F3", "F4", "F5"):
        events = {"NEG_TO_POS": [], "POS_TO_NEG": []}
        for previous, current in zip(rows, rows[1:]):
            if current["time"] - previous["time"] != 5 * 60_000 or current["time"] - rows[0]["time"] < 30 * 60_000:
                continue
            direction = "NEG_TO_POS" if previous[feature] <= 0 < current[feature] else "POS_TO_NEG" if previous[feature] >= 0 > current[feature] else None
            if direction:
                events[direction].append(current)
        for direction, group in events.items():
            persistence = _persistence(rows, feature, group, direction)
            print(f"  {feature} {direction} n={len(group)} persistence mean/median={_mean(persistence):.1f}/{_median(persistence):.1f}m | pre15={_pct(_mean([x['pre15'] for x in group]))}/{_pct(_median([x['pre15'] for x in group]))} | pre30={_pct(_mean([x['pre30'] for x in group]))}/{_pct(_median([x['pre30'] for x in group]))} | post15={_pct(_mean([x['post15'] for x in group]))}/{_pct(_median([x['post15'] for x in group]))} | post30={_pct(_mean([x['post30'] for x in group]))}/{_pct(_median([x['post30'] for x in group]))} | post60={_pct(_mean([x['post60'] for x in group]))}/{_pct(_median([x['post60'] for x in group]))} | post120={_pct(_mean([x['post120'] for x in group]))}/{_pct(_median([x['post120'] for x in group]))}")


def _persistence(rows: Sequence[dict[str, float]], feature: str, events: Sequence[dict[str, float]], direction: str) -> list[float]:
    indexed = {row["time"]: index for index, row in enumerate(rows)}
    sign = 1 if direction == "NEG_TO_POS" else -1
    output = []
    for event in events:
        index, end = indexed[event["time"]], indexed[event["time"]]
        while end + 1 < len(rows) and rows[end + 1]["time"] - rows[end]["time"] == 5 * 60_000 and rows[end + 1][feature] * sign > 0:
            end += 1
        output.append((end - index + 1) * 5.0)
    return output


def _summary(regimes: Sequence[Regime], results: dict[str, list[dict[str, float]]], aggregate: Sequence[dict[str, float]]) -> None:
    print("\nSUMMARY (aggregate quartiles; inspect regime-specific output above before judging direction)")
    print("feature | corr 2h | Q1 mean | Q2 mean | Q3 mean | Q4 mean | Q1 >0 | Q4 >0")
    for feature in FEATURES:
        ordered = sorted(aggregate, key=lambda row: row[feature])
        groups = _quartiles(ordered)
        print(f"{feature} | {_correlation([row[feature] for row in aggregate],[row['future'] for row in aggregate]):+.3f} | " + " | ".join([f"{_pct(_mean([row['future'] for row in group]))}" for group in groups]) + f" | {_ratio(sum(row['future']>0 for row in groups[0]),len(groups[0]))} | {_ratio(sum(row['future']>0 for row in groups[-1]),len(groups[-1]))}")


def _quartiles(rows: Sequence[Any]) -> list[list[Any]]:
    count = len(rows)
    return [list(rows[count * part // 4: count * (part + 1) // 4]) for part in range(4)]


def _nonoverlap(rows: Sequence[dict[str, float]]) -> int:
    chosen, next_time = 0, -math.inf
    for row in sorted(rows, key=lambda value: value["time"]):
        if row["time"] >= next_time:
            chosen += 1
            next_time = row["time"] + HORIZON * 5 * 60_000
    return chosen


def _correlation(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2: return 0.0
    a, b = _mean(left), _mean(right)
    numerator = sum((x-a)*(y-b) for x,y in zip(left,right))
    denominator = math.sqrt(sum((x-a)**2 for x in left) * sum((y-b)**2 for y in right))
    return numerator / denominator if denominator else 0.0


def _mean(values: Sequence[float]) -> float: return statistics.fmean(values) if values else 0.0
def _median(values: Sequence[float]) -> float: return statistics.median(values) if values else 0.0
def _ratio(value: int, total: int) -> str: return f"{value / total:.1%}" if total else "n/a"
def _pct(value: float) -> str: return f"{value * 100:+.3f}%"
def _num(value: float) -> str: return f"{value:+.6f}"


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only DMI/EMA trajectory diagnostic over fixed 5m/2h windows.")
    parser.add_argument("--regimes", default="all")
    parser.add_argument("--market-data-url", default="https://api.binance.com")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "data/studies/real_a_ladder_regime_backtest/klines"))
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


if __name__ == "__main__": main()
