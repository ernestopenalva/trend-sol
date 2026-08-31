"""Read-only diagnostic for H1: entry-price direction inside REAL_A exposure clusters."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from tools.real_a_exit_simulator import _as_float, _parse_timestamp


@dataclass(frozen=True)
class Trade:
    pair_id: str
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    exit_reason: str


@dataclass(frozen=True)
class Cluster:
    index: int
    trades: tuple[Trade, ...]

    @property
    def reasons(self) -> Counter[str]:
        return Counter(trade.exit_reason for trade in self.trades)

    @property
    def transitions(self) -> Counter[str]:
        result: Counter[str] = Counter()
        for previous, current in zip(self.trades, self.trades[1:]):
            result["ASCENDENTE" if current.entry_price > previous.entry_price else "DESCENDENTE" if current.entry_price < previous.entry_price else "IGUAL"] += 1
        return result

    @property
    def profit_lock_count(self) -> int:
        return sum(count for reason, count in self.reasons.items() if reason.startswith("PROFIT_LOCK"))

    @property
    def winner_count(self) -> int:
        return self.profit_lock_count + self.reasons["TRAILING"]

    @property
    def category(self) -> str:
        reasons = self.reasons
        winners = self.winner_count
        if reasons["HARD_STOP"] >= 2 and reasons["HARD_STOP"] > winners:
            return "RUIM_HS_DOMINANTE"
        if winners >= 2 and winners > reasons["HARD_STOP"]:
            return "BOM_LUCRO_DOMINANTE"
        return "MISTO_OU_INSUFICIENTE"


def main() -> None:
    args = _args()
    trades = _load_trades(args.ledger, _timestamp_or_none(args.since), _timestamp_or_none(args.until))
    clusters = [cluster for cluster in _clusters(trades) if len(cluster.trades) >= 3]
    _print_report(clusters)
    if args.output_dir:
        _write_csvs(args.output_dir, clusters)
        print(f"detailed CSVs: {args.output_dir}")


def _load_trades(ledger: Path, since: datetime | None, until: datetime | None) -> list[Trade]:
    output: list[Trade] = []
    seen: set[str] = set()
    with ledger.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("phantom") or row.get("shadow_kind"):
                continue
            if row.get("position_type") != "BOT_EXIT":
                continue
            pair_id = str(row.get("pair_id") or "")
            opened, closed, entry = _parse_timestamp(row.get("opened_at")), _parse_timestamp(row.get("closed_at")), _as_float(row.get("entry_price"))
            if not pair_id or pair_id in seen or opened is None or closed is None or entry is None or entry <= 0:
                continue
            if (since is not None and opened < since) or (until is not None and opened >= until):
                continue
            seen.add(pair_id)
            output.append(Trade(pair_id, opened, closed, entry, str(row.get("exit_reason") or "UNKNOWN")))
    return sorted(output, key=lambda trade: (trade.opened_at, trade.pair_id))


def _clusters(trades: list[Trade]) -> list[Cluster]:
    """Maximal connected components of open-position intervals, with no time threshold."""
    output: list[Cluster] = []
    current: list[Trade] = []
    latest_close: datetime | None = None
    for trade in trades:
        if current and latest_close is not None and trade.opened_at > latest_close:
            output.append(Cluster(len(output) + 1, tuple(current)))
            current, latest_close = [], None
        current.append(trade)
        latest_close = trade.closed_at if latest_close is None else max(latest_close, trade.closed_at)
    if current:
        output.append(Cluster(len(output) + 1, tuple(current)))
    return output


def _print_report(clusters: list[Cluster]) -> None:
    categories = Counter(cluster.category for cluster in clusters)
    print("REAL_A H1 SPACING DIAGNOSTIC | LEDGER ONLY | READ-ONLY")
    print("Predeclared cluster: maximal continuous-exposure component. Each next entry opens before the latest close already in its cluster; a new cluster starts only after exposure returned to zero. No time window is used.")
    print("Predeclared category: RUIM=at least 2 HARD_STOP and HS > (PL+TRAIL); BOM=at least 2 (PL+TRAIL) and (PL+TRAIL) > HS; otherwise MISTO/INSUFFICIENTE.")
    print(f"clusters with 3+ entries: {len(clusters)} | RUIM={categories['RUIM_HS_DOMINANTE']} | BOM={categories['BOM_LUCRO_DOMINANTE']} | MISTO={categories['MISTO_OU_INSUFICIENTE']}")
    for cluster in clusters:
        _print_cluster(cluster)
    _print_comparison(clusters)
    _print_examples(clusters)


def _print_cluster(cluster: Cluster) -> None:
    transitions, reasons = cluster.transitions, cluster.reasons
    entries = " -> ".join(f"{_brt(trade.opened_at)} @ {trade.entry_price:.4f}" for trade in cluster.trades)
    exits = " | ".join(f"{trade.pair_id}:{trade.exit_reason}" for trade in cluster.trades)
    print(f"\nCLUSTER {cluster.index} | {cluster.category} | entries={len(cluster.trades)} | ASC={transitions['ASCENDENTE']} DESC={transitions['DESCENDENTE']} EQ={transitions['IGUAL']} | HS={reasons['HARD_STOP']} BE={reasons['BREAKEVEN']} PL={cluster.profit_lock_count} TRAIL={reasons['TRAILING']}")
    print(f"entries: {entries}")
    print(f"exits: {exits}")


def _print_comparison(clusters: list[Cluster]) -> None:
    bad = [cluster for cluster in clusters if cluster.category == "RUIM_HS_DOMINANTE"]
    good = [cluster for cluster in clusters if cluster.category == "BOM_LUCRO_DOMINANTE"]
    print("\nTRANSITION COMPARISON | category | clusters | ASC | DESC | EQ | DESC share | ASC share")
    for label, group in (("RUIM", bad), ("BOM", good)):
        totals = _transition_totals(group); all_transitions = sum(totals.values())
        print(f"{label} | {len(group)} | {totals['ASCENDENTE']} | {totals['DESCENDENTE']} | {totals['IGUAL']} | {_share(totals['DESCENDENTE'], all_transitions)} | {_share(totals['ASCENDENTE'], all_transitions)}")
    bad_totals, good_totals = _transition_totals(bad), _transition_totals(good)
    bad_total, good_total = sum(bad_totals.values()), sum(good_totals.values())
    evidence = (
        len(bad) >= 3 and len(good) >= 3 and bad_total > 0 and good_total > 0
        and bad_totals["DESCENDENTE"] / bad_total >= 0.50
        and bad_totals["DESCENDENTE"] / bad_total - good_totals["DESCENDENTE"] / good_total >= 0.20
        and good_totals["ASCENDENTE"] / good_total > bad_totals["ASCENDENTE"] / bad_total
    )
    print("H1 PREDECLARED EVIDENCE RULE: at least 3 RUIM and 3 BOM clusters; DESC share in RUIM >=50% and >=20pp above BOM; ASC share in BOM above RUIM.")
    print("H1 RESULT: clear descriptive separation; H1 merits an independent later test." if evidence else "H1 RESULT: mixed/weak descriptive separation; H1 has insufficient basis and stops here.")


def _print_examples(clusters: list[Cluster]) -> None:
    hs = sorted(clusters, key=lambda item: (item.reasons["HARD_STOP"], len(item.trades)), reverse=True)[:3]
    winners = sorted(clusters, key=lambda item: (item.winner_count, len(item.trades)), reverse=True)[:3]
    print("\nEXAMPLES | largest HARD_STOP clusters: " + ", ".join(f"#{item.index} HS={item.reasons['HARD_STOP']} entries={len(item.trades)}" for item in hs))
    print("EXAMPLES | largest PROFIT_LOCK/TRAILING clusters: " + ", ".join(f"#{item.index} PL+TRAIL={item.winner_count} entries={len(item.trades)}" for item in winners))


def _transition_totals(clusters: list[Cluster]) -> Counter[str]:
    total: Counter[str] = Counter()
    for cluster in clusters:
        total.update(cluster.transitions)
    return total


def _share(value: int, total: int) -> str:
    return f"{value / total * 100:.2f}%" if total else "n/a"


def _timestamp_or_none(value: str | None) -> datetime | None:
    return _parse_timestamp(value) if value else None


def _brt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S BRT")


def _write_csvs(directory: Path, clusters: list[Cluster]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "clusters.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle); writer.writerow(("cluster", "category", "entries", "ascending", "descending", "equal", "hard_stop", "breakeven", "profit_lock", "trailing"))
        for cluster in clusters:
            transitions, reasons = cluster.transitions, cluster.reasons
            writer.writerow((cluster.index, cluster.category, len(cluster.trades), transitions["ASCENDENTE"], transitions["DESCENDENTE"], transitions["IGUAL"], reasons["HARD_STOP"], reasons["BREAKEVEN"], cluster.profit_lock_count, reasons["TRAILING"]))
    with (directory / "cluster_entries.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle); writer.writerow(("cluster", "category", "pair_id", "opened_brt", "closed_brt", "entry_price", "transition_from_previous", "exit_reason"))
        for cluster in clusters:
            for index, trade in enumerate(cluster.trades):
                direction = "" if index == 0 else "ASCENDENTE" if trade.entry_price > cluster.trades[index - 1].entry_price else "DESCENDENTE" if trade.entry_price < cluster.trades[index - 1].entry_price else "IGUAL"
                writer.writerow((cluster.index, cluster.category, trade.pair_id, _brt(trade.opened_at), _brt(trade.closed_at), trade.entry_price, direction, trade.exit_reason))


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only REAL_A H1 entry-direction diagnostic by continuous-exposure cluster.")
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--since", help="Optional opened_at inclusive ISO timestamp.")
    parser.add_argument("--until", help="Optional opened_at exclusive ISO timestamp.")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "data" / "analysis" / "real_a_spacing_h1_clusters")
    return parser.parse_args()


if __name__ == "__main__":
    main()
