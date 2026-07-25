from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")


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
    _print_sample_net_pnl(records)

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
    print("symbol       opened       closed       entry     exit      net  reason")
    for record in records[-max(0, args.limit) :]:
        print(
            f"{str(record.get('symbol') or ''):<12} "
            f"{_format_brasilia(record.get('opened_at')):<12} "
            f"{_format_brasilia(record.get('closed_at')):<12} "
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


def _print_sample_net_pnl(records: list[dict[str, Any]]) -> None:
    net_usdt = [_net_pnl_usdt(record) for record in records]
    known_net_usdt = [value for value in net_usdt if value is not None]
    notionals = [_notional_usdt(record) for record in records]
    known_notionals = [value for value in notionals if value is not None]
    net_pct_sum = sum(_num(record.get("net_pnl_pct")) for record in records)

    if not known_net_usdt or len(known_net_usdt) != len(records):
        print(f"Sample net PnL: {net_pct_sum:+.2f}% (sum of closed-trade returns)")
        return

    total_notional = sum(known_notionals)
    sample_return = (sum(known_net_usdt) / total_notional * 100) if total_notional else 0.0
    print(
        f"Sample net PnL: {sum(known_net_usdt):+.4f} USDT "
        f"({sample_return:+.2f}% on {total_notional:.2f} USDT traded)"
    )


def _net_pnl_usdt(record: dict[str, Any]) -> float | None:
    notional = _notional_usdt(record)
    net_pct = _optional_num(record.get("net_pnl_pct"))
    if notional is None or net_pct is None:
        return None
    return notional * net_pct / 100


def _notional_usdt(record: dict[str, Any]) -> float | None:
    configured = _optional_num(record.get("position_notional_usdt"))
    if configured is not None and configured > 0:
        return configured
    quantity = _optional_num(record.get("qty"))
    entry = _optional_num(record.get("entry_price"))
    if quantity is None or entry is None or quantity <= 0 or entry <= 0:
        return None
    return quantity * entry


def _format_brasilia(value: Any) -> str:
    if not value:
        return "n/a"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "n/a"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


def _num(value: Any) -> float:
    number = _optional_num(value)
    return number if number is not None else 0.0


def _optional_num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(value: Any) -> str:
    return f"{_num(value):+.2f}%"


if __name__ == "__main__":
    main()
