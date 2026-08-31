from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.exchange.binance_client import SymbolFilters
from tools.h2_historical_sizing_compare import Trade, _coverage, _executable_notional, _metrics, _simulate


class H2HistoricalSizingCompareTests(unittest.TestCase):
    def test_reducer_uses_protection_timestamp_without_reconstructing_exits(self) -> None:
        start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        one = _trade("one", start, start + timedelta(hours=5), start + timedelta(hours=2))
        two = _trade("two", start + timedelta(hours=1), start + timedelta(hours=4), None, "HARD_STOP")
        three = _trade("three", start + timedelta(hours=3), start + timedelta(hours=6), start + timedelta(hours=4))
        positions, blocked = _simulate([one, two, three], 20, 100, None)
        self.assertFalse(blocked)
        self.assertEqual([item.notional for item in positions], [20, 10, 10])

    def test_executable_path_blocks_only_when_reducer_candidate_is_below_minimum(self) -> None:
        start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        trades = [_trade(str(index), start + timedelta(minutes=index), start + timedelta(hours=2), None, "HARD_STOP") for index in range(5)]
        filters = SymbolFilters(Decimal("0.001"), Decimal("0.001"), Decimal("5"), 0, 0, 0, 0)
        positions, blocked = _simulate(trades, 20, 100, filters)
        self.assertEqual([round(item.notional, 4) for item in positions], [20, 10, 6.6, 5])
        self.assertEqual([item.trade.row["pair_id"] for item in blocked], ["4"])

    def test_coverage_requires_timestamp_only_for_positions_that_must_have_protected(self) -> None:
        start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        protected = _trade("p", start, start + timedelta(hours=1), start + timedelta(minutes=10))
        hard_stop = _trade("hs", start, start + timedelta(hours=1), None, "HARD_STOP")
        missing = _trade("missing", start, start + timedelta(hours=1), None, "BREAKEVEN", missing=True)
        result = _coverage([protected, hard_stop, missing])
        self.assertEqual(result, {"trades": 3, "persisted": 1, "never": 1, "missing": 1})

    def test_capital_efficiency_uses_time_weighted_committed_capital(self) -> None:
        start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        trade = _trade("one", start, start + timedelta(hours=1), None, "HARD_STOP", net_pct=1)
        positions, _ = _simulate([trade], 20, 100, None)
        result = _metrics(positions, 100, start, start + timedelta(hours=2))
        self.assertAlmostEqual(result["avg_committed"], 10)
        self.assertAlmostEqual(result["capital_time_hours"], 20)
        self.assertAlmostEqual(result["capital_efficiency"], 0.02)


def _trade(pair_id, opened, closed, protected, reason="BREAKEVEN", missing=False, net_pct=0) -> Trade:
    row = {"pair_id": pair_id, "entry_price": 100, "net_pnl_pct": net_pct, "exit_reason": reason}
    return Trade(row, opened, closed, protected, missing, "test")
