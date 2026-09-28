"""Compact forward report for the 2026-09-27 BE_OFF_CB experiment cohort."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.console_utils import BRASILIA_TZ


DEFAULT_SINCE_TEXT = "27/09/2026 20:07:29"
SUMMARY_HEADER = "arm | closed | open | net $/trade | PF | HS | PL | TRAIL | median age | max simultaneous"


@dataclass(frozen=True)
class Arm:
    name: str
    ledger: str
    state: str
    events: str


CONTROL = Arm("BE_OFF_CB_SHADOW", "data/trades/trades_be_off_cb_shadow.jsonl",
              "data/state/be_off_cb_shadow.json", "data/telemetry/be_off_cb_shadow_events.jsonl")
EXPERIMENTS = {
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
    parser.add_argument("--since", default=DEFAULT_SINCE_TEXT, help="BRT DD/MM/AAAA HH:MM:SS or ISO timestamp")
    parser.add_argument("--list-accepted", action="store_true", help="With ema_macd, list admitted trades for plotting")
    args = parser.parse_args()
    since = parse_time(args.since)
    if since is None:
        raise SystemExit("invalid --since")
    if args.list_accepted and args.experiment != "ema_macd":
        raise SystemExit("--list-accepted is only valid with --experiment ema_macd")
    if args.experiment is None:
        print_summary(since)
        return
    arm = EXPERIMENTS[args.experiment]
    _validate_experiment_cohort(arm, since)
    if args.experiment == "macd_bu_minus":
        print_macd_bu_minus(arm, since)
    elif args.experiment == "ema_macd":
        print_ema_macd(arm, since, args.list_accepted)
    elif args.experiment == "hs_bull":
        print_hs_bull(arm, since)
    elif args.experiment == "hs_bear":
        print_forced_exit_detail(arm, since, "HS_BEAR_CLUSTER_TRIGGERED", "HS_BEAR_CLUSTER_EXIT")
    else:
        print_forced_exit_detail(arm, since, "CB_EXIT_ALL_TRIGGERED", "CIRCUIT_BREAKER_EXIT_ALL")


def print_summary(since: datetime) -> None:
    print(f"FORWARD EXPERIMENT COHORT | since {_fmt(since)}")
    print(SUMMARY_HEADER)
    for arm in (CONTROL, *EXPERIMENTS.values()):
        if arm is not CONTROL:
            _validate_experiment_cohort(arm, since)
        print(summary_line(arm, since))


def summary_line(arm: Arm, since: datetime) -> str:
    rows, state = _records(arm, since), _state(arm)
    values = [_net_dollars(row) for row in rows]
    net = [value for value in values if value is not None]
    reasons = Counter(str(row.get("exit_reason") or "") for row in rows)
    ages = [_number(row.get("age_seconds")) for row in rows]
    ages = [value for value in ages if value is not None]
    gains, losses = sum(value for value in net if value > 0), -sum(value for value in net if value < 0)
    pf = "N/A" if not rows or not net else ("inf" if losses == 0 and gains > 0 else f"{gains/losses:.3f}" if losses else "N/A")
    net_trade = f"${sum(net)/len(net):+.4f}" if net and len(net) == len(rows) else "N/A"
    med_age = f"{median(ages)/60:.1f}m" if ages else "N/A"
    hs = sum(count for reason, count in reasons.items() if reason.startswith("HARD_STOP"))
    pl = sum(count for reason, count in reasons.items() if reason.startswith("PROFIT_LOCK"))
    trail = sum(count for reason, count in reasons.items() if reason.startswith("TRAILING"))
    opened = len(_open_positions(state, since))
    maximum = state.get("max_simultaneous_positions")
    return f"{arm.name} | {len(rows)} | {opened} | {net_trade} | {pf} | {hs} | {pl} | {trail} | {med_age} | {maximum if maximum is not None else 'N/A'}"


def print_macd_bu_minus(arm: Arm, since: datetime) -> None:
    _comparison(CONTROL, arm, since)
    events = _events(arm, since)
    opportunities = [event for event in events if event.get("event") == "SIGNAL_OPPORTUNITY"]
    bu_minus = [event for event in opportunities if event.get("macd_context") == "BU-"]
    blocked = [event for event in events if event.get("event") == "ENTRY_BLOCKED_MACD_BU_MINUS"]
    blocked_sources = {_source(event) for event in blocked} - {None}
    control_rows = _records_by_source(CONTROL, since)
    outcomes = [control_rows[source] for source in blocked_sources if source in control_rows]
    print("\nMACD BU- hypothesis")
    print(f"opportunities BU- | {len(bu_minus)}")
    print(f"blocked BU- | {len(blocked)}")
    print(f"total opportunities | {len(opportunities)}")
    print("blocked opportunities in control | " + _control_result(outcomes, len(blocked_sources)))
    _print_overlap(CONTROL, arm, since)


def print_ema_macd(arm: Arm, since: datetime, list_accepted: bool) -> None:
    _comparison(CONTROL, arm, since)
    events = _events(arm, since)
    opportunities = [event for event in events if event.get("event") == "SIGNAL_OPPORTUNITY"]
    accepted = [event for event in events if event.get("event") == "OPEN"]
    blocked = [event for event in events if str(event.get("event") or "").startswith("ENTRY_BLOCKED")]
    combos = Counter((str(event.get("ema_context") or "N/A"), str(event.get("macd_context") or "N/A")) for event in opportunities)
    reasons = Counter(str(event.get("event")) for event in blocked)
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
    if list_accepted:
        _print_accepted(arm, accepted, since)


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
    print(f"HS_BULL_ELASTIC | since {_fmt(since)}")
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
    print(f"{label} | since {_fmt(since)}")
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
    print(f"FORWARD EXPERIMENT | since {_fmt(since)}")
    print(SUMMARY_HEADER)
    print(summary_line(control, since))
    print(summary_line(experiment, since))


def _print_overlap(control: Arm, experiment: Arm, since: datetime) -> None:
    control_sources, experiment_sources = _admitted_sources(control, since), _admitted_sources(experiment, since)
    common = control_sources & experiment_sources
    print(f"trades common | {len(common)}")
    print(f"control-only | {len(control_sources-common)}")
    print(f"experiment-only | {len(experiment_sources-common)}")


def _print_accepted(arm: Arm, accepted: list[dict[str, Any]], since: datetime) -> None:
    closed, state = _records_by_source(arm, since), _state(arm)
    opened = {_source(row): row for row in _open_positions(state, since)}
    print("accepted trades")
    print("timestamp | source candle | EMA | MACD | exit | net")
    for event in accepted:
        source = _source(event)
        row = closed.get(source) or opened.get(source) or {}
        print(f"{_fmt(parse_time(event.get('ts')))} | {_fmt_ms(source)} | {event.get('ema_context') or 'N/A'} | "
              f"{event.get('macd_context') or 'N/A'} | {row.get('exit_reason') or 'OPEN'} | {_fmt_net(_net_dollars(row))}")


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


def _net_dollars(row: dict[str, Any]) -> float | None:
    net_pct, notional = _number(row.get("net_pnl_pct")), _number(row.get("position_notional_usdt"))
    return net_pct * notional / 100 if net_pct is not None and notional is not None else None


def _elastic_seconds(row: dict[str, Any]) -> float | None:
    persisted = _number(row.get("hs_elastic_extra_seconds"))
    if persisted is not None and (persisted > 0 or not row.get("hs_elastic")):
        return persisted
    start = parse_time(row.get("hs_elastic_started_at"))
    if start is not None and row.get("hs_elastic"):
        return max(0.0, (datetime.now(timezone.utc) - start).total_seconds())
    return persisted


def _records(arm: Arm, since: datetime) -> list[dict[str, Any]]:
    return [row for row in _jsonl(ROOT / arm.ledger) if (stamp := parse_time(row.get("opened_at"))) is not None and stamp >= since]


def _events(arm: Arm, since: datetime) -> list[dict[str, Any]]:
    return [row for row in _jsonl(ROOT / arm.events) if (stamp := parse_time(row.get("ts"))) is not None and stamp >= since]


def _state(arm: Arm) -> dict[str, Any]:
    try:
        value = json.loads((ROOT / arm.state).read_text(encoding="utf-8"))
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
