from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.atr_exit_study import (
    StudyCandle,
    TradeAtr,
    TradeObservation,
    aggregate_timeframe,
    calculate_entry_atr,
    round_trip_fee_pct,
)


class AtrExitStudyTests(unittest.TestCase):
    def test_atr_pct_and_ladder_multiples_are_correct(self) -> None:
        opened = datetime.fromtimestamp(20 * 60, tz=timezone.utc)
        result = calculate_entry_atr(_candles(20, spread=2.0), opened, 100.0, "1m", 14)

        self.assertAlmostEqual(result.atr_abs or 0, 2.0)
        self.assertAlmostEqual(result.atr_pct or 0, 2.0)
        self.assertAlmostEqual((result.atr_pct or 0) * 3, 6.0)
        self.assertAlmostEqual((result.atr_pct or 0) * 5, 10.0)
        self.assertAlmostEqual((result.atr_pct or 0) * 8, 16.0)
        self.assertAlmostEqual((result.atr_pct or 0) * 10, 20.0)
        self.assertAlmostEqual((result.atr_pct or 0) * 12, 24.0)
        self.assertAlmostEqual((result.atr_pct or 0) * 1.5, 3.0)
        self.assertAlmostEqual((result.atr_pct or 0) * 3, 6.0)
        self.assertAlmostEqual((result.atr_pct or 0) * 6, 12.0)

    def test_fee_and_economic_floor_comparisons(self) -> None:
        observations = [_observation("1m", 0.10, peak_pct=2.0)]
        result = aggregate_timeframe(observations, "1m", fees_pct=0.20, economic_floor_pct=0.25)

        self.assertEqual(result["below_fees"][1.5], 1)
        self.assertEqual(result["below_floor"][1.5], 1)
        self.assertEqual(result["below_fees"][3.0], 0)
        self.assertEqual(result["below_floor"][3.0], 0)

    def test_effective_bnb_discount_matches_bot_convention(self) -> None:
        config = {"fees": {"enabled": True, "taker_fee_pct": 0.10, "use_bnb_discount": True}}
        self.assertAlmostEqual(round_trip_fee_pct(config), 0.15)

    def test_aggregation_is_independent_by_timeframe(self) -> None:
        first = _observation("1m", 0.10, peak_pct=0.60)
        first.atrs["5m"] = TradeAtr("5m", 0.2, 0.20, 20)
        second = _observation("1m", 0.20, peak_pct=0.70)
        second.atrs["5m"] = TradeAtr("5m", None, None, 4, "insufficient_history")

        one_minute = aggregate_timeframe([first, second], "1m", 0.20, 0.25)
        five_minute = aggregate_timeframe([first, second], "5m", 0.20, 0.25)

        self.assertEqual(one_minute["valid"], 2)
        self.assertAlmostEqual(one_minute["median"], 0.15)
        self.assertEqual(five_minute["valid"], 1)
        self.assertEqual(five_minute["excluded"], 1)

    def test_insufficient_candles_excludes_only_that_result(self) -> None:
        result = calculate_entry_atr(
            _candles(13, spread=2.0),
            datetime.fromtimestamp(20 * 60, tz=timezone.utc),
            100.0,
            "1m",
            14,
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "insufficient_history")

    def test_candle_after_opened_at_never_enters_atr(self) -> None:
        opened = datetime.fromtimestamp(20 * 60, tz=timezone.utc)
        baseline = calculate_entry_atr(_candles(20, spread=2.0), opened, 100.0, "1m", 14)
        candles = _candles(20, spread=2.0)
        candles.append(
            StudyCandle(
                open_time_ms=20 * 60_000,
                close_time_ms=21 * 60_000 - 1,
                high=1000.0,
                low=1.0,
                close=500.0,
            )
        )
        with_future = calculate_entry_atr(candles, opened, 100.0, "1m", 14)
        self.assertAlmostEqual(with_future.atr_abs or 0, baseline.atr_abs or 0)

    def test_gap_is_reported_instead_of_inventing_atr(self) -> None:
        candles = _candles(20, spread=2.0)
        del candles[10]
        result = calculate_entry_atr(
            candles,
            datetime.fromtimestamp(20 * 60, tz=timezone.utc),
            100.0,
            "1m",
            14,
        )
        self.assertFalse(result.available)
        self.assertEqual(result.reason, "candle_gap")


def _candles(count: int, spread: float) -> list[StudyCandle]:
    return [
        StudyCandle(
            open_time_ms=index * 60_000,
            close_time_ms=(index + 1) * 60_000 - 1,
            high=100.0 + spread / 2,
            low=100.0 - spread / 2,
            close=100.0,
        )
        for index in range(count)
    ]


def _observation(timeframe: str, atr_pct: float, peak_pct: float) -> TradeObservation:
    return TradeObservation(
        record={},
        opened_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        entry_price=100.0,
        peak_pct=peak_pct,
        age_seconds=3600,
        atrs={timeframe: TradeAtr(timeframe, atr_pct, atr_pct, 20)},
    )


if __name__ == "__main__":
    unittest.main()
