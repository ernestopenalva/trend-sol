"""Research-only idealized engine and fixed-scenario analysis.

Uses runtime admission methods, position engine, FAST predicates and CB clock.
No network, persistence of operational state, or real execution clients.
"""
from __future__ import annotations
import bisect
import json
import math
import random
import statistics
import sys
from pathlib import Path
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.fast_drop_selectors_study import *
from src.monitor.cb_replay_clock import CBReplayClock
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.circuit_breaker_shadow import CircuitBreakerPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.monitor.fast_drop_semantics import loss_reached
from src.monitor.price_structure import Hour, classify, TIDE, WAVE

SCENARIOS={'OPTIMISTIC':1.,'BASE':3.5,'ADVERSE':10.}

class CostClient(PhantomExecutionClient):
    def __init__(self,bps):
        super().__init__();self.bps=bps
    def market_sell(self,symbol,quantity,client_order_id):
        assert self._price and self._price>0
        price=self._price*(1-self.bps/10000)
        return {'status':'FILLED','executedQty':str(quantity),'cummulativeQuoteQty':str(quantity*price),
                'fills':[{'price':str(price),'qty':str(quantity),'commission':'0'}],'phantom':True}

class Memory:
    def _save_state(self):pass
    def _event(self,*args,**kwargs):pass
    def _event_at(self,*args,**kwargs):pass
    def _advance_clock(self,*args,**kwargs):
        self.circuit_breaker_active=self.clock.paused
        self.circuit_breaker_until=iso(self.clock.pause_until) if self.clock.paused else None
    def _block(self,event,signal,bucket,**kwargs):
        self.last_block=event
        return False
    def _open(self,signal,bucket):
        price=signal.price*(1+self.bps/10000)
        notional=self.config['capital']['operational_balance_usdt']*self.config['capital']['trade_size_pct']/100
        client=CostClient(self.bps)
        p=CircuitBreakerPosition(pair_id=f'{self.arm}-{signal.source_candle_open_time}',symbol='SOLUSDT',entry_price=price,
                quantity=notional/price,entry_order={'research':True},open_ts=signal.ts,config=self._exit_config(),client=client,
                logger=self.logger,entry_atr=signal.entry_atr,atr_timeframe=signal.atr_timeframe,atr_period=signal.atr_period,
                source_candle_open_time=signal.source_candle_open_time,position_notional_usdt=notional,
                no_progress_enabled=False,no_progress_tolerance_seconds=None,no_progress_tolerance_source='DISABLED')
        p.phantom=True;p.shadow_kind=self.shadow_kind;p.fast_drop_evaluated=False
        p._study_opened=int(datetime.fromisoformat(signal.ts).timestamp()*1000)
        p._study_context=self.latest_market_context['tf_5m']
        self.positions.append(p);self.entries_by_bucket[bucket]=self.entries_by_bucket.get(bucket,0)+1

class MemControl(Memory,CircuitBreakerShadow):pass
class MemPolicy(Memory,PolicyShadow):pass
class MemHist(Memory,EmaMacdHist1mShadow):pass

def memory(arm,cfg,bps):
    cls=MemControl if arm=='be_off_cb' else MemHist if arm=='ema_macd_hist_1m' else MemPolicy
    obj=object.__new__(cls)
    obj.arm=arm;obj.config=cfg;obj.settings=cfg['instrumentation'][arm+'_shadow'];obj.bps=bps
    obj.enabled=obj.accept_new_entries=True;obj._be_off=True
    obj.policy='MACD_BU_MINUS' if arm=='be_off_cb_macd_bu_minus' else 'EMA_MACD'
    obj.positions=[];obj.entries_by_bucket={};obj.max_simultaneous_positions=0
    obj.clock=CBReplayClock(float(obj.settings['initial_capital_usdt']),float(obj.settings['initial_capital_usdt']),float(obj.settings['initial_capital_usdt']))
    obj.last_signal_source=None;obj.last_input_ms=None;obj.circuit_breaker_active=False
    obj.blocked_circuit_breaker=obj.blocked_capacity=obj.blocked_same_5m=obj.blocked_spacing=0
    obj.latest_market_context={};obj.tactical_candles=[];obj.admission_audit={};obj.last_block=None
    obj.logger=NullLogger();obj.shadow_kind=arm.upper()+'_SHADOW'
    return obj

def on_point(p,point,previous,stamp,reference,context,fast,attempts):
    """One continuous segment, or the opening point (previous=None = gap).

    There is no interpolation between the preceding minute and the open.
    Stop activation occurs at modeled extrema before a subsequent segment.
    """
    stop=p.effective_stop
    target,_=fast_drop_values(p.entry_price,reference)
    attempt_point=point
    crossing=(previous is not None and previous>target and point<=target)
    if crossing:attempt_point=target
    if fast and not p.fast_drop_evaluated and loss_reached(p.entry_price,point):
        if reference is None:
            attempts.append({'source':p.source_candle_open_time,'at':stamp,'minute':stamp//MINUTE*MINUTE,'decision':'NOT_EVALUABLE','reasons':['REFERENCE_MISSING']})
        else:
            p.fast_drop_evaluated=True
            _,velocity=fast_drop_values(p.entry_price,reference)
            precedence=normal_stop_precedes_fast(stop,target)
            reasons=['NORMAL_STOP_PRECEDES'] if precedence else [name for name,ok in [('SPEED_BLOCKED',velocity<=-.1+1e-12),('EMA_BLOCKED',context.get('ema_context') in {'SHO','BEA'})] if not ok]
            attempts.append({'source':p.source_candle_open_time,'at':stamp,'minute':stamp//MINUTE*MINUTE,'decision':'NO_TRIGGER' if reasons else 'TRIGGER',
                             'reasons':reasons,'velocity':velocity,'reference':reference,'target':target,'ema':context.get('ema_context'),'normal_stop':stop})
            if not precedence and fast_drop_allowed(velocity,context.get('ema_context','UNAVAILABLE')):
                p._cb_market_ts=iso(stamp);p._update_trough(attempt_point,iso(stamp));p.client.set_price(attempt_point)
                p._close_at_market(attempt_point,'FAST_DROP',iso(stamp),target)
                return
    # Stop hit on a descending continuous segment: interpolate only within it.
    # Gap/open uses point, so execution may be worse than the stop.
    price=stop if previous is not None and stop is not None and previous>stop and point<=stop else point
    p.client.set_price(price);p.on_tick(price,iso(stamp))

def trade_record(p,closed_ms,fees,last_price=None,bps=0):
    opened=p._study_opened
    exit_price=float(p.exit_price) if closed_ms is not None else last_price*(1-bps/10000)
    net_pct=(exit_price/p.entry_price-1)*100-fees
    return {'source':p.source_candle_open_time,'opened_ms':opened,'closed_ms':closed_ms,
            'entry_price':p.entry_price,'exit_price':float(p.exit_price) if closed_ms is not None else None,
            'mark_price':last_price,'net':net_pct*p.position_notional_usdt/100,
            'net_pct':net_pct,'reason':str(p.exit_reason) if closed_ms is not None else 'OPEN',
            'notional':p.position_notional_usdt,'atr':p.entry_atr,'peak':p.highest_price,'trough':p.trough_price,
            'ema':p._study_context.get('ema_context'),'macd':p._study_context.get('macd_context'),
            'fast_evaluated':p.fast_drop_evaluated,'minutes':((closed_ms or p._study_end)-opened)/MINUTE}

def run(arm,cfg,minutes,signals,contexts,bps,path,fast,start,end):
    obj=memory(arm,cfg,bps);fees=2*float(cfg['fees']['taker_fee_pct']);pending=[];records=[];admissions=[];attempts=[]
    smap={s['boundary_ms']:s for s in signals};cmap={c.boundary_ms:c for c in minutes}
    ctx_times=sorted(contexts);realized=0.;peak=0.;dd=0.;real_peak=0.;real_dd=0.;exposure=0.;max_committed=0.
    daily={};minute_equity=[];last_context={};block_counts=Counter();mid_equity=0.
    midpoint=start+(end-start)//2
    for i,c in enumerate(minutes):
        at=c.open_time_ms
        if at<start:continue
        if c.boundary_ms>end:break
        obj.clock.minute(at,[(at,n) for t,n in pending if t==at]);pending=[]
        ix=bisect.bisect_right(ctx_times,at)-1
        context=contexts[ctx_times[ix]] if ix>=0 else {}
        obj.latest_market_context={'tf_5m':context}
        prior=[cmap[t] for t in (at-MINUTE,at) if t in cmap]
        obj.tactical_candles=[{'open_time':x.open_time_ms,'close_time':x.close_time_ms,'open':x.open,'close':x.close} for x in prior]
        if at in smap:
            s=EntrySignal(**smap[at]['signal']);obj.last_block=None
            accepted=obj._process_signal(s)
            admissions.append({'source':s.source_candle_open_time,'at':at,'accepted':bool(accepted),'reason':'ADMITTED' if accepted else obj.last_block})
            if not accepted:block_counts[obj.last_block]+=1
        max_committed=max(max_committed,sum(p.position_notional_usdt for p in obj.positions))
        exposure+=sum(p.position_notional_usdt for p in obj.positions)
        ref_candle=cmap.get(c.boundary_ms-5*MINUTE)
        reference=ref_candle.close if ref_candle is not None else None
        values=[c.open,c.high,c.low,c.close] if path=='HIGH_FIRST' else [c.open,c.low,c.high,c.close]
        for p in list(obj.positions):
            previous=None
            for j,point in enumerate(values):
                stamp=at+[0,20000,40000,59999][j]
                on_point(p,point,previous,stamp,reference,context,fast,attempts)
                if p.status=='CLOSED':
                    r=trade_record(p,stamp,fees);records.append(r);realized+=r['net'];pending.append((c.boundary_ms,r['net']))
                    real_peak=max(real_peak,realized);real_dd=max(real_dd,real_peak-realized)
                    break
                assert p.status=='OPEN',p.status
                previous=point
            if p.status=='CLOSED':obj.positions.remove(p)
        unrealized=sum(((c.close*(1-bps/10000)/p.entry_price-1)*100-fees)*p.position_notional_usdt/100 for p in obj.positions)
        equity=realized+unrealized;peak=max(peak,equity);dd=max(dd,peak-equity)
        day=datetime.fromtimestamp((c.boundary_ms-1)/1000,BRT).date().isoformat()
        daily[day]=equity
        if c.boundary_ms<=midpoint:mid_equity=equity
        # 1-minute MTM path exported for honest exposure/DD diagnostics.
        minute_equity.append(equity)
    for p in obj.positions:
        p._study_end=end;records.append(trade_record(p,None,fees,minutes[-1].close,bps))
    # Cutoff boundary has no admission; apply final closes for clock audit.
    obj.clock.minute(end,[(end,n) for t,n in pending if t==end])
    dprev=0.;daily_delta={}
    for day,value in daily.items():daily_delta[day]=value-dprev;dprev=value
    wins=sum(r['net'] for r in records if r['closed_ms'] is not None and r['net']>0)
    losses=-sum(r['net'] for r in records if r['closed_ms'] is not None and r['net']<0)
    stats={'admissions':len(records),'closed':sum(r['closed_ms'] is not None for r in records),'open':len(obj.positions),
           'realized_net':realized,'open_mtm_liquidation_net':equity-realized,'net_total':equity,'net_per_admission':equity/len(records) if records else None,
           'pf_closed':wins/losses if losses else None,'dd_minute_mtm':dd,'dd_realized':real_dd,
           'notional_minutes_upper':exposure,'max_committed':max_committed,'mean_committed':exposure/len(minute_equity) if minute_equity else 0,
           'reasons':dict(Counter(r['reason'] for r in records)),'median_minutes':statistics.median(r['minutes'] for r in records) if records else None,
           'crises':obj.clock.crises,'blocked':dict(block_counts)}
    stats['equity_at_midpoint']=mid_equity
    stats['notional_minutes_modeled']=sum(r['notional']*r['minutes'] for r in records)
    return {'arm':arm,'path':path,'fast':fast,'cost_bps_per_side':bps,'stats':stats,'trades':records,'admissions':admissions,'attempts':attempts,'daily_contributions':daily_delta,
            'exposure_note':'Each held minute charged full notional: conservative upper time exposure; exits have modeled intraminute timestamps.'}

def engine_checks():
    # Exercise causal entry minute, gaps, priority, one-shot and policy reuse
    # without consuming empirical economic outcomes.
    cfg=read(OUT/'config.json');cfg['risk']['breakeven']={'mode':'off'}
    obj=memory('be_off_cb',cfg,3.5)
    obj.latest_market_context={'tf_5m':{'ema_context':'SHO','macd_context':'BE-'}}
    signal=EntrySignal('SOLUSDT',100.,iso(START),START-MINUTE,.2,'1m',14)
    assert obj._process_signal(signal)
    p=obj.positions[0];attempts=[]
    target=p.entry_price*.995
    on_point(p,target-.1,None,START+1,100.1,{'ema_context':'SHO'},True,attempts)
    assert p.status=='CLOSED' and p.exit_reason=='FAST_DROP'
    assert abs(p.exit_price-(target-.1)*(1-3.5/10000))<1e-10,'Gap fill interpolated'
    obj=memory('be_off_cb',cfg,0);obj.latest_market_context={'tf_5m':{}};obj._process_signal(signal);p=obj.positions[0];a=[]
    on_point(p,99.5,100.,START+1,100.,{'ema_context':'LON'},True,a)
    assert p.fast_drop_evaluated and p.status=='OPEN'
    on_point(p,99.4,99.5,START+2,100.,{'ema_context':'SHO'},True,a)
    assert p.status=='OPEN' and len(a)==1,'One shot retried'
    obj=memory('be_off_cb',cfg,0);obj.latest_market_context={'tf_5m':{}};obj._process_signal(signal);p=obj.positions[0]
    p.hard_stop_price=99.6;p._refresh_effective_stop();a=[]
    on_point(p,99.4,100.,START+1,100.,{'ema_context':'SHO'},True,a)
    assert p.exit_reason=='HARD_STOP' and abs(p.exit_price-99.6)<1e-10
    # Existing current candle low is not processed for a just-created entry.
    candle=MarketCandle(START-MINUTE,START-1,100.,200.,1.,100.,0.,1)
    nextc=MarketCandle(START,START+MINUTE-1,100.,100.1,99.9,100.,0.,1)
    sample=[{'boundary_ms':START,'signal':asdict(signal)}]
    r=run('be_off_cb',cfg,[candle,nextc],sample,{START:{'ema_context':'SHO'}},0,'HIGH_FIRST',True,START,START+MINUTE)
    assert r['stats']['open']==1 and not r['attempts'],'Pre-entry low leaked'
    dump(OUT/'engine_checks.json',{'status':'PASS','checks':['gap costs','one-shot','stop priority','entry-minute causality'],'script_sha256':digest(Path(__file__))})

def replay():
    assert digest(PREREG)==PREREG_HASH
    f=read(OUT/'fidelity.json')
    assert not any(x['status']=='IMPLEMENTATION_MISMATCH' for x in f['fast_correctness'])
    engine_checks()
    cfg=read(OUT/'config.json');cfg['risk']['breakeven']={'mode':'off'}
    minutes=load_candle_cache(OUT/'market/SOLUSDT_1m.jsonl');signals=read(OUT/'signals.json')
    contexts={int(k):v for k,v in read(OUT/'contexts.json').items()};end=read(OUT/'market_manifest.json')['end_ms']
    for arm in ARMS:
        if not f['controls'][arm]['qualified']:
            print('NOT_INTERPRETABLE',arm,'control gate failed',flush=True);continue
        for scenario,bps in SCENARIOS.items():
            for path in ['HIGH_FIRST','LOW_FIRST']:
                for fast in [False,True]:
                    file=OUT/f'runs/{arm}_{scenario}_{path}_{"FAST" if fast else "CONTROL"}.json'
                    if file.exists():continue
                    print('RUN',arm,scenario,path,fast,flush=True)
                    result=run(arm,cfg,minutes,signals,contexts,bps,path,fast,START,end)
                    dump(file,result)
                    print('DONE',result['stats'],flush=True)

def percentile(values,q):
    values=sorted(values);p=(len(values)-1)*q;lo=int(p);hi=min(lo+1,len(values)-1)
    return values[lo]+(values[hi]-values[lo])*(p-lo)

def bootstrap(values):
    if len(values)<2:return {'n':len(values),'ci95':None,'status':'NOT_ESTIMABLE: fewer than two distinct resampling blocks'}
    rng=random.Random(20261018);n=len(values)
    draws=[sum(values[rng.randrange(n)] for _ in range(n)) for _ in range(5000)]
    return {'n':n,'ci95':[percentile(draws,.025),percentile(draws,.975)],'replicates':5000,'seed':20261018}

def report():
    fidelity=read(OUT/'fidelity.json');signals=read(OUT/'signals.json');end=read(OUT/'market_manifest.json')['end_ms']
    episodes={};episode=0;prior=None
    for s in signals:
        at=s['boundary_ms']
        if prior is not None and at-prior>6*3600000:episode+=1
        episodes[s['signal']['source_candle_open_time']]=episode;prior=at
    ecount=episode+1
    hs=load_candle_cache(OUT/'market/SOLUSDT_1h.jsonl');qs=load_candle_cache(OUT/'market/SOLUSDT_15m.jsonl')
    hstarts=[c.open_time_ms for c in hs];qstarts=[c.open_time_ms for c in qs];regimes={}
    def regime(at):
        if at not in regimes:
            labels=[]
            for candles,starts,rule in [(hs,hstarts,TIDE),(qs,qstarts,WAVE)]:
                ix=bisect.bisect_right(starts,at//rule.interval_ms*rule.interval_ms-rule.interval_ms)
                subset=candles[max(0,ix-rule.count):ix]
                labels.append(classify([Hour(c.open_time_ms,c.high,c.low) for c in subset],at,'REPLAY',rule)['label'])
            regimes[at]=labels
        return regimes[at]
    result={'preregister_sha256':digest(PREREG),'episodes':ecount,'arms':{},'overlap':{}}
    files={}
    for arm in ARMS:
        if not fidelity['controls'][arm]['qualified']:
            result['arms'][arm]={'classification':'NOT_INTERPRETABLE','reason':'Failed preregistered control reconstruction gate'};continue
        cells={}
        for scenario in SCENARIOS:
            for path in ['HIGH_FIRST','LOW_FIRST']:
                key=scenario+'_'+path
                runs=[read(OUT/f'runs/{arm}_{scenario}_{path}_{kind}.json') for kind in ['CONTROL','FAST']]
                control,var=runs;files[(arm,scenario,path)]=runs
                cb={t['source']:t for t in control['trades']};vb={t['source']:t for t in var['trades']}
                common=set(cb)&set(vb);only_c=set(cb)-set(vb);only_v=set(vb)-set(cb)
                contributions={s:(vb[s]['net'] if s in vb else 0)-(cb[s]['net'] if s in cb else 0) for s in set(cb)|set(vb)}
                total=var['stats']['net_total']-control['stats']['net_total']
                assert abs(sum(contributions.values())-total)<1e-7
                daily={d:var['daily_contributions'].get(d,0)-control['daily_contributions'].get(d,0) for d in sorted(set(control['daily_contributions'])|set(var['daily_contributions']))}
                assert abs(sum(daily.values())-total)<1e-7
                episode_values={e:0. for e in range(ecount)}
                for s,net in contributions.items():episode_values[episodes[s]]+=net
                triggers=[t for t in var['trades'] if t['reason']=='FAST_DROP'];dest=[];struct=defaultdict(lambda:{'n':0,'triggers':0,'delta':0.,'episodes':set()})
                for s in set(cb)|set(vb):
                    t=vb.get(s) or cb[s];tide,wave=regime(t['opened_ms']);name=tide+'×'+wave
                    struct[name]['n']+=1;struct[name]['delta']+=contributions[s];struct[name]['episodes'].add(episodes[s])
                    struct[name]['triggers']+=int(s in vb and vb[s]['reason']=='FAST_DROP')
                for t in triggers:
                    c=cb.get(t['source']);mfe=(c['peak']-c['entry_price'])/c['atr'] if c and c.get('atr') else None
                    dest.append({'source':t['source'],'control_reason':c['reason'] if c else 'NOT_ADMITTED','net_delta':t['net']-c['net'] if c else None,
                                 'control_mfe_atr':mfe,'mfe_group':'UNKNOWN' if mfe is None else 'LT8' if mfe<8 else 'GE8',
                                 'control_net':c['net'] if c else None,'fast_net':t['net'],'trigger_minute':t['closed_ms']//MINUTE*MINUTE})
                saving=[d['net_delta'] for d in dest if d['control_reason']=='HARD_STOP']
                sacrifice=[-d['net_delta'] for d in dest if d['control_reason'] in ['PROFIT_LOCK','TRAILING']]
                S=statistics.mean(saving) if saving else None;C=statistics.mean(sacrifice) if sacrifice else None
                binary=len(saving)+len(sacrifice)
                trigger_days=set(datetime.fromtimestamp(t['closed_ms']/1000,BRT).date().isoformat() for t in triggers)
                trigger_episodes=set(episodes[t['source']] for t in triggers)
                positive_days=sorted([(d,n) for d,n in daily.items() if n>0],key=lambda x:x[1],reverse=True)
                all_trades_sorted=sorted(contributions.items(),key=lambda x:x[1],reverse=True)
                direct=sum(vb[s]['net']-cb[s]['net'] for s in common if vb[s]['reason']=='FAST_DROP')
                mid=var['stats']['equity_at_midpoint']-control['stats']['equity_at_midpoint']
                cell={'control':control['stats'],'variant':var['stats'],'delta':total,'delta_per_variant_admission':total/len(vb) if vb else None,
                      'attempt_decisions':dict(Counter(a['decision'] for a in var['attempts'])),
                      'attempt_reasons_overlapping':dict(Counter(reason for a in var['attempts'] for reason in a['reasons'])),
                      'positions_without_valid_fast_evaluation':sum(not t['fast_evaluated'] for t in var['trades']),
                      'common':len(common),'control_only':len(only_c),'variant_only':len(only_v),
                      'paired_delta':sum(vb[s]['net']-cb[s]['net'] for s in common),'paired_mean_delta':statistics.mean(vb[s]['net']-cb[s]['net'] for s in common) if common else None,
                      'paired_fast_direct_delta':direct,'common_nonfast_delta':sum(vb[s]['net']-cb[s]['net'] for s in common if vb[s]['reason']!='FAST_DROP'),
                      'exclusive_delta':sum(vb[s]['net'] for s in only_v)-sum(cb[s]['net'] for s in only_c),
                      'daily_bootstrap':bootstrap(list(daily.values())),'episode_bootstrap':bootstrap(list(episode_values.values())),
                      'half_deltas':[mid,total-mid],'leave_one_day_out_min':min((total-n for n in daily.values()),default=None),
                      'leave_one_day_out':{d:total-n for d,n in daily.items()},
                      'exclude_top3_positive_days':total-sum(n for d,n in positive_days[:3]),
                      'top3_positive_days':positive_days[:3],'bottom3_days':sorted(daily.items(),key=lambda x:x[1])[:3],
                      'top5_trade_contributions':all_trades_sorted[:5],'bottom5_trade_contributions':all_trades_sorted[-5:],
                      'top5_positive_share':sum(max(0,n) for s,n in all_trades_sorted[:5])/sum(max(0,n) for n in contributions.values()) if any(n>0 for n in contributions.values()) else None,
                      'top3_positive_day_share':sum(n for d,n in positive_days[:3])/sum(n for d,n in positive_days) if positive_days else None,
                      'trigger_days':len(trigger_days),'trigger_episodes':len(trigger_episodes),'triggers':len(triggers),
                      'destinations':dict(Counter(d['control_reason'] for d in dest)),'trigger_details':dest,
                      'saving_mean_hs':S,'sacrifice_mean_pl_trail':C,'hs_share_binary':len(saving)/binary if binary else None,
                      'hs_share_all_triggers':len(saving)/len(triggers) if triggers else None,
                      'break_even_hs_share_binary':C/(S+C) if S is not None and C is not None and S>0 and C>0 else None,
                      'mfe':{g:{'n':len([d for d in dest if d['mfe_group']==g]),'delta':sum(d['net_delta'] or 0 for d in dest if d['mfe_group']==g)} for g in ['LT8','GE8','UNKNOWN']},
                      'regimes':{k:{**v,'episodes':len(v['episodes'])} for k,v in struct.items()},
                      'daily_contributions':daily}
                cells[key]=cell
        bases=[cells['BASE_'+p] for p in ['HIGH_FIRST','LOW_FIRST']]
        positive=all(c['delta']>0 for c in bases);negative=all(c['delta']<0 for c in bases)
        robust=(all(c['delta']>0 and min(c['half_deltas'])>0 and c['leave_one_day_out_min']>0 and c['exclude_top3_positive_days']>0 and c['trigger_episodes']>=10 for c in cells.values())
                and all(c['daily_bootstrap']['ci95'] is not None and c['episode_bootstrap']['ci95'] is not None and c['daily_bootstrap']['ci95'][0]>0 and c['episode_bootstrap']['ci95'][0]>0 for c in bases))
        classification='ROBUST' if robust else 'PROMISING' if positive else 'UNFAVORABLE' if negative else 'INCONCLUSIVE'
        result['arms'][arm]={'classification':classification,'cells':cells}
    # Overlap is reported for every fixed cost/path, never pooled as independent.
    eligible=[a for a in ARMS if fidelity['controls'][a]['qualified']]
    for scenario in SCENARIOS:
        for path in ['HIGH_FIRST','LOW_FIRST']:
            result['overlap'][scenario+'_'+path]={}
            for kind in ['CONTROL_ENTRIES','FAST_ENTRIES','TRIGGERS']:
                sets={a:{t['source'] for t in files[(a,scenario,path)][0 if kind=='CONTROL_ENTRIES' else 1]['trades'] if kind!='TRIGGERS' or t['reason']=='FAST_DROP'} for a in eligible}
                result['overlap'][scenario+'_'+path][kind]={a:{b:{'intersection':len(sets[a]&sets[b]),'union':len(sets[a]|sets[b]),'jaccard':len(sets[a]&sets[b])/len(sets[a]|sets[b]) if sets[a]|sets[b] else None} for b in eligible} for a in eligible}
    fw=read(OUT/'forward.json');recon={}
    for arm in eligible:
        obs={r['source_candle_open_time']:r for r in fw['ledgers'][arm] if FORWARD<=int(datetime.fromisoformat(r['opened_at']).timestamp()*1000)<end}
        # Include still-open admissions from checkpoint without inventing exits.
        obs.update({r['source_candle_open_time']:r for r in fw['states'][arm].get('positions',[]) if FORWARD<=int(datetime.fromisoformat(r.get('open_ts') or r.get('opened_at')).timestamp()*1000)<end})
        recon[arm]={}
        for path in ['HIGH_FIRST','LOW_FIRST']:
            sim=files[(arm,'BASE',path)][0]
            sim_set={r['source']:r for r in sim['trades'] if FORWARD<=r['opened_ms']<end};common=set(sim_set)&set(obs)
            decisions={a['source']:a for a in sim['admissions'] if FORWARD<=a['at']<end}
            reason=Counter(decisions.get(s,{}).get('reason','NO_RECONSTRUCTED_OPPORTUNITY') for s in set(obs)-set(sim_set))
            details=[]
            for s in common:
                a=sim_set[s];b=obs[s];closed=b.get('closed_at') is not None
                details.append({'source':s,'sim_reason':a['reason'],'observed_reason':b.get('exit_reason','OPEN'),
                                'reason_matches':a['reason']==b.get('exit_reason','OPEN'),
                                'entry_price_difference':a['entry_price']-b['entry_price'],
                                'net_delta_usdt':a['net']-b['net_pnl_pct']*b.get('position_notional_usdt',20)/100 if closed else None})
            recon[arm][path]={'common':len(common),'observed':len(obs),'simulated':len(sim_set),
                             'recall':len(common)/len(obs) if obs else None,'precision':len(common)/len(sim_set) if sim_set else None,
                             'observed_only_reasons':dict(reason),'sim_only':len(set(sim_set)-set(obs)),
                             'reason_agreement_common':sum(x['reason_matches'] for x in details)/len(details) if details else None,
                             'details':details,'classification':'Different initial CB/positions, execution costs and OHLC trajectory; not claimed runtime parity.'}
    result['forward_portfolio_reconciliation']=recon
    # Compare actual evaluation minute/reason against the modeled attempts.
    # Input-formula parity above is a separate gate from trajectory parity here.
    actual=fw['fast_telemetry'];fast_recon={}
    if 'be_off_cb' in eligible:
        for path in ['HIGH_FIRST','LOW_FIRST']:
            sim=files[('be_off_cb','BASE',path)][1]
            attempts={a['source']:a for a in sim['attempts'] if a['decision']!='NOT_EVALUABLE'}
            admitted={t['source']:t for t in sim['trades']}
            details=[]
            for r in actual:
                if r.get('intelligence')!='FAST_DROP':continue
                source=r['source_candle_open_time'];a=attempts.get(source);at=int(datetime.fromisoformat(r['ts']).timestamp()*1000)
                match=bool(a and a['minute']==at//MINUTE*MINUTE and a['decision']==r['decision'] and a['reasons']==r['reasons'])
                if match:category='DECISION_REASON_MINUTE_MATCH'
                elif source not in admitted:category='DIFFERENT_ADMISSIONS_OR_INHERITED_POSITION'
                elif r['decision']=='NOT_EVALUABLE':category='REFERENCE_AVAILABILITY'
                elif not a:category='DIFFERENT_PRICE_PATH_STOP_OR_ENTRY_STATE'
                elif a.get('ema')!=r.get('values',{}).get('ema_context_used'):category='CONTEXT_AVAILABILITY_OR_HISTORY'
                else:category='PRICE_PATH_COSTS_FIRST_CROSSING_OR_POSITION_STATE'
                details.append({'source':source,'actual_minute':at//MINUTE*MINUTE,'actual_decision':r['decision'],'actual_reasons':r['reasons'],
                                'ideal_attempt':a,'classification':category,
                                'note':'Candidate explanation, not proof of causal attribution; observed-input formula gate separately passed.'})
            fast_recon[path]={'counts':dict(Counter(d['classification'] for d in details)),'details':details}
    result['fast_minute_reason_reconciliation']=fast_recon
    observed_sources={}
    for arm in ARMS:
        rows=fw['ledgers'][arm]+fw['states'][arm].get('positions',[])
        observed_sources[arm]={r['source_candle_open_time'] for r in rows if FORWARD<=int(datetime.fromisoformat(r.get('opened_at') or r.get('open_ts')).timestamp()*1000)<end}
    result['forward_entry_overlap_all_four']={a:{b:{'intersection':len(observed_sources[a]&observed_sources[b]),'union':len(observed_sources[a]|observed_sources[b]),
                                                        'jaccard':len(observed_sources[a]&observed_sources[b])/len(observed_sources[a]|observed_sources[b]) if observed_sources[a]|observed_sources[b] else None} for b in ARMS} for a in ARMS}
    dump(OUT/'summary.json',result)
    render_report(result,fidelity)

def render_report(summary,fidelity):
    manifest=read(OUT/'market_manifest.json')
    cutoff=datetime.fromtimestamp((manifest['end_ms']-1)/1000,BRT).isoformat()
    midpoint=START+(manifest['end_ms']-START)//2
    labels={'PROMISING':'PROMISSOR EXPLORATÓRIO','ROBUST':'ROBUSTO','INCONCLUSIVE':'INCONCLUSIVO','UNFAVORABLE':'DESFAVORÁVEL','NOT_INTERPRETABLE':'NÃO INTERPRETÁVEL'}
    lines=['# FAST_DROP × seletores — simulação histórica idealizada', '',
           '**Estudo exploratório. Não reproduz integralmente o runtime, não autoriza produção nem seleciona vencedor.**','',
           f'Período econômico: **01/06/2026 00:00 BRT até {cutoff}**. {manifest["count_1m"]:,} candles 1m incluindo warm-up; {(manifest["end_ms"]-START)//MINUTE:,} minutos no período econômico; zero lacunas internas. Corte congelado antes da análise. Ponto médio das metades: {datetime.fromtimestamp(midpoint/1000,BRT).isoformat()}.', '',
           'Protocolo: ../PREREGISTER_IDEALIZED.md; hash '+digest(PREREG)+'.',
           'Mercado: '+json.dumps(read(OUT/'market_manifest.json'),ensure_ascii=False), '',
           '## Fidelidade antes da economia','',
           '| Controle | N forward | Sinal reconstruído | EMA/MACD | Passa seletor | Qualificado |','|---|---:|---:|---:|---:|---|']
    for arm,g in fidelity['controls'].items():
        lines.append(f"| {arm} | {g['n']} | {g['signal_recall']:.2%} | {g['context_agreement']:.2%} | {g['selector_pass_rate']:.2%} | {g['qualified']} |")
    lines+=['','Gate FAST (inputs observados): '+str(Counter(x['status'] for x in fidelity['fast_correctness']))+'.',
            'As diferenças de disponibilidade/contexto observadas estão individualizadas em fidelity.json. Não são automaticamente erros da simulação idealizada.',
            'Qualificação acima refere-se ao gate pré-registrado de oportunidades/contextos/filtros, não a paridade integral de carteiras. Concordância de contexto usa correspondências de sinal avaliáveis; entradas sem sinal reconstruído também são reportadas e não foram removidas do recall.',
            '', 'Divergências individuais do gate (source e contexto):', '',
            '| Controle | Source | Sinal corresponde? | EMA observada→ideal | MACD observado→ideal | Passa seletor ideal? |',
            '|---|---:|---|---|---|---|']
    for arm,g in fidelity['controls'].items():
        for r in g['details']:
            if not(r['signal_match'] and r['context_match'] and r['policy_pass']):
                lines.append(f"| {arm} | {r['source']} | {r['signal_match']} | {r['observed_ema']}→{r['reconstructed_ema']} | {r['observed_macd']}→{r['reconstructed_macd']} | {r['policy_pass']} |")
    lines += [
            '', '## Economia por par e cenário','',
            '| Seletor | Cenário | Caminho | C closed/open | F closed/open | Net C | Net F | Delta | FAST |','|---|---|---|---:|---:|---:|---:|---:|---:|']
    for arm,a in summary['arms'].items():
        if 'cells' not in a:continue
        for k,c in a['cells'].items():
            co,v=c['control'],c['variant']
            lines.append(f"| {arm} | {k.split('_')[0]} | {'HIGH_FIRST' if 'HIGH_FIRST' in k else 'LOW_FIRST'} | {co['closed']}/{co['open']} | {v['closed']}/{v['open']} | {co['net_total']:.6f} | {v['net_total']:.6f} | {c['delta']:+.6f} | {c['triggers']} |")
    for arm,a in summary['arms'].items():
        lines+=['',f'## {arm}: {labels[a["classification"]]}','']
        if 'cells' not in a:
            lines.append(a['reason']);continue
        for path in ['HIGH_FIRST','LOW_FIRST']:
            c=a['cells']['BASE_'+path]
            lines += [f'### Base / {path}', '',
                      f"Delta {c['delta']:+.6f} USDT; IC95% dia {c['daily_bootstrap']['ci95']}; IC por episódio não estimável (um bloco).",
                      f"Metades: {c['half_deltas']}; menor leave-one-day-out {c['leave_one_day_out_min']}; sem top3 dias positivos {c['exclude_top3_positive_days']}.",
                      f"Pareados {c['common']}, exclusivos controle/FAST {c['control_only']}/{c['variant_only']}. Delta pareado {c['paired_delta']}; direto FAST pareado {c['paired_fast_direct_delta']}; exclusivos {c['exclusive_delta']}.",
                      f"Disparos {c['triggers']}, dias {c['trigger_days']}, episódios {c['trigger_episodes']}. Destinos: {c['destinations']}.",
                      f"Avaliações modeladas: {c['attempt_decisions']}; motivos (podem sobrepor): {c['attempt_reasons_overlapping']}; posições sem avaliação válida FAST: {c['positions_without_valid_fast_evaluation']} (não implica reprovação por EMA/speed).",
                      f"Economia média HS {c['saving_mean_hs']}; custo médio PL/TRAIL {c['sacrifice_mean_pl_trail']}; proporção HS binária {c['hs_share_binary']}; equilíbrio binário {c['break_even_hs_share_binary']}.",
                      f"Proporção HS entre TODOS os acionamentos: {c['hs_share_all_triggers']}; destinos sem par/abertos não foram atribuídos a HS ou winners.",
                      f"Top3 dias positivos {c['top3_positive_days']}; piores dias {c['bottom3_days']}.",
                      f"Top5 contribuições por source {c['top5_trade_contributions']}; piores cinco {c['bottom5_trade_contributions']}.",
                      f"MFE do controle: {c['mfe']}. Diagnóstico ex post, sem filtro 8 ATR na admissão.",
                      f"Concentração: top5 sources respondem por {c['top5_positive_share']} da soma das contribuições positivas; top3 dias por {c['top3_positive_day_share']} da soma dos dias positivos. Frações não usam net como denominador.",
                      '', '| Métrica | Controle | FAST_DROP |','|---|---:|---:|']
            for label,key in [('Admissões','admissions'),('Fechados','closed'),('Abertos','open'),('Realizado líquido USDT','realized_net'),('MTM líquido posições abertas USDT','open_mtm_liquidation_net'),('Net/admissão USDT','net_per_admission'),('Profit Factor fechados','pf_closed'),('DD MTM 1m USDT','dd_minute_mtm'),('DD realizado USDT','dd_realized'),('Capital máximo comprometido USDT','max_committed'),('Notional×minutos modelados','notional_minutes_modeled'),('Notional×minutos upper','notional_minutes_upper'),('Duração mediana minutos','median_minutes'),('Crises CB','crises')]:
                lines.append(f"| {label} | {c['control'][key]} | {c['variant'][key]} |")
            for reason in ['HARD_STOP','PROFIT_LOCK','TRAILING','FAST_DROP']:
                co,v=c['control'],c['variant'];nco=co['reasons'].get(reason,0);nv=v['reasons'].get(reason,0)
                lines.append(f"| {reason}, N (% fechados) | {nco} ({nco/co['closed']:.2%}) | {nv} ({nv/v['closed']:.2%}) |")
            lines += [
                      '', '| MARÉ×ONDA | Sources união | FAST | Episódios | Delta USDT |','|---|---:|---:|---:|---:|']
            for label,v in sorted(c['regimes'].items()):lines.append(f"| {label} | {v['n']} | {v['triggers']} | {v['episodes']} | {v['delta']:+.6f} |")
    lines+=['','## Limitações e interpretação','',
            'HIGH_FIRST/LOW_FIRST são trajetórias modeladas, não limites econômicos. Instantes intraminuto são artificiais. Stops cruzados em segmentos usam nível cruzado com custos; gaps usam open com custos. Não se assume fill perfeito. O custo é aplicado consistentemente a todas as saídas e à entrada.',
            'As entradas ocorrem no boundary, sem reaproveitar extremos do candle de sinal. As posições abertas permanecem OPEN; net total inclui MTM líquido de liquidação estimada, separado do realizado em cada arquivo de run. PF usa somente fechamentos. DD MTM é amostrado a cada minuto, não máximo intratick.',
            'A exposição modeled usa duração segundo timestamps artificiais; upper cobra o minuto inteiro de posição. Não confundir com capital máximo ou com risco de perda.',
            'Sizing segue os shadows: notional fixo do YAML e capacidade por slots, sem inserir um novo gate de solvência. Em cenários em que a equity contábil cai abaixo de zero, as admissões phantom ainda seguem essa regra. Esses cenários não representam uma carteira financiada exclusivamente por 100 USDT; não interpretar net/capital como retorno implementável nem alterar sizing para ocultar a limitação.',
            'Junho–agosto participaram da motivação/seleção anterior; o forward recente também era conhecido. Nenhum período foi declarado validação independente. Metades e bootstrap não eliminam seleção prévia, dependência, erro de modelo ou comparações múltiplas.',
            f"Episódios definidos por gap de oportunidades >6h: {summary['episodes']}. Os blocos podem ser longos e poucos; não foram repartidos após observar resultados. Bootstrap agrupa contribuições, sem reexecutar CB em séries reordenadas. Leave-one-day-out/top3 removem contribuições, não recalculam admissões.",
            'Com um único episódio, o IC por episódio é NÃO ESTIMÁVEL: um bootstrap degenerado [delta,delta] não seria suporte estatístico. Nenhuma combinação pode cumprir o requisito robusto de pelo menos dez episódios nessa definição congelada.',
            'O limiar p*=C/(S+C) refere-se apenas ao subconjunto binário HS versus PL/TRAIL e exige S,C positivos; demais destinos e trajetória/capacidade estão separados. Uma saída PL/TRAIL não é necessariamente lucrativa líquida.',
            'A matriz completa de sobreposição para os três custos/dois caminhos e a reconciliação forward de entradas, motivos e PnL estão em summary.json. Falha de fidelidade de um par não invalida automaticamente os demais. As métricas por célula MARÉ×ONDA são exploratórias; células <10 episódios não sustentam conclusão.',
            '', '## Reconciliação de carteiras forward', '',
            '| Seletor | Caminho | Comuns | Observadas | Simuladas | Recall | Precisão | Motivo igual nos comuns |',
            '|---|---|---:|---:|---:|---:|---:|---:|']
    for arm,paths in summary['forward_portfolio_reconciliation'].items():
        for path,r in paths.items():
            lines.append(f"| {arm} | {path} | {r['common']} | {r['observed']} | {r['simulated']} | {r['recall']} | {r['precision']} | {r['reason_agreement_common']} |")
    lines.append('')
    for arm,paths in summary['forward_portfolio_reconciliation'].items():
        for path,r in paths.items():
            lines.append(f"Admissões observadas sem admissão idealizada: {r['observed_only_reasons']}; somente na simulação: {r['sim_only']}. Motivos são a decisão modelada, não prova de causa no runtime.")
    lines += ['', 'Carteiras não começam em condições equivalentes: o replay parte flat em junho; o forward tem posições, CB e coortes herdadas. Taxas acima são diagnóstico, sem reivindicar paridade. Não houve exclusão pós-resultado de divergências para melhorar essas taxas. PnL e preços individuais estão no JSON; diferenças de custo/path não são automaticamente bugs.',
              '', '## Reconciliação de avaliações FAST_DROP', '']
    for path,r in summary['fast_minute_reason_reconciliation'].items():
        lines.append(f"{path}: {r['counts']}. Cada linha conserva decisão, motivo e minuto real versus idealizado. Classificações de disponibilidade/estado/path são hipóteses explicativas, não atribuições causais comprovadas.")
    lines+=['','## Sobreposição','',
            'Matriz abaixo: interseção de entradas FORWARD por source, desde o corte válido, incluindo posições abertas. Não é comparação econômica e não qualifica os dois controles reprovados.','',
            '| Controle | '+' | '.join(ARMS)+' |','|---|'+'---:|'*len(ARMS)]
    for a,row in summary['forward_entry_overlap_all_four'].items():
        lines.append('| '+a+' | '+' | '.join(str(row[b]['intersection']) for b in ARMS)+' |')
    lines+=['','Interseção de disparos idealizados entre os pares qualificados (base):','']
    for path in ['HIGH_FIRST','LOW_FIRST']:
        matrix=summary['overlap']['BASE_'+path]['TRIGGERS']
        lines.append(path+': '+json.dumps(matrix,ensure_ascii=False))
    lines+=['','Não há matriz idealizada de disparos válida para os controles reprovados: não se fabricam acionamentos das variantes não executadas. Matrizes de entradas e disparos dos pares qualificados, incluindo Jaccard, estão em summary.json para todos os custos/caminhos.']
    lines+=['','## Conclusões e respostas econômicas','']
    for arm,a in summary['arms'].items():
        if 'cells' not in a:
            lines.append(f"- {arm}: NÃO INTERPRETÁVEL; o gate de reconstrução falhou. Não é resultado econômico negativo do FAST_DROP.")
            continue
        h=a['cells']['BASE_HIGH_FIRST'];l=a['cells']['BASE_LOW_FIRST']
        lines.append(f"- {arm}: {labels[a['classification']]}. Essa categoria do protocolo indica sinal pontual plausível; não confirmação estatística. Delta base HIGH_FIRST/LOW_FIRST: {h['delta']:+.6f}/{l['delta']:+.6f} USDT. Disparos: {h['triggers']}/{l['triggers']}. IC diário: {h['daily_bootstrap']['ci95']} / {l['daily_bootstrap']['ci95']}. Sem os três dias positivos principais: {h['exclude_top3_positive_days']:+.6f}/{l['exclude_top3_positive_days']:+.6f} USDT.")
        for path,c in [('HIGH_FIRST',h),('LOW_FIRST',l)]:
            hs=sum(d['net_delta'] or 0 for d in c['trigger_details'] if d['control_reason']=='HARD_STOP')
            other=-sum(d['net_delta'] or 0 for d in c['trigger_details'] if d['control_reason'] in ['PROFIT_LOCK','TRAILING'])
            lines.append(f"  {path}: HS pareados economizaram {hs:.6f} USDT; PL/TRAIL antecipados custaram {other:.6f} USDT (valor assinado, pode ser negativo). Saldo desses destinos binários {hs-other:+.6f}; componente direto FAST pareado completo {c['paired_fast_direct_delta']:+.6f}; efeito total da carteira {c['delta']:+.6f}. Não atribuir o restante exclusivamente a capacidade: inclui toda alteração de trajetória/CB/admissões.")
        ds=[c['delta'] for c in a['cells'].values()]
        lines.append(f"  Sensibilidade: delta de {min(ds):+.6f} a {max(ds):+.6f} USDT nos seis cenários custo×caminho. Custos mudam preço efetivo de entrada, limiares de perda e trajetórias; não são somente desconto linear de PnL.")
    lines += ['',
              '1. Benefício em algum seletor? O sinal pontual está explicitado acima para os controles qualificados; nenhum par satisfaz os critérios robustos. Net positivo do delta não significa que a carteira seja lucrativa.',
              '2. Consistência ou poucos episódios? A concentração, os ICs diários e a remoção top3 estão publicados; o protocolo produz um único episódio, impedindo inferência por episódios e qualquer alegação de robustez.',
              '3. Efeito entre seletores? Descrever apenas os dois pares qualificados; número de disparos, exposição e admissões diferem. Não generalizar aos dois controles não interpretáveis nem declarar uma interação estatisticamente demonstrada.',
              '4. Justifica forward? Um sinal classificado PROMISING pelo critério exploratório pode motivar discussão de validação nova, mas este estudo não demonstra evidência suficiente para recomendar automaticamente novo shadow. Amostra de disparos, concentração e ausência de validação independente permanecem decisivas. Nenhum braço foi criado.',
              '5. Incertezas? Trajetória intrabar, execução/caudas, dependência temporal, trajetórias de CB herdadas, reconstrução dos controles reprovados e comportamento fora da amostra. Os resultados restringem-se aos parâmetros atuais, seletores interpretáveis e período analisado; não reprovam ou validam genericamente proteção contra quedas rápidas.']
    lines += [
            '', '## Reprodutibilidade','',
            '`python tools/fast_drop_selectors_study.py prepare` (API pública e leitura SSH, corte congelado); `reconstruct`; `fidelity`. Depois `python tools/fast_drop_selectors_engine.py` e `python -c "from tools.fast_drop_selectors_engine import report; report()"`.',
            'Artefatos: config.json, market_manifest.json, forward.json, signals.json, contexts.json, fidelity.json, engine_checks.json, runs/*.json, summary.json. Nenhum código operacional/YAML foi modificado. Nenhum deploy/restart/commit/push foi realizado.']
    (OUT/'RELATORIO.md').write_text('\n'.join(lines)+'\n',encoding='utf8')

if __name__=='__main__':replay()
