from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ


def main() -> None:
    args = _parse_args()
    ledger = _read_jsonl(PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    trades = _read_jsonl(PROJECT_ROOT / "logs" / "trades.jsonl")
    system = _read_jsonl(PROJECT_ROOT / "logs" / "system.log")

    record = _find_record(ledger, args)
    if record is None:
        raise SystemExit("Trade nao encontrado no ledger. Use --pair-id ou confira --opened.")

    pair_id = str(record.get("pair_id"))
    opened = _parse_ts(record.get("opened_at"))
    closed = _parse_ts(record.get("closed_at"))
    events = [
        event
        for event in trades
        if str(event.get("pair_id")) == pair_id and str(event.get("position")) == "B"
    ]
    events.sort(key=lambda item: str(item.get("ts") or ""))

    entry_price = _float(record.get("entry_price")) or _event_float(events, "OPEN", "price")
    entry_atr = _float(record.get("entry_atr")) or _event_float(events, "OPEN", "entry_atr")
    trigger_be = entry_price + 3 * entry_atr if entry_price is not None and entry_atr is not None else None
    stop_be = entry_price + 0.1 * entry_atr if entry_price is not None and entry_atr is not None else None

    be_event = _first(events, lambda event: str(event.get("event") or "").startswith("BREAKEVEN"))
    first_trigger_event = _first(
        events,
        lambda event: trigger_be is not None
        and _float(event.get("price")) is not None
        and _float(event.get("price")) >= trigger_be,
    )
    stop_event = _first(
        events,
        lambda event: _float(event.get("effective_stop")) is not None
        and str(event.get("stop_type") or "") != "review",
    )
    close_event = _first(events, lambda event: str(event.get("event") or "") == "CLOSE")

    after_be = [
        event
        for event in events
        if be_event is not None and _parse_ts(event.get("ts")) is not None and _parse_ts(event.get("ts")) >= _parse_ts(be_event.get("ts"))
    ]
    min_after_be = min(
        (_float(event.get("price")) for event in after_be if _float(event.get("price")) is not None),
        default=None,
    )
    earlier_stop_hits = _events_before_close_at_or_below_stop(after_be, close_event)
    system_events = _system_events_between(system, opened, closed)

    print("TREND-SOL | Trade investigation")
    print()
    print(f"pair_id: {pair_id}")
    print(f"opened: {_fmt_dt(opened)}")
    print(f"closed: {_fmt_dt(closed)}")
    print(f"age: {_fmt_age(record.get('age_seconds'))}")
    print()
    print("Entry:")
    print(f"  entry_price: {_fmt_price(entry_price)}")
    print(f"  entry_atr:   {_fmt_price(entry_atr)}")
    print(f"  BE trigger:  {_fmt_price(trigger_be)}  entry + 3*entry_atr")
    print(f"  BE stop:     {_fmt_price(stop_be)}  entry + 0.1*entry_atr")
    print()
    print("Timeline:")
    print(f"  first price >= BE trigger: {_fmt_event(first_trigger_event)}")
    print(f"  BE event:                  {_fmt_event(be_event)}")
    print(f"  effective_stop changed:    {_fmt_event(stop_event)}")
    print(f"  lowest event price after BE: {_fmt_price(min_after_be)}")
    if earlier_stop_hits:
        print("  observed price <= effective_stop before close: YES")
        for event in earlier_stop_hits[:5]:
            print(f"    {_fmt_event(event)}")
    else:
        print("  observed price <= effective_stop before close: no, not in trade-event logs")
    print()
    print("Exit:")
    print(f"  close event:  {_fmt_event(close_event)}")
    print(f"  exit_reason:  {record.get('exit_reason') or (close_event or {}).get('exit_reason') or 'n/a'}")
    print(f"  final_step:   {record.get('final_step') or 'n/a'}")
    print(f"  stop_hit:     {_fmt_price(record.get('stop_hit') or (close_event or {}).get('effective_stop'))}")
    print(f"  exit_price:   {_fmt_price(record.get('exit_price') or (close_event or {}).get('price'))}")
    print(f"  realized:     {_fmt_signed_pct(record.get('realized_pnl_pct'))}")
    print()
    print("System events during trade:")
    if system_events:
        for event in system_events[:20]:
            print(f"  {_fmt_dt(_parse_ts(event.get('ts')))} {event.get('event') or event.get('message')} {_system_detail(event)}")
        if len(system_events) > 20:
            print(f"  ... {len(system_events) - 20} more")
    else:
        print("  none")
    print()
    _print_conclusion(record, be_event, close_event, opened)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Investiga a linha do tempo de um trade Bot B.")
    parser.add_argument("--opened", default="2026-07-09 04:18", help="Horario local aproximado da abertura.")
    parser.add_argument("--pair-id", help="Pair id exato ou prefixo.")
    return parser.parse_args()


def _find_record(records: list[dict[str, Any]], args: argparse.Namespace) -> Optional[dict[str, Any]]:
    if args.pair_id:
        matches = [item for item in records if str(item.get("pair_id", "")).startswith(args.pair_id)]
        return matches[-1] if matches else None
    target = _parse_local_arg(args.opened)
    if target is None:
        return None
    window = timedelta(minutes=2)
    matches = []
    for record in records:
        opened = _parse_ts(record.get("opened_at"))
        if opened is not None and abs(opened - target) <= window:
            matches.append(record)
    return matches[-1] if matches else None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def _events_before_close_at_or_below_stop(events: list[dict[str, Any]], close_event: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    close_ts = _parse_ts((close_event or {}).get("ts"))
    output = []
    for event in events:
        event_ts = _parse_ts(event.get("ts"))
        if close_ts is not None and event_ts is not None and event_ts >= close_ts:
            continue
        price = _float(event.get("price"))
        stop = _float(event.get("effective_stop"))
        if price is not None and stop is not None and price <= stop:
            output.append(event)
    return output


def _system_events_between(events: list[dict[str, Any]], opened: Optional[datetime], closed: Optional[datetime]) -> list[dict[str, Any]]:
    keywords = ("websocket", "stale", "disconnect", "reconnect", "error", "closed")
    output = []
    for event in events:
        ts = _parse_ts(event.get("ts"))
        name = str(event.get("event") or event.get("message") or "").lower()
        if ts is None or opened is None or closed is None:
            continue
        if opened <= ts <= closed and any(keyword in name for keyword in keywords):
            output.append(event)
    return output


def _first(events: list[dict[str, Any]], predicate: Any) -> Optional[dict[str, Any]]:
    for event in events:
        if predicate(event):
            return event
    return None


def _event_float(events: list[dict[str, Any]], event_name: str, key: str) -> Optional[float]:
    event = _first(events, lambda item: str(item.get("event") or "") == event_name)
    return _float((event or {}).get(key))


def _parse_local_arg(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.strip().replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA_TZ)
    return parsed.astimezone(timezone.utc)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_event(event: Optional[dict[str, Any]]) -> str:
    if not event:
        return "n/a"
    parts = [
        _fmt_dt(_parse_ts(event.get("ts"))),
        str(event.get("event") or "event"),
        f"price={_fmt_price(event.get('price'))}",
    ]
    if event.get("pnl_atr") is not None:
        parts.append(f"pnl_atr={_fmt_number(event.get('pnl_atr'))}")
    if event.get("effective_stop") is not None:
        parts.append(f"stop={_fmt_price(event.get('effective_stop'))}")
    if event.get("stop_type") is not None:
        parts.append(f"type={event.get('stop_type')}")
    if event.get("exit_reason") is not None:
        parts.append(f"reason={event.get('exit_reason')}")
    return " ".join(parts)


def _system_detail(event: dict[str, Any]) -> str:
    details = []
    for key in ("error", "status_code", "close_message", "backoff_seconds", "tick_age"):
        if event.get(key) is not None:
            details.append(f"{key}={event.get(key)}")
    return " ".join(details)


def _print_conclusion(record: dict[str, Any], be_event: Optional[dict[str, Any]], close_event: Optional[dict[str, Any]], opened: Optional[datetime]) -> None:
    be_ts = _parse_ts((be_event or {}).get("ts"))
    close_stop_type = str((close_event or {}).get("stop_type") or "").lower()
    reason = str(record.get("exit_reason") or (close_event or {}).get("exit_reason") or "")
    print("Conclusion:")
    if be_ts is None:
        print("  BE activation was not found in logs.")
    elif opened is not None:
        print(f"  BE activated {_fmt_age((be_ts - opened).total_seconds())} after entry.")
    if close_stop_type == "breakeven" and reason == "REVIEW_STOP":
        print("  Classification bug confirmed: stop_type=breakeven was reported as REVIEW_STOP.")
    elif close_stop_type == "breakeven":
        print("  Exit was classified as breakeven.")
    else:
        print(f"  Close stop_type was {close_stop_type or 'n/a'}.")
    print("  Note: this tool uses persisted trade events; raw tick-by-tick lows are not persisted.")


def _fmt_dt(value: Optional[datetime]) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%Y-%m-%d %H:%M:%S") if value else "n/a"


def _fmt_age(value: Any) -> str:
    seconds = int(_float(value) or 0)
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, _seconds = divmod(remainder, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def _fmt_price(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:.4f}"


def _fmt_number(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:.4f}"


def _fmt_signed_pct(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:+.2f}%"


if __name__ == "__main__":
    main()
