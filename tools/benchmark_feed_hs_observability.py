"""Local synthetic overhead and HEAD/current financial parity; no VPS actions."""
import json
import subprocess
import sys
import time
import types
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.logging_utils import JsonlLogger
from src.monitor.ws_manager import WSManager
from src.monitor.feed_observability import FeedObservability
from src.monitor import forward_experiment_shadows as current
from tests.test_fast_drop_ema_shadow import FastDropTests, _snapshot
from tests.test_forward_experiment_shadows import RiskShadowTests


class CounterLogger:
    def __init__(self):self.writes=0
    def system(self,*args,**kwargs):self.writes+=1


def baseline(path):
    source=subprocess.check_output(['git','show',f'HEAD:{path}'],cwd=ROOT,text=True)
    module=types.ModuleType('audit_baseline_'+Path(path).stem)
    exec(compile(source,path,'exec'),module.__dict__)
    return module


def timed(function,n=20000):
    times=[]
    for _ in range(5):
        start=time.perf_counter()
        for _ in range(n):function()
        times.append((time.perf_counter()-start)*1e6/n)
    return sorted(times)[2]


def financial(shadow):
    return {'clock':shadow.clock.to_state(),'pending_closes':shadow.pending_closes,
            'positions':[p.to_state() for p in shadow.positions],
            'closed_records':shadow.closed_records,'audit_events':shadow.audit_events,
            'equity':shadow.equity,'peak':shadow.peak_equity,'enabled':shadow.enabled,
            'crises':shadow.crises_triggered,'last_input':shadow.last_input_ms,
            'last_signal':shadow.last_signal_source}


def parity(old):
    results={}
    for kind in ('FAST_TRIGGER','FAST_BLOCKED','HS_BULL_ELASTIC','HS_BEAR_CLUSTER_EXIT','CB_EXIT_ALL'):
        signatures=[]
        for module in (old,current):
            with TemporaryDirectory() as tmp:
                root=Path(tmp)
                if kind.startswith('FAST'):
                    f=FastDropTests();_,config=f.make(root)
                    # Construction above has not persisted any financial state.
                    s=module.FastDropEmaShadow(root,config,JsonlLogger(root,config),None,
                        cohort_started_at='2026-10-01T00:00:00+00:00')
                    f.open(s)
                    s.on_tick(99.5,'2026-10-01T00:06:01+00:00')
                    f.reference(s,100 if kind=='FAST_TRIGGER' else 99.6)
                    s.on_closed_5m(_snapshot('SHO' if kind=='FAST_TRIGGER' else 'LON'))
                    s.on_tick(99.4,'2026-10-01T00:06:02+00:00')
                    s.on_tick(98.5,'2026-10-01T00:06:03+00:00')
                    s.on_tick(98.5,'2026-10-01T00:07:01+00:00')
                else:
                    f=RiskShadowTests();template=f._shadow(root,kind);config=template.config
                    if kind=='CB_EXIT_ALL':
                        config['instrumentation']['risk_experiment']['initial_capital_usdt']=10
                    s=module.ExperimentalRiskShadow(root,config,JsonlLogger(root,config),None,
                        settings_key='risk_experiment',strategy=kind,pair_prefix='risk',experiment=kind,
                        cohort_started_at='2026-09-27T20:00:00+00:00')
                    context={'tf_5m':{'ema_context':'LON' if kind=='HS_BULL_ELASTIC' else 'SHO','close':100,
                                     'latest_closed_at_ms':1790539199999}}
                    s.on_approved_real_a_signal(f._signal(100,1790538900000,1),context)
                    s.on_approved_real_a_signal(f._signal(99,1790539200000,6),context)
                    if kind=='CB_EXIT_ALL':
                        s.on_approved_real_a_signal(f._signal(98,1790539500000,11),context)
                        s.on_tick(97.5,'2026-09-27T20:12:00+00:00')
                        s.on_tick(97.5,'2026-09-27T20:13:00+00:00')
                        assert s.circuit_breaker_active
                        assert any(r['exit_reason']=='CIRCUIT_BREAKER_EXIT_ALL' for r in s.closed_records)
                    else:
                        s.on_tick(98.5,'2026-09-27T20:07:00+00:00')
                        if kind=='HS_BULL_ELASTIC':
                            s.on_closed_5m({'tf_5m':{'ema_context':'BEA','close':98.4,'latest_closed_at_ms':1790539799999}})
                        s.on_tick(98.4,'2026-09-27T20:11:00+00:00')
                signatures.append(financial(s))
        results[kind]=signatures[0]==signatures[1]
        if not results[kind]:
            results[kind+'_different_fields']=[key for key in signatures[0] if signatures[0][key]!=signatures[1][key]]
    return results


def main():
    old_ws=baseline('src/monitor/ws_manager.py')
    message=json.dumps({'stream':'solusdt@aggTrade','data':{'T':int(time.time()*1000),'p':'100'}})
    logger=CounterLogger();now=WSManager('wss://example',[],logger,lambda *a:None)
    old=old_ws.WSManager('wss://example',[],CounterLogger(),lambda *a:None)
    # Fixed receive clock avoids accidentally benchmarking an anomaly-log storm.
    receive=time.time()
    with patch('src.monitor.ws_manager.time.time',return_value=receive):
        baseline_us=timed(lambda:old._on_message(None,message))
        current_us=timed(lambda:now._on_message(None,message))
    normal_writes=logger.writes
    # Quantile/summary cost measured separately; not done inside the callback.
    start=time.perf_counter();now.observability.flush();summary_ms=(time.perf_counter()-start)*1000
    with TemporaryDirectory() as tmp:
        logger_io=JsonlLogger(Path(tmp),{'logging':{'console':False}})
        obs=FeedObservability(logger_io)
        for _ in range(1000):obs.received('solusdt@aggTrade',{'T':100000},101000);obs.completed(.1)
        start=time.perf_counter();obs.flush();io_ms=(time.perf_counter()-start)*1000
        bytes_written=(Path(tmp)/'logs/system.log').stat().st_size
    result={'synthetic_noop_callback_baseline_us':baseline_us,'instrumented_us':current_us,
            'incremental_us_per_input':current_us-baseline_us,'normal_input_writes':normal_writes,
            'inputs_measured':100000,'summary_for_100000_inputs_ms':summary_ms,
            'one_summary_1000_samples_actual_io_ms':io_ms,'summary_bytes':bytes_written,
            'summaries_per_minute':6,'parity_vs_HEAD':parity(baseline('src/monitor/forward_experiment_shadows.py')),
            'limits':'Local synthetic measurement, not VPS latency/SLO. Financial parity excludes diagnostic outputs only.'}
    out=ROOT/'data/analysis/feed_hs_observability_20261008';out.mkdir(parents=True,exist_ok=True)
    (out/'benchmark.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result,indent=2))
    if not all(v is True for k,v in result['parity_vs_HEAD'].items() if not k.endswith('_different_fields')):
        raise AssertionError('Financial parity failed')


if __name__=='__main__':main()
