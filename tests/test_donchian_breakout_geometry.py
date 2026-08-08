from __future__ import annotations

import unittest

from tools.donchian_breakout_geometry import (
    HOUR_MS,
    DonchianConfig,
    analyze_symbol,
    summarize,
)
from tools.market_selection_study import MarketCandle


def candle(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> MarketCandle:
    return MarketCandle(
        open_time_ms=index * HOUR_MS,
        close_time_ms=(index + 1) * HOUR_MS - 1,
        open=open_,
        high=high,
        low=low,
        close=close,
        quote_volume=1_000_000,
        trades=1_000,
    )


class DonchianBreakoutGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = DonchianConfig(
            channel_hours=3,
            forward_hours=(1, 2),
        )

    def test_breakout_uses_only_previous_channel_and_enters_next_open(self) -> None:
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101, 103.5),
            candle(4, 104, 105, 103, 104.5),
            candle(5, 104.5, 106, 104, 105),
        ]

        signals, _ = analyze_symbol("TESTUSDT", bars, self.config)

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].decision_ms, bars[3].close_time_ms)
        self.assertEqual(signals[0].channel_high, 103)
        self.assertEqual(signals[0].entry_ms, bars[4].open_time_ms)
        self.assertEqual(signals[0].entry_price, 104)

    def test_consecutive_breakout_closes_form_one_episode(self) -> None:
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101, 103.5),
            candle(4, 103.5, 105, 103, 104.5),
            candle(5, 104.5, 106, 104, 105.5),
            candle(6, 105.5, 106, 104, 105),
        ]

        signals, _ = analyze_symbol("TESTUSDT", bars, self.config)

        self.assertEqual(len(signals), 1)

    def test_round_trip_fee_is_subtracted_from_forward_close(self) -> None:
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101, 103.5),
            candle(4, 104, 104, 104, 104),
            candle(5, 104, 104, 104, 104),
        ]

        signals, controls = analyze_symbol("TESTUSDT", bars, self.config)
        report = summarize(signals, controls, self.config)

        self.assertAlmostEqual(
            report["signal_summary"]["horizons"]["1"]["median_close_net_pct"],
            -self.config.fee_pct,
        )

    def test_close_equal_to_prior_high_is_not_breakout(self) -> None:
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 103, 101, 103),
            candle(4, 103, 104, 102, 103.5),
        ]

        signals, _ = analyze_symbol("TESTUSDT", bars, self.config)

        self.assertEqual(signals, [])


if __name__ == "__main__":
    unittest.main()
