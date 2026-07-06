from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.state_manager import StateManager
from src.console_utils import console_line


def main() -> None:
    state = StateManager(PROJECT_ROOT).load_open_positions()
    if not state:
        print(console_line("No local open positions in data/state/open_positions.json"))
        return
    price = _optional_float(sys.argv[1]) if len(sys.argv) > 1 else None
    print(console_line("pair_id      pos type           status        entry     qty       pnl_pct  peak      stop      age"))
    for item in sorted(state, key=lambda row: (row.get("pair_id", ""), row.get("label", ""))):
        pnl = _pnl(item, price)
        print(console_line(
            f"{str(item.get('pair_id', ''))[:12]:12} "
            f"{item.get('label', ''):3} "
            f"{_friendly_engine(item):14} "
            f"{item.get('status', ''):12} "
            f"{_fmt(item.get('entry_price')):8} "
            f"{_fmt(item.get('quantity')):8} "
            f"{_fmt(pnl):8} "
            f"{_fmt(item.get('highest_price')):8} "
            f"{_fmt(item.get('stop_price')):8} "
            f"{_age(item.get('open_ts'))}"
        ))


def _friendly_engine(item: Dict[str, Any]) -> str:
    if item.get("label") == "A":
        return "BINANCE_TRAIL"
    if item.get("label") == "B":
        return "BOT_EXIT"
    return str(item.get("engine", ""))


def _pnl(item: Dict[str, Any], price: Optional[float]) -> Optional[float]:
    entry = _optional_float(item.get("entry_price"))
    if price is None or entry in (None, 0.0):
        return None
    return ((price / entry) - 1) * 100


def _fmt(value: Any) -> str:
    number = _optional_float(value)
    if number is None:
        return "n/a"
    return f"{number:.4f}"


def _optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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
