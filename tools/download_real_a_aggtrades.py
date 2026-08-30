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
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.real_a_exit_simulator import Seed, load_real_a_seeds


ONE_HOUR_MS = 60 * 60 * 1000


def main() -> None:
    args = _parse_args()
    seeds = load_real_a_seeds(args.ledger, _parse_timestamp(args.since))
    if args.opened_until:
        opened_until = _parse_timestamp(args.opened_until)
        seeds = [seed for seed in seeds if seed.opened_at < opened_until]
    if args.exit_reason:
        seeds = [seed for seed in seeds if seed.ledger_reason == args.exit_reason]
    if not seeds:
        raise ValueError("No seeds remain after the requested opened_at / exit_reason filters.")
    if args.closed_until:
        closed_until = _parse_timestamp(args.closed_until)
        seeds = [seed for seed in seeds if seed.ledger_closed_at <= closed_until]
        if not seeds:
            raise ValueError("No seeds remain at or before --closed-until.")
    until = _parse_timestamp(args.until) if args.until else None
    if args.only_validation_grace and args.post_open_hours is not None:
        raise ValueError("--only-validation-grace cannot be combined with --post-open-hours.")
    if args.only_validation_grace:
        windows = merge_windows(
            (seed.ledger_closed_at, seed.ledger_closed_at + timedelta(seconds=args.validation_grace_seconds))
            for seed in seeds
        )
    else:
        windows = merge_windows(
            (seed.opened_at, max(
                seed.ledger_closed_at + timedelta(seconds=args.validation_grace_seconds),
                seed.opened_at + timedelta(hours=args.post_open_hours) if args.post_open_hours is not None else seed.ledger_closed_at,
                until if until is not None else seed.ledger_closed_at,
            ))
            for seed in seeds
        )
    print(f"REAL_A validation seeds: {len(seeds)}")
    print(f"download windows: {len(windows)}")
    for start, end in windows:
        print(f"  {start.isoformat()} -> {end.isoformat()}")
    if args.dry_run:
        return

    if args.no_resume_dedup and args.output.exists():
        raise ValueError("--no-resume-dedup requires a new output path; it intentionally does not load prior IDs.")
    existing_ids = None if args.no_resume_dedup else _existing_trade_ids(args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if args.output.exists() else "w"
    written = 0
    try:
        with args.output.open(mode, encoding="utf-8") as handle:
            for index, (start, end) in enumerate(windows, start=1):
                print(f"[{index}/{len(windows)}] {start.isoformat()} -> {end.isoformat()}", flush=True)
                for item in fetch_window(
                    args.base_url, args.symbol, start, end, args.request_pause_seconds,
                    args.timeout_seconds, args.retries, progress_prefix=f"[{index}/{len(windows)}]",
                ):
                    trade_id = int(item["a"])
                    if existing_ids is not None and trade_id in existing_ids:
                        continue
                    handle.write(json.dumps(item, separators=(",", ":")) + "\n")
                    if existing_ids is not None:
                        existing_ids.add(trade_id)
                    written += 1
                    if written % 1000 == 0:
                        handle.flush()
                        print(f"  saved {written} new aggTrades", flush=True)
    except KeyboardInterrupt:
        print(f"\nInterrupted safely. Partial data retained in {args.output} ({written} new records this run).", flush=True)
        raise
    total = len(existing_ids) if existing_ids is not None else written
    print(f"complete: {written} new aggTrades; {total} total in {args.output}")


def fetch_window(
    base_url: str, symbol: str, start: datetime, end: datetime, pause_seconds: float,
    timeout_seconds: float, retries: int, progress_prefix: str,
) -> Iterable[dict[str, Any]]:
    """Fetch every aggregate trade in an inclusive time window, paginating by id."""
    start_ms = _to_ms(start)
    end_ms = _to_ms(end)
    cursor_ms = start_ms
    while cursor_ms <= end_ms:
        chunk_end = min(cursor_ms + ONE_HOUR_MS - 1, end_ms)
        print(f"  {progress_prefix} requesting {datetime.fromtimestamp(cursor_ms / 1000, timezone.utc).isoformat()}", flush=True)
        batch = _request(base_url, {"symbol": symbol, "startTime": cursor_ms, "endTime": chunk_end, "limit": 1000}, timeout_seconds, retries)
        page = 1
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
            page += 1
            print(f"  {progress_prefix} requesting continuation page {page} (after aggTrade {last['a']})", flush=True)
            batch = _request(base_url, {"symbol": symbol, "fromId": int(last["a"]) + 1, "limit": 1000}, timeout_seconds, retries)
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


def _request(base_url: str, params: dict[str, Any], timeout_seconds: float, retries: int) -> list[dict[str, Any]]:
    url = f"{base_url.rstrip('/')}/api/v3/aggTrades?{urlencode(params)}"
    for attempt in range(1, retries + 2):
        try:
            with urlopen(url, timeout=timeout_seconds) as response:
                value = json.loads(response.read())
            if not isinstance(value, list):
                raise ValueError(f"Unexpected Binance response: {value}")
            return [item for item in value if isinstance(item, dict)]
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            if attempt > retries:
                raise RuntimeError(f"Binance request failed after {attempt} attempts: {exc}") from exc
            delay = min(30.0, 2.0 ** (attempt - 1))
            print(f"  request failed ({exc}); retry {attempt}/{retries} in {delay:.0f}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only downloader of public aggTrades for REAL_A exit validation.")
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--since", default="2026-08-19T01:05:00-03:00")
    parser.add_argument("--until", help="Optional UTC/offset timestamp to retain post-real-exit ticks for one later variant.")
    parser.add_argument("--opened-until", help="Exclude seeds whose opened_at is at or after this timestamp.")
    parser.add_argument("--exit-reason", help="Restrict seeds to one exact ledger exit reason, e.g. BREAKEVEN.")
    parser.add_argument("--post-open-hours", type=float,
                        help="For every selected seed, retain ticks through opened_at plus this many hours.")
    parser.add_argument("--closed-until", help="Freeze the seed set at this ledger closed_at timestamp.")
    parser.add_argument("--validation-grace-seconds", type=float, default=5.0)
    parser.add_argument("--only-validation-grace", action="store_true",
                        help="Append only the small closed_at-to-closed_at+grace windows to an existing output.")
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--base-url", default="https://data-api.binance.vision")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "solusdt_aggtrades_real_a_validation.jsonl")
    parser.add_argument("--request-pause-seconds", type=float, default=0.05)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-resume-dedup", action="store_true",
                        help="Use bounded memory for a new output only; do not support resumable ID de-duplication.")
    return parser.parse_args()


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _to_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _existing_trade_ids(path: Path) -> set[int]:
    if not path.exists():
        return set()
    output = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
                output.add(int(item["a"]))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
    print(f"resuming from {len(output)} existing aggTrades in {path}", flush=True)
    return output


if __name__ == "__main__":
    main()
