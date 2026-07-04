from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List

from src.logging_utils import JsonlLogger, now_iso
from src.position.position_base import PositionBase


class CycleManager:
    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger) -> None:
        self.project_root = project_root
        self.config = config
        self.logger = logger
        self.closed_pairs: List[Dict[str, Any]] = []
        self.closed_pair_ids: set[str] = set()
        self.closed_positions: set[tuple[str, str]] = set()

    def on_position_closed(self, position: PositionBase) -> None:
        self.closed_positions.add((position.pair_id, position.label))

    def on_pair_closed(self, positions: Iterable[PositionBase]) -> None:
        ordered = sorted(list(positions), key=lambda item: item.label)
        if len(ordered) != 2:
            return
        pair_id = ordered[0].pair_id
        if pair_id in self.closed_pair_ids:
            return

        a, b = ordered
        report = {
            "ts": now_iso(),
            "pair_id": pair_id,
            "entry_A": a.entry_price,
            "entry_B": b.entry_price,
            "exit_A": a.exit_price,
            "exit_B": b.exit_price,
            "pnl_A_pct": a.pnl_pct(a.exit_price or a.entry_price),
            "pnl_B_pct": b.pnl_pct(b.exit_price or b.entry_price),
            "diff_pp": b.pnl_pct(b.exit_price or b.entry_price) - a.pnl_pct(a.exit_price or a.entry_price),
            "exit_reason_A": a.exit_reason,
            "exit_reason_B": b.exit_reason,
            "duration_A": _duration_text(a.open_ts, a.close_ts),
            "duration_B": _duration_text(b.open_ts, b.close_ts),
        }
        self.closed_pair_ids.add(pair_id)
        self.closed_pairs.append(report)
        self._append_pair_report(report)
        self._maybe_close_cycle()

    def _maybe_close_cycle(self) -> None:
        cycle_cfg = self.config["cycle"]
        pairs_per_cycle = int(cycle_cfg["pairs_per_cycle"])
        if len(self.closed_pairs) < pairs_per_cycle:
            return

        cycle_pairs = self.closed_pairs[:pairs_per_cycle]
        self.closed_pairs = self.closed_pairs[pairs_per_cycle:]
        pnl_pct_total = sum(float(pair["pnl_A_pct"]) + float(pair["pnl_B_pct"]) for pair in cycle_pairs)
        operational_balance = float(self.config["capital"]["operational_balance_usdt"])
        estimate_profit = operational_balance * (pnl_pct_total / 100) * (
            float(self.config["capital"]["trade_size_pct"]) / 100
        )
        prolabore = estimate_profit * float(cycle_cfg["prolabore_pct"]) / 100 if estimate_profit > 0 else 0.0
        report = {
            "ts": now_iso(),
            "event": "CYCLE_CLOSED",
            "pairs": len(cycle_pairs),
            "estimated_profit_usdt": estimate_profit,
            "prolabore_usdt": prolabore,
            "stats_min_pairs": int(cycle_cfg["stats_min_pairs"]),
            "stats_ready": len(self.closed_pair_ids) >= int(cycle_cfg["stats_min_pairs"]),
        }
        self.logger.system("cycle closed", **report)

    def _append_pair_report(self, report: Dict[str, Any]) -> None:
        path = self.project_root / "data" / "paired_reports.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, ensure_ascii=False) + "\n")


def _duration_text(start: str, end: str | None) -> str | None:
    if not end:
        return None
    return f"{start} -> {end}"
