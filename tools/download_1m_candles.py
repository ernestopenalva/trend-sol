"""Download a continuous, closed Binance 1m candle series for read-only studies."""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    args = _args()
    start, end = _parse(args.start), _parse(args.end)
    if end <= start:
        raise SystemExit("--end must be after --start")
    if args.output.exists() and not args.force:
        raise SystemExit(f"Refusing to overwrite {args.output}; choose a new --output or use --force.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    cursor = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    with args.output.open("w", encoding="utf-8") as handle:
        while cursor <= end_ms:
            batch = _request(args.base_url, {"symbol": args.symbol, "interval": "1m", "startTime": cursor, "endTime": end_ms, "limit": 1000}, args.timeout_seconds, args.retries)
            if not batch:
                break
            for row in batch:
                close_ms = int(row[6])
                if close_ms > end_ms:
                    continue
                candle = {
                    "open_time_ms": int(row[0]), "close_time_ms": close_ms,
                    "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]),
                    "quote_volume": float(row[7]), "trades": int(row[8]),
                }
                handle.write(json.dumps(candle, separators=(",", ":")) + "\n")
                written += 1
            handle.flush()
            last_open = int(batch[-1][0])
            next_cursor = last_open + 60_000
            if next_cursor <= cursor:
                raise RuntimeError("Binance kline pagination did not advance.")
            cursor = next_cursor
            print(f"saved {written} closed 1m candles through {datetime.fromtimestamp((cursor - 60_000) / 1000, timezone.utc).isoformat()}", flush=True)
            if len(batch) < 1000:
                break
            if args.request_pause_seconds:
                time.sleep(args.request_pause_seconds)
    print(f"complete: {written} closed 1m candles in {args.output}")


def _request(base_url: str, params: dict[str, object], timeout: float, retries: int) -> list[list[object]]:
    url = f"{base_url.rstrip('/')}/api/v3/klines?{urlencode(params)}"
    for attempt in range(1, retries + 2):
        try:
            with urlopen(url, timeout=timeout) as response:
                value = json.loads(response.read())
            if not isinstance(value, list):
                raise ValueError("Unexpected kline response")
            return [row for row in value if isinstance(row, list) and len(row) >= 9]
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            if attempt > retries:
                raise RuntimeError(f"Binance kline request failed after {attempt} attempts: {exc}") from exc
            delay = min(30.0, 2 ** (attempt - 1))
            print(f"request failed ({exc}); retry {attempt}/{retries} in {delay:.0f}s", flush=True)
            time.sleep(delay)
    raise AssertionError("unreachable")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only downloader of continuous closed Binance 1m candles.")
    parser.add_argument("--start", required=True, help="Inclusive ISO timestamp.")
    parser.add_argument("--end", required=True, help="Inclusive ISO timestamp; only closed candles at or before it are written.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--symbol", default="SOLUSDT")
    parser.add_argument("--base-url", default="https://api.binance.com")
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--request-pause-seconds", type=float, default=0.05)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
