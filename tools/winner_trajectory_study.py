"""Offline winner-path diagnosis. No alternative trading arms or live writes.

Reconstructs the frozen systemic BE_OFF_CB admissions with the real exit engine.
One-ATR reversal is a measurement landmark, NOT a proposed exit threshold.
All market-future outcomes are labels, never inputs to a trading decision.
"""
import argparse
import bisect
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.be_off_cb_defensive_review import Review, iso
from tools.be_off_cb_defensive_closure import ms
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import MINUTE_MS, _deduplicate
from tools.ge_replay_study import SignalEvent
from src.monitor.entry_engine import EntrySignal
from tools.trail_gap_systemic_study import snapshot
from tools.be_off_cb_exit_context_study import brt

INPUT=ROOT/'data/studies/trail_activation_gap_systemic/20261005'
CACHE=ROOT/'data/studies/be_off_cb_deterioration/klines'
SIGNALS=ROOT/'data/studies/be_off_cb_defensive_closure/20261002/signals.json'
OUT=ROOT/'data/studies/winner_management/20261006'


def dist(values):
    values=sorted(v for v in values if v is not None)
    def q(f):
        if not values:return None
        i=(len(values)-1)*f;lo=int(i);hi=math.ceil(i)
        return values[lo]+(values[hi]-values[lo])*(i-lo)
    return {'n':len(values),'mean':sum(values)/len(values) if values else None,
            'median':q(.5),'p10':q(.1),'p90':q(.9)}


def terminal(entry,peak,exit_price,atr,net,fees,spread_bps=0):
    advance=peak-entry
    available=(peak*(1-spread_bps/2/10000)/entry-1)*100-fees
    return {'eligible':available>0,'mfe_price':advance,'mfe_atr':advance/atr,
        'mfe_gross_pct':advance/entry*100,'mfe_net_potential_pct':available,
        'net':net,'gross_pct':(exit_price/entry-1)*100 if exit_price is not None else None,
        'giveback_price':peak-exit_price if exit_price is not None else None,
        'giveback_atr':(peak-exit_price)/atr if exit_price is not None else None,
        'giveback_fraction':(peak-exit_price)/advance if exit_price is not None and advance>0 else None,
        'captured_fraction':(exit_price-entry)/advance if exit_price is not None and advance>0 else None}


class Pullbacks:
    """Disjoint peak-to-recovery/exit episodes, including censored terminal ones."""
    def __init__(self,entry,atr,fees):
        self.entry=entry;self.atr=atr;self.fees=fees;self.peak=entry
        self.peak_at=None;self.episode=None;self.episodes=[]
        self.peak_context=None

    def finish(self,recovered,at):
        if self.episode is not None:
            e=self.episode;e['recovered_before_exit']=recovered;e['ended_ms']=at
            e['depth_price']=e['start_peak']-e['trough']
            e['depth_atr']=e['depth_price']/self.atr
            e['depth_fraction']=e['depth_price']/(e['start_peak']-self.entry)
            self.episodes.append(e);self.episode=None

    def observe(self,price,at,context):
        if price>=self.peak:
            if self.episode:self.finish(True,at)
            if price>self.peak:self.peak=price;self.peak_at=at;self.peak_context=context
            return
        # Avoid conditioning on eventual winners: eligibility is known now.
        if (self.peak/self.entry-1)*100<=self.fees:return
        if self.episode is None:
            self.episode={'start_peak':self.peak,'start_ms':self.peak_at or at,
                'advance_at_start_atr':(self.peak-self.entry)/self.atr,
                'start_context':self.peak_context,'retraction_context':context,'trough':price,'trough_ms':at,'landmark':None}
        e=self.episode
        if price<e['trough']:e['trough']=price;e['trough_ms']=at
        if e['landmark'] is None and self.peak-price>=self.atr:
            e['landmark']={'at_ms':at,'price':price,'peak':self.peak,
                'advance_atr':(self.peak-self.entry)/self.atr,
                'depth_atr':(self.peak-price)/self.atr,
                'depth_fraction':(self.peak-price)/(self.peak-self.entry),'context':context}


def future_label(review,landmark,horizon=60):
    """Next complete minutes only: same-candle remainder deliberately excluded."""
    at=landmark['at_ms'];first=bisect.bisect_left(review.opens,at)
    bars=review.minute[first:first+horizon]
    if len(bars)!=horizon or any(c.open_time_ms!=at+i*MINUTE_MS for i,c in enumerate(bars)):
        return {'complete':False,'recovered':None}
    return {'complete':True,'recovered':any(c.high>landmark['peak'] for c in bars),
        'favorable_after_landmark':max(c.high for c in bars)-landmark['price'],
        'adverse_after_landmark':min(c.low for c in bars)-landmark['price'],
        'close_after_60m':bars[-1].close}


def reconstruct(t,d,review,path):
    rp=review.new_position(t['opened_ms'],t['entry_price']);p=rp.position
    cost_pct=((1+review.fees/100)/(1-review.spread/2/10000)-1)*100
    tracker=Pullbacks(p.entry_price,p.entry_atr,cost_pct)
    stops=[];old_state=None;winner_at=None;closed=None
    for c in review.minute[bisect.bisect_left(review.opens,t['opened_ms']):]:
        if c.boundary_ms>review.end:break
        if t['closed_ms'] is not None and c.boundary_ms>t['closed_ms']:break
        ctx=review.context_fields(c.open_time_ms)
        if ctx.get('latest_closed_at_ms') is not None and ctx['latest_closed_at_ms']>=c.open_time_ms:
            raise AssertionError('5m context not closed before modeled segment')
        points=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST' else (c.open,c.low,c.high,c.close))
        previous=None
        for point in points:
            stop=p.effective_stop
            tick=stop if previous is not None and previous>stop and point<=stop else point
            tracker.observe(tick,c.boundary_ms,ctx)
            rp.client.current_price=tick;p.on_tick(tick,iso(c.boundary_ms));previous=point
            if winner_at is None and (p.highest_price/p.entry_price-1)*100>cost_pct:
                winner_at={'at_ms':c.boundary_ms,'context':ctx,'price':p.highest_price}
            row=snapshot(p,tick,c.boundary_ms)
            key=(row['effective_stop'],row['owner'],row['trail_active'])
            if key!=old_state:stops.append({**row,'context':ctx});old_state=key
            if p.status=='CLOSED':closed=c.boundary_ms;break
        if closed is not None:break
    tracker.finish(False,closed or review.end)
    net=review.notional*(p.pnl_pct(p.exit_price)-review.fees)/100 if closed is not None else None
    expected_reason=t['exit_reason'] if t['closed_ms'] is not None else None
    if closed!=t['closed_ms'] or p.exit_reason!=expected_reason:
        raise AssertionError(f'baseline exit parity {path} {t["source_candle"]}: {closed,p.exit_reason} vs {t["closed_ms"],t["exit_reason"]}')
    for actual,expected in ((p.highest_price,t['peak_price']),(p.exit_price,t['exit_price']),(net,t['net_usd'])):
        if actual is None and expected is None:continue
        if actual is None or expected is None or abs(actual-expected)>1e-8:raise AssertionError('baseline economics/MFE parity')
    for e in tracker.episodes:
        e.update(source=t['source_candle'],opened_ms=t['opened_ms'],entry_atr=p.entry_atr,
            path=path,month=brt(t['opened_ms'])[:7])
        e['new_mfe_after_episode_atr']=max(0,p.highest_price-e['start_peak'])/p.entry_atr if e['recovered_before_exit'] else 0
        if e['landmark']:e['future_60m']=future_label(review,e['landmark'])
    return {'source':t['source_candle'],'opened_ms':t['opened_ms'],'closed_ms':closed,
        'month':brt(t['opened_ms'])[:7],'entry':p.entry_price,'entry_atr':p.entry_atr,
        'reason':p.exit_reason or 'OPEN','peak':p.highest_price,'exit':p.exit_price,
        **terminal(p.entry_price,p.highest_price,p.exit_price,p.entry_atr,net,review.fees,review.spread),
        'entry_context':review.context_fields(t['opened_ms']),'winner_at':winner_at,
        'exit_context':review.context_fields(max(t['opened_ms'],(closed or review.end)-MINUTE_MS)),
        'stops':stops,'episodes':tracker.episodes,
        'activation':d['activation'],'first_dominance':d['first_dominance'],
        'owner_minutes':d['interval_minutes']}


def stage(atr):return '<5' if atr<5 else '5–10' if atr<10 else '10–20' if atr<20 else '20+'


def aggregate(rows):
    closed=[r for r in rows if r['closed_ms'] is not None]
    nets=[r['net'] for r in closed];win=sum(max(0,n) for n in nets);loss=-sum(min(0,n) for n in nets)
    days=Counter(brt(r['opened_ms'])[:10] for r in rows)
    return {'N':len(rows),'closed':len(closed),'open':len(rows)-len(closed),
        'net':sum(nets),'PF':win/loss if loss else None,'reasons':dict(Counter(r['reason'] for r in closed)),
        'net_dist':dist(nets),'mfe_atr':dist([r['mfe_atr'] for r in rows]),
        'giveback_atr':dist([r['giveback_atr'] for r in closed]),
        'giveback_fraction':dist([r['giveback_fraction'] for r in closed]),
        'days':len(days),'largest_day_share':max(days.values(),default=0)/len(rows) if rows else None,
        'net_without_top3':sum(nets)-sum(sorted([n for n in nets if n>0],reverse=True)[:3]),
        'activated':sum(bool(r['activation']) for r in rows),
        'trail_dominated':sum(bool(r['first_dominance']) for r in rows),
        'activated_never_dominated':sum(bool(r['activation']) and not r['first_dominance'] for r in rows)}


def landmark_summary(events):
    resolved=[e for e in events if e['future_60m']['complete']]
    trades={e['source'] for e in events};days=Counter(brt(e['opened_ms'])[:10] for e in events)
    return {'events':len(events),'distinct_trades':len(trades),'complete_60m':len(resolved),
        'recovery_60m_rate':sum(e['future_60m']['recovered'] for e in resolved)/len(resolved) if resolved else None,
        'recovered_prior_peak_before_exit_rate':sum(e['recovered_before_exit'] for e in events)/len(events) if events else None,
        'same_minute_recovery':sum(e['recovered_before_exit'] and e['ended_ms']==e['landmark']['at_ms'] for e in events),
        'landmark_same_minute_as_peak':sum(e['start_ms']==e['landmark']['at_ms'] for e in events),
        'depth_atr':dist([e['landmark']['depth_atr'] for e in events]),
        'continuation_atr':dist([e['new_mfe_after_episode_atr'] for e in events]),
        'days':len(days),'largest_day_share':max(days.values(),default=0)/len(events) if events else None}


def prediction(events):
    """Fixed simple ridge-logistic diagnostics, not a policy optimizer.

    Only first landmark per trade avoids pseudo-replication. Train June/July,
    evaluate untouched Aug/Sep/Oct-partial. Future data is used only as label.
    """
    import numpy as np
    first={}
    for e in sorted(events,key=lambda e:e['landmark']['at_ms']):
        if e['future_60m']['complete']:first.setdefault(e['source'],e)
    rows=list(first.values());train=[r for r in rows if r['month']<'2026-08'];test=[r for r in rows if r['month']>='2026-08']
    if len(train)<30 or len(test)<30:return {'train_n':len(train),'test_n':len(test),'status':'INSUFFICIENT'}
    emas=('LON','BUL','BEA','SHO','MUP','MDO','MIX');macds=('BU+','BU-','BE+','BE-')
    def raw(r,kind):
        l=r['landmark'];ctx=l['context']
        s=[math.log1p(l['advance_atr']),l['depth_fraction']]
        c=[float(ctx['ema_context']==v) for v in emas]+[float(ctx['macd_context']==v) for v in macds]
        return [] if kind=='intercept' else s if kind=='stage' else c if kind=='context' else s+c if kind=='both' else s+c+[s[0]*v for v in c]
    results={}
    for kind in ('intercept','stage','context','both','interaction'):
        x=np.array([raw(r,kind) for r in train],dtype=float).reshape(len(train),-1)
        z=np.array([raw(r,kind) for r in test],dtype=float).reshape(len(test),-1)
        if x.shape[1]:
            mean=x.mean(axis=0);std=x.std(axis=0);std[std<1e-9]=1
            x=(x-mean)/std;z=(z-mean)/std
        x=np.column_stack([np.ones(len(x)),x]);z=np.column_stack([np.ones(len(z)),z])
        y=np.array([float(r['future_60m']['recovered']) for r in train]);target=np.array([float(r['future_60m']['recovered']) for r in test])
        beta=np.zeros(x.shape[1]);penalty=np.eye(len(beta));penalty[0,0]=0
        for _ in range(40):
            p=1/(1+np.exp(-np.clip(x@beta,-30,30)))
            grad=x.T@(p-y)+penalty@beta
            h=x.T@((p*(1-p))[:,None]*x)+penalty+np.eye(len(beta))*1e-8
            step=np.linalg.solve(h,grad);beta-=step
            if np.max(np.abs(step))<1e-7:break
        p=1/(1+np.exp(-np.clip(z@beta,-30,30)))
        results[kind]={'brier':float(np.mean((p-target)**2)),
            'logloss':float(-np.mean(target*np.log(p+1e-12)+(1-target)*np.log(1-p+1e-12))),
            'test_months':{m:{'N':sum(r['month']==m for r in test),
                'brier':float(np.mean((p[np.array([r['month']==m for r in test])]-target[np.array([r['month']==m for r in test])])**2))} for m in sorted({r['month'] for r in test})}}
    return {'train_n':len(train),'test_n':len(test),'train_prevalence':float(y.mean()),
        'test_prevalence':float(target.mean()),'L2':1,'models':results}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=OUT)
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((INPUT/'manifest.json').read_text());cfg=manifest['config']
    def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
    if digest(ROOT/'src/position/bot_full_engine.py')!=manifest['source_hashes']['src/position/bot_full_engine.py']:
        raise ValueError('Engine differs from frozen baseline; do not infer parity')
    for tf,expected in manifest['cache_hashes'].items():
        if digest(CACHE/f'SOLUSDT_{tf}.jsonl')!=expected:raise ValueError('Candle cache differs')
    if digest(SIGNALS)!=manifest['signals_sha256']:raise ValueError('Signals differ')
    candles={i:load_candle_cache(CACHE/f'SOLUSDT_{i}.jsonl') for i in ('1m','5m','15m')}
    signals=[SignalEvent(s['boundary_ms'],EntrySignal(**s['signal'])) for s in json.loads(SIGNALS.read_text())]
    review=Review(cfg,candles,signals,ms(manifest['end_brt']))
    output={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        baseline=json.loads((INPUT/f'{path}_ACT10_GAP5.json').read_text())
        details={d['source_candle']:d for d in baseline['details']};rows=[]
        for i,t in enumerate(baseline['run']['trades']):
            if i%100==0:print(path,i,'/',len(baseline['run']['trades']),flush=True)
            rows.append(reconstruct(t,details[t['source_candle']],review,path))
        eligible=[r for r in rows if r['eligible']]
        landmarks=[e for r in eligible for e in r['episodes'] if e['landmark']]
        summary={'all_control':aggregate(rows),'eligible_winners':aggregate(eligible),
            'monthly':{m:aggregate([r for r in eligible if r['month']==m]) for m in sorted({r['month'] for r in rows})},
            'entry_ema':{ctx:aggregate([r for r in eligible if r['entry_context']['ema_context']==ctx]) for ctx in sorted({r['entry_context']['ema_context'] for r in eligible})},
            'landmarks':landmark_summary(landmarks),
            'stage':{band:landmark_summary([e for e in landmarks if stage(e['landmark']['advance_atr'])==band]) for band in ('<5','5–10','10–20','20+')},
            'landmark_ema':{ctx:landmark_summary([e for e in landmarks if e['landmark']['context']['ema_context']==ctx]) for ctx in sorted({e['landmark']['context']['ema_context'] for e in landmarks})},
            'prediction':prediction(landmarks),
            'prediction_without_same_bar_peak':prediction([e for e in landmarks if e['start_ms']!=e['landmark']['at_ms']])}
        (args.out/f'{path}_trajectories.json').write_text(json.dumps(rows),encoding='utf8')
        (args.out/f'{path}_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
        output[path]=summary
    (args.out/'summary.json').write_text(json.dumps(output,indent=2),encoding='utf8')
    (args.out/'manifest.json').write_text(json.dumps({'start_brt':manifest['start_brt'],'end_brt':manifest['end_brt'],
        'parity':'all existing systemic control trades: exit timestamp, reason, net, price and peak',
        'source_control':'BE_OFF_CB; ACT10_GAP5 file is only existing baseline serialization',
        'alternative_policy_replay':False,'forward_exact_trajectory':False,
        'selection':'sell-at-peak net positive after entry/exit spread and round-trip fees; no selection by final PnL',
        'landmark':'first observed >=1 entry ATR drawdown per record-peak episode; diagnostic only',
        'future_label':'next 60 complete 1m bars, excludes same-candle remainder; no decision uses future',
        'models':'fixed descriptive ridge logistic; train Jun/Jul, holdout Aug/Sep/Oct partial; no parameter search',
        'sha256':{'tool':digest(Path(__file__)),'baseline_manifest':digest(INPUT/'manifest.json'),'signals':digest(SIGNALS)},
        'cache_sha256':manifest['cache_hashes']},indent=2),encoding='utf8')


if __name__=='__main__':main()
