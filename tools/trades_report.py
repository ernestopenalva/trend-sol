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
    filter_text = f"since={args.since or 'all'} | strategy={args.strategy or 'all'} | mode={config.get('active_profile') or 'unknown'}"
    if args.run_id:
        filter_text = f"{filter_text} | run_id={args.run_id}"
    print(f"Filter: {filter_text}")
    print()
    _print_summary(records, config)
    print()
    _print_inline_counts("Exit reasons", Counter(_exit_reason(record) for record in records))
    print()
    if args.detail:
        _print_detail_sections(records)
        print()
        _print_detail(records, config)
    else:
        _print_trades(records, config)


def _print_summary(records: list[Dict[str, Any]], config: Dict[str, Any]) -> None:
    pnls = [_gross_pnl(record) for record in records]
    pnls = [value for value in pnls if value is not None]
    net_pnls = [_net_pnl(record, config) for record in records]
    net_pnls = [value for value in net_pnls if value is not None]
    ages = [_float(record.get("age_seconds")) for record in records]
    ages = [value for value in ages if value is not None]
    wins = [value for value in pnls if value > 0]
    net_wins = [value for value in net_pnls if value > 0]
    gross_total = sum(pnls) if pnls else 0
    estimated_fees = _estimated_fees_pct_for_records(records, config)
    estimated_fees_usdt = _estimated_fees_usdt_for_records(records, config)
    estimated_fees_usdt_display = -estimated_fees_usdt if estimated_fees_usdt is not None else None
    net_total = sum(net_pnls) if net_pnls else gross_total - estimated_fees
    print("Summary:")
    print(
        f"  trades: {len(records)} | "
        f"avg age={_fmt_duration(sum(ages) / len(ages) if ages else 0)} | "
        f"slots full time={_slots_full_pct(records, config):.1f}%"
    )
    print(
        f"  fees: estimated sum={_fmt_signed_pct(-estimated_fees)} "
        f"({_fmt_signed_usdt(estimated_fees_usdt_display)}) | {_fee_summary(config)}"
    )
    print(
        f"  gross: total={_fmt_signed_pct(gross_total)} | "
        f"avg/trade={_fmt_signed_pct(sum(pnls) / len(pnls) if pnls else 0)} | "
        f"best={_fmt_signed_pct(max(pnls) if pnls else 0)} | "
        f"worst={_fmt_signed_pct(min(pnls) if pnls else 0)} | "
        f"winrate={(len(wins) / len(pnls) * 100) if pnls else 0:.1f}%"
    )
    print(
        f"  net: total={_fmt_signed_pct(net_total)} | "
        f"avg/trade={_fmt_signed_pct(sum(net_pnls) / len(net_pnls) if net_pnls else 0)} | "
        f"best={_fmt_signed_pct(max(net_pnls) if net_pnls else 0)} | "
        f"worst={_fmt_signed_pct(min(net_pnls) if net_pnls else 0)} | "
        f"winrate={(len(net_wins) / len(net_pnls) * 100) if net_pnls else 0:.1f}%"
    )


def _print_detail_sections(records: list[Dict[str, Any]]) -> None:
    slippages = [_float(record.get("exit_slippage_pct")) for record in records]
    slippages = [value for value in slippages if value is not None]
    print("Technical:")
    print(f"  avg exit slippage: {_fmt_signed_pct(sum(slippages) / len(slippages) if slippages else 0)}")
    print(f"  best exit slippage: {_fmt_signed_pct(max(slippages) if slippages else 0)}")
    print(f"  worst exit slippage: {_fmt_signed_pct(min(slippages) if slippages else 0)}")
    complete_records = [record for record in records if record.get("trough_tracking_complete") is not False]
    trough_pcts = [_float(record.get("trough_pct")) for record in complete_records]
    trough_pcts = [value for value in trough_pcts if value is not None]
    trough_atrs = [_float(record.get("trough_atr")) for record in complete_records]
    trough_atrs = [value for value in trough_atrs if value is not None]
    trough_times = [_float(record.get("time_to_trough_seconds")) for record in complete_records]
    trough_times = [value for value in trough_times if value is not None]
    print()
    print(f"MAE / trough (N={len(trough_pcts)}/{len(records)}):")
    print(f"  avg trough: {_fmt_signed_pct(sum(trough_pcts) / len(trough_pcts) if trough_pcts else None)}")
    print(f"  worst trough: {_fmt_signed_pct(min(trough_pcts) if trough_pcts else None)}")
    print(f"  avg trough ATR: {_fmt_signed_number(sum(trough_atrs) / len(trough_atrs) if trough_atrs else None)}")
    print(f"  avg time to trough: {_fmt_optional_duration(sum(trough_times) / len(trough_times) if trough_times else None)}")
    print()
    _print_counts("Peak step", Counter(str(record.get("final_step") or "UNKNOWN") for record in records))
    print()
    _print_counts("BE floor source", Counter(_be_floor_source(record) for record in records if _be_floor_source(record)))
    print()
    _print_counts(
        "BE floor absorbed ATR stop",
        Counter(str(record.get("be_floor_absorbed_atr_stop")) for record in records if record.get("be_floor_absorbed_atr_stop") is not None),
    )


def _print_counts(title: str, counts: Counter[str]) -> None:
    print(f"{title}:")
    if not counts:
        print("  none: 0")
        return
    for key, value in sorted(counts.items()):
        print(f"  {key}: {value}")


def _print_inline_counts(title: str, counts: Counter[str]) -> None:
    values = counts or Counter({"none": 0})
    print(f"{title}: " + " | ".join(f"{key}={value}" for key, value in sorted(values.items())))


def _print_trades(records: list[Dict[str, Any]], config: Dict[str, Any]) -> None:
    print("Trades:")
    print(
        f"{'opened':11} {'closed':11} {'age':6} {'entry':8} {'peak':18} {'trough':18} "
        f"{'exit':8} {'giveback':19} {'gross':7} {'net':7} reason"
    )
    for record in records:
        print(
            f"{_short_time(record.get('opened_at')):11} "
            f"{_short_time(record.get('closed_at')):11} "
            f"{_fmt_duration(_float(record.get('age_seconds')) or 0):6} "
            f"{_fmt_price(record.get('entry_price')):8} "
            f"{_peak_cell(record):18} "
            f"{_trough_cell(record):18} "
            f"{_fmt_price(record.get('exit_price')):8} "
            f"{_giveback_cell(record):19} "
            f"{_fmt_signed_pct(_gross_pnl(record)):7} "
            f"{_fmt_signed_pct(_net_pnl(record, config)):7} "
            f"{_exit_reason(record)}"
        )
    if any(record.get("trough_price") is not None and record.get("trough_tracking_complete") is False for record in records):
        print("* observed trough; tracking started after the trade opened")


def _print_detail(records: list[Dict[str, Any]], config: Dict[str, Any]) -> None:
    print("Trades detail:")
    for record in records:
        print(
            f"{record.get('pair_id')} qty={_fmt_number(record.get('qty'))} "
            f"entry_atr={_fmt_number(record.get('entry_atr'))} stop_hit={_fmt_price(record.get('stop_hit'))} "
            f"exit={_fmt_price(record.get('exit_price'))} slip={_fmt_signed_pct(record.get('exit_slippage_pct'))} "
            f"exit_source={record.get('exit_price_source') or 'n/a'} "
            f"trigger={_fmt_price(record.get('exit_trigger_price'))} trigger_source={record.get('exit_trigger_price_source') or 'n/a'} "
            f"gross={_fmt_signed_pct(_gross_pnl(record))} fees={_fmt_signed_pct(-(_float(record.get('estimated_fees_pct')) or 0))} "
            f"net={_fmt_signed_pct(_net_pnl(record, config))} giveback={_giveback_cell(record)} "
            f"hard_stop={_fmt_price(record.get('hard_stop_price'))} hard_stop_pct={_fmt_loss_pct(record.get('hard_stop_pct'))} "
            f"hard_stop_on_restore={record.get('hard_stop_applied_on_restore')} "
            f"be_stop={_fmt_price(record.get('be_stop'))} be_net={_fmt_price(record.get('be_net_floor'))} "
            f"be_activation={_fmt_price(record.get('be_activation_price'))} be_source={record.get('be_floor_source') or 'n/a'} "
            f"be_absorbed={record.get('be_floor_absorbed_atr_stop')} "
            f"peak={_fmt_price(record.get('peak_price'))} pnl_abs={_fmt_number(record.get('realized_pnl_abs'))} "
            f"trough={_fmt_price(record.get('trough_price'))} trough_pct={_fmt_signed_pct(record.get('trough_pct'))} "
            f"trough_atr={_fmt_signed_number(record.get('trough_atr'))} trough_at={record.get('trough_at') or 'n/a'} "
            f"time_to_trough={_fmt_optional_duration(record.get('time_to_trough_seconds'))} "
            f"trough_complete={record.get('trough_tracking_complete')} "
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
    ladder: Dict[str, Any] = {}
    in_fees = False
    in_capital = False
    in_ladder = False
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" ") and line.endswith(":"):
            in_fees = line.strip() == "fees:"
            in_capital = line.strip() == "capital:"
            in_ladder = line.strip() == "ladder:"
            continue
        if not line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            if key.strip() == "active_profile":
                config["active_profile"] = _basic_value(value)
            in_fees = False
            in_capital = False
            in_ladder = False
            continue
        if in_fees and line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            fees[key.strip()] = _basic_value(value)
        if in_capital and line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            capital[key.strip()] = _basic_value(value)
        if in_ladder and line.startswith(" ") and ":" in line:
            key, value = line.split(":", 1)
            ladder[key.strip()] = _basic_value(value)
    if fees:
        config["fees"] = fees
    if capital:
        config["capital"] = capital
    if ladder:
        config["ladder"] = ladder
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
    return trade_count * _round_trip_fee_pct(config)


def _round_trip_fee_pct(config: Dict[str, Any]) -> float:
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    if not fees or not bool(fees.get("enabled", False)):
        return 0.0
    taker_fee = _float(fees.get("taker_fee_pct")) or 0.0
    if bool(fees.get("use_bnb_discount", False)):
        taker_fee *= 0.75
    return taker_fee * 2


def _effective_taker_fee_pct(config: Dict[str, Any]) -> float:
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    if not fees or not bool(fees.get("enabled", False)):
        return 0.0
    taker_fee = _float(fees.get("taker_fee_pct")) or 0.0
    if bool(fees.get("use_bnb_discount", False)):
        taker_fee *= 0.75
    return taker_fee


def _fee_summary(config: Dict[str, Any]) -> str:
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    if not fees or not bool(fees.get("enabled", False)):
        return "disabled"
    discount = "yes" if bool(fees.get("use_bnb_discount", False)) else "no"
    configured_taker = _float(fees.get("taker_fee_pct")) or 0.0
    effective_taker = _effective_taker_fee_pct(config)
    round_trip = _round_trip_fee_pct(config)
    return (
        f"taker={configured_taker:.3f}%/side | "
        f"effective taker={effective_taker:.3f}%/side | "
        f"BNB discount={discount} | "
        f"round-trip={round_trip:.3f}%/trade"
    )


def _estimated_fees_pct_for_records(records: list[Dict[str, Any]], config: Dict[str, Any]) -> float:
    total = 0.0
    missing = 0
    for record in records:
        fee = _float(record.get("estimated_fees_pct"))
        if fee is None:
            missing += 1
        else:
            total += fee
    return total + _estimated_fees_pct(missing, config)


def _estimated_fees_usdt_for_records(records: list[Dict[str, Any]], config: Dict[str, Any]) -> Optional[float]:
    if not records:
        return 0.0
    total = 0.0
    found = False
    for record in records:
        notional = _position_notional_usdt(record)
        if notional is None:
            continue
        fee_pct = _float(record.get("estimated_fees_pct"))
        if fee_pct is None:
            fee_pct = _round_trip_fee_pct(config)
        total += notional * fee_pct / 100
        found = True
    return total if found else None


def _gross_pnl(record: Dict[str, Any]) -> Optional[float]:
    value = _float(record.get("gross_pnl_pct"))
    if value is not None:
        return value
    return _float(record.get("realized_pnl_pct"))


def _net_pnl(record: Dict[str, Any], config: Dict[str, Any]) -> Optional[float]:
    value = _float(record.get("net_pnl_pct"))
    if value is not None:
        return value
    gross = _gross_pnl(record)
    if gross is None:
        return None
    return gross - _estimated_fees_pct(1, config)


def _position_notional_usdt(record: Dict[str, Any]) -> Optional[float]:
    configured = _float(record.get("position_notional_usdt"))
    if configured is not None and configured > 0:
        return configured
    quantity = _float(record.get("qty"))
    entry_price = _float(record.get("entry_price"))
    if quantity is not None and entry_price is not None and quantity > 0 and entry_price > 0:
        return quantity * entry_price
    return None


def _be_floor_source(record: Dict[str, Any]) -> str:
    return str(record.get("be_floor_source") or "")


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
    return parsed.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M") if parsed else "n/a"


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


def _fmt_optional_duration(seconds: Any) -> str:
    number = _float(seconds)
    return "n/a" if number is None else _fmt_duration(number)


def _fmt_price(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:.4f}"


def _fmt_number(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:.4f}"


def _fmt_signed_number(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:+.4f}"


def _fmt_signed_usdt(value: Any) -> str:
    number = _float(value)
    return "n/a USDT" if number is None else f"{number:+.4f} USDT"


def _fmt_signed_pct(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"{number:+.2f}%"


def _fmt_loss_pct(value: Any) -> str:
    number = _float(value)
    return "n/a" if number is None else f"-{number:.2f}%"


def _peak_cell(record: Dict[str, Any]) -> str:
    return _price_pct_cell(record.get("peak_price"), record.get("entry_price"))


def _trough_cell(record: Dict[str, Any]) -> str:
    price = record.get("trough_price")
    if price is None:
        return "n/a"
    suffix = "*" if record.get("trough_tracking_complete") is False else ""
    return _price_pct_cell(price, record.get("entry_price"), suffix=suffix, pct=record.get("trough_pct"))


def _giveback_cell(record: Dict[str, Any]) -> str:
    peak = _float(record.get("peak_price"))
    exit_price = _float(record.get("exit_price"))
    if peak in (None, 0.0) or exit_price is None:
        return "n/a"
    difference = peak - exit_price
    percentage = difference / peak * 100
    return f"{difference:+.4f} ({percentage:+.2f}%)"


def _price_pct_cell(price: Any, entry_price: Any, suffix: str = "", pct: Any = None) -> str:
    numeric_price = _float(price)
    numeric_entry = _float(entry_price)
    numeric_pct = _float(pct)
    if numeric_price is None:
        return "n/a"
    if numeric_pct is None and numeric_entry not in (None, 0.0):
        numeric_pct = (numeric_price / numeric_entry - 1) * 100
    pct_text = _fmt_signed_pct(numeric_pct)
    return f"{_fmt_price(numeric_price)}{suffix} ({pct_text})"


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
