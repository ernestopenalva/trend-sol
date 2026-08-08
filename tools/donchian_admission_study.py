from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.donchian_portfolio_replay import (
    PortfolioConfig,
    PortfolioReplayResult,
    PortfolioTrade,
    _discover_symbols,
    replay_portfolio,
    summarize,
    write_trades,
)
from tools.market_selection_study import MarketCandle, load_candle_cache


POLICIES = ("strongest", "alphabetical", "reverse_alphabetical")


def run_admission_study(
    candles: dict[str, Sequence[MarketCandle]],
    config_overrides: dict[str, Any] | None = None,
    replay_start_ms: int | None = None,
    replay_end_ms: int | None = None,
) -> tuple[dict[str, Any], dict[str, PortfolioReplayResult]]:
    overrides = dict(config_overrides or {})
    results: dict[str, PortfolioReplayResult] = {}
    reports: dict[str, Any] = {}
    for policy in POLICIES:
        config = PortfolioConfig(admission_policy=policy, **overrides)
        result = replay_portfolio(
            candles,
            config,
            replay_start_ms=replay_start_ms,
            replay_end_ms=replay_end_ms,
        )
        results[policy] = result
        report = summarize(result, config)
        report["net_usdt_without_top_3"] = _net_without_top(result.trades, 3)
        report["net_usdt_without_top_5"] = _net_without_top(result.trades, 5)
        report["monthly"] = _monthly(result.trades)
        reports[policy] = report

    selections = {
        policy: {(item.symbol, item.opened_ms) for item in result.trades}
        for policy, result in results.items()
    }
    overlap: dict[str, Any] = {}
    for left_index, left in enumerate(POLICIES):
        for right in POLICIES[left_index + 1 :]:
            intersection = selections[left] & selections[right]
            union = selections[left] | selections[right]
            overlap[f"{left}__{right}"] = {
                "common_entries": len(intersection),
                "union_entries": len(union),
                "jaccard": len(intersection) / len(union) if union else None,
            }

    return (
        {
            "study_version": "donchian_admission_sensitivity_v1",
            "policies": reports,
            "entry_overlap": overlap,
        },
        results,
    )


def _net_without_top(trades: Sequence[PortfolioTrade], count: int) -> float:
    values = sorted((item.net_usdt for item in trades), reverse=True)
    return sum(values[count:])


def _monthly(trades: Sequence[PortfolioTrade]) -> dict[str, Any]:
    grouped: dict[str, list[PortfolioTrade]] = defaultdict(list)
    for item in trades:
        month = datetime.fromtimestamp(
            item.closed_ms / 1000,
            tz=timezone.utc,
        ).strftime("%Y-%m")
        grouped[month].append(item)
    return {
        month: {
            "trades": len(items),
            "net_usdt": sum(item.net_usdt for item in items),
            "win_rate": sum(item.net_usdt > 0 for item in items) / len(items),
        }
        for month, items in sorted(grouped.items())
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare Donchian portfolio slot-admission policies."
    )
    parser.add_argument(
        "--hourly-cache-dir",
        default=str(PROJECT_ROOT / "data" / "market_selection" / "klines"),
    )
    parser.add_argument(
        "--universe-cache-dir",
        default=str(PROJECT_ROOT / "data" / "market_bot_replay" / "klines"),
    )
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "data" / "studies" / "donchian_admission"),
    )
    parser.add_argument(
        "--summary-json",
        default=str(
            PROJECT_ROOT
            / "data"
            / "studies"
            / "donchian_admission_sensitivity.json"
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    hourly_cache_dir = Path(args.hourly_cache_dir)
    universe_cache_dir = Path(args.universe_cache_dir)
    symbols = sorted(
        set(
            args.symbols
            or _discover_symbols(universe_cache_dir, hourly_cache_dir)
        )
    )
    if not symbols:
        raise SystemExit("No frozen-universe symbols with hourly candles found")
    candles = {
        symbol: load_candle_cache(hourly_cache_dir / f"{symbol}_1h.jsonl")
        for symbol in symbols
    }
    report, results = run_admission_study(candles)
    report["symbols"] = symbols
    report["coverage"] = {
        symbol: {
            "candles_1h": len(items),
            "first_1h_utc": _iso(items[0].open_time_ms) if items else None,
            "last_1h_utc": _iso(items[-1].close_time_ms) if items else None,
        }
        for symbol, items in candles.items()
    }
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for policy, result in results.items():
        path = output_dir / f"{policy}_trades.jsonl"
        write_trades(path, result.trades)
        report["policies"][policy]["trades_output"] = str(path)

    summary_path = Path(args.summary_json)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("TREND-SOL | Donchian admission sensitivity")
    print(f"Symbols={len(symbols)}")
    for policy in POLICIES:
        item = report["policies"][policy]
        print(
            f"{policy:>20} | entries={item['executed_entries']:>3} | "
            f"net={item['total_net_usdt']:+.4f} | "
            f"without top1={item['total_net_usdt_without_best_trade']:+.4f} | "
            f"without top3={item['net_usdt_without_top_3']:+.4f} | "
            f"PF={item['profit_factor']:.3f} | "
            f"DD={item['realized_max_drawdown_usdt']:.4f}"
        )
    print("Entry overlap:")
    for name, item in report["entry_overlap"].items():
        print(
            f"{name}: common={item['common_entries']} | "
            f"union={item['union_entries']} | jaccard={item['jaccard']:.1%}"
        )
    print(f"Summary JSON: {summary_path}")


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


if __name__ == "__main__":
    main()
