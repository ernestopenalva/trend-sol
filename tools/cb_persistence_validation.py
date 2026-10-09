"""Offline old/new persistence benchmark and full-window runtime replay parity.

Never connects to Binance. Baseline source must be the frozen pre-patch module.
Benchmark runs actual 15-arm aggTrade dispatch on copied states, with a local
in-memory execution client for REAL_A and no network/logger/telemetry writer.
Historical parity bypasses the codec/physical IO and uses in-memory journal transactions; actual
crash durability is tested separately by test_cb_incremental_persistence.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import unittest
import weakref
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.monitor import circuit_breaker_shadow as cb
from src.monitor.cb_persistence import CBHistoryStore
from src.monitor.entry_engine import EntrySignal
from tools.cb_projection_benchmark import KEYS, factory
from tools.market_bot_replay import NullLogger, _deduplicate


def load_old(path):
    spec = importlib.util.spec_from_file_location('cb_frozen_baseline', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def baseline_patch(stack, old):
    for name in ('_load_cb_state', '_run_input', '_save_state', '_project_committed',
                 '_recover_pending_input', '_append_closed_record', '_append_audit_event',
                 '_update_closed_record', '_update_audit_event'):
        stack.enter_context(patch.object(cb.CircuitBreakerShadow, name, getattr(old.CircuitBreakerShadow, name)))


def quantiles(samples):
    ordered = sorted(samples)
    def q(p):
        index = (len(ordered)-1)*p; left = int(index)
        return ordered[left]+(ordered[min(left+1,len(ordered)-1)]-ordered[left])*(index-left)
    return {'N': len(samples), 'p50': q(.5), 'p90': q(.9), 'p99': q(.99),
            'mean': statistics.mean(samples)}


def signature(arm):
    return {'positions': [p.to_state() for p in arm.positions], 'closed': arm.closed_records,
            'events': arm.audit_events, 'clock': arm.clock.to_state(), 'sequence': arm.sequence,
            'last_input_ms': arm.last_input_ms, 'pending_closes': arm.pending_closes,
            'extra': arm._extra_state()}


def bench(args, old, version, empty=False):
    import yaml
    from src.config_profiles import effective_config
    from src.app import Monitor
    from src.monitor.position_registry import PositionRegistry
    from src.monitor.cycle_manager import CycleManager
    from src.monitor.context_shadow import RealAContextShadow
    from src.monitor.ladder_shadow import RealALadderShadow
    from src.monitor.h2_exposure_shadow import H2ExposureShadow
    from src.position.phantom_execution import PhantomExecutionClient
    from src.state_manager import StateManager
    from src.trade_ledger import TradeLedger
    cfg = effective_config(yaml.safe_load((args.snapshots/'config.yaml').read_text()))
    samples = []; per_arm = defaultdict(list); positions = {}; shared = []
    stages = defaultdict(list); current = ['startup']; active = [False]
    originals = {}
    arms = []
    with tempfile.TemporaryDirectory(prefix='trend-sol-bench-') as tmp, ExitStack() as stack:
        root = Path(tmp)
        for key in [*KEYS, 'h2_exposure_shadow', 'be_off_shadow', 'dmi15_trajectory_context_shadow']:
            for field in ('state_file', 'ledger_file', 'events_file'):
                value = cfg.get('instrumentation', {}).get(key, {}).get(field)
                if value and not (root/str(value)).resolve().is_relative_to(root.resolve()):
                    raise ValueError(f'Benchmark path escapes temporary root: {key}.{field}')
        for source in args.snapshots.glob('*.json'):
            if source.name.endswith('_later.json'): continue
            dest = root/'data/state'/source.name; dest.parent.mkdir(parents=True, exist_ok=True)
            if empty:
                state = json.loads(source.read_text())
                if isinstance(state, dict): state['positions'] = []
                else: state = []
                dest.write_text(json.dumps(state))
            else:
                shutil.copyfile(source, dest)
        if version == 'old':
            baseline_patch(stack, old)
            def legacy_state_atomic(path, value):
                path.parent.mkdir(parents=True, exist_ok=True)
                target = path.with_name(f'{path.name}.{os.getpid()}.tmp')
                with target.open('w', encoding='utf8') as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2)
                os.replace(target, path)
            stack.enter_context(patch.object(StateManager, '_atomic_json', staticmethod(legacy_state_atomic)))
            class LegacyPrettyJson:
                loads = staticmethod(json.loads)
                @staticmethod
                def dumps(value, *a, **kw):
                    kw.pop('separators', None); kw['indent'] = 2
                    return json.dumps(value, *a, **kw)
            from src.monitor import context_shadow, h2_exposure_shadow
            stack.enter_context(patch.object(context_shadow, 'json', LegacyPrettyJson))
            stack.enter_context(patch.object(h2_exposure_shadow, 'json', LegacyPrettyJson))
        for key in KEYS:
            if not cfg['instrumentation'][key].get('enabled'):
                raise ValueError(f'Expected current 15-arm snapshot; CB arm disabled: {key}')
        arms = [factory(root, cfg, key) for key in KEYS]
        client = PhantomExecutionClient()
        states = StateManager(root); cycles = CycleManager(root, cfg, NullLogger(), states)
        with patch.object(PositionRegistry, 'reconcile_with_binance', lambda self: None):
            registry = PositionRegistry(cfg, client, NullLogger(), cycles, states, TradeLedger(root), None)
        h2 = H2ExposureShadow(root, cfg, NullLogger(), None, lambda symbol: None)
        beoff = RealALadderShadow(root, cfg, NullLogger(), None, settings_key='be_off_shadow',
            strategy='BE_OFF_SHADOW', shadow_kind='BE_OFF_SHADOW', pair_prefix='beoff',
            variant='BE_OFF', cohort_started_at='2026-10-08T00:00:00+00:00')
        dmi = RealAContextShadow(root, cfg, NullLogger(), None, settings_key='dmi15_trajectory_context_shadow',
            strategy='DMI15_TRAJECTORY_CONTEXT_SHADOW', shadow_kind='DMI15_TRAJECTORY_CONTEXT_SHADOW',
            pair_prefix='dmi15ctx', predicate=lambda engine, context: True)
        all_arms = [('REAL_A', registry), ('H2_EXPOSURE_SHADOW', h2), ('BE_OFF_SHADOW', beoff),
                    ('DMI15_TRAJECTORY_CONTEXT_SHADOW', dmi)] + [(a.strategy, a) for a in arms]
        monitor = Monitor.__new__(Monitor)
        monitor.config = cfg; monitor.logger = NullLogger(); monitor.cycle_manager = cycles
        monitor.registry = registry; monitor.h2_exposure_shadow = h2
        monitor.be_off_shadow = beoff; monitor.dmi15_trajectory_context_shadow = dmi
        monitor.circuit_breaker_shadow = arms[0]; monitor.be_off_cb_shadow = arms[1]
        monitor.forward_experiment_shadows = arms[2:]
        noop = SimpleNamespace(on_tick=lambda *args, **kw: None, on_ws_event=lambda *args, **kw: None)
        for name in ('market_shadow', 'gcr_shadow', 'dmi15_shadow', 'dmi15_spread_shadow',
                     'dmi15_trajectory_shadow', 'dmi15_rsi70_shadow', 'dmi15_combined_shadow',
                     'slow_ge_context_shadow', 'be030_shadow'):
            setattr(monitor, name, noop)
        base = ((max(a.last_input_ms or 0 for a in arms)//60000)+1)*60000
        price = next(a.market_points[-1][1] for a in arms if a.market_points)
        client.set_price(price)
        Monitor._on_ws_event(monitor, 'solusdt@aggTrade', {'p': str(price), 'T': base})
        before = {a.settings_key: deepcopy(signature(a)) for a in arms}
        checkpoint_bytes = {a.strategy: a.state_path.stat().st_size for a in arms}
        for name, arm in all_arms:
            positions[name] = len(arm.positions)
            method = arm.on_tick
            def tick(*values, _name=name, _method=method, **kw):
                current[0] = _name; begin = time.perf_counter()
                result = _method(*values, **kw)
                if active[0]: per_arm[_name].append((time.perf_counter()-begin)*1000)
                current[0] = 'shared'
                return result
            stack.enter_context(patch.object(arm, 'on_tick', tick))
        # Serialization timer covers module-scoped CB dumps without modifying the
        # globally shared json package (which is used by SQLite and test output).
        class JsonProxy:
            loads = staticmethod(json.loads)
            @staticmethod
            def dumps(value, *a, **kw):
                start = time.perf_counter(); result = json.dumps(value, *a, **kw)
                if active[0]: stages[current[0]+':serialization'].append((time.perf_counter()-start)*1000)
                return result
        stack.enter_context(patch.object(cb, 'json', JsonProxy))
        if version == 'old': stack.enter_context(patch.object(old, 'json', JsonProxy))
        for arm in arms:
            if version == 'new':
                for method_name in ('record_input', 'stage'):
                    method = getattr(arm._history_store, method_name)
                    def journal(*values, _method=method, _label=method_name, **kw):
                        start = time.perf_counter(); result = _method(*values, **kw)
                        if active[0]: stages[current[0]+':journal_'+_label].append((time.perf_counter()-start)*1000)
                        return result
                    stack.enter_context(patch.object(arm._history_store, method_name, journal))
            process = arm._process_tick
            def decide(*values, _method=process, **kw):
                start = time.perf_counter(); result = _method(*values, **kw)
                if active[0]: stages[current[0]+':decision'].append((time.perf_counter()-start)*1000)
                return result
            stack.enter_context(patch.object(arm, '_process_tick', decide))
        atomic = cb._atomic_json
        def write(path, content):
            start = time.perf_counter(); result = atomic(path, content)
            if active[0]: stages[current[0]+':atomic_write_fsync_rename'].append((time.perf_counter()-start)*1000)
            return result
        stack.enter_context(patch.object(cb, '_atomic_json', write))
        if version == 'old': stack.enter_context(patch.object(old, '_atomic_json', write))
        if args.trace_markers:
            os.write(2, f'PROFILE_START {version} {empty}\n'.encode())
        start_cpu = time.process_time(); active[0] = True
        for i in range(args.inputs):
            message = json.dumps({'p': str(price), 'T': base+i+1})
            start = time.perf_counter(); decoded = json.loads(message)
            stages['shared:decode'].append((time.perf_counter()-start)*1000)
            Monitor._on_ws_event(monitor, 'solusdt@aggTrade', decoded)
            elapsed = (time.perf_counter()-start)*1000; samples.append(elapsed)
            shared.append(elapsed-sum(per_arm[name][-1] for name, arm in all_arms))
        active[0] = False
        cpu_ms = (time.process_time()-start_cpu)*1000
        if args.trace_markers:
            os.write(2, f'PROFILE_END {version} {empty}\n'.encode())
        after = {a.settings_key: signature(a) for a in arms}
        result = {'version': version, 'scenario': 'zero_positions_same_history' if empty else 'captured_positions',
                  'host': sys.platform, 'callback_ms': quantiles(samples), 'cpu_ms_per_input': cpu_ms/args.inputs,
                  'arms': {name: {'positions': positions[name], 'total_ms': quantiles(values)} for name, values in per_arm.items()},
                  'stages_ms': {name: quantiles(values) for name, values in stages.items()},
                  'shared_ms': quantiles(shared), 'checkpoint_bytes': checkpoint_bytes,
                  'limitations': 'isolated local disk; fake REAL_A fills; network, startup, telemetry writes excluded; aggTrade only'}
        for arm in arms:
            if arm._history_store: arm._history_store.close()
        return result, after


def recursive_equal(left, right, path='root'):
    if isinstance(left, (float, int)) and not isinstance(left, bool) and isinstance(right, (float, int)):
        if abs(left-right) > 1e-8: raise AssertionError((path, left, right))
    elif isinstance(left, dict):
        if left.keys() != right.keys(): raise AssertionError((path, 'keys'))
        for key in left: recursive_equal(left[key], right[key], path+'.'+str(key))
    elif isinstance(left, (list, tuple)):
        if len(left) != len(right): raise AssertionError((path, 'length', len(left), len(right)))
        for i, (a,b) in enumerate(zip(left, right)): recursive_equal(a,b,path+f'[{i}]')
    elif left != right: raise AssertionError((path, left, right))


def historical(args, old):
    manifest = json.loads((args.history/'manifest.json').read_text())
    signals = json.loads((args.history/'signals.json').read_text())
    candles = [json.loads(line) for line in (args.candles/'SOLUSDT_1m.jsonl').open()]
    output = []
    for month in ((args.month,) if getattr(args, 'month', None) else ('2026-06', '2026-08')):
        start = int(datetime.fromisoformat(month+'-01T00:00:00-03:00').timestamp()*1000)
        end_month = '2026-07' if month.endswith('06') else '2026-09'
        end = int(datetime.fromisoformat(end_month+'-01T00:00:00-03:00').timestamp()*1000)
        subset = [c for c in candles if start <= c['open_time_ms'] < end]
        grouped = {s['boundary_ms']: s['signal'] for s in signals if start <= s['boundary_ms'] < end}
        for path in ((args.path,) if getattr(args, 'path', None) else ('HIGH_FIRST', 'LOW_FIRST')):
            results = {}
            for version in ('old', 'new'):
                with tempfile.TemporaryDirectory(prefix='trend-sol-parity-') as tmp, ExitStack() as stack:
                    if version == 'old': baseline_patch(stack, old)
                    # Codec/physical IO are covered by actual benchmark/SIGKILL
                    # tests, not multiplied by every historical OHLC point.
                    # Checkpoint builders, journal revisions and decisions run.
                    class ParityCodec:
                        loads = staticmethod(json.loads)
                        @staticmethod
                        def dumps(value, *a, **kw): return value
                    stack.enter_context(patch.object(cb, 'json', ParityCodec))
                    stack.enter_context(patch.object(old, 'json', ParityCodec))
                    stack.enter_context(patch.object(cb, '_atomic_json', lambda *a: None))
                    stack.enter_context(patch.object(old, '_atomic_json', lambda *a: None))
                    stack.enter_context(patch.object(cb.CircuitBreakerShadow, '_project_committed', lambda s: None))
                    if version == 'new':
                        def journal_init(store, _path, **kw):
                            store.path = Path(_path); store.db = sqlite3.connect(':memory:')
                            store.db.executescript('CREATE TABLE history(kind TEXT,ordinal INTEGER,revision INTEGER,payload TEXT,PRIMARY KEY(kind,ordinal,revision)); CREATE TABLE pending(singleton INTEGER PRIMARY KEY,payload TEXT);')
                        stack.enter_context(patch.object(CBHistoryStore, '__init__', journal_init))
                    cfg = deepcopy(manifest['config'])
                    cfg['instrumentation']['be_off_cb_shadow'].update(enabled=True,
                        state_file='data/state/isolated.json', ledger_file='data/trades/isolated.jsonl')
                    arm = cb.CircuitBreakerShadow(Path(tmp), cfg, NullLogger(), None,
                        settings_key='be_off_cb_shadow', strategy='BE_OFF_CB_SHADOW',
                        shadow_kind='BE_OFF_CB_SHADOW', pair_prefix='beoffcb', be_off=True)
                    for i, candle in enumerate(subset):
                        stamp = candle['open_time_ms']
                        middle = (candle['high'], candle['low']) if path == 'HIGH_FIRST' else (candle['low'], candle['high'])
                        for point in _deduplicate((candle['open'], *middle, candle['close'])):
                            arm.on_tick(point, cb._iso(datetime.fromtimestamp(stamp/1000, timezone.utc)))
                        boundary = candle['close_time_ms']+1
                        if boundary in grouped:
                            event = {**grouped[boundary], 'ts': cb._iso(datetime.fromtimestamp(boundary/1000, timezone.utc))}
                            arm.on_signal(EntrySignal(**event))
                        if i % 5000 == 0:
                            print(f'{month} {path} {version}: {i}/{len(subset)}', flush=True)
                    results[version] = signature(arm)
                    if version == 'new':
                        restored = arm._history_store.restore(arm._history_revision,
                            {'ledger': len(arm.closed_records), 'events': len(arm.audit_events)})
                        recursive_equal(restored['ledger'], arm.closed_records)
                        recursive_equal(restored['events'], arm.audit_events)
                    if arm._history_store: arm._history_store.close()
            recursive_equal(results['old'], results['new'])
            output.append({'month': month, 'path': path, 'candles': len(subset), 'signals': len(grouped),
                           'closed': len(results['new']['closed']), 'sequence': results['new']['sequence'],
                           'parity': 'PASS', 'trades': results['new']['closed']})
    return {'scope': 'full-month runtime machine, OHLC modeled paths; codec and physical IO mocked, checkpoint builders and incremental history transactions run; NOT tick/execution validation',
            'tolerance': 1e-8, 'runs': output}


def historical_case(args, month, path):
    scoped = deepcopy(args); scoped.month = month; scoped.path = path
    return historical(scoped, load_old(args.baseline))


def parallel_history(args):
    cases = [(month, path) for month in ('2026-06', '2026-08') for path in ('HIGH_FIRST', 'LOW_FIRST')]
    with ProcessPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(historical_case, args, month, path) for month, path in cases]
        results = [future.result() for future in futures]
    return {'scope': results[0]['scope'], 'tolerance': 1e-8,
            'runs': [run for result in results for run in result['runs']]}


def tests(args):
    """Close test-owned SQLite handles before TemporaryDirectory cleanup.

    Legacy tests predate closeable journal resources. This is test-runner-only,
    not a production change, and does not close anything inside the tested input.
    """
    stores = weakref.WeakSet()
    original_init = CBHistoryStore.__init__
    original_remove = tempfile.TemporaryDirectory._rmtree
    def initialize(store, *values, **kw):
        original_init(store, *values, **kw); stores.add(store)
    def remove(path, *values, **kw):
        target = Path(path).resolve()
        for store in list(stores):
            if store.path.resolve().is_relative_to(target): store.close()
        return original_remove(path, *values, **kw)
    with patch.object(CBHistoryStore, '__init__', initialize), \
         patch.object(tempfile.TemporaryDirectory, '_rmtree', staticmethod(remove)):
        suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'), pattern=args.test_pattern)
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return {'run': result.testsRun, 'success': result.wasSuccessful(),
            'failures': [{'test': str(test), 'trace': trace} for test, trace in result.failures],
            'errors': [{'test': str(test), 'trace': trace} for test, trace in result.errors],
            'skipped': [{'test': str(test), 'reason': reason} for test, reason in result.skipped]}


def trace_summary(args):
    windows = []; current = None
    for line in args.trace.open():
        start = re.search(r'PROFILE_START (old|new) (True|False)', line)
        if start:
            current = {'version': start[1], 'empty': start[2]=='True', 'sync_ms': [], 'write_ms': [], 'bytes': 0}
            continue
        if 'PROFILE_END' in line and current is not None:
            current.update(syncs_per_input=len(current['sync_ms'])/args.inputs,
                sync_ms_per_input=sum(current['sync_ms'])/args.inputs,
                sync_call_ms=quantiles(current['sync_ms']),
                write_ms_per_input=sum(current['write_ms'])/args.inputs,
                write_call_ms=quantiles(current['write_ms']), bytes_per_input=current['bytes']/args.inputs)
            current.pop('sync_ms'); current.pop('write_ms'); windows.append(current); current = None
            continue
        if current is None: continue
        elapsed = re.search(r'<([0-9.]+)>$', line.strip())
        if not elapsed: continue
        ms = float(elapsed[1])*1000
        if 'fsync(' in line or 'fdatasync(' in line: current['sync_ms'].append(ms)
        elif 'write(' in line or 'pwrite64(' in line or 'writev(' in line:
            current['write_ms'].append(ms)
            size = re.search(r'\)\s+=\s+(\d+)', line)
            if size: current['bytes'] += int(size[1])
    if len(windows) != 4: raise ValueError('Incomplete strace measurement windows')
    return {'inputs_per_window': args.inputs, 'runs': windows,
            'limits': 'strace has overhead; syscall wall includes CPU and waiting; windows exclude startup/migration/teardown'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('benchmark', 'parity', 'tests', 'trace-summary'), required=True)
    parser.add_argument('--trace', type=Path, default=ROOT/'data/analysis/persistence_v3/fsync_local.strace')
    parser.add_argument('--test-pattern', default='test*.py')
    parser.add_argument('--baseline', type=Path, default=ROOT/'data/analysis/persistence_v3/old_circuit_breaker_shadow.py')
    parser.add_argument('--snapshots', type=Path, default=ROOT/'data/analysis/callback_cost_profile_20261008/raw')
    parser.add_argument('--history', type=Path, default=ROOT/'data/studies/be_off_cb_defensive_closure/20261002')
    parser.add_argument('--candles', type=Path, default=ROOT/'data/studies/be_off_cb_deterioration/klines')
    parser.add_argument('--inputs', type=int, default=30)
    parser.add_argument('--trace-markers', action='store_true', help='Two offline benchmark markers per measured window')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists(): raise SystemExit('Choose a new output filename')
    old = load_old(args.baseline) if args.mode in ('benchmark', 'parity') else None
    if args.mode == 'trace-summary':
        result = trace_summary(args)
    elif args.mode == 'tests':
        result = tests(args)
    elif args.mode == 'benchmark':
        rows = []
        for empty in (False, True):
            ends = {}
            for version in ('old', 'new'):
                row, ends[version] = bench(args, old, version, empty)
                rows.append(row); print(json.dumps({'version': version, 'empty': empty, 'callback_ms': row['callback_ms']}), flush=True)
            recursive_equal(ends['old'], ends['new'])
        result = {'runs': rows, 'end_state_parity': 'PASS'}
    else:
        result = parallel_history(args)
    if old is not None: result['baseline_sha256'] = hashlib.sha256(args.baseline.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf8')
    if args.mode == 'tests' and not result['success']: raise SystemExit(1)


if __name__ == '__main__': main()
