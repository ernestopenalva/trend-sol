from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

try:
    import yaml
except ImportError:  # pragma: no cover - production venv includes PyYAML
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger


def main() -> None:
    args = _parse_args()
    config = _load_config()
    records = TradeLedger(PROJECT_ROOT).load()
    filtered = _filter(records, args)
    if args.csv:
        _write_csv(filtered, Path(args.csv))
        print(f"CSV written: {args.csv}")
        return
    _print_report(filtered, args, config)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Relatorio de trades fechados do Bot B.")
    parser.add_argument("--since", help="Filtra trades a partir da data/hora informada.")
    parser.add_argument("--since-field", choices=["opened_at", "closed_at"], default="closed_at")
    parser.add_argument("--strategy", help="Filtra por strategy_version.")
    parser.add_argument("--run-id", help="Filtra por run_id.")
    parser.add_argument("--detail", action="store_true", help="Mostra campos tecnicos completos.")
    parser.add_argument("--csv", help="Exporta CSV para o caminho informado.")
    return parser.parse_args()


def _filter(records: list[Dict[str, Any]], args: argparse.Namespace) -> list[Dict[str, Any]]:
    since = _parse_since(args.since)
    output = []
    for record in records:
        if args.strategy and record.get("strategy_version") != args.strategy:
            continue
        if args.run_id and record.get("run_id") != args.run_id:
            continue
        if since is not None:
            value = _parse_ts(record.get(args.since_field))
            if value is None or value < since:
                continue
        output.append(record)
    return sorted(output, key=lambda item: str(item.get("closed_at") or ""))


def _print_report(records: list[Dict[str, Any]], args: argparse.Namespace, config: Dict[str, Any]) -> None:
    print("TREND-SOL | Bot B trades report")
    print()
    print("Filter:")
    print(f"  since: {args.since or 'all'}")
    print(f"  strategy: {args.strategy or 'all'}")
    print(f"  mode: {config.get('active_profile') or 'unknown'}")
    if args.run_id:
        print(f"  run_id: {args.run_id}")
    print()
    _print_summary(records, config)
    print()
    _print_counts("Exit reasons", Counter(_exit_reason(record) for record in records))
    print()
    _print_counts("Ladder", Counter(str(record.get("final_step") or "UNKNOWN") for record in records))
    print()
    if args.detail:
        _print_detail(records)
    else:
        _print_trades(records)


def _print_summary(records: list[Dict[str, Any]], config: Dict[str, Any]) -> None:
    pnls = [_float(record.get("realized_pnl_pct")) for record in records]
    pnls = [value for value in pnls if value is not None]
    ages = [_float(record.get("age_seconds")) for record in records]
    ages = [value for value in ages if value is not None]
    wins = [value for value in pnls if value > 0]
    gross_total = sum(pnls) if pnls else 0
    estimated_fees = _estimated_fees_pct(len(records), config)
    net_total = gross_total - estimated_fees
    print("Summary:")
    print(f"  trades: {len(records)}")
    print(f"  gross total: {_fmt_signed_pct(gross_total)}")
    print(f"  estimated fees: {_fmt_signed_pct(-estimated_fees)}")
    print(f"  net total: {_fmt_signed_pct(net_total)}")
    print(f"  avg/trade: {_fmt_signed_pct(sum(pnls) / len(pnls) if pnls else 0)}")
    print(f"  best: {_fmt_signed_pct(max(pnls) if pnls else 0)}")
    print(f"  worst: {_fmt_signed_pct(min(pnls) if pnls else 0)}")
    print(f"  winrate: {(len(wins) / len(pnls) * 100) if pnls else 0:.1f}%")
    print(f"  avg age: {_fmt_duration(sum(ages) / len(ages) if ages else 0)}")
    print(f"  slots full time: {_slots_full_pct(records, config):.1f}%")


def _print_counts(title: str, counts: Counter[str]) -> None:
    print(f"{title}:")
    if not counts:
        print("  none: 0")
        return
    for key, value in sorted(counts.items()):
        print(f"  {key}: {value}")


def _print_trades(records: list[Dict[str, Any]]) -> None:
    print("Trades:")
    print("opened  closed  age   entry    exit     pnl     peak_atr  step   reason")
    for record in records:
        print(
            f"{_short_time(record.get('opened_at')):7} "
            f"{_short_time(record.get('closed_at')):7} "
            f"{_fmt_duration(_float(record.get('age_seconds')) or 0):5} "
            f"{_fmt_price(record.get('entry_price')):8} "
            f"{_fmt_price(record.get('exit_price')):8} "
            f"{_fmt_signed_pct(record.get('realized_pnl_pct')):7} "
            f"{_fmt_number(record.get('peak_atr')):8} "
            f"{str(record.get('final_step') or 'n/a'):6} "
            f"{_exit_reason(record)}"
        )


def _print_detail(records: list[Dict[str, Any]]) -> None:
    print("Trades detail:")
    for record in records:
        print(
            f"{record.get('pair_id')} qty={_fmt_number(record.get('qty'))} "
            f"entry_atr={_fmt_number(record.get('entry_atr'))} stop_hit={_fmt_price(record.get('stop_hit'))} "
            f"peak={_fmt_price(record.get('peak_price'))} pnl_abs={_fmt_number(record.get('realized_pnl_abs'))} "
            f"run_id={record.get('run_id')} strategy={record.get('strategy_version')} "
            f"opened_at={record.get('opened_at')} closed_at={record.get('closed_at')}"
        )


def _write_csv(records: list[Dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for record in records for key in record.keys()})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def _load_config() -> Dict[str, Any]:
    path = PROJECT_ROOT / "config" / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if yaml is None:
        return _parse_basic_config(text)
    try:
        data = yaml.safe_load(text) or {}
    except Exception:
        return _parse_basic_config(text)
    return data if isinstance(data, dict) else {}


def _parse_basic_config(text: str) -> Dict[str, Any]:
    config: Dict[str, Any] = {}
    fees: Dict[str, Any] = {}
    capital: Dict[str, Any] = {}
    in_fees = False
    in_capital = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            in_fees = line.strip() == "fees:"
            in_capital = line.strip() == "capital:"
            continue
        if not line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            if key.strip() == "active_profile":
                config["active_profile"] = _basic_value(value)
            in_fees = False
            in_capital = False
            continue
        if in_fees and line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            fees[key.strip()] = _basic_value(value)
        if in_capital and line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            capital[key.strip()] = _basic_value(value)
    if fees:
        config["fees"] = fees
    if capital:
        config["capital"] = capital
    return config


def _basic_value(value: str) -> Any:
    text = value.strip().strip('"').strip("'")
    if text.lower() == "true":
        return True
    if text.lower() == "false":
        return False
    try:
        return float(text)
    except ValueError:
        return text


def _estimated_fees_pct(trade_count: int, config: Dict[str, Any]) -> float:
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    if not fees or not bool(fees.get("enabled", False)):
        return 0.0
    taker_fee = _float(fees.get("taker_fee_pct")) or 0.0
    if bool(fees.get("use_bnb_discount", False)):
        taker_fee *= 0.75
    return trade_count * taker_fee * 2


def _slots_full_pct(records: list[Dict[str, Any]], config: Dict[str, Any]) -> float:
    capital = config.get("capital") if isinstance(config.get("capital"), dict) else {}
    max_open_positions = int(capital.get("max_open_positions", capital.get("max_open_pairs", 1)) or 1)
    if max_open_positions <= 0:
        return 0.0

    events: list[tuple[datetime, int]] = []
    for record in records:
        opened = _parse_ts(record.get("opened_at"))
        closed = _parse_ts(record.get("closed_at"))
        if opened is None or closed is None or closed <= opened:
            continue
        events.append((opened, 1))
        events.append((closed, -1))
    if len(events) < 2:
        return 0.0

    events.sort(key=lambda item: (item[0], item[1]))
    first = events[0][0]
    last = events[-1][0]
    total_seconds = (last - first).total_seconds()
    if total_seconds <= 0:
        return 0.0

    open_positions = 0
    previous = first
    full_seconds = 0.0
    for ts, delta in events:
        elapsed = (ts - previous).total_seconds()
        if elapsed > 0 and open_positions >= max_open_positions:
            full_seconds += elapsed
        open_positions += delta
        previous = ts
    return full_seconds / total_seconds * 100


def _exit_reason(record: Dict[str, Any]) -> str:
    reason = str(record.get("exit_reason") or "UNKNOWN")
    if reason == "REVIEW_STOP" and str(record.get("final_step") or "") == "BE":
        return "BREAKEVEN"
    return reason


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            parsed = datetime.combine(date.fromisoformat(text), time.min)
        else:
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        raise SystemExit(f"Invalid --since value: {value}")
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


def _short_time(value: Any) -> str:
    parsed = _parse_ts(value)
    return parsed.astimezone(BRASILIA_TZ).strftime("%H:%M") if parsed else "n/a"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _fmt_price(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:.4f}"


def _fmt_number(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:.4f}"


def _fmt_signed_pct(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:+.2f}%"


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
