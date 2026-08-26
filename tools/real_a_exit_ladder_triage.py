"""Read-only structural triage of three REAL_A exit-ladder arms over real aggTrades."""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.position.bot_full_engine import BotFullExitPosition
from tools.real_a_exit_simulator import Seed, _NoopClient, _NoopLogger, _exit_config, iter_aggtrade_files, load_real_a_seeds


ARMS = ("A", "B", "C")


@dataclass
class Outcome:
    seed: Seed
    arm: str
    status: str = "OPEN"
    reason: str | None = None
    trigger_price: float | None = None
    trigger_at: datetime | None = None
    gross_pct: float | None = None
    net_pct: float | None = None
    age_seconds: float | None = None
    be_armed: bool = False


def main() -> None:
    args = _parse_args()
    raw = _read_yaml(args.config)
    config = effective_config(raw)
    seeds = load_real_a_seeds(args.ledger, _parse_timestamp(args.since))
    if args.closed_until:
        closed_until = _parse_timestamp(args.closed_until)
        seeds = [seed for seed in seeds if seed.ledger_closed_at <= closed_until]
        if not seeds:
            raise ValueError("No seeds remain at or before --closed-until.")
    arms = _arm_configs(config)
    outcomes, end_at = simulate(seeds, iter_aggtrade_files(args.aggtrades), arms, args.max_gap_seconds)

    baseline = outcomes["A"]
    agreement = _validate_baseline(baseline)
    if agreement < 0.95:
        print("\nTRIAGE NOT RUN: baseline A agreement below 95%.")
        raise SystemExit(2)

    print(f"\nTRIAGE WINDOW END: {_fmt_ts(end_at)}")
    print("All PnL is modeled from aggTrade trigger price; open positions are excluded from PnL.")
    _print_summary(outcomes)
    _print_excluding_winner(outcomes)
    _print_transitions(outcomes)
    _print_added_hard_stops(outcomes)
    _print_open(outcomes)


def simulate(
    seeds: list[Seed], ticks: Iterable[Any], arm_configs: dict[str, dict[str, Any]], max_gap_seconds: float,
) -> tuple[dict[str, dict[str, Outcome]], datetime]:
    pending = iter(sorted(seeds, key=lambda item: item.opened_at))
    next_seed = next(pending, None)
    active: dict[str, dict[str, tuple[Seed, BotFullExitPosition]]] = {arm: {} for arm in ARMS}
    output: dict[str, dict[str, Outcome]] = {arm: {} for arm in ARMS}
    first_at: datetime | None = None
    previous_at: datetime | None = None
    end_at: datetime | None = None
    processed = 0

    for tick in ticks:
        processed += 1
        if first_at is None:
            first_at = tick.timestamp
        if previous_at is not None and tick.timestamp < previous_at:
            raise ValueError("aggTrade input is not chronological.")
        if previous_at is not None and (tick.timestamp - previous_at).total_seconds() > max_gap_seconds:
            raise ValueError(f"aggTrade gap exceeds --max-gap-seconds={max_gap_seconds} at {tick.timestamp.isoformat()}.")
        previous_at = tick.timestamp
        end_at = tick.timestamp
        while next_seed is not None and next_seed.opened_at <= tick.timestamp:
            for arm in ARMS:
                active[arm][next_seed.pair_id] = (next_seed, _position(next_seed, arm_configs[arm]))
            next_seed = next(pending, None)
        for arm in ARMS:
            for pair_id, (seed, position) in list(active[arm].items()):
                event = position.on_tick(tick.price, market_ts=tick.timestamp.isoformat())
                if event is None:
                    continue
                output[arm][pair_id] = _closed_outcome(seed, arm, position, tick.timestamp, event)
                del active[arm][pair_id]
        if processed % 100_000 == 0:
            resolved = sum(len(items) for items in output.values())
            print(f"triage progress: {processed} ticks | resolved={resolved}", flush=True)

    if first_at is None or end_at is None:
        raise ValueError("No aggTrades supplied.")
    earliest = min(seed.opened_at for seed in seeds)
    if (first_at - earliest).total_seconds() > max_gap_seconds:
        raise ValueError("aggTrade coverage begins after the first REAL_A seed.")
    while next_seed is not None:
        for arm in ARMS:
            output[arm][next_seed.pair_id] = Outcome(next_seed, arm)
        next_seed = next(pending, None)
    for arm in ARMS:
        for seed, position in active[arm].values():
            output[arm][seed.pair_id] = Outcome(seed, arm, be_armed=position.be_armed_at is not None)
    return output, end_at


def _position(seed: Seed, config: dict[str, Any]) -> BotFullExitPosition:
    return BotFullExitPosition(
        pair_id=seed.pair_id, symbol=seed.symbol, entry_price=seed.entry_price, quantity=1.0,
        entry_order={}, open_ts=seed.opened_at.isoformat(), config=deepcopy(config),
        client=_NoopClient(), logger=_NoopLogger(), entry_atr=seed.entry_atr,
        atr_timeframe="1m", atr_period=14, no_progress_enabled=seed.no_progress_enabled,
        no_progress_tolerance_seconds=seed.no_progress_tolerance_seconds,
        no_progress_tolerance_source=seed.no_progress_tolerance_source,
    )


def _closed_outcome(seed: Seed, arm: str, position: BotFullExitPosition, at: datetime, event: dict[str, Any]) -> Outcome:
    price = float(event["trigger_price"])
    gross = (price - seed.entry_price) / seed.entry_price * 100
    fees = float(event.get("estimated_fees_pct") or 0)
    return Outcome(seed, arm, "CLOSED", str(event["exit_reason"]), price, at, gross, gross - fees,
                   (at - seed.opened_at).total_seconds(), position.be_armed_at is not None)


def _arm_configs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    current = _exit_config(config)
    original_geometry = deepcopy(current)
    original_geometry.setdefault("ladder", {})["be_activation_buffer_atr"] = 2.9
    be_off = deepcopy(current)
    be_off["breakeven"] = {"mode": "off"}
    return {"A": current, "B": original_geometry, "C": be_off}


def _validate_baseline(rows: dict[str, Outcome]) -> float:
    closed = [item for item in rows.values() if item.status == "CLOSED"]
    matches = [item for item in closed if item.reason == item.seed.ledger_reason]
    agreement = len(matches) / len(rows) if rows else 0.0
    price_errors = [abs(item.trigger_price - item.seed.ledger_trigger_price) for item in matches if item.seed.ledger_trigger_price is not None]
    time_errors = [abs((item.trigger_at - item.seed.ledger_closed_at).total_seconds()) for item in matches if item.trigger_at is not None]
    print("REAL_A baseline autovalidation")
    print(f"seeds: {len(rows)} | reason agreement: {len(matches)}/{len(rows)} ({agreement * 100:.2f}%)")
    print(f"trigger price abs error mean/median: {_mean(price_errors):.8f} / {_median(price_errors):.8f}")
    print(f"trigger time vs ledger closed_at abs seconds mean/median: {_mean(time_errors):.3f} / {_median(time_errors):.3f}")
    return agreement


def _print_summary(arms: dict[str, dict[str, Outcome]]) -> None:
    print("\nARM | closed | open | gross total | gross/trade | net total | net/trade | PF | HS | BE | PL | TRAIL | age mean/median min | biggest winner | BEs armed")
    for arm in ARMS:
        summary = _summary(arms[arm].values())
        print(
            f"{arm} | {summary['closed']} | {summary['open']} | {summary['gross']:+.3f}% | {summary['gross_trade']:+.3f}% | "
            f"{summary['net']:+.3f}% | {summary['net_trade']:+.3f}% | {_fmt_pf(summary['pf'])} | "
            f"{summary['HARD_STOP']} | {summary['BREAKEVEN']} | {summary['PROFIT_LOCK']} | {summary['TRAILING']} | "
            f"{summary['age_mean']:.1f}/{summary['age_median']:.1f} | {summary['winner']:+.3f}% | {summary['be_armed']}"
        )


def _print_excluding_winner(arms: dict[str, dict[str, Outcome]]) -> None:
    print("\nEXCLUDING EACH ARM'S BIGGEST WINNER")
    print("ARM | closed | gross total | gross/trade | net total | net/trade | PF")
    for arm in ARMS:
        closed = [item for item in arms[arm].values() if item.status == "CLOSED"]
        if closed:
            winner = max(closed, key=lambda item: item.gross_pct or float("-inf"))
            closed.remove(winner)
        summary = _summary(closed)
        print(f"{arm} | {summary['closed']} | {summary['gross']:+.3f}% | {summary['gross_trade']:+.3f}% | {summary['net']:+.3f}% | {summary['net_trade']:+.3f}% | {_fmt_pf(summary['pf'])}")


def _print_transitions(arms: dict[str, dict[str, Outcome]]) -> None:
    base = arms["A"]
    for arm in ("B", "C"):
        counts: dict[tuple[str, str], int] = {}
        changed_a_net = changed_arm_net = 0.0
        changed_closed = 0
        for pair_id, original in base.items():
            candidate = arms[arm][pair_id]
            origin = original.reason or "OPEN"
            destination = candidate.reason or "OPEN"
            counts[(origin, destination)] = counts.get((origin, destination), 0) + 1
            if origin != destination and original.status == "CLOSED" and candidate.status == "CLOSED":
                changed_closed += 1
                changed_a_net += float(original.net_pct or 0)
                changed_arm_net += float(candidate.net_pct or 0)
        print(f"\nDESTINATION TRANSITIONS A -> {arm}")
        for destination in ("HARD_STOP", "BREAKEVEN", "PROFIT_LOCK", "TRAILING", "OPEN"):
            count = counts.get(("BREAKEVEN", destination), 0)
            print(f"BE -> {destination}: {count}")
        print(f"changed closed destinations: {changed_closed} | A net={changed_a_net:+.3f}% | {arm} net={changed_arm_net:+.3f}% | delta={changed_arm_net - changed_a_net:+.3f}%")


def _print_added_hard_stops(arms: dict[str, dict[str, Outcome]]) -> None:
    for arm in ("B", "C"):
        added = [item for pair_id, item in arms[arm].items() if item.reason == "HARD_STOP" and arms["A"][pair_id].reason != "HARD_STOP"]
        print(f"\nADDITIONAL HARD_STOPS {arm}: {len(added)}")
        if not added:
            continue
        for item in sorted(added, key=lambda value: value.trigger_at or datetime.max.replace(tzinfo=timezone.utc)):
            print(f"{_fmt_ts(item.trigger_at)} | {item.seed.pair_id} | A={arms['A'][item.seed.pair_id].reason or 'OPEN'} | net={item.net_pct:+.3f}%")


def _print_open(arms: dict[str, dict[str, Outcome]]) -> None:
    for arm in ARMS:
        open_items = [item for item in arms[arm].values() if item.status == "OPEN"]
        print(f"\nOPEN AT END {arm}: {len(open_items)}")
        for item in sorted(open_items, key=lambda value: value.seed.opened_at):
            print(f"{item.seed.pair_id} | entry={item.seed.entry_price:.4f} | opened={_fmt_ts(item.seed.opened_at)} | BE armed={item.be_armed}")


def _summary(items: Iterable[Outcome]) -> dict[str, float | int]:
    closed = [item for item in items if item.status == "CLOSED"]
    gross = sum(float(item.gross_pct or 0) for item in closed)
    net = sum(float(item.net_pct or 0) for item in closed)
    gains = sum(float(item.gross_pct or 0) for item in closed if float(item.gross_pct or 0) > 0)
    losses = -sum(float(item.gross_pct or 0) for item in closed if float(item.gross_pct or 0) < 0)
    ages = [float(item.age_seconds or 0) / 60 for item in closed]
    return {
        "closed": len(closed), "open": sum(item.status == "OPEN" for item in items), "gross": gross, "net": net,
        "gross_trade": gross / len(closed) if closed else 0.0, "net_trade": net / len(closed) if closed else 0.0,
        "pf": gains / losses if losses else math.inf, "HARD_STOP": sum(item.reason == "HARD_STOP" for item in closed),
        "BREAKEVEN": sum(item.reason == "BREAKEVEN" for item in closed), "PROFIT_LOCK": sum(item.reason == "PROFIT_LOCK" for item in closed),
        "TRAILING": sum(item.reason == "TRAILING" for item in closed), "age_mean": _mean(ages), "age_median": _median(ages),
        "winner": max((float(item.gross_pct or 0) for item in closed), default=0.0), "be_armed": sum(item.be_armed for item in items),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only A/B/C exit-ladder triage over REAL_A seeds and contiguous aggTrades.")
    parser.add_argument("--aggtrades", type=Path, nargs="+", required=True)
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "config.yaml")
    parser.add_argument("--since", default="2026-08-19T01:05:00-03:00")
    parser.add_argument("--closed-until", help="Freeze the seed set at this ledger closed_at timestamp.")
    parser.add_argument("--max-gap-seconds", type=float, default=30.0)
    return parser.parse_args()


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def _fmt_pf(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.3f}"


def _fmt_ts(value: datetime | None) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%Y-%m-%d %H:%M:%S BRT") if value else "n/a"


if __name__ == "__main__":
    main()
