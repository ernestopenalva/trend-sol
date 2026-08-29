from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.logging_utils import JsonlLogger
from src.monitor.context_predicates import passes_dmi15_trajectory, passes_slow_ge45
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.entry_engine import EntrySignal


class ContextShadowTests(unittest.TestCase):
    def test_dmi15_trajectory_predicate_is_the_full_existing_rule(self) -> None:
        snapshot = {
            "plus_di14": 20, "plus_di14_15m_ago": 15, "minus_di14": 10,
            "minus_di14_15m_ago": 14, "plus_di14_5m_ago": 19, "minus_di14_5m_ago": 11,
        }
        self.assertTrue(passes_dmi15_trajectory(snapshot))
        self.assertFalse(passes_dmi15_trajectory({**snapshot, "plus_di14_5m_ago": 21}))
        self.assertIsNone(passes_dmi15_trajectory({**snapshot, "minus_di14_15m_ago": None}))

    def test_slow_ge45_is_strict_closed_candle_geometry(self) -> None:
        candles = [SimpleNamespace(high=100 + i, low=90 + i) for i in range(4)]
        self.assertTrue(passes_slow_ge45(candles))
        self.assertFalse(passes_slow_ge45([*candles[:3], SimpleNamespace(high=104, low=90)]))

    def test_admission_can_drain_open_context_positions_without_new_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow = RealAContextShadow(
                root, _config(), JsonlLogger(root, _config()), None,
                settings_key="dmi15_trajectory_context_shadow",
                strategy="DMI15_TRAJECTORY_CONTEXT_SHADOW",
                shadow_kind="DMI15_TRAJECTORY_CONTEXT_SHADOW",
                pair_prefix="testctx", predicate=lambda _engine, _snapshot: True,
            )
            first = EntrySignal("SOLUSDT", 100, "ts", 0, 0.2, "1m", 14)
            self.assertTrue(shadow.on_signal(first))
            shadow.accept_new_entries = False
            self.assertFalse(shadow.on_signal(EntrySignal("SOLUSDT", 101, "ts", 300_000, 0.2, "1m", 14)))
            shadow.on_tick(98, "2026-08-28T00:01:00+00:00")
            self.assertFalse(shadow.open_positions)
            self.assertEqual(len(shadow.ledger.load()), 1)
            self.assertEqual(shadow.ledger.load()[0]["exit_reason"], "HARD_STOP")


def _config() -> dict:
    return {
        "symbol": "SOLUSDT",
        "capital": {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5},
        "trend": {"timeframe": "15m", "ema_period": 20, "ema_slope_lookback": 3},
        "trend_gate": {"mode": "ge30", "candle_interval": "5m", "lookback_candles": 3, "sync": {"enabled": False}},
        "entry": {"timeframe": "1m", "atr_period": 14, "max_entries_per_candle": 1, "admission_candle_interval": "5m", "entry_spacing_atr": 1},
        "risk": {"hard_stop": {"enabled": True, "stop_pct": 1.5}, "no_progress": {"enabled": False}, "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1}, "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]}, "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5}},
        "fees": {"enabled": True, "taker_fee_pct": 0.1}, "ladder": {}, "logging": {"console": False},
        "instrumentation": {"dmi15_trajectory_context_shadow": {"enabled": True, "accept_new_entries": True, "max_open_positions": 5, "state_file": "data/state/context.json", "ledger_file": "data/trades/context.jsonl"}},
    }
