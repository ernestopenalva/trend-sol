from __future__ import annotations

import unittest
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.entry_engine import EntrySignal
from tools.circuit_breaker_shadow_report import _opened_after, _rows


class CircuitBreakerShadowTests(unittest.TestCase):
    def test_incident_late_tick_has_no_financial_effect_and_next_tick_runs(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            shadow.on_signal(EntrySignal('SOLUSDT', 118.49, '2026-09-29T16:25:11+00:00',
                                         1790698980000, .2, '1m', 14))
            positions = [x.to_state() for x in shadow.positions]
            clock, points = shadow.clock.to_state(), deepcopy(shadow.market_points)
            last_input, sequence = shadow.last_input_ms, shadow.sequence
            shadow.on_tick(100.0, '2026-09-29T16:24:00.451+00:00')
            self.assertTrue(shadow.enabled)
            self.assertEqual(shadow.sequence, sequence + 1)
            self.assertEqual(positions, [x.to_state() for x in shadow.positions])
            self.assertEqual(clock, shadow.clock.to_state())
            self.assertEqual(points, shadow.market_points)
            self.assertEqual(last_input, shadow.last_input_ms)
            self.assertEqual(shadow.closed_records, [])
            self.assertEqual(shadow.audit_events[-1]['event'], 'CB_INPUT_REJECTED_LATE')
            self.assertEqual(shadow.audit_events[-1]['financial_effect'], 'NONE')
            shadow.on_tick(118.50, '2026-09-29T16:26:00+00:00')
            self.assertTrue(shadow.enabled)
            self.assertEqual(shadow.last_input_ms, 1790699160000)

    def test_existing_pending_late_tick_recovery_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config(); logger = JsonlLogger(root, config)
            shadow = CircuitBreakerShadow(root, config, logger, None)
            shadow.on_signal(EntrySignal('SOLUSDT', 118.49, '2026-09-29T16:25:11+00:00',
                                         1790698980000, .2, '1m', 14))
            pending = {'kind': 'tick', 'price': 118.49,
                       'observed_at': '2026-09-29T16:24:00.451+00:00',
                       'sequence': shadow.sequence + 1}
            shadow.pending_input_path.write_text(json.dumps(pending), encoding='utf8')
            restored = CircuitBreakerShadow(root, config, logger, None)
            self.assertTrue(restored.enabled)
            self.assertEqual(restored.sequence, pending['sequence'])
            self.assertEqual(restored.clock.to_state(), shadow.clock.to_state())
            for field in ('pair_id', 'status', 'entry_price', 'reserved_qty', 'effective_stop',
                          'highest_price', 'trough_price', 'exit_price', 'exit_reason'):
                self.assertEqual(getattr(restored.positions[0], field), getattr(shadow.positions[0], field))
            self.assertEqual(restored.closed_records, [])
            self.assertTrue(restored.audit_events[-1]['recovered_pending'])
            self.assertTrue(restored.audit_events[-1]['forward_gap_requires_review'])
            self.assertEqual(json.loads(restored.pending_input_path.read_text()), pending)
            again = CircuitBreakerShadow(root, config, logger, None)
            self.assertEqual(len([x for x in again.audit_events if x['event'] == 'CB_INPUT_REJECTED_LATE']), 1)
            self.assertTrue(again.enabled)
            again.on_tick(118.50, '2026-09-29T16:26:00+00:00')
            self.assertTrue(again.enabled)
            self.assertEqual(again.last_input_ms, 1790699160000)

    def test_same_minute_out_of_order_tick_and_late_signal_are_rejected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            shadow.on_tick(118.49, '2026-09-29T16:25:30+00:00')
            shadow.on_tick(100., '2026-09-29T16:25:20+00:00')
            self.assertEqual(shadow.audit_events[-1]['reason'], 'OUT_OF_ORDER_TICK')
            before = shadow.clock.to_state()
            context = deepcopy(shadow.latest_market_context)
            shadow.on_approved_real_a_signal(
                EntrySignal('SOLUSDT', 100., '2026-09-29T16:24:00+00:00',
                            1790698980000, .2, '1m', 14), {'tf_5m': {'ema_context': 'SHO'}})
            self.assertEqual(shadow.open_positions, [])
            self.assertEqual(shadow.latest_market_context, context)
            self.assertEqual(shadow.clock.to_state(), before)
            self.assertTrue(shadow.enabled)

    def test_unexpected_failure_still_disables_arm(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            with self.assertRaises(ValueError):
                shadow._run_input({'kind': 'unknown'})
            self.assertFalse(shadow.enabled)

    def test_frozen_combo_requires_dd_rolling_loss_and_two_closes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
            shadow.clock.equity, shadow.clock.peak = 98.4, 100.0
            shadow.clock.history = [[int((now-timedelta(hours=2)).timestamp()*1000),-.3], [int((now-timedelta(hours=1)).timestamp()*1000),-.3]]
            shadow._advance_clock(now, 100.0)

            self.assertTrue(shadow.circuit_breaker_active)
            self.assertEqual(shadow.crises_triggered, 1)
            self.assertEqual(shadow.circuit_breaker_until, "2026-09-01T18:00:00+00:00")

    def test_two_closes_without_drawdown_do_not_trigger(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
            shadow.clock.equity, shadow.clock.peak = 99.0, 99.0
            shadow.clock.history = [[int((now-timedelta(hours=2)).timestamp()*1000),-.3], [int((now-timedelta(hours=1)).timestamp()*1000),-.3]]
            shadow._advance_clock(now, 100.0)

            self.assertFalse(shadow.circuit_breaker_active)

    def test_approved_real_a_signal_keeps_context_but_uses_own_admission(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            signal = EntrySignal("SOLUSDT", 100.0, "2026-09-04T15:51:00+00:00", 1_788_537_000_000, 0.2, "1m", 14)

            self.assertTrue(shadow.on_approved_real_a_signal(signal, {"tf_5m": {"ema20": 100.0}}))
            self.assertEqual(len(shadow.open_positions), 1)
            self.assertEqual(shadow.latest_market_context, {"tf_5m": {"ema20": 100.0}})

    def test_candles_can_never_open_the_paired_shadow(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp); config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            shadow.on_kline("solusdt@kline_1m", {"k": {"x": True}}, {"tf_5m": {}})
            self.assertEqual(shadow.open_positions, [])

    def test_forward_report_accepts_open_state_timestamp(self) -> None:
        since = datetime(2026, 9, 4, 14, 20, tzinfo=timezone.utc)
        self.assertTrue(_opened_after({"open_ts": "2026-09-04T14:21:00+00:00"}, since))

    def test_forward_report_reads_real_a_list_state(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "open_positions.json"
            path.write_text('[{"status":"OPEN","label":"B"}]', encoding="utf-8")
            self.assertEqual(_rows(path), [{"status": "OPEN", "label": "B"}])


def _config() -> dict:
    return {
        "symbol": "SOLUSDT", "capital": {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5},
        "trend": {"timeframe": "15m", "ema_period": 20, "ema_slope_lookback": 3}, "trend_gate": {"mode": "ge30", "candle_interval": "5m", "lookback_candles": 3, "sync": {"enabled": False}},
        "entry": {"timeframe": "1m", "atr_period": 14, "max_entries_per_candle": 1, "admission_candle_interval": "5m", "entry_spacing_atr": 1},
        "risk": {"hard_stop": {"enabled": True, "stop_pct": 1.5}, "no_progress": {"enabled": False}, "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": .1}, "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]}, "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5}}, "fees": {"enabled": True, "taker_fee_pct": .1}, "ladder": {}, "logging": {"console": False},
        "instrumentation": {"circuit_breaker_shadow": {"enabled": True, "accept_new_entries": True, "initial_capital_usdt": 100, "max_open_positions": 5, "state_file": "data/state/cb.json", "ledger_file": "data/trades/cb.jsonl"}},
    }
