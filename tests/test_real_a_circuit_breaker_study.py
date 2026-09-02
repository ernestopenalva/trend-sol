from datetime import datetime, timezone
import unittest

from tools.real_a_circuit_breaker_study import ClosedTrade, _drawdown_episodes, _equity_points, _largest_uniform_group, _signature


class CircuitBreakerStudyTests(unittest.TestCase):
    def _trade(self, hour, net, signature="v1"):
        when = datetime(2026, 1, 1, hour, tzinfo=timezone.utc)
        return ClosedTrade({"exit_reason": "HARD_STOP"}, when, when, net, net / 5, signature)

    def test_largest_group_does_not_silently_mix_signatures(self):
        selected, signature = _largest_uniform_group([self._trade(0, 1), self._trade(1, -1), self._trade(2, 1, "v2")])
        self.assertEqual(signature, "v1")
        self.assertEqual(len(selected), 2)

    def test_drawdown_episode_is_not_reset_by_small_win(self):
        points = _equity_points([self._trade(0, -1), self._trade(1, 0.1), self._trade(2, -1), self._trade(3, 3)], 100)
        episodes = _drawdown_episodes(points)
        self.assertEqual(len(episodes), 1)
        self.assertEqual(len(episodes[0]), 3)

    def test_floor_price_does_not_fragment_historical_signature(self):
        a = _signature({"strategy_version": "v", "hard_stop_pct": 1.5, "no_progress_enabled": False, "profit_lock_economic_floor": 90.0})
        b = _signature({"strategy_version": "v", "hard_stop_pct": 1.5, "no_progress_enabled": False, "profit_lock_economic_floor": 110.0})
        self.assertEqual(a, b)
