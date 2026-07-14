from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.state_manager import StateManager
from src.trade_ledger import TradeLedger, latest_trade


def main() -> None:
    args = _parse_args()
    state = StateManager(PROJECT_ROOT).load_open_positions()
    config = _load_config()
    price = args.price if args.price is not None else _fetch_current_price(config)
    if not state:
        if _bot_exit_only(config) and not args.detail:
            if args.json:
                print(json.dumps(_normalize_state([], price, config), ensure_ascii=False, indent=2))
                return
            _print_no_open_bot_b(config)
            return
        if args.json:
            print(json.dumps(_normalize_state([], price, config), ensure_ascii=False, indent=2))
            return
        print(_line_now("No local open positions in data/state/open_positions.json"))
        return
    normalized = _normalize_state(state, price, config)

    if args.json:
        print(json.dumps(normalized, ensure_ascii=False, indent=2))
        return
    if args.detail:
        _print_detail(normalized)
        return
    _print_human(normalized, config)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Lista posicoes locais do trend-sol.")
    parser.add_argument("price", nargs="?", type=float, help="Preco atual manual para calcular PnL.")
    parser.add_argument("--detail", action="store_true", help="Mostra a tabela tecnica completa.")
    parser.add_argument("--json", action="store_true", help="Mostra estado normalizado em JSON.")
    return parser.parse_args()


def _load_config() -> Dict[str, Any]:
    try:
        from tool_common import load_config

        return load_config()
    except Exception:
        return {}


def _normalize_state(state: List[Dict[str, Any]], current_price: Optional[float], config: Dict[str, Any]) -> Dict[str, Any]:
    pairs: Dict[str, Dict[str, Any]] = {}
    for item in sorted(state, key=lambda row: (row.get("pair_id", ""), row.get("label", ""))):
        pair_id = str(item.get("pair_id", ""))
        pair = pairs.setdefault(pair_id, {"pair_id": pair_id, "positions": {}})
        position = _normalize_position(item, current_price, config)
        pair["positions"][str(item.get("label", ""))] = position

    for pair in pairs.values():
        opens = [
            _parse_dt(pos.get("open_ts_raw"))
            for pos in pair["positions"].values()
            if pos.get("open_ts_raw")
        ]
        opened = min((value for value in opens if value is not None), default=None)
        pair["opened"] = _short_datetime(opened)
        pair["opened_at"] = _format_brasilia(opened) if opened else None
        pair["age"] = _age_from_dt(opened)
    return {
        "symbol": str(config.get("symbol", "SOLUSDT")),
        "current_price": current_price,
        "pairs": list(pairs.values()),
    }


def _normalize_position(item: Dict[str, Any], current_price: Optional[float], config: Dict[str, Any]) -> Dict[str, Any]:
    label = str(item.get("label", ""))
    entry = _optional_float(item.get("entry_price"))
    exit_price = _optional_float(item.get("exit_price"))
    entry_atr = _optional_float(item.get("entry_atr"))
    peak = None if label == "A" else _optional_float(item.get("highest_price"))
    trough = None if label == "A" else _optional_float(item.get("trough_price"))
    effective_stop = _optional_float(item.get("effective_stop", item.get("stop_price")))
    open_dt = _parse_dt(item.get("open_ts"))
    close_dt = _parse_dt(item.get("close_ts"))
    status = str(item.get("status", ""))
    return {
        "pair_id": str(item.get("pair_id", "")),
        "label": label,
        "type": _friendly_engine(item),
        "status": status,
        "entry": entry,
        "current": current_price,
        "qty": _optional_float(item.get("quantity")),
        "entry_atr": entry_atr,
        "pnl_pct": _pnl_pct(entry, current_price) if status == "OPEN" else None,
        "pnl_atr": _pnl_atr(entry, current_price, entry_atr) if status == "OPEN" else None,
        "peak": peak,
        "peak_pct": _pnl_pct(entry, peak),
        "peak_atr": _pnl_atr(entry, peak, entry_atr),
        "trough": trough,
        "trough_pct": _pnl_pct(entry, trough),
        "trough_atr": _pnl_atr(entry, trough, entry_atr),
        "trough_tracking_complete": item.get("trough_tracking_complete"),
        "effective_stop": effective_stop,
        "stop_type": item.get("stop_type"),
        "hard_stop_pct": _optional_float(item.get("hard_stop_pct")),
        "trail_status": _trail_status(item),
        "exit": exit_price,
        "realized_pct": _pnl_pct(entry, exit_price) if status == "CLOSED" else None,
        "exit_reason": item.get("exit_reason"),
        "closed": _short_time(close_dt),
        "closed_at": _format_brasilia(close_dt) if close_dt else None,
        "open_ts_raw": item.get("open_ts"),
        "closed_ts_raw": item.get("close_ts"),
        "server_order_id": _order_id(item),
        "client_order_id": _client_order_id(item),
        "raw": item,
        "ladder": _ladder_state(item, current_price, config),
        "binance_trail_pct": _binance_trail_pct(item, config),
    }


def _print_human(normalized: Dict[str, Any], config: Dict[str, Any]) -> None:
    symbol = _symbol_text(str(normalized.get("symbol", "SOLUSDT")))
    pairs = normalized.get("pairs", [])
    if _bot_exit_only(config):
        bot_pairs = [pair for pair in pairs if pair.get("positions", {}).get("B")]
        if not bot_pairs:
            _print_no_open_bot_b(config)
            return
        for index, pair in enumerate(bot_pairs):
            if index:
                print()
            _print_position_b(
                pair.get("positions", {}).get("B"),
                config,
                title="Bot Exit",
                opened=pair.get("opened"),
                age=pair.get("age"),
                leading_blank=False,
            )
        return
    for index, pair in enumerate(pairs):
        if index:
            print()
        print(f"{symbol} | pair {str(pair['pair_id'])[:6]} | opened {pair.get('opened') or 'n/a'} | age {pair.get('age') or 'n/a'}")
        positions = pair.get("positions", {})
        _print_position_a(positions.get("A"))
        _print_position_b(positions.get("B"), config)


def _print_position_a(position: Optional[Dict[str, Any]]) -> None:
    if not position:
        return
    print()
    print("A Binance Trail")
    print(f"  {position['status']}")
    print(f"  entry: {_fmt_price(position.get('entry'))}")
    if position.get("status") == "OPEN":
        print(f"  now:   {_fmt_price(position.get('current'))}  ({_fmt_signed_pct(position.get('pnl_pct'))})")
    elif position.get("status") == "CLOSED":
        print(f"  exit:  {_fmt_price(position.get('exit'))}  ({_fmt_signed_pct(position.get('realized_pct'))})")
        if position.get("closed"):
            print(f"  closed: {position['closed']}")
    print(f"  trail: Binance {_fmt_plain_pct(position.get('binance_trail_pct'))}")


def _print_position_b(
    position: Optional[Dict[str, Any]],
    config: Dict[str, Any],
    title: str = "B Bot Exit",
    opened: Optional[str] = None,
    age: Optional[str] = None,
    leading_blank: bool = True,
) -> None:
    if not position:
        return
    ladder = position.get("ladder") or {}
    if leading_blank:
        print()
    print(title)
    print(f"  {position['status']}")
    if position.get("status") == "OPEN":
        entry_context = f"{opened or 'n/a'} (age {age or 'n/a'}) | " if opened or age else ""
        print(
            "  entry: "
            f"{entry_context}{_fmt_price(position.get('entry'))} -> now {_fmt_price(position.get('current'))}  "
            f"({_fmt_signed_pct(position.get('pnl_pct'))} / {_fmt_signed_atr(position.get('pnl_atr'))})"
        )
        print(f"  peak:  {_fmt_price(position.get('peak'))}  {_fmt_pct_atr(position.get('peak_pct'), position.get('peak_atr'))}")
        print(
            f"  trough: {_fmt_price(position.get('trough'))}  "
            f"{_fmt_pct_atr(position.get('trough_pct'), position.get('trough_atr'))}"
            f"{_trough_tracking_note(position)}"
        )
        print(
            f"  step:  {ladder.get('current_step', 'NONE')} | "
            f"{_effective_stop_text(position)}{_lock_text(ladder)}"
        )
        next_event = ladder.get("next_event")
        if next_event:
            print(
                f"  next:  {next_event['name']} at {_fmt_price(next_event['price'])} "
                f"{_fmt_pct_atr(next_event.get('trigger_pct'), next_event.get('trigger_atr'))}"
            )
        trail = ladder.get("trail")
        if trail:
            if trail.get("active"):
                print(
                    "  trail: active, gap "
                    f"{_fmt_plain_pct(trail.get('gap_pct'))} / {_fmt_atr_value(trail.get('gap_atr'))} ATR from peak"
                )
            else:
                print(
                    "  trail: inactive, activates at "
                    f"{_fmt_price(trail.get('activation_price'))} "
                    f"{_fmt_pct_atr(trail.get('activation_pct'), trail.get('activation_atr'))}"
                )
        return

    print(f"  entry:    {_fmt_price(position.get('entry'))}")
    print(f"  peak:     {_fmt_price(position.get('peak'))}  {_fmt_pct_atr(position.get('peak_pct'), position.get('peak_atr'))}")
    print(
        f"  trough:   {_fmt_price(position.get('trough'))}  "
        f"{_fmt_pct_atr(position.get('trough_pct'), position.get('trough_atr'))}"
        f"{_trough_tracking_note(position)}"
    )
    print(f"  step:     {ladder.get('current_step', 'NONE')}{_lock_text(ladder)}")
    print(f"  stop hit: {_fmt_price(position.get('effective_stop'))}")
    print(f"  exit:     {_fmt_price(position.get('exit'))}  ({_fmt_signed_pct(position.get('realized_pct'))})")
    if position.get("exit_reason"):
        print(f"  reason:   {position['exit_reason']}")
    if position.get("closed"):
        print(f"  closed:   {position['closed']}")


def _print_no_open_bot_b(config: Dict[str, Any]) -> None:
    print("No open Bot B position.")
    latest = latest_trade(TradeLedger(PROJECT_ROOT).load())
    if not latest:
        return
    print()
    print("Last closed:")
    print(f"  opened: {_short_time(_parse_dt(latest.get('opened_at'))) or 'n/a'}")
    print(f"  closed: {_short_time(_parse_dt(latest.get('closed_at'))) or 'n/a'}")
    print(
        "  entry: "
        f"{_fmt_price(latest.get('entry_price'))} -> exit {_fmt_price(latest.get('exit_price'))} "
        f"({_fmt_signed_pct(latest.get('realized_pnl_pct'))})"
    )
    print(
        f"  peak: {_fmt_price(latest.get('peak_price'))} "
        f"{_fmt_pct_atr(_pnl_pct(_optional_float(latest.get('entry_price')), _optional_float(latest.get('peak_price'))), latest.get('peak_atr'))}"
    )
    print(
        f"  trough: {_fmt_price(latest.get('trough_price'))} "
        f"{_fmt_pct_atr(latest.get('trough_pct'), latest.get('trough_atr'))}"
        f"{_trough_tracking_note(latest)}"
    )
    print(f"  reason: {latest.get('exit_reason') or 'n/a'} | step {latest.get('final_step') or 'n/a'}")


def _print_detail(normalized: Dict[str, Any]) -> None:
    print(
        "opened_at           pair_id      pos type           status        entry     current   qty      "
        "entry_atr pnl_pct   pnl_atr  peak     peak_atr trough   trough_atr effective_stop stop_type   trail    age    "
        "exit     realized exit_reason closed_at           server_order_id client_order_id"
    )
    for pair in normalized.get("pairs", []):
        for label in ("A", "B"):
            item = pair.get("positions", {}).get(label)
            if not item:
                continue
            print(
                f"{pair.get('opened_at') or 'n/a':19} "
                f"{item.get('pair_id', ''):12} "
                f"{item.get('label', ''):3} "
                f"{item.get('type', ''):14} "
                f"{item.get('status', ''):12} "
                f"{_fmt_price(item.get('entry')):8} "
                f"{_fmt_price(item.get('current')):8} "
                f"{_fmt_qty(item.get('qty')):8} "
                f"{_fmt_price(item.get('entry_atr')):9} "
                f"{_fmt_plain_pct(item.get('pnl_pct')):9} "
                f"{_fmt_atr_value(item.get('pnl_atr')):8} "
                f"{_fmt_price(item.get('peak')):8} "
                f"{_fmt_atr_value(item.get('peak_atr')):8} "
                f"{_fmt_price(item.get('trough')):8} "
                f"{_fmt_atr_value(item.get('trough_atr')):10} "
                f"{_fmt_price(item.get('effective_stop')):14} "
                f"{str(item.get('stop_type') or 'n/a'):11} "
                f"{str(item.get('trail_status') or 'n/a'):8} "
                f"{pair.get('age') or 'n/a':6} "
                f"{_fmt_price(item.get('exit')):8} "
                f"{_fmt_plain_pct(item.get('realized_pct')):8} "
                f"{str(item.get('exit_reason') or 'n/a'):11} "
                f"{item.get('closed_at') or 'n/a':19} "
                f"{str(item.get('server_order_id') or 'n/a'):15} "
                f"{str(item.get('client_order_id') or 'n/a')}"
            )


def _ladder_state(item: Dict[str, Any], current_price: Optional[float], config: Dict[str, Any]) -> Dict[str, Any]:
    if item.get("label") != "B":
        return {}
    entry = _optional_float(item.get("entry_price"))
    entry_atr = _optional_float(item.get("entry_atr"))
    if entry is None or entry_atr in (None, 0.0):
        return {"current_step": "NONE"}

    risk = config.get("risk", {})
    events = _ladder_events(entry, entry_atr, risk)
    achieved_atr = _best_achieved_atr(item, current_price)
    trailing_active = bool(item.get("trailing_active"))
    stop_type = str(item.get("stop_type") or "")
    current_step = _current_step(events, achieved_atr, trailing_active, stop_type)
    next_event = next((event for event in events if event["trigger_atr"] > achieved_atr), None)
    trail_cfg = (risk.get("trailing") or {}) if isinstance(risk.get("trailing"), dict) else {}
    activation_atr = _optional_float(trail_cfg.get("activation_atr")) or 10.0
    gap_atr = _optional_float(trail_cfg.get("gap_atr")) or 5.0
    lock_atr = _lock_for_step(current_step, events, gap_atr)
    return {
        "current_step": current_step,
        "lock_atr": lock_atr,
        "lock_pct": _atr_pct(entry, entry_atr, lock_atr),
        "next_event": next_event,
        "trail": {
            "active": trailing_active,
            "activation_atr": activation_atr,
            "activation_pct": _atr_pct(entry, entry_atr, activation_atr),
            "activation_price": entry + activation_atr * entry_atr,
            "gap_atr": gap_atr,
            "gap_pct": _atr_pct(entry, entry_atr, gap_atr),
        },
    }


def _ladder_events(entry: float, entry_atr: float, risk: Dict[str, Any]) -> List[Dict[str, Any]]:
    breakeven = risk.get("breakeven") if isinstance(risk.get("breakeven"), dict) else {}
    profit_lock = risk.get("profit_lock") if isinstance(risk.get("profit_lock"), dict) else {}
    trailing = risk.get("trailing") if isinstance(risk.get("trailing"), dict) else {}
    events = [
        {
            "name": "BE",
            "trigger_atr": _optional_float(breakeven.get("trigger_atr")) or 3.0,
            "lock_atr": _optional_float(breakeven.get("offset_atr")) or 0.1,
        }
    ]
    for index, step in enumerate(profit_lock.get("steps") or [], start=1):
        trigger = _optional_float(step.get("trigger_atr"))
        lock = _optional_float(step.get("lock_atr"))
        if trigger is not None and lock is not None:
            events.append({"name": f"PL{index}", "trigger_atr": trigger, "lock_atr": lock})
    activation = _optional_float(trailing.get("activation_atr"))
    if activation is not None:
        events.append({"name": "TRAIL", "trigger_atr": activation, "lock_atr": None})
    for event in events:
        event["price"] = entry + float(event["trigger_atr"]) * entry_atr
        event["trigger_pct"] = _atr_pct(entry, entry_atr, event["trigger_atr"])
    return sorted(events, key=lambda event: (float(event["trigger_atr"]), event["name"] == "TRAIL"))


def _best_achieved_atr(item: Dict[str, Any], current_price: Optional[float]) -> float:
    entry = _optional_float(item.get("entry_price"))
    entry_atr = _optional_float(item.get("entry_atr"))
    values = [
        _peak_atr(item),
        _pnl_atr(entry, current_price, entry_atr),
        _pnl_atr(entry, _optional_float(item.get("exit_price")), entry_atr),
    ]
    return max((value for value in values if value is not None), default=0.0)


def _current_step(events: Iterable[Dict[str, Any]], achieved_atr: float, trailing_active: bool, stop_type: str) -> str:
    if trailing_active and stop_type == "trailing":
        return "TRAIL"
    current = "NONE"
    for event in events:
        if event["name"] == "TRAIL":
            continue
        if achieved_atr >= float(event["trigger_atr"]):
            current = str(event["name"])
    return current


def _lock_for_step(step_name: str, events: Iterable[Dict[str, Any]], gap_atr: float) -> Optional[float]:
    if step_name == "TRAIL":
        return gap_atr
    for event in events:
        if event["name"] == step_name:
            return _optional_float(event.get("lock_atr"))
    return None


def _lock_text(ladder: Dict[str, Any]) -> str:
    step = ladder.get("current_step")
    lock = ladder.get("lock_atr")
    lock_pct = ladder.get("lock_pct")
    if step == "TRAIL" and lock is not None:
        return f" | gap {_fmt_plain_pct(lock_pct)} / {_fmt_atr_value(lock)} ATR from peak"
    if lock is not None:
        return f" {_fmt_pct_atr(lock_pct, lock, suffix=' locked')}"
    return ""


def _effective_stop_text(position: Dict[str, Any]) -> str:
    price = _fmt_price(position.get("effective_stop"))
    if str(position.get("stop_type") or "") == "hard_stop":
        pct = _optional_float(position.get("hard_stop_pct"))
        return f"hard stop {price} (-{_fmt_plain_pct(pct)})"
    return f"stop {price}"


def _trough_tracking_note(position: Dict[str, Any]) -> str:
    if position.get("trough") is None and position.get("trough_price") is None:
        return ""
    return " (partial tracking)" if position.get("trough_tracking_complete") is False else ""


def _friendly_engine(item: Dict[str, Any]) -> str:
    if item.get("label") == "A":
        return "BINANCE_TRAIL"
    if item.get("label") == "B":
        return "BOT_EXIT"
    return str(item.get("engine", ""))


def _pnl_pct(entry: Optional[float], price: Optional[float]) -> Optional[float]:
    if price is None or entry in (None, 0.0):
        return None
    return ((price / entry) - 1) * 100


def _pnl_atr(entry: Optional[float], price: Optional[float], entry_atr: Optional[float]) -> Optional[float]:
    if price is None or entry is None or entry_atr in (None, 0.0):
        return None
    return (price - entry) / entry_atr


def _atr_pct(entry: Optional[float], entry_atr: Optional[float], atr_value: Any) -> Optional[float]:
    atr = _optional_float(atr_value)
    if entry in (None, 0.0) or entry_atr is None or atr is None:
        return None
    return atr * entry_atr / entry * 100


def _peak_atr(item: Dict[str, Any]) -> Optional[float]:
    if item.get("label") == "A":
        return None
    return _pnl_atr(
        _optional_float(item.get("entry_price")),
        _optional_float(item.get("highest_price")),
        _optional_float(item.get("entry_atr")),
    )


def _trail_status(item: Dict[str, Any]) -> str:
    if item.get("label") != "B":
        return "n/a"
    return "active" if bool(item.get("trailing_active")) else "inactive"


def _binance_trail_pct(item: Dict[str, Any], config: Dict[str, Any]) -> Optional[float]:
    order = item.get("trailing_order") or {}
    bips = _optional_float(order.get("trailingDelta"))
    if bips is None:
        bips = _optional_float((config.get("exit_server_simple_trail") or {}).get("trailing_delta_bips"))
    return bips / 100 if bips is not None else None


def _order_id(item: Dict[str, Any]) -> Any:
    order = item.get("exit_order") or item.get("trailing_order") or item.get("entry_order") or {}
    return order.get("orderId")


def _client_order_id(item: Dict[str, Any]) -> Any:
    order = item.get("exit_order") or item.get("trailing_order") or item.get("entry_order") or {}
    return order.get("clientOrderId")


def _symbol_text(symbol: str) -> str:
    return symbol.replace("USDT", "/USDT") if symbol.endswith("USDT") else symbol


def _fmt_price(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:.4f}"


def _fmt_qty(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:.4f}"


def _fmt_plain_pct(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:.2f}%"


def _fmt_signed_pct(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:+.2f}%"


def _fmt_signed_atr(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:+.2f} ATR"


def _fmt_pct_atr(pct: Any, atr: Any, suffix: str = "") -> str:
    return f"({_fmt_signed_pct(pct)} / {_fmt_signed_atr(atr)}{suffix})"


def _fmt_atr_value(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    if abs(number - int(number)) < 1e-9:
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_current_price(config: Dict[str, Any]) -> Optional[float]:
    try:
        import requests

        market_cfg = config.get("market_data", {})
        execution_cfg = config.get("execution", {})
        base_url = str(market_cfg.get("rest_url", "https://api.binance.com")).rstrip("/")
        timeout_seconds = int(execution_cfg.get("http_timeout_seconds", 8))
        response = requests.get(
            f"{base_url}/api/v3/ticker/price",
            params={"symbol": str(config.get("symbol", "SOLUSDT"))},
            timeout=timeout_seconds,
        )
        if response.status_code >= 400:
            return None
        return _optional_float((response.json() or {}).get("price"))
    except Exception:
        return None


def _line_now(message: str) -> str:
    return f"{_format_brasilia(datetime.now(timezone.utc))} {message}"


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_brasilia(value: Optional[datetime]) -> str:
    if value is None:
        return "n/a"
    return value.astimezone(BRASILIA_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _short_time(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(BRASILIA_TZ).strftime("%H:%M")


def _short_datetime(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


def _age_from_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "n/a"
    seconds = int((datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds())
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, _seconds = divmod(remainder, 60)
    return f"{hours}h{minutes:02d}m"


def _bot_exit_only(config: Dict[str, Any]) -> bool:
    return str(config.get("position_mode", "paired_ab")) == "bot_exit_only"


if __name__ == "__main__":
    main()
