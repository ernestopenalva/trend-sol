"""Read-only historical sizing comparison: REAL_A baseline versus H2 v2."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_DOWN
from pathlib import Path
from statistics import mean
from typing import Any, Iterable, Optional
import sys

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.exchange.binance_client import BinanceClient, SymbolFilters
from src.trade_ledger import TradeLedger


CLEAN_START = datetime(2026, 8, 15, 18, 52, tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
# _no_progress_due() returns false once BE has armed, so all three reasons
# are direct evidence that the trade never became economically protected.
KNOWN_UNPROTECTED_REASONS = {"HARD_STOP", "REVIEW_STOP", "NO_PROGRESS_EXIT"}


@dataclass(frozen=True)
class Trade:
    row: dict[str, Any]
    opened: datetime
    closed: datetime
    protected_at: Optional[datetime]
    protection_missing: bool
    version: str


@dataclass(frozen=True)
class Position:
    trade: Trade
    notional: float


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only REAL_A vs H2 v2 historical sizing comparison.")
    parser.add_argument("--since", help="Optional BRT/ISO start; never extends before clean telemetry start.")
    parser.add_argument("--until", help="Optional BRT/ISO end (inclusive).")
    parser.add_argument("--capital", type=float, default=100.0, help="Theoretical initial capital.")
    parser.add_argument("--min-notional", type=float, help="Override executable minimum quote notional, for reproducibility.")
    parser.add_argument("--min-qty", type=float, help="Override executable minimum quantity, for reproducibility.")
    parser.add_argument("--step-size", type=float, help="Override executable LOT_SIZE step, for reproducibility.")
    args = parser.parse_args()
    if args.capital <= 0:
        raise SystemExit("--capital must be positive")
    config = yaml.safe_load((PROJECT_ROOT / "config/config.yaml").read_text(encoding="utf-8"))
    requested_since = _parse_user_dt(args.since) or CLEAN_START
    since = max(requested_since, CLEAN_START)
    until = _parse_user_dt(args.until) or datetime.now(timezone.utc)
    if until <= since:
        raise SystemExit("--until must be after the clean cohort start")
    base = args.capital * float(config["capital"]["trade_size_pct"]) / 100
    rows = TradeLedger(PROJECT_ROOT).load()
    trades = _load_clean_trades(rows, since, until)
    coverage = _coverage(trades)
    print("TREND-SOL | historical sizing | REAL_A baseline vs H2 v2 reducer | READ-ONLY")
    print(f"Clean telemetry cohort: {_fmt(since)} -> {_fmt(until)} (pre-15/08/2026 18:52 BRT excluded; no reconstruction)")
    print(f"Source: data/trades/trades_B.jsonl | capital=${args.capital:.2f} | REAL_A base B=${base:.6f}")
    print(f"Protection coverage: trades={coverage['trades']} | persisted timestamp={coverage['persisted']} | known never protected={coverage['never']} | needs reconstruction={coverage['missing']}")
    _version_coverage(trades)
    comparison_trades, excluded = _strict_observable_subset(trades)
    if excluded:
        print(f"Strict observable subset: {len(comparison_trades)}/{len(trades)} trades; excluded {len(excluded)} entries inside {coverage['missing']} unresolved protection window(s). No reconstruction was attempted.")
    if not comparison_trades:
        print("\nNo REAL_A closed trades in the clean cohort.")
        return
    filters, filter_source = _filters(args, config)
    print(f"Executable filters: {filter_source} | min_notional={filters.min_notional} | min_qty={filters.min_qty} | step_size={filters.step_size}")
    real = [Position(trade, base) for trade in comparison_trades]
    h2_theoretical, _ = _simulate(comparison_trades, base, args.capital, None)
    h2_executable, blocked = _simulate(comparison_trades, base, args.capital, filters)
    print("\narm | trades | initial $ | net PnL $ | final balance $ | return % | realized max DD $ | realized max DD % | avg/max committed $ | avg/max simultaneous | avg/min/max entry $")
    for name, positions in (("REAL_A", real), ("H2_THEORETICAL", h2_theoretical), ("H2_EXECUTABLE", h2_executable)):
        _print_metrics(name, _metrics(positions, args.capital, since, until))
    print(f"\nH2 executable blocked only by minNotional/quantity: {len(blocked)}")
    if blocked:
        print("blocked pair_ids: " + ", ".join(item.trade.row.get("pair_id", "?") for item in blocked))
    _h2_breakdown("H2 theoretical", h2_theoretical)
    _h2_breakdown("H2 executable", h2_executable)
    _period_breakdown("Calendar months", real, h2_theoretical, h2_executable)
    _period_breakdown("Non-overlapping ISO weeks", real, h2_theoretical, h2_executable, weekly=True)
    _version_contributions(real, h2_theoretical, h2_executable)
    _verdict(_metrics(real, args.capital, since, until), _metrics(h2_executable, args.capital, since, until))


def _load_clean_trades(rows: Iterable[dict[str, Any]], since: datetime, until: datetime) -> list[Trade]:
    output = []
    for row in rows:
        if bool(row.get("phantom")) or row.get("shadow_kind") or str(row.get("position_type")) != "BOT_EXIT":
            continue
        opened, closed = _parse_ts(row.get("opened_at")), _parse_ts(row.get("closed_at"))
        if not opened or not closed or not (since <= opened <= until):
            continue
        protected = _parse_ts(row.get("be_armed_at"))
        reason = str(row.get("exit_reason") or "")
        if protected is not None and not (opened <= protected <= closed):
            protected = None
            missing = True
        else:
            missing = protected is None and reason not in KNOWN_UNPROTECTED_REASONS
        output.append(Trade(row, opened, closed, protected, missing, _version(row)))
    return sorted(output, key=lambda item: (item.opened, item.closed, str(item.row.get("pair_id"))))


def _coverage(trades: Iterable[Trade]) -> dict[str, int]:
    values = list(trades)
    return {
        "trades": len(values),
        "persisted": sum(item.protected_at is not None for item in values),
        "never": sum(item.protected_at is None and not item.protection_missing for item in values),
        "missing": sum(item.protection_missing for item in values),
    }


def _strict_observable_subset(trades: list[Trade]) -> tuple[list[Trade], list[Trade]]:
    """Exclude a missing-protection trade and entries during its live interval.

    This keeps the remaining H2 state exact without inventing the missing
    protection time.  Positions outside the uncertainty interval remain valid.
    """
    unknown = [item for item in trades if item.protection_missing]
    if not unknown:
        return trades, []
    included, excluded = [], []
    for item in trades:
        affected = any(window.opened <= item.opened < window.closed for window in unknown)
        (excluded if affected else included).append(item)
    return included, excluded


def _version(row: dict[str, Any]) -> str:
    strategy = str(row.get("strategy_version") or "unknown")
    hard_stop = _number(row.get("hard_stop_pct"))
    npe = "NPE_ON" if bool(row.get("no_progress_enabled")) else "NPE_OFF"
    floor = "PL_FLOOR_OBSERVED" if row.get("profit_lock_economic_floor") is not None else "PL_FLOOR_UNOBSERVED"
    return f"{strategy} | HS={hard_stop if hard_stop is not None else '?'} | {npe} | {floor}"


def _version_coverage(trades: list[Trade]) -> None:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for item in trades:
        groups[item.version].append(item)
    print("Historical configuration groups (only fields identifiable in ledger):")
    for version, values in groups.items():
        coverage = _coverage(values)
        print(f"- {version}: trades={coverage['trades']}, persisted={coverage['persisted']}, needs reconstruction={coverage['missing']}")


def _filters(args: argparse.Namespace, config: dict[str, Any]) -> tuple[SymbolFilters, str]:
    manual = (args.min_notional, args.min_qty, args.step_size)
    if any(value is not None for value in manual):
        if not all(value is not None and value > 0 for value in manual):
            raise SystemExit("Manual executable filters require --min-notional, --min-qty and --step-size together.")
        return SymbolFilters(Decimal(str(args.min_qty)), Decimal(str(args.step_size)), Decimal(str(args.min_notional)), 0, 0, 0, 0), "manual CLI override"
    execution = config["execution"]
    client = BinanceClient(str(execution["testnet_url"]), int(execution["recv_window_ms"]), use_server_time_sync=False, http_timeout_seconds=int(execution.get("http_timeout_seconds", 8)))
    return client.symbol_filters(str(config["symbol"])), "current Binance Spot Testnet exchangeInfo (not historical filter archive)"


def _simulate(trades: list[Trade], base: float, capital: float, filters: Optional[SymbolFilters]) -> tuple[list[Position], list[Position]]:
    open_positions: list[Position] = []
    accepted: list[Position] = []
    blocked: list[Position] = []
    for trade in trades:
        open_positions = [position for position in open_positions if position.trade.closed > trade.opened]
        uncovered = sum(position.trade.protected_at is None or position.trade.protected_at > trade.opened for position in open_positions)
        candidate = base / (uncovered + 1)
        committed = sum(position.notional for position in open_positions)
        notional = candidate if filters is None else _executable_notional(candidate, trade, filters)
        if notional is None or committed + notional > capital + 1e-9:
            blocked.append(Position(trade, candidate))
            continue
        position = Position(trade, notional)
        accepted.append(position)
        open_positions.append(position)
    return accepted, blocked


def _executable_notional(candidate: float, trade: Trade, filters: SymbolFilters) -> Optional[float]:
    price = Decimal(str(_number(trade.row.get("entry_price")) or 0))
    if price <= 0 or filters.step_size <= 0:
        return None
    candidate_d = Decimal(str(candidate))
    quantity = _floor_step(candidate_d / price, filters.step_size)
    required = _ceil_step(max(filters.min_qty, filters.min_notional / price), filters.step_size)
    if quantity < required:
        return None
    return float(quantity * price)


def _metrics(positions: list[Position], capital: float, since: datetime, until: datetime) -> dict[str, float | int | None]:
    pnl_events = sorted(((item.trade.closed, _net_dollars(item)) for item in positions), key=lambda item: item[0])
    balance = peak = capital
    drawdown = 0.0
    for _, pnl in pnl_events:
        balance += pnl
        peak = max(peak, balance)
        drawdown = max(drawdown, peak - balance)
    points = sorted({since, until, *(item.trade.opened for item in positions), *(item.trade.closed for item in positions)})
    capital_time = average_committed = average_positions = 0.0
    max_committed = 0.0
    max_positions = 0
    for left, right in zip(points, points[1:]):
        active = [item for item in positions if item.trade.opened <= left and item.trade.closed > left]
        committed = sum(item.notional for item in active)
        seconds = (right - left).total_seconds()
        capital_time += committed * seconds
        average_positions += len(active) * seconds
        max_committed, max_positions = max(max_committed, committed), max(max_positions, len(active))
    duration = max((until - since).total_seconds(), 1)
    average_committed = capital_time / duration
    notionals = [item.notional for item in positions]
    net = balance - capital
    return {
        "trades": len(positions), "net": net, "balance": balance, "return": net / capital * 100,
        "dd": drawdown, "dd_pct": drawdown / capital * 100, "avg_committed": average_committed,
        "max_committed": max_committed, "avg_positions": average_positions / duration, "max_positions": max_positions,
        "avg_entry": mean(notionals) if notionals else None, "min_entry": min(notionals) if notionals else None,
        "max_entry": max(notionals) if notionals else None, "capital_time_hours": capital_time / 3600,
        "capital_efficiency": net / average_committed if average_committed else None,
        "capital_time_efficiency": net / (capital_time / 3600) if capital_time else None,
    }


def _net_dollars(position: Position) -> float:
    net_pct = _number(position.trade.row.get("net_pnl_pct"))
    if net_pct is None:
        gross = _number(position.trade.row.get("gross_pnl_pct")) or 0.0
        fees = _number(position.trade.row.get("estimated_fees_pct")) or 0.0
        net_pct = gross - fees
    return position.notional * net_pct / 100


def _print_metrics(name: str, values: dict[str, float | int | None]) -> None:
    def n(key: str) -> str:
        value = values[key]
        return "n/a" if value is None else f"${float(value):.4f}"
    print(f"{name} | {values['trades']} | $100.00 | ${float(values['net']):+.4f} | ${float(values['balance']):.4f} | {float(values['return']):+.4f}% | ${float(values['dd']):.4f} | {float(values['dd_pct']):.4f}% | ${float(values['avg_committed']):.4f}/${float(values['max_committed']):.4f} | {float(values['avg_positions']):.3f}/{values['max_positions']} | {n('avg_entry')}/{n('min_entry')}/{n('max_entry')}")
    print(f"  efficiency: net/avg committed={n('capital_efficiency')} | capital-time={float(values['capital_time_hours']):.4f} USDT*h | net/capital-time={n('capital_time_efficiency')} per USDT*h")


def _h2_breakdown(name: str, positions: list[Position]) -> None:
    groups: dict[int, list[Position]] = defaultdict(list)
    for position in positions:
        # Recovered from the fixed sizing formula; N is exact for accepted H2 sequence.
        if position.notional <= 0:
            continue
        groups[_n_from_position(position, positions)].append(position)
    print(f"{name} uncovered-count entries: " + ", ".join(f"N={key}: {len(values)} (avg ${mean(x.notional for x in values):.4f})" for key, values in sorted(groups.items())))


def _n_from_position(target: Position, positions: list[Position]) -> int:
    active = [item for item in positions if item.trade.opened < target.trade.opened and item.trade.closed > target.trade.opened]
    return sum(item.trade.protected_at is None or item.trade.protected_at > target.trade.opened for item in active)


def _period_breakdown(title: str, real: list[Position], theoretical: list[Position], executable: list[Position], weekly: bool = False) -> None:
    print(f"\n{title} (contributions; not independent re-simulations):")
    keys = sorted({_period_key(item.trade.opened, weekly) for item in [*real, *theoretical, *executable]})
    for key in keys:
        values = []
        for arm in (real, theoretical, executable):
            selected = [item for item in arm if _period_key(item.trade.opened, weekly) == key]
            values.append(f"{len(selected)} trades / ${sum(_net_dollars(item) for item in selected):+.4f}")
        print(f"{key} | REAL_A {values[0]} | H2 theoretical {values[1]} | H2 executable {values[2]}")


def _version_contributions(real: list[Position], theoretical: list[Position], executable: list[Position]) -> None:
    print("\nHistorical configuration contributions (same continuous simulation, separated for audit):")
    versions = sorted({item.trade.version for item in [*real, *theoretical, *executable]})
    for version in versions:
        line = []
        for arm in (real, theoretical, executable):
            selected = [item for item in arm if item.trade.version == version]
            line.append(f"{len(selected)} / ${sum(_net_dollars(item) for item in selected):+.4f}")
        print(f"{version}\n  REAL_A trades/net={line[0]} | H2 theoretical={line[1]} | H2 executable={line[2]}")


def _verdict(real: dict[str, float | int | None], h2: dict[str, float | int | None]) -> None:
    if float(h2["net"]) > float(real["net"]) and float(h2["dd"]) < float(real["dd"]):
        label = "MELHOR"
    elif float(h2["net"]) < float(real["net"]) and float(h2["dd"]) > float(real["dd"]):
        label = "PIOR"
    else:
        label = "TRADE-OFF RETORNO/RISCO"
    print(f"\nVEREDITO (H2 executável vs REAL_A): {label}. Não implica alteração de runtime ou fórmula.")


def _period_key(value: datetime, weekly: bool) -> str:
    return f"{value.isocalendar().year}-W{value.isocalendar().week:02d}" if weekly else value.strftime("%Y-%m")


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _parse_user_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        except ValueError:
            pass
    return _parse_ts(value)


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError:
        return None


def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")


if __name__ == "__main__":
    main()
