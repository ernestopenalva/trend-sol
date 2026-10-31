"""Preregistered local idealized study. No operational mutations or order APIs."""
from __future__ import annotations
import argparse
import bisect
import hashlib
import json
import math
import subprocess
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.cohort_study import _load_config
from tools.market_selection_study import BinancePublicClient, MarketCandle, load_candle_cache, save_candle_cache, missing_candle_ranges
from tools.market_bot_replay import NullLogger, _kline_payload
from src.config_profiles import effective_config
from src.monitor.entry_engine import EntryEngine, EntrySignal, Candle
from src.monitor.market_context import MarketContextEngine
from src.monitor.forward_experiment_shadows import PolicyShadow, EmaMacdHist1mShadow
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.fast_drop_semantics import fast_drop_values, fast_drop_allowed, normal_stop_precedes_fast, fast_drop_boundary

OUT = ROOT/'data/analysis/fast_drop_selectors_20261018/idealized'
PREREG = OUT.parent/'PREREGISTER_IDEALIZED.md'
PREREG_HASH = '248fcb97a3516c6c5bfa79d507a4807976bf2095cf0c3d3bcf435eefd23da384'
MINUTE = 60000
BRT = timezone(timedelta(hours=-3))
ARMS = ('be_off_cb', 'be_off_cb_macd_bu_minus', 'be_off_cb_ema_macd', 'ema_macd_hist_1m')
START = int(datetime.fromisoformat('2026-06-01T00:00:00-03:00').timestamp()*1000)
FORWARD = int(datetime.fromisoformat('2026-10-08T23:52:00-03:00').timestamp()*1000)

def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def iso(ms):
    return datetime.fromtimestamp(ms/1000, timezone.utc).isoformat()

def aggregate(candles, n):
    groups = defaultdict(list)
    for c in candles:
        groups[c.open_time_ms//(n*MINUTE)*(n*MINUTE)].append(c)
    rows = []
    for at, cs in sorted(groups.items()):
        if len(cs)==n and all(c.open_time_ms==at+i*MINUTE for i,c in enumerate(cs)):
            rows.append(MarketCandle(at,at+n*MINUTE-1,cs[0].open,max(c.high for c in cs),min(c.low for c in cs),cs[-1].close,sum(c.quote_volume for c in cs),sum(c.trades for c in cs)))
    return rows

REMOTE_CAPTURE = r'''
import json,hashlib,datetime
from pathlib import Path
out={'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'ledgers':{},'states':{},'events':{}}
arms=['be_off_cb','be_off_cb_macd_bu_minus','be_off_cb_ema_macd','ema_macd_hist_1m','be_off_cb_fast_drop_ema']
keys=['pair_id','source_candle_open_time','opened_at','closed_at','entry_price','signal_price','exit_price','exit_reason','net_pnl_pct','entry_atr','position_notional_usdt','peak_price','market_context_entry','market_context_exit','run_id']
for a in arms:
 p=Path('data/trades/trades_'+a+'_shadow.jsonl')
 out['ledgers'][a]=[{k:r.get(k) for k in keys} for r in (json.loads(x) for x in p.open() if x.strip())]
 s=Path('data/state/'+a+'_shadow.json');v=json.loads(s.read_text());out['states'][a]={k:v.get(k) for k in ['updated_at','positions','clock','cohort_started_at','last_input_ms']}
 p=Path('data/telemetry/'+a+'_shadow_events.jsonl')
 out['events'][a]=[r for r in (json.loads(x) for x in p.open() if x.strip()) if r.get('ts','')>='2026-10-09T02:52:00']
out['fast_telemetry']=[json.loads(x) for x in Path('data/telemetry/hs_intelligence_events.jsonl').open() if 'FAST_DROP' in x]
out['source_hashes']={s:hashlib.sha256(Path(s).read_bytes()).hexdigest() for s in ['src/app.py','src/monitor/forward_experiment_shadows.py','src/monitor/circuit_breaker_shadow.py','src/monitor/fast_drop_semantics.py','src/monitor/entry_engine.py','src/monitor/market_context.py']}
print(json.dumps(out))
'''

def prepare():
    assert digest(PREREG)==PREREG_HASH, 'Preregister changed'
    OUT.mkdir(parents=True,exist_ok=True)
    cfg=effective_config(_load_config(ROOT/'config/config.yaml'))
    if (OUT/'config.json').exists():
        assert cfg==read(OUT/'config.json'), 'Configuration changed'
    else:
        dump(OUT/'config.json',cfg)
    if not (OUT/'forward.json').exists():
        r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','root@207.154.197.12','cd /root/trend-sol && venv/bin/python -'],input=REMOTE_CAPTURE,text=True,capture_output=True,check=True)
        dump(OUT/'forward.json',json.loads(r.stdout))
    client=BinancePublicClient('https://api.binance.com',30)
    if (OUT/'market_manifest.json').exists():
        manifest=read(OUT/'market_manifest.json')
    else:
        server=int(client.get('/api/v3/time')['serverTime'])
        ceiling=int(datetime.fromisoformat('2026-10-19T00:00:00-03:00').timestamp()*1000)
        end=min(server//MINUTE*MINUTE,ceiling)
        assert datetime.fromtimestamp((end-1)/1000,BRT).date().isoformat()=='2026-10-18'
        manifest={'server_time_ms':server,'end_ms':end,'start_ms':START,'preregister_sha256':digest(PREREG),'capture_utc':iso(server)}
        dump(OUT/'market_manifest.json',manifest)
    end=manifest['end_ms']
    cache=OUT/'market/SOLUSDT_1m.jsonl'
    source=ROOT/'data/analysis/price_structure_72h_20261010/market/SOLUSDT_1m.jsonl'
    rows=load_candle_cache(cache if cache.exists() else source)
    by={c.open_time_ms:c for c in rows if c.boundary_ms<=end}
    warm=min(by)
    gaps=missing_candle_ranges(list(by.values()),warm,end-1,MINUTE)
    for left,right in gaps:
        print('DOWNLOAD',iso(left),iso(right),flush=True)
        for c in client.klines('SOLUSDT','1m',left,right):
            assert c.open_time_ms not in by or by[c.open_time_ms]==c
            by[c.open_time_ms]=c
    rows=sorted(by.values(),key=lambda c:c.open_time_ms)
    assert not missing_candle_ranges(rows,warm,end-1,MINUTE),'Unresolved market gaps'
    save_candle_cache(cache,rows)
    for tf,n in [('5m',5),('15m',15),('1h',60)]:
        save_candle_cache(OUT/f'market/SOLUSDT_{tf}.jsonl',aggregate(rows,n))
    manifest.update(count_1m=len(rows),first_open_ms=warm,last_close_ms=rows[-1].close_time_ms,internal_missing_minutes=0,
                    hashes={tf:digest(OUT/f'market/SOLUSDT_{tf}.jsonl') for tf in ['1m','5m','15m','1h']},config_sha256=digest(OUT/'config.json'))
    dump(OUT/'market_manifest.json',manifest)
    print('PREPARED',len(rows),'end',iso(end),flush=True)

def reconstruct():
    assert digest(PREREG)==PREREG_HASH
    cfg=read(OUT/'config.json');end=read(OUT/'market_manifest.json')['end_ms']
    # Existing cache contains quote volume. In the active YAML, volume drying is
    # disabled; assert no volume-dependent admission before using this cache.
    assert cfg['entry']['require_volume_drying'] is False
    candles={tf:load_candle_cache(OUT/f'market/SOLUSDT_{tf}.jsonl') for tf in ['1m','5m','15m']}
    engine=EntryEngine('SOLUSDT',cfg,NullLogger());ctx=MarketContextEngine(engine,cfg)
    indices={'5m':0,'15m':0};signals=[];contexts={};last_context={}
    for i,c in enumerate(candles['1m']):
        at=c.boundary_ms
        for tf in ['15m','5m']:
            while indices[tf]<len(candles[tf]) and candles[tf][indices[tf]].boundary_ms<=at:
                row=candles[tf][indices[tf]]
                engine.on_kline('solusdt@kline_'+tf,_kline_payload(row))
                indices[tf]+=1
                if tf=='5m':
                    snap=ctx._timeframe_snapshot(engine._candles_for('5m'),'5m')
                    keys=['latest_closed_at_ms','ema_context','macd_context','macd_histogram','macd_histogram_previous','ema50','ema100','ema200']
                    last_context={k:snap.get(k) for k in keys}
                    contexts[str(at)]=last_context
        sig=engine.on_kline('solusdt@kline_1m',_kline_payload(c))
        if sig is not None and START<=at<=end:
            sig=EntrySignal(**{**asdict(sig),'ts':iso(at)})
            signals.append({'boundary_ms':at,'signal':asdict(sig),'context':last_context})
        if i%25000==0:print('RECONSTRUCT',i,'signals',len(signals),flush=True)
    dump(OUT/'signals.json',signals);dump(OUT/'contexts.json',contexts)
    dump(OUT/'reconstruction.json',{'signals':len(signals),'volume_convention':'Legacy quote-volume field, not base volume; active volume gate disabled and asserted. Relative volume not used in this study.','entry_minute':'Signal after previous closed candle; evaluate only new minute','context_implementation':'MarketContextEngine._timeframe_snapshot, runtime 300-candle buffers'})
    print('RECONSTRUCTED',len(signals),flush=True)

def policy(arm,context,at,minutes):
    if arm=='be_off_cb':return True
    obj=object.__new__(PolicyShadow)
    obj.policy='MACD_BU_MINUS' if arm=='be_off_cb_macd_bu_minus' else 'EMA_MACD'
    if arm!='ema_macd_hist_1m':
        return PolicyShadow._entry_policy(obj,context)[0]
    obj=object.__new__(EmaMacdHist1mShadow)
    obj.policy='EMA_MACD';obj.latest_market_context={'tf_5m':context};obj.last_signal_source=None
    obj.tactical_candles=[{'open_time':c.open_time_ms,'close_time':c.close_time_ms,'open':c.open,'close':c.close} for c in minutes]
    # Run the exact existing histogram/tactical admission method; replace only
    # its superclass dispatch into financial admission with a predicate capture.
    # No mutation of the operational classes: use a local proxy via method code
    # below implemented in a subclass for the actual full gate (see hist_gate).
    return hist_gate(obj,at)

def hist_gate(obj,at):
    # Calls the actual EmaMacdHist1mShadow._process_signal and intercepts only
    # CircuitBreakerShadow._process_signal through a research-only subclass MRO.
    class Capture(CircuitBreakerShadow):
        def _process_signal(self,signal):
            return self._entry_policy(self._context_fields())[0]
    class Hist(EmaMacdHist1mShadow,Capture):
        def _event_at(self,*args,**kwargs):pass
    obj.__class__=Hist
    return obj._process_signal(EntrySignal('SOLUSDT',1.,iso(at),at-MINUTE,1.,'1m',14))

def fidelity():
    cfg=read(OUT/'config.json');fw=read(OUT/'forward.json');signals=read(OUT/'signals.json')
    smap={r['signal']['source_candle_open_time']:r for r in signals}
    mins=load_candle_cache(OUT/'market/SOLUSDT_1m.jsonl');mi={c.boundary_ms:c for c in mins}
    contexts={int(k):v for k,v in read(OUT/'contexts.json').items()};cts=sorted(contexts)
    def context(at):
        ix=bisect.bisect_right(cts,at)-1
        return contexts[cts[ix]] if ix>=0 else {}
    correctness=[]
    for r in fw['fast_telemetry']:
        if r.get('intelligence')!='FAST_DROP':continue
        v=r.get('values',{});co=r.get('conditions',{});at=int(datetime.fromisoformat(r['ts']).timestamp()*1000)
        status='INSUFFICIENT_INPUTS'
        if co.get('reference_available') and all(k in v for k in ['reference_price','velocity_target_price','velocity_5m_pct_per_min','effective_stop','ema_context_used']):
            velocity=(v['velocity_target_price']/v['reference_price']-1)*100/5
            normal=normal_stop_precedes_fast(v['effective_stop'],v['velocity_target_price'])
            speed=velocity<=-.10+1e-12;ema=v['ema_context_used'] in {'SHO','BEA'}
            reasons=['NORMAL_STOP_PRECEDES'] if normal else [k for k,ok in [('SPEED_BLOCKED',speed),('EMA_BLOCKED',ema)] if not ok]
            expected='NO_TRIGGER' if reasons else 'TRIGGER'
            correct=abs(velocity-v['velocity_5m_pct_per_min'])<=1e-10 and expected==r['decision'] and reasons==r['reasons'] and v['reference_boundary_ms']==fast_drop_boundary(at)-5*MINUTE
            status='PASS' if correct else 'IMPLEMENTATION_MISMATCH'
        ideal=context(at)
        correctness.append({'pair_id':r['pair_id'],'at':r['ts'],'minute':at//MINUTE*MINUTE,'status':status,'decision':r['decision'],'reasons':r.get('reasons'),
                            'ideal_ema':ideal.get('ema_context'),'observed_ema':v.get('ema_context_used'),
                            'ema_difference_class':'SAME' if ideal.get('ema_context')==v.get('ema_context_used') else 'CONTEXT_AVAILABILITY_OR_HISTORY',
                            'reference_missing':not co.get('reference_available',False)})
    assert not any(x['status']=='IMPLEMENTATION_MISMATCH' for x in correctness),'FAST correctness gate failed'
    arms={}
    end=read(OUT/'market_manifest.json')['end_ms']
    for arm in ARMS:
        rows=[r for r in fw['ledgers'][arm]+fw['states'][arm].get('positions',[]) if r.get('source_candle_open_time') is not None]
        selected={r['source_candle_open_time']:r for r in rows if FORWARD<=int(datetime.fromisoformat(r.get('opened_at') or r.get('open_ts')).timestamp()*1000)<=end}
        details=[]
        for source,r in selected.items():
            s=smap.get(source);at=(source+MINUTE)
            original=((r.get('market_context_entry') or {}).get('tf_5m') or {})
            current=context(at)
            labels_match=(original.get('ema_context'),original.get('macd_context'))==(current.get('ema_context'),current.get('macd_context'))
            minutes=[mi[t] for t in (at-MINUTE,at) if t in mi]
            passed=policy(arm,current,at,minutes)
            details.append({'source':source,'signal_match':s is not None,'context_match':labels_match,'policy_pass':passed,
                            'observed_ema':original.get('ema_context'),'reconstructed_ema':current.get('ema_context'),
                            'observed_macd':original.get('macd_context'),'reconstructed_macd':current.get('macd_context'),
                            'observed_entry_at':r.get('opened_at') or r.get('open_ts'),'ideal_entry_at':iso(at),
                            'signal_price':s['signal']['price'] if s else None,'observed_entry_price':r.get('entry_price')})
        n=len(details);matched=[r for r in details if r['signal_match']]
        recall=sum(r['signal_match'] for r in details)/n if n else 0
        ctx=sum(r['context_match'] for r in matched)/len(matched) if matched else 0
        pol=sum(r['policy_pass'] for r in details)/n if n else 0
        arms[arm]={'n':n,'signal_recall':recall,'context_agreement':ctx,'selector_pass_rate':pol,'qualified':n>=20 and recall>=.95 and ctx>=.99 and pol>=.95,'details':details,
                   'portfolio_initial_conditions':'NOT_EQUIVALENT: historical inherited state versus idealized flat June start; portfolio precision/recall require subsequent diagnostic attribution.'}
    out={'preregister_sha256':digest(PREREG),'fast_correctness':correctness,'controls':arms}
    dump(OUT/'fidelity.json',out)
    print(json.dumps({a:{k:v for k,v in x.items() if k!='details'} for a,x in arms.items()},indent=2),flush=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','reconstruct','fidelity']);args=parser.parse_args()
    {'prepare':prepare,'reconstruct':reconstruct,'fidelity':fidelity}[args.stage]()

if __name__=='__main__':main()
