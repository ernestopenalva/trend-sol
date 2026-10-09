"""Crash-order tests; SIGKILL cases run on Linux (including local WSL)."""
import json
import os
import signal
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.monitor import circuit_breaker_shadow as module
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.entry_engine import EntrySignal
from tests.test_circuit_breaker_shadow import _config
from tools.market_bot_replay import NullLogger


def make(root):
    return CircuitBreakerShadow(root, _config(), NullLogger(), None)


def initial(root, scenario='trail'):
    arm = make(root)
    if scenario == 'hs':
        for n in range(5):
            arm.on_signal(EntrySignal('SOLUSDT', 100.+n, f'2026-06-01T03:{5*n:02d}:00+00:00',
                                     1780282740000+300000*n, .1, '1m', 14))
        arm._history_store.close()
        return make(root)
    arm.on_signal(EntrySignal('SOLUSDT', 100., '2026-06-01T03:00:00+00:00',
                             1780282740000, .1, '1m', 14))
    arm.on_tick(101.5, '2026-06-01T03:00:10+00:00')
    # Seed both paths with a restored state. Existing from_state normalizes the
    # observational no_progress label DISABLED -> DISABLED_BY_CONFIG.
    arm._history_store.close()
    return make(root)


def financial(arm):
    return {'positions': [p.to_state() for p in arm.positions],
            'clock': arm.clock.to_state(), 'pending_closes': arm.pending_closes,
            'sequence': arm.sequence, 'last_input_ms': arm.last_input_ms,
            'closed': arm.closed_records, 'events': arm.audit_events,
            'buckets': arm.entries_by_bucket, 'equity': arm.equity, 'peak': arm.peak_equity}


def worker(root, stage):
    stage, scenario = stage.split(':')
    arm = make(root)
    def kill():
        os.kill(os.getpid(), signal.SIGKILL)
    def sql_trace(sql):
        if stage == 'pending_transaction' and sql.startswith('INSERT OR REPLACE INTO pending'):
            kill()
        if stage == 'history_transaction' and sql.startswith('INSERT INTO history'):
            kill()
    arm._history_store.db.set_trace_callback(sql_trace)
    original_input = arm._history_store.record_input
    def input_write(item):
        if stage == 'before_input': kill()
        original_input(item)
        if stage == 'after_input': kill()
    arm._history_store.record_input = input_write
    original_stage = arm._history_store.stage
    def history_write(*args):
        if stage == 'before_history': kill()
        result = original_stage(*args)
        if stage == 'after_history': kill()
        return result
    arm._history_store.stage = history_write
    original_atomic = module._atomic_json
    def atomic(path, content):
        if path == arm.state_path and stage == 'before_checkpoint': kill()
        original_atomic(path, content)
        if path == arm.state_path and stage == 'after_checkpoint': kill()
    module._atomic_json = atomic
    original_sync = os.fsync
    def sync(fd):
        result = original_sync(fd)
        if stage == 'checkpoint_fsync' and os.readlink(f'/proc/self/fd/{fd}') == str(arm.state_path)+'.tmp':
            kill()
        return result
    os.fsync = sync
    original_replace = os.replace
    def replace(src, dst):
        if Path(dst) == arm.state_path and stage == 'before_rename': kill()
        result = original_replace(src, dst)
        if Path(dst) == arm.state_path and stage == 'after_rename': kill()
        return result
    os.replace = replace
    original_append = module.append_projection
    def append(path, content):
        if stage == 'partial_projection' and path == arm.ledger.path:
            with path.open('ab') as output:
                output.write(content.encode()[:max(1, len(content.encode())//2)])
                output.flush(); original_sync(output.fileno())
            kill()
        return original_append(path, content)
    module.append_projection = append
    original_project = arm._project_committed
    def project():
        if stage == 'before_projection': kill()
        original_project()
        if stage == 'after_projection': kill()
    arm._project_committed = project
    arm.on_tick(97. if scenario == 'hs' else 100.9,
                '2026-06-01T03:21:20+00:00' if scenario == 'hs' else '2026-06-01T03:00:20+00:00')
    raise AssertionError('Crash hook was not reached')


class IncrementalPersistenceTests(unittest.TestCase):
    def test_journal_cannot_be_reused_as_another_arm(self):
        from src.monitor.cb_persistence import CBHistoryStore
        import shutil
        with TemporaryDirectory() as tmp:
            root = Path(tmp); arm = initial(root)
            path = arm._history_store.path; arm.close()
            other = root/'other.history.sqlite'
            shutil.copyfile(path, other)
            with self.assertRaisesRegex(ValueError, 'another arm'):
                CBHistoryStore(other, must_exist=True)
    def test_repeat_migration_preserves_current_legacy_backup(self):
        from src.monitor.cb_persistence import preserve_files
        import hashlib
        with TemporaryDirectory() as tmp:
            path = Path(tmp)/'state.json'
            module._atomic_json(path, '{"sequence":1}')
            preserve_files([path])
            module._atomic_json(path, '{"sequence":2}')
            preserve_files([path])
            self.assertEqual(path.with_name('state.json.pre-v3').read_text(), '{"sequence":1}')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(path.with_name('state.json.pre-v3.'+digest).read_bytes(), path.read_bytes())

    def test_first_input_recovery_without_any_checkpoint(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); arm = make(root)
            event = EntrySignal('SOLUSDT', 100., '2026-06-01T03:00:00+00:00',
                                1780282740000, .1, '1m', 14)
            with patch.object(module, '_atomic_json', side_effect=OSError('first checkpoint interrupted')):
                with self.assertRaises(OSError): arm.on_signal(event)
            arm._history_store.close()
            recovered = make(root)
            try:
                self.assertEqual(recovered.sequence, 1)
                self.assertEqual(len(recovered.positions), 1)
                self.assertEqual(len(recovered.audit_events), 2)
            finally:
                recovered._history_store.close()
    def test_read_only_audit_view_restores_history_without_expanding_checkpoint(self):
        from src.monitor.cb_persistence import read_cb_checkpoint
        from tools.circuit_breaker_shadow_report import _integrity_issue
        with TemporaryDirectory() as tmp:
            arm = initial(Path(tmp))
            try:
                arm.on_tick(100.9, '2026-06-01T03:00:20+00:00')
                before = arm.state_path.read_bytes()
                state = read_cb_checkpoint(arm.state_path)
                self.assertEqual(state['closed_records'], arm.closed_records)
                self.assertEqual(state['audit_events'], arm.audit_events)
                self.assertIsNone(_integrity_issue(state, state['_journal_pending']))
                self.assertEqual(before, arm.state_path.read_bytes())
            finally:
                arm._history_store.close()
    def test_rollback_export_preserves_history_pending_and_source(self):
        from tools.cb_persistence_rollback_export import export
        with TemporaryDirectory() as tmp:
            root = Path(tmp); arm = initial(root)
            arm.on_tick(100.9, '2026-06-01T03:00:20+00:00')
            source = arm.state_path.read_bytes()
            try:
                result = export(arm.state_path, root/'rollback')
                state = json.loads((root/'rollback'/arm.state_path.name).read_text())
                self.assertEqual(state['cb_schema'], 2)
                self.assertEqual(state['closed_records'], arm.closed_records)
                self.assertEqual(state['audit_events'], arm.audit_events)
                self.assertEqual(result['sequence'], arm.sequence)
                self.assertEqual(source, arm.state_path.read_bytes())
                with self.assertRaisesRegex(ValueError, 'already exist'):
                    export(arm.state_path, root/'rollback')
            finally:
                arm._history_store.close()

    def test_missing_history_never_silently_resets(self):
        with TemporaryDirectory() as tmp:
            arm = initial(Path(tmp)); path = arm._history_store.path
            arm._history_store.close(); path.unlink()
            with self.assertRaisesRegex(ValueError, 'journal missing'):
                make(Path(tmp))

    def test_history_durable_before_checkpoint_and_checkpoint_is_lean(self):
        with TemporaryDirectory() as tmp:
            arm = initial(Path(tmp))
            original = module._atomic_json
            def checkpoint(path, content):
                if path == arm.state_path:
                    state = json.loads(content)
                    self.assertNotIn('closed_records', state)
                    self.assertNotIn('audit_events', state)
                    rows = arm._history_store.db.execute(
                        'SELECT payload FROM history WHERE kind="ledger"').fetchall()
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(json.loads(rows[0][0])['exit_reason'], 'TRAILING')
                return original(path, content)
            try:
                with patch.object(module, '_atomic_json', side_effect=checkpoint):
                    arm.on_tick(100.9, '2026-06-01T03:00:20+00:00')
            finally:
                arm._history_store.close()

    def test_incremental_history_does_not_grow_on_steady_inputs(self):
        with TemporaryDirectory() as tmp:
            arm = initial(Path(tmp))
            count = arm._history_store.db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
            try:
                for i in range(11, 20):
                    arm.on_tick(101.5, f'2026-06-01T03:00:{i}+00:00')
                self.assertEqual(count, arm._history_store.db.execute('SELECT COUNT(*) FROM history').fetchone()[0])
                self.assertEqual(1, arm._history_store.db.execute('SELECT COUNT(*) FROM pending').fetchone()[0])
            finally:
                arm._history_store.close()

    def test_legacy_migration_backup_and_restore(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); arm = initial(root)
            state = json.loads(arm.state_path.read_text())
            state.update(cb_schema=2, closed_records=arm.closed_records, audit_events=arm.audit_events)
            state.pop('history_revision'); state.pop('history_counts')
            old = financial(arm); arm._history_store.close()
            module._atomic_json(arm.state_path, json.dumps(state))
            restored = make(root)
            try:
                self.assertEqual(old, financial(restored))
                self.assertEqual(json.loads(restored.state_path.with_name(restored.state_path.name+'.pre-v3').read_text()), state)
                restored.on_tick(101.5, '2026-06-01T03:00:11+00:00')
                self.assertEqual(3, json.loads(restored.state_path.read_text())['cb_schema'])
            finally:
                restored._history_store.close()

    @unittest.skipIf(os.name == 'nt', 'Actual SIGKILL requires Linux; run on local WSL')
    def test_28_sigkill_cycles_match_uninterrupted_recovery(self):
        stages = ('before_input', 'after_input', 'before_history', 'after_history',
                  'before_checkpoint', 'checkpoint_fsync', 'before_rename', 'after_rename',
                  'after_checkpoint', 'before_projection', 'partial_projection', 'after_projection',
                  'pending_transaction', 'history_transaction')
        for cycle in range(28):
            stage = stages[cycle % len(stages)]
            scenario = 'trail' if cycle < len(stages) else 'hs'
            price = 97. if scenario == 'hs' else 100.9
            moment = '2026-06-01T03:21:20+00:00' if scenario == 'hs' else '2026-06-01T03:00:20+00:00'
            boundary = '2026-06-01T03:22:00+00:00' if scenario == 'hs' else '2026-06-01T03:01:00+00:00'
            with self.subTest(cycle=cycle, stage=stage, scenario=scenario), TemporaryDirectory() as a, TemporaryDirectory() as b:
                expected = initial(Path(a), scenario)
                input_durable = stage not in ('before_input', 'pending_transaction')
                if input_durable:
                    expected.on_tick(price, moment)
                seed = initial(Path(b), scenario); seed._history_store.close()
                child = subprocess.run([sys.executable, '-m', 'tests.test_cb_incremental_persistence', b, stage+':'+scenario],
                                       capture_output=True, text=True, timeout=30)
                self.assertEqual(child.returncode, -signal.SIGKILL, child.stderr)
                recovered = make(Path(b))
                try:
                    self.assertEqual(financial(expected), financial(recovered))
                    self.assertEqual(recovered.ledger.load(), expected.ledger.load())
                    self.assertEqual(len(recovered.closed_records), len({r['pair_id'] for r in recovered.closed_records}))
                    # Roll the pending close into equity/CB and check second restart.
                    if input_durable:
                        expected.on_tick(price, boundary)
                        recovered.on_tick(price, boundary)
                        if scenario == 'hs': self.assertTrue(recovered.circuit_breaker_active)
                    recovered._history_store.close()
                    again = make(Path(b))
                    try:
                        self.assertEqual(financial(expected), financial(again))
                    finally:
                        again._history_store.close()
                finally:
                    expected._history_store.close()
                    recovered._history_store.close()


if __name__ == '__main__':
    if len(sys.argv) == 3:
        worker(Path(sys.argv[1]), sys.argv[2])
    else:
        unittest.main()
