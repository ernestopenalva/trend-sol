"""Read-only checkpoint composition + local JSON CPU benchmark (not live VPS timing).

Does not instantiate arms, process inputs, rewrite checkpoints or simulate trading.
"""
import argparse
import json
import time
from pathlib import Path


def quantile(values, q):
    s=sorted(values)
    if not s:return None
    at=(len(s)-1)*q;lo=int(at)
    return s[lo]+(s[min(lo+1,len(s)-1)]-s[lo])*(at-lo)


def distribution(values):
    return {'N':len(values),'p50':quantile(values,.5),'p90':quantile(values,.9),
            'p99':quantile(values,.99),'mean':sum(values)/len(values) if values else None}


def encoded_bytes(value):
    return len(json.dumps(value,ensure_ascii=False).encode('utf8'))


def composition(state):
    parts={key:encoded_bytes(value) for key,value in state.items()}
    total=encoded_bytes(state)
    parts_sorted=sorted(parts.items(),key=lambda x:x[1],reverse=True)
    return {'total_bytes':total,'parts':dict(parts_sorted),
            'historical_bytes':parts.get('closed_records',0)+parts.get('audit_events',0),
            'historical_fraction':(parts.get('closed_records',0)+parts.get('audit_events',0))/total,
            'closed_records':len(state.get('closed_records',[])),
            'audit_events':len(state.get('audit_events',[])),
            'positions':len(state.get('positions',[])),
            'cb_schema':state.get('cb_schema')}


def measure(state,repeats):
    wall=[];cpu=[]
    for _ in range(repeats):
        w=time.perf_counter();c=time.thread_time()
        result=json.dumps(state,ensure_ascii=False)
        cpu.append((time.thread_time()-c)*1000)
        wall.append((time.perf_counter()-w)*1000)
        del result
    return {'serialization_wall_ms':distribution(wall),'serialization_cpu_ms':distribution(cpu)}


def inspect(raw,repeats=30):
    arms=[]
    for path in sorted(raw.glob('*shadow.json')):
        state=json.loads(path.read_text(encoding='utf8'))
        if not isinstance(state,dict) or state.get('cb_schema') not in (2,3):continue
        # Both variants retain identical immutable history; no trading is executed.
        empty={**state,'positions':[]}
        arms.append({'file':path.name,'updated_at':state.get('updated_at'),
                     **composition(state), 'captured_positions':measure(state,repeats),
                     'zero_positions_same_history':measure(empty,repeats),
                     'zero_position_bytes':encoded_bytes(empty)})
    return {'scope':'Local serialization only, real VPS checkpoint copies; NOT live callback per-arm measurements',
            'arms':arms,'method':'json.dumps ensure_ascii=False, thread CPU and wall; no IO in timed region',
            'repeats':repeats}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw',type=Path,default=Path('data/analysis/callback_cost_profile_20261008/raw'))
    parser.add_argument('--repeats',type=int,default=30)
    args=parser.parse_args()
    result=inspect(args.raw,args.repeats)
    print(json.dumps(result))


if __name__=='__main__':main()
