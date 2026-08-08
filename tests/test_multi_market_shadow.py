from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

import yaml

from src.app import Monitor
from src.logging_utils import JsonlLogger
from src.monitor.entry_engine import EntrySignal
from src.monitor.multi_market_shadow import MultiMarketShadow, SelectedMarket, ShadowMarketSelector


class FakeMarketClient:
    def exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": symbol,
                    "baseAsset": symbol.removesuffix("USDT"),
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                }
                for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "USDCUSDT")
            ]
        }

    def tickers_24h(self):
        return [
            {"symbol": "AAAUSDT", "priceChangePercent": "5", "quoteVolume": "50000000"},
            {"symbol": "BBBUSDT", "priceChangePercent": "8", "quoteVolume": "40000000"},
            {"symbol": "CCCUSDT", "priceChangePercent": "6", "quoteVolume": "30000000"},
            {"symbol": "DDDUSDT", "priceChangePercent": "-1", "quoteVolume": "60000000"},
            {"symbol": "USDCUSDT", "priceChangePercent": "10", "quoteVolume": "90000000"},
        ]

    def rolling_tickers(self, symbols, window):
        self.rolling_request = (symbols, window)
        return [
            {"symbol": "AAAUSDT", "priceChangePercent": "12"},
            {"symbol": "BBBUSDT", "priceChangePercent": "9"},
            {"symbol": "CCCUSDT", "priceChangePercent": "7"},
            {"symbol": "DDDUSDT", "priceChangePercent": "20"},
        ]

    def book_tickers(self):
        return [
            {"symbol": symbol, "bidPrice": "99.99", "askPrice": "100.01"}
            for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "USDCUSDT")
        ]

    def klines(self, symbol, interval, limit):
        return []


class FakeTelemetry:
    def __init__(self):
        self.events = []

    def submit(self, stream, event):
        self.events.append((stream, event))
        return True


class MultiMarketShadowTests(unittest.TestCase):
    def test_selector_uses_positive_liquid_tight_spread_top_three(self):
        settings = {
            "top_count": 3,
            "max_universe_symbols": 50,
            "min_quote_volume_usdt": 10_000_000,
            "max_spread_bps": 10,
            "require_positive_24h": True,
            "require_positive_7d": True,
            "excluded_base_assets": ["USDC"],
        }
        selected = ShadowMarketSelector(FakeMarketClient(), settings).select()

        self.assertEqual([item.symbol for item in selected], ["BBBUSDT", "CCCUSDT", "AAAUSDT"])
        self.assertEqual([item.rank for item in selected], [1, 2, 3])

    def test_admission_uses_fixed_virtual_notional_caps_five_and_restores_state(self):
        with TemporaryDirectory() as tmp:
            shadow, telemetry = _shadow(Path(tmp))
            shadow.selected = {
                "AAAUSDT": SelectedMarket("AAAUSDT", "AAA", 1, 8, 12, 50_000_000, 2)
            }
            shadow.epoch_started_ms = 1_000
            shadow.epoch_entries = {"AAAUSDT": 0}

            for index in range(6):
                shadow._admit_signal(
                    "AAAUSDT",
                    EntrySignal(
                        symbol="AAAUSDT",
                        price=100 + index,
                        ts=f"2026-07-25T00:0{index}:00+00:00",
                        source_candle_open_time=index,
                        entry_atr=0.1,
                        atr_timeframe="1m",
                        atr_period=14,
                    ),
                    index,
                )

            positions = shadow.positions["AAAUSDT"]
            self.assertEqual(len(positions), 5)
            self.assertTrue(all(position.phantom for position in positions))
            self.assertTrue(
                all(
                    abs(position.quantity * position.entry_price - 20.0) < 1e-9
                    for position in positions
                )
            )
            blocked = [
                event
                for _, event in telemetry.events
                if event.get("event_type") == "ADMISSION_BLOCKED"
            ]
            self.assertEqual(blocked[-1]["reason"], "BLOCKED_MAX_POSITIONS")

            restored, _ = _shadow(Path(tmp))
            restored._load_state()
            self.assertEqual(restored.open_position_count, 5)
            self.assertEqual(restored.positions["AAAUSDT"][0].shadow_selection_rank, 1)
            self.assertEqual(restored.epoch_entries["AAAUSDT"], 5)

    def test_hard_stop_quarantine_waits_for_next_hour(self):
        with TemporaryDirectory() as tmp:
            shadow, _ = _shadow(Path(tmp))
            shadow._quarantine_after_hard_stop(
                "AAAUSDT",
                "2026-07-25T10:23:00+00:00",
            )

            expected = int(
                datetime(2026, 7, 25, 11, tzinfo=timezone.utc).timestamp() * 1000
            )
            self.assertEqual(shadow.quarantined_until_ms["AAAUSDT"], expected)
            self.assertTrue(shadow._is_quarantined("AAAUSDT", expected))

    def test_virtual_hard_stop_closes_only_in_shadow_ledger(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow, _ = _shadow(root)
            shadow.selected = {
                "AAAUSDT": SelectedMarket("AAAUSDT", "AAA", 1, 8, 12, 50_000_000, 2)
            }
            shadow.epoch_started_ms = 1_000
            shadow.epoch_entries = {"AAAUSDT": 0}
            shadow._admit_signal(
                "AAAUSDT",
                EntrySignal(
                    symbol="AAAUSDT",
                    price=100,
                    ts="2026-07-25T10:00:00+00:00",
                    source_candle_open_time=1,
                    entry_atr=0.1,
                    atr_timeframe="1m",
                    atr_period=14,
                ),
                1,
            )

            shadow._on_tick(
                "AAAUSDT",
                97,
                "2026-07-25T10:23:00+00:00",
            )

            record = json.loads((root / "ledger.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["position_type"], "MARKET_SHADOW")
            self.assertEqual(record["exit_reason"], "HARD_STOP")
            self.assertEqual(record["shadow_selection_rank"], 1)
            self.assertFalse((root / "data/trades/trades_B.jsonl").exists())
            self.assertEqual(shadow.open_position_count, 0)

    def test_non_live_tick_is_never_dispatched_to_live_sol_registry(self):
        monitor = Monitor.__new__(Monitor)
        monitor.config = {"symbol": "SOLUSDT"}
        monitor.market_shadow = Mock()
        monitor.registry = Mock()
        monitor._stop_after_cycle_if_needed = Mock()
        monitor.last_price = None
        monitor.last_tick_monotonic = None

        monitor._on_ws_event("btcusdt@aggTrade", {"p": "100", "T": 1_000})

        monitor.market_shadow.on_ws_event.assert_called_once()
        monitor.registry.on_tick.assert_not_called()
        self.assertIsNone(monitor.last_price)

    def test_ge30_shadow_shares_selection_but_keeps_positions_and_files_independent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy, _ = _shadow(root)
            legacy.selected = {
                "AAAUSDT": SelectedMarket("AAAUSDT", "AAA", 1, 8, 12, 50_000_000, 2)
            }
            legacy.epoch_started_ms = 1_000
            legacy.epoch_entries = {"AAAUSDT": 0}
            ge30, _ = _shadow(
                root,
                shadow_kind="TOP3_MARKET_GE30",
                gate1_mode="ge30",
                settings_override={
                    "state_file": "state_ge30.json",
                    "ledger_file": "ledger_ge30.jsonl",
                },
                selection_source=legacy,
            )

            ge30._evaluate_selection("TEST", 1_000, reset_epoch=True)
            legacy._admit_signal(
                "AAAUSDT",
                EntrySignal("AAAUSDT", 100, "2026-07-25T00:00:00+00:00", 1, 0.1, "1m", 14),
                1_000,
            )

            self.assertEqual(set(ge30.selected), set(legacy.selected))
            self.assertEqual(legacy.open_position_count, 1)
            self.assertEqual(ge30.open_position_count, 0)
            self.assertNotEqual(legacy.state_path, ge30.state_path)
            self.assertEqual(ge30.engines["AAAUSDT"].gate1_mode, "ge30")
            self.assertEqual(legacy.settings["ledger_file"], "ledger.jsonl")
            self.assertEqual(ge30.settings["ledger_file"], "ledger_ge30.jsonl")

            ge30._admit_signal(
                "AAAUSDT",
                EntrySignal("AAAUSDT", 101, "2026-07-25T00:01:00+00:00", 2, 0.1, "1m", 14),
                1_001,
            )
            self.assertEqual(legacy.open_position_count, 1)
            self.assertEqual(ge30.open_position_count, 1)
            self.assertEqual(
                ge30.positions["AAAUSDT"][0].shadow_kind,
                "TOP3_MARKET_GE30",
            )

            legacy.selected = {
                "BBBUSDT": SelectedMarket("BBBUSDT", "BBB", 1, 9, 13, 40_000_000, 2)
            }
            ge30.sync_selection_from_source(2_000)
            self.assertEqual(set(ge30.selected), {"BBBUSDT"})
            self.assertEqual(ge30.epoch_started_ms, legacy.epoch_started_ms)


def _shadow(
    root: Path,
    shadow_kind: str = "TOP3_MARKET",
    gate1_mode: str = "legacy_ema",
    settings_override=None,
    selection_source=None,
):
    project_root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((project_root / "config/config.yaml").read_text(encoding="utf-8"))
    config["active_profile"] = "intraday"
    config["trend"].update(config["profiles"]["intraday"]["trend"])
    config["entry"].update(config["profiles"]["intraday"]["entry"])
    config["instrumentation"]["multi_market_shadow"]["state_file"] = "state.json"
    config["instrumentation"]["multi_market_shadow"]["ledger_file"] = "ledger.jsonl"
    config["logging"]["console"] = False
    logger = JsonlLogger(root, config)
    telemetry = FakeTelemetry()
    return (
        MultiMarketShadow(
            root,
            config,
            FakeMarketClient(),
            logger,
            telemetry,  # type: ignore[arg-type]
            shadow_kind=shadow_kind,
            gate1_mode=gate1_mode,
            settings_override=settings_override,
            selection_source=selection_source,
        ),
        telemetry,
    )


if __name__ == "__main__":
    unittest.main()
