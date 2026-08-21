from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.indicators.indicators import rsi
from src.trade_ledger import TradeLedger
from tools.cohort_study import _load_config
from tools.ge_replay_study import load_ge_market_data
from tools.market_context_report import _parse_user_dt, _parse_ts
from tools.market_selection_study import BinancePublicClient, MarketCandle


RSI_PERIOD = 14
SMA_PERIOD = 14
WARMUP_DAYS = 30
THRESHOLD = 70.0


def main() -> None:
    args = _parse_args()
    since = _parse_user_dt(args.since)
    if since is None:
        raise SystemExit("--since is required")
    records = _trades_since(Path(args.ledger), since)
    if not records:
        raise SystemExit("No closed DMI15_SHADOW_C trades matched the requested period.")
    buckets = [_entry_bucket(item) for item in records]
    if any(value is None for value in buckets):
        raise SystemExit("At least one C trade has no usable source_candle_open_time/opened_at.")
    first_bucket = min(int(value) for value in buckets if value is not None)
    last_bucket = max(int(value) for value in buckets if value is not None)
    raw = _load_config(Path(args.config))
    market_data = raw.get("market_data", {}) if isinstance(raw.get("market_data"), dict) else {}
    base_url = str(args.market_data_url or market_data.get("rest_url") or "https://api.binance.com")
    symbol = str(args.symbol or raw.get("symbol") or "SOLUSDT")
    client = BinancePublicClient(base_url, int(args.http_timeout_seconds))
    candles = load_ge_market_data(
        client,
        symbol,
        "5m",
        first_bucket - WARMUP_DAYS * 24 * 60 * 60 * 1000,
        last_bucket + 5 * 60 * 1000 - 1,
        Path(args.cache_dir),
        bool(args.offline),
    )
    ma_by_open = rsi_sma_by_open(candles)
    rows = [
        {
            "record": record,
            "bucket": int(bucket),
            "rsi_ma": ma_by_open.get(int(bucket)),
            "reason": _reason(record),
        }
        for record, bucket in zip(records, buckets)
    ]
    unavailable = [item for item in rows if item["rsi_ma"] is None]
    resolved = [item for item in rows if item["rsi_ma"] is not None]
    _print_report(since, records, resolved, unavailable, bool(args.detail))


def rsi_sma_by_open(candles: Sequence[MarketCandle]) -> dict[int, Optional[float]]:
    values = rsi([item.close for item in candles], RSI_PERIOD)
    ma = sma_optional(values, SMA_PERIOD)
    return {item.open_time_ms: ma[index] for index, item in enumerate(candles)}


def sma_optional(values: Sequence[Optional[float]], period: int) -> list[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be positive")
    output: list[Optional[float]] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        if any(value is None for value in window):
            continue
        output[index] = sum(float(value) for value in window if value is not None) / period
    return output


def _trades_since(path: Path, since: datetime) -> list[Dict[str, Any]]:
    records = TradeLedger(PROJECT_ROOT, path).load()
    output = []
    for item in records:
        if str(item.get("position_type")) != "DMI15_SHADOW":
            continue
        if str(item.get("shadow_kind")) != "DMI15_SHADOW":
            continue
        opened = _parse_ts(item.get("opened_at"))
        if opened is not None and opened >= since:
            output.append(item)
    return sorted(output, key=lambda item: str(item.get("opened_at") or ""))


def _entry_bucket(record: Dict[str, Any]) -> Optional[int]:
    try:
        value = int(record.get("source_candle_open_time"))
        return value if value >= 0 else None
    except (TypeError, ValueError):
        opened = _parse_ts(record.get("opened_at"))
        if opened is None:
            return None
        value = int(opened.timestamp() * 1000)
        return value - value % (5 * 60 * 1000)


def _reason(record: Dict[str, Any]) -> str:
    value = str(record.get("exit_reason") or "UNKNOWN")
    return "BREAKEVEN" if value == "REVIEW_STOP" and str(record.get("final_step")) == "BE" else value


def _print_report(
    since: datetime,
    all_records: Sequence[Dict[str, Any]],
    rows: Sequence[Dict[str, Any]],
    unavailable: Sequence[Dict[str, Any]],
    detail: bool,
) -> None:
    hard_stops = [item for item in rows if item["reason"] == "HARD_STOP"]
    non_hard_stops = [item for item in rows if item["reason"] != "HARD_STOP"]
    print("TREND-SOL | DMI15_SHADOW_C RSI-based MA entry study")
    print(f"Filter | closed C trades | since={since.astimezone(BRASILIA_TZ).strftime('%d/%m %H:%M')}")
    print("Formula | closed 5m candles only | RSI Wilder-14(close) | RSI-based MA=SMA-14(RSI)")
    print("Entry mapping | source_candle_open_time persisted by C; no intrabar or future candle used")
    print()
    print("1. UNIVERSE")
    print(f"closed C trades matched | {len(all_records)}")
    print(f"RSI-based MA resolved | {len(rows)}")
    print(f"RSI-based MA unavailable | {len(unavailable)}")
    print()
    print("2. HARD_STOP VS NON-HARD_STOP")
    print("group | trades | RSI-MA mean | RSI-MA median | RSI-MA>70")
    _print_group("HARD_STOP", hard_stops)
    _print_group("non-HARD_STOP", non_hard_stops)
    print()
    print("3. NON-HARD-STOP BY EXIT")
    print("reason | trades | RSI-MA mean | RSI-MA median | RSI-MA>70")
    for reason in ("BREAKEVEN", "PROFIT_LOCK", "TRAILING"):
        _print_group(reason, [item for item in non_hard_stops if item["reason"] == reason])
    other = [item for item in non_hard_stops if item["reason"] not in {"BREAKEVEN", "PROFIT_LOCK", "TRAILING"}]
    if other:
        _print_group("OTHER", other)
    print()
    _print_interpretation(hard_stops, non_hard_stops)
    if detail:
        print()
        print("4. TRADE DETAIL")
        print("opened | reason | RSI-based MA | >70")
        for item in rows:
            record = item["record"]
            value = float(item["rsi_ma"])
            opened = _parse_ts(record.get("opened_at"))
            stamp = opened.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M") if opened else "n/a"
            print(f"{stamp} | {item['reason']} | {value:.2f} | {'yes' if value > THRESHOLD else 'no'}")
        for item in unavailable:
            record = item["record"]
            print(f"{record.get('opened_at') or 'n/a'} | {item['reason']} | unavailable | n/a")


def _print_group(label: str, rows: Sequence[Dict[str, Any]]) -> None:
    values = [float(item["rsi_ma"]) for item in rows if item["rsi_ma"] is not None]
    over = sum(value > THRESHOLD for value in values)
    mean = f"{statistics.fmean(values):.2f}" if values else "n/a"
    med = f"{statistics.median(values):.2f}" if values else "n/a"
    rate = f"{over / len(values) * 100:.1f}%" if values else "n/a"
    print(f"{label} | {len(values)} | {mean} | {med} | {over} ({rate})")


def _print_interpretation(hard_stops: Sequence[Dict[str, Any]], non_hard_stops: Sequence[Dict[str, Any]]) -> None:
    hard_rate = _over_rate(hard_stops)
    non_rate = _over_rate(non_hard_stops)
    print("4. DIRECT ANSWER")
    if hard_rate is None or non_rate is None:
        print("result | insufficient resolved data")
        return
    difference = hard_rate - non_rate
    ratio = hard_rate / non_rate if non_rate else math.inf if hard_rate else 0.0
    print(f"HARD_STOP RSI-MA>70 | {hard_rate:.1f}%")
    print(f"non-HARD_STOP RSI-MA>70 | {non_rate:.1f}%")
    print(f"difference | {difference:+.1f} percentage points")
    print(f"rate ratio | {'inf' if math.isinf(ratio) else f'{ratio:.2f}x'}")
    if len(hard_stops) < 5:
        print("conclusion | sample of hard stops is too small for a GCR test decision")
    elif difference > 0:
        print("conclusion | directional evidence: high RSI-based MA is more frequent in hard stops; a read-only GCR test may be justified, not a production rule")
    else:
        print("conclusion | no support: high RSI-based MA is not more frequent in hard stops; do not use it to activate GCR")


def _over_rate(rows: Sequence[Dict[str, Any]]) -> Optional[float]:
    values = [float(item["rsi_ma"]) for item in rows if item["rsi_ma"] is not None]
    return sum(value > THRESHOLD for value in values) / len(values) * 100 if values else None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only DMI15_SHADOW_C RSI-based SMA entry study")
    parser.add_argument("--since", required=True)
    parser.add_argument("--ledger", default=str(PROJECT_ROOT / "data/trades/trades_dmi15_shadow.jsonl"))
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--market-data-url")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--cache-dir", default=str(PROJECT_ROOT / "data/studies/dmi15_rsi_ma/klines"))
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    return parser.parse_args()


if __name__ == "__main__":
    main()
