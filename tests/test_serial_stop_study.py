from __future__ import annotations

import unittest

from tools.serial_stop_study import (
    PriceBandRule,
    RiskBudgetRule,
    run_price_band_replay,
    run_risk_budget_replay,
)


class SerialStopStudyTests(unittest.TestCase):
    def test_risk_budget_blocks_trade_below_minimum_notional(self) -> None:
        records = [
            _trade("a", "2026-07-01T00:00:00+00:00", "2026-07-01T10:00:00+00:00", 100),
            _trade("b", "2026-07-01T00:10:00+00:00", "2026-07-01T10:00:00+00:00", 100.1),
            _trade("c", "2026-07-01T00:20:00+00:00", "2026-07-01T10:00:00+00:00", 100.2),
        ]

        decisions = run_risk_budget_replay(
            records,
            [RiskBudgetRule("RISK_1", 1.0)],
            operational_balance_usdt=100,
            min_notional_usdt=10,
        )

        self.assertEqual([item.factor for item in decisions], [1.0, 1.0, 0.0])

    def test_risk_budget_scales_trade_when_remaining_notional_is_valid(self) -> None:
        records = [
            _trade(str(index), f"2026-07-01T00:{index:02d}:00+00:00", "2026-07-01T10:00:00+00:00", 100)
            for index in range(5)
        ]

        decisions = run_risk_budget_replay(
            records,
            [RiskBudgetRule("RISK_2", 2.0)],
            operational_balance_usdt=100,
            min_notional_usdt=10,
        )

        self.assertEqual([item.factor for item in decisions[:4]], [1.0, 1.0, 1.0, 1.0])
        self.assertAlmostEqual(decisions[4].factor, (2.0 - 4 * 0.44) / 0.44)

    def test_price_band_replay_removes_blocked_trade_from_context(self) -> None:
        records = [
            _trade("a", "2026-07-01T00:00:00+00:00", "2026-07-01T10:00:00+00:00", 100),
            _trade("b", "2026-07-01T00:10:00+00:00", "2026-07-01T10:00:00+00:00", 100.1),
            _trade("c", "2026-07-01T00:20:00+00:00", "2026-07-01T10:00:00+00:00", 100.2),
            _trade("d", "2026-07-01T00:30:00+00:00", "2026-07-01T10:00:00+00:00", 100.3),
        ]

        decisions = run_price_band_replay(
            records,
            [PriceBandRule("BAND025_MAX2", 0.25, 2)],
        )
        blocked = [item.record["pair_id"] for item in decisions if item.blocked]

        self.assertEqual(blocked, ["c"])


def _trade(
    pair_id: str,
    opened_at: str,
    closed_at: str,
    entry_price: float,
) -> dict:
    return {
        "pair_id": pair_id,
        "position": "B",
        "position_type": "BOT_EXIT",
        "profile": "intraday",
        "opened_at": opened_at,
        "closed_at": closed_at,
        "entry_price": entry_price,
        "hard_stop_price": entry_price * 0.98,
        "hard_stop_pct": 2.0,
        "position_notional_usdt": 20,
        "estimated_fees_pct": 0.2,
        "net_pnl_pct": 0.1,
        "exit_reason": "BREAKEVEN",
        "strategy_version": "test",
    }


if __name__ == "__main__":
    unittest.main()
