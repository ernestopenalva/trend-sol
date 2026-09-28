from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO

from src.indicators.indicators import ema
from src.monitor.entry_engine import Candle as EngineCandle
from src.monitor.market_context import MarketContextEngine
from tools.ema_context_monitor import (
    ALL_PERIODS,
    HISTORY_POINTS,
    REQUIRED_CLOSED_CANDLES,
    Candle,
    _available_indices,
    _closed_candles,
    _context_at,
    _print_monitor,
)


class EmaContextMonitorTests(unittest.TestCase):
    def test_drops_in_progress_candle(self) -> None:
        rows = [[0, 0, 0, 0, "100", 0, 299_999], [300_000, 0, 0, 0, "101", 0, 599_999]]
        self.assertEqual([item.close for item in _closed_candles(rows, now_ms=300_000)], [100.0])

    def test_requires_twelve_safe_ema200_current_previous_points(self) -> None:
        closes = [100.0 + index / 10 for index in range(REQUIRED_CLOSED_CANDLES)]
        values = {period: ema(closes, period) for period in ALL_PERIODS}
        self.assertGreaterEqual(len(_available_indices(values, len(closes))), HISTORY_POINTS)

    def test_monitor_context_matches_market_context_on_same_closed_snapshot(self) -> None:
        closes = [100 + index * 0.03 + ((index % 7) - 3) * 0.02 for index in range(300)]
        monitor_candles = [Candle(index * 300_000, index * 300_000 + 299_999, close)
                           for index, close in enumerate(closes)]
        values = {period: ema(closes, period) for period in ALL_PERIODS}
        monitor = _context_at(values, len(closes) - 1)

        engine_candles = [EngineCandle(
            open_time=item.open_time_ms, close_time=item.close_time_ms,
            open=item.close, high=item.close + 0.1, low=item.close - 0.1,
            close=item.close, volume=1000, closed=True,
        ) for item in monitor_candles]
        engine = MarketContextEngine.__new__(MarketContextEngine)
        engine.settings = {}
        official = engine._timeframe_snapshot(engine_candles, "5m")

        for field in (
            "ema50", "ema50_previous", "ema50_direction",
            "ema100", "ema100_previous", "ema100_direction",
            "ema200", "ema200_previous", "ema200_direction", "ema_context",
            "macd_line", "macd_line_previous", "macd_direction", "macd_position", "macd_context",
        ):
            self.assertEqual(monitor[field], official[field], field)

    def test_output_uses_current_taxonomy_and_omits_legacy_score(self) -> None:
        candles = [Candle(index * 300_000, index * 300_000 + 299_999,
                          100 + index * 0.02 + (index % 3) * 0.01)
                   for index in range(300)]
        output = StringIO()
        with redirect_stdout(output):
            _print_monitor("SOLUSDT", candles)
        text = output.getvalue()
        self.assertIn("EMA | current | previous | delta | direction", text)
        self.assertIn("EMA CONTEXT:", text)
        self.assertIn("MACD CONTEXT:", text)
        self.assertIn("BRT | EMA_CONTEXT | MACD_CONTEXT | EMA50 | EMA100 | EMA200 | MACD", text)
        self.assertIn("snapshot alignment: EMA and MACD use the same closed 5m", text)
        for legacy in ("EMA SCORE", "TREND READ", "SCORE TRAJECTORY", "EMA20 |"):
            self.assertNotIn(legacy, text)


if __name__ == "__main__":
    unittest.main()
