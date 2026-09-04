from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow


class CircuitBreakerShadowTests(unittest.TestCase):
    def test_frozen_combo_requires_dd_rolling_loss_and_two_closes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
            shadow.equity, shadow.peak_equity = 98.4, 100.0
            shadow.closed_history = [(now - timedelta(hours=2), -0.3), (now - timedelta(hours=1), -0.3)]
            shadow._evaluate_after_close(now, 100.0)

            self.assertTrue(shadow.circuit_breaker_active)
            self.assertEqual(shadow.crises_triggered, 1)
            self.assertEqual(shadow.circuit_breaker_until, "2026-09-01T18:00:00+00:00")

    def test_two_closes_without_drawdown_do_not_trigger(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
            shadow.equity, shadow.peak_equity = 99.0, 99.0
            shadow.closed_history = [(now - timedelta(hours=2), -0.3), (now - timedelta(hours=1), -0.3)]
            shadow._evaluate_after_close(now, 100.0)

            self.assertFalse(shadow.circuit_breaker_active)


def _config() -> dict:
    return {
        "symbol": "SOLUSDT", "capital": {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5},
        "trend": {"timeframe": "15m", "ema_period": 20, "ema_slope_lookback": 3}, "trend_gate": {"mode": "ge30", "candle_interval": "5m", "lookback_candles": 3, "sync": {"enabled": False}},
        "entry": {"timeframe": "1m", "atr_period": 14, "max_entries_per_candle": 1, "admission_candle_interval": "5m", "entry_spacing_atr": 1},
        "risk": {"hard_stop": {"enabled": True, "stop_pct": 1.5}, "no_progress": {"enabled": False}, "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": .1}, "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]}, "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5}}, "fees": {"enabled": True, "taker_fee_pct": .1}, "ladder": {}, "logging": {"console": False},
        "instrumentation": {"circuit_breaker_shadow": {"enabled": True, "accept_new_entries": True, "initial_capital_usdt": 100, "max_open_positions": 5, "state_file": "data/state/cb.json", "ledger_file": "data/trades/cb.jsonl"}},
    }
