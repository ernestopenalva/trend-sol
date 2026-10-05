"""Fixed GAP5/7/9/13 systemic replay and effective-floor audit. Offline only."""
from __future__ import annotations
import json
import math
import sys
from collections import Counter,defaultdict
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.position.bot_full_engine import BotFullExitPosition
from tools.pl_lon_elastic_study import ROOT,OUT as INPUT,CACHE,PRIOR,MONTHS,SignalEvent,EntrySignal,iso,systemic,serialize
from tools.trail_atr_lifecycle_study import PATHS,table,fmt,dist
from tools.be_off_cb_defensive_closure import digest,ms,month,metrics,compare
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import MINUTE_MS

OUT=ROOT/'data/studies/trail_gap_systemic/20261005'
GAPS=(5,7,9,13);LEVELS=(5,8,10,12,15,20)
ORIGINAL_PROCESS=systemic.process_candle_systemic


def active_floor_minutes(detail,end):
    """Minute-boundary accounting: retain last modeled state at tied timestamps."""
    states={row['at_ms']:row for row in detail['floor_events']}
    stop=detail['closed_ms'] or end;cursor=detail['opened_ms'];owner='HARD_STOP';active=False
    counts=Counter()
    for at,row in sorted(states.items()):
        if at>stop:break
        if active:counts[owner]+=max(0,(at-cursor)/MINUTE_MS)
        cursor=at;owner=row['owner'];active=row['trail_active']
    if active:counts[owner]+=max(0,(stop-cursor)/MINUTE_MS)
    return dict(counts)


def realized_contributions(control,variant):
    left={t['source_candle']:t for t in control['trades']};right={t['source_candle']:t for t in variant['trades']}
    rows=[]
    for source in sorted(set(left)|set(right)):
        c=left.get(source);v=right.get(source)
        cn=c['net_usd'] if c and c['closed_ms'] is not None else 0
        vn=v['net_usd'] if v and v['closed_ms'] is not None else 0
        kind='COMMON_RESOLVED' if c and v and c['closed_ms'] and v['closed_ms'] else 'COMMON_CENSORED_TIMING' if c and v else 'VARIANT_ONLY' if v else 'CONTROL_ONLY'
        rows.append({'source_candle':source,'contribution':vn-cn,'kind':kind})
    return sorted(rows,key=lambda r:r['contribution'],reverse=True)


def dominant(p):
    # Honor the engine's retained stop_type on ties, not a fresh theoretical max.
    if p.stop_type=='trailing':return 'TRAIL'
    if p.stop_type=='profit_lock':return p.profit_lock_step or 'PL_UNKNOWN'
    return p.stop_type.upper()


def snapshot(p,price,at):
    return {'at_ms':at,'price':price,'peak':p.highest_price,'peak_atr':(p.highest_price-p.entry_price)/p.entry_atr,
        'effective_stop':p.effective_stop,'owner':dominant(p),'stop_type':p.stop_type,
        'PL_step':p.profit_lock_step,'PL_stop':p.profit_lock_stop,'PL_armed':sorted(p.applied_steps),
        'trail_active':p.trailing_active,'trail_stop':p.trailing_stop,
        'candidate':p.highest_price-p.trailing_gap_atr*p.entry_atr}


def activation_case(candidate,pl):
    if pl is None:return 'NO_PL'
    if math.isclose(candidate,pl,rel_tol=0,abs_tol=1e-9):return 'TIE'
    return 'TRAIL_ABOVE_PL' if candidate>pl else 'PL_ABOVE_TRAIL'


class GapPosition(BotFullExitPosition):
    def on_tick(self,price,ts=None):
        before=dominant(self);old_peak=self.highest_price;active=self.trailing_active
        event=super().on_tick(price,ts);s=self.study;at=ms(ts);row=snapshot(self,price,at)
        if self.status=='CLOSED':self.modeled_closed_ms=at
        if self.highest_price>old_peak:
            self.peak_events.append({'at_ms':at,'price':self.highest_price,'owner':row['owner']})
        if not active and self.trailing_active:
            self.activation={**row,'comparison':activation_case(row['candidate'],self.profit_lock_stop),
                'PL2_armed':'atr:2' in self.applied_steps}
        if self.trailing_active and self.first_dominance is None and row['owner']=='TRAIL' and (self.profit_lock_stop is None or self.trailing_stop>self.profit_lock_stop):
            self.first_dominance={**row,'previous_owner':before,
                'minutes_from_activation':(at-self.activation['at_ms'])/MINUTE_MS,
                'advance_from_activation_atr':row['peak_atr']-self.activation['peak_atr']}
        for level in LEVELS:
            if level not in self.levels and row['peak_atr']>=level:self.levels[level]=deepcopy(row)
        if before!=row['owner'] or self.highest_price>old_peak or self.status=='CLOSED':
            self.floor_events.append(row)
        return event


class Instrumentation:
    def __init__(self,start,end,notional):
        self.start=start;self.end=end;self.notional=notional;self.positions=[]
        self.exposure=defaultdict(Counter)

    def factory(self,*args,**kwargs):
        p=GapPosition(*args,**kwargs);p.study=self
        p.activation=None;p.first_dominance=None;p.levels={};p.floor_events=[];p.peak_events=[];p.modeled_closed_ms=None
        p.floor_minutes=Counter();self.positions.append(p);return p

    def processor(self,positions,trades,candle,*args):
        if self.start<=candle.open_time_ms<self.end:
            mo=month(candle.open_time_ms);n=sum(r.position.status=='OPEN' for r in positions)
            self.exposure[mo]['observed_minutes']+=1;self.exposure[mo]['slot_minutes']+=n
            self.exposure[mo]['max_sampled_simultaneous']=max(n,self.exposure[mo]['max_sampled_simultaneous'])
            for rp in positions:
                if rp.position.status!='OPEN':continue
                owner=dominant(rp.position);rp.position.floor_minutes[owner]+=1
                self.exposure[mo]['floor_'+owner]+=1
        return ORIGINAL_PROCESS(positions,trades,candle,*args)

    def details(self):
        return [{ 'source_candle':p.source_candle_open_time,'opened_ms':ms(p.open_ts),'entry_atr':p.entry_atr,
            'entry_price':p.entry_price,'closed_ms':p.modeled_closed_ms,
            'activation':p.activation,'first_dominance':p.first_dominance,'levels':p.levels,
            'floor_minutes':dict(p.floor_minutes),'floor_events':p.floor_events,'peaks':p.peak_events,
            'terminal_peak':p.highest_price,'terminal_stop':p.effective_stop,'terminal_owner':dominant(p)} for p in self.positions]


def enrich_stats(run,detail,exposure,window,start,end,notional):
    r=metrics(run,window);sel=lambda at:window=='ALL' or month(at)==window
    closed=[t for t in run['trades'] if t['closed_ms'] is not None and sel(t['closed_ms'])]
    entries=[t for t in run['trades'] if sel(t['opened_ms'])]
    # Calendar-window open inventory, including carry-ins; not just entries of month.
    bounds={mo:(ms(mo+'-01T00:00:00-03:00'),ms(nextmo+'-01T00:00:00-03:00')) for mo,nextmo in
        zip(MONTHS[:-1],('2026-07','2026-08','2026-09','2026-10','2026-11'))}
    cutoff=end if window=='ALL' else min(end,bounds[window][1])
    open_at=([t for t in run['trades'] if t['closed_ms'] is None] if window=='ALL' else
        [t for t in run['trades'] if t['opened_ms']<cutoff and (t['closed_ms'] is None or t['closed_ms']>=cutoff)])
    x=Counter()
    for mo,c in exposure.items():
        if window=='ALL' or mo==window:
            for k,v in c.items():
                if k=='max_sampled_simultaneous':x[k]=max(x[k],v)
                else:x[k]+=v
    ages=[(t['closed_ms']-t['opened_ms'])/MINUTE_MS for t in closed]
    r.update({'entries':len(entries),'open_at_window_end':len(open_at),'censored_at_data_end':sum(t['closed_ms'] is None for t in entries),
        'win_rate':sum(t['net_usd']>0 for t in closed)/len(closed) if closed else None,
        'mean_age':sum(ages)/len(ages) if ages else None,'mean_simultaneous':x['slot_minutes']/x['observed_minutes'] if x['observed_minutes'] else 0,
        'capital_mean':notional*x['slot_minutes']/x['observed_minutes'] if x['observed_minutes'] else 0,
        'capital_max':notional*r['max_sim'],'slot_hours':x['slot_minutes']/60,
        'floor_hours':{k[6:]:v/60 for k,v in x.items() if k.startswith('floor_')},
        'after_activation_floor_hours':{owner:sum(active_floor_minutes(d,end).get(owner,0) for d in detail)/60 for owner in ('PL1','PL2','PL3','TRAIL','HARD_STOP','REVIEW')}
            if window=='ALL' else None,
        'giveback_pct':dist([(t['peak_price']-t['exit_price'])/t['entry_price']*100 for t in closed]),
        'activation_cases':dict(Counter(d['activation']['comparison'] for d in detail if d['activation'] and sel(d['activation']['at_ms']))),
        'activation_PL2_not_armed':sum(not d['activation']['PL2_armed'] for d in detail if d['activation'] and sel(d['activation']['at_ms'])),
        'activated':sum(d['activation'] is not None and sel(d['activation']['at_ms']) for d in detail),
        'first_dominance':{'n':sum(d['first_dominance'] is not None and sel(d['first_dominance']['at_ms']) for d in detail),
            **{k:dist([d['first_dominance'][k] for d in detail if d['first_dominance'] and sel(d['first_dominance']['at_ms'])]) for k in ('peak_atr','minutes_from_activation','advance_from_activation_atr')},
            'previous_owner':dict(Counter(d['first_dominance']['previous_owner'] for d in detail if d['first_dominance'] and sel(d['first_dominance']['at_ms']))),
            'PL_vigente_superado':dict(Counter(d['first_dominance']['PL_step'] or 'NO_PL' for d in detail if d['first_dominance'] and sel(d['first_dominance']['at_ms'])))},
        'activated_never_dominated':sum(d['activation'] is not None and d['first_dominance'] is None and sel(d['opened_ms']) for d in detail),
        'peak_levels':{str(level):{'n':sum(str(level) in d['levels'] and sel(d['levels'][str(level)]['at_ms']) for d in detail),
            'owners':dict(Counter(d['levels'][str(level)]['owner'] for d in detail if str(level) in d['levels'] and sel(d['levels'][str(level)]['at_ms'])))} for level in LEVELS}})
    return r


def paired_analysis(control,variant,detail,start,end):
    left={t['source_candle']:t for t in control['trades']};right={t['source_candle']:t for t in variant['trades']}
    dr={d['source_candle']:d for d in detail};rows=[]
    for source in sorted(set(left)&set(right)):
        c=left[source];v=right[source];resolved=c['closed_ms'] is not None and v['closed_ms'] is not None
        d=dr[source];newpeak=d['terminal_peak']>c.get('peak_price',float('inf'))+1e-9
        delta=v['net_usd']-c['net_usd'] if resolved else None
        recover=next((p['at_ms'] for p in d['peaks'] if c['closed_ms'] is not None and p['at_ms']>=c['closed_ms'] and p['price']>c['peak_price']+1e-9),None)
        peakatr=(c['peak_price']-c['entry_price'])/d['entry_atr'] if c['closed_ms'] else None
        band='<10' if peakatr is None or peakatr<10 else '10–12' if peakatr<12 else '12–15' if peakatr<15 else '15–20' if peakatr<20 else '20+'
        rows.append({'source_candle':source,'control_reason':c['exit_reason'],'variant_reason':v['exit_reason'],
            'control_net':c['net_usd'],'variant_net':v['net_usd'],'delta':delta,'resolved':resolved,'band':band,
            'variant_new_peak':newpeak,'recovery_after_control_exit_ms':recover,
            'recovery_min':(recover-c['closed_ms'])/MINUTE_MS if recover is not None else None,
            'additional_min':(v['closed_ms']-c['closed_ms'])/MINUTE_MS if resolved else None,
            'giveback_additional_pct':((v['peak_price']-v['exit_price'])/v['entry_price']-(c['peak_price']-c['exit_price'])/c['entry_price'])*100 if resolved else None,
            'giveback_variant_pct':(v['peak_price']-v['exit_price'])/v['entry_price']*100 if resolved else None,
            'variant_age':(v['closed_ms']-v['opened_ms'])/MINUTE_MS if v['closed_ms'] else None,
            'benefit_continuation':bool(resolved and delta>1e-10 and recover is not None and v['net_usd']>0),
            'cost':bool(resolved and delta<-1e-10)})
    def group(rs):
        valid=[r for r in rs if r['resolved']]
        return {'n':len(rs),'resolved':len(valid),'delta':sum(r['delta'] for r in valid),
            'delta_dist':dist([r['delta'] for r in valid]),
            'variant_net':sum(r['variant_net'] for r in valid),
            'reasons':dict(Counter(r['variant_reason'] for r in rs)),
            'new_peak_n':sum(r['variant_new_peak'] for r in rs),'recovery_n':sum(r['recovery_after_control_exit_ms'] is not None for r in rs),
            **{k:dist([r[k] for r in valid]) for k in ('additional_min','giveback_additional_pct','giveback_variant_pct','variant_age','recovery_min')}}
    matrix={}
    requested={'TRAILING → TRAILING','TRAILING → PROFIT_LOCK','TRAILING → HARD_STOP','TRAILING → OPEN',
        'PROFIT_LOCK → TRAILING','PROFIT_LOCK → PROFIT_LOCK','PROFIT_LOCK → HARD_STOP',
        'HARD_STOP → HARD_STOP','HARD_STOP → TRAILING','HARD_STOP → PROFIT_LOCK'}
    for key in sorted(requested|{r['control_reason']+' → '+r['variant_reason'] for r in rows}):matrix[key]=group([r for r in rows if r['control_reason']+' → '+r['variant_reason']==key])
    descending=realized_contributions(control,variant)
    full_delta=compare(control,variant,'ALL')['delta']
    concentration={str(n):{'n':len(descending[:n]),'sources':[r['source_candle'] for r in descending[:n]],
        'kinds':dict(Counter(r['kind'] for r in descending[:n])),
        'contribution':sum(r['contribution'] for r in descending[:n]),
        'share_systemic_delta':sum(r['contribution'] for r in descending[:n])/full_delta if full_delta else None,
        'systemic_delta_excluding_top':full_delta-sum(r['contribution'] for r in descending[:n])} for n in (5,10,20)}
    assert math.isclose(sum(r['contribution'] for r in descending),full_delta,abs_tol=1e-8)
    blocked=[]
    for a in variant['admissions']:
        if a['decision']=='CAPACITY':
            t=left.get(a['source_candle']);blocked.append({**a,'control_admitted':t is not None,
                'control_reason':t['exit_reason'] if t else 'NO_CONTROL_TRADE','control_net':t['net_usd'] if t else None,
                'also_admitted_elsewhere':a['source_candle'] in right})
    decomposition=compare(control,variant,'ALL');decomposition.pop('fast_control_destinations',None)
    return {'decomposition':decomposition,'transitions':matrix,
        'benefit':group([r for r in rows if r['benefit_continuation']]),
        'cost':group([r for r in rows if r['cost']]),'all_improved':group([r for r in rows if r['resolved'] and r['delta']>1e-10]),
        'all_worsened':group([r for r in rows if r['cost']]),
        'bands':{b:group([r for r in rows if r['band']==b]) for b in ('10–12','12–15','15–20','20+')},
        'concentration':concentration,'paired_rows':rows,'capacity_blocked':blocked,
        'capacity_control_admitted_n':sum(b['control_admitted'] for b in blocked),
        'capacity_control_observed_net':sum(b['control_net'] for b in blocked if b['control_net'] is not None)}


def main():
    OUT.mkdir(parents=True,exist_ok=True);m=json.loads((INPUT/'manifest.json').read_text());config=m['config']
    start=ms(m['start_brt']);end=ms(m['end_brt']);assert end==ms('2026-10-02T22:28:00-03:00')
    for tf,sha in m['cache_hashes'].items():
        if digest(CACHE/f'SOLUSDT_{tf}.jsonl')!=sha:raise ValueError('Frozen cache changed')
    if digest(PRIOR/'signals.json')!=m['signals_sha256']:raise ValueError('Signals changed')
    candles=load_candle_cache(CACHE/'SOLUSDT_1m.jsonl')
    signals=[SignalEvent(r['boundary_ms'],EntrySignal(**r['signal'])) for r in json.loads((PRIOR/'signals.json').read_text())]
    notional=config['capital']['operational_balance_usdt']*config['capital']['trade_size_pct']/100
    spread=config.get('instrumentation',{}).get('market_bot_replay',{}).get('round_trip_spread_bps',5)
    results={};details={};exposures={};summary={};comparisons={}
    for path in PATHS:
        results[path]={};details[path]={};exposures[path]={};summary[path]={};comparisons[path]={}
        for gap in GAPS:
            name=f'GAP_{gap}';cfg=deepcopy(config);cfg['risk']['trailing']['gap_atr']=gap
            assert cfg['risk']['trailing']['activation_atr']==10
            inst=Instrumentation(start,end,notional)
            print(path,name,flush=True)
            with patch.object(systemic,'BotFullExitPosition',inst.factory),patch.object(systemic,'process_candle_systemic',inst.processor):
                run=systemic.run_systemic(name=name,config=cfg,signals=signals,candles=candles,contexts=[],start_ms=start,end_ms=end,
                    path=path,spread_bps=spread,fast_enabled=False)
            serialized=serialize(run,signals,notional)
            if gap==5:
                expected=json.loads((INPUT/f'{path}_systemic.json').read_text())['BE_OFF_CB']
                if serialized!=expected:raise AssertionError('GAP5 full systemic parity mismatch; stopped')
                print('GAP5 FULL PARITY PASS',flush=True)
            detail=json.loads(json.dumps(inst.details()));exposure={k:dict(v) for k,v in inst.exposure.items()}
            results[path][name]=serialized;details[path][name]=detail;exposures[path][name]=exposure
            summary[path][name]={mo:enrich_stats(serialized,detail,exposure,mo,start,end,notional) for mo in MONTHS}
            if gap!=5:
                comparisons[path][name]=paired_analysis(results[path]['GAP_5'],serialized,detail,start,end)
                for mo in MONTHS:summary[path][name][mo]['delta_vs_gap5']=summary[path][name][mo]['net']-summary[path]['GAP_5'][mo]['net']
            else:
                for mo in MONTHS:summary[path][name][mo]['delta_vs_gap5']=0
            (OUT/f'{path}_{name}.json').write_text(json.dumps({'run':serialized,'details':detail,'exposure':exposure},allow_nan=False),encoding='utf-8')
    for name,data in [('summary',summary),('comparisons',comparisons)]:
        (OUT/f'{name}.json').write_text(json.dumps(data,indent=2,allow_nan=True),encoding='utf-8')
    manifest={'start_brt':m['start_brt'],'end_brt':m['end_brt'],'config':config,'notional':notional,'spread_bps':spread,
        'cache_hashes':m['cache_hashes'],'signals_sha256':m['signals_sha256'],'gaps':GAPS,'activation_atr':10,
        'tool_sha256':digest(Path(__file__)),'source_hashes':{f:digest(ROOT/f) for f in ('src/position/bot_full_engine.py','tools/be_off_cb_fast_drop_systemic_replay.py')},
        'parity':'GAP5 full serialize exact: trades, CB, admissions, capacity, spacing, final open inventory HIGH/LOW',
        'time':'OHLC1m order assumptions; modeled tick times boundary1m. Dominance durations use floor at minute open, transitions at boundary; not exact intraminute seconds.',
        'monthly':'economic exits/admissions by actual BRT month each arm; exposure by minute open; open inventory at calendar-window end',
        'mechanism':'dominant owner uses real engine stop_type including tie ratchet; PL label profit_lock_step, may include economic floor; no theoretical floor substitution',
        'thresholds':'5/8/10/12/15/20 first observed point with peak>=level, may overshoot, not interpolated entry rule',
        'counterfactual_limit':'observed control outcome for exact-source capacity denied is attribution, not guaranteed recoverable net; no blocked-trade fabrication',
        'concentration':'union of sources ranked by realized contribution (common resolved, new/denied admissions, censored timing distinctly labeled); removing contributions is accounting sensitivity NOT a new replay',
        'censor':'OPEN net and paired delta remain null; realized stream timing contributions separately from resolved pairing'}
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    write_report(summary,comparisons,manifest)
    finalize_from_runs()
    print('DONE',OUT,flush=True)


def write_report(s,comparisons,m):
    lines=['# GAP fixo e dominância — replay sistêmico',f"Base congelada {m['start_brt']} → {m['end_brt']}. ATR1m_entry congelado; activation10; razões10:5/10:7/10:9/10:13.",
        'Quatro trajetórias independentes, todos os sinais, engine/CB/slots/spacing originais. GAP5 com paridade integral. Sem alterações operacionais.',
        'Durações de dominância: owner real do engine no início do minuto, transições rotuladas no boundary; não ticks reais. Peaked levels usam primeiro ponto observado >=nível e podem ultrapassá-lo. Meses de net seguem a saída de cada braço.']
    for path in PATHS:
        lines+=[f'## {path}',table(['mês','GAP','entradas','closed','open fim','net $','delta $','net/trade','PF','DD $','win %','idade média/med','sim média/max','capital médio/max','slot-h','cap bloqueios','HS','PL','TRAIL','CB crises','CB h'],[
            [mo,arm,r['entries'],r['closed'],r['open_at_window_end'],fmt(r['net']),fmt(r['delta_vs_gap5']),fmt(r['net_trade']),fmt(r['pf']),fmt(r['dd']),fmt(r['win_rate']*100) if r['win_rate'] is not None else 'N/A',fmt(r['mean_age'])+'/'+fmt(r['median_age']),fmt(r['mean_simultaneous'])+'/'+str(r['max_sim']),fmt(r['capital_mean'])+'/'+fmt(r['capital_max']),fmt(r['slot_hours']),r['blocked_capacity'],r['HARD_STOP'],r['PROFIT_LOCK'],r['TRAILING'],r['crises'],fmt(r['cooldown_h'])] for mo in MONTHS for arm,windows in s[path].items() for r in [windows[mo]]]),
            '### Dominância agregada',table(['GAP','ativou','TRAIL>PL','empate','PL>TRAIL','sem PL','PL2 não armado','TRAIL dominou','ativou/nunca dominou','peak primeira dom med ATR','atraso med min','avanço med ATR','PL h','TRAIL h','outro h'],[
            [arm,r['activated'],r['activation_cases'].get('TRAIL_ABOVE_PL',0),r['activation_cases'].get('TIE',0),r['activation_cases'].get('PL_ABOVE_TRAIL',0),r['activation_cases'].get('NO_PL',0),r['activation_PL2_not_armed'],r['first_dominance']['n'],r['activated_never_dominated'],fmt(r['first_dominance']['peak_atr']['median']),fmt(r['first_dominance']['minutes_from_activation']['median']),fmt(r['first_dominance']['advance_from_activation_atr']['median']),fmt(sum(v for k,v in r['floor_hours'].items() if k.startswith('PL'))),fmt(r['floor_hours'].get('TRAIL',0)),fmt(sum(v for k,v in r['floor_hours'].items() if not k.startswith('PL') and k!='TRAIL'))] for arm,windows in s[path].items() for r in [windows['ALL']]]),
            table(['GAP','nível peak observado','N','owners'],[[arm,l,x['n'],json.dumps(x['owners'])] for arm,windows in s[path].items() for l,x in windows['ALL']['peak_levels'].items()])]
        for arm,c in comparisons[path].items():
            lines+=[f'### {arm}: decomposição econômica',json.dumps(c['decomposition']),
                'Matriz: somente pares resolvidos entram no delta; OPEN permanece não resolvido.',
                table(['transição','N','resolvidos','delta $','adicional med min','giveback adicional med %','recuperou/novo peak'],[[k,r['n'],r['resolved'],fmt(r['delta']),fmt(r['additional_min']['median']),fmt(r['giveback_additional_pct']['median']),str(r['recovery_n'])+'/'+str(r['new_peak_n'])] for k,r in c['transitions'].items()]),
                table(['grupo','N','saldo $','delta med','adicional med min'],[[k,c[k]['n'],fmt(c[k]['delta']),fmt(c[k]['delta_dist']['median']),fmt(c[k]['additional_min']['median'])] for k in ('benefit','cost','all_improved','all_worsened')]),
                table(['controle peak ATR','N comuns','net variante $','delta $','giveback med %','idade med','novo peak N','exits'],[[b,r['n'],fmt(r['variant_net']),fmt(r['delta']),fmt(r['giveback_variant_pct']['median']),fmt(r['variant_age']['median']),r['new_peak_n'],json.dumps(r['reasons'])] for b,r in c['bands'].items()]),
                table(['top N','contrib $','particip delta %','delta sem top $'],[[n,fmt(r['contribution']),fmt(r['share_systemic_delta']*100) if r['share_systemic_delta'] is not None else 'N/A',fmt(r['systemic_delta_excluding_top'])] for n,r in c['concentration'].items()]),
                f"Capacidade: {len(c['capacity_blocked'])} bloqueios; {c['capacity_control_admitted_n']} admitidos no controle por source exato; net observado destes no controle={fmt(c['capacity_control_observed_net'])}. Não é PnL causal garantido de sinais hipoteticamente readmitidos."]
    lines+=['## Limites','In-sample, apenas2 ordens OHLC hipotéticas; sem estimar intraminuto. Efeito sistêmico real do replay é net independente, não apenas common-only. Comparações por source são decomposição, sem novas entradas forçadas. Sem testar ativações, outros GAPs ou contextos.']
    (OUT/'report.md').write_text('\n\n'.join(lines),encoding='utf-8')


def safe_json(value):
    if isinstance(value,dict):return {k:safe_json(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [safe_json(v) for v in value]
    if isinstance(value,float) and not math.isfinite(value):
        if math.isnan(value):raise ValueError('NaN in study artifact')
        return 'inf' if value>0 else '-inf'
    return value


def finalize_from_runs():
    """Audit presentation from canonical systemic events, without new replays."""
    m=json.loads((OUT/'manifest.json').read_text());start=ms(m['start_brt']);end=ms(m['end_brt'])
    s={};comp={};audit={'paths':{},'wallclock_detail_timestamps_ignored':0}
    for path in PATHS:
        s[path]={};comp[path]={};base=None;audit['paths'][path]={}
        for gap in GAPS:
            name=f'GAP_{gap}';f=OUT/f'{path}_{name}.json';data=json.loads(f.read_text());run=data['run'];detail=data['details']
            if gap==5:
                assert run==json.loads((INPUT/f'{path}_systemic.json').read_text())['BE_OFF_CB'];base=run
            by_source={t['source_candle']:t for t in run['trades']}
            assert len(detail)==len(by_source)
            for d in detail:
                t=by_source[d['source_candle']]
                if d['closed_ms']!=t['closed_ms']:
                    d['engine_wallclock_closed_ms_ignored']=d['closed_ms']
                if d.get('engine_wallclock_closed_ms_ignored') is not None:
                    audit['wallclock_detail_timestamps_ignored']+=1
                d['closed_ms']=t['closed_ms']
                assert start<=d['opened_ms']<=end
                limit=d['closed_ms'] or end
                assert d['opened_ms']<=limit<=end
                assert sum(d['floor_minutes'].values())==(limit-d['opened_ms'])/MINUTE_MS
                d['after_activation_floor_minutes']=active_floor_minutes(d,end)
                assert sum(d['after_activation_floor_minutes'].values())<=sum(d['floor_minutes'].values())+1e-8
                for row in d['floor_events']:assert d['opened_ms']<=row['at_ms']<=limit
                if d['activation']:assert d['opened_ms']<=d['activation']['at_ms']<=limit
                if d['first_dominance']:assert d['activation']['at_ms']<=d['first_dominance']['at_ms']<=limit
            slot_minutes=sum(x['slot_minutes'] for x in data['exposure'].values())
            assert slot_minutes==sum(sum(d['floor_minutes'].values()) for d in detail)
            assert slot_minutes==sum(((t['closed_ms'] or end)-t['opened_ms'])/MINUTE_MS for t in run['trades'])
            audit['paths'][path][name]={'entries':len(detail),'slot_minutes':slot_minutes,
                'floor_minutes':sum(sum(d['floor_minutes'].values()) for d in detail),
                'after_activation_minutes':sum(sum(d['after_activation_floor_minutes'].values()) for d in detail),
                'canonical_time_and_exposure_verified':True}
            s[path][name]={mo:enrich_stats(run,detail,data['exposure'],mo,start,end,m['notional']) for mo in MONTHS}
            if gap!=5:
                comp[path][name]=paired_analysis(base,run,detail,start,end)
                for mo in MONTHS:s[path][name][mo]['delta_vs_gap5']=s[path][name][mo]['net']-s[path]['GAP_5'][mo]['net']
            else:
                for mo in MONTHS:s[path][name][mo]['delta_vs_gap5']=0
            f.write_text(json.dumps(safe_json(data),allow_nan=False),encoding='utf-8')
    m['tool_sha256']=digest(Path(__file__))
    m['concentration']='union of sources, ranked by realized systemic contribution; changed admissions and censored timing labeled, removal is accounting sensitivity only'
    m['detail_exit_clock']='canonical closed_ms from systemic trade records, not engine mock wall clock'
    m['PL_before_dominance']='PL_step in first_dominance is the actual active PL after original engine arms PLs and before updating trailing; previous_owner denotes previous tick, not necessarily this intratick PL'
    m['capital_limit']='nominal commitment at configured fixed20USDT per position; original replay has no additional cash-solvency/resizing gate as realized equity changes'
    m['monthly_dd_definition']='monthly closed-trade realized curve starts zero for metric; ALL full realized curve. Replay and CB remain continuous across months, never reset monthly.'
    for name,value in [('summary',s),('comparisons',comp),('audit',audit),('manifest',m)]:
        (OUT/f'{name}.json').write_text(json.dumps(safe_json(value),indent=2,allow_nan=False),encoding='utf-8')
    write_report(s,comp,m)
    with (OUT/'report.md').open('a',encoding='utf-8') as f:
        f.write('\n\n## Tempo sob PL após ativar TRAIL\n\n'+table(['path','GAP','PL h após ativação','TRAIL h após ativação','PL vigente ao ser superado'],[
            [p,arm,fmt(sum(v for k,v in r['after_activation_floor_hours'].items() if k.startswith('PL'))),fmt(r['after_activation_floor_hours']['TRAIL']),json.dumps(r['first_dominance']['PL_vigente_superado'])]
            for p in PATHS for arm,windows in s[p].items() for r in [windows['ALL']]]))
        f.write('\n\nA largura e o atraso de dominância variam juntos nestes quatro braços. A telemetria explica o mecanismo, mas não permite atribuir causalmente uma porcentagem do net a cada efeito sem outro experimento — não feito. Capital é compromisso nominal pelo sizing original fixo; não foi adicionado gate de solvência de caixa.\n')


if __name__=='__main__':main()
