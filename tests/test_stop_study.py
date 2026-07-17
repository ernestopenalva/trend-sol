from __future__ import annotations

import unittest

from tools.stop_study import run_study


class StopStudyTests(unittest.TestCase):
    def test_end_to_end_rules_and_episode_grouping(self) -> None:
        records = [
            {
                "pair_id": "winner",
                "opened_at": "2026-07-01T00:00:00+00:00",
                "closed_at": "2026-07-01T10:00:00+00:00",
                "entry_price": 100,
                "entry_atr": 0.25,
                "exit_price": 105,
                "trough_price": 98,
                "trough_at": "2026-07-01T02:00:00+00:00",
                "net_pnl_pct": 4.8,
                "exit_reason": "TRAILING",
            },
            {
                "pair_id": "loss-one",
                "opened_at": "2026-07-01T00:10:00+00:00",
                "closed_at": "2026-07-01T03:00:00+00:00",
                "entry_price": 100,
                "entry_atr": 0.25,
                "exit_price": 97,
                "trough_price": 97,
                "trough_at": "2026-07-01T03:00:00+00:00",
                "net_pnl_pct": -3.2,
                "exit_reason": "HARD_STOP",
            },
            {
                "pair_id": "loss-two",
                "opened_at": "2026-07-01T00:20:00+00:00",
                "closed_at": "2026-07-01T03:30:00+00:00",
                "entry_price": 100,
                "entry_atr": 0.25,
                "exit_price": 97,
                "trough_price": 97,
                "trough_at": "2026-07-01T03:30:00+00:00",
                "net_pnl_pct": -3.2,
                "exit_reason": "HARD_STOP",
            },
            {
                "pair_id": "reentry",
                "opened_at": "2026-07-01T04:00:00+00:00",
                "closed_at": "2026-07-01T05:00:00+00:00",
                "entry_price": 98,
                "entry_atr": 0.25,
                "exit_price": 99,
                "trough_price": 98,
                "trough_at": "2026-07-01T04:00:00+00:00",
                "net_pnl_pct": 0.82,
                "exit_reason": "BREAKEVEN",
            },
        ]
        trough_events = [
            {"pair_id": "winner", "ts": "2026-07-01T02:00:00+00:00", "price": 98},
            {"pair_id": "loss-one", "ts": "2026-07-01T02:10:00+00:00", "price": 98},
            {"pair_id": "loss-two", "ts": "2026-07-01T02:20:00+00:00", "price": 98},
        ]
        snapshots = [
            {"pair_id": "winner", "ts": "2026-07-01T01:00:00+00:00", "price": 99},
            {"pair_id": "loss-one", "ts": "2026-07-01T01:10:00+00:00", "price": 99},
            {"pair_id": "loss-two", "ts": "2026-07-01T01:20:00+00:00", "price": 99},
        ]

        results = run_study(
            records=records,
            snapshots=snapshots,
            trough_events=trough_events,
            rejected_signals=[{"ts": "2026-07-01T02:30:00+00:00"}],
            hard_stops=[1.5],
            time_stops=[1],
            hybrids=[(8, 1.5, 3.0)],
            cluster_guards=[(2, 60, 60)],
            episode_gap_hours=6,
            fee_pct=0.2,
        )

        self.assertEqual(
            [result.family for result in results],
            ["baseline", "hard_stop", "time_stop", "hybrid", "cluster_guard"],
        )
        baseline = results[0]
        self.assertEqual(len(baseline.outcomes), 2)
        self.assertEqual(len(baseline.episodes), 1)
        hard_stop = results[1]
        self.assertEqual(len(hard_stop.outcomes), 3)
        self.assertEqual(hard_stop.winners_cut, 1)
        self.assertEqual(hard_stop.losses_avoided, 2)
        self.assertEqual(len(hard_stop.episodes), 1)
        self.assertEqual(hard_stop.rejected_that_fit, 1)
        cluster = results[-1]
        self.assertEqual([item.pair_id for item in cluster.outcomes], ["reentry"])
        self.assertEqual(cluster.winners_cut, 1)


if __name__ == "__main__":
    unittest.main()
