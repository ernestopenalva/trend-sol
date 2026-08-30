"""Read-only audit of v1.4 BE records whose reconstructed PL1 precedes real BE.

This intentionally consumes the existing first-touch CSV and the already
downloaded aggTrades.  It never repeats the 122-record cohort study, downloads
market data, or changes any production component.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from tools.cohort_study import _load_config
from tools.real_a_be_pl1_order_study import BeSeed, _state
from tools.real_a_exit_simulator import Tick, _as_float, _parse_timestamp, iter_aggtrade_files

TIMING_TOLERANCE_SECONDS = 5.0
BE_ARMED_AT_TELEMETRY_INTRODUCED = datetime.fromisoformat("2026-08-15T21:52:03+00:00")


@dataclass
class AuditCase:
    seed: BeSeed
    ledger: dict[str, Any]
    effective_pl1: float
    be_activation_price: float | None
    be_stop: float | None
    pl1_touched_at: datetime | None = None
    be_arm_touched_at: datetime | None = None
    be_stop_after_pl_at: datetime | None = None


def main() -> None:
    args = _args()
    cases = _load_cases(args.details, args.ledger)
    if not cases:
        raise SystemExit("No v1.4 / HS 1.5% pre-BE PL1 records found in the supplied details CSV.")
    config = effective_config(_load_config(args.config))
    for case in cases:
        state = _state(case.seed, config)
        case.effective_pl1 = state.pl1_price
    _observe_ticks(cases, iter_aggtrade_files(args.aggtrades))
    _print_report(cases)
    if args.output:
        _write_csv(args.output, cases)
        print(f"detailed CSV: {args.output}")


def _load_cases(details_path: Path, ledger_path: Path) -> list[AuditCase]:
    selected: set[str] = set()
    with details_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("strategy_version") != "b_atr_v1.4" or row.get("ledger_hard_stop_pct") != "1.50000000":
                continue
            if row.get("outcome") != "PL1_FIRST":
                continue
            resolved, closed = _parse_brt(row.get("resolved_brt")), _parse_brt(row.get("closed_brt"))
            if resolved is not None and closed is not None and (resolved - closed).total_seconds() < -TIMING_TOLERANCE_SECONDS:
                selected.add(str(row["pair_id"]))
    records: dict[str, dict[str, Any]] = {}
    with ledger_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and str(row.get("pair_id")) in selected:
                records[str(row["pair_id"])] = row
    missing = selected - records.keys()
    if missing:
        raise ValueError(f"Missing selected ledger records: {', '.join(sorted(missing))}")
    cases: list[AuditCase] = []
    for pair_id in selected:
        row = records[pair_id]
        seed = _seed_from_record(row)
        cases.append(AuditCase(seed, row, 0.0, _as_float(row.get("be_activation_price")), _as_float(row.get("be_stop"))))
    return sorted(cases, key=lambda case: case.seed.opened_at)


def _seed_from_record(row: dict[str, Any]) -> BeSeed:
    values = {
        "opened": _parse_timestamp(row.get("opened_at")), "closed": _parse_timestamp(row.get("closed_at")),
        "entry": _as_float(row.get("entry_price")), "atr": _as_float(row.get("entry_atr")),
        "peak": _as_float(row.get("peak_price")), "trough": _as_float(row.get("trough_price")),
        "hard_stop": _as_float(row.get("hard_stop_price")),
    }
    if any(value is None for value in values.values()):
        raise ValueError(f"Incomplete ledger fields for {row.get('pair_id')}")
    return BeSeed(
        str(row["pair_id"]), values["opened"], values["closed"], values["entry"], values["atr"], values["peak"], values["trough"],
        str(row.get("strategy_version")), str(row.get("profile") or "unknown"), values["hard_stop"], _as_float(row.get("hard_stop_pct")),
        str(row["pl_shadow_step"]) if row.get("pl_shadow_step") else None, _as_float(row.get("pl_shadow_activation_price")),
    )


def _observe_ticks(cases: list[AuditCase], ticks: Iterable[Tick]) -> None:
    pending = iter(cases)
    next_case = next(pending, None)
    active: list[AuditCase] = []
    processed = 0
    for tick in ticks:
        processed += 1
        while next_case is not None and next_case.seed.opened_at <= tick.timestamp:
            active.append(next_case)
            next_case = next(pending, None)
        for case in active:
            if case.be_arm_touched_at is None and case.be_activation_price is not None and tick.price >= case.be_activation_price:
                case.be_arm_touched_at = tick.timestamp
            if case.pl1_touched_at is None and tick.price >= case.effective_pl1:
                case.pl1_touched_at = tick.timestamp
            if (
                case.pl1_touched_at is not None and case.be_stop_after_pl_at is None
                and case.be_stop is not None and tick.timestamp >= case.pl1_touched_at and tick.price <= case.be_stop
            ):
                case.be_stop_after_pl_at = tick.timestamp
        if processed % 100_000 == 0:
            print(f"tick progress: {processed} | selected={len(cases)}", flush=True)


def _print_report(cases: list[AuditCase]) -> None:
    print("REAL_A v1.4 PRE-BE PL1 AUDIT | LEDGER + EXISTING AGGTRADES | READ-ONLY")
    print(f"selected cases: {len(cases)} (PL1_FIRST more than {TIMING_TOLERANCE_SECONDS:g}s before real BREAKEVEN close)")
    print("BE_ARMED_AT TELEMETRY: field was introduced in the code path on 15/08/2026 18:52:03 BRT; a missing value before that deployment is telemetry absence, not evidence that BE was not armed.")
    print("\npair_id | opened BRT | PL1 before BE | PL1-BE stop % | PL1-BE stop ATR | BE arm-BE stop % | BE arm-BE stop ATR | be_armed_at | persisted PL compatible | tick sequence")
    for case in cases:
        print("{} | {} | {} | {} | {} | {} | {} | {} | {} | {}".format(
            case.seed.pair_id, _brt(case.seed.opened_at), _seconds_label(case.pl1_touched_at, case.seed.closed_at),
            _distance_pct(case.effective_pl1, case.be_stop, case.seed.entry_price), _distance_atr(case.effective_pl1, case.be_stop, case.seed.entry_atr),
            _distance_pct(case.be_activation_price, case.be_stop, case.seed.entry_price), _distance_atr(case.be_activation_price, case.be_stop, case.seed.entry_atr),
            case.ledger.get("be_armed_at") or "NONE", _pl_compatibility(case), _sequence(case),
        ))
    _print_geometry_summary(cases)
    _print_persisted_pl_summary(cases)


def _print_geometry_summary(cases: list[AuditCase]) -> None:
    measures = {
        "PL1 -> BE stop price": [_difference(case.effective_pl1, case.be_stop) for case in cases],
        "PL1 -> BE stop %": [_distance_float_pct(case.effective_pl1, case.be_stop, case.seed.entry_price) for case in cases],
        "PL1 -> BE stop ATR": [_distance_float_atr(case.effective_pl1, case.be_stop, case.seed.entry_atr) for case in cases],
        "BE activation -> BE stop price": [_difference(case.be_activation_price, case.be_stop) for case in cases],
        "BE activation -> BE stop %": [_distance_float_pct(case.be_activation_price, case.be_stop, case.seed.entry_price) for case in cases],
        "BE activation -> BE stop ATR": [_distance_float_atr(case.be_activation_price, case.be_stop, case.seed.entry_atr) for case in cases],
    }
    print("\nRAW GEOMETRY SUMMARY — no compressed/distant threshold is imposed")
    for label, values in measures.items():
        usable = [value for value in values if value is not None]
        print(f"{label} | median={statistics.median(usable):.8f} | mean={statistics.fmean(usable):.8f} | min={min(usable):.8f} | max={max(usable):.8f}" if usable else f"{label} | unavailable")


def _print_persisted_pl_summary(cases: list[AuditCase]) -> None:
    available = [case for case in cases if _as_float(case.ledger.get("profit_lock_effective_trigger")) is not None]
    print("\nPERSISTED PL FIELDS")
    print(f"profit_lock_effective_trigger present: {len(available)}/{len(cases)}")
    if available:
        errors = [abs(float(case.ledger["profit_lock_effective_trigger"]) - case.effective_pl1) for case in available]
        print("effective trigger abs error | mean={:.10f} | max={:.10f}".format(statistics.fmean(errors), max(errors)))
    print("A missing persisted PL field means PL was not recorded as armed in the final ledger record; it cannot by itself distinguish an operational miss from a prior configuration difference.")


def _sequence(case: AuditCase) -> str:
    events = [(case.be_arm_touched_at, "BE_ARM"), (case.pl1_touched_at, "PL1_TOUCH"), (case.be_stop_after_pl_at, "BE_STOP_TOUCH"), (case.seed.closed_at, "CLOSED_BE")]
    return " -> ".join(name for timestamp, name in sorted((item for item in events if item[0] is not None), key=lambda item: item[0]))


def _pl_compatibility(case: AuditCase) -> str:
    persisted = _as_float(case.ledger.get("profit_lock_effective_trigger"))
    if persisted is None:
        return "MISSING"
    return "MATCH" if abs(persisted - case.effective_pl1) < 1e-8 else "DIFFERS"


def _difference(top: float | None, bottom: float | None) -> float | None:
    return None if top is None or bottom is None else top - bottom


def _distance_float_pct(top: float | None, bottom: float | None, entry: float) -> float | None:
    difference = _difference(top, bottom)
    return None if difference is None else difference / entry * 100


def _distance_float_atr(top: float | None, bottom: float | None, atr: float) -> float | None:
    difference = _difference(top, bottom)
    return None if difference is None else difference / atr


def _distance_pct(top: float | None, bottom: float | None, entry: float) -> str:
    value = _distance_float_pct(top, bottom, entry)
    return "" if value is None else f"{value:+.6f}%"


def _distance_atr(top: float | None, bottom: float | None, atr: float) -> str:
    value = _distance_float_atr(top, bottom, atr)
    return "" if value is None else f"{value:+.6f}"


def _seconds_label(left: datetime | None, right: datetime) -> str:
    return "MISSING" if left is None else f"{(right - left).total_seconds():.3f}s"


def _parse_brt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y %H:%M:%S BRT").replace(tzinfo=BRASILIA_TZ).astimezone(_parse_timestamp("2026-01-01T00:00:00+00:00").tzinfo)
    except ValueError:
        return None


def _brt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S BRT")


def _write_csv(path: Path, cases: list[AuditCase]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("pair_id", "opened_brt", "closed_brt", "entry", "entry_atr", "hard_stop_pct", "hard_stop_price", "be_activation_price", "be_stop", "be_armed_at", "exit_trigger_price", "profit_lock_raw_trigger", "profit_lock_effective_trigger", "profit_lock_step", "profit_lock_action", "effective_pl1", "pl1_touched_brt", "seconds_pl1_before_close", "be_arm_touched_brt", "be_stop_after_pl_brt", "pl1_minus_be_stop_price", "pl1_minus_be_stop_pct", "pl1_minus_be_stop_atr", "be_activation_minus_be_stop_price", "be_activation_minus_be_stop_pct", "be_activation_minus_be_stop_atr", "persisted_pl_compatible", "sequence"))
        for case in cases:
            row = case.ledger
            writer.writerow((case.seed.pair_id, _brt(case.seed.opened_at), _brt(case.seed.closed_at), case.seed.entry_price, case.seed.entry_atr, case.seed.hard_stop_pct, case.seed.hard_stop_price, case.be_activation_price, case.be_stop, row.get("be_armed_at"), row.get("exit_trigger_price"), row.get("profit_lock_raw_trigger"), row.get("profit_lock_effective_trigger"), row.get("profit_lock_step"), row.get("profit_lock_action"), case.effective_pl1, _brt(case.pl1_touched_at) if case.pl1_touched_at else "", _seconds_label(case.pl1_touched_at, case.seed.closed_at), _brt(case.be_arm_touched_at) if case.be_arm_touched_at else "", _brt(case.be_stop_after_pl_at) if case.be_stop_after_pl_at else "", _difference(case.effective_pl1, case.be_stop), _distance_float_pct(case.effective_pl1, case.be_stop, case.seed.entry_price), _distance_float_atr(case.effective_pl1, case.be_stop, case.seed.entry_atr), _difference(case.be_activation_price, case.be_stop), _distance_float_pct(case.be_activation_price, case.be_stop, case.seed.entry_price), _distance_float_atr(case.be_activation_price, case.be_stop, case.seed.entry_atr), _pl_compatibility(case), _sequence(case)))


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only audit of v1.4 PL1 touches preceding a real REAL_A BREAKEVEN close.")
    parser.add_argument("--details", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "real_a_be_pl1_order_details.csv")
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "config.yaml")
    parser.add_argument("--aggtrades", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "real_a_be_pl1_pre_be_audit.csv")
    return parser.parse_args()


if __name__ == "__main__":
    main()
