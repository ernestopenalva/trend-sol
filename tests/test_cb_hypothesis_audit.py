import random
import unittest
from types import SimpleNamespace

from tools.circuit_breaker_hypothesis_audit import CheckedGuard, IndependentDetector, frozen_config, order_audit


class DetectorAuditTests(unittest.TestCase):
    def test_exact_equality_over_expiry_retriggers_and_repeated_calls(self):
        rng=random.Random(472)
        guard=CheckedGuard([])
        result=SimpleNamespace(trades=[])
        for minute in range(1600):
            boundary=(minute+1)*60_000
            if rng.random()<.07:
                result.trades.append(SimpleNamespace(opened_ms=boundary-30_000,closed_ms=boundary,net_pct=rng.choice([-3.,-1.7,.05,2.,5.])))
            guard.allows(boundary,result)
        self.assertGreater(guard.original.crises,0)
        self.assertEqual(guard.original.paused_minutes,guard.independent.paused_minutes)

    def test_future_and_duplicate_closes_are_rejected(self):
        future=SimpleNamespace(opened_ms=0,closed_ms=120_000,net_pct=-1.)
        with self.assertRaises(AssertionError):
            CheckedGuard([]).allows(60_000,SimpleNamespace(trades=[future]))
        close=SimpleNamespace(opened_ms=0,closed_ms=60_000,net_pct=-1.)
        with self.assertRaises(AssertionError):
            CheckedGuard([]).allows(60_000,SimpleNamespace(trades=[close,close]))

    def test_same_minute_order_can_change_peak_and_crisis(self):
        # Same total dollars (-.6), but different intraminute high-water marks.
        guard=CheckedGuard([])
        trades=[SimpleNamespace(opened_ms=i,closed_ms=60_000,net_pct=pnl/20*100)
                for i,pnl in enumerate([1.,-1.6])]
        guard.allows(60_000,SimpleNamespace(trades=trades))
        check=order_audit(guard)
        self.assertEqual(check['reverse']['different_minutes'],1)

    def test_replay_sources_match_frozen_revision(self):
        config,hashes=frozen_config()
        self.assertEqual(config['capital']['trade_size_pct'],20)
        self.assertTrue(hashes)
