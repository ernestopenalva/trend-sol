from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def main() -> None:
    args = _arguments()
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    settings = config.get("instrumentation", {}).get("multi_market_shadow", {})
    ledger_path = PROJECT_ROOT / str(
        args.ledger
        or settings.get("ledger_file", "data/trades/trades_shadow_top3.jsonl")
    )
    state_path = PROJECT_ROOT / str(
        args.state
        or settings.get("state_file", "data/state/multi_market_shadow.json")
    )
    records = _load_jsonl(ledger_path)
    state = _load_json(state_path)

    print("TREND-SOL | Top 3 multi-market shadow")
    selected = sorted(
        state.get("selected", []),
        key=lambda item: item.get("rank", 999),
    )
    if selected:
        selection = " | ".join(
            f"#{item.get('rank')} {item.get('symbol')} "
            f"(24h={_pct(item.get('change_24h_pct'))}, "
            f"7d={_pct(item.get('change_7d_pct'))}, "
            f"spread={_num(item.get('spread_bps')):.2f}bps)"
            for item in selected
        )
        print(f"Selected: {selection}")
    else:
        print("Selected: none")

    open_by_symbol: dict[str, int] = defaultdict(int)
    for position in state.get("positions", []):
        if position.get("status") == "OPEN":
            open_by_symbol[str(position.get("symbol"))] += 1
    open_text = " | ".join(
        f"{symbol}={count}/{settings.get('max_open_positions_per_symbol', 5)}"
        for symbol, count in sorted(open_by_symbol.items())
    )
    print(f"Open virtual positions: {open_text or 'none'}")
    print(f"Closed virtual trades: {len(records)}")

    print("\nBy market:")
    print("symbol       trades  hard_stop    gross       net   avg net       PF")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[str(record.get("symbol") or "UNKNOWN")].append(record)
    if not grouped:
        print("none              0          0   +0.00%    +0.00%    +0.00%      n/a")
    for symbol, items in sorted(grouped.items()):
        gross = sum(_num(item.get("gross_pnl_pct")) for item in items)
        net = sum(_num(item.get("net_pnl_pct")) for item in items)
        hard_stops = sum(item.get("exit_reason") == "HARD_STOP" for item in items)
        avg_net = net / len(items) if items else 0.0
        print(
            f"{symbol:<12} {len(items):>6} {hard_stops:>10} "
            f"{gross:>+8.2f}% {net:>+8.2f}% {avg_net:>+8.2f}% "
            f"{_profit_factor(items):>8}"
        )

    print("\nRecent trades:")
    print("symbol       opened               entry     exit      net  reason")
    for record in records[-max(0, args.limit) :]:
        print(
            f"{str(record.get('symbol') or ''):<12} "
            f"{str(record.get('opened_at') or '')[:16]:<20} "
            f"{_num(record.get('entry_price')):>8.4f} "
            f"{_num(record.get('exit_price')):>8.4f} "
            f"{_num(record.get('net_pnl_pct')):>+7.2f}% "
            f"{record.get('exit_reason') or 'n/a'}"
        )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report Top 3 multi-market shadow results."
    )
    parser.add_argument("--ledger", help="Ledger path relative to the project root.")
    parser.add_argument("--state", help="State path relative to the project root.")
    parser.add_argument("--limit", type=int, default=20)
    return parser.parse_args()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _profit_factor(records: list[dict[str, Any]]) -> str:
    profits = sum(
        max(0.0, _num(record.get("net_pnl_pct")))
        for record in records
    )
    losses = -sum(
        min(0.0, _num(record.get("net_pnl_pct")))
        for record in records
    )
    if losses == 0:
        return "inf" if profits > 0 else "n/a"
    return f"{profits / losses:.2f}"


def _num(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _pct(value: Any) -> str:
    return f"{_num(value):+.2f}%"


if __name__ == "__main__":
    main()
