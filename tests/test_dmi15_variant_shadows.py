from __future__ import annotations

import unittest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from src.monitor.dmi15_combined_shadow import Dmi15CombinedShadowRegistry
from src.monitor.dmi15_shadow import Dmi15ShadowRegistry
from src.monitor.dmi15_rsi70_shadow import Dmi15Rsi70ShadowRegistry
from src.monitor.dmi15_spread_shadow import Dmi15SpreadShadowRegistry
from src.monitor.dmi15_trajectory_shadow import Dmi15TrajectoryShadowRegistry
from src.logging_utils import JsonlLogger


class _Logger:
    def decision(self, payload):
        return None


def _config(key: str) -> dict:
    return {"instrumentation": {key: {"enabled": False, "min_di_spread": 6, "max_rsi_ma": 70}}}


def _snapshot(**overrides: float) -> dict:
    values = {
        "plus_di14": 20.0,
        "plus_di14_5m_ago": 19.0,
        "minus_di14": 10.0,
        "minus_di14_5m_ago": 11.0,
        "rsi14_sma14": 70.0,
    }
    values.update(overrides)
    return values


class Dmi15VariantShadowTests(unittest.TestCase):
    def test_trajectory_requires_both_di_directions(self) -> None:
        shadow = Dmi15TrajectoryShadowRegistry(Path("."), _config("dmi15_trajectory_shadow"), _Logger())
        self.assertTrue(shadow._passes_additional_entry_gate(1, 10.0, _snapshot()))
        self.assertFalse(shadow._passes_additional_entry_gate(2, 10.0, _snapshot(minus_di14_5m_ago=9.0)))
        self.assertEqual(shadow.blocked_trajectory, 1)

    def test_rsi70_allows_exact_threshold_and_blocks_above(self) -> None:
        shadow = Dmi15Rsi70ShadowRegistry(Path("."), _config("dmi15_rsi70_shadow"), _Logger())
        self.assertTrue(shadow._passes_additional_entry_gate(1, 10.0, _snapshot(rsi14_sma14=70.0)))
        self.assertFalse(shadow._passes_additional_entry_gate(2, 10.0, _snapshot(rsi14_sma14=70.01)))
        self.assertEqual(shadow.blocked_rsi_ma, 1)

    def test_unavailable_indicators_are_not_counted_as_rule_blocks(self) -> None:
        trajectory = Dmi15TrajectoryShadowRegistry(Path("."), _config("dmi15_trajectory_shadow"), _Logger())
        rsi70 = Dmi15Rsi70ShadowRegistry(Path("."), _config("dmi15_rsi70_shadow"), _Logger())
        self.assertFalse(trajectory._passes_additional_entry_gate(1, 10.0, _snapshot(plus_di14_5m_ago=None)))
        self.assertFalse(rsi70._passes_additional_entry_gate(1, 10.0, _snapshot(rsi14_sma14=None)))
        self.assertEqual((trajectory.blocked_trajectory, trajectory.blocked_required_indicator_unavailable), (0, 1))
        self.assertEqual((rsi70.blocked_rsi_ma, rsi70.blocked_required_indicator_unavailable), (0, 1))

    def test_combined_counts_only_first_failed_gate(self) -> None:
        shadow = Dmi15CombinedShadowRegistry(Path("."), _config("dmi15_combined_shadow"), _Logger())
        self.assertFalse(shadow._passes_additional_entry_gate(1, 5.0, _snapshot(rsi14_sma14=99.0)))
        self.assertEqual((shadow.blocked_spread, shadow.blocked_trajectory, shadow.blocked_rsi_ma), (1, 0, 0))
        self.assertFalse(shadow._passes_additional_entry_gate(2, 10.0, _snapshot(minus_di14_5m_ago=9.0, rsi14_sma14=99.0)))
        self.assertEqual((shadow.blocked_spread, shadow.blocked_trajectory, shadow.blocked_rsi_ma), (1, 1, 0))

    def test_closed_5m_variants_write_independent_states_and_ledgers(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _runtime_config()
            logger = JsonlLogger(root, config)
            c = Dmi15ShadowRegistry(root, config, logger)
            d = Dmi15SpreadShadowRegistry(root, config, logger)
            e = Dmi15TrajectoryShadowRegistry(root, config, logger)
            f = Dmi15Rsi70ShadowRegistry(root, config, logger)
            g = Dmi15CombinedShadowRegistry(root, config, logger)
            allowed = _context(300_000)
            for shadow in (c, d, e, f, g):
                self.assertTrue(shadow.on_closed_5m(allowed, 0.2, "1m", 14))
            self.assertFalse(e.on_closed_5m(_context(600_000, minus_di14_5m_ago=9), 0.2, "1m", 14))
            self.assertFalse(f.on_closed_5m(_context(600_000, rsi14_sma14=71), 0.2, "1m", 14))
            self.assertFalse(g.on_closed_5m(_context(600_000, plus_di14=20, minus_di14=15, minus_di14_15m_ago=17), 0.2, "1m", 14))
            self.assertEqual((e.blocked_trajectory, f.blocked_rsi_ma, g.blocked_spread), (1, 1, 1))
            for shadow in (c, d, e, f, g):
                shadow.on_tick(98.0, "2026-08-19T12:06:00+00:00")
                self.assertFalse(shadow.open_positions)
                self.assertEqual(len(shadow.ledger.load()), 1)
                self.assertEqual(shadow.ledger.load()[0]["exit_reason"], "HARD_STOP")
                self.assertTrue(shadow.state_path.exists())
                self.assertEqual(json.loads(shadow.state_path.read_text(encoding="utf-8"))["positions"], [])


def _context(bucket: int, **overrides: float) -> dict:
    snapshot = _snapshot()
    snapshot.update({
        "latest_open_at_ms": bucket,
        "latest_closed_at_ms": bucket + 299_999,
        "close": 100.0,
        "plus_di14_15m_ago": 10.0,
        "minus_di14_15m_ago": 15.0,
    })
    snapshot.update(overrides)
    return {"captured_at": "2026-08-19T12:05:00+00:00", "tf_5m": snapshot}


def _runtime_config() -> dict:
    config = {
        "symbol": "SOLUSDT",
        "capital": {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5},
        "entry": {"max_entries_per_candle": 1},
        "risk": {
            "hard_stop": {"enabled": True, "stop_pct": 1.5},
            "no_progress": {"enabled": False},
            "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1},
            "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]},
            "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5},
        },
        "fees": {"enabled": True, "taker_fee_pct": 0.1},
        "ladder": {},
        "logging": {"console": False},
        "instrumentation": {},
    }
    for key in ("dmi15_shadow", "dmi15_spread_shadow", "dmi15_trajectory_shadow", "dmi15_rsi70_shadow", "dmi15_combined_shadow"):
        config["instrumentation"][key] = {
            "enabled": True,
            "min_di_spread": 6,
            "max_rsi_ma": 70,
            "state_file": f"data/state/{key}.json",
            "ledger_file": f"data/trades/trades_{key}.jsonl",
        }
    return config
