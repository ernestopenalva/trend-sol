"""Read-only follow-up diagnostics for the validated REAL_A A/B/C ladder triage."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Iterable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from tools.real_a_exit_ladder_triage import ARMS, Outcome, _arm_configs, _fmt_pf, _fmt_ts, _parse_timestamp, _summary, _validate_baseline, simulate
from tools.real_a_exit_simulator import iter_aggtrade_files, load_real_a_seeds


SPLIT_AT = datetime.fromisoformat("2026-08-22T22:06:00-03:00").astimezone(timezone.utc)
SLOT_LIMIT = 5


def main() -> None:
    args = _parse_args()
    with args.config.open(encoding="utf-8") as handle:
        config = effective_config(yaml.safe_load(handle) or {})
    seeds = load_real_a_seeds(args.ledger, _parse_timestamp(args.since))
    if args.closed_until:
        cutoff = _parse_timestamp(args.closed_until)
        seeds = [seed for seed in seeds if seed.ledger_closed_at <= cutoff]
    outcomes, end_at = simulate(seeds, iter_aggtrade_files(args.aggtrades), _arm_configs(config), args.max_gap_seconds)
    if _validate_baseline(outcomes["A"]) < 0.95:
        raise SystemExit("FOLLOW-UP NOT RUN: baseline A agreement below 95%.")

    print("\nFOLLOW-UP DIAGNOSTICS — read-only; no entries were admitted or discarded.")
    print(f"Window end: {_fmt_ts(end_at)} | slot limit used only for diagnosis: {SLOT_LIMIT}")
    _print_capacity(outcomes, seeds)
    _print_regimes(outcomes, end_at)


def _print_capacity(outcomes: dict[str, dict[str, Outcome]], seeds: Iterable) -> None:
    print("\n1) CAPACITY / SLOT OCCUPANCY AT EACH REAL_A ENTRY")
    for arm in ("B", "C"):
        rows: list[tuple[object, int]] = []
        for seed in sorted(seeds, key=lambda item: item.opened_at):
            open_count = sum(
                other.opened_at < seed.opened_at
                and (outcomes[arm][other.pair_id].trigger_at is None or outcomes[arm][other.pair_id].trigger_at > seed.opened_at)
                for other in seeds
            )
            rows.append((seed, open_count))
        buckets = Counter(_bucket(count) for _seed, count in rows)
        conflicts = [(seed, count) for seed, count in rows if count >= SLOT_LIMIT]
        print(f"\nARM {arm}")
        print("simultaneous at entry | 0 | 1 | 2 | 3 | 4 | 5+")
        print("count                 | " + " | ".join(str(buckets[label]) for label in ("0", "1", "2", "3", "4", "5+")))
        print(f"max simultaneous: {max((count for _seed, count in rows), default=0)}")
        print(f"real entries with {SLOT_LIMIT}+ positions open: {len(conflicts)}")
        _print_conflicts(arm, conflicts, outcomes["A"])


def _bucket(value: int) -> str:
    return "5+" if value >= SLOT_LIMIT else str(value)


def _print_conflicts(arm: str, conflicts: list[tuple[object, int]], baseline: dict[str, Outcome]) -> None:
    if not conflicts:
        print("capacity conflicts: none")
        return
    print("timestamp | trade id | open positions | real A destination | A gross | A net")
    selected: list[Outcome] = []
    for seed, count in conflicts:
        item = baseline[seed.pair_id]
        selected.append(item)
        print(f"{_fmt_ts(seed.opened_at)} | {seed.pair_id} | {count} | {item.reason or 'OPEN'} | {_pct(item.gross_pct)} | {_pct(item.net_pct)}")
    closed = [item for item in selected if item.status == "CLOSED"]
    reasons = Counter(item.reason for item in closed)
    print("aggregate by A destination: " + ", ".join(f"{reason}={reasons[reason]}" for reason in ("HARD_STOP", "BREAKEVEN", "PROFIT_LOCK", "TRAILING")))
    print(f"aggregate A gross/net (closed only): {sum(item.gross_pct or 0 for item in closed):+.3f}% / {sum(item.net_pct or 0 for item in closed):+.3f}%")


def _print_regimes(outcomes: dict[str, dict[str, Outcome]], end_at: datetime) -> None:
    print("\n2) REGIMES (attributed by exit time; positions remain live across the split)")
    periods = (
        ("19/08 01:05 → 22/08 22:06 BRT", _parse_timestamp("2026-08-19T01:05:00-03:00"), SPLIT_AT),
        ("22/08 22:06 BRT → window end", SPLIT_AT, end_at),
    )
    for label, start, stop in periods:
        print(f"\n{label}")
        print("ARM | closed | open at end | gross total | gross/trade | net total | net/trade | PF | HS | BE | PL | TRAIL | age mean/median min")
        summaries = {}
        for arm in ARMS:
            all_rows = list(outcomes[arm].values())
            closed = [item for item in all_rows if item.status == "CLOSED" and item.trigger_at is not None and start <= item.trigger_at < stop]
            open_at_end = sum(item.seed.opened_at < stop and (item.trigger_at is None or item.trigger_at >= stop) for item in all_rows)
            summary = _summary(closed)
            summaries[arm] = summary
            print(
                f"{arm} | {summary['closed']} | {open_at_end} | {summary['gross']:+.3f}% | {summary['gross_trade']:+.3f}% | "
                f"{summary['net']:+.3f}% | {summary['net_trade']:+.3f}% | {_fmt_pf(summary['pf'])} | "
                f"{summary['HARD_STOP']} | {summary['BREAKEVEN']} | {summary['PROFIT_LOCK']} | {summary['TRAILING']} | "
                f"{summary['age_mean']:.1f}/{summary['age_median']:.1f}"
            )
        a_net_trade = float(summaries["A"]["net_trade"])
        print(f"net/trade delta vs A: B-A={float(summaries['B']['net_trade']) - a_net_trade:+.3f} pp | C-A={float(summaries['C']['net_trade']) - a_net_trade:+.3f} pp")


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only capacity and regime follow-up for REAL_A A/B/C ladder triage.")
    parser.add_argument("--aggtrades", type=Path, nargs="+", required=True)
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "config.yaml")
    parser.add_argument("--since", default="2026-08-19T01:05:00-03:00")
    parser.add_argument("--closed-until", help="Use the identical frozen seed set as the prior A/B/C run.")
    parser.add_argument("--max-gap-seconds", type=float, default=30.0)
    return parser.parse_args()


if __name__ == "__main__":
    main()
