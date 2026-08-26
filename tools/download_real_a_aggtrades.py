"""Download public Binance aggregate trades needed by REAL_A exit validation.

The output is analysis data only.  It does not connect to Testnet, use credentials,
place orders, modify runtime state, or alter production JSONL ledgers.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.real_a_exit_simulator import Seed, load_real_a_seeds


ONE_HOUR_MS = 60 * 60 * 1000


def main() -> None:
    args = _parse_args()
    seeds = load_real_a_seeds(args.ledger, _parse_timestamp(args.since))
    until = _parse_timestamp(args.until) if args.until else None
    windows = merge_windows(
        (seed.opened_at, max(seed.ledger_closed_at, until) if until is not None else seed.ledger_closed_at)
        for seed in seeds
    )
    print(f"REAL_A validation seeds: {len(seeds)}")
    print(f"download windows: {len(windows)}")
    for start, end in windows:
        print(f"  {start.isoformat()} -> {end.isoformat()}")
    if args.dry_run:
        return

    trades: dict[int, dict[str, Any]] = {}
    for start, end in windows:
        for item in fetch_window(args.base_url, args.symbol, start, end, args.request_pause_seconds):
            trade_id = int(item["a"])
            trades[trade_id] = item
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for item in sorted(trades.values(), key=lambda value: (int(value["T"]), int(value["a"]))):
            handle.write(json.dumps(item, separators=(",", ":")) + "\n")
    print(f"wrote {len(trades)} aggTrades to {args.output}")


def fetch_window(base_url: str, symbol: str, start: datetime, end: datetime, pause_seconds: float) -> Iterable[dict[str, Any]]:
    """Fetch every aggregate trade in an inclusive time window, paginating by id."""
    start_ms = _to_ms(start)
    end_ms = _to_ms(end)
    cursor_ms = start_ms
    while cursor_ms <= end_ms:
        chunk_end = min(cursor_ms + ONE_HOUR_MS - 1, end_ms)
        batch = _request(base_url, {"symbol": symbol, "startTime": cursor_ms, "endTime": chunk_end, "limit": 1000})
        while batch:
            for item in batch:
                timestamp = int(item["T"])
                if cursor_ms <= timestamp <= chunk_end:
                    yield item
            last = batch[-1]
            last_timestamp = int(last["T"])
            if len(batch) < 1000 or last_timestamp >= chunk_end:
                break
            if pause_seconds:
                time.sleep(pause_seconds)
            batch = _request(base_url, {"symbol": symbol, "fromId": int(last["a"]) + 1, "limit": 1000})
        cursor_ms = chunk_end + 1
        if pause_seconds:
            time.sleep(pause_seconds)


def merge_windows(windows: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    ordered = sorted(windows)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _request(base_url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/v3/aggTrades?{urlencode(params)}"
    with urlopen(url, timeout=30) as response:
        value = json.loads(response.read())
    if not isinstance(value, list):
        raise ValueError(f"Unexpected Binance response: {value}")
    return [item for item in value if isinstance(item, dict)]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only downloader of public aggTrades for REAL_A exit validation.")
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--since", default="2026-08-19T01:05:00-03:00")
    parser.add_argument("--until", help="Optional UTC/offset timestamp to retain post-real-exit ticks for one later variant.")
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--base-url", default="https://data-api.binance.vision")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "solusdt_aggtrades_real_a_validation.jsonl")
    parser.add_argument("--request-pause-seconds", type=float, default=0.05)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


if __name__ == "__main__":
    main()
