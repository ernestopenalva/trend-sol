"""Read-only structural audit of the historical v1.4 PL ladder versus BE."""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.position.bot_full_engine import BotFullExitPosition
from tools.cohort_study import _load_config
from tools.real_a_be_pl1_order_study import _historical_exit_config
from tools.real_a_exit_simulator import _NoopClient, _NoopLogger, _as_float, _parse_timestamp

TIMING_TOLERANCE_SECONDS = 5.0
EQUALITY_ABS_TOLERANCE = 1e-10


@dataclass(frozen=True)
class LadderGeometry:
    be_stop: float
    be_activation: float
    pl_step: str
    pl_effective_stop: float
    pl_effective_trigger: float


@dataclass(frozen=True)
class Row:
    pair_id: str
    cohort: str
    opened_at: datetime
    entry: float
    atr: float
    gross_pct: float | None
    geometry: LadderGeometry

    @property
    def difference(self) -> float:
        return self.geometry.pl_effective_stop - self.geometry.be_stop

    @property
    def classification(self) -> str:
        if math.isclose(self.geometry.pl_effective_stop, self.geometry.be_stop, abs_tol=EQUALITY_ABS_TOLERANCE):
            return "PL_STOP_EQUALS_BE"
        return "PL_STOP_ABOVE_BE" if self.difference > 0 else "PL_STOP_BELOW_BE"


def main() -> None:
    args = _args()
    config = effective_config(_load_config(args.config))
    start, end = _timestamp(args.since), _timestamp(args.until)
    ledger = _load_ledger(args.ledger, start, end)
    post_be_pairs = _load_post_be_pairs(args.details)
    cohort_a = [_row(ledger[pair_id], config, "A: 89 post-BE PL1") for pair_id in post_be_pairs if pair_id in ledger]
    if len(cohort_a) != len(post_be_pairs):
        raise ValueError(f"Missing ledger rows for {len(post_be_pairs) - len(cohort_a)} post-BE detail rows.")
    profit_lock_records = [record for record in ledger.values() if str(record.get("exit_reason")) == "PROFIT_LOCK"]
    missing_step = [record for record in profit_lock_records if _step_index(record.get("profit_lock_step")) is None]
    cohort_b = [_row(record, config, "B: real PROFIT_LOCK", _step_index(record.get("profit_lock_step")) or 1)
                for record in profit_lock_records if _step_index(record.get("profit_lock_step")) is not None]
    _print_report(cohort_a, cohort_b, len(missing_step))
    if args.output:
        _write_csv(args.output, [*cohort_a, *cohort_b])
        print(f"detailed CSV: {args.output}")


def _load_post_be_pairs(path: Path) -> list[str]:
    pairs: list[str] = []
    with path.open(encoding="utf-8", newline="") as handle:
        for record in csv.DictReader(handle):
            if record.get("strategy_version") != "b_atr_v1.4" or record.get("ledger_hard_stop_pct") != "1.50000000":
                continue
            if record.get("outcome") != "PL1_FIRST":
                continue
            resolved, closed = _parse_brt(record.get("resolved_brt")), _parse_brt(record.get("closed_brt"))
            if resolved is not None and closed is not None and (resolved - closed).total_seconds() > TIMING_TOLERANCE_SECONDS:
                pairs.append(str(record["pair_id"]))
    return pairs


def _load_ledger(path: Path, start: datetime, end: datetime) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("phantom") or row.get("shadow_kind"):
                continue
            if row.get("position_type") != "BOT_EXIT" or row.get("strategy_version") != "b_atr_v1.4":
                continue
            if not math.isclose(_as_float(row.get("hard_stop_pct")) or 0.0, 1.5):
                continue
            opened = _parse_timestamp(row.get("opened_at"))
            if opened is not None and start <= opened < end:
                selected[str(row.get("pair_id"))] = row
    return selected


def _row(record: dict[str, Any], config: dict[str, Any], cohort: str, step_index: int = 1) -> Row:
    opened = _parse_timestamp(record.get("opened_at")); entry = _as_float(record.get("entry_price")); atr = _as_float(record.get("entry_atr"))
    if opened is None or entry is None or atr is None or entry <= 0 or atr <= 0:
        raise ValueError(f"Incomplete ladder fields for {record.get('pair_id')}")
    position = BotFullExitPosition(
        pair_id=str(record["pair_id"]), symbol="SOLUSDT", entry_price=entry, quantity=1.0, entry_order={},
        open_ts=opened.isoformat(), config=_historical_exit_config(config, "b_atr_v1.4"), client=_NoopClient(),
        logger=_NoopLogger(), entry_atr=atr, atr_timeframe="1m", atr_period=14,
    )
    be_plan = position._breakeven_plan()
    plans = position._profit_lock_candidates(999.0, 999.0)
    if be_plan is None or len(plans) < step_index:
        raise ValueError(f"Could not derive v1.4 BE/PL geometry for {record.get('pair_id')}")
    plan = plans[step_index - 1]
    geometry = LadderGeometry(
        float(be_plan["be_stop"]), float(be_plan["be_activation_price"]), f"PL{step_index}",
        float(plan["effective_stop"]), float(plan["effective_trigger"]),
    )
    return Row(str(record["pair_id"]), cohort, opened, entry, atr, _as_float(record.get("gross_pnl_pct")), geometry)


def _step_index(value: Any) -> int | None:
    text = str(value or "").upper()
    if text.startswith("PL") and text[2:].isdigit():
        return int(text[2:])
    return None


def _print_report(a: list[Row], b: list[Row], missing_profit_lock_step: int) -> None:
    print("REAL_A v1.4 PL1 / BE STRUCTURAL AUDIT | READ-ONLY")
    print(f"Cohort A selection: PL1_FIRST more than {TIMING_TOLERANCE_SECONDS:g}s after a real BREAKEVEN close; no aggTrades are read in this audit.")
    print("\nFORMULA — historical v1.4 code path")
    print("BE stop = max(entry + 0.1*ATR, entry*(1 + (2*taker_fee + BE_margin)/100))")
    print("BE activation = max(entry + 3*ATR, BE stop + 0.5*ATR)")
    print("PL1 raw stop = entry + 1.5*ATR")
    print("PL1 effective stop = max(PL1 raw stop, entry*(1 + (2*taker_fee + PL_margin)/100))")
    print("PL1 activation = PL1 effective stop + (5 - 1.5)*ATR")
    print("With v1.4 fees=0.10%, BE_margin=PL_margin=0.05%, both economic floors equal entry*1.0025. Therefore PL1 stop equals BE stop whenever both are absorbed by that same floor; in particular when 1.5*ATR <= 0.0025*entry.")
    _print_cohort(a, include_gross=False)
    _print_cohort(b, include_gross=True)
    if missing_profit_lock_step:
        print(f"real PROFIT_LOCK omitted from geometry classification due missing persisted profit_lock_step: {missing_profit_lock_step}")
    above = sum(row.classification == "PL_STOP_ABOVE_BE" for row in a)
    equal = sum(row.classification == "PL_STOP_EQUALS_BE" for row in a)
    below = sum(row.classification == "PL_STOP_BELOW_BE" for row in a)
    print("\nIMPACT ON 89/18")
    print(f"Of {len(a)} post-BE PL1 touches: {above} raised the stop above BE; {equal} only armed PL1 at the same economic stop; {below} were below BE.")


def _print_cohort(rows: list[Row], *, include_gross: bool) -> None:
    label = rows[0].cohort if rows else "empty cohort"
    groups = {name: [row for row in rows if row.classification == name] for name in ("PL_STOP_ABOVE_BE", "PL_STOP_EQUALS_BE", "PL_STOP_BELOW_BE")}
    print(f"\nCOHORT | {label} | trades={len(rows)}")
    print("class | count | % | diff price mean/median/min/max | diff % mean/median/min/max | diff ATR mean/median/min/max")
    for name, group in groups.items():
        print(f"{name} | {len(group)} | {_pct_count(len(group), len(rows))} | {_summary(group, 'price')} | {_summary(group, 'pct')} | {_summary(group, 'atr')}")
    if include_gross:
        print("real PROFIT_LOCK gross by class | class | count | mean | median | min | max")
        for name, group in groups.items():
            gross = [row.gross_pct for row in group if row.gross_pct is not None]
            if gross:
                print(f"gross | {name} | {len(gross)} | {statistics.fmean(gross):+.6f}% | {statistics.median(gross):+.6f}% | {min(gross):+.6f}% | {max(gross):+.6f}%")
            else:
                print(f"gross | {name} | 0 | n/a | n/a | n/a | n/a")


def _summary(rows: list[Row], unit: str) -> str:
    if not rows:
        return "n/a"
    values = []
    for row in rows:
        if unit == "price": values.append(row.difference)
        elif unit == "pct": values.append(row.difference / row.entry * 100)
        else: values.append(row.difference / row.atr)
    suffix = "%" if unit == "pct" else ""
    return "{:+.8f}{} / {:+.8f}{} / {:+.8f}{} / {:+.8f}{}".format(statistics.fmean(values), suffix, statistics.median(values), suffix, min(values), suffix, max(values), suffix)


def _pct_count(count: int, total: int) -> str:
    return f"{count / total * 100:.2f}%" if total else "0.00%"


def _timestamp(value: str) -> datetime:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"Invalid timestamp: {value}")
    return parsed


def _parse_brt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d/%m/%Y %H:%M:%S BRT").replace(tzinfo=BRASILIA_TZ).astimezone(_timestamp("2026-01-01T00:00:00+00:00").tzinfo)
    except ValueError:
        return None


def _write_csv(path: Path, rows: list[Row]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("cohort", "pair_id", "opened_brt", "step", "entry", "entry_atr", "be_stop", "be_activation", "pl_effective_stop", "pl_effective_trigger", "classification", "difference_price", "difference_pct", "difference_atr", "gross_pct"))
        for row in rows:
            writer.writerow((row.cohort, row.pair_id, row.opened_at.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S BRT"), row.geometry.pl_step, row.entry, row.atr, row.geometry.be_stop, row.geometry.be_activation, row.geometry.pl_effective_stop, row.geometry.pl_effective_trigger, row.classification, row.difference, row.difference / row.entry * 100, row.difference / row.atr, row.gross_pct))


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only v1.4 PL stop versus BE structural audit.")
    parser.add_argument("--details", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "real_a_be_pl1_order_details.csv")
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "config.yaml")
    parser.add_argument("--since", default="2026-08-01T00:00:00-03:00")
    parser.add_argument("--until", default="2026-08-26T00:00:00-03:00")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "real_a_pl1_be_structure_audit.csv")
    return parser.parse_args()


if __name__ == "__main__":
    main()
