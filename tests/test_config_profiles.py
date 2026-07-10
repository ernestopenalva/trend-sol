from __future__ import annotations

import unittest

from src.config_profiles import effective_config


class ConfigProfileTests(unittest.TestCase):
    def base_config(self):
        return {
            "symbol": "SOLUSDT",
            "market_data": {"trade_stream": "solusdt@aggTrade"},
            "trend": {"timeframe": "1d", "ema_period": 50},
            "entry": {
                "timeframe": "4h",
                "pullback_atr_multiplier": 1.5,
                "rsi_threshold": 45,
            },
            "profiles": {
                "production": {
                    "trend": {"timeframe": "1d"},
                    "entry": {"timeframe": "4h", "rsi_threshold": 45},
                },
                "intraday": {
                    "trend": {"timeframe": "15m"},
                    "entry": {"timeframe": "1m", "rsi_threshold": 55},
                },
            },
        }

    def test_production_keeps_daily_and_four_hour_streams(self) -> None:
        config = self.base_config()
        config["active_profile"] = "production"
        effective = effective_config(config)
        self.assertEqual(effective["trend"]["timeframe"], "1d")
        self.assertEqual(effective["entry"]["timeframe"], "4h")
        self.assertEqual(
            effective["market_data"]["kline_streams"],
            ["solusdt@kline_4h", "solusdt@kline_1d"],
        )

    def test_intraday_uses_short_timeframes(self) -> None:
        config = self.base_config()
        config["active_profile"] = "intraday"
        effective = effective_config(config)
        self.assertEqual(effective["trend"]["timeframe"], "15m")
        self.assertEqual(effective["entry"]["timeframe"], "1m")
        self.assertEqual(effective["entry"]["rsi_threshold"], 55)
        self.assertEqual(
            effective["market_data"]["kline_streams"],
            ["solusdt@kline_1m", "solusdt@kline_15m"],
        )


if __name__ == "__main__":
    unittest.main()
