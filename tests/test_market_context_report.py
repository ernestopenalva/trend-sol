from __future__ import annotations

import unittest

from tools.market_context_report import _extreme, _rsi_move


class MarketContextReportTests(unittest.TestCase):
    def test_rsi_move_formats_fifteen_minute_delta(self) -> None:
        self.assertEqual(
            _rsi_move({"rsi14_15m_ago": 52.1, "rsi14": 64.3}),
            "RSI 52.1→64.3 (+12.2)",
        )

    def test_peak_and_trough_include_price_percentage_and_atr(self) -> None:
        trade = {
            "entry_price": 76.20,
            "peak_price": 76.43,
            "peak_atr": 4.0564,
            "trough_price": 76.18,
            "trough_pct": -0.0262,
            "trough_atr": -0.3526,
        }

        self.assertEqual(
            _extreme(trade, "peak_price", "peak_pct", "peak_atr"),
            "76.4300 (+0.30% / +4.06 ATR)",
        )
        self.assertEqual(
            _extreme(trade, "trough_price", "trough_pct", "trough_atr"),
            "76.1800 (-0.03% / -0.35 ATR)",
        )

    def test_extreme_is_null_safe(self) -> None:
        self.assertEqual(
            _extreme({}, "peak_price", "peak_pct", "peak_atr"),
            "n/a",
        )


if __name__ == "__main__":
    unittest.main()
