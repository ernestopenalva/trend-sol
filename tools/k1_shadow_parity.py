"""Independent two-path K1 reference/runtime parity gate. No clients or live state."""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
STUDY = ROOT / 'data/analysis/marola_20261030'
OUT = ROOT / 'data/analysis/ema_macd_cluster_act10_k1'
REFERENCE_SCRIPT = ROOT / 'data/analysis/ema_macd_cluster_act10_k1_reference_switch_run.py'
V2_LABELS = ROOT / 'data/analysis/keyed_20261101/reference/rotulos_continuos_v2.json'
sys.path.insert(0, str(ROOT))


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reference(path):
    source = REFERENCE_SCRIPT.read_text(encoding='utf8')
    source = source.replace("ROOT = Path(__file__).resolve().parents[1]", f"ROOT = Path({str(ROOT)!r})")
    source = source.replace("U = Path('/home/claude/w/marola_20261030'); OUT = Path('/home/claude/w/runs3')",
                            f"U = Path({str(STUDY)!r}); OUT = Path({str(OUT / 'reference_runs')!r})")
    source = source.replace("Path('/home/claude/w/out/rotulos_continuos.json')",
                            f"Path({str(ROOT / 'data/analysis/keyed_20261101/reference/rotulos_continuos.json')!r})")
    source = source.replace("Path('/home/claude/w/out/rotulos_continuos_v2.json')", f"Path({str(V2_LABELS)!r})")
    argv = ['switch_run.py', 'CONST:C04+ACT10', path, 'VIVO', 'NOVO', 'SPC:1']
    namespace = {'__name__': '__main__', '__file__': str(REFERENCE_SCRIPT)}
    old_argv = sys.argv
    try:
        sys.argv = argv
        exec(compile(source, str(REFERENCE_SCRIPT), 'exec'), namespace)
    finally:
        sys.argv = old_argv
    result = namespace['r']
    save(OUT / 'reference_runs' / f'{path}.json', result)
    save(OUT / f'reference_{path}_provenance.json', {
        'reference_script_sha256': sha(REFERENCE_SCRIPT),
        'adapted_script_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'adaptations': 'local paths only; SPC:1, VIVO and NOVO retained',
        'market_manifest_sha256': sha(STUDY / 'market_manifest.json'),
        'v2_labels_sha256': sha(V2_LABELS),
        'switch_run_block_count': getattr(namespace.get('LAST'), 'spc_blocks', 0),
    })
    return namespace


def candidate(path):
    # Import runtime source before replay12_engine inserts the historical frozen tree.
    from src.monitor.keyed_shadow import EmaMacdClusterAct10K1Shadow
    from src.monitor.forward_experiment_shadows import FastDropEmaShadow
    import yaml
    import replay12_engine as engine

    labels = read(ROOT / 'data/analysis/keyed_20261101/labels_verified.json')
    config = read(STUDY / 'config.json')
    current = yaml.safe_load((ROOT / 'config/config.yaml').read_text(encoding='utf8'))
    config['instrumentation'].update(current['instrumentation'])
    original_make = engine.make
    instances = []

    class K1Runtime(EmaMacdClusterAct10K1Shadow, engine.Core):
        _close_and_record = engine.Risk._close_and_record

        def _open(self, signal, bucket):
            details = EmaMacdClusterAct10K1Shadow._k1_block_details(self, signal)
            if details is not None:
                self.k1_block_count += 1
                self.k1_blocks.append({'at': self.now, 'source': signal.source_candle_open_time,
                                       **details})
                self._block('ENTRY_BLOCKED_SPACING_QUEDA', signal, bucket, **details)
                return False
            engine.Memory._open(self, signal, bucket)
            return True

        def _process_signal(self, signal):
            before = self.k1_block_count
            result = EmaMacdClusterAct10K1Shadow._process_signal(self, signal)
            return False if self.k1_block_count > before else result

    def make(_arm, cfg, bps, start):
        value = original_make('EMA_MACD_HIST_1M_SHADOW', cfg, bps, start)
        value.__class__ = K1Runtime
        key = 'ema_macd_cluster_act10_k1_shadow'
        value.settings = dict(cfg['instrumentation'][key])
        value.arm = value.strategy = value.shadow_kind = 'EMA_MACD_CLUSTER_ACT10_K1_SHADOW'
        value.pair_prefix = 'ema_macd_cluster_act10_k1'
        value.experiment = 'HS_BEAR_CLUSTER_EXIT'
        value.policy = 'EMA_MACD'
        value.k1_block_count = 0
        value.k1_blocks = []
        instances.append(value)
        return value

    engine.make = make
    run_source = inspect.getsource(engine.run)
    segment_source = inspect.getsource(engine.segment)
    run_source = run_source.replace("arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW'", 'isinstance(o,FastDropEmaShadow)')
    segment_source = segment_source.replace("o.arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW'", 'isinstance(o,FastDropEmaShadow)')
    exec(compile(run_source, '<k1-candidate-run>', 'exec'), engine.__dict__)
    exec(compile(segment_source, '<k1-candidate-segment>', 'exec'), engine.__dict__)
    engine.FastDropEmaShadow = FastDropEmaShadow
    manifest = read(STUDY / 'market_manifest.json')
    bars = [engine.Bar(json.loads(line)) for line in (STUDY / 'market/1m.jsonl').open(encoding='utf8')]
    result = engine.run('EMA_MACD_CLUSTER_ACT10_K1_SHADOW', config, bars,
        read(STUDY / 'signals.json')['REAL_A'], read(STUDY / 'contexts_runtime_projection.json'),
        read(STUDY / 'regimes.json'), 'BASE', path, manifest['start_ms'], manifest['end_ms'])
    instance = instances[-1] if instances else None
    result['k1_block_count'] = int(getattr(instance, 'k1_block_count', 0))
    result['k1_blocks'] = getattr(instance, 'k1_blocks', [])
    save(OUT / 'candidate_runs' / f'{path}.json', result)
    save(OUT / f'candidate_{path}_provenance.json', {
        'runtime_source_sha256': sha(ROOT / 'src/monitor/keyed_shadow.py'),
        'context_gate_source_sha256': sha(ROOT / 'src/monitor/context_shadow.py'),
        'engine_source_sha256': sha(ROOT / 'tools/replay12_engine.py'),
        'config_sha256': sha(ROOT / 'config/config.yaml'),
        'adapter': 'current K1 gate + frozen replay engine / in-memory fills',
        'labels_verified_sha256': sha(ROOT / 'data/analysis/keyed_20261101/labels_verified.json'),
        'frozen_market_manifest_sha256': sha(STUDY / 'market_manifest.json'),
    })
    return result


def compare(path):
    reference_run = read(OUT / 'reference_runs' / f'{path}.json')
    candidate_run = read(OUT / 'candidate_runs' / f'{path}.json')
    reference_provenance = read(OUT / f'reference_{path}_provenance.json')
    candidate_rows = candidate_run['records']
    reference_rows = reference_run['records']
    clean = lambda rows, ignored=(): [{key: value for key, value in row.items()
                                       if key != 'pair_id' and key not in ignored} for row in rows]
    crows, rrows = clean(candidate_rows), clean(reference_rows)
    economic_candidate, economic_reference = clean(candidate_rows, ('fast_evaluated',)), clean(reference_rows, ('fast_evaluated',))
    fast_audit_mismatches = sum(left.get('fast_evaluated') != right.get('fast_evaluated')
                                for left, right in zip(candidate_rows, reference_rows))
    trade_difference = None
    for index, (candidate_row, reference_row) in enumerate(zip(crows, rrows)):
        if candidate_row != reference_row:
            keys = set(candidate_row) | set(reference_row)
            trade_difference = {'trade_index': index, 'source': candidate_row.get('source'),
                'fields': {key: [candidate_row.get(key), reference_row.get(key)]
                           for key in sorted(keys) if candidate_row.get(key) != reference_row.get(key)}}
            break
    if trade_difference is None and len(crows) != len(rrows):
        trade_difference = {'candidate_trades': len(crows), 'reference_trades': len(rrows)}
    expected = {'LOW_FIRST': (942, -31.597134, 186),
                'HIGH_FIRST': (943, -38.111394, 184)}[path]
    reference_blocks = int(reference_provenance['switch_run_block_count'])
    comparison = {
        'path': path,
        'passed': (len(economic_candidate) == len(economic_reference)
                   and economic_candidate == economic_reference and len(crows) == expected[0]
                   and abs(float(candidate_run['stats']['net_total']) - expected[1]) <= 0.0000005
                   and abs(float(reference_run['stats']['net_total']) - expected[1]) <= 0.0000005
                   and int(candidate_run['k1_block_count']) == expected[2]
                   and reference_blocks == expected[2]
                   and candidate_run['stats'] == reference_run['stats']
                   and candidate_run['admissions'] == reference_run['admissions']),
        'expected': {'trades': expected[0], 'net_total': expected[1], 'k1_blocks': expected[2]},
        'candidate': {'trades': len(crows), 'net_total': candidate_run['stats']['net_total'],
                      'k1_blocks': candidate_run['k1_block_count']},
        'reference': {'trades': len(rrows), 'net_total': reference_run['stats']['net_total'],
                      'k1_blocks': reference_blocks},
        'all_economic_trade_fields_equal_except_pair_id': economic_candidate == economic_reference,
        'reference_only_fast_evaluated_audit_differences': fast_audit_mismatches,
        'stats_equal': candidate_run['stats'] == reference_run['stats'],
        'admissions_equal': candidate_run['admissions'] == reference_run['admissions'],
        'first_trade_divergence': trade_difference,
        'candidate_sha256': sha(OUT / 'candidate_runs' / f'{path}.json'),
        'reference_sha256': sha(OUT / 'reference_runs' / f'{path}.json'),
    }
    save(OUT / f'PARITY_{path}.json', comparison)
    return comparison


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if len(sys.argv) == 3 and sys.argv[1] in ('reference', 'candidate'):
        path = sys.argv[2]
        if path not in ('LOW_FIRST', 'HIGH_FIRST'):
            raise SystemExit('invalid path')
        if sys.argv[1] == 'reference':
            reference(path)
        else:
            candidate(path)
        return 0
    results = []
    for path in ('LOW_FIRST', 'HIGH_FIRST'):
        for stage in ('reference', 'candidate'):
            print(stage.upper(), path, flush=True)
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), stage, path],
                cwd=ROOT, check=False)
            if completed.returncode:
                raise SystemExit(completed.returncode)
        proof = compare(path)
        results.append(proof)
        print(json.dumps(proof, ensure_ascii=False), flush=True)
        if not proof['passed']:
            break
    result = {'passed': len(results) == 2 and all(item['passed'] for item in results),
              'cases_expected': 2, 'cases_completed': len(results), 'cases': results,
              'deployment_performed': False, 'restart_performed': False,
              'git_commands_performed': False,
              'reference_script_sha256': sha(REFERENCE_SCRIPT),
              'runtime_source_sha256': sha(ROOT / 'src/monitor/keyed_shadow.py'),
              'context_gate_source_sha256': sha(ROOT / 'src/monitor/context_shadow.py')}
    save(OUT / 'PARIDADE_COMPLETA.json', result)
    print('PARITY_SUITE', 'PASS' if result['passed'] else 'DIVERGENCE', flush=True)
    return 0 if result['passed'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
