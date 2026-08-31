from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tools.real_a_spacing_h1_cluster_diagnostic import Cluster, Trade, _clusters


class SpacingH1ClusterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 8, 1, tzinfo=timezone.utc)

    def test_cluster_is_maximal_continuous_exposure_without_time_threshold(self) -> None:
        trades = [
            Trade("a", self.start, self.start + timedelta(minutes=20), 100, "HARD_STOP"),
            Trade("b", self.start + timedelta(minutes=10), self.start + timedelta(minutes=15), 99, "HARD_STOP"),
            Trade("c", self.start + timedelta(minutes=19), self.start + timedelta(minutes=25), 98, "BREAKEVEN"),
            Trade("d", self.start + timedelta(minutes=26), self.start + timedelta(minutes=30), 101, "TRAILING"),
        ]
        clusters = _clusters(trades)
        self.assertEqual([len(cluster.trades) for cluster in clusters], [3, 1])
        self.assertEqual(clusters[0].transitions["DESCENDENTE"], 2)

    def test_predeclared_good_and_bad_categories(self) -> None:
        bad = Cluster(1, tuple(
            Trade(str(index), self.start + timedelta(minutes=index), self.start + timedelta(minutes=10), 100 + index, "HARD_STOP")
            for index in range(3)
        ))
        good = Cluster(2, tuple(
            Trade(str(index), self.start + timedelta(minutes=index), self.start + timedelta(minutes=10), 100 + index, "TRAILING")
            for index in range(3)
        ))
        self.assertEqual(bad.category, "RUIM_HS_DOMINANTE")
        self.assertEqual(good.category, "BOM_LUCRO_DOMINANTE")

    def test_profit_lock_economic_exit_counts_as_profit_lock(self) -> None:
        cluster = Cluster(3, tuple(
            Trade(str(index), self.start + timedelta(minutes=index), self.start + timedelta(minutes=10), 100 + index, "PROFIT_LOCK_ECONOMIC_EXIT")
            for index in range(3)
        ))
        self.assertEqual(cluster.profit_lock_count, 3)
        self.assertEqual(cluster.category, "BOM_LUCRO_DOMINANTE")
