from __future__ import annotations

import unittest

from tools.regime_study import (
    DOWN,
    MIXED,
    UP,
    Kline,
    RegimePolicy,
    apply_policies,
    build_regime_points,
    label_records,
    missing_kline_ranges,
)


HOUR_MS = 3_600_000


class RegimeStudyTests(unittest.TestCase):
    def test_entry_uses_only_last_fully_closed_candle(self) -> None:
        klines = _klines([100, 101, 102, 90])
        points = build_regime_points(klines, ema_period=2, slope_lookback=1)
        record = _trade("1970-01-01T03:30:00+00:00")

        observation = label_records([record], points)[0]

        self.assertEqual(observation.point.candle.open_time_ms, 2 * HOUR_MS)
        self.assertEqual(observation.regime, UP)

    def test_entry_at_hour_boundary_uses_previous_candle(self) -> None:
        klines = _klines([100, 101, 102, 90])
        points = build_regime_points(klines, ema_period=2, slope_lookback=1)
        record = _trade("1970-01-01T03:00:00+00:00")

        observation = label_records([record], points)[0]

        self.assertEqual(observation.point.candle.open_time_ms, 2 * HOUR_MS)

    def test_regime_classification_covers_up_down_and_mixed(self) -> None:
        points = build_regime_points(
            _klines([100, 101, 102, 101.55, 90]),
            ema_period=2,
            slope_lookback=1,
            slope_deadband_pct=0.1,
        )

        self.assertEqual(points[2].regime, UP)
        self.assertEqual(points[3].regime, MIXED)
        self.assertEqual(points[4].regime, DOWN)

    def test_policy_resizes_only_affected_regime(self) -> None:
        points = build_regime_points(
            _klines([100, 101, 102, 90]),
            ema_period=2,
            slope_lookback=1,
        )
        records = [
            _trade("1970-01-01T03:30:00+00:00", "up"),
            _trade("1970-01-01T04:30:00+00:00", "down"),
        ]
        observations = label_records(records, points)
        policy = RegimePolicy("HALF_DOWN", frozenset({DOWN}), 0.5)

        decisions = apply_policies(observations, [policy])

        self.assertEqual([item.factor for item in decisions], [1.0, 0.5])

    def test_missing_ranges_detect_internal_cache_gap(self) -> None:
        klines = [_kline(0, 100), _kline(2, 102), _kline(3, 103)]

        ranges = missing_kline_ranges(
            klines,
            required_start_ms=0,
            required_end_ms=4 * HOUR_MS - 1,
            interval_ms=HOUR_MS,
        )

        self.assertEqual(ranges, [(HOUR_MS, 2 * HOUR_MS - 1)])

    def test_missing_ranges_does_not_require_candle_open_at_entry(self) -> None:
        klines = [_kline(0, 100), _kline(1, 101), _kline(2, 102)]

        ranges = missing_kline_ranges(
            klines,
            required_start_ms=0,
            required_end_ms=3 * HOUR_MS + 30 * 60_000 - 1,
            interval_ms=HOUR_MS,
        )

        self.assertEqual(ranges, [])


def _klines(closes: list[float]) -> list[Kline]:
    return [_kline(index, close) for index, close in enumerate(closes)]


def _kline(index: int, close: float) -> Kline:
    open_time = index * HOUR_MS
    return Kline(
        open_time_ms=open_time,
        close_time_ms=open_time + HOUR_MS - 1,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1,
    )


def _trade(opened_at: str, pair_id: str = "pair") -> dict:
    return {
        "pair_id": pair_id,
        "position": "B",
        "position_type": "BOT_EXIT",
        "profile": "intraday",
        "opened_at": opened_at,
        "closed_at": "2026-07-02T00:00:00+00:00",
        "entry_price": 100,
        "position_notional_usdt": 20,
        "net_pnl_pct": 0.1,
        "exit_reason": "BREAKEVEN",
        "strategy_version": "test",
    }


if __name__ == "__main__":
    unittest.main()
