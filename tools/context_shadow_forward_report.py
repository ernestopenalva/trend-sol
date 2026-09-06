"""Forward report for the current REAL_A context-shadow cohort only."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.trade_ledger import TradeLedger
from tools.market_context_report import _parse_ts
from src.console_utils import BRASILIA_TZ
from datetime import datetime, timezone


ARMS = (
    ("REAL_A", "data/trades/trades_B.jsonl", "data/state/open_positions.json"),
    ("DMI15_TRAJECTORY_CONTEXT_SHADOW", "data/trades/trades_dmi15_trajectory_context_shadow.jsonl", "data/state/dmi15_trajectory_context_shadow.json"),
    ("SLOW_GE_CONTEXT_SHADOW", "data/trades/trades_slow_ge_context_shadow.jsonl", "data/state/slow_ge_context_shadow.json"),
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare the active REAL_A context-shadow forward cohort.")
    parser.add_argument("--since", required=True, help="Coorte inicial, em BRT (DD/MM/AAAA HH:MM) ou ISO 8601.")
    parser.add_argument("--until", help="Fim opcional, em BRT ou ISO 8601.")
    parser.add_argument("--since-field", choices=("opened_at", "closed_at"), default="opened_at")
    parser.add_argument("--capital", type=float, default=100.0, help="Capital inicial teórico usado somente no DD normalizado.")
    args = parser.parse_args()
    if args.capital <= 0:
        raise SystemExit("--capital must be positive")
    since, until = _parse_user_dt(args.since), _parse_user_dt(args.until)
    print("TREND-SOL | nova coorte: REAL_A vs context shadows")
    print(f"Filtro: {args.since_field} desde {args.since}" + (f" até {args.until}" if args.until else ""))
    print("strategy | closed | gross | gross/trade | net | net/trade | HS rate | TRAIL rate | HS | BE | PL | TRAIL | avg age | median age | open now | blocked context | context unavailable | capacity | same 5m | spacing | max simultaneous")
    records_by_arm: dict[str, list[dict[str, Any]]] = {}
    for name, ledger_path, state_path in ARMS:
        records = _records(PROJECT_ROOT / ledger_path, since, until, args.since_field)
        records_by_arm[name] = records
        state = _load_state(PROJECT_ROOT / state_path)
        _line(name, records, state, real_a=name == "REAL_A")
    _normalized_phantom_comparison(records_by_arm, args.capital)
    print("\nPrimary metric declared before forward: gross/trade. Net/trade is supplementary only.")
    print("All three arms use ladder A; EMA telemetry is observational and absent from this decision path.")


def _records(path: Path, since: Any, until: Any, field: str) -> list[dict[str, Any]]:
    records = TradeLedger(PROJECT_ROOT, path).load()
    if path.name == "trades_B.jsonl":
        records = [
            item for item in records
            if not item.get("phantom") and not item.get("shadow_kind") and item.get("position_type") == "BOT_EXIT"
        ]
    return [item for item in records if _within(item.get(field), since, until)]


def _within(value: Any, since: Any, until: Any) -> bool:
    timestamp = _parse_ts(value)
    return timestamp is not None and (since is None or timestamp >= since) and (until is None or timestamp <= until)


def _parse_user_dt(value: str | None):
    if not value:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m %H:%M", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%d/%m %H:%M":
                parsed = parsed.replace(year=datetime.now(BRASILIA_TZ).year)
            return parsed.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or BRASILIA_TZ).astimezone(timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid date/time: {value}") from exc


def _line(name: str, records: list[dict[str, Any]], state: Any, *, real_a: bool) -> None:
    gross = [_number(item.get("gross_pnl_pct")) for item in records]
    net = [_number(item.get("net_pnl_pct")) for item in records]
    ages = [_number(item.get("age_seconds")) for item in records]
    gross, net, ages = [item for item in gross if item is not None], [item for item in net if item is not None], [item for item in ages if item is not None]
    reasons = Counter(str(item.get("exit_reason") or "UNKNOWN") for item in records)
    count = len(records)
    open_positions = _open_positions(state, real_a=real_a)
    counters = ("- | - | - | - | - | -") if real_a else " | ".join(
        str(state.get(key, 0))
        for key in ("blocked_context", "blocked_context_unavailable", "blocked_capacity", "blocked_same_5m", "blocked_spacing", "max_simultaneous_positions")
    )
    print(
        f"{name} | {count} | {sum(gross):+.2f}% | {_mean(gross):+.2f}% | {sum(net):+.2f}% | {_mean(net):+.2f}% | "
        f"{(reasons['HARD_STOP'] / count * 100 if count else 0):.1f}% | {(reasons['TRAILING'] / count * 100 if count else 0):.1f}% | "
        f"{reasons['HARD_STOP']} | {reasons['BREAKEVEN']} | {reasons['PROFIT_LOCK']} | {reasons['TRAILING']} | "
        f"{(_mean(ages) / 3600 if ages else 0):.2f}h | {(median(ages) / 3600 if ages else 0):.2f}h | "
        f"{len(open_positions)} | {counters}"
    )


def _normalized_phantom_comparison(records_by_arm: dict[str, list[dict[str, Any]]], capital: float) -> None:
    """Compare the recorded arms after replacing only REAL_A Testnet exits.

    This is a trade-by-trade re-emission, not a slippage adjustment or a
    new simulation: entries, quantities, fees and close ordering stay as
    recorded. Context shadows retain their recorded phantom exits.
    """
    stats = {
        name: _normalized_realized_stats(records_by_arm[name], capital, real_a=name == "REAL_A")
        for name, _, _ in ARMS
    }
    print("\nNormalized phantom comparison")
    print("arm | net $ | realized max DD $")
    for name, _, _ in ARMS:
        item = stats[name]
        if item is None:
            print(f"{name} | unavailable | unavailable")
        else:
            print(f"{name} | ${item['net']:+.4f} | ${item['drawdown']:.4f}")
    real = stats["REAL_A"]
    for name, _, _ in ARMS:
        if name == "REAL_A":
            continue
        item = stats[name]
        if real is None or item is None:
            print(f"{name} - REAL_A | unavailable | —")
        else:
            print(f"{name} - REAL_A | ${item['net'] - real['net']:+.4f} | —")


def _normalized_realized_stats(
    records: list[dict[str, Any]], capital: float, *, real_a: bool
) -> dict[str, float] | None:
    events: list[tuple[datetime, float]] = []
    for record in records:
        entry = _number(record.get("entry_price"))
        quantity = _number(record.get("qty"))
        notional = _number(record.get("position_notional_usdt"))
        fee_pct = _number(record.get("estimated_fees_pct"))
        closed = _parse_ts(record.get("closed_at"))
        exit_price = _number(record.get("exit_trigger_price")) if real_a else _number(record.get("exit_price"))
        if None in (entry, exit_price, quantity, notional, fee_pct, closed):
            return None
        net = (exit_price - entry) * quantity - notional * fee_pct / 100
        events.append((closed, net))

    balance = peak = capital
    drawdown = 0.0
    for _, net in sorted(events):
        balance += net
        peak = max(peak, balance)
        drawdown = max(drawdown, peak - balance)
    return {"net": balance - capital, "drawdown": drawdown}


def _load_state(path: Path) -> Any:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, (dict, list)) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _open_positions(state: Any, *, real_a: bool) -> list[dict[str, Any]]:
    positions = state if isinstance(state, list) else state.get("positions", [])
    return [
        item for item in positions
        if item.get("status") == "OPEN"
        and (not real_a or not item.get("phantom", False))
        and (not real_a or item.get("label") == "B")
    ]


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


if __name__ == "__main__":
    main()
