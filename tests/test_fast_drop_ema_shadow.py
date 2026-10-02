import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.monitor.forward_experiment_shadows import FastDropEmaShadow
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.entry_engine import EntrySignal
from tests.test_circuit_breaker_shadow import _config


CONTEXT_CLOSE = 1790813099999  # 2026-10-01 00:04:59.999 UTC


def _snapshot(context):
    return {'tf_5m': {'ema_context': context, 'latest_closed_at_ms': CONTEXT_CLOSE}}


class FastDropTests(unittest.TestCase):
    def make(self, root, capital=100):
        config = _config()
        config['instrumentation']['be_off_cb_fast_drop_ema_shadow'] = {
            'enabled': True, 'accept_new_entries': True, 'initial_capital_usdt': capital,
            'max_open_positions': 1, 'state_file': 'data/state/fast.json',
            'ledger_file': 'data/trades/fast.jsonl', 'events_file': 'data/events/fast.jsonl'}
        return FastDropEmaShadow(root, config, JsonlLogger(root, config), None,
                                 cohort_started_at='2026-10-01T00:00:00+00:00'), config

    def open(self, shadow):
        signal = EntrySignal('SOLUSDT', 100., '2026-10-01T00:05:00+00:00',
                             1790813040000, .2, '1m', 14)
        self.assertTrue(shadow.on_approved_real_a_signal(signal, _snapshot('LON')))

    def reference(self, shadow, price=100.):
        from datetime import datetime
        boundary = int(datetime.fromisoformat('2026-10-01T00:02:00+00:00').timestamp()*1000)
        shadow.on_closed_1m({'x': True, 'T': boundary - 1, 'c': str(price)})

    def test_fixed_conditions_use_trigger_context(self):
        for context, price, reference, fires in (
            ('SHO', 99.5, 100., True), ('BEA', 99.5, 100., True),
            ('LON', 99.5, 100., False), ('BUL', 99.5, 100., False),
            ('SHO', 99.6, 100., False), ('BEA', 99.5, 99.6, False)):
            with self.subTest(context=context, price=price, reference=reference), TemporaryDirectory() as tmp:
                shadow, _ = self.make(Path(tmp)); self.open(shadow); self.reference(shadow, reference)
                shadow.on_closed_5m(_snapshot(context))
                shadow.on_tick(price, '2026-10-01T00:06:01+00:00')
                self.assertEqual(len(shadow.closed_records), int(fires))
                if fires:
                    row = shadow.closed_records[0]
                    self.assertEqual(row['exit_reason'], 'FAST_DROP')
                    self.assertEqual(row['ema_context'], context)
                    self.assertAlmostEqual(row['velocity_5m_pct_per_min'], -.1)
                    event = [e for e in shadow.audit_events if e['event'] == 'FAST_DROP'][-1]
                    self.assertEqual(event['ts'], '2026-10-01T00:06:01+00:00')

    def test_release_slot_equity_cb_independence_and_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); shadow, config = self.make(root); self.open(shadow); self.reference(shadow)
            control = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None, be_off=True)
            self.open(control)
            shadow.on_closed_5m(_snapshot('SHO'))
            shadow.on_tick(99.5, '2026-10-01T00:06:01+00:00')
            self.assertEqual(shadow.open_positions, [])
            control.on_tick(99.5, '2026-10-01T00:06:01+00:00')
            self.assertEqual(len(control.open_positions), 1)
            restored, _ = self.make(root)
            self.assertEqual(restored.minute_closes, shadow.minute_closes)
            self.assertEqual(len(restored.closed_records), 1)
            restored.on_tick(99.5, '2026-10-01T00:07:01+00:00')
            self.assertAlmostEqual(restored.equity, 99.86)
            self.assertAlmostEqual(control.equity, 100.)
            self.assertEqual(len(restored.clock.history), 1)
            self.assertTrue(restored.on_signal(EntrySignal('SOLUSDT', 99.5, '2026-10-01T00:10:00+00:00',
                                                        1790813340000, .2, '1m', 14)))

    def test_first_crossing_persists_and_does_not_recalibrate(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); shadow, _ = self.make(root); self.open(shadow); self.reference(shadow)
            shadow.on_tick(99.5, '2026-10-01T00:06:01+00:00')  # LON at first crossing
            restored, _ = self.make(root)
            self.assertTrue(restored.open_positions[0].fast_drop_evaluated)
            restored.on_closed_5m(_snapshot('SHO'))
            restored.on_tick(99.4, '2026-10-01T00:06:02+00:00')
            self.assertEqual(restored.closed_records, [])

    def test_fast_closes_can_trigger_own_cb_and_block_admission(self):
        from datetime import datetime
        with TemporaryDirectory() as tmp:
            shadow, _ = self.make(Path(tmp), capital=10)
            self.open(shadow); self.reference(shadow)
            shadow.on_closed_5m(_snapshot('SHO'))
            shadow.on_tick(99.5, '2026-10-01T00:06:01+00:00')
            shadow.on_tick(99.5, '2026-10-01T00:07:01+00:00')
            self.assertTrue(shadow.on_signal(EntrySignal('SOLUSDT', 100., '2026-10-01T00:10:00+00:00',
                                                       1790813340000, .2, '1m', 14)))
            boundary = int(datetime.fromisoformat('2026-10-01T00:07:00+00:00').timestamp()*1000)
            shadow.on_closed_1m({'x': True, 'T': boundary-1, 'c': '100'})
            shadow.on_tick(99.5, '2026-10-01T00:11:01+00:00')
            shadow.on_tick(99.5, '2026-10-01T00:12:01+00:00')
            self.assertEqual(len(shadow.clock.history), 2)
            self.assertAlmostEqual(shadow.equity, 9.72)
            self.assertTrue(shadow.circuit_breaker_active)
            self.assertFalse(shadow.on_signal(EntrySignal('SOLUSDT', 100., '2026-10-01T00:15:00+00:00',
                                                        1790813640000, .2, '1m', 14)))


if __name__ == '__main__':
    unittest.main()
