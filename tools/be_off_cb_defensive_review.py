"""Causal, isolated defensive diagnosis. Never writes operational state.

No alternative portfolio rules are implemented. Hypothetical exits and denied
signals keep baseline admissions, CB, slots and sizing fixed.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config_profiles import effective_config
from src.monitor.entry_engine import EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from tools.be_off_cb_deterioration_study import _signals
from tools.be_off_cb_exit_context_study import brt, exit_context
from tools.be_off_cb_fast_drop_audit import TrackingCircuitGuard
from tools.be_off_cb_fast_drop_systemic_replay import process_candle_systemic, _fast_decision
from tools.cohort_study import _load_config
from tools.ge_replay_study import OpenPosition, ReplayTrade, SignalEvent, WARMUP_CANDLES, load_ge_market_data, process_candle, run_universe
from tools.market_bot_replay import MINUTE_MS, NullLogger, ReplayExecutionClient, _bot_exit_config, _round_trip_fees_pct
from tools.market_selection_study import BinancePublicClient

OUT = ROOT / 'data/studies/be_off_cb_defensive_review/20261002'
MONTHS = ('2026-06','2026-07','2026-08','2026-09','2026-10')


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def mean(v):
    return statistics.fmean(v) if v else None


def median(v):
    return statistics.median(v) if v else None


def classify_pattern(monthly):
    if any(x['n'] < 10 for x in monthly):
        return 'amostra insuficiente'
    positive = sum(x['delta'] > 0 for x in monthly)
    return 'consistente' if positive == len(monthly) else 'promissor' if positive >= len(monthly)-1 else 'contraditório'


def recovery_rate(records, at_ms, window=20, days=None):
    eligible = sorted((r for r in records if r.get('crossed_ms') is not None and
                       r.get('closed_ms') is not None and r['closed_ms'] < at_ms and
                       (days is None or r['closed_ms'] >= at_ms-days*86400000)), key=lambda r:r['closed_ms'])
    if days is None:
        eligible = eligible[-window:]
    return {'n': len(eligible), 'rate': mean([r['exit_reason'] != 'HARD_STOP' for r in eligible]),
            'latest_outcome_ms': max((r['closed_ms'] for r in eligible), default=None)}


def hs_clusters(records, gap_minutes=60):
    hs = sorted((r for r in records if r['exit_reason']=='HARD_STOP'), key=lambda r:r['closed_ms'])
    groups=[]
    for r in hs:
        if not groups or r['closed_ms']-groups[-1][-1]['closed_ms'] > gap_minutes*MINUTE_MS:
            groups.append([])
        groups[-1].append(r)
    return [g for g in groups if len(g)>=2]


def active(records, at_ms, after_exits=False):
    return [r for r in records if r['opened_ms'] < at_ms and
            (r.get('closed_ms') is None or (r['closed_ms'] > at_ms if after_exits else r['closed_ms'] >= at_ms))]


def exit_economics(r, price, notional, spread, fees):
    fill=price*(1-spread/2/10000)
    pnl=(fill/r['entry_price']-1)*100
    net=notional*(pnl-fees)/100
    delta=net-r['net_usd'] if r.get('net_usd') is not None else None
    return {'hypothetical_price':fill,'hypothetical_pnl_pct':pnl,'hypothetical_net':net,
            'control_net':r.get('net_usd'),'delta':delta,'control_exit':r['exit_reason'],
            'control_closed_at':brt(r['closed_ms']) if r.get('closed_ms') else None}


class Review:
    def __init__(self, config, candles, signals, end):
        self.config=config; self.candles=candles; self.end=end
        self.minute=candles['1m']; self.opens=[c.open_time_ms for c in self.minute]
        self.index={c.boundary_ms:c for c in self.minute}
        self.five_boundaries=[c.boundary_ms for c in candles['5m']]
        self.context_cache={};self.signal={s.boundary_ms:s.signal for s in signals}
        self.notional=float(config['capital']['operational_balance_usdt'])*float(config['capital']['trade_size_pct'])/100
        self.spread=float(config.get('instrumentation',{}).get('market_bot_replay',{}).get('round_trip_spread_bps',5))
        self.fees=_round_trip_fees_pct(config)

    def context(self, at):
        i=bisect.bisect_right(self.five_boundaries,at)-1
        if i not in self.context_cache:
            # Shared official telemetry implementation; cutoff is precisely at.
            self.context_cache[i]=exit_context(self.candles['5m'],self.five_boundaries,at+MINUTE_MS,
                self.config.get('instrumentation',{}).get('market_context',{}))
        return self.context_cache[i]

    def context_fields(self, at):
        s=self.context(at)
        return {k:s.get(k) for k in ('latest_open_at_ms','latest_closed_at_ms','previous_closed_at_ms',
                    'ema_context','macd_context','ema50_direction','ema100_direction','ema200_direction',
                    'ema50','ema100','ema200','macd_line','macd_signal','macd_histogram','histogram_state')}

    def price_at(self, at):
        c=self.index.get(at)
        return c.close if c else None

    def new_position(self, opened, price=None):
        signal=self.signal[opened]
        entry=signal.price*(1+self.spread/2/10000) if price is None else price
        client=ReplayExecutionClient(self.spread/2)
        position=BotFullExitPosition(pair_id=f'diag-{opened}',symbol=signal.symbol,entry_price=entry,
            quantity=self.notional/entry,entry_order={'replay':True},open_ts=iso(opened),
            config=_bot_exit_config(self.config),client=client,logger=NullLogger(),entry_atr=signal.entry_atr,
            atr_timeframe=signal.atr_timeframe,atr_period=signal.atr_period,position_id=1,
            source_candle_open_time=signal.source_candle_open_time,position_notional_usdt=self.notional)
        return OpenPosition(position,client,opened,self.notional)

    def future(self, at, hours):
        n=hours*60
        values=[self.index.get(at+k*MINUTE_MS) for k in range(1,n+1)]
        if any(v is None for v in values):return {'complete':False,'observed_minutes':sum(v is not None for v in values)}
        price=self.price_at(at)
        return {'complete':True,'favorable_pct':(max(c.high for c in values)/price-1)*100,
                'adverse_pct':(min(c.low for c in values)/price-1)*100,
                'end_return_pct':(values[-1].close/price-1)*100}

    def standalone(self, opened, path, baseline=None, fast=False, elastic=False):
        rp=self.new_position(opened,baseline['entry_price'] if baseline else None)
        p=rp.position; trades=[];evaluated=set();cross=None; started=None; recovered=False; worst=0.; lost=False; closed_ms=None
        lo=bisect.bisect_left(self.opens,opened)
        for candle in self.minute[lo:]:
            if candle.boundary_ms>self.end:break
            at=candle.open_time_ms
            if elastic:
                ctx=self.context(at)['ema_context']; original=p.entry_price*.985
                if started and p.hard_stop_price is None and ctx!='LON':
                    close=self.context(at)['close']
                    if close<=original:
                        rp.client.current_price=close;p._close_at_market(close,'HARD_STOP_ELASTIC_CONTEXT_LOST',iso(at),original);lost=True;closed_ms=at
                    else:
                        p.hard_stop_price=original;p._refresh_effective_stop();recovered=True
                if p.status=='CLOSED':break
                previous=None
                points=(candle.open,candle.high,candle.low,candle.close) if path=='HIGH_FIRST' else (candle.open,candle.low,candle.high,candle.close)
                for point in points:
                    if point<=original and ctx=='LON' and p.hard_stop_price is not None:
                        started=started or candle.boundary_ms;p.hard_stop_price=None;p.effective_stop=p.review_stop;p.stop_type='review';p._refresh_effective_stop()
                    tick=p.effective_stop if previous is not None and previous>p.effective_stop and point<=p.effective_stop else point
                    if started:worst=min(worst,(tick/p.entry_price-1)*100)
                    rp.client.current_price=tick;p.on_tick(tick,iso(candle.boundary_ms));previous=point
                    if p.status=='CLOSED':break
            elif fast:
                # Reuse the corrected replay evaluator and normal-stop priority.
                # Tuple close timestamps match context_before's strict eligibility.
                ctx=self.context(at)
                context_index=[(ctx['latest_closed_at_ms'],ctx['ema_context'],ctx['macd_context'])]
                was=bool(evaluated)
                process_candle_systemic([rp],trades,candle,path,self.fees,True,evaluated,self.index,context_index)
                if not was and evaluated:
                    _,target,ema,velocity=_fast_decision(p,candle.boundary_ms,p.entry_price*.995,None,self.index,context_index,at)
                    cross={'crossed_ms':candle.boundary_ms,'velocity':velocity,'context':ema,
                           'snapshot':self.context_fields(at),'target':target}
            else:
                process_candle([rp],trades,candle,path,self.fees)
            if p.status=='CLOSED':
                # Engine close_ts may be wall-clock time of the modeled client.
                # As in ge_replay_study, use the simulated candle boundary.
                closed_ms=candle.boundary_ms
                break
        closed=p.status=='CLOSED'
        closed_ms=closed_ms if closed else None
        net=self.notional*((p.exit_price/p.entry_price-1)*100-self.fees)/100 if closed else None
        return {'opened_ms':opened,'closed_ms':closed_ms,'exit_reason':p.exit_reason if closed else 'CENSORED',
                'net_usd':net,'cross':cross,'elastic_started_ms':started,'elastic_recovered':recovered,
                'elastic_context_lost':lost,'worst_pct':worst,
                'extra_adverse_pct':max(0,-worst-1.5) if started else None,
                'extra_minutes':(closed_ms-baseline['closed_ms'])/MINUTE_MS if closed and baseline and baseline.get('closed_ms') is not None else None}

    def run_diagnostics(self, path, raw):
        records=raw['trades']; crises=raw['crises']; paused=set(raw['paused']); rows=[]
        for i,r in enumerate(records):
            if i%250==0:print(f'{path}: FAST/HS diagnostics {i}/{len(records)}',flush=True)
            clone=self.standalone(r['opened_ms'],path,r,fast=True)
            r['crossed_ms']=clone['cross']['crossed_ms'] if clone['cross'] else None
            r['fast_eligible']=clone['exit_reason']=='FAST_DROP'
            r['fast_audit']=clone['cross']
            if r['fast_eligible']:
                rows.append({'mechanism':'FAST_DROP_EMA','path':path,'month':brt(clone['closed_ms'])[:7],
                    'at_ms':clone['closed_ms'],'opened_ms':r['opened_ms'],'control_exit':r['exit_reason'],
                    'control_net':r['net_usd'],'hypothetical_net':clone['net_usd'],
                    'delta':clone['net_usd']-r['net_usd'] if r['net_usd'] is not None else None,
                    'entry_context':r['entry_context'],'trigger_context':clone['cross']['snapshot']})
            if r['exit_reason']=='HARD_STOP':
                ctx=self.context_fields(r['closed_ms']-MINUTE_MS);r['exit_context']=ctx
                if ctx['ema_context']=='LON':
                    e=self.standalone(r['opened_ms'],path,r,elastic=True)
                    rows.append({'mechanism':'HS_BULL_ELASTIC','path':path,'month':brt(r['closed_ms'])[:7],
                        'at_ms':r['closed_ms'],'opened_ms':r['opened_ms'],'control_exit':'HARD_STOP',
                        'control_net':r['net_usd'],'hypothetical_net':e['net_usd'],
                        'delta':e['net_usd']-r['net_usd'] if e['net_usd'] is not None else None,**e})
        # Earliest eligible trigger per target per variant: do not count a trade
        # repeatedly as several triggering hard stops occur in the same episode.
        seen=defaultdict(set); hs_events=[]
        for i,r in enumerate(records):
            if r['exit_reason']!='HARD_STOP':continue
            at=r['closed_ms'];quote=r['exit_price']/(1-self.spread/2/10000);ctx=r['exit_context']
            others=[q for q in records[i+1:] if q['opened_ms']<at and
                    (q.get('closed_ms') is None or q['closed_ms']>=at) and q['entry_price']>quote]
            hs_events.append({'at_ms':at,'opened_ms':r['opened_ms'],'negative_others':len(others),'context':ctx,
                              'price':quote,'control_net':r['net_usd']})
            variants=['SHO'] if ctx['ema_context']=='SHO' else ['BEA'] if ctx['ema_context']=='BEA' else ['OTHER']
            if ctx['ema_context'] in ('SHO','BEA'):variants.append('SHO+BEA')
            for variant in variants:
                for q in others:
                    if q['opened_ms'] in seen[variant]:continue
                    seen[variant].add(q['opened_ms'])
                    rows.append({'mechanism':'HS_BEAR_'+variant,'path':path,'month':brt(at)[:7],
                        'at_ms':at,'opened_ms':q['opened_ms'],'trigger_context':ctx,
                        **exit_economics(q,quote,self.notional,self.spread,self.fees)})
        crisis_rows=[]
        for at in crises:
            price=self.price_at(at);ctx=self.context_fields(at)
            prior=[r for r in records if r.get('closed_ms') is not None and r['closed_ms']<=at]
            last4=[r for r in prior if r['closed_ms']>at-4*3600000]
            equity=peak=0.
            for q in sorted(prior,key=lambda r:r['closed_ms']):equity+=q['net_usd'];peak=max(peak,equity)
            positions=active(records,at,True)
            recover=None
            for t in range(at+5*MINUTE_MS,min(at+6*3600000,self.end)+1,5*MINUTE_MS):
                s=self.context(t)
                if s['ema_context'] in ('LON','BUL') and s['macd_context'] in ('BU+','BE+'):
                    recover=t;break
            row={'at_ms':at,'path':path,'month':brt(at)[:7],'context':ctx,'open':len(positions),
                 'net_last4h':sum(q['net_usd'] for q in last4),'closes_last4h':len(last4),
                 'realized_dd':peak-equity,'prior_realized_net':equity,'recovery_context_ms':recover,
                 'recovery_minutes':(recover-at)/MINUTE_MS if recover else None,
                 'future':{str(h):self.future(at,h) for h in (1,2,4,6)},
                 'positions':[{'opened_ms':q['opened_ms'],'pnl_pct':(price/q['entry_price']-1)*100,
                               'hs_gap_entry_pct':(price/q['entry_price']-.985)*100,'control_exit':q['exit_reason']} for q in positions]}
            crisis_rows.append(row)
            for q in positions:
                if q['opened_ms'] in seen['CB_EXIT_ALL']:continue
                seen['CB_EXIT_ALL'].add(q['opened_ms'])
                rows.append({'mechanism':'CB_EXIT_ALL','path':path,'month':brt(at)[:7],'at_ms':at,
                             'opened_ms':q['opened_ms'],**exit_economics(q,price,self.notional,self.spread,self.fees)})
        blocked=[]
        for at in sorted(set(self.signal)&paused):
            prior_crises=[t for t in crises if t<=at]
            crisis=prior_crises[-1] if prior_crises else None
            if crisis is None:continue
            h=(at-crisis)/3600000
            print_progress=len(blocked)%250==0
            if print_progress:print(f'{path}: isolated CB denied signals {len(blocked)}',flush=True)
            outcome=self.standalone(at,path)
            blocked.append({'at_ms':at,'crisis_ms':crisis,'hours_after_trigger':h,
                            'bucket':'0-1h' if h<1 else '1-2h' if h<2 else '2-4h' if h<4 else '4-6h',
                            'month':brt(crisis)[:7],'path':path,**outcome})
        cluster_rows=[]
        for group in hs_clusters(records):
            start,finish=group[0]['closed_ms'],group[-1]['closed_ms'];at=start-MINUTE_MS
            price=self.index[start].open
            positions=active(records,at)
            snapshots=[]
            for offset in (-30,-15,-5,0):
                t=at+offset*MINUTE_MS;p=self.price_at(t);pp=active(records,t)
                losses=[(p/q['entry_price']-1)*100 for q in pp] if p else []
                snapshots.append({'at_ms':t,'offset_min':offset,'open':len(pp),'negative':sum(x<0 for x in losses),
                    'near_hs_within_0p3':sum(x<=-1.2 for x in losses),'mean_pnl_pct':mean(losses),
                    'worst_pnl_pct':min(losses,default=None),'context':self.context_fields(t),
                    'recovery20':recovery_rate(records,t),'recovery7d':recovery_rate(records,t,days=7)})
            prior_closed=[r for r in records if r.get('closed_ms') is not None and r['closed_ms']<start]
            equity=peak=0.
            for q in prior_closed:equity+=q['net_usd'];peak=max(peak,equity)
            pnl4=sum(q['net_usd'] for q in prior_closed if q['closed_ms']>start-4*3600000)
            ref=self.price_at(start-5*MINUTE_MS)
            fast_count=sum(r.get('fast_eligible',False) for r in group)
            bear_count=sum(e['negative_others']>0 and e['context']['ema_context']=='SHO' for e in hs_events if start<=e['at_ms']<=finish)
            bull_count=sum(r['exit_context']['ema_context']=='LON' for r in group)
            cb_events=[t for t in crises if start<=t<=finish]
            cluster_rows.append({'path':path,'month':brt(start)[:7],'start_ms':start,'end_ms':finish,
                'start_brt':brt(start),'end_brt':brt(finish),'hs':len(group),'net':sum(r['net_usd'] for r in group),
                'entry_BUL_BU_MINUS':sum(r['entry_context']['ema_context']=='BUL' and r['entry_context']['macd_context']=='BU-' for r in group),
                'entry_span_minutes':(max(r['opened_ms'] for r in group)-min(r['opened_ms'] for r in group))/MINUTE_MS,
                'velocity5_pct_per_min':((price/ref-1)*100/5) if ref else None,
                'dd_before':peak-equity,'closed_pnl4h_before':pnl4,
                'cb_active_before':start-MINUTE_MS in paused,'cb_triggers_during':cb_events,
                'fast_targets':fast_count,'bear_SHO_triggers':bear_count,'bull_LON_targets':bull_count,
                'unintercepted_by_exit_predicates':not(fast_count or bear_count or bull_count or cb_events),
                'snapshots':snapshots,'trades':[r['opened_ms'] for r in group]})
        # Comparator cohort: at least two positions below -0.5%; hourly-separated
        # observations, not a newly proposed gate or tuned threshold.
        risk=[]; live={}; entries=defaultdict(list);exits=defaultdict(list);last=-10**18
        for r in records:
            entries[r['opened_ms']].append(r)
            if r.get('closed_ms'):exits[r['closed_ms']].append(r['opened_ms'])
        hs_times=sorted(r['closed_ms'] for r in records if r['exit_reason']=='HARD_STOP')
        for c in self.minute:
            at=c.open_time_ms
            if at>self.end:break
            for key in exits.get(at,[]):live.pop(key,None)
            for q in entries.get(at,[]):live[q['opened_ms']]=q
            negative=sum(c.open/q['entry_price']-1<=-.005 for q in live.values())
            if negative>=2 and at-last>=60*MINUTE_MS:
                last=at; complete=at+60*MINUTE_MS<=self.end
                hs_next=bisect.bisect_right(hs_times,at+60*MINUTE_MS)-bisect.bisect_right(hs_times,at)
                risk.append({'at_ms':at,'path':path,'month':brt(at)[:7],'negative_0p5':negative,
                    'recovery20':recovery_rate(records,at),'recovery7d':recovery_rate(records,at,days=7),
                    'hs_next60':hs_next if complete else None,'context':self.context_fields(at)})
        return {'trades':records,'counterfactual_exits':rows,'hs_events':hs_events,'crises':crisis_rows,
                'blocked_signals':blocked,'clusters':cluster_rows,'risk_episodes':risk}


def summarize(rows):
    complete=[r for r in rows if r.get('delta') is not None]
    hs=[r for r in complete if r['control_exit']=='HARD_STOP']
    winners=[r for r in complete if r['control_exit'] in ('PROFIT_LOCK','TRAILING')]
    return {'n':len(rows),'resolved':len(complete),'pending':len(rows)-len(complete),
            'control_exits':dict(Counter(r.get('control_exit') for r in rows)),
            'hs_savings':sum(r['delta'] for r in hs),'winner_delta':sum(r['delta'] for r in winners),
            'winner_cost':sum(-min(0,r['delta']) for r in winners),
            'delta':sum(r['delta'] for r in complete),'delta_per_resolved':mean([r['delta'] for r in complete])}


def output_summary(results):
    sections=[]
    for path,d in results.items():
        mechanisms=sorted(set(r['mechanism'] for r in d['counterfactual_exits']))
        for name in mechanisms:
            selected=[r for r in d['counterfactual_exits'] if r['mechanism']==name]
            months={m:summarize([r for r in selected if r['month']==m]) for m in MONTHS}
            sections.append({'path':path,'mechanism':name,'months':months,'aggregate':summarize(selected),
                             'status':classify_pattern(list(months.values()))})
    return sections


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    config=deepcopy(effective_config(_load_config(ROOT/'config/config.yaml')))
    config.setdefault('risk',{})['breakeven']={'mode':'off'}
    start=int(datetime.fromisoformat('2026-06-01T00:00:00-03:00').timestamp()*1000)
    manifest_path=OUT/'manifest.json'
    end=(int(datetime.now(timezone.utc).timestamp()*1000)//MINUTE_MS)*MINUTE_MS
    if manifest_path.exists():end=json.loads(manifest_path.read_text())['end_ms']
    source_paths=['tools/ge_replay_study.py','tools/be_off_cb_fast_drop_systemic_replay.py',
                  'src/monitor/market_context.py','src/position/bot_full_engine.py','src/monitor/fast_drop_semantics.py']
    hashes={p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in source_paths}
    metadata={'start_ms':start,'end_ms':end,'start_brt':brt(start),'end_brt':brt(end),'config':config,'source_hashes':hashes}
    if manifest_path.exists() and json.loads(manifest_path.read_text())!=metadata:
        raise SystemExit('Frozen input mismatch: do not overwrite previous study.')
    manifest_path.write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    client=BinancePublicClient(str(config.get('market_data',{}).get('rest_url') or 'https://api.binance.com'),30)
    cache=ROOT/'data/studies/be_off_cb_deterioration/klines';candles={}
    for interval in ('1m','5m','15m'):
        print(f'Load {interval} through {brt(end)}',flush=True)
        candles[interval]=load_ge_market_data(client,str(config.get('symbol') or 'SOLUSDT'),interval,
            start-WARMUP_CANDLES*15*MINUTE_MS,end,cache,False)
    print('Generate unchanged baseline signals',flush=True)
    signal_path=OUT/'signals.json'
    if signal_path.exists():
        signals=[SignalEvent(r['boundary_ms'],EntrySignal(**r['signal'])) for r in json.loads(signal_path.read_text())]
    else:
        signals=_signals(config,candles,start,end)
        signal_path.write_text(json.dumps([asdict(s) for s in signals]),encoding='utf-8')
    coverage={'diagnostic_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'cache_hashes':{i:hashlib.sha256((cache/f'SOLUSDT_{i}.jsonl').read_bytes()).hexdigest() for i in candles},
              'coverage':{i:{'n':len(c),'first':brt(c[0].open_time_ms),'last':brt(c[-1].boundary_ms)} for i,c in candles.items()}}
    (OUT/'coverage.json').write_text(json.dumps(coverage,indent=2),encoding='utf-8')
    review=Review(config,candles,signals,end)
    results={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        checkpoint=OUT/f'baseline_{path}.json'
        if checkpoint.exists():raw=json.loads(checkpoint.read_text())
        else:
            guard=TrackingCircuitGuard(float(config['capital']['operational_balance_usdt']),review.notional)
            print(f'Original BE_OFF_CB baseline {path}',flush=True)
            replay=run_universe(name=f'BE_OFF_CB_{path}',lookback=0,config=config,signals=signals,
                execution_candles=candles['1m'],start_ms=start,end_ms=end,intrabar_path=path,
                round_trip_spread_bps=review.spread,admission_guard=guard.allows)
            records=[{**asdict(t),'net_usd':review.notional*t.net_pct/100} for t in replay.trades]
            for rp in replay.open_positions:
                records.append({'opened_ms':rp.opened_ms,'closed_ms':None,'entry_price':rp.position.entry_price,
                    'exit_reason':'CENSORED','exit_price':None,'net_usd':None})
            for r in records:
                r['entry_context']=review.context_fields(r['opened_ms'])
                r['source_candle']=review.signal[r['opened_ms']].source_candle_open_time
            raw={'trades':records,'crises':guard.crisis_starts,'paused':sorted(guard.paused_boundaries),
                 'closed':len(replay.trades),'open_end':len(replay.open_positions)}
            checkpoint.write_text(json.dumps(raw),encoding='utf-8')
        diagnostic=OUT/f'diagnostic_{path}.json'
        if diagnostic.exists():d=json.loads(diagnostic.read_text())
        else:
            d=review.run_diagnostics(path,raw);diagnostic.write_text(json.dumps(d),encoding='utf-8')
        results[path]=d
        print(f'{path}: clusters={len(d["clusters"])} crises={len(d["crises"])} cf_rows={len(d["counterfactual_exits"])}',flush=True)
    summaries=output_summary(results)
    (OUT/'summary.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
    for section in ('clusters','crises','hs_events','risk_episodes','counterfactual_exits','blocked_signals'):
        (OUT/f'{section}.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for d in results.values() for r in d[section]),encoding='utf-8')
    lines=['# BE_OFF_CB — revisão defensiva', '',f'Período BRT: {brt(start)} → {brt(end)}.',
           'Baseline contínuo, sem reset mensal; mês do trigger, HIGH/LOW separados.',
           'Diagnóstico isolado: CF não recalcula slots/admissões/CB/cooldown/equity. Não somar mecanismos entre si.',
           'HS-cluster usa quote do HS disparador; CB liquidation usa close1m após fechamentos; half-spread/fees iguais ao baseline.',
           'FAST reutiliza process_candle_systemic e fórmula causal corrigida; fills OHLC teóricos, não ticks reais.',
           'Cluster: >=2 HS consecutivos separados por <=60m; definição fixa, sem otimização.',
           'Recuperação: últimos20 crossers fechados e últimos7dias, closed_at estritamente anterior. Censurados fora da taxa.',
           'Retorno de contexto: primeiro 5m fechado com EMA LON/BUL e MACD BU+/BE+, sem regra de reabertura implementada.',
           'Sinais bloqueados simulados individualmente: não significam admissões viáveis simultaneamente nem perda realmente evitada.',
           'Toque/previsão OHLC tem resolução1m; outubro e horizontes finais são censurados. Nenhum threshold/cooldown alternativo foi testado.', '']
    for s in summaries:
        lines += [f'## {s["path"]} · {s["mechanism"]} · {s["status"]}', '',
                  '| mês | N | resolvidos | HS | PL | TRAIL | economia HS $ | custo winners $ | delta $ |',
                  '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for m,v in [*s['months'].items(),('ALL',s['aggregate'])]:
            c=v['control_exits'];lines.append(f'| {m} | {v["n"]} | {v["resolved"]} | {c.get("HARD_STOP",0)} | {c.get("PROFIT_LOCK",0)} | {c.get("TRAILING",0)} | {v["hs_savings"]:.4f} | {v["winner_cost"]:.4f} | {v["delta"]:.4f} |')
        lines.append('')
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print(f'Artifacts {OUT}',flush=True)


if __name__=='__main__':main()
