from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.trades_report import _parse_since, _parse_ts


SHADOW_STEP_EVENT = re.compile(r"^PROFIT_LOCK_SHADOW_(?:ATR|PCT)_(\d+)$")
REAL_STEP_EVENT = re.compile(r"^PROFIT_LOCK_(?:ATR|PCT)_(\d+)$")


def main() -> None:
    args = _arguments()
    ledger_path = _resolve(args.ledger)
    events_path = _resolve(args.events)
    records = _real_sol_records(_load_jsonl(ledger_path), args.since, args.since_field)
    events = _events_for_records(_load_jsonl(events_path), records)
    print_report(records, events, ledger_path, events_path, args)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estudo offline do Profit Lock net-floor shadow em trades reais SOLUSDT."
    )
    parser.add_argument(
        "--ledger",
        default="data/trades/trades_B.jsonl",
        help="Ledger JSONL de trades reais.",
    )
    parser.add_argument(
        "--events",
        default="logs/trades.jsonl",
        help="JSONL de eventos de trade.",
    )
    parser.add_argument("--since", help="Data/hora inicial em DD/MM HH:MM ou ISO 8601.")
    parser.add_argument(
        "--since-field",
        choices=("opened_at", "closed_at"),
        default="opened_at",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="Maximo de casos na comparacao trade a trade.",
    )
    return parser.parse_args()


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    output: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                output.append(value)
    return output


def _real_sol_records(
    records: list[dict[str, Any]],
    since_text: str | None,
    since_field: str,
) -> list[dict[str, Any]]:
    since = _parse_since(since_text)
    output = []
    for record in records:
        if str(record.get("symbol") or "").upper() != "SOLUSDT":
            continue
        if bool(record.get("phantom", False)):
            continue
        if str(record.get("position_type") or "") != "BOT_EXIT":
            continue
        if since is not None:
            timestamp = _parse_ts(record.get(since_field))
            if timestamp is None or timestamp < since:
                continue
        output.append(record)
    return sorted(output, key=lambda item: str(item.get("closed_at") or ""))


def _events_for_records(
    events: list[dict[str, Any]],
    records: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    pair_ids = {str(item.get("pair_id")) for item in records}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        pair_id = str(event.get("pair_id") or "")
        if pair_id not in pair_ids or bool(event.get("phantom", False)):
            continue
        grouped[pair_id].append(event)
    for items in grouped.values():
        items.sort(key=lambda item: str(item.get("ts") or ""))
    return grouped


def print_report(
    records: list[dict[str, Any]],
    events: dict[str, list[dict[str, Any]]],
    ledger_path: Path,
    events_path: Path,
    args: argparse.Namespace,
) -> None:
    real_pl = [item for item in records if str(item.get("exit_reason")) == "PROFIT_LOCK"]
    reached_real_pl = [item for item in records if _real_pl_step(item, events) is not None]
    observable = [item for item in records if _shadow_outcome(item, events) is not None]
    censored = [item for item in records if bool(item.get("pl_shadow_censored_by_real_exit"))]

    print("TREND-SOL | Profit Lock net-floor shadow study")
    print(
        f"Filter: symbol=SOLUSDT | real only | phantoms=no | "
        f"since={args.since or 'all'} | since_field={args.since_field}"
    )
    print(f"Ledger: {ledger_path}")
    print(f"Events: {events_path}")

    print("\n1. UNIVERSO")
    print(f"real trades | {len(records)}")
    print(f"reached real Profit Lock | {len(reached_real_pl)}")
    print(f"shadow observable | {len(observable)}")
    print(f"censored by real exit | {len(censored)}")
    print(f"covered period | {_period(records)}")

    print("\n2. PROFIT LOCK REAL")
    _print_outcome_summary(real_pl, shadow=False)
    step_counts = Counter(_real_pl_step(item, events) or "UNKNOWN" for item in real_pl)
    print("exit distribution | " + _counts(step_counts))

    print("\n3. NET-FLOOR SHADOW | observable cases only")
    _print_shadow_summary(observable, events)

    print("\n4. IMPACTO DO FLOOR | activated shadow steps from recorded events")
    impacts = _floor_impacts(events)
    if not impacts:
        print("no recorded PROFIT_LOCK_SHADOW_* activation events")
    else:
        for step in ("PL1", "PL2", "PL3"):
            items = [item for item in impacts if item["step"] == step]
            absorbed = sum(bool(item["absorbed"]) for item in items)
            print(f"{step} | activations={len(items)} | floor absorbed raw stop={absorbed}")
        unchanged = sum(not bool(item["absorbed"]) for item in impacts)
        print(f"all steps | floor did not change stop={unchanged}")
        print(
            f"raw_stop avg={_fmt(_average(item['raw'] for item in impacts), 6)} | "
            f"net_floor avg={_fmt(_average(item['floor'] for item in impacts), 6)} | "
            f"avg final-minus-raw={_fmt_signed(_average(item['difference_pct'] for item in impacts))} of entry"
        )

    print("\n5. COMPARACAO TRADE A TRADE")
    print(
        "pair_id | opened_at | real_reason | real_net | shadow_status | shadow_step | "
        "shadow_net_est | delta_net | censored"
    )
    comparisons = _comparisons(records, events)
    for item in comparisons[: max(0, args.limit)]:
        print(
            f"{item['pair_id']} | {item['opened_at']} | {item['real_reason']} | "
            f"{_fmt_signed(item['real_net'])} | {item['shadow_status']} | {item['shadow_step']} | "
            f"{_fmt_signed(item['shadow_net'])} | {_fmt_signed(item['delta'])} | {item['censored']}"
        )
    if not comparisons:
        print("none")

    print("\n6. CONCLUSAO")
    _print_conclusion(records, observable, events)


def _print_outcome_summary(records: list[dict[str, Any]], shadow: bool) -> None:
    gross = [_number(item.get("gross_pnl_pct")) for item in records]
    net = [_number(item.get("net_pnl_pct")) for item in records]
    negatives = sum(value < 0 for value in net if value is not None)
    print(
        f"trades={len(records)} | gross total={_fmt_signed(_sum(gross))} | "
        f"net total={_fmt_signed(_sum(net))} | avg net={_fmt_signed(_average(net))} | "
        f"net<0={negatives} ({_pct(negatives, len(net))})"
    )


def _print_shadow_summary(
    records: list[dict[str, Any]],
    events: dict[str, list[dict[str, Any]]],
) -> None:
    outcomes = [_shadow_outcome(item, events) for item in records]
    outcomes = [item for item in outcomes if item is not None]
    shadow_gross = [item[0] for item in outcomes]
    shadow_net = [item[1] for item in outcomes]
    real_net = [_number(item.get("net_pnl_pct")) for item in records]
    deltas = [shadow - real for shadow, real in zip(shadow_net, real_net) if real is not None]
    negatives = sum(value < 0 for value in shadow_net)
    print(
        f"trades={len(outcomes)} | gross est={_fmt_signed(_sum(shadow_gross))} | "
        f"net est={_fmt_signed(_sum(shadow_net))} | avg net={_fmt_signed(_average(shadow_net))} | "
        f"net<0={negatives} ({_pct(negatives, len(shadow_net))})"
    )
    print(
        f"corresponding real net={_fmt_signed(_sum(real_net))} | "
        f"estimated delta={_fmt_signed(_sum(deltas))}"
    )


def _real_pl_step(
    record: dict[str, Any],
    events: dict[str, list[dict[str, Any]]],
) -> str | None:
    highest = 0
    for event in events.get(str(record.get("pair_id")), []):
        match = REAL_STEP_EVENT.match(str(event.get("event") or ""))
        if match:
            highest = max(highest, int(match.group(1)))
    if highest:
        return f"PL{highest}"
    final_step = str(record.get("final_step") or "")
    return final_step if final_step in {"PL1", "PL2", "PL3"} else None


def _shadow_snapshot(
    record: dict[str, Any],
    events: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    snapshot = dict(record)
    for event in events.get(str(record.get("pair_id")), []):
        if str(event.get("event")) == "PROFIT_LOCK_SHADOW_CLOSE":
            snapshot.update(event)
    return snapshot


def _shadow_outcome(
    record: dict[str, Any],
    events: dict[str, list[dict[str, Any]]],
) -> tuple[float, float] | None:
    snapshot = _shadow_snapshot(record, events)
    if str(snapshot.get("pl_shadow_status") or "") != "CLOSED":
        return None
    entry = _number(record.get("entry_price"))
    close = _number(snapshot.get("pl_shadow_close_price"))
    if close is None:
        close = _number(snapshot.get("price"))
    if entry is None or entry <= 0 or close is None:
        return None
    gross = (close / entry - 1) * 100
    fees = _number(record.get("estimated_fees_pct")) or 0.0
    return gross, gross - fees


def _floor_impacts(
    events: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for pair_id, items in events.items():
        for event in items:
            match = SHADOW_STEP_EVENT.match(str(event.get("event") or ""))
            if not match:
                continue
            step = f"PL{match.group(1)}"
            raw = _number(event.get("pl_shadow_raw_stop"))
            floor = _number(event.get("pl_shadow_net_floor"))
            stop = _number(event.get("pl_shadow_stop"))
            entry = _number(event.get("price"))
            entry_atr = _number(event.get("entry_atr"))
            pnl_atr = _number(event.get("pnl_atr"))
            if entry is not None and entry_atr and pnl_atr is not None:
                entry = entry - pnl_atr * entry_atr
            difference = ((stop - raw) / entry * 100) if None not in (stop, raw, entry) and entry else None
            unique[(pair_id, step)] = {
                "step": step,
                "raw": raw,
                "floor": floor,
                "absorbed": bool(event.get("pl_shadow_floor_absorbed")),
                "difference_pct": difference,
            }
    return list(unique.values())


def _comparisons(
    records: list[dict[str, Any]],
    events: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    output = []
    for record in records:
        outcome = _shadow_outcome(record, events)
        snapshot = _shadow_snapshot(record, events)
        real_net = _number(record.get("net_pnl_pct"))
        shadow_net = outcome[1] if outcome else None
        delta = shadow_net - real_net if shadow_net is not None and real_net is not None else None
        censored = bool(record.get("pl_shadow_censored_by_real_exit"))
        if outcome is None and not censored and not record.get("pl_shadow_enabled"):
            continue
        output.append(
            {
                "pair_id": record.get("pair_id") or "n/a",
                "opened_at": record.get("opened_at") or "n/a",
                "real_reason": record.get("exit_reason") or "n/a",
                "real_net": real_net,
                "shadow_status": snapshot.get("pl_shadow_status") or "n/a",
                "shadow_step": snapshot.get("pl_shadow_active_step") or snapshot.get("pl_shadow_step") or "n/a",
                "shadow_net": shadow_net,
                "delta": delta,
                "censored": censored,
            }
        )
    output.sort(
        key=lambda item: (
            0 if item["real_reason"] == "PROFIT_LOCK" and (item["real_net"] or 0) < 0 else 1,
            0 if item["delta"] is not None else 1,
            -abs(item["delta"] or 0),
        )
    )
    return output


def _print_conclusion(
    records: list[dict[str, Any]],
    observable: list[dict[str, Any]],
    events: dict[str, list[dict[str, Any]]],
) -> None:
    observable_pl_negative = [
        item
        for item in observable
        if str(item.get("exit_reason")) == "PROFIT_LOCK"
        and (_number(item.get("net_pnl_pct")) or 0) < 0
    ]
    improved_negatives = 0
    avoided = 0
    deltas = []
    worse = 0
    for record in observable:
        outcome = _shadow_outcome(record, events)
        real_net = _number(record.get("net_pnl_pct"))
        if outcome is None or real_net is None:
            continue
        delta = outcome[1] - real_net
        deltas.append(delta)
        worse += delta < 0
        if record in observable_pl_negative:
            improved_negatives += delta > 0
            avoided += outcome[1] >= 0
    censored = sum(bool(item.get("pl_shadow_censored_by_real_exit")) for item in records)
    print(
        f"reduced negative real PROFIT_LOCK outcomes | "
        f"{'yes' if improved_negatives else 'no observed evidence'} "
        f"({improved_negatives}/{len(observable_pl_negative)} improved)"
    )
    print(
        f"negative PROFIT_LOCK avoided | {avoided}/{len(observable_pl_negative)} observable negatives"
    )
    print(f"estimated net total change | {_fmt_signed(_sum(deltas))}")
    print(f"evident adverse cases | {worse} observable trades with lower estimated net")
    if not observable:
        evidence = "INSUFFICIENT: no uncensored shadow close was recorded"
    elif len(observable) < 20 or censored > len(observable):
        evidence = (
            f"INSUFFICIENT: observable sample={len(observable)}, censored={censored}; "
            "keep shadow running before promotion"
        )
    else:
        evidence = (
            f"REVIEWABLE: observable sample={len(observable)}, censored={censored}; "
            "also inspect adverse cases and execution slippage before promotion"
        )
    print(f"promotion evidence | {evidence}")


def _period(records: list[dict[str, Any]]) -> str:
    timestamps: list[datetime] = []
    for record in records:
        for field in ("opened_at", "closed_at"):
            value = _parse_ts(record.get(field))
            if value is not None:
                timestamps.append(value)
    if not timestamps:
        return "n/a"
    return f"{min(timestamps).isoformat()} to {max(timestamps).isoformat()}"


def _counts(values: Counter[str]) -> str:
    if not values:
        return "none"
    return " | ".join(f"{key}={values[key]}" for key in ("PL1", "PL2", "PL3", "UNKNOWN") if values[key])


def _number(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _sum(values: Any) -> float:
    return sum(value for value in values if value is not None)


def _average(values: Any) -> float | None:
    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def _fmt(value: float | None, decimals: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{decimals}f}"


def _fmt_signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def _pct(count: int, total: int) -> str:
    return f"{(count / total * 100) if total else 0:.1f}%"


if __name__ == "__main__":
    main()
