"""Replay-only FAST_DROP one-shot vs re-evaluation; no parameter search."""
import bisect
import hashlib
import json
import sys
from collections import Counter,defaultdict
from dataclasses import asdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools import be_off_cb_fast_drop_systemic_replay as systemic
from tools.winner_trajectory_study import INPUT,CACHE,SIGNALS,dist
from tools.market_selection_study import load_candle_cache
from tools.be_off_cb_deterioration_study import build_context_index
from tools.ge_replay_study import SignalEvent
from src.monitor.entry_engine import EntrySignal
from src.monitor.fast_drop_semantics import fast_drop_values,loss_reached,fast_drop_allowed,normal_stop_precedes_fast
from tools.market_bot_replay import MINUTE_MS,_deduplicate
from tools.be_off_cb_defensive_closure import ms,serialize
from tools.be_off_cb_exit_context_study import brt

OUT=ROOT/'data/studies/fast_drop_v2/20261007'
MODES=('CONTROL','ONE_SHOT','REEVALUATE','REEVALUATE_FRESH')


def context_at(contexts,at):
    i=bisect.bisect_left(contexts,(at,))-1
    value=contexts[i] if i>=0 else None
    expected=at-at%300000-1
    return value, bool(value and value[0]==expected),expected


def evaluate(entry,price,reference,context,fresh,mode,normal_stop):
    target,velocity=fast_drop_values(entry,reference)
    predicates={'loss':loss_reached(entry,price),'reference':reference is not None,
        'normal_priority':not normal_stop_precedes_fast(normal_stop,target),
        'speed':velocity is not None and velocity<=-.1+1e-12,
        'EMA':context in ('SHO','BEA'),'fresh':fresh}
    allowed=all(predicates[k] for k in ('loss','reference','normal_priority','speed','EMA'))
    if mode=='REEVALUATE_FRESH':allowed=allowed and fresh
    return target,velocity,predicates,allowed


class Observer:
    def __init__(self,mode,path,stream):
        self.mode=mode;self.path=path;self.stream=stream
        self.stats=defaultdict(lambda:{'zone':False,'valid':0,'attempts':0,'trigger':False,'after_first':False,
            'blocked_speed':0,'blocked_EMA':0,'blocked_fresh':0,'missing_reference':0,'stale_used':0})
        self.all_sources={};self.closes={}

    def process(self,positions,trades,candle,path,fees,fast_enabled,evaluated,minute_index,contexts):
        at=candle.open_time_ms;ctx,fresh,expected=context_at(contexts,at)
        ema=ctx[1] if ctx else 'UNAVAILABLE'
        reference_candle=minute_index.get(candle.boundary_ms-5*MINUTE_MS)
        reference=reference_candle.close if reference_candle and reference_candle.close_time_ms<at else None
        points=_deduplicate((candle.open,candle.high,candle.low,candle.close) if path=='HIGH_FIRST' else (candle.open,candle.low,candle.high,candle.close))
        for rp in list(positions):
            p=rp.position;source=p.source_candle_open_time
            self.all_sources[source]=rp.opened_ms
            if p.status!='OPEN':continue
            previous=None;s=self.stats[source]
            for index,point in enumerate(points):
                normal=p.effective_stop
                normal_cross=previous is not None and previous>normal and point<=normal
                normal_hit=point<=normal
                loss=loss_reached(p.entry_price,point)
                chosen=False;target=p.entry_price*.995
                fast_first_cross=previous is not None and previous>target and point<=target
                # If already below loss, a lower normal stop crossed before the
                # next modeled endpoint cannot be overtaken by a late FAST decision.
                normal_first=normal_hit and (not fast_first_cross or normal_stop_precedes_fast(normal,target))
                # A normal stop above the loss threshold is reached first on an
                # interpolated downward segment. Its later endpoint was not visited.
                if loss and not (normal_cross and normal_stop_precedes_fast(normal,target)):
                    s['zone']=True
                    skipped=self.mode=='CONTROL' or (self.mode=='ONE_SHOT' and p.pair_id in evaluated)
                    target,velocity,predicates,allowed=evaluate(p.entry_price,point,reference,ema,fresh,self.mode,normal)
                    valid=not skipped and reference is not None
                    if not skipped:
                        s['attempts']+=1;s['valid']+=int(valid)
                        s['missing_reference']+=int(reference is None)
                        if valid:
                            if self.mode=='ONE_SHOT':evaluated.add(p.pair_id)
                            s['blocked_speed']+=int(not predicates['speed'])
                            s['blocked_EMA']+=int(not predicates['EMA'])
                            s['blocked_fresh']+=int(self.mode=='REEVALUATE_FRESH' and not fresh)
                            s['stale_used']+=int(ctx is not None and not fresh and self.mode!='REEVALUATE_FRESH')
                        chosen=valid and allowed and not normal_first
                    event={'mode':self.mode,'path':path,'source':source,'modeled_at_ms':at,
                        'label_boundary_ms':candle.boundary_ms,'point_index':index,'price':point,
                        'entry':p.entry_price,'pnl_pct':(point/p.entry_price-1)*100,
                        'reference_boundary':candle.boundary_ms-5*MINUTE_MS,'reference':reference,
                        'target':target,'velocity':velocity,'EMA':ema,'context_close':ctx[0] if ctx else None,
                        'expected_context_close':expected,'predicates':predicates,'valid_reference_evaluation':valid,
                        'normal_stop_first_on_segment':normal_first,'skipped':skipped,'fired':chosen,
                        'valid_evaluation_number':s['valid']}
                    self.stream.write(json.dumps(event)+'\n')
                if chosen:
                    # First descending crossing can interpolate. Later decisions/gaps
                    # use actual available endpoint, never an already passed target.
                    tick=target if fast_first_cross else point
                    rp.client.current_price=tick;p.on_tick(tick,systemic._iso(candle.boundary_ms))
                    if p.status=='OPEN':p._close_at_market(tick,'FAST_DROP',systemic._iso(candle.boundary_ms),target)
                    s['trigger']=p.exit_reason=='FAST_DROP';s['after_first']=s['trigger'] and s['valid']>1
                else:
                    tick=normal if normal_cross else point
                    rp.client.current_price=tick;p.on_tick(tick,systemic._iso(candle.boundary_ms))
                previous=point
                if p.status=='CLOSED':
                    systemic._append_trade(p,rp.opened_ms,candle.boundary_ms,fees,trades)
                    self.closes[source]={'source':source,'opened_ms':rp.opened_ms,'closed_ms':candle.boundary_ms,
                        'reason':p.exit_reason,'entry':p.entry_price,'exit':p.exit_price,'net_pct':trades[-1].net_pct}
                    break

    def counts(self,month=None):
        selected={k:s for k,s in self.stats.items() if month is None or brt(self.all_sources[k])[:7]==month}
        zone=[s for s in selected.values() if s['zone']];valid=[s for s in zone if s['valid']]
        counts=[s['valid'] for s in valid]
        fires=sum(s['trigger'] for s in zone)
        return {'zone_trades':len(zone),'evaluated_trades':len(valid),
            'valid_evaluations_per_evaluated_trade':dist(counts),'max_evaluations':max(counts,default=0),
            'FAST':fires,'FAST_pct_zone':fires/len(zone)*100 if zone else None,
            'after_first_valid':sum(s['after_first'] for s in zone),
            **{k:sum(s[k] for s in zone) for k in ('blocked_speed','blocked_EMA','blocked_fresh','missing_reference','stale_used')}}


def attribution(observer,control,notional):
    output=[]
    for source,r in observer.closes.items():
        if r['reason']!='FAST_DROP':continue
        c=control.closes.get(source)
        output.append({'source':source,'FAST_closed_ms':r['closed_ms'],'FAST_net':r['net_pct']*notional/100,
            'control_reason':c['reason'] if c else 'NO_CLOSED_MATCH',
            'control_net':c['net_pct']*notional/100 if c else None,
            'delta':(r['net_pct']-c['net_pct'])*notional/100 if c else None,
            'HS_anticipated':bool(c and c['reason']=='HARD_STOP' and r['closed_ms']<c['closed_ms'])})
    return {'rows':output,'HS_anticipated':sum(r['HS_anticipated'] for r in output),
        'HS_saving':sum(r['delta'] for r in output if r['control_reason']=='HARD_STOP'),
        'winners_sacrificed':sum(r['control_reason'] in ('PROFIT_LOCK','TRAILING') and r['control_net']>0 and r['delta']<0 for r in output),
        'by_control_exit':{reason:{'N':len(s),'delta':sum(r['delta'] for r in s if r['delta'] is not None)}
            for reason in sorted({r['control_reason'] for r in output})
            for s in [[r for r in output if r['control_reason']==reason]]}}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    frozen=json.loads((INPUT/'manifest.json').read_text());cfg=frozen['config']
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    for tf,h in frozen['cache_hashes'].items():
        if digest(CACHE/f'SOLUSDT_{tf}.jsonl')!=h:raise ValueError('Frozen candle cache changed')
    if digest(SIGNALS)!=frozen['signals_sha256']:raise ValueError('Frozen signals changed')
    candles={tf:load_candle_cache(CACHE/f'SOLUSDT_{tf}.jsonl') for tf in ('1m','5m')}
    contexts=build_context_index(candles['5m'])
    signals=[SignalEvent(s['boundary_ms'],EntrySignal(**s['signal'])) for s in json.loads(SIGNALS.read_text())]
    start=ms(frozen['start_brt']);end=ms(frozen['end_brt']);notional=frozen['notional']
    months=('2026-06','2026-07','2026-08','2026-09','2026-10')
    original=systemic.process_candle_systemic;summary={}
    systemic._SIGNAL_BOUNDARIES={s.boundary_ms for s in signals}
    try:
        for path in ('HIGH_FIRST','LOW_FIRST'):
            summary[path]={};observers={}
            for mode in MODES:
                print(path,mode,flush=True)
                with (OUT/f'{path}_{mode}_evaluations.jsonl').open('w',encoding='utf8') as stream:
                    observer=Observer(mode,path,stream);observers[mode]=observer
                    systemic.process_candle_systemic=observer.process
                    run=systemic.run_systemic(name=f'{mode}_{path}',config=cfg,signals=signals,
                        candles=candles['1m'],contexts=contexts,start_ms=start,end_ms=end,path=path,
                        spread_bps=frozen['spread_bps'],fast_enabled=mode!='CONTROL')
                for rp in run.result.open_positions:observer.all_sources[rp.position.source_candle_open_time]=rp.opened_ms
                data={'counts':observer.counts(),'metrics':systemic.metrics(run,notional,None),
                    'monthly':{m:{'counts_by_entry_month':observer.counts(m),'metrics_by_close_month':systemic.metrics(run,notional,m)} for m in months},
                    'admitted':len(run.result.entry_times),'open':len(run.result.open_positions)}
                (OUT/f'{path}_{mode}_trades.json').write_text(json.dumps({'closed':list(observer.closes.values()),
                    'admissions':run.admission_audit,'per_trade_evaluations':dict(observer.stats)},indent=2),encoding='utf8')
                summary[path][mode]=data
            for mode in MODES[1:]:summary[path][mode]['attribution_FAST_vs_control']=attribution(observers[mode],observers['CONTROL'],notional)
            # Verify unmodified control economics against previously frozen full-engine control.
            old=json.loads((INPUT/f'{path}_ACT10_GAP5.json').read_text())['run']['trades']
            actual=list(observers['CONTROL'].closes.values())
            if len(actual)!=len(old) or any(abs(a['net_pct']-b['net_pct'])>1e-8 or a['closed_ms']!=b['closed_ms'] or a['reason']!=b['exit_reason'] for a,b in zip(actual,old)):
                raise AssertionError('Control parity failed')
    finally:systemic.process_candle_systemic=original
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
    (OUT/'manifest.json').write_text(json.dumps({'start_brt':frozen['start_brt'],'end_brt':frozen['end_brt'],
        'thresholds':{'loss':-.5,'velocity':-.1,'EMA':['SHO','BEA']},'modes':MODES,
        'systemic':True,'cadence':'up to 4 deduplicated OHLC points per 1m, context frozen at minute open',
        'fresh':'context close == floor(evaluation_at/5m)*5m-1ms; no manufactured gaps',
        'fills':'first descending crossing at target; later evaluations/gaps at available endpoint; normal stop priority',
        'current_forward_one_shot':True,'no_06_07_oct_calibration':True,'control_parity':'passed both paths',
        'source_sha256':{str(p.relative_to(ROOT)):digest(p) for p in (Path(__file__),SIGNALS,INPUT/'manifest.json',ROOT/'src/monitor/fast_drop_semantics.py')},
        'cache_sha256':frozen['cache_hashes']},indent=2),encoding='utf8')


if __name__=='__main__':main()
