from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

@dataclass
class Outcome:
    pair_id: str
    rule: str
    triggered_at: datetime
    baseline_net: float
    simulated_net: float
    slot_hours_freed: float
    fidelity: str
    phantom: bool

    @property
    def delta(self) -> float:
        return self.simulated_net - self.baseline_net


@dataclass
class StudyResult:
    rule: str
    family: str
    outcomes: list[Outcome] = field(default_factory=list)
    rejected_that_fit: int = 0
    episode_gap_hours: float = 6.0
    observation_days: float = 0.0

    @property
    def real_outcomes(self) -> list[Outcome]:
        return [item for item in self.outcomes if not item.phantom]

    @property
    def phantom_outcomes(self) -> list[Outcome]:
        return [item for item in self.outcomes if item.phantom]

    @property
    def winners_cut(self) -> int:
        return sum(1 for item in self.real_outcomes if item.baseline_net > 0 and item.simulated_net < item.baseline_net)

    @property
    def losses_avoided(self) -> int:
        return sum(1 for item in self.real_outcomes if item.baseline_net < 0 and item.simulated_net > item.baseline_net)

    @property
    def pnl_lost(self) -> float:
        return sum(max(0.0, -item.delta) for item in self.real_outcomes)

    @property
    def pnl_saved(self) -> float:
        return sum(max(0.0, item.delta) for item in self.real_outcomes)

    @property
    def delta_net(self) -> float:
        return sum(item.delta for item in self.real_outcomes)

    @property
    def slot_hours_freed(self) -> float:
        return sum(item.slot_hours_freed for item in self.real_outcomes)

    @property
    def episodes(self) -> list[list[Outcome]]:
        return group_outcomes(self.real_outcomes, self.episode_gap_hours)

    @property
    def average_episode_cost(self) -> Optional[float]:
        episodes = self.episodes
        if not episodes:
            return None
        return sum(sum(item.simulated_net for item in episode) for episode in episodes) / len(episodes)

    @property
    def episode_score(self) -> tuple[int, int, int]:
        improved = worse = flat = 0
        for episode in self.episodes:
            delta = sum(item.delta for item in episode)
            if delta > 1e-9:
                improved += 1
            elif delta < -1e-9:
                worse += 1
            else:
                flat += 1
        return improved, worse, flat


def main() -> None:
    args = _parse_args()
    config = _load_config(Path(args.config))
    ledger_path = Path(args.ledger) if args.ledger else PROJECT_ROOT / "data/trades/trades_B.jsonl"
    records = _read_jsonl(ledger_path)
    snapshots = _read_jsonl(Path(args.snapshots))
    trough_events = _read_jsonl(Path(args.trough_events))
    rejected = _read_jsonl(Path(args.rejected_signals))
    state = _read_json(Path(args.state), [])
    records.extend(_provisional_open_records(state, snapshots, config))
    records = _profile_filter(records, args.profile)
    snapshots = _profile_filter(snapshots, args.profile)
    trough_events = _profile_filter(trough_events, args.profile)
    rejected = _profile_filter(rejected, args.profile)

    results = run_study(
        records=records,
        snapshots=snapshots,
        trough_events=trough_events,
        rejected_signals=rejected,
        hard_stops=_csv_floats(args.hard_stops),
        time_stops=_csv_floats(args.time_stops),
        hybrids=[_parse_hybrid(value) for value in args.hybrid],
        cluster_guards=[_parse_cluster_guard(value) for value in _csv_text(args.cluster_guards)],
        episode_gap_hours=float(args.episode_gap_hours),
        fee_pct=_round_trip_fee_pct(config),
    )
    _print_report(records, results, args.profile, float(args.episode_gap_hours), args.detail)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estudo offline de stops e guards; nunca envia ordens nem altera o estado do bot."
    )
    parser.add_argument("--hard-stops", default="1.0,1.5,2.0,3.0", help="Stops percentuais separados por virgula.")
    parser.add_argument("--time-stops", default="6,12,24,48", help="Idades em horas, condicionais a PnL negativo.")
    parser.add_argument(
        "--hybrid",
        action="append",
        default=None,
        help='Regra hibrida, por exemplo "8atr,floor=1.5,cap=3.0"; pode ser repetida.',
    )
    parser.add_argument(
        "--cluster-guards",
        default="2/60/60,2/60/120",
        help="count/lookback_minutes/pause_minutes, separado por virgula.",
    )
    parser.add_argument("--episode-gap-hours", type=float, default=6.0)
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="all")
    parser.add_argument("--detail", action="store_true", help="Mostra o placar de cada episodio.")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--ledger")
    parser.add_argument("--snapshots", default=str(PROJECT_ROOT / "data/telemetry/position_snapshots.jsonl"))
    parser.add_argument("--trough-events", default=str(PROJECT_ROOT / "data/telemetry/trough_events.jsonl"))
    parser.add_argument("--rejected-signals", default=str(PROJECT_ROOT / "data/telemetry/rejected_signals.jsonl"))
    parser.add_argument("--state", default=str(PROJECT_ROOT / "data/state/open_positions.json"))
    args = parser.parse_args()
    if args.hybrid is None:
        args.hybrid = ["8atr,floor=1.5,cap=3.0"]
    return args


def run_study(
    records: Sequence[Dict[str, Any]],
    snapshots: Sequence[Dict[str, Any]],
    trough_events: Sequence[Dict[str, Any]],
    rejected_signals: Sequence[Dict[str, Any]],
    hard_stops: Sequence[float],
    time_stops: Sequence[float],
    hybrids: Sequence[tuple[float, float, float]],
    cluster_guards: Sequence[tuple[int, int, int]],
    episode_gap_hours: float,
    fee_pct: float,
) -> list[StudyResult]:
    usable = [record for record in records if _entry_price(record) is not None and _baseline_end(record) is not None]
    observation_days = _observation_days(usable)
    paths = _build_paths(usable, snapshots, trough_events)
    rejected_times = [_parse_ts(item.get("ts")) for item in rejected_signals]
    rejected_times = [value for value in rejected_times if value is not None]
    results: list[StudyResult] = []

    baseline = StudyResult(
        rule="BASELINE_CURRENT",
        family="baseline",
        episode_gap_hours=episode_gap_hours,
        observation_days=observation_days,
    )
    for record in usable:
        if str(record.get("exit_reason") or "") != "HARD_STOP":
            continue
        closed = _parse_ts(record.get("closed_at")) or _baseline_end(record)[0]
        net = _baseline_net(record, fee_pct)
        baseline.outcomes.append(
            Outcome(str(record.get("pair_id")), baseline.rule, closed, net, net, 0.0, "actual_fill", _is_phantom(record))
        )
    results.append(baseline)

    for stop_pct in hard_stops:
        result = StudyResult(
            rule=f"HARD_STOP_{stop_pct:g}%",
            family="hard_stop",
            episode_gap_hours=episode_gap_hours,
            observation_days=observation_days,
        )
        freed: list[tuple[datetime, datetime]] = []
        for record in usable:
            outcome = _simulate_price_stop(record, paths.get(_pair_id(record), []), stop_pct, fee_pct, result.rule)
            if outcome:
                result.outcomes.append(outcome)
                end = _baseline_end(record)[0]
                if not outcome.phantom and outcome.triggered_at < end:
                    freed.append((outcome.triggered_at, end))
        result.rejected_that_fit = _count_times_in_intervals(rejected_times, freed)
        results.append(result)

    snapshot_paths = _snapshot_paths(snapshots)
    for hours in time_stops:
        result = StudyResult(
            rule=f"TIME_STOP_{hours:g}h",
            family="time_stop",
            episode_gap_hours=episode_gap_hours,
            observation_days=observation_days,
        )
        freed: list[tuple[datetime, datetime]] = []
        for record in usable:
            outcome = _simulate_time_stop(record, snapshot_paths.get(_pair_id(record), []), hours, fee_pct, result.rule)
            if outcome:
                result.outcomes.append(outcome)
                end = _baseline_end(record)[0]
                if not outcome.phantom and outcome.triggered_at < end:
                    freed.append((outcome.triggered_at, end))
        result.rejected_that_fit = _count_times_in_intervals(rejected_times, freed)
        results.append(result)

    for atr_multiple, floor_pct, cap_pct in hybrids:
        rule = f"HYBRID_{atr_multiple:g}ATR_{floor_pct:g}-{cap_pct:g}%"
        result = StudyResult(rule, "hybrid", episode_gap_hours=episode_gap_hours, observation_days=observation_days)
        freed: list[tuple[datetime, datetime]] = []
        for record in usable:
            entry = _entry_price(record)
            entry_atr = _float(record.get("entry_atr"))
            if entry is None or entry_atr is None or entry_atr <= 0:
                continue
            atr_pct = atr_multiple * entry_atr / entry * 100
            stop_pct = min(cap_pct, max(floor_pct, atr_pct))
            outcome = _simulate_price_stop(record, paths.get(_pair_id(record), []), stop_pct, fee_pct, rule)
            if outcome:
                result.outcomes.append(outcome)
                end = _baseline_end(record)[0]
                if not outcome.phantom and outcome.triggered_at < end:
                    freed.append((outcome.triggered_at, end))
        result.rejected_that_fit = _count_times_in_intervals(rejected_times, freed)
        results.append(result)

    real_records = [record for record in usable if not _is_phantom(record)]
    hard_stop_closes = sorted(
        _parse_ts(record.get("closed_at"))
        for record in real_records
        if str(record.get("exit_reason") or "") == "HARD_STOP" and _parse_ts(record.get("closed_at")) is not None
    )
    for count, lookback_minutes, pause_minutes in cluster_guards:
        rule = f"CLUSTER_GUARD_{count}/{lookback_minutes}/{pause_minutes}m"
        windows = _cluster_windows(hard_stop_closes, count, lookback_minutes, pause_minutes)
        result = StudyResult(rule, "cluster_guard", episode_gap_hours=episode_gap_hours, observation_days=observation_days)
        for record in real_records:
            opened = _parse_ts(record.get("opened_at"))
            if opened is None or not _time_in_intervals(opened, windows):
                continue
            baseline_net = _baseline_net(record, fee_pct)
            end = _baseline_end(record)[0]
            result.outcomes.append(
                Outcome(
                    _pair_id(record),
                    rule,
                    opened,
                    baseline_net,
                    0.0,
                    max(0.0, (end - opened).total_seconds() / 3600),
                    "historical_replay",
                    False,
                )
            )
        results.append(result)
    return results


def _simulate_price_stop(
    record: Dict[str, Any],
    path: Sequence[tuple[datetime, float, str]],
    stop_pct: float,
    fee_pct: float,
    rule: str,
) -> Optional[Outcome]:
    entry = _entry_price(record)
    baseline_end = _baseline_end(record)
    if entry is None or baseline_end is None or stop_pct <= 0:
        return None
    threshold = entry * (1 - stop_pct / 100)
    crossing = next((event for event in path if event[1] <= threshold), None)
    fidelity = "event_path"
    if crossing is None:
        trough = _float(record.get("trough_price"))
        trough_at = _parse_ts(record.get("trough_at"))
        if trough is None or trough > threshold or trough_at is None:
            return None
        crossing = (trough_at, threshold, "trough_summary")
        fidelity = "trough_approx"
    triggered_at = crossing[0]
    end_at = baseline_end[0]
    if triggered_at > end_at:
        return None
    baseline_net = _baseline_net(record, fee_pct)
    simulated_net = -stop_pct - fee_pct
    return Outcome(
        _pair_id(record),
        rule,
        triggered_at,
        baseline_net,
        simulated_net,
        max(0.0, (end_at - triggered_at).total_seconds() / 3600),
        fidelity,
        _is_phantom(record),
    )


def _simulate_time_stop(
    record: Dict[str, Any],
    snapshots: Sequence[tuple[datetime, float]],
    hours: float,
    fee_pct: float,
    rule: str,
) -> Optional[Outcome]:
    opened = _parse_ts(record.get("opened_at"))
    entry = _entry_price(record)
    baseline_end = _baseline_end(record)
    if opened is None or entry is None or baseline_end is None or hours <= 0:
        return None
    eligible_at = opened + timedelta(hours=hours)
    crossing = next(((ts, price) for ts, price in snapshots if ts >= eligible_at and price < entry), None)
    if crossing is None or crossing[0] > baseline_end[0]:
        return None
    gross = (crossing[1] / entry - 1) * 100
    baseline_net = _baseline_net(record, fee_pct)
    return Outcome(
        _pair_id(record),
        rule,
        crossing[0],
        baseline_net,
        gross - fee_pct,
        max(0.0, (baseline_end[0] - crossing[0]).total_seconds() / 3600),
        "hourly_snapshot",
        _is_phantom(record),
    )


def group_outcomes(outcomes: Sequence[Outcome], gap_hours: float) -> list[list[Outcome]]:
    ordered = sorted(outcomes, key=lambda item: item.triggered_at)
    episodes: list[list[Outcome]] = []
    for outcome in ordered:
        if not episodes or outcome.triggered_at - episodes[-1][-1].triggered_at > timedelta(hours=gap_hours):
            episodes.append([outcome])
        else:
            episodes[-1].append(outcome)
    return episodes


def _build_paths(
    records: Sequence[Dict[str, Any]],
    snapshots: Sequence[Dict[str, Any]],
    trough_events: Sequence[Dict[str, Any]],
) -> dict[str, list[tuple[datetime, float, str]]]:
    paths: dict[str, list[tuple[datetime, float, str]]] = {}
    for event, source in [*((item, "snapshot") for item in snapshots), *((item, "trough_event") for item in trough_events)]:
        pair_id = _pair_id(event)
        ts = _parse_ts(event.get("ts"))
        price = _float(event.get("price"))
        if pair_id and ts is not None and price is not None:
            paths.setdefault(pair_id, []).append((ts, price, source))
    for record in records:
        pair_id = _pair_id(record)
        opened = _parse_ts(record.get("opened_at"))
        entry = _entry_price(record)
        if pair_id and opened is not None and entry is not None:
            paths.setdefault(pair_id, []).append((opened, entry, "entry"))
        closed = _parse_ts(record.get("closed_at"))
        trigger = _float(record.get("exit_trigger_price"))
        exit_price = _float(record.get("exit_price"))
        if pair_id and closed is not None and trigger is not None:
            paths.setdefault(pair_id, []).append((closed, trigger, "exit_trigger"))
        elif pair_id and closed is not None and exit_price is not None:
            paths.setdefault(pair_id, []).append((closed, exit_price, "exit"))
    for values in paths.values():
        values.sort(key=lambda item: item[0])
    return paths


def _snapshot_paths(snapshots: Sequence[Dict[str, Any]]) -> dict[str, list[tuple[datetime, float]]]:
    paths: dict[str, list[tuple[datetime, float]]] = {}
    for item in snapshots:
        pair_id = _pair_id(item)
        ts = _parse_ts(item.get("ts"))
        price = _float(item.get("price"))
        if pair_id and ts is not None and price is not None:
            paths.setdefault(pair_id, []).append((ts, price))
    for values in paths.values():
        values.sort(key=lambda item: item[0])
    return paths


def _cluster_windows(
    hard_stop_closes: Sequence[datetime],
    count: int,
    lookback_minutes: int,
    pause_minutes: int,
) -> list[tuple[datetime, datetime]]:
    if count <= 0 or lookback_minutes <= 0 or pause_minutes <= 0:
        return []
    windows: list[tuple[datetime, datetime]] = []
    lookback = timedelta(minutes=lookback_minutes)
    for ts in hard_stop_closes:
        recent = [item for item in hard_stop_closes if ts - lookback <= item <= ts]
        if len(recent) >= count:
            windows.append((ts, ts + timedelta(minutes=pause_minutes)))
    return _merge_intervals(windows)


def _merge_intervals(intervals: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _count_times_in_intervals(times: Sequence[datetime], intervals: Sequence[tuple[datetime, datetime]]) -> int:
    return sum(1 for value in set(times) if _time_in_intervals(value, intervals))


def _time_in_intervals(value: datetime, intervals: Sequence[tuple[datetime, datetime]]) -> bool:
    return any(start <= value < end for start, end in intervals)


def _print_report(
    records: Sequence[Dict[str, Any]],
    results: Sequence[StudyResult],
    profile: str,
    episode_gap_hours: float,
    detail: bool,
) -> None:
    real = sum(1 for item in records if not _is_phantom(item))
    phantom = sum(1 for item in records if _is_phantom(item))
    provisional = sum(1 for item in records if bool(item.get("provisional_open", False)))
    print("TREND-SOL | offline stop study")
    print(f"Sample: profile={profile} | real={real} | phantom={phantom} | provisional open={provisional}")
    print(f"Episodes: consecutive affected events <= {episode_gap_hours:g}h belong to the same episode")
    print("Fidelity: event_path/hourly_snapshot > trough_approx; simulated stops exclude execution slippage.")
    print("Replay: blocked entries keep all later historical events unchanged; slot substitutions are not reconstructed.")
    print()
    print("Rules:")
    print(
        f"{'rule':32} {'hitR':>4} {'hitPh':>5} {'cutW':>4} {'saveL':>5} {'lost':>8} {'saved':>8} "
        f"{'delta':>8} {'slot_h':>8} {'rej_fit':>7} {'episodes':>8} {'avg_ep':>8} {'score +/-/=':>11}"
    )
    for result in results:
        improved, worse, flat = result.episode_score
        print(
            f"{result.rule:32} {len(result.real_outcomes):4d} {len(result.phantom_outcomes):5d} "
            f"{result.winners_cut:4d} {result.losses_avoided:5d} "
            f"{result.pnl_lost:+8.2f} {result.pnl_saved:+8.2f} {result.delta_net:+8.2f} "
            f"{result.slot_hours_freed:8.1f} {result.rejected_that_fit:7d} {len(result.episodes):8d} "
            f"{_fmt_optional(result.average_episode_cost):>8} {improved:>3}/{worse}/{flat:<3}"
        )
    if any(result.phantom_outcomes for result in results):
        print()
        print("Phantom counterfactual (excluded from every real aggregate above):")
        for result in results:
            if not result.phantom_outcomes:
                continue
            phantom_delta = sum(item.delta for item in result.phantom_outcomes)
            print(f"  {result.rule}: hits={len(result.phantom_outcomes)} | synthetic delta={phantom_delta:+.2f}%")
    print()
    print("Episode frequency:")
    for result in results:
        frequency = len(result.episodes) / result.observation_days if result.observation_days > 0 else 0.0
        print(
            f"  {result.rule}: {len(result.episodes)} episodes / {result.observation_days:.2f} observed days "
            f"({frequency:.3f}/observed day; do not extrapolate short samples)"
        )
    if detail:
        print()
        print("Episode detail:")
        for result in results:
            for index, episode in enumerate(result.episodes, start=1):
                baseline = sum(item.baseline_net for item in episode)
                simulated = sum(item.simulated_net for item in episode)
                fidelities = ",".join(sorted({item.fidelity for item in episode}))
                print(
                    f"  {result.rule} #{index}: {episode[0].triggered_at.isoformat()} -> "
                    f"{episode[-1].triggered_at.isoformat()} | trades={len(episode)} | "
                    f"baseline={baseline:+.2f}% | simulated={simulated:+.2f}% | "
                    f"delta={simulated - baseline:+.2f}% | fidelity={fidelities}"
                )


def _provisional_open_records(
    state: Any,
    snapshots: Sequence[Dict[str, Any]],
    config: Dict[str, Any],
) -> list[Dict[str, Any]]:
    if not isinstance(state, list):
        return []
    latest: dict[str, Dict[str, Any]] = {}
    for snapshot in snapshots:
        pair_id = _pair_id(snapshot)
        ts = _parse_ts(snapshot.get("ts"))
        if not pair_id or ts is None:
            continue
        previous = latest.get(pair_id)
        if previous is None or ts > (_parse_ts(previous.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)):
            latest[pair_id] = snapshot
    fee_pct = _round_trip_fee_pct(config)
    records: list[Dict[str, Any]] = []
    for item in state:
        if not isinstance(item, dict) or item.get("status") != "OPEN":
            continue
        pair_id = _pair_id(item)
        snapshot = latest.get(pair_id)
        entry = _float(item.get("entry_price"))
        price = _float(snapshot.get("price")) if snapshot else None
        closed_at = snapshot.get("ts") if snapshot else None
        if not pair_id or entry is None or price is None or closed_at is None:
            continue
        gross = (price / entry - 1) * 100
        records.append(
            {
                **item,
                "profile": item.get("profile") or config.get("active_profile"),
                "opened_at": item.get("open_ts"),
                "closed_at": closed_at,
                "exit_price": price,
                "gross_pnl_pct": gross,
                "net_pnl_pct": gross - fee_pct,
                "provisional_open": True,
            }
        )
    return records


def _profile_filter(items: Sequence[Dict[str, Any]], profile: str) -> list[Dict[str, Any]]:
    if profile == "all":
        return list(items)
    return [item for item in items if str(item.get("profile") or "") == profile]


def _observation_days(records: Sequence[Dict[str, Any]]) -> float:
    starts = [_parse_ts(item.get("opened_at")) for item in records]
    ends = [_baseline_end(item)[0] for item in records if _baseline_end(item) is not None]
    values = [value for value in [*starts, *ends] if value is not None]
    if len(values) < 2:
        return 0.0
    return max((max(values) - min(values)).total_seconds() / 86400, 1 / 1440)


def _baseline_end(record: Dict[str, Any]) -> Optional[tuple[datetime, float]]:
    ts = _parse_ts(record.get("closed_at"))
    price = _float(record.get("exit_price"))
    return (ts, price) if ts is not None and price is not None else None


def _baseline_net(record: Dict[str, Any], fee_pct: float) -> float:
    net = _float(record.get("net_pnl_pct"))
    if net is not None:
        return net
    gross = _float(record.get("gross_pnl_pct"))
    if gross is None:
        entry = _entry_price(record)
        end = _baseline_end(record)
        gross = (end[1] / entry - 1) * 100 if entry is not None and end is not None else 0.0
    return gross - fee_pct


def _entry_price(record: Dict[str, Any]) -> Optional[float]:
    return _float(record.get("entry_price", record.get("entry")))


def _pair_id(record: Dict[str, Any]) -> str:
    return str(record.get("pair_id") or record.get("phantom_id") or "")


def _is_phantom(record: Dict[str, Any]) -> bool:
    return bool(record.get("phantom", False)) or str(record.get("position_type") or "") == "PHANTOM"


def _parse_hybrid(value: str) -> tuple[float, float, float]:
    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    if not parts or not parts[0].endswith("atr"):
        raise SystemExit(f"Invalid --hybrid value: {value}")
    multiple = _positive_float(parts[0][:-3], "hybrid ATR multiple")
    values: dict[str, float] = {}
    for part in parts[1:]:
        if "=" not in part:
            raise SystemExit(f"Invalid --hybrid value: {value}")
        key, raw = part.split("=", 1)
        values[key] = _positive_float(raw, f"hybrid {key}")
    if "floor" not in values or "cap" not in values or values["floor"] > values["cap"]:
        raise SystemExit(f"Invalid --hybrid value: {value}")
    return multiple, values["floor"], values["cap"]


def _parse_cluster_guard(value: str) -> tuple[int, int, int]:
    try:
        count, lookback, pause = (int(part) for part in value.split("/"))
    except (TypeError, ValueError):
        raise SystemExit(f"Invalid --cluster-guards value: {value}") from None
    if min(count, lookback, pause) <= 0:
        raise SystemExit(f"Invalid --cluster-guards value: {value}")
    return count, lookback, pause


def _csv_floats(value: str) -> list[float]:
    return [_positive_float(item, "list value") for item in _csv_text(value)]


def _csv_text(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _positive_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise SystemExit(f"Invalid {label}: {value}") from None
    if not math.isfinite(number) or number <= 0:
        raise SystemExit(f"Invalid {label}: {value}")
    return number


def _round_trip_fee_pct(config: Dict[str, Any]) -> float:
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    if not fees or not bool(fees.get("enabled", False)):
        return 0.0
    taker = _float(fees.get("taker_fee_pct")) or 0.0
    if bool(fees.get("use_bnb_discount", False)):
        taker *= 0.75
    return taker * 2


def _load_config(path: Path) -> Dict[str, Any]:
    if not path.exists() or yaml is None:
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _read_jsonl(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    records: list[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


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


def _fmt_optional(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.2f}"


if __name__ == "__main__":
    main()
