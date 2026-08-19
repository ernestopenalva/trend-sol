from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from src.indicators.indicators import dmi_adx
from src.logging_utils import JsonlLogger
from src.monitor.entry_engine import Candle, EntryEngine, EntrySignal
from src.monitor.dmi15_shadow import Dmi15ShadowRegistry
from src.monitor.gcr_shadow import GcrShadowRegistry
from src.monitor.market_context import MarketContextEngine
from src.no_progress import resolved_no_progress_tolerance
from src.position.bot_full_engine import BotFullExitPosition


class FakeClient:
    def __init__(self) -> None:
        self.sells = []

    def market_sell(self, symbol, quantity, client_order_id):
        self.sells.append((symbol, quantity, client_order_id))
        return {"executedQty": str(quantity), "cummulativeQuoteQty": str(quantity * 99), "clientOrderId": client_order_id}


class NewPackageTests(unittest.TestCase):
    def test_tolerance_uses_strict_last_twenty_and_exactly_four_samples(self) -> None:
        old = [{"be_armed_at": "x", "time_to_be_seconds": 1}] * 4
        recent = [{"be_armed_at": None, "time_to_be_seconds": None}] * 16 + [
            {"be_armed_at": "x", "time_to_be_seconds": value} for value in (100, 200, 300, 400)
        ]
        result = resolved_no_progress_tolerance(old + recent, _settings())
        self.assertEqual(result["source"], "ROLLING_MEDIAN")
        self.assertAlmostEqual(result["median_seconds"], 250)
        self.assertAlmostEqual(result["seconds"], 312.5)
        fallback_recent = [{"be_armed_at": None, "time_to_be_seconds": None}] * 17 + [
            {"be_armed_at": "x", "time_to_be_seconds": value} for value in (100, 200, 300)
        ]
        fallback = resolved_no_progress_tolerance(old + fallback_recent, _settings())
        self.assertEqual(fallback["source"], "DEFAULT")
        self.assertEqual(fallback["seconds"], 7200)

    def test_no_progress_closes_at_frozen_tolerance_and_be_disables_it(self) -> None:
        with TemporaryDirectory() as tmp:
            logger = _logger(Path(tmp))
            client = FakeClient()
            position = _position(client, logger, tolerance=7200)
            self.assertIsNone(position.on_tick(99.5, "2026-08-15T01:59:59+00:00"))
            event = position.on_tick(99.5, "2026-08-15T02:00:00+00:00")
            self.assertEqual(event["exit_reason"], "NO_PROGRESS_EXIT")

            proven = _position(FakeClient(), logger, tolerance=1)
            proven.on_tick(100.7, "2026-08-15T00:00:01+00:00")
            self.assertIsNotNone(proven.be_armed_at)
            self.assertIsNone(proven.on_tick(100.4, "2026-08-15T04:00:00+00:00"))

    def test_restored_legacy_position_is_grandfathered(self) -> None:
        with TemporaryDirectory() as tmp:
            logger = _logger(Path(tmp))
            original = _position(FakeClient(), logger, tolerance=7200)
            state = original.to_state()
            state.pop("no_progress_enabled")
            state.pop("no_progress_tolerance_seconds")
            restored = BotFullExitPosition.from_state(state, original.config, FakeClient(), logger)  # type: ignore[arg-type]
            self.assertFalse(restored.no_progress_enabled)

    def test_market_context_has_symmetric_5m_and_15m_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = EntryEngine("SOLUSDT", _context_config(), _logger(Path(tmp)))
            candles = [_candle(index, 100 + index * 0.1) for index in range(80)]
            engine.auxiliary_candles["5m"] = candles
            engine.trend_candles = candles
            context = MarketContextEngine(engine, _context_config()).refresh()
            expected = {"ema20", "ema50", "ema20_slope_pct", "ema50_slope_pct", "adx14", "plus_di14", "minus_di14", "rsi14", "relative_volume"}
            self.assertTrue(expected.issubset(context["tf_5m"]))
            self.assertTrue(expected.issubset(context["tf_15m"]))
            self.assertIsNotNone(context["tf_5m"]["plus_di14_15m_ago"])
            self.assertIsNotNone(context["tf_5m"]["minus_di14_15m_ago"])
            self.assertIsNotNone(context["tf_5m"]["rsi14_15m_ago"])
            before = context["tf_5m"]["ema20"]
            engine.auxiliary_candles["5m"].append(
                Candle(99_000_000, 99_299_999, 500, 501, 499, 500, 9999, False)
            )
            after = MarketContextEngine(engine, _context_config()).refresh()["tf_5m"]["ema20"]
            self.assertEqual(before, after)

    def test_gcr_blocks_until_previous_position_arms_be(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _context_config()
            shadow = GcrShadowRegistry(root, config, _logger(root))
            one = EntrySignal("SOLUSDT", 100, "ts", 0, 0.2, "1m", 14)
            two = EntrySignal("SOLUSDT", 101, "ts", 300_000, 0.2, "1m", 14)
            self.assertTrue(shadow.on_signal(one))
            self.assertFalse(shadow.on_signal(two))
            self.assertEqual(shadow.blocked_gcr, 1)
            shadow.on_tick(100.7, "2026-08-15T00:01:00+00:00")
            self.assertTrue(shadow.on_signal(two))
            self.assertEqual(len(shadow.open_positions), 2)

    def test_gcr_no_progress_exit_releases_admission(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _context_config()
            shadow = GcrShadowRegistry(root, config, _logger(root))
            self.assertTrue(shadow.on_signal(EntrySignal("SOLUSDT", 100, "ts", 0, 0.2, "1m", 14)))
            shadow.open_positions[0].open_ts = "2026-08-15T00:00:00+00:00"
            shadow.open_positions[0].no_progress_tolerance_seconds = 7200
            shadow.on_tick(99.5, "2026-08-15T02:00:00+00:00")
            self.assertEqual(len(shadow.open_positions), 0)
            self.assertTrue(shadow.on_signal(EntrySignal("SOLUSDT", 99.5, "ts", 300_000, 0.2, "1m", 14)))

    def test_dmi15_shadow_uses_only_strict_dmi_rule_and_own_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _context_config()
            config["risk"]["no_progress"]["enabled"] = False
            shadow = Dmi15ShadowRegistry(root, config, _logger(root))
            context = {
                "captured_at": "2026-08-19T12:05:00+00:00",
                "tf_5m": {
                    "latest_open_at_ms": 300_000,
                    "latest_closed_at_ms": 599_999,
                    "close": 100,
                    "plus_di14": 30,
                    "plus_di14_15m_ago": 20,
                    "minus_di14": 10,
                    "minus_di14_15m_ago": 15,
                },
            }
            self.assertTrue(shadow.on_closed_5m(context, 0.2, "1m", 14))
            self.assertEqual(len(shadow.open_positions), 1)
            self.assertFalse(shadow.open_positions[0].no_progress_enabled)
            self.assertFalse(shadow.on_closed_5m(context, 0.2, "1m", 14))

            blocked = deepcopy(context)
            blocked["tf_5m"]["latest_open_at_ms"] = 600_000
            blocked["tf_5m"]["latest_closed_at_ms"] = 899_999
            blocked["tf_5m"]["plus_di14"] = 19
            self.assertFalse(shadow.on_closed_5m(blocked, 0.2, "1m", 14))
            self.assertEqual(len(shadow.open_positions), 1)
            shadow.on_tick(98.0, "2026-08-19T12:06:00+00:00")
            self.assertEqual(len(shadow.open_positions), 0)
            records = shadow.ledger.load()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["position_type"], "DMI15_SHADOW")
            self.assertEqual(records[0]["shadow_kind"], "DMI15_SHADOW")

    def test_restored_position_disables_npe_when_config_disables_it(self) -> None:
        with TemporaryDirectory() as tmp:
            logger = _logger(Path(tmp))
            original = _position(FakeClient(), logger, tolerance=7200)
            config = dict(original.config)
            config["no_progress"] = {"enabled": False}
            restored = BotFullExitPosition.from_state(
                original.to_state(), config, FakeClient(), logger  # type: ignore[arg-type]
            )
            self.assertFalse(restored.no_progress_enabled)
            self.assertIsNone(restored.no_progress_tolerance_seconds)
            self.assertEqual(restored.no_progress_tolerance_source, "DISABLED_BY_CONFIG")


def _settings():
    return {"default_hours": 2, "rolling_window": 20, "min_be_samples": 4, "tolerance_buffer_pct": 25}


def _logger(root: Path):
    return JsonlLogger(root, {"logging": {"console": False, "trade_log": "logs/trades.jsonl", "decision_log": "logs/decisions.jsonl", "system_log": "logs/system.log"}})


def _position(client, logger, tolerance):
    return BotFullExitPosition("p", "SOLUSDT", 100, 1, {}, "2026-08-15T00:00:00+00:00", {
        "hard_stop": {"enabled": True, "stop_pct": 1.5},
        "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1},
        "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]},
        "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5},
    }, client, logger, entry_atr=0.2, no_progress_enabled=True, no_progress_tolerance_seconds=tolerance)


def _context_config():
    return {
        "symbol": "SOLUSDT", "active_profile": "intraday", "strategy_version": "x",
        "trend": {"timeframe": "15m", "ema_period": 50, "ema_slope_lookback": 3},
        "trend_gate": {"mode": "ge30", "candle_interval": "5m", "lookback_candles": 3},
        "entry": {"timeframe": "1m", "atr_period": 14, "max_entries_per_candle": 1, "entry_spacing_atr": 0},
        "capital": {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5},
        "risk": {"hard_stop": {"enabled": True, "stop_pct": 1.5}, "no_progress": {"enabled": True, **_settings()}, "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1}, "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]}, "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5}},
        "fees": {"enabled": True, "taker_fee_pct": 0.1}, "ladder": {},
        "instrumentation": {"enabled": True, "gcr_shadow": {"enabled": True}, "dmi15_shadow": {"enabled": True}, "market_context": {"enabled": True, "slope_lookback_candles": 3, "relative_volume_period": 20}},
    }


def _candle(index, close):
    return Candle(index * 300_000, (index + 1) * 300_000 - 1, close - .05, close + .2, close - .2, close, 100 + index, True)


if __name__ == "__main__":
    unittest.main()
