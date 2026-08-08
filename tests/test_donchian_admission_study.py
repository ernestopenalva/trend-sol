from __future__ import annotations

import unittest

from tools.donchian_admission_study import _net_without_top
from tools.donchian_portfolio_replay import PortfolioTrade


def trade(net_usdt: float) -> PortfolioTrade:
    return PortfolioTrade(
        symbol="TESTUSDT",
        opened_ms=0,
        closed_ms=1,
        entry_price=100,
        exit_price=100,
        quantity=0.2,
        notional_usdt=20,
        gross_pct=0,
        net_pct=net_usdt / 20 * 100,
        gross_usdt=0,
        fees_usdt=0,
        net_usdt=net_usdt,
        exit_reason="TEST",
        holding_hours=1,
        breakout_margin_pct=1,
    )


class DonchianAdmissionStudyTests(unittest.TestCase):
    def test_net_without_top_removes_largest_winners(self) -> None:
        trades = [trade(10), trade(5), trade(2), trade(-4)]

        self.assertEqual(_net_without_top(trades, 1), 3)
        self.assertEqual(_net_without_top(trades, 3), -4)


if __name__ == "__main__":
    unittest.main()
