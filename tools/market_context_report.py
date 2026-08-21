from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger


def main() -> None:
    args = _args()
    paths = {
        "A": PROJECT_ROOT / "data/trades/trades_B.jsonl",
        "B": PROJECT_ROOT / "data/trades/trades_gcr_shadow.jsonl",
        "C": PROJECT_ROOT / "data/trades/trades_dmi15_shadow.jsonl",
        "D": PROJECT_ROOT / "data/trades/trades_dmi15_spread_shadow.jsonl",
    }
    labels = {"A": "REAL_A", "B": "GCR_SHADOW_B", "C": "DMI15_SHADOW_C", "D": "DMI15_SPREAD6_SHADOW_D"}
    path = paths[args.strategy]
    records = _filter(TradeLedger(PROJECT_ROOT, path).load(), args)
    print("TREND-SOL | market context report")
    print(f"strategy | {labels[args.strategy]} | trades | {len(records)}")
    print("opened | closed | age | peak | trough | net | reason | phase | 5m indicators | 15m indicators | GE15")
    for record in records:
        phases = (("entry", record.get("market_context_entry")), ("exit", record.get("market_context_exit")))
        for phase, context in phases:
            if not isinstance(context, dict):
                if args.detail:
                    print(
                        f"{_dt(record.get('opened_at'))} | {_dt(record.get('closed_at'))} | {_age(record)} | "
                        f"{_extreme(record, 'peak_price', 'peak_pct', 'peak_atr')} | "
                        f"{_extreme(record, 'trough_price', 'trough_pct', 'trough_atr')} | "
                        f"{_pct(record.get('net_pnl_pct'))} | {record.get('exit_reason')} | {phase} | "
                        "unavailable | unavailable | unavailable"
                    )
                continue
            print(
                f"{_dt(record.get('opened_at'))} | {_dt(record.get('closed_at'))} | {_age(record)} | "
                f"{_extreme(record, 'peak_price', 'peak_pct', 'peak_atr')} | "
                f"{_extreme(record, 'trough_price', 'trough_pct', 'trough_atr')} | "
                f"{_pct(record.get('net_pnl_pct'))} | {record.get('exit_reason')} | {phase} | "
                f"{_tf(context.get('tf_5m'), show_rsi_ma=True)} | {_tf(context.get('tf_15m'))} | "
                f"{(context.get('ge15') or {}).get('status', 'n/a')}"
            )


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only market-context report.")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--since-field", choices=["opened_at", "closed_at"], default="opened_at")
    parser.add_argument("--profile")
    parser.add_argument("--strategy", choices=["A", "B", "C", "D"], default="A")
    parser.add_argument("--detail", action="store_true")
    return parser.parse_args()


def _filter(records: list[Dict[str, Any]], args: argparse.Namespace) -> list[Dict[str, Any]]:
    since = _parse_user_dt(args.since)
    until = _parse_user_dt(args.until)
    output = []
    for item in records:
        if args.profile and item.get("profile") != args.profile:
            continue
        value = _parse_ts(item.get(args.since_field))
        if since and (value is None or value < since):
            continue
        if until and (value is None or value > until):
            continue
        if args.strategy == "A" and (item.get("phantom") or item.get("position_type") != "BOT_EXIT"):
            continue
        output.append(item)
    return sorted(output, key=lambda item: str(item.get("opened_at") or ""))


def _tf(value: Any, show_rsi_ma: bool = False) -> str:
    item = value if isinstance(value, dict) else {}
    return (
        f"EMA20={_num(item.get('ema20'))} EMA50={_num(item.get('ema50'))} "
        f"S20={_pct(item.get('ema20_slope_pct'))} S50={_pct(item.get('ema50_slope_pct'))} "
        f"ADX={_num(item.get('adx14'))} +DI={_num(item.get('plus_di14'))} "
        f"-DI={_num(item.get('minus_di14'))} {_rsi_move(item)}"
        f"{' RSI-MA14=' + _num(item.get('rsi14_sma14')) if show_rsi_ma else ''} "
        f"RVOL={_num(item.get('relative_volume'))}x"
    )


def _rsi_move(item: Dict[str, Any]) -> str:
    previous = _float(item.get("rsi14_15m_ago"))
    current = _float(item.get("rsi14"))
    if previous is None or current is None:
        return f"RSI={_num(current)}"
    return f"RSI {previous:.1f}→{current:.1f} ({current - previous:+.1f})"


def _parse_user_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%d/%m %H:%M", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%d/%m %H:%M":
                parsed = parsed.replace(year=datetime.now(BRASILIA_TZ).year)
            return parsed.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        except ValueError:
            pass
    raise SystemExit(f"Invalid date/time: {value}")


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> str:
    parsed = _parse_ts(value)
    return parsed.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M") if parsed else "n/a"


def _age(item: Dict[str, Any]) -> str:
    value = item.get("age_seconds")
    return f"{float(value) / 3600:.1f}h" if value is not None else "n/a"


def _num(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "n/a"


def _pct(value: Any) -> str:
    try:
        return f"{float(value):+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def _extreme(
    item: Dict[str, Any],
    price_key: str,
    pct_key: str,
    atr_key: str,
) -> str:
    price = _float(item.get(price_key))
    pct = _float(item.get(pct_key))
    entry = _float(item.get("entry_price"))
    if pct is None and price is not None and entry not in (None, 0):
        pct = (price / entry - 1) * 100
    atr_value = _float(item.get(atr_key))
    if price is None:
        return "n/a"
    pct_text = f"{pct:+.2f}%" if pct is not None else "n/a"
    atr_text = f"{atr_value:+.2f} ATR" if atr_value is not None else "n/a ATR"
    return f"{price:.4f} ({pct_text} / {atr_text})"


def _float(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
