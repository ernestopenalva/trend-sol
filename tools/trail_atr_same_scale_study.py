"""Fixed-cache, same-timeframe ATR aging vs scale diagnosis; no live writes."""
from __future__ import annotations
import bisect
import json
import math
import sys
from copy import deepcopy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import trail_atr_lifecycle_study as old
from tools.trail_atr_lifecycle_study import (ROOT,INPUT,CACHE,PRIOR,PATHS,MONTHS,VARIANTS,
    ClosedATR,ATRPosition,Review,SignalEvent,EntrySignal,ReplayExecutionClient,NullLogger,
    MINUTE_MS,iso,ms,brt,month,digest,load_candle_cache,_deduplicate,capture,
    economics,quantile,spearman,table,fmt)

OUT=ROOT/'data/studies/trail_atr_same_scale/20261005'


class NoContextReview(Review):
    def context_fields(self,at):return {}  # No EMA/MACD calculation or grouping in this study.


class ScalePosition(ATRPosition):
    def initialize_study(self,variant,series,review,opened):
        super().initialize_study(variant,series,review,opened)
        self.entry_gap_atr=self.entry_atr if series.timeframe=='1m' else self.clock_snapshot['atr']

    def _current_trailing_stop(self):
        if not hasattr(self,'entry_gap_atr'):return super()._current_trailing_stop()
        return self.highest_price-5*self.entry_gap_atr

    def on_tick(self,price,ts=None):
        if 'ENTRY_SCALE' not in self.diagnostic_activation and price-self.entry_price>=10*self.entry_gap_atr:
            self.diagnostic_activation['ENTRY_SCALE']={'modeled_at_ms':ms(ts),'evaluated_at_ms':self.at,
                'price':price,'atr':self.entry_gap_atr}
        return super().on_tick(price,ts)


def replay(t,review,series,path,variant,parity=False):
    fresh=review.new_position(t['opened_ms'],t['entry_price']);client=ReplayExecutionClient(review.spread/2)
    p=ScalePosition.from_state(deepcopy(fresh.position.to_state()),fresh.position.config,client,NullLogger())
    p.initialize_study(variant,series,review,t['opened_ms']);closed=None;last=t['opened_ms']
    trajectory=[];last_id=None
    for i in range(bisect.bisect_left(review.opens,t['opened_ms']),len(review.minute)):
        c=review.minute[i]
        if c.boundary_ms>review.end:break
        p.set_clock(c.open_time_ms);last=c.open_time_ms
        if p.clock_snapshot['close_ms']!=last_id:
            trajectory.append({'evaluated_ms':last,**p.clock_snapshot});last_id=p.clock_snapshot['close_ms']
        previous=None
        for point in _deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST' else (c.open,c.low,c.high,c.close)):
            stop=p.effective_stop
            tick=stop if previous is not None and previous>stop and point<=stop else point
            client.current_price=tick;p.on_tick(tick,iso(c.boundary_ms));previous=point
            if p.status=='CLOSED':closed=c.boundary_ms;break
        if closed is not None:break
    net=review.notional*(p.pnl_pct(p.exit_price)-review.fees)/100 if closed else None
    if parity:
        if (closed!=t['closed_ms'] or p.exit_reason!=t['exit_reason'] or net is None or
            any(abs(a-b)>1e-8 for a,b in ((p.exit_price,t['exit_price']),(net,t['net_usd']),(p.highest_price,t['peak_price'])))):
            raise AssertionError(f'PARITY FAILED — stop before alternatives: {path} {t["source_candle"]}')
    terminal=review.minute[-1].close*(1-review.spread/2/10000)
    peak=p.highest_price;price=p.exit_price
    return {'variant':variant,'timeframe':series.timeframe,'closed_ms':closed,'evaluated_exit_ms':last if closed else None,
        'reason':p.exit_reason or 'OPEN/CENSORED','exit_price':price,'net':net,'delta':None,
        'peak':peak,'peak_event':p.peaks[-1],'peaks':p.peaks,'trajectory':trajectory,
        'entry_gap_atr':p.entry_gap_atr,'atr_peak':p.peak_snapshot['atr'],'atr_exit':series.snapshot(last),
        'age_min':((closed or review.end)-t['opened_ms'])/MINUTE_MS,
        'additional_min':((closed or review.end)-t['closed_ms'])/MINUTE_MS,
        'captured_abs':price-p.entry_price if closed else None,
        'captured_move_own':capture(p.entry_price,price,peak) if closed else None,
        'captured_move_control_peak':capture(p.entry_price,price,t['peak_price']) if closed else None,
        'giveback_abs':peak-price if closed else None,
        'giveback_pct':(peak-price)/p.entry_price*100 if closed else None,
        'isolated_peak_to_trough_pct':max(p.max_peak_drawdown_pct,(peak-price)/p.entry_price*100) if closed else p.max_peak_drawdown_pct,
        'activation':p.activation,'diagnostic_activation':p.diagnostic_activation,'pl_armed':sorted(p.applied_steps),
        'stop':p.effective_stop,'trailing_stop':p.trailing_stop,'gap_updates':p.gap_updates,
        'counters':dict(p.counters),'stop_rise_atr_decrease_no_peak':p.additional_stop_rise,
        'effective_stop_rise_atr_updates':p.effective_stop_rise,'censored':closed is None,
        'terminal_mtm_informative':review.notional*(p.pnl_pct(terminal)-review.fees)/100 if not closed else None}


def distribution(v):
    vals=[x for x in v if x is not None]
    return {'n':len(vals),'mean':sum(vals)/len(vals) if vals else None,
        **{f'p{int(q*100)}':quantile(vals,q) for q in (.1,.25,.5,.75,.9)}}


def paired_metrics(es,scale,variant):
    # Both sides must be closed. Censored never receive realized net or delta.
    valid=[e for e in es if e['replays'][scale][variant]['net'] is not None and e['replays'][scale]['ATR_ENTRY']['net'] is not None]
    view=[]
    for e in valid:
        arms={v:dict(r) for v,r in e['replays'][scale].items()};c=arms['ATR_ENTRY']
        for r in arms.values():
            r['delta']=r['net']-c['net'] if r['net'] is not None else None
            r['additional_min']=(r['closed_ms']-c['closed_ms'])/MINUTE_MS if r['closed_ms'] else None
            r['captured_move_control_peak']=capture(e['entry_price'],r['exit_price'],c['peak']) if r['net'] is not None else None
        view.append({'arms':arms})
    metrics=economics(view,variant)
    metrics.update({'n':len(es),'paired_closed':len(valid),
        'variant_closed':sum(e['replays'][scale][variant]['net'] is not None for e in es),
        'variant_censored':sum(e['replays'][scale][variant]['net'] is None for e in es),
        'unpaired_excluded':len(es)-len(valid)})
    return metrics


def ratio_stats(es):
    keys=('peak1_entry1','exit1_entry1','peak5_entry5','exit5_entry5',
          'entry5_entry1','peak5_peak1','exit5_exit1','old_cross_ratio','temporal_log','scale_log')
    return {**{k:distribution([e['ratios'][k] for e in es]) for k in keys},
        **{f'ATR_{tf}_{stage}':distribution([e['atr_snapshots'][tf][stage]['atr'] for e in es])
           for tf in ('1m','5m') for stage in ('entry','peak','exit')}}


def activation_stats(es,scale,variant):
    rows=[];base_missing=alt_missing=both=0
    for e in es:
        # Identical observed original lifetime for all diagnostic thresholds.
        d=e['activation_diagnostic'][scale];b=d.get('ENTRY_SCALE');a=d.get(variant)
        if b is None:base_missing+=1
        if a is None:alt_missing+=1
        if a and b:
            both+=1;rows.append({'delta_min':(a['modeled_at_ms']-b['modeled_at_ms'])/MINUTE_MS,
                'price_delta':a['price']-b['price'],'entry_age_min':(a['modeled_at_ms']-e['opened_ms'])/MINUTE_MS})
    return {'n':len(es),'base_not_reached':base_missing,'updated_not_reached':alt_missing,'both_reached':both,
        **{k:distribution([r[k] for r in rows]) for k in ('delta_min','price_delta','entry_age_min')}}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    m=json.loads((INPUT/'manifest.json').read_text());config=m['config'];end=ms(m['end_brt'])
    assert end==ms('2026-10-02T22:28:00-03:00')
    assert config['risk']['trailing']=={'mode':'atr','activation_atr':10,'gap_atr':5}
    cs={tf:load_candle_cache(CACHE/f'SOLUSDT_{tf}.jsonl') for tf in ('1m','5m','15m')}
    for tf in cs:
        if digest(CACHE/f'SOLUSDT_{tf}.jsonl')!=m['cache_hashes'][tf]:raise ValueError('Frozen cache changed')
    if digest(PRIOR/'signals.json')!=m['signals_sha256']:raise ValueError('Frozen signals changed')
    signals=[SignalEvent(r['boundary_ms'],EntrySignal(**r['signal'])) for r in json.loads((PRIOR/'signals.json').read_text())]
    review=NoContextReview(config,cs,signals,end)
    series={tf:ClosedATR(cs[tf],14) for tf in ('1m','5m')}
    for tf,a in series.items():a.timeframe=tf
    original={p:[t for t in json.loads((INPUT/f'{p}_systemic.json').read_text())['BE_OFF_CB']['trades'] if t['exit_reason']=='TRAILING'] for p in PATHS}
    if [len(original[p]) for p in PATHS]!=[567,601]:raise AssertionError('Universe mismatch; stopped')
    prior={}
    for line in (old.OUT/'events.jsonl').open(encoding='utf-8'):
        e=json.loads(line)
        prior[(e['path'],e['source_candle'])]={'arms':{'ATR_ENTRY':{
            k:e['arms']['ATR_ENTRY'][k] for k in ('closed_ms','exit_price','net','peak','reason','diagnostic_activation')}}}
    if {(p,t['source_candle']) for p in PATHS for t in original[p]}!=set(prior):raise AssertionError('Prior universe mismatch; stopped')
    events=[]
    # Complete baseline parity phase before running ANY alternative.
    for p in PATHS:
        print('PARITY',p,len(original[p]),flush=True)
        for t in original[p]:
            r=replay(t,review,series['1m'],p,'ATR_ENTRY',parity=True)
            prev=prior[(p,t['source_candle'])]['arms']['ATR_ENTRY']
            for key in ('closed_ms','exit_price','net','peak','reason'):
                if r[key]!=prev[key]:raise AssertionError(f'Prior exact parity mismatch {key}')
            events.append({'path':p,'source_candle':t['source_candle'],'opened_ms':t['opened_ms'],
                'month':month(t['closed_ms']),'entry_price':t['entry_price'],'original_exit_ms':t['closed_ms'],
                'replays':{'1m':{'ATR_ENTRY':r},'5m':{}},'trade':t})
    print('PARITY PASS 567/601; alternatives now allowed',flush=True)
    for i,e in enumerate(events):
        t=e.pop('trade');p=e['path'];base=e['replays']['1m']['ATR_ENTRY']
        opened=t['opened_ms'];atpeak=base['peak_event']['evaluated_at_ms'];atexit=base['evaluated_exit_ms']
        atrs={tf:{label:series[tf].snapshot(at) for label,at in [('entry',opened),('peak',atpeak),('exit',atexit)]} for tf in series}
        e['entry_atr_series_relative_error']=atrs['1m']['entry']['atr']/review.signal[opened].entry_atr-1
        # Entry1m is the authoritative stored Wilder14 value (300-candle seed).
        atrs['1m']['entry']['atr']=review.signal[opened].entry_atr
        e['atr_snapshots']=atrs;e['age_min']=base['age_min']
        a={tf:{k:s['atr'] for k,s in vals.items()} for tf,vals in atrs.items()}
        e['ratios']={'peak1_entry1':a['1m']['peak']/a['1m']['entry'],
            'exit1_entry1':a['1m']['exit']/a['1m']['entry'],
            'peak5_entry5':a['5m']['peak']/a['5m']['entry'],'exit5_entry5':a['5m']['exit']/a['5m']['entry'],
            'entry5_entry1':a['5m']['entry']/a['1m']['entry'],'peak5_peak1':a['5m']['peak']/a['1m']['peak'],
            'exit5_exit1':a['5m']['exit']/a['1m']['exit'],'old_cross_ratio':a['5m']['peak']/a['1m']['entry']}
        e['ratios']['temporal_log']=math.log(e['ratios']['peak5_entry5'])
        e['ratios']['scale_log']=math.log(e['ratios']['entry5_entry1'])
        assert math.isclose(math.log(e['ratios']['old_cross_ratio']),e['ratios']['temporal_log']+e['ratios']['scale_log'],abs_tol=1e-12)
        for tf in series:
            for v in VARIANTS:
                if tf=='1m' and v=='ATR_ENTRY':continue
                e['replays'][tf][v]=replay(t,review,series[tf],p,v)
        # 5m activation diagnostics on the ORIGINAL lifetime, not a longer variant.
        # Updated5m thresholds were observed on the identical original lifetime.
        d5=deepcopy(prior[(p,t['source_candle'])]['arms']['ATR_ENTRY']['diagnostic_activation'])
        # Constant ENTRY5 threshold searched over original observed modeled ticks.
        rp=review.new_position(opened,t['entry_price']);pos=rp.position
        for c in review.minute[bisect.bisect_left(review.opens,opened):]:
            if c.boundary_ms>t['closed_ms']:break
            previous=None
            for point in _deduplicate((c.open,c.high,c.low,c.close) if p=='HIGH_FIRST' else (c.open,c.low,c.high,c.close)):
                tick=pos.effective_stop if previous is not None and previous>pos.effective_stop and point<=pos.effective_stop else point
                if 'ENTRY_SCALE' not in d5 and tick-t['entry_price']>=10*a['5m']['entry']:
                    d5['ENTRY_SCALE']={'modeled_at_ms':c.boundary_ms,'evaluated_at_ms':c.open_time_ms,'price':tick}
                rp.client.current_price=tick;pos.on_tick(tick,iso(c.boundary_ms));previous=point
                if pos.status=='CLOSED':break
            if pos.status=='CLOSED':break
        e['activation_diagnostic']={'1m':base['diagnostic_activation'],'5m':d5}
        if (i+1)%100==0:print('alternatives',i+1,flush=True)
    produce(events,m)
    print('DONE',OUT,flush=True)


def produce(events,m):
    summary={};description={};activations={};audit={'universe':{},'snapshot_checks':0}
    for p in PATHS:
        es=[e for e in events if e['path']==p];audit['universe'][p]=len(es)
        q1=quantile([e['age_min'] for e in es],1/3);q2=quantile([e['age_min'] for e in es],2/3)
        for e in es:e['age_group']='SHORT' if e['age_min']<=q1 else 'MEDIUM' if e['age_min']<=q2 else 'LONG'
        summary[p]={mo:{tf:{v:paired_metrics([e for e in es if mo=='ALL' or e['month']==mo],tf,v) for v in VARIANTS} for tf in ('1m','5m')} for mo in MONTHS}
        description[p]={mo:ratio_stats([e for e in es if mo=='ALL' or e['month']==mo]) for mo in MONTHS}
        description[p]['age']={'tertiles_min':[q1,q2],
            'spearman':{tf:spearman([e['age_min'] for e in es],[e['ratios'][f'peak{tf[0]}_entry{tf[0]}'] for e in es]) for tf in ('1m','5m')},
            'groups':{g:{'n':len(sel),'age':distribution([e['age_min'] for e in sel]),'ratios':ratio_stats(sel)} for g in ('SHORT','MEDIUM','LONG') for sel in [[e for e in es if e['age_group']==g]]}}
        activations[p]={mo:{tf:{v:activation_stats([e for e in es if mo=='ALL' or e['month']==mo],tf,v) for v in VARIANTS[1:]} for tf in ('1m','5m')} for mo in MONTHS}
    for e in events:
        for vals in e['atr_snapshots'].values():
            for name,snap in vals.items():
                at=e['opened_ms'] if name=='entry' else e['replays']['1m']['ATR_ENTRY']['peak_event']['evaluated_at_ms'] if name=='peak' else e['replays']['1m']['ATR_ENTRY']['evaluated_exit_ms']
                assert snap['close_ms']<at;audit['snapshot_checks']+=1
        for arms in e['replays'].values():
            for r in arms.values():
                for snap in r['trajectory']:
                    assert snap['close_ms']<snap['evaluated_ms'];audit['snapshot_checks']+=1
                prior_stop=None
                for u in r['gap_updates']:
                    assert u['atr_snapshot']['close_ms']<u['evaluated_at_ms']
                    assert prior_stop is None or u['next_trailing_stop']>=prior_stop
                    prior_stop=u['next_trailing_stop']
    audit['entry1m_relative_recompute_error']=distribution([abs(e['entry_atr_series_relative_error']) for e in events])
    (OUT/'events.jsonl').write_text(''.join(json.dumps(e,allow_nan=False)+'\n' for e in events),encoding='utf-8')
    for name,value in [('summary',summary),('atr_distributions',description),('activation',activations),('audit',audit)]:
        (OUT/f'{name}.json').write_text(json.dumps(value,indent=2,allow_nan=False),encoding='utf-8')
    manifest={'start_brt':m['start_brt'],'end_brt':m['end_brt'],'cache_hashes':m['cache_hashes'],
        'signals_sha256':m['signals_sha256'],'tool_sha256':digest(Path(__file__)),
        'dependencies':{str(f):digest(ROOT/f) for f in ('tools/trail_atr_lifecycle_study.py','src/position/bot_full_engine.py','src/indicators/indicators.py')},
        'original_events_sha256':digest(old.OUT/'events.jsonl'),'config':m['config'],
        'economic_activation':'10 x original stored entryATR1m in ALL six variants; PL/HS unchanged',
        'entry1m_seed':'authoritative stored ATR14 from300-candle EntryEngine buffer; full-vector causal ATR14 peak/dynamic; numerical recomputation error audited',
        'time':'1m open cutoff; closed candles with close_time<cutoff; modeled exits at1m boundary, not exact ticks',
        'dd':'isolated realized curve zero-start ordered close times; selected original TRAILs, not portfolio DD',
        'pairing':'within-scale ENTRY comparator; only both closed enter economic delta; original exit month BRT',
        'scale_decomposition':'per-trade log(peak5/entry1)=log(peak5/entry5)+log(entry5/entry1); mean logs additive, medians not additive',
        'context':'no EMA/MACD calculation, filtering or grouping',
        'activation_diagnostic':'original control lifetime only; censored threshold not treated as future activation'}
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    lines=['# ATR mesma escala — envelhecimento vs timeframe',f"Base congelada {m['start_brt']} → {m['end_brt']}; paridade exata567 HIGH/601 LOW.",
        'Ativação econômica10×entryATR1m, PL/HS/fees/spread preservados em todos os braços. ENTRY5m é somente controle paralelo de GAP, não o runtime. Replay isolado, não sistêmico. Sem EMA/MACD. Meses: saída original BRT.',
        'PF/DD condicionados à população originalmente TRAILING, não inferir risco de carteira. Todos os deltas nas tabelas econômicas usam ENTRY da MESMA escala.']
    for p in PATHS:
        lines+=[f'## {p}',table(['mês','escala','GAP','N','pares','cens','net $','net/trade $','delta $','PF','DD $','capt própria %','capt ENTRY %','giveback % med','adicional min med','W→L','L→W'],[
            [mo,tf,v,r['n'],r['paired_closed'],r['variant_censored'],fmt(r['net']),fmt(r['net_trade']),fmt(r['delta']),fmt(r['pf']),fmt(r['realized_dd_isolated']),fmt(100*r['captured_move_own']['mean']) if r['closed'] else 'N/A',fmt(100*r['captured_move_control_peak']['mean']) if r['closed'] else 'N/A',fmt(r['giveback_pct']['median']),fmt(r['additional_min']['median']),r['winners_to_losers'],r['losers_to_winners']]
            for mo in MONTHS for tf,vs in summary[p][mo].items() for v,r in vs.items()]),
            '### Razões causais (mesmo peak/exit do controle original)',table(['mês','razão','N','mean','p10','p25','median','p75','p90'],[
            [mo,k,d['n'],fmt(d['mean']),fmt(d['p10']),fmt(d['p25']),fmt(d['p50']),fmt(d['p75']),fmt(d['p90'])] for mo in MONTHS for k,d in description[p][mo].items()]),
            f"Spearman idade: {description[p]['age']['spearman']}; tercis {description[p]['age']['tertiles_min']} min.",
            table(['idade','N','idade med','peak1/entry1 med','peak5/entry5 med'],[[g,x['n'],fmt(x['age']['p50']),fmt(x['ratios']['peak1_entry1']['p50']),fmt(x['ratios']['peak5_entry5']['p50'])] for g,x in description[p]['age']['groups'].items()]),
            '### Ativação apenas diagnóstica, dentro da vida original',table(['mês','escala','atualizado','N','ENTRY não atingiu','atualizado não atingiu','ambos','delta min med','delta preço med'],[
            [mo,tf,v,r['n'],r['base_not_reached'],r['updated_not_reached'],r['both_reached'],fmt(r['delta_min']['p50']),fmt(r['price_delta']['p50'])] for mo in MONTHS for tf,vs in activations[p][mo].items() for v,r in vs.items()])]
    cens=[(e,tf,v,r) for e in events for tf,arms in e['replays'].items() for v,r in arms.items() if r['censored']]
    lines+=['## OPEN/CENSORED — fora da economia pareada',table(['path','source','mês','escala','GAP','idade min','peak','stop','MTM informativo $'],[[e['path'],e['source_candle'],e['month'],tf,v,fmt(r['age_min']),fmt(r['peak']),fmt(r['stop']),fmt(r['terminal_mtm_informative'])] for e,tf,v,r in cens]),
        'Distribuições absolutas entry/peak/exit e trajetórias dinâmicas estão em events.jsonl; tabela de razões em atr_distributions.json. Decomposição em logs separa efeito temporal de escala sem somar medianas indevidamente.']
    lines+=['## Efeito puro de escala — GAP ENTRY5m vs GAP ENTRY1m',
        'Ambos com ativação original1m; apenas largura do GAP congelado difere. Estes deltas NÃO são os deltas adaptativos dentro de5m.',
        table(['path','mês','N','pares fechados','delta ENTRY5−ENTRY1 $','log temporal médio','log escala médio','fração escala log %'],[
            [p,mo,len(sel),len(resolved),fmt(sum(e['replays']['5m']['ATR_ENTRY']['net']-e['replays']['1m']['ATR_ENTRY']['net'] for e in resolved)),
             fmt(stats['temporal_log']['mean']),fmt(stats['scale_log']['mean']),
             fmt(100*stats['scale_log']['mean']/(stats['temporal_log']['mean']+stats['scale_log']['mean']))]
            for p in PATHS for mo in MONTHS
            for sel in [[e for e in events if e['path']==p and (mo=='ALL' or e['month']==mo)]]
            for resolved in [[e for e in sel if all(e['replays'][tf]['ATR_ENTRY']['net'] is not None for tf in ('1m','5m'))]]
            for stats in [ratio_stats(sel)]])]
    (OUT/'report.md').write_text('\n\n'.join(lines),encoding='utf-8')


if __name__=='__main__':main()
