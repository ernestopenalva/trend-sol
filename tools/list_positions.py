from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.state_manager import StateManager


def main() -> None:
    state = StateManager(PROJECT_ROOT).load_open_positions()
    if not state:
        print(_line_now("No local open positions in data/state/open_positions.json"))
        return
    price = _optional_float(sys.argv[1]) if len(sys.argv) > 1 else _fetch_current_price()
    print(
        "opened_at           pair_id      pos type           status        entry     current   qty      "
        "entry_atr pnl_pct   pnl_atr  peak     peak_atr effective_stop stop_type   trail    age"
    )
    for item in sorted(state, key=lambda row: (row.get("pair_id", ""), row.get("label", ""))):
        pnl = _pnl(item, price)
        pnl_atr = _pnl_atr(item, price)
        peak_atr = _peak_atr(item)
        print(
            f"{_opened_at(item.get('open_ts')):19} "
            f"{str(item.get('pair_id', ''))[:12]:12} "
            f"{item.get('label', ''):3} "
            f"{_friendly_engine(item):14} "
            f"{item.get('status', ''):12} "
            f"{_fmt(item.get('entry_price')):8} "
            f"{_fmt(price):8} "
            f"{_fmt(item.get('quantity')):8} "
            f"{_fmt(item.get('entry_atr')):9} "
            f"{_fmt_pct(pnl):9} "
            f"{_fmt_atr(pnl_atr):8} "
            f"{_fmt(item.get('highest_price')):8} "
            f"{_fmt_atr(peak_atr):8} "
            f"{_fmt(_effective_stop(item)):14} "
            f"{str(item.get('stop_type') or 'n/a'):11} "
            f"{_trail_status(item):8} "
            f"{_age(item.get('open_ts'))}"
        )


def _friendly_engine(item: Dict[str, Any]) -> str:
    if item.get("label") == "A":
        return "BINANCE_TRAIL"
    if item.get("label") == "B":
        return "BOT_EXIT"
    return str(item.get("engine", ""))


def _pnl(item: Dict[str, Any], price: Optional[float]) -> Optional[float]:
    if item.get("status") != "OPEN":
        return None
    entry = _optional_float(item.get("entry_price"))
    if price is None or entry in (None, 0.0):
        return None
    return ((price / entry) - 1) * 100


def _pnl_atr(item: Dict[str, Any], price: Optional[float]) -> Optional[float]:
    if item.get("status") != "OPEN":
        return None
    entry = _optional_float(item.get("entry_price"))
    entry_atr = _optional_float(item.get("entry_atr"))
    if price is None or entry is None or entry_atr in (None, 0.0):
        return None
    return (price - entry) / entry_atr


def _peak_atr(item: Dict[str, Any]) -> Optional[float]:
    entry = _optional_float(item.get("entry_price"))
    peak = _optional_float(item.get("highest_price"))
    entry_atr = _optional_float(item.get("entry_atr"))
    if entry is None or peak is None or entry_atr in (None, 0.0):
        return None
    return (peak - entry) / entry_atr


def _effective_stop(item: Dict[str, Any]) -> Any:
    return item.get("effective_stop", item.get("stop_price"))


def _trail_status(item: Dict[str, Any]) -> str:
    if item.get("label") != "B":
        return "n/a"
    return "active" if bool(item.get("trailing_active")) else "inactive"


def _fmt(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:.4f}"


def _fmt_pct(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:.2f}%"


def _fmt_atr(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:.2f}"


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_current_price() -> Optional[float]:
    try:
        from tool_common import load_config
        import requests

        config = load_config()
        market_cfg = config.get("market_data", {})
        execution_cfg = config.get("execution", {})
        base_url = str(market_cfg.get("rest_url", "https://api.binance.com")).rstrip("/")
        timeout_seconds = int(execution_cfg.get("http_timeout_seconds", 8))
        response = requests.get(
            f"{base_url}/api/v3/ticker/price",
            params={"symbol": str(config["symbol"])},
            timeout=timeout_seconds,
        )
        if response.status_code >= 400:
            return None
        return _optional_float((response.json() or {}).get("price"))
    except Exception:
        return None


def _line_now(message: str) -> str:
    return f"{_format_brasilia(datetime.now(timezone.utc))} {message}"


def _opened_at(value: Any) -> str:
    if not value:
        return "n/a"
    try:
        opened = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return "n/a"
    return _format_brasilia(opened)


def _format_brasilia(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BRASILIA_TZ).strftime("%Y-%m-%dT%H:%M:%S")


def _age(value: Any) -> str:
    if not value:
        return "n/a"
    try:
        opened = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        seconds = int((datetime.now(timezone.utc) - opened.astimezone(timezone.utc)).total_seconds())
    except ValueError:
        return "n/a"
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, _seconds = divmod(remainder, 60)
    return f"{hours}h{minutes:02d}m"


if __name__ == "__main__":
    main()
