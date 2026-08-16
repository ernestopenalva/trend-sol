from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from src.config_profiles import effective_config


class ConfigProfileTests(unittest.TestCase):
    def test_runtime_intraday_config_resolves_ge15_and_hard_stop_15(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        config = yaml.safe_load((project_root / "config/config.yaml").read_text(encoding="utf-8"))
        effective = effective_config(config)

        self.assertEqual(effective["trend_gate"]["candle_interval"], "5m")
        self.assertEqual(effective["trend_gate"]["lookback_candles"], 3)
        self.assertTrue(effective["trend_gate"]["sync"]["enabled"])
        self.assertEqual(effective["trend_gate"]["sync"]["timeout_seconds"], 15)
        self.assertEqual(effective["risk"]["hard_stop"]["stop_pct"], 1.5)
        self.assertEqual(effective["entry"]["timeframe"], "1m")
        self.assertEqual(effective["entry"]["atr_period"], 14)

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

    def test_ge30_and_ema_observations_add_only_required_streams(self) -> None:
        config = self.base_config()
        config["active_profile"] = "intraday"
        config["trend_gate"] = {
            "mode": "ge30",
            "candle_interval": "5m",
            "lookback_candles": 6,
        }
        config["ema_observations"] = {
            "enabled": True,
            "slope_window_minutes": 30,
            "variants": [
                {"period": 50, "interval": "15m"},
                {"period": 20, "interval": "5m"},
            ],
        }

        effective = effective_config(config)

        self.assertEqual(
            effective["market_data"]["kline_streams"],
            ["solusdt@kline_1m", "solusdt@kline_15m", "solusdt@kline_5m"],
        )

    def test_enabled_hard_stop_requires_valid_percentage(self) -> None:
        config = self.base_config()
        config["risk"] = {"hard_stop": {"enabled": True, "stop_pct": 0}}

        with self.assertRaisesRegex(ValueError, "hard_stop.stop_pct"):
            effective_config(config)

    def test_ge_sync_requires_positive_timeout_and_skip_action(self) -> None:
        config = self.base_config()
        config["active_profile"] = "intraday"
        config["trend_gate"] = {
            "mode": "ge30",
            "candle_interval": "5m",
            "lookback_candles": 3,
            "sync": {
                "enabled": True,
                "timeout_seconds": 0,
                "expire_on_next_entry_candle": True,
                "timeout_action": "SKIP",
            },
        }
        with self.assertRaisesRegex(ValueError, "sync.timeout_seconds"):
            effective_config(config)

        config["trend_gate"]["sync"]["timeout_seconds"] = 15
        config["trend_gate"]["sync"]["timeout_action"] = "USE_STALE"
        with self.assertRaisesRegex(ValueError, "sync.timeout_action"):
            effective_config(config)

    def test_enabled_phantoms_require_explicit_positive_limits(self) -> None:
        config = self.base_config()
        config["instrumentation"] = {
            "enabled": True,
            "phantoms": {"enabled": True, "max_open_positions": 0, "max_age_hours": 72},
        }

        with self.assertRaisesRegex(ValueError, "phantoms.max_open_positions"):
            effective_config(config)

    def test_profit_lock_shadow_requires_non_negative_parameters(self) -> None:
        config = self.base_config()
        config["risk"] = {
            "profit_lock": {
                "net_floor_shadow": {
                    "enabled": True,
                    "net_margin_pct": 0.05,
                    "activation_buffer_atr": -0.5,
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "net_floor_shadow.activation_buffer_atr"):
            effective_config(config)

    def test_profit_lock_economic_floor_requires_non_negative_margin(self) -> None:
        config = self.base_config()
        config["risk"] = {
            "profit_lock": {
                "economic_floor": {
                    "enabled": True,
                    "net_margin_pct": -0.01,
                }
            }
        }

        with self.assertRaisesRegex(ValueError, "economic_floor.net_margin_pct"):
            effective_config(config)

    def test_multi_market_shadow_requires_epoch_cap_to_cover_slots(self) -> None:
        config = self.base_config()
        config["instrumentation"] = {
            "enabled": True,
            "multi_market_shadow": {
                "enabled": True,
                "top_count": 3,
                "reevaluate_hours": 4,
                "max_universe_symbols": 50,
                "max_open_positions_per_symbol": 5,
                "max_entries_per_selection_epoch": 4,
                "min_quote_volume_usdt": 10_000_000,
                "max_spread_bps": 10,
            },
        }

        with self.assertRaisesRegex(ValueError, "max_entries_per_selection_epoch"):
            effective_config(config)

    def test_ge30_shadow_requires_independent_state_and_ledger(self) -> None:
        config = self.base_config()
        config["instrumentation"] = {
            "enabled": True,
            "multi_market_shadow": {
                "enabled": True,
                "top_count": 3,
                "reevaluate_hours": 4,
                "max_universe_symbols": 50,
                "max_open_positions_per_symbol": 5,
                "max_entries_per_selection_epoch": 5,
                "min_quote_volume_usdt": 10_000_000,
                "max_spread_bps": 10,
                "state_file": "same.json",
                "ledger_file": "same.jsonl",
                "ge30_variant": {
                    "enabled": True,
                    "state_file": "same.json",
                    "ledger_file": "same.jsonl",
                },
            },
        }

        with self.assertRaisesRegex(ValueError, "must be independent from legacy"):
            effective_config(config)


if __name__ == "__main__":
    unittest.main()
