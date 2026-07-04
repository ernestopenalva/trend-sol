from __future__ import annotations

import unittest

from src.indicators.indicators import atr, ema, rsi, volume_ma


class IndicatorTests(unittest.TestCase):
    def test_ema_starts_with_simple_average(self) -> None:
        values = [1, 2, 3, 4, 5]
        result = ema(values, 3)
        self.assertEqual(result[:2], [None, None])
        self.assertAlmostEqual(result[2] or 0, 2.0)
        self.assertAlmostEqual(result[-1] or 0, 4.0)

    def test_atr_uses_true_range(self) -> None:
        result = atr([10, 12, 11], [8, 9, 8], [9, 10, 9], 2)
        self.assertIsNone(result[0])
        self.assertAlmostEqual(result[1] or 0, 2.5)
        self.assertAlmostEqual(result[2] or 0, 2.75)

    def test_rsi_reacts_to_mixed_series(self) -> None:
        result = rsi([10, 11, 12, 11, 12, 13], 3)
        self.assertIsNone(result[2])
        self.assertGreater(result[-1] or 0, 50)
        self.assertLess(result[-1] or 0, 100)

    def test_volume_ma(self) -> None:
        self.assertEqual(volume_ma([10, 20, 30], 2), [None, 15.0, 25.0])


if __name__ == "__main__":
    unittest.main()
