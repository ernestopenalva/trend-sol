"""Read-only trailing-exit laboratory over REAL_A ledger seeds and aggTrades.

The CONTROL path delegates all exit processing to ``BotFullExitPosition``.  The
three experimental arms override *only* the trailing-stop calculation after the
unchanged runtime activation at 10 x entry_atr.  No entries, capacity, state,
or production files are modified.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
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
from src.indicators.indicators import atr
from src.position.bot_full_engine import BotFullExitPosition
from tools.real_a_exit_simulator import (
    Seed,
    Tick,
    _NoopClient,
    _NoopLogger,
    _as_float,
    _exit_config,
    _parse_timestamp,
    iter_aggtrade_files,
    load_real_a_seeds,
)


CLEAN_START = "2026-08-15T18:52:00-03:00"
ARMS = ("CONTROL", "FRACTIONAL_30", "FRACTIONAL_50", "CURRENT_ATR_5")


@dataclass(frozen=True)
class Candle1m:
    open_time: datetime
    close_time: datetime
    high: float
    low: float
    close: float


class CurrentAtrSeries:
    """ATR values indexed by the close time of a *closed* 1m candle."""

    def __init__(self, candles: list[Candle1m], period: int) -> None:
        self.candles = sorted(candles, key=lambda item: item.open_time)
        self.period = period
        self.close_times = [item.close_time for item in self.candles]
        values = atr(
            [item.high for item in self.candles],
            [item.low for item in self.candles],
            [item.close for item in self.candles],
            period,
        )
        self.values = [float(value) if value is not None else None for value in values]

    def validate(self, start: datetime, end: datetime) -> None:
        if not self.candles:
            raise ValueError("No 1m candles supplied for CURRENT_ATR_5.")
        earliest_required = start - timedelta(minutes=self.period)
        if self.candles[0].open_time > earliest_required:
            raise ValueError(
                "CURRENT_ATR_5 not run: 1m candle cache lacks the required Wilder-14 warmup before the first seed."
            )
        if self.candles[-1].close_time < end:
            raise ValueError("CURRENT_ATR_5 not run: 1m candle cache ends before the supplied aggTrade data.")
        relevant = [item for item in self.candles if earliest_required <= item.open_time <= end]
        for previous, current in zip(relevant, relevant[1:]):
            if (current.open_time - previous.open_time).total_seconds() != 60:
                raise ValueError(
                    "CURRENT_ATR_5 not run: 1m candle cache has a gap; current ATR cannot be guaranteed."
                )
        if self.value_at(start) is None:
            raise ValueError("CURRENT_ATR_5 not run: Wilder-14 ATR is not initialized at the first seed.")

    def value_at(self, timestamp: datetime) -> float | None:
        # bisect_right means a candle is usable only after its own close.
        index = bisect.bisect_right(self.close_times, timestamp) - 1
        return self.values[index] if index >= 0 else None


class LabPosition(BotFullExitPosition):
    def __init__(self, *args: Any, arm: str, current_atr: CurrentAtrSeries | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.lab_arm = arm
        self.current_atr_series = current_atr
        self.lab_tick_at: datetime | None = None
        self.last_current_atr: float | None = None
        self.trailing_activated_at: datetime | None = None

    def on_lab_tick(self, tick: Tick) -> dict[str, Any] | None:
        self.lab_tick_at = tick.timestamp
        if self.current_atr_series is not None:
            self.last_current_atr = self.current_atr_series.value_at(tick.timestamp)
            if self.last_current_atr is None:
                raise ValueError("CURRENT_ATR_5 received a tick before a closed, initialized 1m ATR was available.")
        was_active = self.trailing_active
        result = self.on_tick(tick.price, market_ts=tick.timestamp.isoformat())
        if not was_active and self.trailing_active:
            self.trailing_activated_at = tick.timestamp
        return result

    def _current_trailing_stop(self) -> float | None:
        if self.lab_arm == "FRACTIONAL_30":
            return self.entry_price + 0.70 * (self.highest_price - self.entry_price)
        if self.lab_arm == "FRACTIONAL_50":
            return self.entry_price + 0.50 * (self.highest_price - self.entry_price)
        if self.lab_arm == "CURRENT_ATR_5":
            if self.last_current_atr is None:
                return None
            return self.highest_price - 5.0 * self.last_current_atr
        return super()._current_trailing_stop()


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
    trailing_activated: bool = False
    trailing_activated_at: datetime | None = None
    highest_price: float | None = None
    trailing_stop: float | None = None
    current_atr_at_exit: float | None = None


def main() -> None:
    args = _args()
    since = _timestamp(args.since)
    raw = _read_yaml(args.config)
    config = effective_config(raw)
    seeds = load_real_a_seeds(args.ledger, since)
    if args.until:
        until = _timestamp(args.until)
        seeds = [seed for seed in seeds if seed.opened_at < until]
    if args.closed_until:
        closed_until = _timestamp(args.closed_until)
        seeds = [seed for seed in seeds if seed.ledger_closed_at <= closed_until]
    if not seeds:
        raise SystemExit("No REAL_A closed seeds in the requested opened_at window.")

    # The stream is materialized once to establish a strict, common market path
    # for all four arms.  The supplied data is analytical input, never runtime data.
    ticks = list(iter_aggtrade_files(args.aggtrades))
    if not ticks:
        raise SystemExit("No aggTrades supplied.")
    _validate_ticks(ticks, seeds, args.max_gap_seconds, args.validation_grace_seconds)
    current_atr = CurrentAtrSeries(_load_1m_candles(args.candles_1m), int(config["entry"]["atr_period"]))
    current_atr.validate(min(item.opened_at for item in seeds), ticks[-1].timestamp)

    outcomes = _simulate(seeds, ticks, _exit_config(config), current_atr)
    agreement = _print_control_validation(outcomes["CONTROL"])
    if agreement < 0.95:
        print("\nLAB NOT RUN: CONTROL reason agreement is below 95%.")
        raise SystemExit(2)

    _print_header(args, seeds, ticks, current_atr)
    _print_activation_audit(outcomes)
    _print_summary(outcomes)
    _print_differences(outcomes)
    _print_current_atr_differences(outcomes)
    base_notional = args.capital * float(config["capital"]["trade_size_pct"]) / 100.0
    _print_portfolios(outcomes, args.capital, base_notional)
    _print_weekly(outcomes, base_notional)
    _print_verdict(outcomes, args.capital, base_notional)
    _write_details(args.output, outcomes)
    print(f"\nDetailed CSV: {args.output}")


def _simulate(
    seeds: list[Seed], ticks: list[Tick], exit_config: dict[str, Any], current_atr: CurrentAtrSeries,
) -> dict[str, dict[str, Outcome]]:
    pending = iter(sorted(seeds, key=lambda item: item.opened_at))
    next_seed = next(pending, None)
    active: dict[str, dict[str, LabPosition]] = {arm: {} for arm in ARMS}
    output: dict[str, dict[str, Outcome]] = {arm: {} for arm in ARMS}
    for index, tick in enumerate(ticks, start=1):
        while next_seed is not None and next_seed.opened_at <= tick.timestamp:
            for arm in ARMS:
                active[arm][next_seed.pair_id] = _position(next_seed, exit_config, arm, current_atr)
            next_seed = next(pending, None)
        for arm in ARMS:
            for pair_id, position in list(active[arm].items()):
                event = position.on_lab_tick(tick)
                if event is None:
                    continue
                output[arm][pair_id] = _closed(position, tick.timestamp, event)
                del active[arm][pair_id]
        if index % 100_000 == 0:
            resolved = sum(len(value) for value in output.values())
            print(f"trailing lab progress: {index} ticks | resolved={resolved}", flush=True)
    for arm in ARMS:
        for position in active[arm].values():
            output[arm][position.pair_id] = _open(position)
    while next_seed is not None:
        for arm in ARMS:
            output[arm][next_seed.pair_id] = Outcome(next_seed, arm)
        next_seed = next(pending, None)
    return output


def _position(seed: Seed, config: dict[str, Any], arm: str, current_atr: CurrentAtrSeries) -> LabPosition:
    position = LabPosition(
        pair_id=seed.pair_id, symbol=seed.symbol, entry_price=seed.entry_price, quantity=1.0,
        entry_order={}, open_ts=seed.opened_at.isoformat(), config=deepcopy(config),
        client=_NoopClient(), logger=_NoopLogger(), entry_atr=seed.entry_atr,
        atr_timeframe="1m", atr_period=14, no_progress_enabled=seed.no_progress_enabled,
        no_progress_tolerance_seconds=seed.no_progress_tolerance_seconds,
        no_progress_tolerance_source=seed.no_progress_tolerance_source,
        arm=arm, current_atr=current_atr if arm == "CURRENT_ATR_5" else None,
    )
    position._lab_seed = seed
    return position


def _closed(position: LabPosition, at: datetime, event: dict[str, Any]) -> Outcome:
    price = float(event["trigger_price"])
    gross = (price - position.entry_price) / position.entry_price * 100
    fees = float(event.get("estimated_fees_pct") or 0)
    return Outcome(
        seed=_seed(position), arm=position.lab_arm, status="CLOSED", reason=str(event["exit_reason"]),
        trigger_price=price, trigger_at=at, gross_pct=gross, net_pct=gross - fees,
        trailing_activated=position.trailing_active, trailing_activated_at=position.trailing_activated_at,
        highest_price=position.highest_price, trailing_stop=position.trailing_stop,
        current_atr_at_exit=position.last_current_atr,
    )


def _open(position: LabPosition) -> Outcome:
    return Outcome(
        seed=_seed(position), arm=position.lab_arm, trailing_activated=position.trailing_active,
        trailing_activated_at=position.trailing_activated_at, highest_price=position.highest_price,
        trailing_stop=position.trailing_stop, current_atr_at_exit=position.last_current_atr,
    )


def _seed(position: LabPosition) -> Seed:
    # All fields are immutable on the seed; retain it once on the position object.
    return getattr(position, "_lab_seed")


def _validate_ticks(ticks: list[Tick], seeds: list[Seed], maximum_gap: float, grace: float) -> None:
    previous: datetime | None = None
    for tick in ticks:
        if previous is not None:
            gap = (tick.timestamp - previous).total_seconds()
            if gap < 0:
                raise ValueError("aggTrade input is not chronological.")
            if gap > maximum_gap:
                raise ValueError(f"aggTrade gap exceeds --max-gap-seconds={maximum_gap} at {tick.timestamp.isoformat()}.")
        previous = tick.timestamp
    for seed in seeds:
        if (ticks[0].timestamp - seed.opened_at).total_seconds() > maximum_gap:
            raise ValueError(f"aggTrade coverage starts too late for {seed.pair_id}.")
        if (seed.ledger_closed_at + timedelta(seconds=grace) - ticks[-1].timestamp).total_seconds() > maximum_gap:
            raise ValueError(f"aggTrade coverage ends too early for control validation of {seed.pair_id}.")


def _load_1m_candles(path: Path) -> list[Candle1m]:
    output: list[Candle1m] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            open_ms = row.get("open_time_ms", row.get("openTime"))
            close_ms = row.get("close_time_ms", row.get("closeTime"))
            high, low, close = _as_float(row.get("high", row.get("h"))), _as_float(row.get("low", row.get("l"))), _as_float(row.get("close", row.get("c")))
            if None in (open_ms, close_ms, high, low, close):
                continue
            try:
                output.append(Candle1m(
                    datetime.fromtimestamp(float(open_ms) / 1000, timezone.utc),
                    datetime.fromtimestamp(float(close_ms) / 1000, timezone.utc),
                    float(high), float(low), float(close),
                ))
            except (TypeError, ValueError, OSError):
                continue
    if not output:
        raise ValueError(f"No readable 1m candles in {path}.")
    return output


def _print_control_validation(rows: dict[str, Outcome]) -> float:
    values = list(rows.values())
    matches = [item for item in values if item.reason == item.seed.ledger_reason]
    errors = [abs(float(item.trigger_price) - float(item.seed.ledger_trigger_price)) for item in matches if item.trigger_price is not None and item.seed.ledger_trigger_price is not None]
    print("REAL_A trailing lab — CONTROL autovalidation")
    print(f"seeds: {len(values)} | reason agreement: {len(matches)}/{len(values)} ({len(matches) / len(values) * 100:.2f}%)")
    print(f"trigger price abs error mean/median: {_mean(errors):.8f} / {_median(errors):.8f}")
    divergent = [item for item in values if item.reason != item.seed.ledger_reason]
    for item in divergent:
        print(f"  {item.seed.pair_id} | ledger={item.seed.ledger_reason} | CONTROL={item.reason or 'OPEN'}")
    return len(matches) / len(values) if values else 0.0


def _print_header(args: argparse.Namespace, seeds: list[Seed], ticks: list[Tick], series: CurrentAtrSeries) -> None:
    print("\nTREND-SOL | REAL_A trailing exit lab | READ-ONLY")
    print(f"seed window (opened_at): {_fmt(min(item.opened_at for item in seeds))} -> {_fmt(max(item.opened_at for item in seeds))}")
    print(f"aggTrade path: {_fmt(ticks[0].timestamp)} -> {_fmt(ticks[-1].timestamp)} | seeds={len(seeds)}")
    print(f"CURRENT_ATR_5 source: {args.candles_1m} | Wilder {series.period}, 1m closed candles | cache={_fmt(series.candles[0].open_time)} -> {_fmt(series.candles[-1].close_time)}")
    print("All arms reuse REAL_A entry/BE/PL/HS/fees and trailing activation at 10 x entry_atr. Only post-activation trailing stop differs.")


def _print_activation_audit(outcomes: dict[str, dict[str, Outcome]]) -> None:
    activated = {arm: {pair_id for pair_id, item in values.items() if item.trailing_activated} for arm, values in outcomes.items()}
    print("\nTRAILING ACTIVATION AUDIT")
    for arm in ARMS:
        final = sum(item.reason == "TRAILING" for item in outcomes[arm].values())
        print(f"{arm} | activated={len(activated[arm])} | final TRAILING={final}")
    same = all(activated[arm] == activated["CONTROL"] for arm in ARMS)
    print("activation set identical across arms: YES" if same else "activation set identical across arms: NO")
    if not same:
        for arm in ARMS[1:]:
            print(f"  {arm} only={len(activated[arm] - activated['CONTROL'])} | CONTROL only={len(activated['CONTROL'] - activated[arm])}")
        print("A difference means a competing exit closed a path before it could reach the unchanged activation threshold; it is not a changed activation rule.")


def _metrics(rows: Iterable[Outcome]) -> dict[str, Any]:
    closed = [item for item in rows if item.status == "CLOSED"]
    gross = sum(float(item.gross_pct or 0) for item in closed); net = sum(float(item.net_pct or 0) for item in closed)
    positive = sum(float(item.gross_pct or 0) for item in closed if float(item.gross_pct or 0) > 0)
    negative = -sum(float(item.gross_pct or 0) for item in closed if float(item.gross_pct or 0) < 0)
    trailing = [item for item in closed if item.reason == "TRAILING"]
    return {
        "closed": len(closed), "open": sum(item.status == "OPEN" for item in rows), "gross": gross, "net": net,
        "gross_avg": gross / len(closed) if closed else 0.0, "net_avg": net / len(closed) if closed else 0.0,
        "pf": positive / negative if negative else math.inf, "reasons": Counter(item.reason for item in closed),
        "trail_mean": _mean([float(item.gross_pct or 0) for item in trailing]), "trail_median": _median([float(item.gross_pct or 0) for item in trailing]),
        "trail_best": max((float(item.gross_pct or 0) for item in trailing), default=0.0), "trail_worst": min((float(item.gross_pct or 0) for item in trailing), default=0.0),
    }


def _print_summary(outcomes: dict[str, dict[str, Outcome]]) -> None:
    print("\nARM | closed | open | gross total | gross/seed | net total | net/seed | PF | HS | BE | PL | TRAIL | trailing mean/median/best/worst")
    for arm in ARMS:
        value = _metrics(outcomes[arm].values()); reasons = value["reasons"]
        print(f"{arm} | {value['closed']} | {value['open']} | {value['gross']:+.3f}% | {value['gross_avg']:+.3f}% | {value['net']:+.3f}% | {value['net_avg']:+.3f}% | {_pf(value['pf'])} | {reasons['HARD_STOP']} | {reasons['BREAKEVEN']} | {reasons['PROFIT_LOCK']} | {reasons['TRAILING']} | {value['trail_mean']:+.3f}%/{value['trail_median']:+.3f}%/{value['trail_best']:+.3f}%/{value['trail_worst']:+.3f}%")


def _print_differences(outcomes: dict[str, dict[str, Outcome]]) -> None:
    base = outcomes["CONTROL"]
    print("\nDIFFERENCES VS CONTROL | arm | changed | exit-price delta mean | net delta mean | improved | worsened | equal")
    for arm in ARMS[1:]:
        changed = []; improved = worsened = equal = 0
        for pair_id, trial in outcomes[arm].items():
            control = base[pair_id]
            same = (control.reason, control.trigger_price, control.trigger_at) == (trial.reason, trial.trigger_price, trial.trigger_at)
            if not same:
                changed.append((control, trial))
            if control.net_pct is None or trial.net_pct is None:
                continue
            delta = trial.net_pct - control.net_pct
            if delta > 1e-12: improved += 1
            elif delta < -1e-12: worsened += 1
            else: equal += 1
        price_deltas = [trial.trigger_price - control.trigger_price for control, trial in changed if control.trigger_price is not None and trial.trigger_price is not None]
        net_deltas = [trial.net_pct - control.net_pct for control, trial in changed if control.net_pct is not None and trial.net_pct is not None]
        print(f"{arm} | {len(changed)} | {_mean(price_deltas):+.8f} | {_mean(net_deltas):+.3f}% | {improved} | {worsened} | {equal}")
        if arm.startswith("FRACTIONAL"):
            givebacks = [_giveback(trial) for _, trial in changed if trial.reason == "TRAILING"]
            print(f"  observed final giveback among changed TRAILING exits: mean/median={_mean(givebacks):.2f}%/{_median(givebacks):.2f}% (execution and competing exit can differ from the theoretical giveback).")


def _giveback(item: Outcome) -> float:
    if item.highest_price is None or item.trigger_price is None or item.highest_price <= item.seed.entry_price:
        return float("nan")
    return (item.highest_price - item.trigger_price) / (item.highest_price - item.seed.entry_price) * 100


def _print_current_atr_differences(outcomes: dict[str, dict[str, Outcome]]) -> None:
    print("\nCURRENT_ATR_5 DIVERGENCES VS CONTROL")
    expanded = contracted = equal = 0
    for pair_id, trial in outcomes["CURRENT_ATR_5"].items():
        control = outcomes["CONTROL"][pair_id]
        if (control.reason, control.trigger_price, control.trigger_at) == (trial.reason, trial.trigger_price, trial.trigger_at):
            continue
        current = trial.current_atr_at_exit
        ratio = current / trial.seed.entry_atr if current is not None else float("nan")
        label = "MAIOR" if ratio > 1 + 1e-12 else "MENOR" if ratio < 1 - 1e-12 else "IGUAL"
        expanded += label == "MAIOR"; contracted += label == "MENOR"; equal += label == "IGUAL"
        print(f"{pair_id} | CONTROL={control.reason or 'OPEN'} | CURRENT={trial.reason or 'OPEN'} | entry_atr={trial.seed.entry_atr:.8f} | current_atr={current if current is not None else float('nan'):.8f} | ratio={ratio:.4f} | {label}")
    print(f"summary: ATR expanded={expanded} | contracted={contracted} | equal={equal}")


def _portfolio(rows: Iterable[Outcome], capital: float, base_notional: float) -> dict[str, float]:
    balance = peak = capital; max_dd = 0.0
    closed = sorted((item for item in rows if item.status == "CLOSED" and item.trigger_at), key=lambda item: item.trigger_at or datetime.max.replace(tzinfo=timezone.utc))
    for item in closed:
        balance += base_notional * float(item.net_pct or 0) / 100
        peak = max(peak, balance); max_dd = max(max_dd, peak - balance)
    return {"balance": balance, "net": balance - capital, "return": (balance - capital) / capital * 100, "dd": max_dd, "dd_pct": max_dd / capital * 100}


def _print_portfolios(outcomes: dict[str, dict[str, Outcome]], capital: float, base_notional: float) -> None:
    print(f"\nTHEORETICAL PORTFOLIO | same fixed REAL_A sizing (${base_notional:.2f} per seed); no compounding; open positions excluded")
    print("arm | initial | final balance | net PnL $ | return | realized max DD $ | realized max DD %")
    for arm in ARMS:
        value = _portfolio(outcomes[arm].values(), capital, base_notional)
        print(f"{arm} | ${capital:.2f} | ${value['balance']:.4f} | ${value['net']:+.4f} | {value['return']:+.4f}% | ${value['dd']:.4f} | {value['dd_pct']:.4f}%")


def _print_weekly(outcomes: dict[str, dict[str, Outcome]], base_notional: float) -> None:
    print("\nNON-OVERLAPPING ISO WEEKS (contributions; same fixed seed set)")
    weeks = sorted({f"{item.seed.opened_at.isocalendar().year}-W{item.seed.opened_at.isocalendar().week:02d}" for values in outcomes.values() for item in values.values()})
    for week in weeks:
        fields = []
        for arm in ARMS:
            rows = [item for item in outcomes[arm].values() if _week(item.seed.opened_at) == week and item.status == "CLOSED"]
            fields.append(f"{arm}: {len(rows)} closed / ${sum(base_notional * float(item.net_pct or 0) / 100 for item in rows):+.4f}")
        print(f"{week} | " + " | ".join(fields))


def _print_verdict(outcomes: dict[str, dict[str, Outcome]], capital: float, base_notional: float) -> None:
    control = _portfolio(outcomes["CONTROL"].values(), capital, base_notional)
    print("\nVERDICT (descriptive; no runtime change is authorized)")
    for arm in ARMS[1:]:
        value = _portfolio(outcomes[arm].values(), capital, base_notional)
        if value["net"] > control["net"] and value["dd"] <= control["dd"]:
            label = "MELHOR"
        elif value["net"] < control["net"] and value["dd"] >= control["dd"]:
            label = "PIOR"
        else:
            label = "TRADE-OFF"
        print(f"{arm}: {label} vs CONTROL (net delta=${value['net'] - control['net']:+.4f}; DD delta=${value['dd'] - control['dd']:+.4f}).")
    print("The observed N is reported above; this lab does not impose an arbitrary minimum sample size. Small trailing-activation cohorts are limited evidence.")


def _write_details(path: Path, outcomes: dict[str, dict[str, Outcome]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(("pair_id", "arm", "opened_brt", "entry", "entry_atr", "ledger_reason", "status", "reason", "trigger_brt", "trigger_price", "gross_pct", "net_pct", "trailing_activated", "trailing_activated_brt", "highest_price", "trailing_stop", "current_atr_at_exit", "current_atr_entry_ratio", "giveback_pct"))
        for arm in ARMS:
            for item in sorted(outcomes[arm].values(), key=lambda value: value.seed.opened_at):
                ratio = item.current_atr_at_exit / item.seed.entry_atr if item.current_atr_at_exit is not None else None
                writer.writerow((item.seed.pair_id, arm, _fmt(item.seed.opened_at), item.seed.entry_price, item.seed.entry_atr, item.seed.ledger_reason, item.status, item.reason or "", _fmt(item.trigger_at) if item.trigger_at else "", item.trigger_price or "", item.gross_pct if item.gross_pct is not None else "", item.net_pct if item.net_pct is not None else "", item.trailing_activated, _fmt(item.trailing_activated_at) if item.trailing_activated_at else "", item.highest_price or "", item.trailing_stop or "", item.current_atr_at_exit or "", ratio or "", _giveback(item) if item.reason == "TRAILING" else ""))


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only REAL_A A/B/C/D trailing-exit laboratory over real aggTrades.")
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "config.yaml")
    parser.add_argument("--aggtrades", type=Path, nargs="+", required=True)
    parser.add_argument("--candles-1m", type=Path, required=True, help="Continuous historical 1m candle JSONL for CURRENT_ATR_5.")
    parser.add_argument("--since", default=CLEAN_START)
    parser.add_argument("--until", help="Optional opened_at end-exclusive timestamp for a frozen seed cohort.")
    parser.add_argument("--closed-until", help="Optional inclusive ledger closed_at cutoff; freezes a live study cohort.")
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--max-gap-seconds", type=float, default=30.0)
    parser.add_argument("--validation-grace-seconds", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "real_a_trailing_lab_details.csv")
    return parser.parse_args()


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _timestamp(value: str) -> datetime:
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"Invalid timestamp: {value}")
    return parsed


def _week(value: datetime) -> str:
    iso = value.isocalendar(); return f"{iso.year}-W{iso.week:02d}"


def _mean(values: Iterable[float]) -> float:
    usable = [item for item in values if not math.isnan(item)]
    return statistics.fmean(usable) if usable else 0.0


def _median(values: Iterable[float]) -> float:
    usable = [item for item in values if not math.isnan(item)]
    return statistics.median(usable) if usable else 0.0


def _pf(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.3f}"


def _fmt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S BRT")


if __name__ == "__main__":
    main()
