from __future__ import annotations

import unittest

from tools.market_selection_study import MarketCandle
from tools.trend_gate_study import (
    FIFTEEN_MINUTES_MS,
    TrendGateConfig,
    analyze_symbol,
    evaluate_efficiency_gate,
)


def candle(index: int, close: float, quote_volume: float = 200_000) -> MarketCandle:
    return MarketCandle(
        open_time_ms=index * FIFTEEN_MINUTES_MS,
        close_time_ms=(index + 1) * FIFTEEN_MINUTES_MS - 1,
        open=close,
        high=close + 0.1,
        low=close - 0.1,
        close=close,
        quote_volume=quote_volume,
        trades=100,
    )


class TrendGateStudyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = TrendGateConfig(
            min_quote_volume_24h=1,
            forward_hours=(1,),
        )

    def test_clean_advance_passes_efficiency_gate(self) -> None:
        closes = [100 + index * 0.2 for index in range(17)]

        passed, _, advance_atr, efficiency = evaluate_efficiency_gate(
            closes,
            16,
            atr_value=1.0,
            config=self.config,
        )

        self.assertTrue(passed)
        self.assertGreater(advance_atr, 2)
        self.assertAlmostEqual(efficiency, 1.0)

    def test_zigzag_with_same_net_advance_fails_efficiency_gate(self) -> None:
        closes = [100, 103, 99, 104, 100, 105, 101, 106, 102, 107, 103, 108, 104, 109, 105, 110, 106]

        passed, _, _, efficiency = evaluate_efficiency_gate(
            closes,
            16,
            atr_value=1.0,
            config=self.config,
        )

        self.assertFalse(passed)
        self.assertLess(efficiency, self.config.minimum_efficiency)

    def test_observations_are_hourly_and_reference_next_candle(self) -> None:
        bars = [candle(index, 100 + index * 0.05) for index in range(140)]

        observations = analyze_symbol(
            "TESTUSDT",
            bars,
            self.config,
            start_ms=0,
            end_ms=140 * FIFTEEN_MINUTES_MS,
        )

        self.assertTrue(observations)
        self.assertTrue(all(item.decision_ms % (60 * 60 * 1000) == 0 for item in observations))
        self.assertTrue(all(item.reference_ms == item.decision_ms for item in observations))

    def test_future_prices_do_not_change_current_gate_state(self) -> None:
        prefix = [candle(index, 100 + index * 0.05) for index in range(120)]
        rising_future = prefix + [candle(index, 106 + index) for index in range(120, 130)]
        falling_future = prefix + [candle(index, 106 - index) for index in range(120, 130)]

        rising = analyze_symbol(
            "TESTUSDT", rising_future, self.config, 0, 121 * FIFTEEN_MINUTES_MS
        )
        falling = analyze_symbol(
            "TESTUSDT", falling_future, self.config, 0, 121 * FIFTEEN_MINUTES_MS
        )

        self.assertEqual(rising[-1].decision_ms, falling[-1].decision_ms)
        self.assertEqual(rising[-1].gate_a_ema_slope, falling[-1].gate_a_ema_slope)
        self.assertEqual(rising[-1].gate_b_efficiency, falling[-1].gate_b_efficiency)


if __name__ == "__main__":
    unittest.main()
