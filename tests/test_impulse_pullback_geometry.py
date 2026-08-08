from __future__ import annotations

import unittest

from tools.impulse_pullback_geometry import (
    HOUR_MS,
    MINUTE_MS,
    GeometryConfig,
    analyze_symbol,
    select_audit_events,
    summarize,
)
from tools.market_selection_study import MarketCandle


def bar(open_ms: int, minutes: int, open_: float, high: float, low: float, close: float) -> MarketCandle:
    return MarketCandle(
        open_time_ms=open_ms,
        close_time_ms=open_ms + minutes * MINUTE_MS - 1,
        open=open_,
        high=high,
        low=low,
        close=close,
        quote_volume=1_000_000,
        trades=1_000,
    )


def flat_15m(count: int = 20) -> list[MarketCandle]:
    return [
        bar(index * 15 * MINUTE_MS, 15, 100.0, 100.10, 99.90, 100.0)
        for index in range(count)
    ]


def minute_history(end_open_ms: int) -> list[MarketCandle]:
    output = []
    open_ms = 0
    while open_ms <= end_open_ms:
        output.append(bar(open_ms, 1, 100.0, 100.05, 99.95, 100.0))
        open_ms += MINUTE_MS
    return output


class ImpulsePullbackGeometryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = GeometryConfig(
            min_impulse_atr=3.0,
            expiry_minutes=30,
            forward_minutes=(15,),
        )

    def test_confirms_only_after_pullback_and_local_breakout(self) -> None:
        candles_15m = flat_15m()
        impulse_open = len(candles_15m) * 15 * MINUTE_MS
        candles_15m.append(bar(impulse_open, 15, 100.0, 102.2, 99.9, 102.0))
        first_after_impulse = candles_15m[-1].close_time_ms + 1
        candles_1m = minute_history(first_after_impulse - MINUTE_MS)
        prices = [102.0, 101.8, 101.5, 101.55, 101.65, 101.70, 101.75, 101.95]
        for offset, close in enumerate(prices):
            open_ = prices[offset - 1] if offset else 102.0
            candles_1m.append(
                bar(
                    first_after_impulse + offset * MINUTE_MS,
                    1,
                    open_,
                    max(open_, close) + 0.02,
                    min(open_, close) - 0.02,
                    close,
                )
            )
        for offset in range(len(prices), len(prices) + 20):
            candles_1m.append(
                bar(first_after_impulse + offset * MINUTE_MS, 1, 102.0, 102.2, 101.9, 102.1)
            )

        events = analyze_symbol("TESTUSDT", candles_15m, candles_1m, [], self.config)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "CONFIRMED")
        self.assertIsNotNone(events[0].pullback_entered_ms)
        self.assertGreater(events[0].confirmation_ms, events[0].pullback_entered_ms)
        self.assertGreater(events[0].confirmation_price, events[0].confirmation_threshold)
        self.assertIn("15", events[0].forward["horizons"])
        self.assertEqual(
            set(events[0].phase_forward),
            {"impulse_close", "pullback_band", "confirmation"},
        )

    def test_invalidates_when_pullback_crosses_maximum_after_entering_band(self) -> None:
        candles_15m = flat_15m()
        impulse_open = len(candles_15m) * 15 * MINUTE_MS
        candles_15m.append(bar(impulse_open, 15, 100.0, 102.2, 99.9, 102.0))
        first_after_impulse = candles_15m[-1].close_time_ms + 1
        candles_1m = minute_history(first_after_impulse - MINUTE_MS)
        for offset, close in enumerate((101.6, 101.4, 100.7)):
            candles_1m.append(
                bar(
                    first_after_impulse + offset * MINUTE_MS,
                    1,
                    102.0 if offset == 0 else (101.6, 101.4)[offset - 1],
                    102.02,
                    close - 0.02,
                    close,
                )
            )

        events = analyze_symbol("TESTUSDT", candles_15m, candles_1m, [], self.config)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].status, "INVALIDATED")
        self.assertEqual(events[0].reason, "pullback_too_deep")
        self.assertIsNotNone(events[0].pullback_entered_ms)

    def test_summary_subtracts_round_trip_fee(self) -> None:
        candles_15m = flat_15m()
        impulse_open = len(candles_15m) * 15 * MINUTE_MS
        candles_15m.append(bar(impulse_open, 15, 100.0, 102.2, 99.9, 102.0))
        first_after_impulse = candles_15m[-1].close_time_ms + 1
        candles_1m = minute_history(first_after_impulse - MINUTE_MS)
        prices = [102.0, 101.6, 101.65, 101.70, 101.75, 101.80, 101.85, 102.05]
        for offset, close in enumerate(prices):
            open_ = prices[offset - 1] if offset else 102.0
            candles_1m.append(
                bar(
                    first_after_impulse + offset * MINUTE_MS,
                    1,
                    open_,
                    max(open_, close) + 0.01,
                    min(open_, close) - 0.01,
                    close,
                )
            )
        for offset in range(len(prices), len(prices) + 20):
            candles_1m.append(
                bar(first_after_impulse + offset * MINUTE_MS, 1, 102.05, 102.07, 102.03, 102.05)
            )

        events = analyze_symbol("TESTUSDT", candles_15m, candles_1m, [], self.config)
        report = summarize(events, self.config)
        row = report["all_confirmed"]["horizons"]["15"]

        self.assertEqual(row["complete_events"], 1)
        self.assertAlmostEqual(row["median_close_net_pct"], -self.config.fee_pct, places=6)
        self.assertIsNone(row["mean_close_net_without_best_pct"])
        matched = report["matched_confirmed_comparison"]
        self.assertEqual(matched["events"], 1)
        self.assertEqual(
            matched["phases"]["confirmation"]["horizons"]["15"]["complete_events"],
            1,
        )

    def test_audit_selection_includes_each_available_status(self) -> None:
        candles_15m = flat_15m()
        impulse_open = len(candles_15m) * 15 * MINUTE_MS
        candles_15m.append(bar(impulse_open, 15, 100.0, 102.2, 99.9, 102.0))
        first_after_impulse = candles_15m[-1].close_time_ms + 1
        candles_1m = minute_history(first_after_impulse - MINUTE_MS)
        candles_1m.append(bar(first_after_impulse, 1, 102.0, 102.0, 100.6, 100.7))
        invalidated = analyze_symbol(
            "INVALIDUSDT", candles_15m, candles_1m, [], self.config
        )[0]
        no_pullback = minute_history(first_after_impulse - MINUTE_MS)
        for offset in range(40):
            no_pullback.append(
                bar(
                    first_after_impulse + offset * MINUTE_MS,
                    1,
                    102.0,
                    102.05,
                    101.95,
                    102.0,
                )
            )
        expired = analyze_symbol(
            "EXPIREDUSDT", candles_15m, no_pullback, [], self.config
        )[0]

        selected = select_audit_events([invalidated, expired], 20)

        self.assertEqual({item.status for item in selected}, {"INVALIDATED", "EXPIRED"})

    def test_regime_uses_only_closed_hourly_candles(self) -> None:
        hourly = [
            bar(index * HOUR_MS, 60, 100 + index, 101 + index, 99 + index, 100 + index)
            for index in range(60)
        ]
        config = GeometryConfig(regime_ema_period=10, regime_slope_lookback=2)

        from tools.impulse_pullback_geometry import build_regime_timeline

        timeline = build_regime_timeline(hourly, config)

        self.assertEqual(timeline[-1].regime, "UP")
        self.assertEqual(timeline[-1].close_time_ms, hourly[-1].close_time_ms)


if __name__ == "__main__":
    unittest.main()
