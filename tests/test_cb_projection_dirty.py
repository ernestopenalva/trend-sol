"""Projection optimization must not change durable commits or recovery."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.logging_utils import JsonlLogger
from src.monitor import circuit_breaker_shadow as module
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.entry_engine import EntrySignal
from tests.test_circuit_breaker_shadow import _config
from tests import test_cb_forward_equivalence as equivalence


class ProjectionDirtyTests(unittest.TestCase):
    def make(self, root):
        cfg = _config()
        return CircuitBreakerShadow(root, cfg, JsonlLogger(root, cfg), None)

    def signal(self):
        return EntrySignal('SOLUSDT', 100., '2026-09-05T00:00:00+00:00',
                           1788566340000, .1, '1m', 14)

    def test_steady_tick_skips_projection_json_but_commits_pending_and_checkpoint(self):
        with TemporaryDirectory() as tmp:
            s = self.make(Path(tmp))
            s.on_tick(100., '2026-09-05T00:00:00+00:00')
            original = module.json.dumps
            def dump(value, *args, **kwargs):
                self.assertFalse(any(value is row for row in s.audit_events + s.closed_records))
                return original(value, *args, **kwargs)
            with patch.object(module.json, 'dumps', side_effect=dump), \
                 patch.object(module, '_atomic_json', wraps=module._atomic_json) as write:
                s.on_tick(100., '2026-09-05T00:00:01+00:00')
            self.assertEqual([x.args[0] for x in write.call_args_list],
                             [s.pending_input_path, s.state_path])
            self.assertEqual(s.sequence, 2)
            self.assertEqual(s.last_input_ms, 1788566401000)
            self.assertEqual(s._projection_dirty, {'ledger': False, 'events': False})

    def test_append_and_in_place_update_mark_only_corresponding_projection(self):
        with TemporaryDirectory() as tmp:
            s = self.make(Path(tmp)); s._project_committed()
            record = {'status': 'before', 'nested': {'value': 1}}
            s._append_closed_record(record)
            self.assertEqual(s._projection_dirty, {'ledger': True, 'events': False})
            s._update_closed_record(record, {'status': 'after'})
            self.assertIs(s.closed_records[0], record)
            with patch.object(module, '_atomic_json', wraps=module._atomic_json) as write:
                s._project_committed()
            self.assertEqual([x.args[0] for x in write.call_args_list], [s.ledger.path])
            self.assertEqual(json.loads(s.ledger.path.read_text()), record)
            event = {'event': 'before'}
            s._append_audit_event(event); s._update_audit_event(event, {'event': 'after'})
            self.assertEqual(s._projection_dirty, {'ledger': False, 'events': True})
            s._project_committed()
            self.assertEqual(json.loads(s.audit_path.read_text()), event)

    def test_close_marks_ledger_and_events_once_per_commit(self):
        with TemporaryDirectory() as tmp:
            s = self.make(Path(tmp)); s.on_signal(self.signal())
            with patch.object(module, '_atomic_json', wraps=module._atomic_json) as write:
                s.on_tick(98., '2026-09-05T00:00:10+00:00')
            paths = [x.args[0] for x in write.call_args_list]
            self.assertEqual(paths.count(s.ledger.path), 1)
            # Ordinary HS does not append a separate CB audit event today.
            self.assertEqual(paths.count(s.audit_path), 0)
            self.assertEqual(s.ledger.load(), s.closed_records)
            self.assertEqual(s.closed_records[0]['exit_reason'], 'HARD_STOP')

    def test_partial_failure_keeps_failed_projection_dirty(self):
        with TemporaryDirectory() as tmp:
            s = self.make(Path(tmp)); s._project_committed()
            s._append_closed_record({'trade': 1}); s._append_audit_event({'event': 'CB'})
            original = module._atomic_json
            def fail(path, content):
                if path == s.audit_path:
                    raise OSError('projection failure')
                return original(path, content)
            with patch.object(module, '_atomic_json', side_effect=fail):
                with self.assertRaises(OSError): s._project_committed()
            self.assertEqual(s._projection_dirty, {'ledger': False, 'events': True})
            s._project_committed()
            self.assertEqual(s._projection_dirty, {'ledger': False, 'events': False})

    def test_existing_projection_divergence_still_fails_and_remains_dirty(self):
        with TemporaryDirectory() as tmp:
            s = self.make(Path(tmp)); s._append_audit_event({'event': 'before'})
            s._project_committed()
            s._update_audit_event(s.audit_events[0], {'event': 'after'})
            with self.assertRaisesRegex(ValueError, 'diverges'): s._project_committed()
            self.assertTrue(s._projection_dirty['events'])

    def test_partial_mutation_exception_does_not_leave_projection_clean(self):
        class PartialUpdate(dict):
            def update(self, fields):
                self['partial'] = True
                raise ValueError('partial update')
        with TemporaryDirectory() as tmp:
            s = self.make(Path(tmp)); s._project_committed()
            record = PartialUpdate()
            with self.assertRaisesRegex(ValueError, 'partial update'):
                s._update_closed_record(record, {})
            self.assertTrue(s._projection_dirty['ledger'])

    def test_restart_repairs_projection_even_with_no_new_mutations(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); s = self.make(root); s.on_signal(self.signal())
            s.on_tick(98., '2026-09-05T00:00:10+00:00')
            expected = s.ledger.path.read_bytes(); s.ledger.path.unlink()
            restored = self.make(root)
            self.assertEqual(restored.ledger.path.read_bytes(), expected)
            self.assertEqual(restored.sequence, s.sequence)
            self.assertEqual(restored.last_input_ms, s.last_input_ms)
            self.assertFalse(any(restored._projection_dirty.values()))

    def test_update_committed_before_failed_projection_is_repaired_at_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); s = self.make(root); s.on_signal(self.signal())
            process = s._process_tick
            def close_and_update(*args):
                process(*args)
                s._update_closed_record(s.closed_records[0], {'audit_annotation': 'updated'})
            with patch.object(s, '_process_tick', side_effect=close_and_update), \
                 patch.object(s, '_project_committed', side_effect=OSError('after checkpoint')):
                with self.assertRaises(OSError): s.on_tick(98., '2026-09-05T00:00:10+00:00')
            self.assertTrue(s._projection_dirty['ledger'])
            restored = self.make(root)
            self.assertEqual(restored.ledger.load()[0]['audit_annotation'], 'updated')
            self.assertEqual(len(restored.ledger.load()), 1)

    def test_dirty_is_arm_local(self):
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            left, right = self.make(Path(a)), self.make(Path(b))
            left._project_committed(); right._project_committed()
            left._append_audit_event({'event': 'CB'})
            self.assertTrue(left._projection_dirty['events'])
            self.assertFalse(right._projection_dirty['events'])

    def test_two_events_inside_input_project_once_after_checkpoint(self):
        with TemporaryDirectory() as tmp:
            s = self.make(Path(tmp)); s.on_tick(100., '2026-09-05T00:00:00+00:00')
            process = s._process_tick
            def two_events(*args):
                process(*args)
                s._event('TEST_A'); s._event('TEST_B')
                s._save_state()  # Still suppressed inside the transaction.
            with patch.object(s, '_process_tick', side_effect=two_events), \
                 patch.object(module, '_atomic_json', wraps=module._atomic_json) as write:
                s.on_tick(100., '2026-09-05T00:00:01+00:00')
            paths = [x.args[0] for x in write.call_args_list]
            self.assertEqual(paths, [s.pending_input_path, s.state_path, s.audit_path])
            self.assertEqual([json.loads(x)['event'] for x in s.audit_path.read_text().splitlines()][-2:],
                             ['TEST_A', 'TEST_B'])

    def test_noop_pending_recovery_preserves_watermark_and_projection(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); s = self.make(root)
            s.on_tick(100., '2026-09-05T00:00:00+00:00')
            expected = s.audit_path.read_bytes(); original = module._atomic_json
            def fail(path, content):
                if path == s.state_path: raise OSError('before checkpoint')
                return original(path, content)
            with patch.object(module, '_atomic_json', side_effect=fail):
                with self.assertRaises(OSError): s.on_tick(100., '2026-09-05T00:00:01+00:00')
            restored = self.make(root)
            self.assertEqual(restored.sequence, 2)
            self.assertEqual(restored.last_input_ms, 1788566401000)
            self.assertEqual(restored.audit_path.read_bytes(), expected)
            again = self.make(root)
            self.assertEqual(again.sequence, restored.sequence)
            self.assertEqual(again.closed_records, restored.closed_records)

    def test_cb_trigger_marks_events_not_ledger(self):
        from datetime import datetime, timedelta, timezone
        with TemporaryDirectory() as tmp:
            s = self.make(Path(tmp)); s._project_committed()
            now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
            s.clock.equity, s.clock.peak = 98.4, 100.
            s.clock.history = [[int((now-timedelta(hours=h)).timestamp()*1000), -.3] for h in (2, 1)]
            s._advance_clock(now, 100.)
            self.assertTrue(s.circuit_breaker_active)
            self.assertEqual(s.audit_events[-1]['event'], 'CIRCUIT_BREAKER_TRIGGERED')
            self.assertEqual(s._projection_dirty, {'ledger': False, 'events': False})
            self.assertEqual(json.loads(s.audit_path.read_text().splitlines()[-1]), s.audit_events[-1])

    def test_fixed_sequence_parity_against_always_serialized_projections(self):
        original = CircuitBreakerShadow._project_committed
        def old_cost(s):
            s._projection_dirty.update(ledger=True, events=True)
            return original(s)
        fixture = equivalence.ForwardEquivalenceTests()
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            with patch.object(CircuitBreakerShadow, '_project_committed', old_cost):
                baseline = fixture._integration(Path(a), True)
            optimized = fixture._integration(Path(b), True)
            self.assertEqual(baseline, optimized)
            def files(root):
                result = {}
                for path in root.rglob('*'):
                    if path.is_file() and ('state' in path.parts or 'trades' in path.parts or path.name == 'circuit_breaker_shadow_events.jsonl'):
                        rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
                        for row in rows: row.pop('updated_at', None)
                        result[str(path.relative_to(root))] = rows
                return result
            self.assertEqual(files(Path(a)), files(Path(b)))


if __name__ == '__main__': unittest.main()
