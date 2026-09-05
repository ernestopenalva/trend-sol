"""Offline audit of the historical CB replay; never imports the forward shadow.

Only writes a new JSON audit artifact. Uses the frozen historical config from Git.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from src.config_profiles import effective_config
from tools.real_a_circuit_breaker_replay import CircuitGuard, _rules, _signals, _metrics, _ts
from tools.ge_replay_study import WARMUP_CANDLES, load_ge_market_data, run_universe
from tools.market_bot_replay import MINUTE_MS

REVISION = '0d3f9dc'
SOURCE_FILES = (
    'tools/real_a_circuit_breaker_replay.py', 'tools/ge_replay_study.py',
    'tools/market_bot_replay.py', 'src/monitor/entry_engine.py',
    'src/position/bot_full_engine.py', 'src/position/position_base.py',
    'src/indicators/indicators.py', 'src/config_profiles.py',
)
WINDOWS = (
    ('IS_HIGH', '2026-08-14T13:45:00-03:00', '2026-09-01T00:00:00-03:00', 'HIGH_FIRST'),
    ('OOS_HIGH', '2026-09-01T00:00:00-03:00', '2026-09-03T20:30:00-03:00', 'HIGH_FIRST'),
    ('OOS_LOW', '2026-09-01T00:00:00-03:00', '2026-09-03T20:30:00-03:00', 'LOW_FIRST'),
)
EXPECTED = {
    'IS_HIGH': [(-6.729, 9.794, .632, 317, 1), (-5.480, 8.545, .631, 268, 1, 9, 54., 102)],
    'OOS_HIGH': [(-3.394, 3.883, .180, 71, 1), (-2.869, 3.198, .168, 60, 1, 2, 12., 29)],
    'OOS_LOW': [(-3.227, 3.883, .220, 71, 1), (-2.707, 3.198, .215, 60, 1, 2, 12., 29)],
}


class IndependentDetector:
    """Separate state machine: online closes, rolling deque, rising-edge timer.

    Does not call CircuitGuard or its helpers. Float accumulation order matches
    the ledger's order deliberately; alternate ordering is audited separately.
    """
    def __init__(self):
        self.balance = self.maximum = 100.0
        self.max_dd = 0.0
        self.window = deque()
        self.condition = False
        self.deadline = -1
        self.count = self.paused_minutes = 0

    def minute(self, time_ms, new_closes):
        for trade in new_closes:
            money = 20.0 * trade.net_pct / 100
            self.balance += money
            self.maximum = max(self.maximum, self.balance)
            self.max_dd = max(self.max_dd, self.maximum - self.balance)
            self.window.append((trade.closed_ms, money))
        while self.window and self.window[0][0] <= time_ms - 14_400_000:
            self.window.popleft()
        loss_window = sum(money for _, money in self.window) / 100.0 * 100
        dd = (self.maximum - self.balance) / 100.0 * 100
        active_condition = dd >= 1.5 and loss_window <= -.5 and len(self.window) >= 2
        if active_condition and not self.condition:
            self.deadline = max(self.deadline, time_ms + 21_600_000)
            self.count += 1
        self.condition = active_condition
        allowed = time_ms >= self.deadline
        self.paused_minutes += int(not allowed)
        return allowed


class CheckedGuard:
    def __init__(self, signals):
        self.original = CircuitGuard(_rules()[2], 6, 100, 20)
        self.independent = IndependentDetector()
        self.signal_counts = Counter(x.boundary_ms for x in signals)
        self.trace = []
        self.closes = []
        self.seen = set()
        self.cursor = 0

    def allows(self, boundary, result):
        new = result.trades[self.cursor:]
        for trade in new:
            key = trade.opened_ms
            if key in self.seen:
                raise AssertionError(f'Duplicate closing/entry timestamp: {key}')
            if not trade.opened_ms < trade.closed_ms <= boundary:
                raise AssertionError(f'Future/invalid close at {boundary}: {trade}')
            if self.closes and trade.closed_ms < self.closes[-1].closed_ms:
                raise AssertionError('Non-chronological closing stream')
            self.seen.add(key)
            self.closes.append(trade)
        expected = self.original.allows(boundary, result)
        actual = self.independent.minute(boundary, new)
        a, b = self.original, self.independent
        if (expected, a.crises, a.pause_until, a.equity, a.peak, a.was_true) != (
            actual, b.count, b.deadline, b.balance, b.maximum, b.condition
        ):
            raise AssertionError(f'EXACT DETECTOR MISMATCH at {boundary}')
        self.trace.append((boundary, a.crises, a.pause_until, expected,
                           self.signal_counts[boundary] if not expected else 0))
        self.cursor = len(result.trades)
        return expected


def order_audit(guard):
    groups = defaultdict(list)
    for trade in guard.closes:
        groups[trade.closed_ms].append(trade)
    output = {}
    for name, key in [('as_recorded', lambda x:0), ('reverse', None), ('losses_first', lambda x:x.net_pct), ('gains_first', lambda x:-x.net_pct)]:
        detector = IndependentDetector()
        differences = []
        for boundary, crises, deadline, allowed, blocked in guard.trace:
            items = groups.get(boundary, [])
            items = list(reversed(items)) if key is None else sorted(items, key=key)
            decision = detector.minute(boundary, items)
            if (detector.count, detector.deadline, decision) != (crises, deadline, allowed):
                differences.append(boundary)
        output[name] = {'different_minutes':len(differences), 'first_difference':differences[:1],
                        'max_realized_dd':detector.max_dd,'final_equity':detector.balance}
    output['minutes_with_multiple_closes'] = sum(len(x)>1 for x in groups.values())
    return output


def episodes(trace):
    triggers, pauses, blocked = [], [], []
    count = 0
    start = None
    for boundary, crises, deadline, allowed, n in trace:
        if crises != count:
            triggers.append({'trigger_ms':boundary, 'scheduled_until_ms':deadline})
            count = crises
        if not allowed and start is None:
            start = boundary
        if allowed and start is not None:
            pauses.append({'start_ms':start, 'release_ms':boundary})
            start = None
        if n:
            blocked.append({'timestamp_ms':boundary,'count':n})
    if start is not None:
        pauses.append({'start_ms':start,'release_ms':None})
    return {'triggers':triggers,'pauses':pauses,'blocked_signals':blocked}


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def frozen_config():
    def original(path):
        return subprocess.check_output(['git','show',f'{REVISION}:{path}'],cwd=ROOT)
    hashes={}
    for path in SOURCE_FILES:
        before=original(path).replace(b'\r\n',b'\n')
        now=(ROOT/path).read_bytes().replace(b'\r\n',b'\n')
        if before != now:
            raise SystemExit(f'STOP: replay dependency differs from {REVISION}: {path}')
        hashes[path]=hashlib.sha256(now).hexdigest()
    config=effective_config(yaml.safe_load(original('config/config.yaml')))
    assert config['capital']['operational_balance_usdt']==100
    assert config['capital']['trade_size_pct']==20
    return config, hashes


def raw_cache_check(path, interval):
    # Reject conflicting duplicates; identical cache repeats are reported.
    seen={}; duplicates=0; previous=None; disorder=0
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if not line.strip(): continue
            row=json.loads(line); stamp=row['open_time_ms']
            if previous is not None and stamp<previous: disorder+=1
            previous=stamp
            if stamp in seen:
                duplicates+=1
                if seen[stamp]!=row: raise ValueError(f'Conflicting candles in {path} at {stamp}')
            seen[stamp]=row
            if row['close_time_ms']!=stamp+interval-1: raise ValueError('Invalid candle duration')
    return {'candles':len(seen),'duplicate_rows':duplicates,'out_of_order_rows':disorder,'sha256':sha(path)}


def replay_case(label, start, end, path, config, candles):
    start_ms=int(_ts(start).timestamp()*1000); end_ms=int(_ts(end).timestamp()*1000)-1
    print(f'{label}: generating signals {start} -> {end}',flush=True)
    signals=_signals(config,candles,start_ms,end_ms)
    # Prefix invariance: removing future candles must not change earlier signals.
    mid=((start_ms+end_ms)//2)//60_000*60_000
    prefix=_signals(config,{k:[x for x in v if x.boundary_ms<=mid] for k,v in candles.items()},start_ms,mid)
    normalize=lambda seq:[(x.boundary_ms,x.signal.price,x.signal.entry_atr,x.signal.source_candle_open_time) for x in seq]
    assert normalize(prefix)==normalize([x for x in signals if x.boundary_ms<=mid]), 'Signal lookahead/prefix mismatch'
    assert all(x.signal.source_candle_open_time+MINUTE_MS<=x.boundary_ms for x in signals)
    args=dict(lookback=0,config=config,signals=signals,execution_candles=candles['1m'],
              start_ms=start_ms,end_ms=end_ms,intrabar_path=path,
              round_trip_spread_bps=float(config['instrumentation']['market_bot_replay'].get('round_trip_spread_bps',5)))
    control=run_universe(name='CONTROL',**args)
    checked=CheckedGuard(signals)
    combo=run_universe(name='COMBO',admission_guard=checked.allows,**args)
    assert combo.blocked_circuit==sum(x[4] for x in checked.trace)
    assert checked.original.paused_minutes==checked.independent.paused_minutes
    rows=[]
    for result in (control,combo):
        metrics=_metrics(result,100,20); metrics['open']=len(result.open_positions)
        rows.append(metrics)
    rows[1].update(crises=checked.original.crises,pause_hours=checked.original.paused_minutes/60,blocked=combo.blocked_circuit)
    actual=[(round(r['net'],3),round(r['dd'],3),round(r['pf'],3),r['trades'],r['open']) for r in rows]
    actual[1]+= (rows[1]['crises'],rows[1]['pause_hours'],rows[1]['blocked'])
    print(label, 'EXACT detector equality OK;', rows,flush=True)
    print('Previous displayed results reproduced:',actual==EXPECTED.get(label),flush=True)
    return {'window':[start,end],'intrabar_path':path,'signals':len(signals),
            'metrics':rows,'expected_displayed':EXPECTED.get(label),'actual_displayed':actual,
            'matches_previous_display':actual==EXPECTED.get(label),'exact_detector_equality':True,
            'same_minute_order_audit':order_audit(checked),'events':episodes(checked.trace),
            'signal_prefix_invariance':True,'entries_control':control.entry_times,'entries_combo':combo.entry_times,
            'closes_control':[asdict(t) for t in control.trades],'closes_combo':[asdict(t) for t in combo.trades]}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cache-dir',default='data/studies/real_a_circuit_breaker/klines')
    p.add_argument('--output',default=None)
    p.add_argument('--oos-until',default='2026-09-03T20:30:00-03:00',help='Original OOS_END if preserved; original pasted report omits seconds.')
    a=p.parse_args()
    config,hashes=frozen_config()
    folder=Path(a.cache_dir)
    files=[folder/f'SOLUSDT_{interval}.jsonl' for interval in ('1m','5m','15m')]
    missing=[str(f) for f in files if not f.exists()]
    if missing: raise SystemExit('STOP: original offline caches required: '+', '.join(missing))
    manifest={f.name:raw_cache_check(f,n*MINUTE_MS) for f,n in zip(files,(1,5,15))}
    first=int(_ts(WINDOWS[0][1]).timestamp()*1000)-WARMUP_CANDLES*15*MINUTE_MS
    last=int(_ts(a.oos_until).timestamp()*1000)-1
    candles={i:load_ge_market_data(None,'SOLUSDT',i,first,last,folder,True) for i in ('1m','5m','15m')}
    out={'historical_revision':REVISION,'source_hashes':hashes,'cache_manifest':manifest,
         'resolved_config':{k:config[k] for k in ('capital','entry','trend','trend_gate','risk','ladder','fees')},
         'warmup_minutes':WARMUP_CANDLES*15,
         'oos_end_seconds_uncertain':a.oos_until==WINDOWS[1][2],
         'limits':['OHLC modeled exits; original replay admission resets every minute, not every 5m',
                   'Independent detector shares closures, not implementation; execution engine is shared',
                   'Original OOS console displayed minute precision only; use archived OOS_END if available'], 'runs':{}}
    for label,start,end,path in WINDOWS:
        if label.startswith('OOS'): end=a.oos_until
        out['runs'][label]=replay_case(label,start,end,path,config,candles)
    dest=Path(a.output or ('data/analysis/cb_hypothesis_audit_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.json'))
    dest.parent.mkdir(parents=True,exist_ok=True)
    with dest.open('x',encoding='utf-8') as f: json.dump(out,f,indent=2,allow_nan=True)
    print('Audit artifact:',dest,flush=True)


if __name__=='__main__': main()
