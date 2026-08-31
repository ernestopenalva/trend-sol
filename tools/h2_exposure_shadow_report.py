"""Read H2 and REAL_A ledgers as two non-compounding theoretical portfolios."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger


ARMS = {
    "REAL_A": ("data/trades/trades_B.jsonl", "data/state/open_positions.json"),
    "H2_EXPOSURE_SHADOW": ("data/trades/trades_h2_exposure_shadow.jsonl", "data/state/h2_exposure_shadow.json"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare REAL_A and H2 as fixed-capital theoretical portfolios.")
    parser.add_argument("--since", required=True, help="Início BRT: DD/MM/AAAA HH:MM ou ISO 8601.")
    parser.add_argument("--until", help="Fim BRT/ISO; padrão: agora.")
    parser.add_argument("--capital", type=float, default=100.0, help="Capital inicial teórico de ambos os braços.")
    args = parser.parse_args()
    since, until = _parse_user_dt(args.since), _parse_user_dt(args.until) or datetime.now(timezone.utc)
    if args.capital <= 0:
        raise SystemExit("--capital must be positive")
    records: dict[str, list[dict[str, Any]]] = {}
    states: dict[str, list[dict[str, Any]]] = {}
    for arm, (ledger_path, state_path) in ARMS.items():
        rows = TradeLedger(PROJECT_ROOT, PROJECT_ROOT / ledger_path).load()
        if arm == "REAL_A":
            rows = [x for x in rows if not x.get("phantom") and not x.get("shadow_kind") and x.get("position_type") == "BOT_EXIT"]
        else:
            rows = [x for x in rows if x.get("position_type") == "H2_EXPOSURE_SHADOW"]
        records[arm] = [x for x in rows if _within(x.get("opened_at"), since, until)]
        states[arm] = [
            row for row in _open_state(PROJECT_ROOT / state_path, real_a=arm == "REAL_A")
            if (_parse_ts(row.get("open_ts") or row.get("opened_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= since
        ]

    print("TREND-SOL | REAL_A vs H2_EXPOSURE_SHADOW | carteira teórica, sem compounding")
    print(f"Cohort by opened_at: {_fmt(since)} -> {_fmt(until)} | initial capital each: ${args.capital:.2f}")
    print("arm | closed | open now | net closed $ | realized balance $ | return on initial | realized max DD $ | realized max DD % | avg/max committed $ | avg/max simultaneous")
    for arm in ARMS:
        stats = _portfolio(records[arm], states[arm], args.capital, since, until)
        print(
            f"{arm} | {stats['closed']} | {stats['open']} | ${stats['net']:+.4f} | ${stats['balance']:.4f} | "
            f"{stats['return_pct']:+.4f}% | ${stats['drawdown']:.4f} | {stats['drawdown_pct']:.4f}% | "
            f"${stats['average_committed']:.4f}/${stats['max_committed']:.4f} | {stats['average_positions']:.3f}/{stats['max_positions']}"
        )
    _overlap(records, states)
    blocks = _h2_blocks(PROJECT_ROOT / "data/telemetry/h2_exposure_shadow_events.jsonl", since, until)
    _h2_sizing(records["H2_EXPOSURE_SHADOW"], PROJECT_ROOT / ARMS["H2_EXPOSURE_SHADOW"][1], blocks, since)
    print("\nNotes: PnL and fees use the runtime ledger convention (round-trip fee % applied to entry notional).")
    print("'realized max DD' is drawdown of closed-trade equity only. It is not an intratrade mark-to-market drawdown; the ledger has no continuous shared portfolio-equity series.")
    print("Open positions are excluded from closed PnL and realized balance; their committed entry notional is included in exposure metrics.")


def _portfolio(records: list[dict[str, Any]], opens: list[dict[str, Any]], capital: float, since: datetime, until: datetime) -> dict[str, float | int]:
    closed_events = []
    intervals: list[tuple[datetime, datetime, float]] = []
    for row in records:
        opened, closed = _parse_ts(row.get("opened_at")), _parse_ts(row.get("closed_at"))
        notional = _number(row.get("position_notional_usdt")) or 0.0
        if opened and closed:
            intervals.append((max(opened, since), min(closed, until), notional))
            closed_events.append((closed, _net_usdt(row)))
    for row in opens:
        opened = _parse_ts(row.get("open_ts") or row.get("opened_at"))
        notional = _number(row.get("position_notional_usdt")) or 0.0
        if opened and opened <= until:
            intervals.append((max(opened, since), until, notional))
    balance, peak, max_dd = capital, capital, 0.0
    for _, pnl in sorted(closed_events):
        balance += pnl
        peak = max(peak, balance)
        max_dd = max(max_dd, peak - balance)
    points = {(since, 0.0), (until, 0.0)}
    for opened, closed, _ in intervals:
        if closed >= opened:
            points.add((opened, 0.0))
            points.add((closed, 0.0))
    ordered = sorted(ts for ts, _ in points)
    weighted_capital = weighted_positions = 0.0
    duration = max((until - since).total_seconds(), 1.0)
    max_capital = 0.0
    max_positions = 0
    for left, right in zip(ordered, ordered[1:]):
        active = [notional for opened, closed, notional in intervals if opened <= left and closed > left]
        seconds = max(0.0, (right - left).total_seconds())
        committed = sum(active)
        weighted_capital += committed * seconds
        weighted_positions += len(active) * seconds
        max_capital, max_positions = max(max_capital, committed), max(max_positions, len(active))
    return {
        "closed": len(records), "open": len(opens), "net": balance - capital, "balance": balance,
        "return_pct": (balance - capital) / capital * 100, "drawdown": max_dd,
        "drawdown_pct": max_dd / capital * 100, "average_committed": weighted_capital / duration,
        "max_committed": max_capital, "average_positions": weighted_positions / duration,
        "max_positions": max_positions,
    }


def _net_usdt(row: dict[str, Any]) -> float:
    saved = _number(row.get("net_pnl_usdt"))
    if saved is not None:
        return saved
    return ((_number(row.get("net_pnl_pct")) or 0.0) / 100) * (_number(row.get("position_notional_usdt")) or 0.0)


def _overlap(records: dict[str, list[dict[str, Any]]], states: dict[str, list[dict[str, Any]]]) -> None:
    keys = {arm: {_signal_key(row) for row in [*records[arm], *states[arm]] if _signal_key(row) is not None} for arm in ARMS}
    real, h2 = keys["REAL_A"], keys["H2_EXPOSURE_SHADOW"]
    print("\nTrade overlap (source candle; includes positions still open):")
    print(f"common={len(real & h2)} | REAL_A only={len(real - h2)} | H2 only={len(h2 - real)}")
    print("Expected differences can arise only from each arm's own capacity/same-5m/spacing admission or real-order execution; H2 adds no voluntary risk gate.")


def _h2_blocks(path: Path, since: datetime, until: datetime) -> Counter[str]:
    counts: Counter[str] = Counter()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _within(row.get("ts"), since, until) and str(row.get("event", "")).startswith(("BLOCKED", "ENTRY_BLOCKED")):
                counts[str(row["event"])] += 1
    print("H2 admission blocks: " + (", ".join(f"{name}={count}" for name, count in sorted(counts.items())) or "none"))
    return counts


def _h2_sizing(records: list[dict[str, Any]], state_path: Path, blocks: Counter[str], since: datetime) -> None:
    entries = [
        {**(row.get("h2") if isinstance(row.get("h2"), dict) else {}), "effective_notional": row.get("position_notional_usdt")}
        for row in records
    ]
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    metadata = state.get("entry_metadata", {}) if isinstance(state, dict) else {}
    positions = state.get("positions", []) if isinstance(state, dict) else []
    for position in positions if isinstance(positions, list) else []:
        opened = _parse_ts(position.get("open_ts"))
        if position.get("status") == "OPEN" and opened and opened >= since:
            entries.append({**(metadata.get(str(position.get("pair_id")), {}) if isinstance(metadata, dict) else {}), "effective_notional": position.get("position_notional_usdt")})
    notionals = [_number(item.get("effective_notional")) for item in entries]
    notionals = [item for item in notionals if item is not None]
    uncovered = Counter(str(item.get("uncovered_count", "unknown")) for item in entries)
    versions = Counter(str(item.get("sizing_version") or "v1_harmonic_legacy") for item in entries)
    print("H2 sizing: " + (f"entries={len(entries)} | avg/min/max entry notional=${mean(notionals):.4f}/${min(notionals):.4f}/${max(notionals):.4f}" if notionals else "entries=0"))
    print("H2 sizing version: " + (", ".join(f"{key}={value}" for key, value in sorted(versions.items())) or "none"))
    print("H2 uncovered-count distribution at entry: " + (", ".join(f"N={key}:{value}" for key, value in sorted(uncovered.items())) or "none"))
    print(f"H2 blocked minNotional: {blocks['ENTRY_BLOCKED_MIN_NOTIONAL_H2']}")


def _open_state(path: Path, *, real_a: bool) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = data if isinstance(data, list) else data.get("positions", [])
    return [
        row for row in rows if row.get("status") == "OPEN" and (not real_a or (row.get("label") == "B" and not row.get("phantom", False)))
    ]


def _signal_key(row: dict[str, Any]) -> int | None:
    value = row.get("source_candle_open_time")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_user_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or BRASILIA_TZ).astimezone(timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"Invalid date/time: {value}") from exc


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError:
        return None


def _within(value: Any, since: datetime, until: datetime) -> bool:
    parsed = _parse_ts(value)
    return parsed is not None and since <= parsed <= until


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")


if __name__ == "__main__":
    main()
