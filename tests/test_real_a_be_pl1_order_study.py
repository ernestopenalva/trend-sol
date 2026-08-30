from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tools.real_a_be_pl1_order_study import BeSeed, FollowThrough, _cohort_label, _peak_bucket, _state
from tools.real_a_exit_simulator import Tick


class BePl1OrderStudyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opened = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.seed = BeSeed(
            "pair", self.opened, self.opened + timedelta(minutes=5), 100.0, 0.1, 100.6, 99.8,
            "b_atr_v1.4", "intraday", 98.5, 1.5, None, None,
        )

    def test_tick_order_distinguishes_hard_stop_first(self) -> None:
        state = FollowThrough(self.seed, pl1_price=100.5, hard_stop_price=98.5, reference_price=100.52)
        state.record(Tick(self.opened + timedelta(seconds=1), 98.4), 30)
        state.record(Tick(self.opened + timedelta(seconds=2), 100.6), 30)
        self.assertEqual(state.outcome(), "HARD_STOP_FIRST")

    def test_peak_buckets_have_declared_boundaries(self) -> None:
        self.assertEqual(_peak_bucket(0.249), "<0.25%")
        self.assertEqual(_peak_bucket(0.25), "0.25-0.52%")
        self.assertEqual(_peak_bucket(0.52), "0.52-1.00%")

    def test_historical_cohorts_are_not_aggregated(self) -> None:
        v13 = BeSeed("old", self.opened, self.opened, 100.0, 0.1, 101.0, 99.0,
                       "b_atr_v1.3", "intraday", 98.0, 2.0, "PL1", 100.5)
        exception = BeSeed("exception", self.opened, self.opened, 100.0, 0.1, 101.0, 99.0,
                           "b_atr_v1.4", "intraday", 98.0, 2.0, None, None)
        self.assertIn("CURRENT LADDER", _cohort_label(self.seed))
        self.assertIn("PL-SHADOW", _cohort_label(v13))
        self.assertIn("EXCEPTION", _cohort_label(exception))

    def test_historical_thresholds_use_version_formula_and_persisted_stop(self) -> None:
        config = {
            "risk": {
                "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]},
                "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1},
                "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5},
            },
            "fees": {"enabled": True, "taker_fee_pct": 0.10, "use_bnb_discount": False},
            "ladder": {"be_net_margin_pct": 0.05, "be_activation_buffer_atr": 0.5},
        }
        old = BeSeed("old", self.opened, self.opened, 73.16, 0.09806533627396716, 73.83, 73.0,
                     "b_atr_v1.3", "intraday", 71.6968, 2.0, "PL1", 73.65032668136983)
        current = BeSeed("current", self.opened, self.opened, 100.0, 0.1, 101.0, 99.0,
                         "b_atr_v1.4", "intraday", 97.7, 1.5, None, None)
        self.assertAlmostEqual(_state(old, config).pl1_price, 73.65032668136983)
        self.assertEqual(_state(old, config).hard_stop_price, 71.6968)
        self.assertAlmostEqual(_state(current, config).pl1_price, 100.6)
        self.assertEqual(_state(current, config).hard_stop_price, 97.7)
