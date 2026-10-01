"""Explain the fixed FAST_DROP candidate by month; diagnostic/read-only."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from tools.be_off_cb_deterioration_study import COMBO_RULE
from tools.cohort_study import _load_config
from tools.ge_replay_study import WARMUP_CANDLES, ReplayResult, SignalEvent, generate_ge_signals, load_ge_market_data, run_universe
from tools.market_bot_replay import MINUTE_MS, _round_trip_fees_pct
from tools.market_selection_study import BinancePublicClient, MarketCandle
from tools.real_a_circuit_breaker_replay import CircuitGuard

MONTHS = ("2026-06", "2026-07", "2026-08")


class TrackingCircuitGuard(CircuitGuard):
    def __init__(self, capital: float, notional: float) -> None:
        super().__init__(COMBO_RULE, 6.0, capital, notional)
        self.crisis_starts: list[int] = []
        self.paused_boundaries: set[int] = set()

    def allows(self, boundary: int, result: ReplayResult) -> bool:
        for trade in result.trades[self.cursor:]:
            dollars = self.notional * trade.net_pct / 100
            self.equity += dollars
            self.peak = max(self.peak, self.equity)
            self.history.append((trade.closed_ms, dollars))
        self.cursor = len(result.trades)
        state = self._state(boundary)
        if state and not self.was_true:
            self.pause_until = max(self.pause_until, boundary + self.cooldown_ms)
            self.crises += 1
            self.crisis_starts.append(boundary)
        self.was_true = state
        paused = boundary < self.pause_until
        if paused:
            self.paused_minutes += 1
            self.paused_boundaries.add(boundary)
        return not paused


def month_of(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, BRASILIA_TZ).strftime("%Y-%m")


def overlap_minutes(start: int, end: int, month: str) -> int:
    begin = datetime.fromisoformat(month + "-01T00:00:00-03:00").astimezone(timezone.utc)
    if month.endswith("06"):
        finish = datetime.fromisoformat("2026-07-01T00:00:00-03:00").astimezone(timezone.utc)
    elif month.endswith("07"):
        finish = datetime.fromisoformat("2026-08-01T00:00:00-03:00").astimezone(timezone.utc)
    else:
        finish = datetime.fromisoformat("2026-09-01T00:00:00-03:00").astimezone(timezone.utc)
    lo, hi = max(start, int(begin.timestamp() * 1000)), min(end, int(finish.timestamp() * 1000))
    return max(0, (hi - lo) // MINUTE_MS)


def classify_cb_time(at_ms: int, crisis_starts: Sequence[int], cooldown_ms: int, month: str) -> str:
    if any(start <= at_ms < start + cooldown_ms for start in crisis_starts):
        return "DURING"
    starts = [item for item in crisis_starts if month_of(item) == month]
    return "BEFORE" if not starts or at_ms < starts[0] else "AFTER"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--cache-dir", default="data/studies/be_off_cb_deterioration/klines")
    parser.add_argument("--crossings", default="data/studies/be_off_cb_deterioration/crossings.jsonl")
    parser.add_argument("--offline", action="store_true", default=True)
    args = parser.parse_args()
    config = deepcopy(effective_config(_load_config(Path(args.config))))
    config.setdefault("risk", {})["breakeven"] = {"mode": "off"}
    capital = float(config["capital"]["operational_balance_usdt"])
    notional = capital * float(config["capital"]["trade_size_pct"]) / 100
    spread = float(config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0))
    start = datetime.fromisoformat("2026-06-01T00:00:00-03:00").astimezone(timezone.utc)
    end = datetime.fromisoformat("2026-09-01T00:00:00-03:00").astimezone(timezone.utc)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000) - 1
    client = BinancePublicClient(str(config.get("market_data", {}).get("rest_url") or "https://api.binance.com"), 20)
    cache = Path(args.cache_dir)
    candles: dict[str, list[MarketCandle]] = {
        interval: load_ge_market_data(client, str(config.get("symbol") or "SOLUSDT"), interval, start_ms - WARMUP_CANDLES * 15 * MINUTE_MS, end_ms, cache, True)
        for interval in ("1m", "5m", "15m")
    }
    signals, _ = generate_ge_signals(config, candles["1m"], candles["5m"], candles["15m"], start_ms, end_ms, 0)
    crossing_rows = [json.loads(line) for line in Path(args.crossings).read_text(encoding="utf-8").splitlines() if line.strip()]
    fees = _round_trip_fees_pct(config)
    cf_net_pct = -0.50 - fees - spread / 2 / 100
    for path in ("HIGH_FIRST", "LOW_FIRST"):
        guard = TrackingCircuitGuard(capital, notional)
        replay = run_universe(name=f"AUDIT_{path}", lookback=0, config=config, signals=signals, execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=path, round_trip_spread_bps=spread, admission_guard=guard.allows)
        trades = {trade.opened_ms: trade for trade in replay.trades}
        print(f"\n{path}")
        print("month | triggers | HS | PL | TRAIL | HS rate | avg HS saving $ | avg sacrificed loss $ | delta $ | delta/trigger $")
        for month in MONTHS:
            fast = [row for row in crossing_rows if row["path"] == path and row["opened_at_brt"][:7] == month and row["level_pct"] == 0.5 and row["velocity"].get("5") is not None and row["velocity"]["5"] <= -0.10]
            exits = Counter(row["exit_reason"] for row in fast)
            hs_delta = [(cf_net_pct - row["net_pct"]) * notional / 100 for row in fast if row["exit_reason"] == "HS"]
            sacrificed = [(row["net_pct"] - cf_net_pct) * notional / 100 for row in fast if row["exit_reason"] in {"PL", "TRAIL"}]
            delta = sum((cf_net_pct - row["net_pct"]) * notional / 100 for row in fast)
            rate = exits["HS"] / len(fast) if fast else 0
            print(f"{month} | {len(fast)} | {exits['HS']} | {exits['PL']} | {exits['TRAIL']} | {rate:.1%} | {sum(hs_delta)/len(hs_delta) if hs_delta else 0:.4f} | {sum(sacrificed)/len(sacrificed) if sacrificed else 0:.4f} | {delta:+.4f} | {delta/len(fast) if fast else 0:+.4f}")

        print("month | CB crises | cooldown h | blocked opportunities | FAST before | during cooldown | after")
        for month in MONTHS:
            starts = [item for item in guard.crisis_starts if month_of(item) == month]
            cooldown_minutes = sum(1 for boundary in guard.paused_boundaries if month_of(boundary) == month)
            # Guard is checked before slot/spacing, so these are raw entry signals suppressed by CB.
            blocked = sum(1 for signal in signals if month_of(signal.boundary_ms) == month and signal.boundary_ms in guard.paused_boundaries)
            fast = [row for row in crossing_rows if row["path"] == path and row["opened_at_brt"][:7] == month and row["level_pct"] == 0.5 and row["velocity"].get("5") is not None and row["velocity"]["5"] <= -0.10]
            timing = Counter(classify_cb_time(int(row["crossed_ms"]), guard.crisis_starts, guard.cooldown_ms, month) for row in fast)
            print(f"{month} | {len(starts)} | {cooldown_minutes/60:.2f} | {blocked} | {timing['BEFORE']} | {timing['DURING']} | {timing['AFTER']}")

        print("month | admitted trades | reached -0.50% | FAST_DROP | fast/reached | recovered after -0.50% | recovery rate | median MAE reached")
        rows_half = [row for row in crossing_rows if row["path"] == path and row["level_pct"] == 0.5]
        for month in MONTHS:
            monthly_trades = [trade for trade in replay.trades if month_of(trade.opened_ms) == month]
            reached = [row for row in rows_half if row["opened_at_brt"][:7] == month]
            fast = [row for row in reached if row["velocity"].get("5") is not None and row["velocity"]["5"] <= -0.10]
            recovered = [row for row in reached if row["exit_reason"] in {"PL", "TRAIL"}]
            maes = sorted((trades[int(row["opened_ms"])].trough_price / trades[int(row["opened_ms"])].entry_price - 1) * 100 for row in reached if int(row["opened_ms"]) in trades)
            median_mae = maes[len(maes)//2] if maes else 0
            print(f"{month} | {len(monthly_trades)} | {len(reached)} | {len(fast)} | {len(fast)/len(reached) if reached else 0:.1%} | {len(recovered)} | {len(recovered)/len(reached) if reached else 0:.1%} | {median_mae:.3f}%")

    print("\nMETHOD | baseline is a systemic BE_OFF_CB replay; FAST_DROP deltas are trade-level counterfactuals and do NOT rerun downstream CB, slots, spacing, admissions, or replacement trades.")


if __name__ == "__main__":
    main()
