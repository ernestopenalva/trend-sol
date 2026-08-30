from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tools.real_a_be_pl1_order_study import BeSeed, FollowThrough, _peak_bucket
from tools.real_a_exit_simulator import Tick


class BePl1OrderStudyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.opened = datetime(2026, 8, 1, tzinfo=timezone.utc)
        self.seed = BeSeed("pair", self.opened, self.opened + timedelta(minutes=5), 100.0, 0.1, 100.6, 99.8)

    def test_tick_order_distinguishes_hard_stop_first(self) -> None:
        state = FollowThrough(self.seed, pl1_price=100.5, hard_stop_price=98.5, reference_price=100.52)
        state.record(Tick(self.opened + timedelta(seconds=1), 98.4), 30)
        state.record(Tick(self.opened + timedelta(seconds=2), 100.6), 30)
        self.assertEqual(state.outcome(), "HARD_STOP_FIRST")

    def test_peak_buckets_have_declared_boundaries(self) -> None:
        self.assertEqual(_peak_bucket(0.249), "<0.25%")
        self.assertEqual(_peak_bucket(0.25), "0.25-0.52%")
        self.assertEqual(_peak_bucket(0.52), "0.52-1.00%")
