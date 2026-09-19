"""Export the previously inspected REAL_A trade list with closed-candle contexts.

This is a read-only inspection utility.  It deliberately consumes the existing
REAL_A control export so its EMA-stack labels are not recomputed: the prior
study's BULLISH_STACK/BEARISH_STACK/MIXED classifications are converted only
to A/B/M.  Slow GE and DMI labels use only candles whose close boundary is no
later than the trade timestamp.
"""
from __future__ import annotations

import argparse
import csv
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.indicators.indicators import dmi_adx
from tools.market_selection_study import MarketCandle, load_candle_cache

DEFAULT_INPUT = PROJECT_ROOT / "data/analysis/real_a_control_20260801_20260826_brt.txt"
DEFAULT_CACHE = PROJECT_ROOT / "data/studies/real_a_dmi15_trajectory_regime_backtest/klines"
DEFAULT_OUTPUT = PROJECT_ROOT / "data/analysis/real_a_control_20260801_20260826_brt_contexts.txt"


def main() -> None:
    args = _args()
    source = Path(args.input)
    rows = _read_rows(source)
    if not rows:
        raise SystemExit("The input trade list has no data rows.")

    first = min(_timestamp(row["opened"]) for row in rows)
    last = max(_timestamp(row["closed"]) for row in rows)
    # This matches the earlier EMA-context study's historical warmup.  It is
    # also far more than the 15-minute GE and DMI-14 minimum requirements.
    warmup_start = first - timedelta(days=7)
    cache = Path(args.cache_dir)
    candles_5m = _candles(cache / "SOLUSDT_5m.jsonl", warmup_start, last)
    candles_15m = _candles(cache / "SOLUSDT_15m.jsonl", warmup_start, last)
    contexts = _contexts(candles_5m, candles_15m)

    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter="|", lineterminator="\n")
        writer.writerow((
            "opened", "closed", "age", "entry", "exit", "reason",
            "entry_ema", "exit_ema", "entry_gelento", "exit_gelento",
            "entry_dmi", "exit_dmi",
        ))
        for row in rows:
            opened, closed = _timestamp(row["opened"]), _timestamp(row["closed"])
            writer.writerow((
                row["opened"], row["closed"], row["age"], row["entry"], row["exit"], row["exit reason"],
                _ema_label(row["context na abertura"]), _ema_label(row["context na saida"]),
                _at(contexts["slow_ge"], opened), _at(contexts["slow_ge"], closed),
                _at(contexts["dmi"], opened), _at(contexts["dmi"], closed),
            ))
    print(f"Read-only context list written: {destination} ({len(rows)} trades)")
    print("EMA labels were copied from the prior export and converted BULLISH/BEARISH/MIXED -> A/B/M.")
    print("Slow GE: closed 15m t vs t-3. DMI: Wilder 14 on closed 5m candles. No future candle was used.")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only REAL_A trade list with A/B/M contexts.")
    parser.add_argument("--input", default=str(DEFAULT_INPUT))
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    return parser.parse_args()


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="|")
        required = {"opened", "closed", "age", "entry", "exit", "exit reason", "context na abertura", "context na saida"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise SystemExit(f"Unexpected prior-export columns in {path}.")
        return [dict(row) for row in reader]


def _timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%d/%m/%Y %H:%M:%S BRT").replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)


def _candles(path: Path, start: datetime, end: datetime) -> list[MarketCandle]:
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    all_candles = load_candle_cache(path)
    selected = [item for item in all_candles if start_ms <= item.boundary_ms <= end_ms]
    if not selected:
        raise SystemExit(f"No cached candles in requested range: {path}")
    return selected


def _contexts(candles_5m: Sequence[MarketCandle], candles_15m: Sequence[MarketCandle]) -> dict[str, tuple[list[int], list[str | None]]]:
    plus, minus, _ = dmi_adx(
        (item.high for item in candles_5m),
        (item.low for item in candles_5m),
        (item.close for item in candles_5m),
        14,
    )
    dmi_times = [item.boundary_ms for item in candles_5m]
    dmi_labels = [_dmi_label(plus, minus, index) for index in range(len(candles_5m))]
    slow_times = [item.boundary_ms for item in candles_15m]
    slow_labels = [_slow_ge_label(candles_15m, index) for index in range(len(candles_15m))]
    return {"dmi": (dmi_times, dmi_labels), "slow_ge": (slow_times, slow_labels)}


def _slow_ge_label(candles: Sequence[MarketCandle], index: int) -> str | None:
    if index < 3:
        return None
    now, before = candles[index], candles[index - 3]
    if now.high > before.high and now.low > before.low:
        return "A"
    if now.high < before.high and now.low < before.low:
        return "B"
    return "M"


def _dmi_label(plus: Sequence[float | None], minus: Sequence[float | None], index: int) -> str | None:
    if index < 3 or any(value is None for value in (plus[index], plus[index - 1], plus[index - 3], minus[index], minus[index - 1], minus[index - 3])):
        return None
    p, p1, p3 = float(plus[index]), float(plus[index - 1]), float(plus[index - 3])
    m, m1, m3 = float(minus[index]), float(minus[index - 1]), float(minus[index - 3])
    if p > p3 and m < m3 and p > m and p > p1 and m < m1:
        return "A"
    if m > m3 and p < p3 and m > p and m > m1 and p < p1:
        return "B"
    return "M"


def _at(series: tuple[list[int], list[str | None]], instant: datetime) -> str:
    boundaries, labels = series
    index = bisect_right(boundaries, int(instant.timestamp() * 1000)) - 1
    if index < 0 or labels[index] is None:
        raise SystemExit("A trade timestamp precedes the available closed-candle context.")
    return str(labels[index])


def _ema_label(value: str) -> str:
    labels = {"BULLISH_STACK": "A", "BEARISH_STACK": "B", "MIXED": "M"}
    try:
        return labels[value]
    except KeyError as error:
        raise SystemExit(f"Unexpected existing EMA label: {value!r}") from error


if __name__ == "__main__":
    main()
