from __future__ import annotations

import unittest

from tools.cohort_study import (
    CohortRule,
    SizingRule,
    run_replay,
    run_sizing_replay,
    sizing_economics,
)


class CohortStudyTests(unittest.TestCase):
    def test_static_replay_blocks_fourth_and_fifth_under_pressure(self) -> None:
        records = [
            _trade("a", "2026-07-01T00:00:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            _trade("b", "2026-07-01T00:10:00+00:00", "2026-07-01T10:00:00+00:00", 101, 0.1),
            _trade("c", "2026-07-01T00:20:00+00:00", "2026-07-01T10:00:00+00:00", 102, 0.1),
            _trade("d", "2026-07-01T03:00:00+00:00", "2026-07-01T04:00:00+00:00", 98, -2.2, "HARD_STOP"),
            _trade("e", "2026-07-01T03:10:00+00:00", "2026-07-01T04:10:00+00:00", 98.1, 0.3),
        ]
        rule = CohortRule("BASE", 3, 0.75, 120, 1.0)

        decisions = run_replay(records, [rule], ["static"])
        blocked = [item for item in decisions if item.blocked]

        self.assertEqual([item.record["pair_id"] for item in blocked], ["d", "e"])
        self.assertEqual(blocked[0].metrics.open_positions, 3)
        self.assertEqual(blocked[1].metrics.open_positions, 4)

    def test_sequential_replay_removes_blocked_trade_from_later_context(self) -> None:
        records = [
            _trade("a", "2026-07-01T00:00:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            _trade("b", "2026-07-01T00:10:00+00:00", "2026-07-01T03:05:00+00:00", 101, 0.1),
            _trade("c", "2026-07-01T00:20:00+00:00", "2026-07-01T10:00:00+00:00", 102, 0.1),
            _trade("d", "2026-07-01T03:00:00+00:00", "2026-07-01T10:00:00+00:00", 98, -2.2, "HARD_STOP"),
            _trade("e", "2026-07-01T03:10:00+00:00", "2026-07-01T04:10:00+00:00", 98.1, 0.3),
        ]
        rule = CohortRule("BASE", 3, 0.75, 120, 1.0)

        decisions = run_replay(records, [rule], ["sequential"])
        blocked = [item for item in decisions if item.blocked]

        self.assertEqual([item.record["pair_id"] for item in blocked], ["d"])

    def test_replay_excludes_phantoms_and_does_not_score_cleanup(self) -> None:
        records = [
            _trade("a", "2026-07-01T00:00:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            _trade("b", "2026-07-01T00:10:00+00:00", "2026-07-01T10:00:00+00:00", 101, 0.1),
            _trade("c", "2026-07-01T00:20:00+00:00", "2026-07-01T10:00:00+00:00", 102, 0.1),
            _trade("cleanup", "2026-07-01T03:00:00+00:00", "2026-07-01T04:00:00+00:00", 98, -0.5),
            {
                **_trade(
                    "phantom",
                    "2026-07-01T03:01:00+00:00",
                    "2026-07-01T04:00:00+00:00",
                    97,
                    -2.2,
                    "HARD_STOP",
                ),
                "phantom": True,
            },
        ]
        records[3]["strategy_version"] = "b_atr_cleanup"
        rule = CohortRule("BASE", 3, 0.75, 120, 1.0)

        decisions = run_replay(records, [rule], ["static"])
        blocked = [item for item in decisions if item.blocked]

        self.assertEqual([item.record["pair_id"] for item in blocked], ["cleanup"])
        self.assertFalse(blocked[0].scored)

    def test_sizing_replay_reduces_fourth_entry_when_cohort_is_negative(self) -> None:
        records = [
            _trade("a", "2026-07-01T00:00:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            _trade("b", "2026-07-01T00:10:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            _trade("c", "2026-07-01T00:20:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            {
                **_trade(
                    "phantom",
                    "2026-07-01T00:30:00+00:00",
                    "2026-07-01T10:00:00+00:00",
                    100,
                    -2.2,
                ),
                "phantom": True,
            },
            _trade("d", "2026-07-01T03:00:00+00:00", "2026-07-01T10:00:00+00:00", 99.5, 0.2),
        ]
        rule = SizingRule("CLAUDE_HALF", 3, -0.3, 0.66, 0.5)

        decisions = run_sizing_replay(records, [rule])
        reduced = [item for item in decisions if item.reduced]

        self.assertEqual([item.record["pair_id"] for item in reduced], ["d"])
        self.assertEqual(reduced[0].metrics.negative_positions, 3)
        self.assertEqual(reduced[0].metrics.open_positions, 3)

    def test_sizing_economics_weights_winners_and_losses_by_notional(self) -> None:
        records = [
            _trade("a", "2026-07-01T00:00:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            _trade("b", "2026-07-01T00:10:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            _trade("c", "2026-07-01T00:20:00+00:00", "2026-07-01T10:00:00+00:00", 100, 0.1),
            _trade("d", "2026-07-01T03:00:00+00:00", "2026-07-01T10:00:00+00:00", 99.5, 0.2),
            _trade("e", "2026-07-01T03:10:00+00:00", "2026-07-01T10:00:00+00:00", 99.4, -2.2, "HARD_STOP"),
        ]
        rule = SizingRule("CLAUDE_HALF", 3, -0.3, 0.66, 0.5)

        decisions = run_sizing_replay(records, [rule])
        economics = sizing_economics(decisions, operational_balance_usdt=100)

        self.assertAlmostEqual(economics.actual_net_usdt, -0.4)
        self.assertAlmostEqual(economics.hypothetical_net_usdt, -0.2)
        self.assertAlmostEqual(economics.delta_usdt, 0.2)
        self.assertAlmostEqual(economics.saved_losses_usdt, 0.22)
        self.assertAlmostEqual(economics.foregone_winners_usdt, 0.02)
        self.assertAlmostEqual(economics.balance_delta_pct, 0.2)


def _trade(
    pair_id: str,
    opened_at: str,
    closed_at: str,
    entry_price: float,
    net_pnl_pct: float,
    exit_reason: str = "BREAKEVEN",
) -> dict:
    return {
        "pair_id": pair_id,
        "position": "B",
        "position_type": "BOT_EXIT",
        "profile": "intraday",
        "opened_at": opened_at,
        "closed_at": closed_at,
        "entry_price": entry_price,
        "entry_atr": 0.1,
        "qty": 0.2,
        "position_notional_usdt": 20,
        "estimated_fees_pct": 0.2,
        "net_pnl_pct": net_pnl_pct,
        "exit_reason": exit_reason,
        "strategy_version": "test",
    }


if __name__ == "__main__":
    unittest.main()
