"""Read-only phase-one anatomy of REAL_A realized-equity deterioration.

This deliberately does *not* pause historical entries or claim a
counterfactual result. It establishes the homogeneous ledger cohort and the
scale of realized drawdown/window damage before any threshold is selected.
"""
from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger

WINDOWS_HOURS = (1, 2, 4, 6, 12)


@dataclass(frozen=True)
class ClosedTrade:
    row: dict[str, Any]
    opened: datetime
    closed: datetime
    net_pct: float
    net_dollars: float
    signature: str


@dataclass(frozen=True)
class EquityPoint:
    trade: ClosedTrade
    equity: float
    peak: float
    drawdown: float


def main() -> None:
    args = _args()
    if args.capital <= 0:
        raise SystemExit("--capital must be positive")
    since, until = _parse_user_dt(args.since), _parse_user_dt(args.until)
    if since is None or until is None or until <= since:
        raise SystemExit("--since and --until must be valid BRT/ISO timestamps, with --until after --since")
    rows = TradeLedger(PROJECT_ROOT, Path(args.ledger)).load()
    all_trades = _load_trades(rows, since, until, args.capital, args)
    if not all_trades:
        raise SystemExit("No closed REAL_A trades in the requested opened_at window.")
    cohort, signature = _largest_uniform_group(all_trades)
    points = _equity_points(cohort, args.capital)

    print("TREND-SOL | REAL_A circuit-breaker phase 1 | LEDGER DESCRIPTIVE | READ-ONLY")
    print(f"Requested opened_at window: {_fmt(since)} -> {_fmt(until)} (end exclusive)")
    print("No entry is blocked and no replay is run by this phase.")
    _print_cohort(all_trades, cohort, signature, args)
    _write_detail(Path(args.output), points, args.capital)
    _print_distribution(points, args.capital, bool(args.detail))
    _print_worst_episodes(points, args.capital, args.episodes)
    _print_hard_stop_clusters(points, 12, args.episodes)
    print("\nNEXT STEP")
    print("Use this distribution to predeclare only a few round candidate detectors (DD, rolling PnL, optionally damage+activity).")
    print("Then compare only those through independent full-engine replay with 1h/2h/4h/6h cooldowns.")
    print(f"Per-close detail CSV: {args.output}")
    print("This output is descriptive only and is not a circuit-breaker verdict.")


def _load_trades(rows: Iterable[dict[str, Any]], since: datetime, until: datetime, capital: float, args: argparse.Namespace) -> list[ClosedTrade]:
    output = []
    fallback_notional = capital * 0.20
    for row in rows:
        if bool(row.get("phantom")) or row.get("shadow_kind") or str(row.get("position_type")) != "BOT_EXIT":
            continue
        if args.strategy_version and str(row.get("strategy_version") or "") != args.strategy_version:
            continue
        if args.hard_stop_pct is not None and not math.isclose(_number(row.get("hard_stop_pct")) or math.nan, args.hard_stop_pct, abs_tol=1e-9):
            continue
        if args.npe != "all" and _as_bool(row.get("no_progress_enabled")) != (args.npe == "true"):
            continue
        opened, closed, net = _parse_ts(row.get("opened_at")), _parse_ts(row.get("closed_at")), _number(row.get("net_pnl_pct"))
        if opened is None or closed is None or net is None or not (since <= opened < until):
            continue
        notional = _number(row.get("position_notional_usdt")) or fallback_notional
        output.append(ClosedTrade(row, opened, closed, net, notional * net / 100, _signature(row)))
    return sorted(output, key=lambda item: (item.closed, item.opened, str(item.row.get("pair_id") or "")))


def _signature(row: dict[str, Any]) -> str:
    def value(key: str) -> str:
        item = row.get(key)
        return "UNKNOWN" if item is None else str(item)
    # profit_lock_economic_floor is the price floor persisted for this specific
    # trade, not a historical configuration value. Grouping on it would split
    # one unchanged configuration into hundreds of artificial signatures.
    return " | ".join((f"version={value('strategy_version')}", f"HS={value('hard_stop_pct')}", f"NPE={value('no_progress_enabled')}"))


def _largest_uniform_group(trades: list[ClosedTrade]) -> tuple[list[ClosedTrade], str]:
    groups: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        groups[trade.signature].append(trade)
    signature, values = max(groups.items(), key=lambda item: (len(item[1]), item[0]))
    return values, signature


def _equity_points(trades: list[ClosedTrade], capital: float) -> list[EquityPoint]:
    equity = peak = capital
    output = []
    for trade in trades:
        equity += trade.net_dollars
        peak = max(peak, equity)
        output.append(EquityPoint(trade, equity, peak, peak - equity))
    return output


def _print_cohort(all_trades: list[ClosedTrade], cohort: list[ClosedTrade], signature: str, args: argparse.Namespace) -> None:
    groups = Counter(item.signature for item in all_trades)
    print("\nCOHORT / HISTORICAL COMPARABILITY")
    selected = any((args.strategy_version, args.hard_stop_pct is not None, args.npe != "all"))
    print(f"{'Explicitly selected' if selected else 'Largest ledger-identifiable uniform'} group: {len(cohort)}/{len(all_trades)} closed trades")
    print(f"Signature: {signature}")
    print(f"Uniform group span by opened_at: {_fmt(min(x.opened for x in cohort))} -> {_fmt(max(x.opened for x in cohort))}")
    print("Other ledger-identifiable signatures within the selected universe:")
    for name, count in sorted(groups.items(), key=lambda item: (-item[1], item[0])):
        if name != signature:
            print(f"  {count:4d} | {name}")
    observed_floor = sum(item.row.get("profit_lock_economic_floor") is not None for item in all_trades)
    print(f"PL economic-floor price is populated for {observed_floor}/{len(all_trades)} trades; it is telemetry, not a configuration signature.")
    print("Caveat: fields marked UNKNOWN cannot establish equality of an unpersisted historical setting.")


def _print_distribution(points: list[EquityPoint], capital: float, detail: bool) -> None:
    print("\nREALIZED EQUITY / DETERIORATION DISTRIBUTION")
    print("Each point is a closed trade. Equity uses ledger net return with persisted notional (or fixed 20% capital when absent).")
    if detail:
        print("closed BRT | net % | net $ | reason | equity $ | DD $ | DD % | 1h net/trades/HS | 2h ... | 4h ... | 6h ... | 12h ...")
        for point in points:
            pieces = []
            for hours in WINDOWS_HOURS:
                selected = _in_window(points, point.trade.closed, hours)
                pieces.append(f"{hours}h={sum(x.trade.net_dollars for x in selected):+.2f}/{len(selected)}/{sum(x.trade.row.get('exit_reason') == 'HARD_STOP' for x in selected)}")
            print(f"{_fmt(point.trade.closed)} | {point.trade.net_pct:+.3f}% | {point.trade.net_dollars:+.3f} | {point.trade.row.get('exit_reason') or '?'} | {point.equity:.3f} | {point.drawdown:.3f} | {point.drawdown / capital * 100:.3f}% | " + " | ".join(pieces))
    print("\nCandidate-scale distribution (realized/descriptive only; these are not selected rules):")
    print("metric | min | p05 | p10 | p25 | p50 | max")
    print("DD % | " + _quantile_line([item.drawdown / capital * 100 for item in points]))
    for hours in WINDOWS_HOURS:
        values = [sum(x.trade.net_dollars for x in _in_window(points, item.trade.closed, hours)) / capital * 100 for item in points]
        print(f"rolling net {hours}h % capital | " + _quantile_line(values))


def _in_window(points: list[EquityPoint], end: datetime, hours: float) -> list[EquityPoint]:
    start = end - timedelta(hours=hours)
    return [item for item in points if start < item.trade.closed <= end]


def _quantile_line(values: list[float]) -> str:
    return f"{min(values, default=0):+.3f}% | " + " | ".join(f"{_quantile(values, q):+.3f}%" for q in (0.05, 0.10, 0.25, 0.50)) + f" | {max(values, default=0):+.3f}%"


def _write_detail(path: Path, points: list[EquityPoint], capital: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        header = ["pair_id", "opened_brt", "closed_brt", "net_pct", "net_dollars", "exit_reason", "equity_dollars", "drawdown_dollars", "drawdown_pct"]
        for hours in WINDOWS_HOURS:
            header.extend((f"rolling_{hours}h_net_dollars", f"rolling_{hours}h_trades", f"rolling_{hours}h_hard_stops"))
        writer.writerow(header)
        for point in points:
            row = [point.trade.row.get("pair_id") or "", _fmt(point.trade.opened), _fmt(point.trade.closed), f"{point.trade.net_pct:.8f}", f"{point.trade.net_dollars:.8f}", point.trade.row.get("exit_reason") or "", f"{point.equity:.8f}", f"{point.drawdown:.8f}", f"{point.drawdown/capital*100:.8f}"]
            for hours in WINDOWS_HOURS:
                selected = _in_window(points, point.trade.closed, hours)
                row.extend((f"{sum(x.trade.net_dollars for x in selected):.8f}", len(selected), sum(x.trade.row.get("exit_reason") == "HARD_STOP" for x in selected)))
            writer.writerow(row)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(statistics.quantiles(ordered, n=100, method="inclusive")[int(q * 100) - 1]) if len(ordered) > 1 else ordered[0]


def _print_worst_episodes(points: list[EquityPoint], capital: float, limit: int) -> None:
    episodes = _drawdown_episodes(points)
    episodes.sort(key=lambda values: max(x.drawdown for x in values), reverse=True)
    print(f"\nALL-TIME REALIZED DRAWDOWN EXCURSIONS (top {min(limit, len(episodes))})")
    print("An excursion ends only after equity regains its prior all-time peak. It is not an acute-crisis definition.")
    if not episodes:
        print("None: equity never fell below a preceding realized peak.")
        return
    for index, values in enumerate(episodes[:limit], 1):
        start, end = values[0], values[-1]
        deepest = max(values, key=lambda x: x.drawdown)
        reasons = Counter(str(x.trade.row.get("exit_reason") or "UNKNOWN") for x in values)
        recovery = _recovery(points, deepest)
        sequence = ",".join(str(x.trade.row.get("exit_reason") or "?") for x in values)
        print(f"#{index} | {_fmt(start.trade.closed)} -> {_fmt(end.trade.closed)} | duration={(end.trade.closed-start.trade.closed).total_seconds()/3600:.2f}h | net from peak={end.equity-start.peak:+.3f}$ | max DD={deepest.drawdown:.3f}$/{deepest.drawdown/capital*100:.3f}% | HS/BE/PL/TRAIL={reasons['HARD_STOP']}/{reasons['BREAKEVEN']}/{reasons['PROFIT_LOCK']}/{reasons['TRAILING']} | recover 25/50/100={recovery} | sequence={sequence}")


def _print_hard_stop_clusters(points: list[EquityPoint], gap_hours: float, limit: int) -> None:
    """Acute descriptive clusters; small BE outcomes never split a cluster."""
    stops = [item for item in points if item.trade.row.get("exit_reason") == "HARD_STOP"]
    clusters: list[list[EquityPoint]] = []
    for item in stops:
        if not clusters or item.trade.closed - clusters[-1][-1].trade.closed > timedelta(hours=gap_hours):
            clusters.append([item])
        else:
            clusters[-1].append(item)
    clusters = [item for item in clusters if len(item) >= 2]
    clusters.sort(key=lambda values: sum(x.trade.net_dollars for x in values))
    print(f"\nACUTE HARD_STOP CLUSTERS (>=2 HS, gap <= {gap_hours:g}h; descriptive only)")
    if not clusters:
        print("None.")
        return
    for index, values in enumerate(clusters[:limit], 1):
        print(f"#{index} | {_fmt(values[0].trade.closed)} -> {_fmt(values[-1].trade.closed)} | HS={len(values)} | HS net={sum(x.trade.net_dollars for x in values):+.3f}$ | span={(values[-1].trade.closed-values[0].trade.closed).total_seconds()/3600:.2f}h")


def _drawdown_episodes(points: list[EquityPoint]) -> list[list[EquityPoint]]:
    output: list[list[EquityPoint]] = []
    active: list[EquityPoint] = []
    for point in points:
        if point.drawdown > 1e-9:
            active.append(point)
        elif active:
            output.append(active)
            active = []
    if active:
        output.append(active)
    return output


def _recovery(points: list[EquityPoint], trough: EquityPoint) -> str:
    initial = trough.drawdown
    answer = []
    for fraction in (0.25, 0.50, 1.00):
        target = initial * (1 - fraction)
        found = next((x for x in points if x.trade.closed >= trough.trade.closed and x.drawdown <= target + 1e-9), None)
        answer.append("not reached" if found is None else f"{(found.trade.closed-trough.trade.closed).total_seconds()/3600:.2f}h")
    return "/".join(answer)


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only phase-one REAL_A circuit-breaker crisis anatomy.")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True, help="End-exclusive BRT/ISO timestamp")
    parser.add_argument("--ledger", default=str(PROJECT_ROOT / "data/trades/trades_B.jsonl"))
    parser.add_argument("--strategy-version", help="Exact ledger strategy_version; use this for a replay-comparable configuration.")
    parser.add_argument("--hard-stop-pct", type=float, help="Exact ledger hard-stop percentage.")
    parser.add_argument("--npe", choices=("all", "true", "false"), default="all", help="Filter no_progress_enabled without treating missing telemetry as false.")
    parser.add_argument("--capital", type=float, default=100.0)
    parser.add_argument("--episodes", type=int, default=10)
    parser.add_argument("--detail", action="store_true", help="Also print every closed-trade row; CSV is always written.")
    parser.add_argument("--output", default=str(PROJECT_ROOT / "data/analysis/real_a_circuit_breaker_equity.csv"))
    return parser.parse_args()


def _parse_user_dt(value: str) -> datetime | None:
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        except ValueError:
            pass
    return _parse_ts(value)


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    return None


def _fmt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")


if __name__ == "__main__":
    main()
