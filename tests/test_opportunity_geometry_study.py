from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.market_selection_study import MarketCandle
from tools.opportunity_geometry_study import OpportunityConfig, classify_path, run_study
from tools.trend_gate_study import FIFTEEN_MINUTES_MS


def candle(index: int, open_price: float, high: float, low: float, close: float) -> MarketCandle:
    return MarketCandle(
        open_time_ms=index * FIFTEEN_MINUTES_MS,
        close_time_ms=(index + 1) * FIFTEEN_MINUTES_MS - 1,
        open=open_price,
        high=high,
        low=low,
        close=close,
        quote_volume=1_000_000,
        trades=100,
    )


class OpportunityGeometryStudyTests(unittest.TestCase):
    def test_target_before_stop_is_winner_after_fees(self) -> None:
        bars = [
            candle(0, 100, 100.4, 99.8, 100.2),
            candle(1, 100.2, 101.2, 100.0, 101.0),
            candle(2, 101.0, 101.1, 98.5, 99.0),
        ]

        result = classify_path(100, bars, upside_pct=1, downside_pct=1, fee_pct=0.2)

        self.assertEqual(result.outcome, "target")
        self.assertAlmostEqual(result.net_pct, 0.8)
        self.assertEqual(result.bars_to_resolution, 2)

    def test_stop_before_target_is_loss_after_fees(self) -> None:
        bars = [
            candle(0, 100, 100.4, 98.9, 99.2),
            candle(1, 99.2, 101.2, 99.0, 101.0),
        ]

        result = classify_path(100, bars, upside_pct=1, downside_pct=1, fee_pct=0.2)

        self.assertEqual(result.outcome, "stop")
        self.assertAlmostEqual(result.net_pct, -1.2)

    def test_same_candle_touch_is_conservative_loss(self) -> None:
        bars = [candle(0, 100, 101.2, 98.8, 100.5)]

        result = classify_path(100, bars, upside_pct=1, downside_pct=1, fee_pct=0.2)

        self.assertEqual(result.outcome, "ambiguous_loss")
        self.assertAlmostEqual(result.net_pct, -1.2)

    def test_timeout_uses_final_close_and_round_trip_fee(self) -> None:
        bars = [
            candle(0, 100, 100.4, 99.7, 100.1),
            candle(1, 100.1, 100.6, 99.9, 100.5),
        ]

        result = classify_path(100, bars, upside_pct=1, downside_pct=1, fee_pct=0.2)

        self.assertEqual(result.outcome, "timeout")
        self.assertAlmostEqual(result.net_pct, 0.3)
        self.assertEqual(result.bars_to_resolution, 2)

    def test_study_samples_hourly_and_enters_at_next_candle_open(self) -> None:
        start_ms = int(
            datetime(2025, 10, 1, tzinfo=timezone.utc).timestamp() * 1000
        )
        bars = []
        for index in range(12):
            price = 200.0 if index in (4, 8) else 100.0
            bars.append(
                MarketCandle(
                    open_time_ms=start_ms + index * FIFTEEN_MINUTES_MS,
                    close_time_ms=start_ms + (index + 1) * FIFTEEN_MINUTES_MS - 1,
                    open=price,
                    high=price * 1.02,
                    low=price * 0.999,
                    close=price,
                    quote_volume=1_000_000,
                    trades=100,
                )
            )
        config = OpportunityConfig(
            upside_pcts=(1.0,),
            downside_pcts=(1.0,),
            horizons_hours=(1,),
            min_quote_volume_24h=1,
            liquidity_candles=4,
        )

        report = run_study({"TESTUSDT": bars}, config)
        window = report["windows"]["2025-10-01__2026-02-01"]
        geometry = window["geometries"]["up_1.00__down_1.00__hours_1"]

        self.assertEqual(window["eligible_observations_by_horizon"]["1"], 2)
        self.assertEqual(geometry["observations"], 2)
        self.assertEqual(geometry["target_first_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
