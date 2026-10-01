"""Compact forward report for the 2026-09-27 BE_OFF_CB experiment cohort."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.console_utils import BRASILIA_TZ


COHORT_STARTED_TEXT = "27/09/2026 20:07:29"
COMPARABILITY_FLOOR_TEXT = "28/09/2026 01:47:00"
# Kept as a compatibility alias for imports made by earlier versions/tests.
DEFAULT_SINCE_TEXT = COHORT_STARTED_TEXT
SUMMARY_HEADER = "arm | closed | open | net | net $/trade | PF | DD $ | HS | PL | TRAIL | median age | max simultaneous"
ADMISSION_CUTOFF = ContextVar('admission_cutoff', default=None)


@dataclass(frozen=True)
class Arm:
    name: str
    ledger: str
    state: str
    events: str


@dataclass(frozen=True)
class ComparableWindow:
    cohort_started: datetime
    comparable_since: datetime | None
    observed_at: datetime
    pending_reason: str | None = None


CONTROL = Arm("BE_OFF_CB_SHADOW", "data/trades/trades_be_off_cb_shadow.jsonl",
              "data/state/be_off_cb_shadow.json", "data/telemetry/be_off_cb_shadow_events.jsonl")
REAL_A = Arm('REAL_A', 'data/trades/trades_B.jsonl', 'data/state/open_positions.json', '')
DMI15_CONTEXT = Arm('DMI15_TRAJECTORY_CONTEXT_SHADOW',
                    'data/trades/trades_dmi15_trajectory_context_shadow.jsonl',
                    'data/state/dmi15_trajectory_context_shadow.json', '')
EXPERIMENTS = {
    'ema_macd_hist_1m': Arm('EMA_MACD_HIST_1M_SHADOW', 'data/trades/trades_ema_macd_hist_1m_shadow.jsonl',
                           'data/state/ema_macd_hist_1m_shadow.json', 'data/telemetry/ema_macd_hist_1m_shadow_events.jsonl'),
    'fast_drop': Arm('BE_OFF_CB_FAST_DROP_EMA_SHADOW', 'data/trades/trades_be_off_cb_fast_drop_ema_shadow.jsonl',
                     'data/state/be_off_cb_fast_drop_ema_shadow.json', 'data/telemetry/be_off_cb_fast_drop_ema_shadow_events.jsonl'),
    "hs_bull": Arm("HS_BULL_ELASTIC_SHADOW", "data/trades/trades_hs_bull_elastic_shadow.jsonl",
                   "data/state/hs_bull_elastic_shadow.json", "data/telemetry/hs_bull_elastic_shadow_events.jsonl"),
    "hs_bear": Arm("HS_BEAR_CLUSTER_EXIT_SHADOW", "data/trades/trades_hs_bear_cluster_exit_shadow.jsonl",
                   "data/state/hs_bear_cluster_exit_shadow.json", "data/telemetry/hs_bear_cluster_exit_shadow_events.jsonl"),
    "cb_exit": Arm("CB_EXIT_ALL_SHADOW", "data/trades/trades_cb_exit_all_shadow.jsonl",
                   "data/state/cb_exit_all_shadow.json", "data/telemetry/cb_exit_all_shadow_events.jsonl"),
    "macd_bu_minus": Arm("BE_OFF_CB_MACD_BU_MINUS_SHADOW", "data/trades/trades_be_off_cb_macd_bu_minus_shadow.jsonl",
                         "data/state/be_off_cb_macd_bu_minus_shadow.json", "data/telemetry/be_off_cb_macd_bu_minus_shadow_events.jsonl"),
    "ema_macd": Arm("BE_OFF_CB_EMA_MACD_SHADOW", "data/trades/trades_be_off_cb_ema_macd_shadow.jsonl",
                    "data/state/be_off_cb_ema_macd_shadow.json", "data/telemetry/be_off_cb_ema_macd_shadow_events.jsonl"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", choices=tuple(EXPERIMENTS))
    parser.add_argument('--since', help='Admission cutoff in BRT: DD/MM/YYYY HH:MM[:SS]')
    parser.add_argument("--list-accepted", action="store_true",
                        help="List accepted trades for ema_macd or macd_bu_minus")
    args = parser.parse_args()
    cutoff = None
    if args.since is not None:
        for fmt in ('%d/%m/%Y %H:%M', '%d/%m/%Y %H:%M:%S'):
            try:
                cutoff = datetime.strptime(args.since, fmt).replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
                break
            except ValueError:
                pass
        if cutoff is None:
            parser.error('--since must use DD/MM/YYYY HH:MM or DD/MM/YYYY HH:MM:SS (BRT)')
    token = ADMISSION_CUTOFF.set(cutoff)
    try:
        _run_report(args, cutoff)
    finally:
        ADMISSION_CUTOFF.reset(token)


def _run_report(args, cutoff):
    cohort_started = parse_time(COHORT_STARTED_TEXT)
    floor = parse_time(COMPARABILITY_FLOOR_TEXT)
    if cohort_started is None or floor is None:  # pragma: no cover - constants are tested
        raise SystemExit("invalid report window constants")
    window = (ComparableWindow(cohort_started, cutoff, datetime.now(timezone.utc)) if cutoff is not None
              else determine_comparable_window(cohort_started, floor))
    if args.list_accepted and args.experiment not in {"ema_macd", "macd_bu_minus"}:
        raise SystemExit("--list-accepted is only valid with --experiment ema_macd or macd_bu_minus")
    if cutoff is None:
        _print_window(window)
    else:
        print(f'FORWARD EXPERIMENT REPORT | since {_fmt(cutoff)}')
    if window.comparable_since is None:
        print("\nCOMPARABLE WINDOW | PENDING")
        print(f"reason | {window.pending_reason}")
        print("comparative metrics | N/A (warm-up is excluded)")
        if args.experiment:
            _print_accepted(EXPERIMENTS[args.experiment], window.cohort_started, window.observed_at)
        return
    since = window.comparable_since
    if args.experiment is None:
        print_summary(since)
        return
    arm = EXPERIMENTS[args.experiment]
    _validate_experiment_cohort(arm, since)
    if args.experiment not in ('macd_bu_minus', 'ema_macd'):
        _comparison(CONTROL, arm, since)
    if args.experiment == "macd_bu_minus":
        print_macd_bu_minus(arm, since)
    elif args.experiment == "ema_macd":
        print_ema_macd(arm, since, args.list_accepted)
    elif args.experiment == "hs_bull":
        print_hs_bull(arm, since)
    elif args.experiment == "hs_bear":
        print_forced_exit_detail(arm, since, "HS_BEAR_CLUSTER_TRIGGERED", "HS_BEAR_CLUSTER_EXIT")
    elif args.experiment == 'cb_exit':
        print_forced_exit_detail(arm, since, "CB_EXIT_ALL_TRIGGERED", "CIRCUIT_BREAKER_EXIT_ALL")
    elif args.experiment == 'ema_macd_hist_1m':
        events = [row for row in _events(arm, since) if row.get('event') == 'ADMISSION_FILTERS']
        print('\nopportunities')
        print(f'total oportunidades | {len(events)}')
        print(f"bloqueadas por EMA_MACD | {sum(not row['ema_macd_pass'] for row in events)}")
        print(f"bloqueadas adicionalmente por histogram | {sum(row['ema_macd_pass'] and not row['histogram_pass'] for row in events)}")
        print(f"bloqueadas adicionalmente por 1m | {sum(row['ema_macd_pass'] and row['histogram_pass'] and not row['confirmation_1m_pass'] for row in events)}")
        print(f"admitidas finais | {sum(row['final_decision'] == 'admitted' for row in events)}")
    _print_accepted(arm, since, window.observed_at)


def determine_comparable_window(
    cohort_started: datetime,
    floor: datetime,
    *,
    observed_at: datetime | None = None,
    arms: Iterable[Arm] | None = None,
) -> ComparableWindow:
    """Find the first recorded instant at/after the floor with no inherited state.

    A position is inherited when it was opened before the candidate instant and
    remains open at that instant.  The same rule is applied to recorded CB
    cooldown intervals.  We never infer a missing close: an unresolved position
    keeps the comparable window pending.
    """
    observed_at = (observed_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected = tuple(arms or (CONTROL, *EXPERIMENTS.values()))
    if observed_at < floor:
        carryovers = _unresolved_before(selected, floor)
        suffix = f"; {carryovers} pre-floor position(s) still open" if carryovers else ""
        return ComparableWindow(cohort_started, None, observed_at,
                                f"waiting for comparability floor {_fmt(floor)}{suffix}")

    intervals = _position_intervals(selected)
    cooldowns = _cooldown_intervals(selected)
    candidate, pending = first_comparable_instant(intervals, cooldowns, floor, observed_at)
    return ComparableWindow(cohort_started, candidate, observed_at, pending)


def first_comparable_instant(
    position_intervals: Iterable[tuple[datetime, datetime | None, str]],
    cooldown_intervals: Iterable[tuple[datetime, datetime | None, str]],
    floor: datetime,
    observed_at: datetime,
) -> tuple[datetime | None, str | None]:
    """Resolve the first flat, cooldown-free instant supported by observations."""
    intervals = list(position_intervals)
    cooldowns = list(cooldown_intervals)
    candidate = floor
    while True:
        position_blockers = [interval for interval in intervals if _covers_prior(interval, candidate)]
        cooldown_blockers = [interval for interval in cooldowns if _covers_prior(interval, candidate)]
        blockers = [*position_blockers, *cooldown_blockers]
        if not blockers:
            return candidate, None
        if any(end is None for _, end, _ in blockers):
            names = sorted({label for _, end, label in blockers if end is None})
            return None, "inherited position(s) still open: " + ", ".join(names)
        next_candidate = max(end for _, end, _ in blockers if end is not None)
        if next_candidate > observed_at:
            return None, f"inherited cooldown/position persists through {_fmt(next_candidate)}"
        if next_candidate <= candidate:  # defensive guard for malformed intervals
            return None, "unable to resolve inherited state interval"
        candidate = next_candidate


def _covers_prior(interval: tuple[datetime, datetime | None, str], instant: datetime) -> bool:
    start, end, _ = interval
    return start < instant and (end is None or end > instant)


def _position_intervals(arms: Iterable[Arm]) -> list[tuple[datetime, datetime | None, str]]:
    intervals: list[tuple[datetime, datetime | None, str]] = []
    closed_ids: set[tuple[str, str]] = set()
    for arm in arms:
        for row in _jsonl(ROOT / arm.ledger):
            opened = parse_time(row.get("opened_at") or row.get("open_ts"))
            closed = parse_time(row.get("closed_at") or row.get("close_ts"))
            if opened is None or closed is None:
                continue
            pair_id = str(row.get("pair_id") or "N/A")
            closed_ids.add((arm.name, pair_id))
            intervals.append((opened, closed, f"{arm.name}:{pair_id}"))
        for row in _state(arm).get("positions", []):
            if row.get("status") != "OPEN":
                continue
            pair_id = str(row.get("pair_id") or "N/A")
            if (arm.name, pair_id) in closed_ids:
                continue
            opened = parse_time(row.get("open_ts") or row.get("opened_at"))
            if opened is not None:
                intervals.append((opened, None, f"{arm.name}:{pair_id}"))
    return intervals


def _cooldown_intervals(arms: Iterable[Arm]) -> list[tuple[datetime, datetime | None, str]]:
    intervals: list[tuple[datetime, datetime | None, str]] = []
    for arm in arms:
        for event in _jsonl(ROOT / arm.events):
            if event.get("event") != "CIRCUIT_BREAKER_TRIGGERED":
                continue
            started = parse_time(event.get("trigger_time") or event.get("ts"))
            ended = parse_time(event.get("cooldown_until") or event.get("breaker_until"))
            if started is not None:
                intervals.append((started, ended, f"{arm.name}:CB"))
        state = _state(arm)
        if state.get("circuit_breaker_active"):
            started = parse_time(state.get("circuit_breaker_started_at"))
            ended = parse_time(state.get("circuit_breaker_until"))
            if started is not None:
                intervals.append((started, ended, f"{arm.name}:CB"))
    return intervals


def _unresolved_before(arms: Iterable[Arm], instant: datetime) -> int:
    return sum(1 for start, end, _ in _position_intervals(arms)
               if start < instant and (end is None or end > instant))


def _print_window(window: ComparableWindow) -> None:
    print("FORWARD EXPERIMENT COHORT")
    print(f"cohort_started | {_fmt(window.cohort_started)}")
    print(f"comparable_since | {_fmt(window.comparable_since) if window.comparable_since else 'PENDING'}")
    print(f"observed_at | {_fmt(window.observed_at)}")


def _print_warmup(window: ComparableWindow) -> None:
    end = window.comparable_since or window.observed_at
    print(f"\nWARM-UP / NON-COMPARABLE | {_fmt(window.cohort_started)} to {_fmt(end)}")
    print("arm | opportunities | opens | blocks | closed | currently open")
    for arm in (CONTROL, *EXPERIMENTS.values()):
        events = _events_between(arm, window.cohort_started, end)
        opportunities = sum(row.get("event") == "SIGNAL_OPPORTUNITY" for row in events)
        opens = sum(row.get("event") == "OPEN" for row in events)
        blocks = sum(str(row.get("event") or "").startswith("ENTRY_BLOCKED") for row in events)
        closed = len(_records_between(arm, window.cohort_started, end))
        current = len([row for row in _open_positions(_state(arm), window.cohort_started)
                       if (stamp := parse_time(row.get("open_ts") or row.get("opened_at"))) is not None and stamp < end])
        print(f"{arm.name} | {opportunities} | {opens} | {blocks} | {closed} | {current}")
    print("warm-up economics/pairing | excluded")


def _print_operational_warmup(experiment: str, arm: Arm, window: ComparableWindow) -> None:
    end = window.comparable_since or window.observed_at
    events = _events_between(arm, window.cohort_started, end)
    records = _records_between(arm, window.cohort_started, end)
    counts = operational_warmup_counts(experiment, events, records)
    print("\nOPERATIONAL EVENTS DURING NON-COMPARABLE WARM-UP")
    print("not valid for comparison versus control")
    if experiment == "hs_bull":
        for name in ("HS_ELASTIC_STARTED", "HS_ELASTIC_ENDED_RECOVERED",
                     "HS_ELASTIC_EXIT_CONTEXT_LOST", "EXPERIMENTAL_CLOSE"):
            print(f"{name} | {counts[name]}")
        elastic = [row for row in _open_positions(_state(arm), window.cohort_started)
                   if row.get("hs_elastic") and
                   (stamp := parse_time(row.get("open_ts") or row.get("opened_at"))) is not None and stamp < end]
        pair_ids = ", ".join(str(row.get("pair_id") or "N/A") for row in elastic) or "none"
        print(f"positions currently in HS_ELASTIC | {len(elastic)} | {pair_ids}")
    elif experiment == "hs_bear":
        print(f"HS_BEAR_CLUSTER_TRIGGERED | {counts['HS_BEAR_CLUSTER_TRIGGERED']}")
        print(f"EXPERIMENTAL_CLOSE | {counts['EXPERIMENTAL_CLOSE']}")
        print(f"positions closed by cluster | {counts['positions closed by cluster']}")
    elif experiment == "cb_exit":
        print(f"CIRCUIT_BREAKER_TRIGGERED | {counts['CIRCUIT_BREAKER_TRIGGERED']}")
        print(f"CB_EXIT_ALL_TRIGGERED | {counts['CB_EXIT_ALL_TRIGGERED']}")
        print(f"EXPERIMENTAL_CLOSE | {counts['EXPERIMENTAL_CLOSE']}")
        print(f"positions liquidated by CB | {counts['positions liquidated by CB']}")
    elif experiment == "macd_bu_minus":
        _print_macd_warmup(events)
    elif experiment == "ema_macd":
        _print_ema_macd_warmup(events)
    print("comparative net/PF/DD versus BE_OFF_CB_SHADOW | N/A")


def operational_warmup_counts(
    experiment: str,
    events: Iterable[dict[str, Any]],
    records: Iterable[dict[str, Any]],
) -> Counter[str]:
    """Count own-arm operational evidence without producing economics or pairs."""
    counts = Counter(str(row.get("event") or "") for row in events)
    exits = Counter(str(row.get("exit_reason") or "") for row in records)
    if experiment == "hs_bear":
        counts["positions closed by cluster"] = exits["HS_BEAR_CLUSTER_EXIT"]
    elif experiment == "cb_exit":
        counts["positions liquidated by CB"] = exits["CIRCUIT_BREAKER_EXIT_ALL"]
    return counts


def _print_macd_warmup(events: list[dict[str, Any]]) -> None:
    opportunities = [row for row in events if row.get("event") == "SIGNAL_OPPORTUNITY"]
    opens = {_source(row) for row in events if row.get("event") == "OPEN"} - {None}
    bu_minus = [row for row in opportunities if row.get("macd_context") == "BU-"]
    bu_blocked = [row for row in events if row.get("event") == "ENTRY_BLOCKED_MACD_BU_MINUS"]
    accepted_non_bu = sum(_source(row) in opens and row.get("macd_context") != "BU-" for row in opportunities)
    other_blocks = Counter(str(row.get("event")) for row in events
                           if str(row.get("event") or "").startswith("ENTRY_BLOCKED")
                           and row.get("event") != "ENTRY_BLOCKED_MACD_BU_MINUS")
    distribution = Counter(str(row.get("macd_context") or "UNAVAILABLE") for row in opportunities)
    print(f"total opportunities | {len(opportunities)}")
    print(f"BU- opportunities | {len(bu_minus)}")
    print(f"BU- blocked | {len(bu_blocked)}")
    print(f"accepted non-BU- opportunities | {accepted_non_bu}")
    print("other block reasons")
    if other_blocks:
        for reason, count in sorted(other_blocks.items()):
            print(f"  {reason} | {count}")
    else:
        print("  none | 0")
    print("MACD context distribution")
    for context in ("BU+", "BU-", "BE+", "BE-"):
        print(f"  {context} | {distribution[context]}")
    unavailable = sum(count for context, count in distribution.items()
                      if context not in {"BU+", "BU-", "BE+", "BE-"})
    if unavailable:
        print(f"  UNAVAILABLE/OTHER | {unavailable}")


def _print_ema_macd_warmup(events: list[dict[str, Any]]) -> None:
    opportunities = [row for row in events if row.get("event") == "SIGNAL_OPPORTUNITY"]
    accepted = [row for row in events if row.get("event") == "OPEN"]
    blocked = [row for row in events if str(row.get("event") or "").startswith("ENTRY_BLOCKED")]
    combos = Counter((str(row.get("ema_context") or "UNAVAILABLE"),
                      str(row.get("macd_context") or "UNAVAILABLE")) for row in opportunities)
    reasons = Counter(str(row.get("event")) for row in blocked)
    print(f"total opportunities | {len(opportunities)}")
    print(f"accepted | {len(accepted)}")
    print(f"blocked | {len(blocked)}")
    print("block reasons")
    if reasons:
        for reason, count in sorted(reasons.items()):
            print(f"  {reason} | {count}")
    else:
        print("  none | 0")
    print("EMA + MACD matrix")
    for ema_context in ("LON", "BUL", "BEA", "SHO", "MUP", "MDO", "MIX"):
        values = " | ".join(f"{macd_context}={combos[(ema_context, macd_context)]}"
                            for macd_context in ("BU+", "BU-", "BE+", "BE-"))
        print(f"  {ema_context} | {values}")
    unavailable = sum(count for (ema, macd), count in combos.items()
                      if ema not in {"LON", "BUL", "BEA", "SHO", "MUP", "MDO", "MIX"}
                      or macd not in {"BU+", "BU-", "BE+", "BE-"})
    if unavailable:
        print(f"  UNAVAILABLE/OTHER | {unavailable}")


def print_summary(since: datetime) -> None:
    print(f"\nCOMPARABLE SUMMARY | since {_fmt(since)}")
    print(SUMMARY_HEADER.replace(' | median age', ' | FAST | median age'))
    for arm in (REAL_A, CONTROL, *EXPERIMENTS.values(), DMI15_CONTEXT):
        if arm not in (CONTROL, REAL_A):
            _validate_experiment_cohort(arm, since)
        print(summary_line(arm, since, include_fast=True))


def summary_line(arm: Arm, since: datetime, include_fast: bool = False) -> str:
    rows, state = _records(arm, since), _state(arm)
    values = [_net_dollars(row) for row in rows]
    net = [value for value in values if value is not None]
    reasons = Counter(str(row.get("exit_reason") or "") for row in rows)
    ages = [_number(row.get("age_seconds")) for row in rows]
    ages = [value for value in ages if value is not None]
    gains, losses = sum(value for value in net if value > 0), -sum(value for value in net if value < 0)
    pf = "N/A" if not rows or not net else ("inf" if losses == 0 and gains > 0 else f"{gains/losses:.3f}" if losses else "N/A")
    net_trade = f"${sum(net)/len(net):+.4f}" if net and len(net) == len(rows) else "N/A"
    drawdown = realized_max_drawdown(rows)
    dd = f"${drawdown:.4f}" if drawdown is not None else "N/A"
    med_age = f"{median(ages)/60:.1f}m" if ages else "N/A"
    hs = sum(count for reason, count in reasons.items() if reason.startswith("HARD_STOP"))
    pl = sum(count for reason, count in reasons.items() if reason.startswith("PROFIT_LOCK"))
    trail = sum(count for reason, count in reasons.items() if reason.startswith("TRAILING"))
    opened = len(_open_positions(state, since))
    maximum = max_simultaneous(arm, since)
    fast = f" | {reasons['FAST_DROP']}" if include_fast else ''
    return f"{arm.name} | {len(rows)} | {opened} | {_sum_net(rows)} | {net_trade} | {pf} | {dd} | {hs} | {pl} | {trail}{fast} | {med_age} | {maximum}"


def print_macd_bu_minus(arm: Arm, since: datetime) -> None:
    _comparison(CONTROL, arm, since)
    events = _events(arm, since)
    opportunities = [event for event in events if event.get("event") == "SIGNAL_OPPORTUNITY"]
    bu_minus = [event for event in opportunities if event.get("macd_context") == "BU-"]
    blocked = [event for event in events if event.get("event") == "ENTRY_BLOCKED_MACD_BU_MINUS"]
    blocked_sources = {_source(event) for event in blocked} - {None}
    control_opened_sources = blocked_sources & _admitted_sources(CONTROL, since)
    control_rows = _records_by_source(CONTROL, since)
    outcomes = [control_rows[source] for source in control_opened_sources if source in control_rows]
    print("\nMACD BU- hypothesis")
    print(f"opportunities BU- | {len(bu_minus)}")
    print(f"blocked BU- | {len(blocked)}")
    print(f"total opportunities | {len(opportunities)}")
    _print_overlap(CONTROL, arm, since)
    print("BU- blocked by experiment and opened by control | "
          + _control_result(outcomes, len(control_opened_sources)))


def print_ema_macd(arm: Arm, since: datetime, list_accepted: bool) -> None:
    _comparison(CONTROL, arm, since)
    events = _events(arm, since)
    opportunities = [event for event in events if event.get("event") == "SIGNAL_OPPORTUNITY"]
    accepted = [event for event in events if event.get("event") == "OPEN"]
    blocked = [event for event in events if str(event.get("event") or "").startswith("ENTRY_BLOCKED")]
    matrix_blocked = [event for event in blocked
                      if str(event.get("event") or "").startswith("ENTRY_BLOCKED_EMA_MACD_")]
    combos = Counter((str(event.get("ema_context") or "N/A"), str(event.get("macd_context") or "N/A")) for event in opportunities)
    reasons = Counter(str(event.get("event")) for event in blocked)
    blocked_sources = {_source(event) for event in matrix_blocked} - {None}
    control_opened_sources = blocked_sources & _admitted_sources(CONTROL, since)
    control_rows = _records_by_source(CONTROL, since)
    control_outcomes = [control_rows[source] for source in control_opened_sources if source in control_rows]
    print("\nEMA + MACD hypothesis")
    print(f"accepted opportunities | {len(accepted)}")
    print(f"blocked opportunities | {len(blocked)}")
    print("combinations EMA + MACD")
    for (ema_context, macd_context), count in sorted(combos.items()):
        print(f"  {ema_context} + {macd_context} | {count}")
    print("block reasons")
    for reason, count in sorted(reasons.items()):
        print(f"  {reason} | {count}")
    _print_overlap(CONTROL, arm, since)
    print("matrix-blocked opportunities opened by control | "
          + _control_result(control_outcomes, len(control_opened_sources)))


def print_hs_bull(arm: Arm, since: datetime) -> None:
    events, rows, state = _events(arm, since), _records(arm, since), _state(arm)
    started = [event for event in events if event.get("event") == "HS_ELASTIC_STARTED"]
    recovered = [event for event in events if event.get("event") == "HS_ELASTIC_ENDED_RECOVERED"]
    lost = [event for event in events if event.get("event") == "HS_ELASTIC_EXIT_CONTEXT_LOST"]
    elastic_rows = [row for row in rows if row.get("hs_original_at")]
    active_elastic = [row for row in _open_positions(state, since) if row.get("hs_original_at")]
    elastic_positions = [*elastic_rows, *active_elastic]
    subsequent = Counter(str(row.get("exit_reason") or "") for row in elastic_rows)
    worst = [_number(row.get("hs_elastic_worst_pnl_pct")) for row in elastic_positions]
    worst = [value for value in worst if value is not None]
    extra = [_elastic_seconds(row) for row in elastic_positions]
    extra = [value for value in extra if value is not None]
    print(f"\nHS_BULL_ELASTIC | comparable since {_fmt(since)}")
    print(f"HS reached -1.5% in LON | {len(started)}")
    print(f"entered HS_ELASTIC | {len(started)}")
    print(f"recovered and restored normal HS | {len(recovered)}")
    print(f"lost LON and closed | {len(lost)}")
    print(f"subsequent PL | {sum(v for k,v in subsequent.items() if k.startswith('PROFIT_LOCK'))}")
    print(f"subsequent TRAIL | {sum(v for k,v in subsequent.items() if k.startswith('TRAILING'))}")
    print(f"worst additional drawdown beyond -1.5% | {max(0.0, -1.5-min(worst)):.4f}%" if worst else "worst additional drawdown beyond -1.5% | N/A")
    print(f"additional open time median | {median(extra)/60:.1f}m" if extra else "additional open time median | N/A")
    print(f"additional slot occupancy | {sum(extra)/3600:.3f}h" if extra else "additional slot occupancy | N/A")
    print("economic comparison vs BE_OFF_CB | " + _paired_economics(CONTROL, arm, since, sources={_source(event) for event in started} - {None}))


def print_forced_exit_detail(arm: Arm, since: datetime, trigger_name: str, exit_reason: str) -> None:
    events, experiment_rows = _events(arm, since), _records(arm, since)
    triggers = [event for event in events if event.get("event") == trigger_name]
    victim_ids = [str(pair_id) for event in triggers for pair_id in event.get("victim_pair_ids", [])]
    by_pair = {str(row.get("pair_id")): row for row in experiment_rows}
    event_victims = [by_pair[pair_id] for pair_id in victim_ids if pair_id in by_pair and by_pair[pair_id].get("exit_reason") == exit_reason]
    ledger_victims = [row for row in experiment_rows if row.get("exit_reason") == exit_reason]
    victims = list({str(row.get("pair_id")): row for row in [*event_victims, *ledger_victims]}.values())
    control = _records_by_source(CONTROL, since)
    paired = [(row, control.get(_source(row))) for row in victims]
    resolved = [(row, control_row) for row, control_row in paired if control_row is not None]
    reasons = Counter(str(control_row.get("exit_reason") or "") for _, control_row in resolved)
    label = "HS_BEAR" if trigger_name.startswith("HS_BEAR") else "CB_EXIT"
    print(f"\n{label} | comparable since {_fmt(since)}")
    if label == "HS_BEAR":
        print(f"HS_BEAR_CLUSTER_TRIGGERED events | {len(triggers)}")
        print(f"negative positions closed early | {len(victims)}")
    else:
        crises = sum(event.get("event") == "CIRCUIT_BREAKER_TRIGGERED" for event in events)
        liquidation_crises = sum(bool(event.get("victim_pair_ids")) for event in triggers)
        if victims and liquidation_crises == 0:
            liquidation_crises = len({str(row.get("closed_at") or "")[:16] for row in victims})
        print(f"CB crises | {crises}")
        print(f"crises that liquidated positions | {liquidation_crises}")
        print(f"positions liquidated | {len(victims)}")
    print(f"{'early-close result' if label == 'HS_BEAR' else 'PnL at liquidation'} | {_sum_net(victims)}")
    print(f"corresponding BE_OFF_CB result | {_sum_net([row for _,row in resolved])}")
    print(f"economic difference | {_delta_net(resolved)}")
    print(f"control pending/unavailable | {len(victims)-len(resolved)}")
    print("control destinations | " + " | ".join(f"{reason}={count}" for reason, count in sorted(reasons.items())) if reasons else "control destinations | N/A")


def _comparison(control: Arm, experiment: Arm, since: datetime) -> None:
    print(f"\nCOMPARABLE EXPERIMENT | since {_fmt(since)}")
    fast = experiment == EXPERIMENTS['fast_drop']
    print(SUMMARY_HEADER.replace(' | median age', ' | FAST | median age') if fast else SUMMARY_HEADER)
    print(summary_line(control, since, include_fast=fast))
    print(summary_line(experiment, since, include_fast=fast))


def _print_overlap(control: Arm, experiment: Arm, since: datetime) -> None:
    control_sources, experiment_sources = _admitted_sources(control, since), _admitted_sources(experiment, since)
    common = control_sources & experiment_sources
    print(f"trades common | {len(common)}")
    print(f"control-only | {len(control_sources-common)}")
    print(f"experiment-only | {len(experiment_sources-common)}")


def _print_accepted(arm: Arm, since: datetime, until: datetime) -> None:
    accepted = [event for event in _events_between(arm, since, until) if event.get("event") == "OPEN"]
    closed = [row for row in _records(arm, since)
              if (stamp := parse_time(row.get("opened_at") or row.get("open_ts"))) is not None and stamp < until]
    opened = [row for row in _open_positions(_state(arm), since)
              if (stamp := parse_time(row.get("open_ts") or row.get("opened_at"))) is not None and stamp < until]
    sources = {_source(event) for event in accepted}
    for row in [*closed, *opened]:
        if _source(row) not in sources:
            context = (row.get('market_context_entry') or {}).get('tf_5m') or {}
            accepted.append({**context, 'event': 'OPEN', 'source_candle_open_time': _source(row),
                             'ts': row.get('opened_at') or row.get('open_ts')})
    rows = accepted_trade_rows(accepted, closed, opened)
    print("\nACCEPTED TRADES")
    print("source_candle | entry BRT | entry price | exit BRT | exit price | net | EMA context | MACD context | reason / exit type")
    for item in rows:
        is_open = item['status'] == 'OPEN'
        print(f"{_fmt_ms(item['source'])} | {_fmt(item['opened_at'])} | {_fmt_price(item['entry'])} | "
              f"{'OPEN' if is_open else _fmt(item['closed_at'])} | {'OPEN' if is_open else _fmt_price(item['exit'])} | "
              f"{'OPEN' if is_open else _fmt_net(item['net'])} | {item['ema']} | {item['macd']} | "
              f"{'OPEN' if is_open else item['exit_reason']}")
    if not rows:
        print("N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A")


def accepted_trade_rows(
    accepted_events: Iterable[dict[str, Any]],
    closed_rows: Iterable[dict[str, Any]],
    open_rows: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join accepted OPEN telemetry to its current or final position record."""
    closed = {_source(row): row for row in closed_rows if _source(row) is not None}
    opened = {_source(row): row for row in open_rows if _source(row) is not None}
    output = []
    seen: set[int] = set()
    for event in accepted_events:
        source = _source(event)
        if source is None or source in seen:
            continue
        seen.add(source)
        final = closed.get(source)
        row = final or opened.get(source) or {}
        output.append({
            "opened_at": parse_time(row.get("opened_at") or row.get("open_ts") or event.get("ts")),
            "closed_at": parse_time(final.get("closed_at") or final.get("close_ts")) if final else None,
            "source": source,
            "ema": str(event.get("ema_context") or "N/A"),
            "macd": str(event.get("macd_context") or "N/A"),
            "entry": _number(row.get("entry_price") if row.get("entry_price") is not None else event.get("price")),
            "exit": _number(final.get("exit_price")) if final else None,
            "exit_reason": str(final.get("exit_reason") or "N/A") if final else "N/A",
            "net": _net_dollars(final) if final else None,
            "status": "CLOSED" if final else "OPEN",
        })
    return sorted(output, key=lambda row: (row["opened_at"] or datetime.min.replace(tzinfo=timezone.utc), row["source"]))


def _control_result(rows: list[dict[str, Any]], expected: int) -> str:
    if not expected:
        return "N/A"
    return f"resolved={len(rows)}/{expected} | net={_sum_net(rows)} | " + _destination_counts(rows)


def _paired_economics(control: Arm, experiment: Arm, since: datetime, sources: set[int]) -> str:
    left, right = _records_by_source(control, since), _records_by_source(experiment, since)
    common = sources & set(left) & set(right)
    pairs = [(right[source], left[source]) for source in common]
    return f"resolved={len(pairs)}/{len(sources)} | delta={_delta_net(pairs)} | control {_destination_counts([control_row for _,control_row in pairs])}"


def _destination_counts(rows: Iterable[dict[str, Any]]) -> str:
    counts = Counter(str(row.get("exit_reason") or "N/A") for row in rows)
    return " | ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "destinations=N/A"


def _delta_net(pairs: Iterable[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    values = []
    for experiment, control in pairs:
        left, right = _net_dollars(experiment), _net_dollars(control)
        if left is not None and right is not None:
            values.append(left-right)
    return f"${sum(values):+.4f}" if values else "N/A"


def _sum_net(rows: Iterable[dict[str, Any]]) -> str:
    rows = list(rows)
    values = [_net_dollars(row) for row in rows]
    return f"${sum(value for value in values if value is not None):+.4f}" if rows and all(value is not None for value in values) else "N/A"


def _fmt_net(value: float | None) -> str:
    return f"${value:+.4f}" if value is not None else "N/A"


def _fmt_price(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "N/A"


def _net_dollars(row: dict[str, Any]) -> float | None:
    net_pct, notional = _number(row.get("net_pnl_pct")), _number(row.get("position_notional_usdt"))
    return net_pct * notional / 100 if net_pct is not None and notional is not None else None


def realized_max_drawdown(rows: Iterable[dict[str, Any]]) -> float | None:
    """Peak-to-trough drawdown of closed-trade realized equity, starting at zero."""
    ordered = []
    for row in rows:
        closed_at = parse_time(row.get("closed_at") or row.get("close_ts"))
        net = _net_dollars(row)
        if closed_at is None or net is None:
            return None
        ordered.append((closed_at, net))
    if not ordered:
        return None
    equity = peak = max_drawdown = 0.0
    for _, net in sorted(ordered, key=lambda item: item[0]):
        equity += net
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak-equity)
    return max_drawdown


def _elastic_seconds(row: dict[str, Any]) -> float | None:
    persisted = _number(row.get("hs_elastic_extra_seconds"))
    if persisted is not None and (persisted > 0 or not row.get("hs_elastic")):
        return persisted
    start = parse_time(row.get("hs_elastic_started_at"))
    if start is not None and row.get("hs_elastic"):
        return max(0.0, (datetime.now(timezone.utc) - start).total_seconds())
    return persisted


def _records(arm: Arm, since: datetime) -> list[dict[str, Any]]:
    return [row for row in _jsonl(ROOT / arm.ledger) if (stamp := parse_time(row.get("opened_at"))) is not None and stamp >= since
            and (arm != REAL_A or (row.get('position_type') == 'BOT_EXIT' and not row.get('phantom') and not row.get('shadow_kind')))]


def _records_between(arm: Arm, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return [row for row in _jsonl(ROOT / arm.ledger)
            if (stamp := parse_time(row.get("opened_at") or row.get("open_ts"))) is not None
            and start <= stamp < end]


def _events(arm: Arm, since: datetime) -> list[dict[str, Any]]:
    return _admission_events(arm, [row for row in _jsonl(ROOT / arm.events)
                             if (stamp := parse_time(row.get("ts"))) is not None and stamp >= since])


def _events_between(arm: Arm, start: datetime, end: datetime) -> list[dict[str, Any]]:
    return _admission_events(arm, [row for row in _jsonl(ROOT / arm.events)
            if (stamp := parse_time(row.get("ts"))) is not None and start <= stamp < end])


def _admission_events(arm: Arm, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cutoff = ADMISSION_CUTOFF.get()
    if cutoff is None:
        return rows
    positions = [*_jsonl(ROOT / arm.ledger), *_state(arm).get('positions', [])]
    excluded = {str(row['pair_id']) for row in positions if row.get('pair_id')
                and (opened := parse_time(row.get('opened_at') or row.get('open_ts'))) is not None
                and opened < cutoff}
    output = []
    for row in rows:
        if str(row.get('pair_id')) in excluded:
            continue
        item = dict(row)
        for key in ('trigger_pair_ids', 'victim_pair_ids'):
            if key in item:
                item[key] = [pair for pair in item[key] if str(pair) not in excluded]
        if row.get('trigger_pair_ids') and not item['trigger_pair_ids']:
            continue
        output.append(item)
    return output


def _state(arm: Arm) -> dict[str, Any]:
    try:
        value = json.loads((ROOT / arm.state).read_text(encoding="utf-8"))
        if arm == REAL_A and isinstance(value, list):
            return {'positions': [p for p in value if p.get('label') == 'B' and not p.get('phantom') and not p.get('shadow_kind')]}
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _open_positions(state: dict[str, Any], since: datetime) -> list[dict[str, Any]]:
    return [row for row in state.get("positions", []) if row.get("status") == "OPEN"
            and (stamp := parse_time(row.get("open_ts") or row.get("opened_at"))) is not None and stamp >= since]


def _records_by_source(arm: Arm, since: datetime) -> dict[int, dict[str, Any]]:
    return {source: row for row in _records(arm, since) if (source := _source(row)) is not None}


def _admitted_sources(arm: Arm, since: datetime) -> set[int]:
    state_sources = {_source(row) for row in _open_positions(_state(arm), since)}
    closed_sources = {_source(row) for row in _records(arm, since)}
    event_sources = {_source(row) for row in _events(arm, since) if row.get("event") == "OPEN"}
    return (state_sources | closed_sources | event_sources) - {None}


def max_simultaneous(arm: Arm, since: datetime) -> int:
    return calculate_max_simultaneous(_records(arm, since), _open_positions(_state(arm), since), since)


def calculate_max_simultaneous(
    closed_rows: Iterable[dict[str, Any]],
    open_rows: Iterable[dict[str, Any]],
    since: datetime,
) -> int:
    """Recalculate concurrency using only positions admitted in this window."""
    points: list[tuple[datetime, int]] = []
    seen: set[str] = set()
    for row in [*closed_rows, *open_rows]:
        pair_id = str(row.get("pair_id") or id(row))
        if pair_id in seen:
            continue
        seen.add(pair_id)
        opened = parse_time(row.get("opened_at") or row.get("open_ts"))
        if opened is None or opened < since:
            continue
        points.append((opened, 1))
        closed = parse_time(row.get("closed_at") or row.get("close_ts"))
        if closed is not None:
            points.append((closed, -1))
    current = maximum = 0
    # A close at the same instant as an open frees its slot first.
    for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
        current += delta
        maximum = max(maximum, current)
    return maximum


def pair_by_source(control: Iterable[dict[str, Any]], experiment: Iterable[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return (experiment, control) pairs with an exact source candle match."""
    left = {_source(row): row for row in control if _source(row) is not None}
    right = {_source(row): row for row in experiment if _source(row) is not None}
    return [(right[source], left[source]) for source in sorted(set(left) & set(right))]


def filter_since(rows: Iterable[dict[str, Any]], since: datetime, field: str) -> list[dict[str, Any]]:
    return [row for row in rows if (stamp := parse_time(row.get(field))) is not None and stamp >= since]


def event_counts(rows: Iterable[dict[str, Any]], since: datetime) -> Counter[str]:
    return Counter(str(row.get("event")) for row in filter_since(rows, since, "ts"))


def _validate_experiment_cohort(arm: Arm, since: datetime) -> None:
    state = _state(arm)
    marker = parse_time(state.get("cohort_started_at"))
    cohort_floor = parse_time(DEFAULT_SINCE_TEXT)
    if marker is not None and cohort_floor is not None and marker < cohort_floor:
        raise SystemExit(f"{arm.name}: state cohort {_fmt(marker)} predates forward cohort {_fmt(cohort_floor)}")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    output = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            output.append(value)
    return output


def _source(row: dict[str, Any]) -> int | None:
    try:
        return int(row.get("source_candle_open_time"))
    except (TypeError, ValueError):
        return None


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or BRASILIA_TZ).astimezone(timezone.utc)
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _fmt(value: datetime | None) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S BRT") if value else "N/A"


def _fmt_ms(value: int | None) -> str:
    return _fmt(datetime.fromtimestamp(value/1000, timezone.utc)) if value is not None else "N/A"


if __name__ == "__main__":
    main()
