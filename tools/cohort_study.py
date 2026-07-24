from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover - production venv includes PyYAML
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@dataclass(frozen=True)
class CohortRule:
    name: str
    min_open_positions: int
    underwater_ratio: float
    oldest_age_minutes: float
    below_average_entry_atr: float


@dataclass(frozen=True)
class CohortMetrics:
    open_positions: int
    underwater_positions: int
    underwater_ratio: float
    oldest_age_minutes: float
    average_entry_price: float
    current_price: float
    entry_atr: Optional[float]
    below_average_entry_atr: Optional[float]


@dataclass(frozen=True)
class ReplayDecision:
    mode: str
    rule: CohortRule
    record: Dict[str, Any]
    metrics: CohortMetrics
    blocked: bool

    @property
    def scored(self) -> bool:
        return _score_eligible(self.record)


@dataclass(frozen=True)
class SizingRule:
    name: str
    min_open_positions: int
    negative_threshold_pct: float
    negative_fraction: float
    size_factor: float


@dataclass(frozen=True)
class SizingMetrics:
    open_positions: int
    negative_positions: int
    negative_fraction: float
    current_price: float


@dataclass(frozen=True)
class SizingDecision:
    rule: SizingRule
    record: Dict[str, Any]
    metrics: SizingMetrics
    reduced: bool

    @property
    def scored(self) -> bool:
        return _score_eligible(self.record)


@dataclass(frozen=True)
class SizingEconomics:
    actual_net_usdt: float
    hypothetical_net_usdt: float
    delta_usdt: float
    saved_losses_usdt: float
    foregone_winners_usdt: float
    saved_fees_usdt: float
    balance_delta_pct: float


def main() -> None:
    args = _parse_args()
    config = _load_config(Path(args.config))
    rules = [_parse_rule(item) for item in args.rule] if args.rule else _rules_from_config(config)
    for rule in rules:
        _validate_rule(rule)
    sizing_rules = (
        []
        if args.no_sizing
        else (
            [_parse_sizing_rule(item) for item in args.sizing_rule]
            if args.sizing_rule
            else _sizing_rules_from_config(config)
        )
    )
    for rule in sizing_rules:
        _validate_sizing_rule(rule)
    records = _load_records([Path(item) for item in args.ledger], [Path(item) for item in args.state])
    records = [
        item
        for item in records
        if args.profile == "all"
        or not item.get("profile")
        or str(item.get("profile")) == args.profile
    ]
    modes = [args.mode] if args.mode != "both" else ["static", "sequential"]
    decisions = [
        decision
        for rule in rules
        for mode in modes
        for decision in _run_mode(records, rule, mode)
    ]
    sizing_decisions = run_sizing_replay(records, sizing_rules)
    episode_gap = float(
        args.episode_gap_hours
        if args.episode_gap_hours is not None
        else _study_config(config).get("episode_gap_hours", 6)
    )
    operational_balance = float(
        args.operational_balance_usdt
        if args.operational_balance_usdt is not None
        else _operational_balance(config)
    )
    _print_report(
        records,
        decisions,
        rules,
        modes,
        sizing_decisions,
        sizing_rules,
        operational_balance,
        episode_gap,
        args.detail,
    )


def run_replay(
    records: Sequence[Dict[str, Any]],
    rules: Sequence[CohortRule],
    modes: Sequence[str] = ("static", "sequential"),
) -> list[ReplayDecision]:
    normalized_records = [_normalize_record(item) for item in records]
    normalized = _dedupe_records(item for item in normalized_records if _is_real_bot_position(item))
    for rule in rules:
        _validate_rule(rule)
    return [
        decision
        for rule in rules
        for mode in modes
        for decision in _run_mode(normalized, rule, mode)
    ]


def run_sizing_replay(
    records: Sequence[Dict[str, Any]],
    rules: Sequence[SizingRule],
) -> list[SizingDecision]:
    normalized_records = [_normalize_record(item) for item in records]
    normalized = _dedupe_records(item for item in normalized_records if _is_real_bot_position(item))
    for rule in rules:
        _validate_sizing_rule(rule)
    ordered = sorted(normalized, key=_record_sort_key)
    decisions: list[SizingDecision] = []
    for candidate in ordered:
        opened = _opened_at(candidate)
        if opened is None:
            continue
        existing = [
            item
            for item in ordered
            if item is not candidate and _is_open_at(item, opened) and (_opened_at(item) or opened) < opened
        ]
        for rule in rules:
            decisions.append(_sizing_decision(rule, candidate, existing))
    return decisions


def sizing_economics(
    decisions: Sequence[SizingDecision],
    operational_balance_usdt: float,
) -> SizingEconomics:
    selected = [item for item in decisions if item.reduced and item.scored]
    actual = sum(_net_usdt(item.record) or 0.0 for item in selected)
    hypothetical = sum(
        (_net_usdt(item.record) or 0.0) * item.rule.size_factor
        for item in selected
    )
    saved_losses = sum(
        -actual_net * (1 - item.rule.size_factor)
        for item in selected
        if (actual_net := _net_usdt(item.record) or 0.0) < 0
    )
    foregone_winners = sum(
        actual_net * (1 - item.rule.size_factor)
        for item in selected
        if (actual_net := _net_usdt(item.record) or 0.0) > 0
    )
    saved_fees = sum(
        (_fees_usdt(item.record) or 0.0) * (1 - item.rule.size_factor)
        for item in selected
    )
    delta = hypothetical - actual
    return SizingEconomics(
        actual_net_usdt=actual,
        hypothetical_net_usdt=hypothetical,
        delta_usdt=delta,
        saved_losses_usdt=saved_losses,
        foregone_winners_usdt=foregone_winners,
        saved_fees_usdt=saved_fees,
        balance_delta_pct=(delta / operational_balance_usdt * 100)
        if operational_balance_usdt > 0
        else 0.0,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay offline da admissao por pressao da coorte; nunca envia ordens nem altera o bot."
    )
    parser.add_argument("--ledger", action="append", required=True, help="Ledger JSONL; pode ser repetido.")
    parser.add_argument("--state", action="append", default=[], help="Estado JSON opcional; pode ser repetido.")
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument("--mode", choices=["static", "sequential", "both"], default="both")
    parser.add_argument(
        "--rule",
        action="append",
        help="name/min_open/underwater_ratio/oldest_age_minutes/below_average_entry_atr",
    )
    parser.add_argument(
        "--sizing-rule",
        action="append",
        help="name/min_open/negative_threshold_pct/negative_fraction/size_factor",
    )
    parser.add_argument("--no-sizing", action="store_true", help="Oculta o estudo de sizing degressivo.")
    parser.add_argument("--operational-balance-usdt", type=float)
    parser.add_argument("--episode-gap-hours", type=float)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    return parser.parse_args()


def _run_mode(records: Sequence[Dict[str, Any]], rule: CohortRule, mode: str) -> list[ReplayDecision]:
    ordered = sorted(records, key=_record_sort_key)
    if mode == "static":
        decisions: list[ReplayDecision] = []
        for candidate in ordered:
            opened = _opened_at(candidate)
            if opened is None:
                continue
            existing = [
                item
                for item in ordered
                if item is not candidate and _is_open_at(item, opened) and (_opened_at(item) or opened) < opened
            ]
            decisions.append(_decision(mode, rule, candidate, existing))
        return decisions
    if mode != "sequential":
        raise ValueError(f"unsupported replay mode: {mode}")

    active: list[Dict[str, Any]] = []
    decisions = []
    for candidate in ordered:
        opened = _opened_at(candidate)
        if opened is None:
            continue
        active = [item for item in active if _is_open_at(item, opened)]
        decision = _decision(mode, rule, candidate, active)
        decisions.append(decision)
        if not decision.blocked:
            active.append(candidate)
    return decisions


def _decision(
    mode: str,
    rule: CohortRule,
    candidate: Dict[str, Any],
    existing: Sequence[Dict[str, Any]],
) -> ReplayDecision:
    metrics = _cohort_metrics(candidate, existing)
    gap = metrics.below_average_entry_atr
    blocked = bool(
        metrics.open_positions >= rule.min_open_positions
        and metrics.underwater_ratio + 1e-12 >= rule.underwater_ratio
        and metrics.oldest_age_minutes + 1e-12 >= rule.oldest_age_minutes
        and gap is not None
        and gap + 1e-12 >= rule.below_average_entry_atr
    )
    return ReplayDecision(mode, rule, candidate, metrics, blocked)


def _sizing_decision(
    rule: SizingRule,
    candidate: Dict[str, Any],
    existing: Sequence[Dict[str, Any]],
) -> SizingDecision:
    current_price = _float(candidate.get("entry_price")) or 0.0
    negative = sum(
        1
        for item in existing
        if (
            pnl_pct := _mark_pnl_pct(item, current_price)
        ) is not None
        and pnl_pct <= rule.negative_threshold_pct + 1e-12
    )
    count = len(existing)
    fraction = negative / count if count else 0.0
    metrics = SizingMetrics(
        open_positions=count,
        negative_positions=negative,
        negative_fraction=fraction,
        current_price=current_price,
    )
    reduced = bool(
        count >= rule.min_open_positions
        and fraction + 1e-12 >= rule.negative_fraction
    )
    return SizingDecision(rule, candidate, metrics, reduced)


def _cohort_metrics(candidate: Dict[str, Any], existing: Sequence[Dict[str, Any]]) -> CohortMetrics:
    opened = _opened_at(candidate)
    current_price = _float(candidate.get("entry_price")) or 0.0
    entry_atr = _float(candidate.get("entry_atr"))
    underwater = sum(
        1 for item in existing if (_float(item.get("entry_price")) or current_price) > current_price
    )
    count = len(existing)
    ratio = underwater / count if count else 0.0
    ages = [
        max(0.0, (opened - item_opened).total_seconds() / 60)
        for item in existing
        if opened is not None and (item_opened := _opened_at(item)) is not None
    ]
    average_entry = _quantity_weighted_entry(existing)
    gap_atr = (
        (average_entry - current_price) / entry_atr
        if entry_atr is not None and entry_atr > 0 and average_entry > 0
        else None
    )
    return CohortMetrics(
        open_positions=count,
        underwater_positions=underwater,
        underwater_ratio=ratio,
        oldest_age_minutes=max(ages) if ages else 0.0,
        average_entry_price=average_entry,
        current_price=current_price,
        entry_atr=entry_atr,
        below_average_entry_atr=gap_atr,
    )


def _mark_pnl_pct(record: Dict[str, Any], current_price: float) -> Optional[float]:
    entry = _float(record.get("entry_price"))
    if entry is None or entry <= 0 or current_price <= 0:
        return None
    return (current_price / entry - 1) * 100


def _quantity_weighted_entry(records: Sequence[Dict[str, Any]]) -> float:
    weighted = 0.0
    total_weight = 0.0
    for record in records:
        entry = _float(record.get("entry_price"))
        if entry is None:
            continue
        qty = _float(record.get("qty")) or _float(record.get("quantity"))
        if qty is None or qty <= 0:
            notional = _float(record.get("position_notional_usdt"))
            qty = notional / entry if notional is not None and notional > 0 and entry > 0 else 1.0
        weighted += entry * qty
        total_weight += qty
    return weighted / total_weight if total_weight else 0.0


def _load_records(ledger_paths: Sequence[Path], state_paths: Sequence[Path]) -> list[Dict[str, Any]]:
    records: list[Dict[str, Any]] = []
    for path in ledger_paths:
        records.extend(_read_jsonl(path))
    for path in state_paths:
        state = _read_json(path, [])
        if isinstance(state, list):
            records.extend(item for item in state if isinstance(item, dict))
    normalized = [_normalize_record(item) for item in records]
    return _dedupe_records(item for item in normalized if _is_real_bot_position(item))


def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    output = dict(record)
    output["opened_at"] = output.get("opened_at") or output.get("open_ts")
    output["closed_at"] = output.get("closed_at") or output.get("close_ts")
    output["qty"] = output.get("qty") if output.get("qty") is not None else output.get("quantity")
    return output


def _dedupe_records(records: Iterable[Dict[str, Any]]) -> list[Dict[str, Any]]:
    output: dict[str, Dict[str, Any]] = {}
    anonymous: list[Dict[str, Any]] = []
    for record in records:
        pair_id = str(record.get("pair_id") or "")
        if not pair_id:
            anonymous.append(record)
            continue
        previous = output.get(pair_id)
        if previous is None or (_closed_at(record) is not None and _closed_at(previous) is None):
            output[pair_id] = record
    return [*output.values(), *anonymous]


def _is_real_bot_position(record: Dict[str, Any]) -> bool:
    if bool(record.get("phantom", False)) or str(record.get("position_type") or "") == "PHANTOM":
        return False
    label = str(record.get("label") or record.get("position") or "B")
    return label == "B"


def _is_open_at(record: Dict[str, Any], ts: datetime) -> bool:
    opened = _opened_at(record)
    closed = _closed_at(record)
    return opened is not None and opened < ts and (closed is None or ts < closed)


def _score_eligible(record: Dict[str, Any]) -> bool:
    strategy = str(record.get("strategy_version") or "").lower()
    run_id = str(record.get("run_id") or "").lower()
    return (
        "cleanup" not in strategy
        and "cleanup" not in run_id
        and _closed_at(record) is not None
        and _net_pnl(record) is not None
    )


def _print_report(
    records: Sequence[Dict[str, Any]],
    decisions: Sequence[ReplayDecision],
    rules: Sequence[CohortRule],
    modes: Sequence[str],
    sizing_decisions: Sequence[SizingDecision],
    sizing_rules: Sequence[SizingRule],
    operational_balance_usdt: float,
    episode_gap_hours: float,
    detail: bool,
) -> None:
    scored_records = [item for item in records if _score_eligible(item)]
    scored = len(scored_records)
    context_only = len(records) - scored
    baseline_net = sum(_net_pnl(item) or 0.0 for item in scored_records)
    baseline_usdt = sum(_net_usdt(item) or 0.0 for item in scored_records)
    baseline_winners = sum((_net_pnl(item) or 0.0) > 0 for item in scored_records)
    baseline_losses = sum((_net_pnl(item) or 0.0) < 0 for item in scored_records)
    print("TREND-SOL | cohort pressure replay")
    print(f"Sample: real entries={len(records)} | scored outcomes={scored} | context/unresolved={context_only}")
    print(
        f"Baseline: net sum={baseline_net:+.2f}% | net={baseline_usdt:+.4f} USDT | "
        f"winners={baseline_winners} | losses={baseline_losses}"
    )
    print("Current price proxy: candidate market fill; existing PnL is reconstructed at that timestamp.")
    print("Static keeps historical occupancy; sequential removes blocked actual entries but creates no missing signals.")
    print()
    print("Rules:")
    for rule in rules:
        print(
            f"  {rule.name}: min_open={rule.min_open_positions} | underwater>={rule.underwater_ratio:.0%} | "
            f"oldest>={rule.oldest_age_minutes:g}m | below_avg>={rule.below_average_entry_atr:g} new ATR"
        )
    print()
    print(
        f"{'mode':10} {'rule':10} {'checks':>6} {'blocked':>7} {'b4':>4} {'b5+':>4} "
        f"{'cutW':>5} {'saveL':>5} {'saveHS':>6} {'unknown':>7} {'delta_pp':>9} {'delta_usdt':>11} "
        f"{'saved_fee':>10} {'episodes':>8} {'score +/-/=':>11}"
    )
    for mode in modes:
        for rule in rules:
            selected = [item for item in decisions if item.mode == mode and item.rule == rule]
            blocked = [item for item in selected if item.blocked]
            scored_blocked = [item for item in blocked if item.scored]
            net_values = [_net_pnl(item.record) for item in scored_blocked]
            net_values = [value for value in net_values if value is not None]
            delta_pp = -sum(net_values)
            delta_usdt = -sum(_net_usdt(item.record) or 0.0 for item in scored_blocked)
            fees_usdt = sum(_fees_usdt(item.record) or 0.0 for item in scored_blocked)
            episodes = _decision_episodes(scored_blocked, episode_gap_hours)
            improved = sum(1 for group in episodes if -sum(_net_pnl(item.record) or 0 for item in group) > 1e-9)
            worse = sum(1 for group in episodes if -sum(_net_pnl(item.record) or 0 for item in group) < -1e-9)
            flat = len(episodes) - improved - worse
            checks = sum(1 for item in selected if item.metrics.open_positions >= rule.min_open_positions)
            print(
                f"{mode:10} {rule.name:10} {checks:6d} {len(blocked):7d} "
                f"{sum(item.metrics.open_positions == 3 for item in blocked):4d} "
                f"{sum(item.metrics.open_positions >= 4 for item in blocked):4d} "
                f"{sum((_net_pnl(item.record) or 0) > 0 for item in scored_blocked):5d} "
                f"{sum((_net_pnl(item.record) or 0) < 0 for item in scored_blocked):5d} "
                f"{sum(str(item.record.get('exit_reason') or '') == 'HARD_STOP' for item in scored_blocked):6d} "
                f"{sum(not item.scored for item in blocked):7d} {delta_pp:+9.2f} {delta_usdt:+11.4f} "
                f"{fees_usdt:10.4f} {len(episodes):8d} {improved:3d}/{worse}/{flat:<3d}"
            )
    print()
    print("cutW=net winners blocked | saveL=net losses avoided | saveHS=HARD_STOPs avoided")
    print("delta_pp=sum of blocked net returns with the sign reversed; positive improves the historical sample.")
    print("saved_fee=estimated fees avoided in USDT; episode score groups blocked entries by the configured gap.")
    if sizing_rules:
        _print_sizing_report(
            sizing_decisions,
            sizing_rules,
            baseline_usdt,
            operational_balance_usdt,
            episode_gap_hours,
        )
    if not detail:
        return
    print()
    print("Blocked trade detail:")
    for item in decisions:
        if not item.blocked:
            continue
        record = item.record
        metrics = item.metrics
        print(
            f"  {item.mode}/{item.rule.name} {_fmt_ts(record.get('opened_at'))} "
            f"pair={record.get('pair_id')} attempt={metrics.open_positions + 1} "
            f"underwater={metrics.underwater_positions}/{metrics.open_positions} "
            f"oldest={metrics.oldest_age_minutes:.0f}m below_avg={_fmt_optional(metrics.below_average_entry_atr)}ATR "
            f"outcome={record.get('exit_reason') or 'OPEN'} net={_fmt_signed(_net_pnl(record))} "
            f"strategy={record.get('strategy_version') or 'state'} "
            f"scored={'yes' if item.scored else 'no'}"
        )
    if sizing_rules:
        print()
        print("Reduced-size trade detail:")
        for item in sizing_decisions:
            if not item.reduced:
                continue
            record = item.record
            metrics = item.metrics
            actual = _net_usdt(record)
            hypothetical = actual * item.rule.size_factor if actual is not None else None
            delta = hypothetical - actual if actual is not None and hypothetical is not None else None
            print(
                f"  {item.rule.name} {_fmt_ts(record.get('opened_at'))} "
                f"pair={record.get('pair_id')} attempt={metrics.open_positions + 1} "
                f"negative={metrics.negative_positions}/{metrics.open_positions} "
                f"factor={item.rule.size_factor:.2f} outcome={record.get('exit_reason') or 'OPEN'} "
                f"net={_fmt_signed(_net_pnl(record))} actual={_fmt_usdt(actual)} "
                f"hyp={_fmt_usdt(hypothetical)} delta={_fmt_usdt(delta)} "
                f"scored={'yes' if item.scored else 'no'}"
            )


def _print_sizing_report(
    decisions: Sequence[SizingDecision],
    rules: Sequence[SizingRule],
    baseline_usdt: float,
    operational_balance_usdt: float,
    episode_gap_hours: float,
) -> None:
    print()
    print("Degressive sizing study (all approved entries remain admitted):")
    print(f"Operational balance: {operational_balance_usdt:.2f} USDT")
    print("Sizing rules:")
    for rule in rules:
        print(
            f"  {rule.name}: min_open={rule.min_open_positions} | "
            f"position_pnl<={rule.negative_threshold_pct:.2f}% | "
            f"negative>={rule.negative_fraction:.0%} | size={rule.size_factor:.0%}"
        )
    print()
    print(
        f"{'rule':13} {'checks':>6} {'reduced':>7} {'b4':>4} {'b5+':>4} "
        f"{'redW':>5} {'redL':>5} {'redHS':>6} {'saveL':>9} {'cutW':>9} "
        f"{'feeSave':>8} {'delta':>9} {'bal_pp':>8} {'hyp_net':>9} "
        f"{'episodes':>8} {'score +/-/=':>11}"
    )
    for rule in rules:
        selected = [item for item in decisions if item.rule == rule]
        reduced = [item for item in selected if item.reduced]
        scored_reduced = [item for item in reduced if item.scored]
        economics = sizing_economics(scored_reduced, operational_balance_usdt)
        episodes = _sizing_episodes(scored_reduced, episode_gap_hours)
        episode_deltas = [
            sum(
                ((_net_usdt(item.record) or 0.0) * item.rule.size_factor)
                - (_net_usdt(item.record) or 0.0)
                for item in group
            )
            for group in episodes
        ]
        improved = sum(value > 1e-9 for value in episode_deltas)
        worse = sum(value < -1e-9 for value in episode_deltas)
        flat = len(episodes) - improved - worse
        checks = sum(
            item.metrics.open_positions >= rule.min_open_positions
            for item in selected
        )
        print(
            f"{rule.name:13} {checks:6d} {len(reduced):7d} "
            f"{sum(item.metrics.open_positions == 3 for item in reduced):4d} "
            f"{sum(item.metrics.open_positions >= 4 for item in reduced):4d} "
            f"{sum((_net_pnl(item.record) or 0.0) > 0 for item in scored_reduced):5d} "
            f"{sum((_net_pnl(item.record) or 0.0) < 0 for item in scored_reduced):5d} "
            f"{sum(str(item.record.get('exit_reason') or '') == 'HARD_STOP' for item in scored_reduced):6d} "
            f"{economics.saved_losses_usdt:9.4f} {economics.foregone_winners_usdt:9.4f} "
            f"{economics.saved_fees_usdt:8.4f} {economics.delta_usdt:+9.4f} "
            f"{economics.balance_delta_pct:+8.3f} "
            f"{baseline_usdt + economics.delta_usdt:+9.4f} {len(episodes):8d} "
            f"{improved:3d}/{worse}/{flat:<3d}"
        )
    print()
    print("redW/redL=net winners/losses reduced | saveL/cutW/feeSave/delta/hyp_net are USDT")
    print("bal_pp=delta as percentage points of operational balance; sizing never frees a slot.")


def _decision_episodes(
    decisions: Sequence[ReplayDecision],
    gap_hours: float,
) -> list[list[ReplayDecision]]:
    ordered = sorted(decisions, key=lambda item: _opened_at(item.record) or datetime.min.replace(tzinfo=timezone.utc))
    output: list[list[ReplayDecision]] = []
    for item in ordered:
        opened = _opened_at(item.record)
        if opened is None:
            continue
        previous = _opened_at(output[-1][-1].record) if output else None
        if previous is None or opened - previous > timedelta(hours=gap_hours):
            output.append([item])
        else:
            output[-1].append(item)
    return output


def _sizing_episodes(
    decisions: Sequence[SizingDecision],
    gap_hours: float,
) -> list[list[SizingDecision]]:
    ordered = sorted(decisions, key=lambda item: _opened_at(item.record) or datetime.min.replace(tzinfo=timezone.utc))
    output: list[list[SizingDecision]] = []
    for item in ordered:
        opened = _opened_at(item.record)
        if opened is None:
            continue
        previous = _opened_at(output[-1][-1].record) if output else None
        if previous is None or opened - previous > timedelta(hours=gap_hours):
            output.append([item])
        else:
            output[-1].append(item)
    return output


def _rules_from_config(config: Dict[str, Any]) -> list[CohortRule]:
    values = _study_config(config).get("rules") or []
    rules = []
    for item in values:
        if not isinstance(item, dict):
            continue
        rules.append(
            CohortRule(
                name=str(item.get("name") or f"RULE_{len(rules) + 1}").upper(),
                min_open_positions=int(item.get("min_open_positions", 3)),
                underwater_ratio=float(item.get("underwater_ratio", 0.75)),
                oldest_age_minutes=float(item.get("oldest_age_minutes", 120)),
                below_average_entry_atr=float(item.get("below_average_entry_atr", 1.0)),
            )
        )
    if rules:
        return rules
    return [
        CohortRule("SENSITIVE", 3, 0.66, 60, 0.5),
        CohortRule("BASE", 3, 0.75, 120, 1.0),
        CohortRule("SELECTIVE", 3, 1.0, 240, 2.0),
    ]


def _study_config(config: Dict[str, Any]) -> Dict[str, Any]:
    instrumentation = config.get("instrumentation") if isinstance(config.get("instrumentation"), dict) else {}
    value = instrumentation.get("cohort_guard_study")
    return value if isinstance(value, dict) else {}


def _sizing_rules_from_config(config: Dict[str, Any]) -> list[SizingRule]:
    values = _sizing_study_config(config).get("rules") or []
    rules = []
    for item in values:
        if not isinstance(item, dict):
            continue
        rules.append(
            SizingRule(
                name=str(item.get("name") or f"SIZING_{len(rules) + 1}").upper(),
                min_open_positions=int(item.get("min_open_positions", 3)),
                negative_threshold_pct=float(item.get("negative_threshold_pct", -0.3)),
                negative_fraction=float(item.get("negative_fraction", 0.66)),
                size_factor=float(item.get("size_factor", 0.5)),
            )
        )
    if rules:
        return rules
    return [
        SizingRule("CLAUDE_HALF", 3, -0.3, 0.66, 0.5),
        SizingRule("NEG_02_HALF", 3, -0.2, 0.66, 0.5),
        SizingRule("NEG_05_HALF", 3, -0.5, 0.66, 0.5),
        SizingRule("FRAC75_HALF", 3, -0.3, 0.75, 0.5),
        SizingRule("FRAC100_HALF", 3, -0.3, 1.0, 0.5),
        SizingRule("CLAUDE_QTR", 3, -0.3, 0.66, 0.25),
        SizingRule("CLAUDE_75", 3, -0.3, 0.66, 0.75),
    ]


def _sizing_study_config(config: Dict[str, Any]) -> Dict[str, Any]:
    instrumentation = config.get("instrumentation") if isinstance(config.get("instrumentation"), dict) else {}
    value = instrumentation.get("cohort_sizing_study")
    return value if isinstance(value, dict) else {}


def _operational_balance(config: Dict[str, Any]) -> float:
    capital = config.get("capital") if isinstance(config.get("capital"), dict) else {}
    value = _float(capital.get("operational_balance_usdt"))
    return value if value is not None and value > 0 else 100.0


def _parse_rule(value: str) -> CohortRule:
    parts = [item.strip() for item in value.split("/")]
    if len(parts) != 5:
        raise ValueError("rule must be name/min_open/underwater_ratio/oldest_age_minutes/below_average_entry_atr")
    return CohortRule(parts[0].upper(), int(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]))


def _parse_sizing_rule(value: str) -> SizingRule:
    parts = [item.strip() for item in value.split("/")]
    if len(parts) != 5:
        raise ValueError("sizing rule must be name/min_open/negative_threshold_pct/negative_fraction/size_factor")
    return SizingRule(parts[0].upper(), int(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]))


def _validate_rule(rule: CohortRule) -> None:
    if rule.min_open_positions < 1:
        raise ValueError(f"{rule.name}: min_open_positions must be at least 1")
    if not 0 <= rule.underwater_ratio <= 1:
        raise ValueError(f"{rule.name}: underwater_ratio must be between 0 and 1")
    if rule.oldest_age_minutes < 0:
        raise ValueError(f"{rule.name}: oldest_age_minutes cannot be negative")
    if rule.below_average_entry_atr < 0:
        raise ValueError(f"{rule.name}: below_average_entry_atr cannot be negative")


def _validate_sizing_rule(rule: SizingRule) -> None:
    if rule.min_open_positions < 1:
        raise ValueError(f"{rule.name}: min_open_positions must be at least 1")
    if rule.negative_threshold_pct > 0:
        raise ValueError(f"{rule.name}: negative_threshold_pct must be zero or negative")
    if not 0 <= rule.negative_fraction <= 1:
        raise ValueError(f"{rule.name}: negative_fraction must be between 0 and 1")
    if not 0 < rule.size_factor <= 1:
        raise ValueError(f"{rule.name}: size_factor must be greater than zero and at most 1")


def _record_sort_key(record: Dict[str, Any]) -> tuple[datetime, int, str]:
    return (
        _opened_at(record) or datetime.max.replace(tzinfo=timezone.utc),
        int(_float(record.get("position_id")) or 0),
        str(record.get("pair_id") or ""),
    )


def _opened_at(record: Dict[str, Any]) -> Optional[datetime]:
    return _parse_ts(record.get("opened_at") or record.get("open_ts"))


def _closed_at(record: Dict[str, Any]) -> Optional[datetime]:
    return _parse_ts(record.get("closed_at") or record.get("close_ts"))


def _net_pnl(record: Dict[str, Any]) -> Optional[float]:
    value = _float(record.get("net_pnl_pct"))
    if value is not None:
        return value
    gross = _float(record.get("gross_pnl_pct"))
    fees = _float(record.get("estimated_fees_pct"))
    return gross - (fees or 0.0) if gross is not None else None


def _net_usdt(record: Dict[str, Any]) -> Optional[float]:
    net = _net_pnl(record)
    notional = _position_notional(record)
    return notional * net / 100 if net is not None and notional is not None else None


def _fees_usdt(record: Dict[str, Any]) -> Optional[float]:
    fees = _float(record.get("estimated_fees_pct"))
    notional = _position_notional(record)
    return notional * fees / 100 if fees is not None and notional is not None else None


def _position_notional(record: Dict[str, Any]) -> Optional[float]:
    notional = _float(record.get("position_notional_usdt"))
    if notional is not None:
        return notional
    entry = _float(record.get("entry_price"))
    qty = _float(record.get("qty")) or _float(record.get("quantity"))
    return entry * qty if entry is not None and qty is not None else None


def _load_config(path: Path) -> Dict[str, Any]:
    if not path.exists() or yaml is None:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _read_jsonl(path: Path) -> list[Dict[str, Any]]:
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


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


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


def _float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _fmt_ts(value: Any) -> str:
    parsed = _parse_ts(value)
    return parsed.strftime("%d/%m %H:%M") if parsed else "n/a"


def _fmt_optional(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _fmt_signed(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.2f}%"


def _fmt_usdt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.4f}USDT"


if __name__ == "__main__":
    main()
