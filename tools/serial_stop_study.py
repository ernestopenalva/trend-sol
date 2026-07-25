from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.cohort_study import (
    _closed_at,
    _dedupe_records,
    _float,
    _fmt_ts,
    _is_real_bot_position,
    _load_config,
    _normalize_record,
    _opened_at,
    _operational_balance,
    _position_notional,
    _read_jsonl,
    _record_sort_key,
    _score_eligible,
)


@dataclass(frozen=True)
class RiskBudgetRule:
    name: str
    budget_pct: float


@dataclass(frozen=True)
class RiskBudgetDecision:
    rule: RiskBudgetRule
    record: Dict[str, Any]
    factor: float
    risk_before_usdt: float
    full_trade_risk_usdt: Optional[float]
    risk_after_usdt: float

    @property
    def scored(self) -> bool:
        return _score_eligible(self.record)


@dataclass(frozen=True)
class PriceBandRule:
    name: str
    band_pct: float
    max_positions: int


@dataclass(frozen=True)
class PriceBandDecision:
    rule: PriceBandRule
    record: Dict[str, Any]
    nearby_positions: int
    blocked: bool

    @property
    def scored(self) -> bool:
        return _score_eligible(self.record)


def main() -> None:
    args = _parse_args()
    config = _load_config(Path(args.config))
    study = _study_config(config)
    records = _load_records(
        [Path(item) for item in args.ledger],
        include_market_shadow=args.include_market_shadow,
    )
    records = [
        item
        for item in records
        if args.profile == "all"
        or not item.get("profile")
        or str(item.get("profile")) == args.profile
    ]
    balance = (
        float(args.operational_balance_usdt)
        if args.operational_balance_usdt is not None
        else _operational_balance(config)
    )
    min_notional = float(
        args.min_notional_usdt
        if args.min_notional_usdt is not None
        else study.get("min_notional_usdt", 10)
    )
    risk_rules = (
        [RiskBudgetRule(f"RISK_{value:g}", value) for value in args.risk_budget_pct]
        if args.risk_budget_pct
        else _risk_rules(study)
    )
    band_rules = (
        [_parse_band_rule(value) for value in args.price_band_rule]
        if args.price_band_rule
        else _band_rules(study)
    )
    episode_gap = float(
        args.episode_gap_hours
        if args.episode_gap_hours is not None
        else study.get("episode_gap_hours", 6)
    )
    risk_decisions = run_risk_budget_replay(
        records, risk_rules, balance, min_notional, args.include_market_shadow
    )
    dynamic_risk_decisions = []
    if args.events:
        events = [item for path in args.events for item in _read_jsonl(Path(path))]
        dynamic_risk_decisions = run_dynamic_risk_budget_replay(
            records, events, risk_rules, balance, min_notional, args.include_market_shadow
        )
    band_decisions = run_price_band_replay(records, band_rules, args.include_market_shadow)
    _print_report(
        records,
        risk_decisions,
        risk_rules,
        band_decisions,
        band_rules,
        balance,
        min_notional,
        episode_gap,
        args.detail,
        args.include_market_shadow,
    )
    if dynamic_risk_decisions:
        _print_dynamic_risk_report(
            records, dynamic_risk_decisions, risk_rules, episode_gap, args.detail,
            args.include_market_shadow,
        )


def run_risk_budget_replay(
    records: Sequence[Dict[str, Any]],
    rules: Sequence[RiskBudgetRule],
    operational_balance_usdt: float,
    min_notional_usdt: float,
    include_market_shadow: bool = False,
) -> list[RiskBudgetDecision]:
    ordered = _normalized_study_records(records, include_market_shadow)
    output: list[RiskBudgetDecision] = []
    for rule in rules:
        if rule.budget_pct <= 0:
            raise ValueError(f"{rule.name}: budget_pct must be positive")
        budget_usdt = operational_balance_usdt * rule.budget_pct / 100
        active: list[tuple[Dict[str, Any], float]] = []
        for candidate in ordered:
            opened = _opened_at(candidate)
            if opened is None:
                continue
            active = [
                (item, factor)
                for item, factor in active
                if _closed_at(item) is None or opened < _closed_at(item)
            ]
            risk_before = sum(
                (_full_trade_risk_usdt(item) or 0.0) * factor
                for item, factor in active
            )
            full_risk = _full_trade_risk_usdt(candidate)
            factor = 1.0
            if full_risk is not None and full_risk > 0:
                factor = min(1.0, max(0.0, (budget_usdt - risk_before) / full_risk))
                notional = _position_notional(candidate)
                if notional is None or notional * factor + 1e-12 < min_notional_usdt:
                    factor = 0.0
            risk_after = risk_before + (full_risk or 0.0) * factor
            decision = RiskBudgetDecision(
                rule=rule,
                record=candidate,
                factor=factor,
                risk_before_usdt=risk_before,
                full_trade_risk_usdt=full_risk,
                risk_after_usdt=risk_after,
            )
            output.append(decision)
            if factor > 0:
                active.append((candidate, factor))
    return output


def run_dynamic_risk_budget_replay(
    records: Sequence[Dict[str, Any]],
    events: Sequence[Dict[str, Any]],
    rules: Sequence[RiskBudgetRule],
    operational_balance_usdt: float,
    min_notional_usdt: float,
    include_market_shadow: bool = False,
) -> list[RiskBudgetDecision]:
    """Replay risk budgets, releasing reservation as historical stops are raised."""
    ordered = _normalized_study_records(records, include_market_shadow)
    known_pairs = {str(item.get("pair_id")) for item in ordered}
    lifecycle = sorted(
        (
            (timestamp, index, event)
            for index, event in enumerate(events)
            if str(event.get("pair_id")) in known_pairs
            if (timestamp := _timestamp(event.get("ts"))) is not None
        ),
        key=lambda item: (item[0], item[1]),
    )
    output: list[RiskBudgetDecision] = []
    for rule in rules:
        if rule.budget_pct <= 0:
            raise ValueError(f"{rule.name}: budget_pct must be positive")
        budget_usdt = operational_balance_usdt * rule.budget_pct / 100
        active: dict[str, tuple[Dict[str, Any], float, float]] = {}
        event_index = 0
        for candidate in ordered:
            opened = _opened_at(candidate)
            if opened is None:
                continue
            while event_index < len(lifecycle) and lifecycle[event_index][0] <= opened:
                _, _, event = lifecycle[event_index]
                _apply_lifecycle_event(active, event)
                event_index += 1
            active = {
                pair_id: value
                for pair_id, value in active.items()
                if _closed_at(value[0]) is None or opened < _closed_at(value[0])
            }
            risk_before = sum(risk for _, _, risk in active.values())
            full_risk = _full_trade_risk_usdt(candidate)
            factor = 1.0
            if full_risk is not None and full_risk > 0:
                factor = min(1.0, max(0.0, (budget_usdt - risk_before) / full_risk))
                notional = _position_notional(candidate)
                if notional is None or notional * factor + 1e-12 < min_notional_usdt:
                    factor = 0.0
            risk_after = risk_before + (full_risk or 0.0) * factor
            output.append(RiskBudgetDecision(rule, candidate, factor, risk_before, full_risk, risk_after))
            if factor > 0:
                active[str(candidate.get("pair_id"))] = (candidate, factor, (full_risk or 0.0) * factor)
    return output


def _apply_lifecycle_event(
    active: dict[str, tuple[Dict[str, Any], float, float]], event: Dict[str, Any]
) -> None:
    pair_id = str(event.get("pair_id"))
    current = active.get(pair_id)
    if current is None:
        return
    record, factor, risk = current
    if str(event.get("event") or "") == "CLOSE":
        active.pop(pair_id, None)
        return
    effective_stop = _float(event.get("effective_stop"))
    if effective_stop is not None:
        active[pair_id] = (record, factor, _risk_to_stop_usdt(record, effective_stop) * factor)


def run_price_band_replay(
    records: Sequence[Dict[str, Any]],
    rules: Sequence[PriceBandRule],
    include_market_shadow: bool = False,
) -> list[PriceBandDecision]:
    ordered = _normalized_study_records(records, include_market_shadow)
    output: list[PriceBandDecision] = []
    for rule in rules:
        if rule.band_pct <= 0 or rule.max_positions < 1:
            raise ValueError(f"{rule.name}: band_pct and max_positions must be positive")
        active: list[Dict[str, Any]] = []
        for candidate in ordered:
            opened = _opened_at(candidate)
            if opened is None:
                continue
            active = [
                item
                for item in active
                if _closed_at(item) is None or opened < _closed_at(item)
            ]
            candidate_price = _float(candidate.get("entry_price")) or 0.0
            nearby = sum(
                _entry_distance_pct(candidate_price, _float(item.get("entry_price"))) <= rule.band_pct + 1e-12
                for item in active
            )
            blocked = nearby >= rule.max_positions
            output.append(PriceBandDecision(rule, candidate, nearby, blocked))
            if not blocked:
                active.append(candidate)
    return output


def _print_report(
    records: Sequence[Dict[str, Any]],
    risk_decisions: Sequence[RiskBudgetDecision],
    risk_rules: Sequence[RiskBudgetRule],
    band_decisions: Sequence[PriceBandDecision],
    band_rules: Sequence[PriceBandRule],
    balance: float,
    min_notional: float,
    episode_gap_hours: float,
    detail: bool,
    include_market_shadow: bool,
) -> None:
    scored = [item for item in records if _score_eligible(item) and _is_study_position(item, include_market_shadow)]
    baseline_usdt = sum(_net_usdt(item) or 0.0 for item in scored)
    print("TREND-SOL | serial hard-stop study")
    print(
        f"Sample: {'real + market-shadow' if include_market_shadow else 'real'} entries={len(records)} | scored outcomes={len(scored)} | "
        f"baseline net={baseline_usdt:+.4f} USDT"
    )
    print(
        "Risk-budget fidelity: conservative; each admitted position reserves hard-stop risk "
        "until its historical close."
    )
    print(
        "The replay does not release risk at BE/PL and does not invent signals made possible "
        "by changed occupancy."
    )
    print()
    print(
        f"Risk budget sizing | operational balance={balance:.2f} USDT | "
        f"minimum notional={min_notional:.2f} USDT"
    )
    print(
        f"{'rule':10} {'changed':>7} {'scaled':>6} {'blocked':>7} {'chgW':>5} "
        f"{'chgL':>5} {'HS':>4} {'saveL':>9} {'cutW':>9} {'delta':>9} "
        f"{'hyp_net':>9} {'episodes':>8} {'score +/-/=':>11}"
    )
    for rule in risk_rules:
        selected = [item for item in risk_decisions if item.rule == rule and item.factor < 1 - 1e-12]
        scored_selected = [item for item in selected if item.scored]
        saved_losses, cut_winners, delta = _factor_economics(scored_selected)
        episodes = _risk_episodes(scored_selected, episode_gap_hours)
        scores = _episode_score(
            [
                sum(_decision_delta_usdt(item) for item in group)
                for group in episodes
            ]
        )
        print(
            f"{rule.name:10} {len(selected):7d} "
            f"{sum(0 < item.factor < 1 for item in selected):6d} "
            f"{sum(item.factor == 0 for item in selected):7d} "
            f"{sum((_net_usdt(item.record) or 0.0) > 0 for item in scored_selected):5d} "
            f"{sum((_net_usdt(item.record) or 0.0) < 0 for item in scored_selected):5d} "
            f"{sum(str(item.record.get('exit_reason') or '') == 'HARD_STOP' for item in scored_selected):4d} "
            f"{saved_losses:9.4f} {cut_winners:9.4f} {delta:+9.4f} "
            f"{baseline_usdt + delta:+9.4f} {len(episodes):8d} "
            f"{scores[0]:3d}/{scores[1]}/{scores[2]:<3d}"
        )
    print()
    print("Price-band admission (sequential replay):")
    print(
        f"{'rule':13} {'blocked':>7} {'cutW':>5} {'saveL':>5} {'saveHS':>6} "
        f"{'delta':>9} {'hyp_net':>9} {'episodes':>8} {'score +/-/=':>11}"
    )
    for rule in band_rules:
        blocked = [item for item in band_decisions if item.rule == rule and item.blocked]
        scored_blocked = [item for item in blocked if item.scored]
        delta = -sum(_net_usdt(item.record) or 0.0 for item in scored_blocked)
        episodes = _band_episodes(scored_blocked, episode_gap_hours)
        scores = _episode_score(
            [
                -sum(_net_usdt(item.record) or 0.0 for item in group)
                for group in episodes
            ]
        )
        print(
            f"{rule.name:13} {len(blocked):7d} "
            f"{sum((_net_usdt(item.record) or 0.0) > 0 for item in scored_blocked):5d} "
            f"{sum((_net_usdt(item.record) or 0.0) < 0 for item in scored_blocked):5d} "
            f"{sum(str(item.record.get('exit_reason') or '') == 'HARD_STOP' for item in scored_blocked):6d} "
            f"{delta:+9.4f} {baseline_usdt + delta:+9.4f} {len(episodes):8d} "
            f"{scores[0]:3d}/{scores[1]}/{scores[2]:<3d}"
        )
    print()
    print("saveL/cutW/delta/hyp_net are USDT; positive delta improves the historical sample.")
    if not detail:
        return
    print()
    print("Changed risk-budget entries:")
    for item in risk_decisions:
        if item.factor >= 1 - 1e-12:
            continue
        print(
            f"  {item.rule.name} {_fmt_ts(item.record.get('opened_at'))} "
            f"pair={item.record.get('pair_id')} factor={item.factor:.3f} "
            f"risk={item.risk_before_usdt:.4f}->{item.risk_after_usdt:.4f}USDT "
            f"outcome={item.record.get('exit_reason') or 'OPEN'} "
            f"delta={_decision_delta_usdt(item):+.4f}USDT scored={'yes' if item.scored else 'no'}"
        )


def _print_dynamic_risk_report(
    records: Sequence[Dict[str, Any]],
    decisions: Sequence[RiskBudgetDecision],
    rules: Sequence[RiskBudgetRule],
    episode_gap_hours: float,
    detail: bool,
    include_market_shadow: bool,
) -> None:
    scored = [item for item in records if _score_eligible(item) and _is_study_position(item, include_market_shadow)]
    baseline_usdt = sum(_net_usdt(item) or 0.0 for item in scored)
    print()
    print("Dynamic risk-budget replay (releases reservation as effective_stop rises):")
    print(
        f"{'rule':10} {'changed':>7} {'scaled':>6} {'blocked':>7} {'chgW':>5} "
        f"{'chgL':>5} {'HS':>4} {'saveL':>9} {'cutW':>9} {'delta':>9} "
        f"{'hyp_net':>9} {'episodes':>8} {'score +/-/=':>11}"
    )
    for rule in rules:
        selected = [item for item in decisions if item.rule == rule and item.factor < 1 - 1e-12]
        scored_selected = [item for item in selected if item.scored]
        saved_losses, cut_winners, delta = _factor_economics(scored_selected)
        episodes = _risk_episodes(scored_selected, episode_gap_hours)
        scores = _episode_score([sum(_decision_delta_usdt(item) for item in group) for group in episodes])
        print(
            f"{rule.name:10} {len(selected):7d} "
            f"{sum(0 < item.factor < 1 for item in selected):6d} "
            f"{sum(item.factor == 0 for item in selected):7d} "
            f"{sum((_net_usdt(item.record) or 0.0) > 0 for item in scored_selected):5d} "
            f"{sum((_net_usdt(item.record) or 0.0) < 0 for item in scored_selected):5d} "
            f"{sum(str(item.record.get('exit_reason') or '') == 'HARD_STOP' for item in scored_selected):4d} "
            f"{saved_losses:9.4f} {cut_winners:9.4f} {delta:+9.4f} "
            f"{baseline_usdt + delta:+9.4f} {len(episodes):8d} "
            f"{scores[0]:3d}/{scores[1]}/{scores[2]:<3d}"
        )
    if not detail:
        return
    print("Changed dynamic-risk entries:")
    for item in decisions:
        if item.factor >= 1 - 1e-12:
            continue
        print(
            f"  {item.rule.name} {_fmt_ts(item.record.get('opened_at'))} "
            f"pair={item.record.get('pair_id')} factor={item.factor:.3f} "
            f"risk={item.risk_before_usdt:.4f}->{item.risk_after_usdt:.4f}USDT "
            f"outcome={item.record.get('exit_reason') or 'OPEN'} "
            f"delta={_decision_delta_usdt(item):+.4f}USDT scored={'yes' if item.scored else 'no'}"
        )


def _factor_economics(
    decisions: Sequence[RiskBudgetDecision],
) -> tuple[float, float, float]:
    saved_losses = 0.0
    cut_winners = 0.0
    for item in decisions:
        actual = _net_usdt(item.record) or 0.0
        if actual < 0:
            saved_losses += -actual * (1 - item.factor)
        elif actual > 0:
            cut_winners += actual * (1 - item.factor)
    return saved_losses, cut_winners, saved_losses - cut_winners


def _decision_delta_usdt(item: RiskBudgetDecision) -> float:
    actual = _net_usdt(item.record) or 0.0
    return actual * item.factor - actual


def _full_trade_risk_usdt(record: Dict[str, Any]) -> Optional[float]:
    entry = _float(record.get("entry_price"))
    stop = _float(record.get("hard_stop_price"))
    notional = _position_notional(record)
    fees_pct = _float(record.get("estimated_fees_pct")) or 0.0
    if entry is None or entry <= 0 or stop is None or notional is None:
        return None
    return _risk_to_stop_usdt(record, stop)


def _risk_to_stop_usdt(record: Dict[str, Any], stop: float) -> float:
    entry = _float(record.get("entry_price"))
    notional = _position_notional(record)
    fees_pct = _float(record.get("estimated_fees_pct")) or 0.0
    if entry is None or entry <= 0 or notional is None:
        return 0.0
    gross_loss_pct = (entry - stop) / entry * 100
    return notional * max(0.0, gross_loss_pct + fees_pct) / 100


def _timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _entry_distance_pct(candidate: float, existing: Optional[float]) -> float:
    if candidate <= 0 or existing is None or existing <= 0:
        return float("inf")
    return abs(candidate - existing) / existing * 100


def _net_usdt(record: Dict[str, Any]) -> Optional[float]:
    net_pct = _float(record.get("net_pnl_pct"))
    if net_pct is None:
        gross = _float(record.get("gross_pnl_pct"))
        fees = _float(record.get("estimated_fees_pct")) or 0.0
        net_pct = gross - fees if gross is not None else None
    notional = _position_notional(record)
    return notional * net_pct / 100 if net_pct is not None and notional is not None else None


def _normalized_study_records(
    records: Sequence[Dict[str, Any]],
    include_market_shadow: bool = False,
) -> list[Dict[str, Any]]:
    normalized = [_normalize_record(item) for item in records]
    return sorted(
        _dedupe_records(
            item for item in normalized if _is_study_position(item, include_market_shadow)
        ),
        key=_record_sort_key,
    )


def _is_study_position(record: Dict[str, Any], include_market_shadow: bool) -> bool:
    return _is_real_bot_position(record) or (
        include_market_shadow
        and str(record.get("position_type") or "") == "MARKET_SHADOW"
    )


def _load_records(paths: Sequence[Path], include_market_shadow: bool = False) -> list[Dict[str, Any]]:
    return _normalized_study_records(
        [item for path in paths for item in _read_jsonl(path)], include_market_shadow
    )


def _risk_episodes(
    decisions: Sequence[RiskBudgetDecision],
    gap_hours: float,
) -> list[list[RiskBudgetDecision]]:
    return _episodes(decisions, gap_hours)


def _band_episodes(
    decisions: Sequence[PriceBandDecision],
    gap_hours: float,
) -> list[list[PriceBandDecision]]:
    return _episodes(decisions, gap_hours)


def _episodes(items: Sequence[Any], gap_hours: float) -> list[list[Any]]:
    ordered = sorted(items, key=lambda item: _opened_at(item.record) or datetime.min.replace(tzinfo=timezone.utc))
    output: list[list[Any]] = []
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


def _episode_score(values: Sequence[float]) -> tuple[int, int, int]:
    improved = sum(value > 1e-9 for value in values)
    worse = sum(value < -1e-9 for value in values)
    return improved, worse, len(values) - improved - worse


def _study_config(config: Dict[str, Any]) -> Dict[str, Any]:
    instrumentation = config.get("instrumentation") if isinstance(config.get("instrumentation"), dict) else {}
    value = instrumentation.get("serial_stop_study")
    return value if isinstance(value, dict) else {}


def _risk_rules(study: Dict[str, Any]) -> list[RiskBudgetRule]:
    values = study.get("risk_budgets_pct") or [1.0, 1.5, 2.0]
    return [RiskBudgetRule(f"RISK_{float(value):g}", float(value)) for value in values]


def _band_rules(study: Dict[str, Any]) -> list[PriceBandRule]:
    values = study.get("price_band_rules") or [
        {"name": "BAND025_MAX2", "band_pct": 0.25, "max_positions": 2},
        {"name": "BAND050_MAX2", "band_pct": 0.5, "max_positions": 2},
        {"name": "BAND050_MAX3", "band_pct": 0.5, "max_positions": 3},
        {"name": "BAND100_MAX3", "band_pct": 1.0, "max_positions": 3},
    ]
    return [
        PriceBandRule(
            str(item.get("name") or f"BAND_{index + 1}").upper(),
            float(item.get("band_pct", 0.5)),
            int(item.get("max_positions", 3)),
        )
        for index, item in enumerate(values)
        if isinstance(item, dict)
    ]


def _parse_band_rule(value: str) -> PriceBandRule:
    parts = [item.strip() for item in value.split("/")]
    if len(parts) != 3:
        raise ValueError("price band rule must be name/band_pct/max_positions")
    return PriceBandRule(parts[0].upper(), float(parts[1]), int(parts[2]))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay offline de risco coletivo e concentracao; nunca envia ordens nem altera o bot."
    )
    parser.add_argument("--ledger", action="append", required=True, help="Ledger JSONL; pode ser repetido.")
    parser.add_argument(
        "--events", action="append",
        help="JSONL de eventos de trade; ativa replay dinamico que libera risco conforme effective_stop.",
    )
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument(
        "--include-market-shadow", action="store_true",
        help="Inclui somente MARKET_SHADOW alem das posicoes reais; mantem exclusao como padrao.",
    )
    parser.add_argument("--risk-budget-pct", action="append", type=float)
    parser.add_argument("--price-band-rule", action="append")
    parser.add_argument("--operational-balance-usdt", type=float)
    parser.add_argument("--min-notional-usdt", type=float)
    parser.add_argument("--episode-gap-hours", type=float)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    return parser.parse_args()


if __name__ == "__main__":
    main()
