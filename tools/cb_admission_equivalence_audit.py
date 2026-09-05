"""Offline 1m/5m admission comparison; no production modules are modified."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.monitor.position_registry import PositionRegistry
from tools import ge_replay_study as engine
from tools.circuit_breaker_hypothesis_audit import (
    WINDOWS, CheckedGuard, frozen_config, raw_cache_check, episodes,
)
from tools.real_a_circuit_breaker_replay import _signals, _metrics, _ts


class AdmissionCounter:
    def __init__(self, config):
        self.context = SimpleNamespace(config=config)
        self.counts = Counter()

    def bucket(self, source):
        # Exactly the runtime's source-candle bucketing, not evaluation time.
        return PositionRegistry._admission_bucket(self.context, source)

    def count(self, source):
        return self.counts[self.bucket(source)]

    def record(self, source):
        self.counts[self.bucket(source)] += 1


def isolated_runner():
    """Compile a private copy with only the admission counter substituted.

    The original on-disk function and module globals are never changed.
    Replacement guards stop execution if the audited source drifts.
    """
    source = inspect.getsource(engine.run_universe)
    replacements = (
        ('    sequence = 0\n', '    sequence = 0\n    counter = AdmissionCounter(config)\n'),
        ('        admitted = 0\n', ''),
        ('            if admitted >= max_per_candle:\n',
         '            if counter.count(event.signal.source_candle_open_time) >= max_per_candle:\n'),
        ('            admitted += 1\n',
         '            counter.record(event.signal.source_candle_open_time)\n'),
    )
    for before, after in replacements:
        assert source.count(before) == 1, ('Replay source changed', before)
        source = source.replace(before, after)
    namespace = dict(vars(engine), AdmissionCounter=AdmissionCounter)
    exec(compile(source, '<isolated-admission-only-replay>', 'exec'), namespace)
    return namespace['run_universe']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache-dir', default='data/analysis/cb_audit_input_20260905/data/studies/real_a_circuit_breaker/klines')
    parser.add_argument('--reference', default='data/analysis/cb_hypothesis_audit_result.json')
    parser.add_argument('--output', default='data/analysis/cb_admission_equivalence_result.json')
    args = parser.parse_args()
    dest = Path(args.output)
    if dest.exists():
        raise SystemExit('Output already exists; choose a new audit filename.')
    config, hashes = frozen_config()
    reference = json.loads(Path(args.reference).read_text(encoding='utf-8'))
    cache = Path(args.cache_dir)
    manifest = {interval: raw_cache_check(cache / f'SOLUSDT_{interval}.jsonl', minutes * 60_000)
                for interval, minutes in [('1m', 1), ('5m', 5), ('15m', 15)]}
    first = int(_ts(WINDOWS[0][1]).timestamp()*1000) - engine.WARMUP_CANDLES*15*60_000
    last = int(_ts(WINDOWS[-1][2]).timestamp()*1000)-1
    candles = {i:engine.load_ge_market_data(None, 'SOLUSDT', i, first, last, cache, True)
               for i in ('1m','5m','15m')}
    runner = isolated_runner()
    out = {'source_hashes':hashes, 'cache_manifest':manifest, 'runs':{},
           'only_change':'Admission uses runtime source-candle buckets; 1m or 5m.',
           'oos_end_note':'20:30:00 BRT, same as prior audit; original console omitted seconds.'}
    for label, start, end, path in WINDOWS:
        start_ms = int(_ts(start).timestamp()*1000)
        end_ms = int(_ts(end).timestamp()*1000)-1
        signals = _signals(config, candles, start_ms, end_ms)
        signal_sources = {x.boundary_ms:x.signal.source_candle_open_time for x in signals}
        assert len(signal_sources) == len(signals)
        assert all(x.signal.source_candle_open_time+60_000 <= x.boundary_ms for x in signals)
        case = {'window':[start,end], 'path':path, 'signals':len(signals), 'arms':{}}
        for cadence in ('1m','5m'):
            cfg = deepcopy(config)
            cfg['entry']['admission_candle_interval'] = cadence
            for arm in ('CONTROL','COMBO'):
                print(f'{label} | {cadence} | {arm}: running', flush=True)
                guard = CheckedGuard(signals) if arm == 'COMBO' else None
                result = runner(name=arm, lookback=0, config=cfg, signals=signals,
                    execution_candles=candles['1m'], start_ms=start_ms, end_ms=end_ms,
                    intrabar_path=path, round_trip_spread_bps=5.,
                    admission_guard=guard.allows if guard else None)
                buckets = AdmissionCounter(cfg)
                for timestamp, price in result.entry_times:
                    buckets.record(signal_sources[timestamp])
                assert max(buckets.counts.values(), default=0) <= 1
                metrics = _metrics(result, 100, 20)
                metrics.update(open_end=len(result.open_positions),
                    entries=len(result.entry_times), blocked_admission=result.blocked_candle_limit,
                    blocked_capacity=result.blocked_slots, blocked_spacing=result.blocked_spacing,
                    blocked_circuit=result.blocked_circuit,
                    crises=guard.original.crises if guard else 0,
                    pause_hours=guard.original.paused_minutes/60 if guard else 0)
                closes = [asdict(x) for x in result.trades]
                if cadence == '1m':
                    old = reference['runs'][label]
                    key = 'control' if arm == 'CONTROL' else 'combo'
                    assert closes == old[f'closes_{key}'], '1m trade regression'
                    assert [list(x) for x in result.entry_times] == old[f'entries_{key}']
                    for field, value in old['metrics'][0 if arm == 'CONTROL' else 1].items():
                        if field in metrics:
                            assert metrics[field] == value, (field,metrics[field],value)
                    if guard:
                        assert episodes(guard.trace) == old['events']
                case['arms'][f'{arm}_{cadence}'] = {'metrics':metrics, 'closes':closes,
                    'entries':result.entry_times, 'events':episodes(guard.trace) if guard else None,
                    'exact_detector_equality':True if guard else None,
                    'admission_bucket_verified':True}
                print(json.dumps(metrics), flush=True)
        out['runs'][label] = case
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open('x', encoding='utf-8') as stream:
        json.dump(out, stream, indent=2)
    print('COMPLETE:', dest, flush=True)


if __name__ == '__main__':
    main()
