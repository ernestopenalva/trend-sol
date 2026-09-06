from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.h2_exposure_shadow_report import _phantom_realized_stats, _portfolio


class H2ExposureShadowReportTests(unittest.TestCase):
    def test_portfolio_uses_variable_notionals_and_ledger_fee_convention(self) -> None:
        since = datetime(2026, 8, 1, tzinfo=timezone.utc)
        until = datetime(2026, 8, 2, tzinfo=timezone.utc)
        records = [
            {"opened_at": "2026-08-01T01:00:00+00:00", "closed_at": "2026-08-01T02:00:00+00:00", "position_notional_usdt": 40, "net_pnl_pct": 1},
            {"opened_at": "2026-08-01T03:00:00+00:00", "closed_at": "2026-08-01T04:00:00+00:00", "position_notional_usdt": 10, "net_pnl_usdt": -0.5},
        ]
        stats = _portfolio(records, [], 100, since, until)
        self.assertAlmostEqual(stats["net"], -0.1)
        self.assertAlmostEqual(stats["max_committed"], 40)
        self.assertEqual(stats["max_positions"], 1)

    def test_phantom_stats_use_each_trade_trigger_not_the_market_fill(self) -> None:
        rows = [
            {
                "opened_at": "2026-08-01T01:00:00+00:00",
                "closed_at": "2026-08-01T02:00:00+00:00",
                "entry_price": 100,
                "exit_price": 105,
                "exit_trigger_price": 99,
                "qty": 2,
                "position_notional_usdt": 200,
                "estimated_fees_pct": 0.2,
            },
        ]
        stats = _phantom_realized_stats(rows, 100)
        self.assertIsNotNone(stats)
        assert stats is not None
        self.assertAlmostEqual(stats["net"], -2.4)
        self.assertAlmostEqual(stats["drawdown"], 2.4)

    def test_phantom_stats_refuse_to_mix_missing_trigger_rows(self) -> None:
        rows = [{"closed_at": "2026-08-01T02:00:00+00:00"}]
        self.assertIsNone(_phantom_realized_stats(rows, 100))
