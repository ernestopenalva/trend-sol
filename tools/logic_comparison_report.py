from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.market_context_report import _filter, _parse_ts, _parse_user_dt
from src.trade_ledger import TradeLedger


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare REAL_A and DMI15 shadows B through G.")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--since-field", choices=["opened_at", "closed_at"], default="opened_at")
    parser.add_argument("--profile")
    parser.add_argument("--strategy", choices=["A", "B", "C", "D", "E", "F", "G", "both", "all"], default="all")
    args = parser.parse_args()
    requested_strategy = args.strategy
    setattr(args, "strategy", "A")
    real = _filter(TradeLedger(PROJECT_ROOT).load(), args)
    setattr(args, "strategy", "B")
    shadow = _filter(
        TradeLedger(PROJECT_ROOT, PROJECT_ROOT / "data/trades/trades_gcr_shadow.jsonl").load(), args
    )
    dmi = _filter(
        TradeLedger(PROJECT_ROOT, PROJECT_ROOT / "data/trades/trades_dmi15_shadow.jsonl").load(), args
    )
    setattr(args, "strategy", "D")
    dmi_spread = _filter(
        TradeLedger(PROJECT_ROOT, PROJECT_ROOT / "data/trades/trades_dmi15_spread_shadow.jsonl").load(), args
    )
    setattr(args, "strategy", "E")
    dmi_trajectory = _filter(TradeLedger(PROJECT_ROOT, PROJECT_ROOT / "data/trades/trades_dmi15_trajectory_shadow.jsonl").load(), args)
    setattr(args, "strategy", "F")
    dmi_rsi70 = _filter(TradeLedger(PROJECT_ROOT, PROJECT_ROOT / "data/trades/trades_dmi15_rsi70_shadow.jsonl").load(), args)
    setattr(args, "strategy", "G")
    dmi_combined = _filter(TradeLedger(PROJECT_ROOT, PROJECT_ROOT / "data/trades/trades_dmi15_combined_shadow.jsonl").load(), args)
    gcr_state = _json(PROJECT_ROOT / "data/state/gcr_shadow.json")
    dmi_state = _json(PROJECT_ROOT / "data/state/dmi15_shadow.json")
    dmi_spread_state = _json(PROJECT_ROOT / "data/state/dmi15_spread_shadow.json")
    trajectory_state = _json(PROJECT_ROOT / "data/state/dmi15_trajectory_shadow.json")
    rsi70_state = _json(PROJECT_ROOT / "data/state/dmi15_rsi70_shadow.json")
    combined_state = _json(PROJECT_ROOT / "data/state/dmi15_combined_shadow.json")
    print("TREND-SOL | REAL_A vs shadows B/C/D/E/F/G")
    print("strategy | trades | gross | gross/trade | net | fees | avg net | winrate | profit factor | avg age | median age | slots full | max simultaneous | HS | NPE | BE | PL | TRAIL")
    if requested_strategy in ("A", "both", "all"):
        _line("REAL_A", real, 5)
    if requested_strategy in ("B", "both", "all"):
        _line("GCR_SHADOW_B", shadow, 5)
    if requested_strategy in ("C", "all"):
        _line("DMI15_SHADOW_C", dmi, 5)
    if requested_strategy in ("D", "all"):
        _line("DMI15_SPREAD6_SHADOW_D", dmi_spread, 5)
    if requested_strategy in ("E", "all"):
        _line("DMI15_TRAJECTORY_SHADOW_E", dmi_trajectory, 5)
    if requested_strategy in ("F", "all"):
        _line("DMI15_RSI70_SHADOW_F", dmi_rsi70, 5)
    if requested_strategy in ("G", "all"):
        _line("DMI15_COMBINED_SHADOW_G", dmi_combined, 5)
    print()
    print(f"REAL_A blocked same 5m | {_count_events(PROJECT_ROOT / 'logs/decisions.jsonl', 'ENTRY_BLOCKED_SAME_5M_CANDLE', args)}")
    print(f"GCR_SHADOW_B blocked same 5m | {_count_events(PROJECT_ROOT / 'data/telemetry/gcr_shadow_events.jsonl', 'ENTRY_BLOCKED_SAME_5M_CANDLE', args)}")
    print(f"GCR_SHADOW_B blocked GCR | {_count_events(PROJECT_ROOT / 'data/telemetry/gcr_shadow_events.jsonl', 'ENTRY_BLOCKED_GCR', args)}")
    print(f"GCR_SHADOW_B max simultaneous positions | {gcr_state.get('max_simultaneous_positions', 0)} (lifetime state)")
    print(f"DMI15_SHADOW_C blocked same 5m | {_count_events(PROJECT_ROOT / 'data/telemetry/dmi15_shadow_events.jsonl', 'ENTRY_BLOCKED_SAME_5M_CANDLE', args)}")
    print(f"DMI15_SHADOW_C blocked capacity | {_count_events(PROJECT_ROOT / 'data/telemetry/dmi15_shadow_events.jsonl', 'ENTRY_BLOCKED_SHADOW_CAPACITY', args)}")
    print(f"DMI15_SHADOW_C max simultaneous positions | {dmi_state.get('max_simultaneous_positions', 0)} (lifetime state)")
    print(f"DMI15_SPREAD6_SHADOW_D blocked same 5m | {_count_events(PROJECT_ROOT / 'data/telemetry/dmi15_spread_shadow_events.jsonl', 'ENTRY_BLOCKED_SAME_5M_CANDLE', args)}")
    print(f"DMI15_SPREAD6_SHADOW_D blocked capacity | {_count_events(PROJECT_ROOT / 'data/telemetry/dmi15_spread_shadow_events.jsonl', 'ENTRY_BLOCKED_SHADOW_CAPACITY', args)}")
    print(f"DMI15_SPREAD6_SHADOW_D blocked spread<6 | {_count_events(PROJECT_ROOT / 'data/telemetry/dmi15_spread_shadow_events.jsonl', 'ENTRY_BLOCKED_DMI_SPREAD', args)}")
    print(f"DMI15_SPREAD6_SHADOW_D max simultaneous positions | {dmi_spread_state.get('max_simultaneous_positions', 0)} (lifetime state)")
    _shadow_blockers("DMI15_TRAJECTORY_SHADOW_E", "dmi15_trajectory_shadow", trajectory_state, args, trajectory=True)
    _shadow_blockers("DMI15_RSI70_SHADOW_F", "dmi15_rsi70_shadow", rsi70_state, args, rsi=True)
    _shadow_blockers("DMI15_COMBINED_SHADOW_G", "dmi15_combined_shadow", combined_state, args, spread=True, trajectory=True, rsi=True)


def _line(name: str, records: list[Dict[str, Any]], slots: int) -> None:
    gross = [_float(item.get("gross_pnl_pct")) for item in records]
    net = [_float(item.get("net_pnl_pct")) for item in records]
    ages = [_float(item.get("age_seconds")) for item in records]
    reasons = Counter(str(item.get("exit_reason") or "UNKNOWN") for item in records)
    gross = [x for x in gross if x is not None]
    net = [x for x in net if x is not None]
    ages = [x for x in ages if x is not None]
    fees = sum((_float(item.get("estimated_fees_pct")) or 0) for item in records)
    wins = len([x for x in net if x > 0])
    gains = sum(x for x in net if x > 0)
    losses = abs(sum(x for x in net if x < 0))
    pf = gains / losses if losses else float("inf") if gains else 0
    print(
        f"{name} | {len(records)} | {sum(gross):+.2f}% | {(sum(gross)/len(gross) if gross else 0):+.2f}% | {sum(net):+.2f}% | {-fees:+.2f}% | "
        f"{(sum(net)/len(net) if net else 0):+.2f}% | {(wins/len(net)*100 if net else 0):.1f}% | "
        f"{pf:.2f} | {(sum(ages)/len(ages)/3600 if ages else 0):.2f}h | "
        f"{(median(ages)/3600 if ages else 0):.2f}h | {_slots_full(records, slots):.1f}% | "
        f"{_max_simultaneous(records)} | {reasons['HARD_STOP']} | "
        f"{reasons['NO_PROGRESS_EXIT']} | {reasons['BREAKEVEN']} | {reasons['PROFIT_LOCK']} | {reasons['TRAILING']}"
    )


def _shadow_blockers(
    label: str, stem: str, state: Dict[str, Any], args: argparse.Namespace,
    *, spread: bool = False, trajectory: bool = False, rsi: bool = False,
) -> None:
    path = PROJECT_ROOT / f"data/telemetry/{stem}_events.jsonl"
    print(f"{label} blocked same 5m | {_count_events(path, 'ENTRY_BLOCKED_SAME_5M_CANDLE', args)}")
    print(f"{label} blocked capacity | {_count_events(path, 'ENTRY_BLOCKED_SHADOW_CAPACITY', args)}")
    if spread:
        print(f"{label} blocked spread<6 | {_count_events(path, 'ENTRY_BLOCKED_DMI_SPREAD', args)}")
    if trajectory:
        print(f"{label} blocked trajectory | {_count_events(path, 'ENTRY_BLOCKED_DMI_TRAJECTORY', args)}")
    if rsi:
        print(f"{label} blocked RSI-MA>70 | {_count_events(path, 'ENTRY_BLOCKED_RSI_MA', args)}")
    print(f"{label} blocked required indicator unavailable | {_count_events(path, 'ENTRY_SKIPPED_REQUIRED_INDICATOR_UNAVAILABLE', args)}")
    print(f"{label} max simultaneous positions | {state.get('max_simultaneous_positions', 0)} (lifetime state)")


def _max_simultaneous(records: list[Dict[str, Any]]) -> int:
    events = []
    for item in records:
        opened = str(item.get("opened_at") or "")
        closed = str(item.get("closed_at") or "")
        if opened and closed:
            events.extend(((opened, 1), (closed, -1)))
    active = maximum = 0
    for _, change in sorted(events, key=lambda value: (value[0], value[1])):
        active += change
        maximum = max(maximum, active)
    return maximum


def _slots_full(records: list[Dict[str, Any]], slots: int) -> float:
    if not records or slots <= 0:
        return 0.0
    events = []
    for item in records:
        opened = str(item.get("opened_at") or "")
        closed = str(item.get("closed_at") or "")
        if opened and closed:
            events.extend(((opened, 1), (closed, -1)))
    if len(events) < 2:
        return 0.0
    from datetime import datetime
    parsed = sorted((datetime.fromisoformat(ts.replace("Z", "+00:00")), change) for ts, change in events)
    active = 0
    full_seconds = 0.0
    previous = parsed[0][0]
    for timestamp, change in parsed:
        if active >= slots:
            full_seconds += max(0.0, (timestamp - previous).total_seconds())
        active += change
        previous = timestamp
    total = (parsed[-1][0] - parsed[0][0]).total_seconds()
    return full_seconds / total * 100 if total > 0 else 0.0


def _count_events(path: Path, reason: str, args: argparse.Namespace) -> int:
    if not path.exists():
        return 0
    since = _parse_user_dt(args.since)
    until = _parse_user_dt(args.until)
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if reason not in (event.get("reason"), event.get("event")):
            continue
        timestamp = _parse_ts(event.get("ts"))
        if since and (timestamp is None or timestamp < since):
            continue
        if until and (timestamp is None or timestamp > until):
            continue
        count += 1
    return count


def _json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _float(value: Any):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
