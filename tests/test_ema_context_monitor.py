from __future__ import annotations

import unittest

from tools.ema_context_monitor import (
    REQUIRED_CLOSED_CANDLES,
    _available_indices,
    _closed_candles,
    _direction,
    _score,
    DOWN,
    FLAT,
    UP,
)
from src.indicators.indicators import ema


class EmaContextMonitorTests(unittest.TestCase):
    def test_drops_in_progress_candle(self) -> None:
        rows = [[0, 0, 0, 0, "100", 0, 299_999], [300_000, 0, 0, 0, "101", 0, 599_999]]
        self.assertEqual([item.close for item in _closed_candles(rows, now_ms=300_000)], [100.0])

    def test_requires_twelve_safe_ema100_t_vs_t_minus_three_points(self) -> None:
        closes = [100.0 + index for index in range(REQUIRED_CLOSED_CANDLES)]
        values = {period: ema(closes, period) for period in (20, 50, 100)}
        self.assertGreaterEqual(len(_available_indices(values, len(closes))), 12)

    def test_score_uses_only_number_of_rising_emas(self) -> None:
        self.assertEqual(_score([UP, UP, DOWN]), (6.7, "MOSTLY_RISING"))
        self.assertEqual(_direction(0), FLAT)
