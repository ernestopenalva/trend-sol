from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.donchian_admission_study import POLICIES, run_admission_study
from tools.donchian_portfolio_replay import write_trades
from tools.market_selection_study import (
    HOUR_MS,
    BinancePublicClient,
    load_candle_cache,
    load_universe_snapshot,
    merge_candles,
    save_candle_cache,
)


DAY_MS = 24 * HOUR_MS
DEFAULT_START = "2025-10-01"
DEFAULT_END = "2026-04-18"


def run_out_of_sample(
    client: BinancePublicClient,
    symbols: list[str],
    cache_dir: Path,
    replay_start_ms: int,
    replay_end_ms: int,
    min_quote_volume_24h: float,
    download_missing: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    data_start_ms = replay_start_ms - 7 * DAY_MS
    candles = {}
    for index, symbol in enumerate(symbols, start=1):
        path = cache_dir / f"{symbol}_1h.jsonl"
        cached = load_candle_cache(path)
        downloaded = []
        if not cached and download_missing:
            downloaded.extend(
                client.klines(symbol, "1h", data_start_ms, replay_end_ms)
            )
        elif cached and download_missing:
            first = min(item.open_time_ms for item in cached)
            last = max(item.close_time_ms for item in cached)
            if first > data_start_ms:
                downloaded.extend(
                    client.klines(
                        symbol,
                        "1h",
                        data_start_ms,
                        min(first - 1, replay_end_ms),
                    )
                )
            if last < replay_end_ms:
                downloaded.extend(
                    client.klines(
                        symbol,
                        "1h",
                        max(last + 1, data_start_ms),
                        replay_end_ms,
                    )
                )
        merged = merge_candles(cached, downloaded)
        selected = [
            item
            for item in merged
            if data_start_ms <= item.open_time_ms and item.close_time_ms <= replay_end_ms
        ]
        if downloaded:
            save_candle_cache(path, merged)
        candles[symbol] = selected
        print(
            f"[{index:>2}/{len(symbols)}] {symbol}: "
            f"{len(selected)} hourly candles ({len(downloaded)} downloaded)"
        )

    report, results = run_admission_study(
        candles,
        config_overrides={"min_quote_volume_24h": min_quote_volume_24h},
        replay_start_ms=replay_start_ms,
        replay_end_ms=replay_end_ms,
    )
    report["sample"] = {
        "replay_start_utc": _iso(replay_start_ms),
        "replay_end_utc": _iso(replay_end_ms),
        "warmup_days": 7,
        "min_quote_volume_24h": min_quote_volume_24h,
        "universe_note": (
            "Currently listed snapshot; eligibility at each signal uses only trailing "
            "24h quote volume. Delisted markets are unavailable (survivorship bias)."
        ),
    }
    report["coverage"] = {
        symbol: {
            "candles_1h": len(items),
            "first_1h_utc": _iso(items[0].open_time_ms) if items else None,
            "last_1h_utc": _iso(items[-1].close_time_ms) if items else None,
        }
        for symbol, items in candles.items()
    }
    return report, results


def _parse_date_start(value: str) -> int:
    return int(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def _parse_date_end(value: str) -> int:
    return _parse_date_start(value) + DAY_MS - 1


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run frozen Donchian policies on a prior out-of-sample window."
    )
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--market-data-url", default="https://api.binance.com")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument(
        "--universe-snapshot",
        default=str(PROJECT_ROOT / "data" / "market_selection" / "universe.jsonl"),
    )
    parser.add_argument(
        "--cache-dir",
        default=str(PROJECT_ROOT / "data" / "studies" / "donchian_oos" / "klines"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "data" / "studies" / "donchian_oos"),
    )
    parser.add_argument("--min-quote-volume-24h", type=float, default=10_000_000)
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    replay_start_ms = _parse_date_start(args.start)
    replay_end_ms = _parse_date_end(args.end)
    if replay_end_ms <= replay_start_ms:
        raise ValueError("end must be after start")
    markets = load_universe_snapshot(Path(args.universe_snapshot))
    symbols = sorted({item.symbol for item in markets})
    if not symbols:
        raise SystemExit("Universe snapshot is empty")
    client = BinancePublicClient(args.market_data_url, args.http_timeout_seconds)
    output_dir = Path(args.output_dir)
    report, results = run_out_of_sample(
        client,
        symbols,
        Path(args.cache_dir),
        replay_start_ms,
        replay_end_ms,
        args.min_quote_volume_24h,
        download_missing=not args.offline,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    for policy in POLICIES:
        path = output_dir / f"{policy}_trades.jsonl"
        write_trades(path, results[policy].trades)
        report["policies"][policy]["trades_output"] = str(path)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("TREND-SOL | frozen Donchian out-of-sample")
    print(f"Period: {args.start} to {args.end} | symbols in snapshot: {len(symbols)}")
    print(f"Historical liquidity floor: {args.min_quote_volume_24h:,.0f} USDT / 24h")
    for policy in POLICIES:
        item = report["policies"][policy]
        print(
            f"{policy:>20} | entries={item['executed_entries']:>4} | "
            f"net={item['total_net_usdt']:+.4f} | "
            f"without top1={item['total_net_usdt_without_best_trade']:+.4f} | "
            f"without top3={item['net_usdt_without_top_3']:+.4f} | "
            f"PF={item['profit_factor']:.3f} | "
            f"DD={item['realized_max_drawdown_usdt']:.4f}"
        )
    print(f"Summary JSON: {summary_path}")


if __name__ == "__main__":
    main()
