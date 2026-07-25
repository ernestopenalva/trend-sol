from __future__ import annotations

import unittest

from tools.market_selection_study import (
    HOUR_MS,
    MarketCandle,
    build_candidate_snapshots,
    forward_outcome,
    missing_candle_ranges,
    _trimmed_mean,
)


class MarketSelectionStudyTests(unittest.TestCase):
    def test_ranking_snapshot_uses_only_trailing_data(self) -> None:
        candles = [_candle(index, 100 + index, 1_000_000) for index in range(169)]

        snapshots = build_candidate_snapshots(
            {"AAAUSDT": candles},
            decision_interval_hours=1,
            min_quote_volume_usdt=10_000_000,
            require_positive_24h=True,
            require_positive_7d=True,
        )

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].decision_ms, 169 * HOUR_MS)
        self.assertAlmostEqual(snapshots[0].change_24h_pct, (268 / 244 - 1) * 100)
        self.assertAlmostEqual(snapshots[0].change_7d_pct, (268 / 100 - 1) * 100)

    def test_negative_24h_market_is_not_selected(self) -> None:
        closes = [200 - index for index in range(170)]
        candles = [
            _candle(index, close, 1_000_000)
            for index, close in enumerate(closes)
        ]

        snapshots = build_candidate_snapshots(
            {"AAAUSDT": candles},
            decision_interval_hours=1,
            min_quote_volume_usdt=10_000_000,
            require_positive_24h=True,
            require_positive_7d=True,
        )

        self.assertEqual(snapshots, [])

    def test_forward_outcome_starts_after_decision_candle(self) -> None:
        candles = {
            item.boundary_ms: item
            for item in [
                _candle(0, 100, 1),
                _candle(1, 110, 1, high=115, low=95),
                _candle(2, 105, 1, high=120, low=90),
            ]
        }

        outcome = forward_outcome(candles, HOUR_MS, 2, 100)

        self.assertAlmostEqual(outcome.return_pct, 5)
        self.assertAlmostEqual(outcome.mfe_pct, 20)
        self.assertAlmostEqual(outcome.mae_pct, -10)

    def test_missing_ranges_ignore_candle_still_open_at_end(self) -> None:
        candles = [_candle(index, 100, 1) for index in range(3)]

        ranges = missing_candle_ranges(
            candles,
            required_start_ms=0,
            required_end_ms=3 * HOUR_MS + 30 * 60_000 - 1,
            interval_ms=HOUR_MS,
        )

        self.assertEqual(ranges, [])

    def test_trimmed_mean_removes_extreme_tails(self) -> None:
        values = [0.0] * 18 + [-100.0, 1000.0]

        self.assertEqual(_trimmed_mean(values, 0.05), 0.0)


def _candle(
    index: int,
    close: float,
    quote_volume: float,
    high: float | None = None,
    low: float | None = None,
) -> MarketCandle:
    open_time = index * HOUR_MS
    return MarketCandle(
        open_time_ms=open_time,
        close_time_ms=open_time + HOUR_MS - 1,
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        quote_volume=quote_volume,
        trades=100,
    )


if __name__ == "__main__":
    unittest.main()
