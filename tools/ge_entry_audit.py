from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger

FIVE_MINUTES_MS = 300_000
ONE_MINUTE_MS = 60_000


@dataclass(frozen=True)
class DecisionMatch:
    buy_index: int
    ge_index: int
    buy: Dict[str, Any]
    ge: Dict[str, Any]
    lag_seconds: Optional[float]


def main() -> None:
    args = _parse_args()
    records = select_real_trades(TradeLedger(PROJECT_ROOT).load(), args)
    decisions = read_jsonl(PROJECT_ROOT / "logs" / "decisions.jsonl")
    audits = audit_entries(records, decisions, max_match_seconds=args.match_window_seconds)
    if args.limit:
        audits = audits[-args.limit :]
    print_report(audits, args)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audita mecanicamente o GE15 usado nas entradas reais. "
            "Le apenas ledger, decisions.jsonl e market_context_entry persistido."
        )
    )
    parser.add_argument("--since", help="Inicio local no formato DD/MM HH:MM.")
    parser.add_argument("--until", help="Fim local no formato DD/MM HH:MM.")
    parser.add_argument(
        "--since-field",
        choices=["opened_at", "closed_at"],
        default="opened_at",
    )
    parser.add_argument("--profile", default="intraday")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--match-window-seconds", type=float, default=30.0)
    parser.add_argument("--detail", action="store_true")
    return parser.parse_args()


def select_real_trades(records: Iterable[Dict[str, Any]], args: argparse.Namespace) -> list[Dict[str, Any]]:
    since = parse_local_time(args.since)
    until = parse_local_time(args.until)
    output = []
    for record in records:
        if bool(record.get("phantom", False)):
            continue
        if str(record.get("position_type") or "") != "BOT_EXIT":
            continue
        if args.profile and str(record.get("profile") or "") != args.profile:
            continue
        selected_time = parse_ts(record.get(args.since_field))
        if since is not None and (selected_time is None or selected_time < since):
            continue
        if until is not None and (selected_time is None or selected_time > until):
            continue
        output.append(record)
    return sorted(output, key=lambda item: str(item.get("opened_at") or ""))


def audit_entries(
    records: Iterable[Dict[str, Any]],
    decisions: list[Dict[str, Any]],
    max_match_seconds: float = 30.0,
) -> list[Dict[str, Any]]:
    used_buy_indexes: set[int] = set()
    output = []
    for record in records:
        match = match_entry_decision(
            record,
            decisions,
            used_buy_indexes,
            max_match_seconds=max_match_seconds,
        )
        if match is not None:
            used_buy_indexes.add(match.buy_index)
        output.append(build_audit(record, match))
    return output


def match_entry_decision(
    record: Dict[str, Any],
    decisions: list[Dict[str, Any]],
    used_buy_indexes: set[int],
    max_match_seconds: float,
) -> Optional[DecisionMatch]:
    opened = parse_ts(record.get("opened_at"))
    if opened is None:
        return None
    candidates: list[tuple[float, int]] = []
    for index, event in enumerate(decisions):
        if index in used_buy_indexes:
            continue
        if str(event.get("reason") or "") != "buy_signal" or int_or_none(event.get("gate")) != 5:
            continue
        event_time = parse_ts(event.get("ts"))
        if event_time is None or event_time > opened:
            continue
        lag = (opened - event_time).total_seconds()
        if 0 <= lag <= max_match_seconds:
            candidates.append((lag, index))
    if not candidates:
        return None
    lag, buy_index = min(candidates, key=lambda item: (item[0], -item[1]))
    ge_index = -1
    for index in range(buy_index - 1, -1, -1):
        event = decisions[index]
        if str(event.get("reason") or "") == "ge_structure" and int_or_none(event.get("gate")) == 1:
            ge_index = index
            break
        # Reaching another completed evaluation means this GE cannot belong to the selected buy.
        if str(event.get("reason") or "") == "buy_signal" and int_or_none(event.get("gate")) == 5:
            break
    if ge_index < 0:
        return None
    ge = decisions[ge_index]
    ge_time = parse_ts(ge.get("ts"))
    buy_time = parse_ts(decisions[buy_index].get("ts"))
    if ge_time is None or buy_time is None or abs((buy_time - ge_time).total_seconds()) > 2:
        return None
    return DecisionMatch(
        buy_index=buy_index,
        ge_index=ge_index,
        buy=decisions[buy_index],
        ge=ge,
        lag_seconds=lag,
    )


def build_audit(record: Dict[str, Any], match: Optional[DecisionMatch]) -> Dict[str, Any]:
    source_open_ms = int_or_none(record.get("source_candle_open_time"))
    expected_latest_close_ms = expected_latest_5m_close(source_open_ms)
    context = record.get("market_context_entry")
    context = context if isinstance(context, dict) else {}
    ge_context = context.get("ge15")
    ge_context = ge_context if isinstance(ge_context, dict) else {}
    tf5 = context.get("tf_5m")
    tf5 = tf5 if isinstance(tf5, dict) else {}
    actual_latest_close_ms = int_or_none(
        ge_context.get("latest_closed_at_ms", tf5.get("latest_closed_at_ms"))
    )
    lookback_candles = int_or_none((match.ge if match else {}).get("lookback_candles")) or 3
    interval = str((match.ge if match else {}).get("candle_interval") or "5m")
    interval_ms = interval_to_ms(interval)
    reference_close_ms = (
        actual_latest_close_ms - lookback_candles * interval_ms
        if actual_latest_close_ms is not None and interval_ms is not None
        else None
    )
    freshness, stale_intervals = classify_freshness(
        expected_latest_close_ms,
        actual_latest_close_ms,
        interval_ms or FIVE_MINUTES_MS,
    )
    high_now = float_or_none((match.ge if match else {}).get("high_now"))
    high_reference = float_or_none((match.ge if match else {}).get("high_lookback"))
    low_now = float_or_none((match.ge if match else {}).get("low_now"))
    low_reference = float_or_none((match.ge if match else {}).get("low_lookback"))
    recomputed = (
        high_now > high_reference and low_now > low_reference
        if None not in (high_now, high_reference, low_now, low_reference)
        else None
    )
    logged = bool_or_none((match.ge if match else {}).get("passed"))
    context_status = str(ge_context.get("status") or "UNAVAILABLE")
    arithmetic = (
        "CONSISTENT"
        if recomputed is not None and logged is not None and recomputed == logged
        else "MISMATCH"
        if recomputed is not None and logged is not None
        else "UNAVAILABLE"
    )
    context_consistency = (
        "CONSISTENT"
        if logged is not None and context_status in ("PASS", "BLOCK")
        and (context_status == "PASS") == logged
        else "MISMATCH"
        if logged is not None and context_status in ("PASS", "BLOCK")
        else "UNAVAILABLE"
    )
    boundary_signal = (
        source_open_ms is not None
        and (source_open_ms + ONE_MINUTE_MS) % FIVE_MINUTES_MS == 0
    )
    return {
        "pair_id": record.get("pair_id"),
        "opened_at": record.get("opened_at"),
        "source_candle_open_time": source_open_ms,
        "buy_decision_at": (match.buy if match else {}).get("ts"),
        "match_lag_seconds": match.lag_seconds if match else None,
        "decision_matched": match is not None,
        "interval": interval,
        "lookback_candles": lookback_candles,
        "expected_latest_close_ms": expected_latest_close_ms,
        "actual_latest_close_ms": actual_latest_close_ms,
        "reference_close_ms": reference_close_ms,
        "freshness": freshness,
        "stale_intervals": stale_intervals,
        "boundary_signal": boundary_signal,
        "high_now": high_now,
        "high_reference": high_reference,
        "high_passed": high_now > high_reference if None not in (high_now, high_reference) else None,
        "low_now": low_now,
        "low_reference": low_reference,
        "low_passed": low_now > low_reference if None not in (low_now, low_reference) else None,
        "logged_passed": logged,
        "recomputed_passed": recomputed,
        "context_status": context_status,
        "arithmetic": arithmetic,
        "context_consistency": context_consistency,
    }


def expected_latest_5m_close(source_open_ms: Optional[int]) -> Optional[int]:
    if source_open_ms is None:
        return None
    source_boundary = source_open_ms + ONE_MINUTE_MS
    return (source_boundary // FIVE_MINUTES_MS) * FIVE_MINUTES_MS - 1


def classify_freshness(
    expected_close_ms: Optional[int],
    actual_close_ms: Optional[int],
    interval_ms: int,
) -> tuple[str, Optional[int]]:
    if expected_close_ms is None or actual_close_ms is None or interval_ms <= 0:
        return "UNAVAILABLE", None
    delta = expected_close_ms - actual_close_ms
    if delta == 0:
        return "FRESH", 0
    if delta > 0 and delta % interval_ms == 0:
        intervals = delta // interval_ms
        return f"STALE_{intervals}x{interval_ms // 60_000}m", intervals
    if delta < 0:
        return "FUTURE_CANDLE", None
    return "MISALIGNED", None


def print_report(audits: list[Dict[str, Any]], args: argparse.Namespace) -> None:
    matched = [item for item in audits if item["decision_matched"]]
    arithmetic_available = [item for item in audits if item["arithmetic"] != "UNAVAILABLE"]
    fresh = [item for item in audits if item["freshness"] == "FRESH"]
    stale = [item for item in audits if str(item["freshness"]).startswith("STALE_")]
    future = [item for item in audits if item["freshness"] == "FUTURE_CANDLE"]
    boundary = [item for item in audits if item["boundary_signal"]]
    boundary_stale = [item for item in boundary if str(item["freshness"]).startswith("STALE_")]
    print("TREND-SOL | GE15 entry precision audit")
    print(
        f"Filter | real REAL_A (historical B) | profile={args.profile or 'all'} | "
        f"since={args.since or 'all'} | until={args.until or 'all'}"
    )
    print("Sources | data/trades/trades_B.jsonl | logs/decisions.jsonl | market_context_entry")
    print("Read-only | no Binance request | no orders | no state changes")
    print()
    print("1. UNIVERSE")
    print(f"trades | {len(audits)}")
    print(f"entry decisions matched | {len(matched)}")
    print(f"entry decisions unavailable | {len(audits) - len(matched)}")
    print()
    print("2. ARITHMETIC")
    print(
        f"consistent | {sum(item['arithmetic'] == 'CONSISTENT' for item in audits)}/{len(arithmetic_available)}"
    )
    print(f"mismatch | {sum(item['arithmetic'] == 'MISMATCH' for item in audits)}")
    print(
        f"context vs decision mismatch | "
        f"{sum(item['context_consistency'] == 'MISMATCH' for item in audits)}"
    )
    print()
    print("3. CANDLE FRESHNESS")
    print(f"fresh | {len(fresh)}")
    print(f"stale | {len(stale)}")
    print(f"future/look-ahead | {len(future)}")
    print(f"unavailable/misaligned | {len(audits) - len(fresh) - len(stale) - len(future)}")
    print(f"signals closing on exact 5m boundary | {len(boundary)}")
    print(f"boundary signals that used stale 5m candle | {len(boundary_stale)}")
    print()
    print("4. TRADE-BY-TRADE")
    print(
        "pair_id | opened | source 1m | latest 5m candle used | expected latest 5m | reference candle | "
        "freshness | high now/ref | low now/ref | logged | recalculated | arithmetic | match lag"
    )
    for item in audits:
        print(
            f"{str(item.get('pair_id') or 'n/a')[:12]} | {_fmt_dt(item.get('opened_at'))} | "
            f"{_fmt_ms(item.get('source_candle_open_time'))} | "
            f"{_fmt_candle(item.get('actual_latest_close_ms'), item.get('interval'))} | "
            f"{_fmt_candle(item.get('expected_latest_close_ms'), item.get('interval'))} | "
            f"{_fmt_candle(item.get('reference_close_ms'), item.get('interval'))} | {item.get('freshness')} | "
            f"{_comparison(item.get('high_now'), item.get('high_reference'))} | "
            f"{_comparison(item.get('low_now'), item.get('low_reference'))} | "
            f"{_pass(item.get('logged_passed'))} | {_pass(item.get('recomputed_passed'))} | "
            f"{item.get('arithmetic')} | {_fmt_seconds(item.get('match_lag_seconds'))}"
        )
        if args.detail:
            print(
                f"  boundary={item.get('boundary_signal')} | interval={item.get('interval')} | "
                f"lookback={item.get('lookback_candles')} | context={item.get('context_status')} | "
                f"context_consistency={item.get('context_consistency')} | "
                f"buy_decision_at={item.get('buy_decision_at') or 'n/a'}"
            )
    print()
    print("5. INTERPRETATION")
    if any(item["arithmetic"] == "MISMATCH" for item in audits):
        print("result | GE arithmetic mismatch detected; inspect those rows before strategy analysis")
    elif future:
        print("result | future candle detected; possible look-ahead/data-order defect")
    elif stale:
        print("result | arithmetic is reproducible, but at least one entry used a stale closed 5m candle")
    elif audits and len(matched) == len(audits):
        print("result | recorded GE arithmetic and candle freshness are mechanically consistent")
    else:
        print("result | insufficient recorded data to audit every entry")
    print("note | this audit tests implementation precision, not whether GE15 defines a useful trend")


def read_jsonl(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    output = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                output.append(item)
    return output


def parse_local_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%d/%m %H:%M", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            if fmt == "%d/%m %H:%M":
                parsed = parsed.replace(year=datetime.now(BRASILIA_TZ).year)
            return parsed.replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        except ValueError:
            continue
    raise SystemExit(f"Invalid date/time: {value}")


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def interval_to_ms(value: str) -> Optional[int]:
    if value.endswith("m"):
        try:
            return int(value[:-1]) * 60_000
        except ValueError:
            return None
    if value.endswith("h"):
        try:
            return int(value[:-1]) * 3_600_000
        except ValueError:
            return None
    return None


def int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def bool_or_none(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _fmt_dt(value: Any) -> str:
    parsed = parse_ts(value)
    return parsed.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M:%S") if parsed else "n/a"


def _fmt_ms(value: Any) -> str:
    milliseconds = int_or_none(value)
    if milliseconds is None:
        return "n/a"
    parsed = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return parsed.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M:%S")


def _fmt_candle(close_ms_value: Any, interval_value: Any) -> str:
    close_ms = int_or_none(close_ms_value)
    interval_ms = interval_to_ms(str(interval_value or "5m"))
    if close_ms is None or interval_ms is None:
        return "n/a"
    open_dt = datetime.fromtimestamp((close_ms - interval_ms + 1) / 1000, tz=timezone.utc)
    boundary_dt = datetime.fromtimestamp((close_ms + 1) / 1000, tz=timezone.utc)
    return (
        f"{open_dt.astimezone(BRASILIA_TZ).strftime('%d/%m %H:%M')}"
        f"->{boundary_dt.astimezone(BRASILIA_TZ).strftime('%H:%M')}"
    )


def _comparison(left: Any, right: Any) -> str:
    first = float_or_none(left)
    second = float_or_none(right)
    if first is None or second is None:
        return "n/a"
    symbol = ">" if first > second else "=" if first == second else "<"
    return f"{first:.4f}{symbol}{second:.4f}"


def _pass(value: Any) -> str:
    return "PASS" if value is True else "BLOCK" if value is False else "n/a"


def _fmt_seconds(value: Any) -> str:
    number = float_or_none(value)
    return f"{number:.1f}s" if number is not None else "n/a"


if __name__ == "__main__":
    main()
