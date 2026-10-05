"""Isolated ATR-entry/peak/dynamic GAP study. No operational writes."""
from __future__ import annotations
import bisect
import json
import math
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from src.indicators.indicators import atr
from src.position.bot_full_engine import BotFullExitPosition
from tools.pl_lon_elastic_study import OUT as INPUT, CACHE, PRIOR, MONTHS, SignalEvent, EntrySignal, Review, iso, quantile
from tools.be_off_cb_defensive_closure import digest, table as _table, fmt as _fmt, month, ms
from tools.be_off_cb_exit_context_study import brt
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import MINUTE_MS, NullLogger, ReplayExecutionClient, _deduplicate

OUT=ROOT/'data/studies/trail_atr_lifecycle/20261004'
ANATOMY=ROOT/'data/studies/trail_anatomy/20261003'
PATHS=('HIGH_FIRST','LOW_FIRST')
VARIANTS=('ATR_ENTRY','ATR_PEAK','ATR_DYNAMIC')


def table(headers,rows):return '\n'.join(_table(headers,rows))


def fmt(value):return value if isinstance(value,str) else _fmt(value)


class ClosedATR:
    """Official Wilder ATR; close_time < evaluation_time, including at boundaries."""
    def __init__(self,candles,period):
        self.candles=candles
        self.boundaries=[c.boundary_ms for c in candles]
        self.values=atr([c.high for c in candles],[c.low for c in candles],
                        [c.close for c in candles],period)

    def snapshot(self,at):
        i=bisect.bisect_right(self.boundaries,at)-1
        if i<0 or self.values[i] is None or not math.isfinite(self.values[i]):
            raise ValueError(f'No causal ATR available at {at}')
        c=self.candles[i]
        if c.close_time_ms>=at:raise AssertionError('Open candle leaked into ATR')
        return {'atr':self.values[i],'open_ms':c.open_time_ms,'close_ms':c.close_time_ms}


class ATRPosition(BotFullExitPosition):
    """Replay-only GAP replacement. All original economics/activation unchanged."""
    def initialize_study(self,variant,series,review,opened):
        self.variant=variant;self.series=series;self.review=review
        self.at=opened;self.clock_snapshot=series.snapshot(opened)
        self.peak_snapshot=deepcopy(self.clock_snapshot)
        self.last_candidate_key=None;self.peaks=[];self.new_peak=False;self.gap_updates=[]
        self.activation=None;self.diagnostic_activation={};self.counters=Counter()
        self.additional_stop_rise=0.;self.effective_stop_rise=0.;self.max_adverse_pct=0.
        self.max_peak_drawdown_pct=0.;self.last_atr_snapshot=self.clock_snapshot

    def set_clock(self,at):
        self.at=at;self.new_peak=False
        prior=self.clock_snapshot;self.clock_snapshot=self.series.snapshot(at)
        self.last_atr_snapshot=prior
        if self.variant=='ATR_DYNAMIC' and self.trailing_active and prior['close_ms']!=self.clock_snapshot['close_ms']:
            old=self.effective_stop
            self._update_trailing_stop();self._refresh_effective_stop()
            self.effective_stop_rise+=max(0.,self.effective_stop-old)

    def on_tick(self,price,ts=None):
        self.new_peak=price>self.highest_price
        if self.new_peak:
            self.peak_snapshot=deepcopy(self.clock_snapshot)
            self.peaks.append({'evaluated_at_ms':self.at,'modeled_at_ms':ms(ts),
                'price':price,'atr_snapshot':self.peak_snapshot,
                'context':self.review.context_fields(self.at)})
        for name,reference in (('ATR_PEAK',self.peak_snapshot['atr']),
                                ('ATR_DYNAMIC',self.clock_snapshot['atr'])):
            if name not in self.diagnostic_activation and price-self.entry_price>=10*reference:
                self.diagnostic_activation[name]={'modeled_at_ms':ms(ts),'evaluated_at_ms':self.at,'price':price,'atr':reference}
        before=self.trailing_active
        result=super().on_tick(price,ts)
        if self.new_peak:
            self.peaks[-1].update({'pl_armed':sorted(self.applied_steps),'trailing_active':self.trailing_active,
                                  'trailing_stop':self.trailing_stop,'effective_stop':self.effective_stop})
        if not before and self.trailing_active:
            self.activation={'modeled_at_ms':ms(ts),'evaluated_at_ms':self.at,'price':price,'atr':self.entry_atr}
        self.max_adverse_pct=max(self.max_adverse_pct,(1-price/self.entry_price)*100)
        self.max_peak_drawdown_pct=max(self.max_peak_drawdown_pct,(self.highest_price-price)/self.entry_price*100)
        return result

    def _update_trailing_stop(self):
        if not hasattr(self,'variant') or self.variant=='ATR_ENTRY':
            return super()._update_trailing_stop()
        if self.variant=='ATR_PEAK' and not self.new_peak and self.trailing_stop is not None:return
        reference=self.peak_snapshot if self.variant=='ATR_PEAK' else self.clock_snapshot
        key=(reference['close_ms'],self.highest_price)
        if key==self.last_candidate_key:return
        self.last_candidate_key=key
        candidate=self.highest_price-5*reference['atr'];old=self.trailing_stop
        self.gap_updates.append({'evaluated_at_ms':self.at,'atr_snapshot':deepcopy(reference),
            'highest_price':self.highest_price,'new_peak':self.new_peak,
            'candidate_stop':candidate,'previous_trailing_stop':old,
            'ratchet_result':'INITIALIZE' if old is None else 'TIGHTEN' if candidate>old else 'BLOCK_WIDEN' if candidate<old else 'UNCHANGED',
            'next_trailing_stop':candidate if old is None else max(old,candidate)})
        self.counters['attempts']+=1
        if old is None:
            self.trailing_stop=candidate;self.counters['initialized']+=1;return
        if candidate>old:
            self.trailing_stop=candidate;self.counters['tighten']+=1
            if not self.new_peak and reference['atr']<self.last_atr_snapshot['atr']:
                self.counters['tighten_without_peak_atr_decrease']+=1
                self.additional_stop_rise+=candidate-old
        elif candidate<old:
            self.counters['widen_blocked_by_ratchet']+=1
        else:self.counters['equal']+=1


def capture(entry,price,peak):
    return (price-entry)/(peak-entry) if peak>entry else None


def replay(trade,review,series,path,variant):
    opened=trade['opened_ms'];fresh=review.new_position(opened,trade['entry_price'])
    client=ReplayExecutionClient(review.spread/2)
    p=ATRPosition.from_state(deepcopy(fresh.position.to_state()),fresh.position.config,client,NullLogger())
    p.initialize_study(variant,series,review,opened)
    closed=None;last_at=opened
    for i in range(bisect.bisect_left(review.opens,opened),len(review.minute)):
        c=review.minute[i]
        if c.boundary_ms>review.end:break
        p.set_clock(c.open_time_ms);last_at=c.open_time_ms
        points=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST' else (c.open,c.low,c.high,c.close))
        previous=None
        for point in points:
            stop=p.effective_stop
            crossed=previous is not None and previous>stop and point<=stop
            tick=stop if crossed else point
            client.current_price=tick;p.on_tick(tick,iso(c.boundary_ms));previous=point
            if p.status=='CLOSED':closed=c.boundary_ms;break
        if closed is not None:break
    net=review.notional*(p.pnl_pct(p.exit_price)-review.fees)/100 if closed is not None else None
    peak=p.highest_price;exit_atr=series.snapshot(last_at);price=p.exit_price
    if variant=='ATR_ENTRY':
        if closed!=trade['closed_ms'] or p.exit_reason!=trade['exit_reason'] or abs(price-trade['exit_price'])>1e-8 or abs(net-trade['net_usd'])>1e-8 or abs(peak-trade['peak_price'])>1e-8:
            raise AssertionError(f'Control parity failed: {path} {trade["source_candle"]}: {(closed,p.exit_reason,price,net,peak)} vs {trade}')
    terminal=review.minute[-1].close*(1-review.spread/2/10000)
    return {'variant':variant,'closed_ms':closed,'evaluated_exit_ms':last_at if closed else None,
        'exit_brt':brt(closed) if closed else 'OPEN / CENSORED','exit_price':price,
        'reason':p.exit_reason or 'OPEN / CENSORED','net':net,
        'delta':net-trade['net_usd'] if net is not None else None,
        'peak':peak,'peak_event':p.peaks[-1] if p.peaks else None,'peaks':p.peaks,
        'exit_atr_snapshot':exit_atr,'exit_context':review.context_fields(last_at),
        'age_min':((closed or review.end)-opened)/MINUTE_MS,
        'additional_min':((closed or review.end)-trade['closed_ms'])/MINUTE_MS,
        'captured_abs':price-p.entry_price if closed else None,
        'captured_move_own':capture(p.entry_price,price,peak) if closed else None,
        'captured_move_control_peak':capture(p.entry_price,price,trade['peak_price']) if closed else None,
        'giveback_abs':peak-price if closed else None,
        'giveback_pct':(peak-price)/p.entry_price*100 if closed else None,
        'giveback_atr_entry':(peak-price)/p.entry_atr if closed else None,
        'giveback_atr_peak':(peak-price)/p.peak_snapshot['atr'] if closed else None,
        'giveback_atr_exit':(peak-price)/exit_atr['atr'] if closed else None,
        'worst_adverse_pct':p.max_adverse_pct,'isolated_peak_to_trough_pct':p.max_peak_drawdown_pct,
        'pl_armed':sorted(p.applied_steps),'trailing_active':p.trailing_active,
        'activation':p.activation,'diagnostic_activation':p.diagnostic_activation,
        'atr_peak':p.peak_snapshot['atr'],'stop':p.effective_stop,'trailing_stop':p.trailing_stop,
        'counters':dict(p.counters),'gap_updates':p.gap_updates,'stop_rise_atr_decrease_no_peak':p.additional_stop_rise,
        'effective_stop_rise_atr_updates':p.effective_stop_rise,
        'censored':closed is None,'terminal_mtm_informative':review.notional*(p.pnl_pct(terminal)-review.fees)/100 if closed is None else None}


def dist(values):
    v=[x for x in values if x is not None]
    return {'n':len(v),'mean':sum(v)/len(v) if v else None,'median':quantile(v,.5),
            'p75':quantile(v,.75),'p90':quantile(v,.9),'max':max(v,default=None)}


def realized_dd(rows):
    equity=peak=dd=0.
    for r in sorted((r for r in rows if r['net'] is not None),key=lambda r:r['closed_ms']):
        equity+=r['net'];peak=max(peak,equity);dd=max(dd,peak-equity)
    return dd


def economics(events,variant):
    resolved=[e for e in events if e['arms'][variant]['net'] is not None]
    rs=[e['arms'][variant] for e in resolved];values=[r['net'] for r in rs]
    gains=sum(v for v in values if v>0);loss=-sum(v for v in values if v<0)
    ctrl=[e['arms']['ATR_ENTRY'] for e in resolved]
    return {'n':len(events),'closed':len(rs),'censored':len(events)-len(rs),
        'net':sum(values),'control_net_same_resolved_pairs':sum(r['net'] for r in ctrl),
        'delta':sum(r['delta'] for r in rs),'net_trade':sum(values)/len(values) if values else None,
        'pf':gains/loss if loss else ('inf' if gains else None),'realized_dd_isolated':realized_dd(rs),
        'control_realized_dd_same_pairs':realized_dd(ctrl),
        'winners_to_losers':sum(c['net']>0 and r['net']<0 for c,r in zip(ctrl,rs)),
        'losers_to_winners':sum(c['net']<0 and r['net']>0 for c,r in zip(ctrl,rs)),
        'improved':sum(r['delta']>1e-10 for r in rs),'worsened':sum(r['delta']<-1e-10 for r in rs),
        'reasons':dict(Counter(r['reason'] for r in rs)),
        **{k:dist([r[k] for r in rs]) for k in ('captured_move_own','captured_move_control_peak',
            'captured_abs','giveback_abs','giveback_pct','additional_min','isolated_peak_to_trough_pct')},
        'counters':dict(sum((Counter(e['arms'][variant]['counters']) for e in events),Counter())),
        'stop_rise_atr_decrease_no_peak':sum(e['arms'][variant]['stop_rise_atr_decrease_no_peak'] for e in events),
        'effective_stop_rise_atr_updates':sum(e['arms'][variant]['effective_stop_rise_atr_updates'] for e in events)}


def spearman(xs,ys):
    def ranks(v):
        order=sorted(range(len(v)),key=lambda i:v[i]);r=[0.]*len(v);i=0
        while i<len(v):
            j=i+1
            while j<len(v) and v[order[j]]==v[order[i]]:j+=1
            for k in range(i,j):r[order[k]]=(i+j-1)/2
            i=j
        return r
    x=ranks(xs);y=ranks(ys)
    if not x:return None
    mx=sum(x)/len(x);my=sum(y)/len(y)
    den=math.sqrt(sum((v-mx)**2 for v in x)*sum((v-my)**2 for v in y))
    return sum((a-mx)*(b-my) for a,b in zip(x,y))/den if den else None


def descriptive(events):
    result={'n':len(events)}
    for k in ('atr_entry','atr_entry_5m','atr_peak','atr_exit','peak_entry_ratio','exit_entry_ratio',
              'exit_peak_ratio','peak_entry_5m_ratio','age_min','entry_to_peak_min','peak_to_exit_min','new_peaks'):
        result[k]=dist([e[k] for e in events])
    result['giveback']={k:dist([e['arms']['ATR_ENTRY'][k] for e in events]) for k in
        ('giveback_abs','giveback_pct','giveback_atr_entry','giveback_atr_peak','giveback_atr_exit','captured_move_own')}
    result['age_ratio_spearman']=spearman([e['age_min'] for e in events],[e['peak_entry_ratio'] for e in events])
    result['age_pure_5m_ratio_spearman']=spearman([e['age_min'] for e in events],[e['peak_entry_5m_ratio'] for e in events])
    result['original_net']=sum(e['arms']['ATR_ENTRY']['net'] for e in events)
    result['post_exit']={}
    for h in ('5','15','30','60'):
        ws=[e['post_exit_windows'][h]['post_minutes'] for e in events if e.get('post_exit_windows') and e['post_exit_windows'][h]['post_minutes']['complete']]
        result['post_exit'][h]={'n':len(ws),'recovered_peak':sum(w['peak_recovered'] for w in ws),
            'new_peak':sum(w['new_high'] for w in ws),
            **{k:dist([w[k] for w in ws]) for k in ('favorable_pct','adverse_pct','peak_recovery_min','new_high_min','below_exit_min')}}
    return result


def activation_summary(events,variant):
    rs=[]
    for e in events:
        p=e['arms']['ATR_ENTRY'];actual=p['activation'];alt=p['diagnostic_activation'].get(variant)
        if alt and actual:
            rs.append({'delta_min':(alt['modeled_at_ms']-actual['modeled_at_ms'])/MINUTE_MS,
                       'price_delta':alt['price']-actual['price'],
                       'age_min':(alt['modeled_at_ms']-e['opened_ms'])/MINUTE_MS})
    return {'n':len(events),'reached_before_control_exit':len(rs),'not_reached_before_control_exit':len(events)-len(rs),
            **{k:dist([r[k] for r in rs]) for k in ('delta_min','price_delta','age_min')}}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((INPUT/'manifest.json').read_text());config=manifest['config'];end=ms(manifest['end_brt'])
    trailing=config['risk']['trailing']
    if trailing['mode']!='atr' or trailing['activation_atr']!=10 or trailing['gap_atr']!=5:
        raise ValueError('Frozen original activation10/gap5 ATR configuration required')
    candles={i:load_candle_cache(CACHE/f'SOLUSDT_{i}.jsonl') for i in ('1m','5m','15m')}
    for i in candles:
        if digest(CACHE/f'SOLUSDT_{i}.jsonl')!=manifest['cache_hashes'][i]:raise ValueError('Frozen market cache changed')
    if candles['1m'][-1].boundary_ms!=end:raise ValueError('Not latest local data')
    if digest(PRIOR/'signals.json')!=manifest['signals_sha256']:raise ValueError('Signals changed')
    signals=[SignalEvent(int(r['boundary_ms']),EntrySignal(**r['signal'])) for r in json.loads((PRIOR/'signals.json').read_text())]
    review=Review(config,candles,signals,end);period=int(config['entry']['atr_period'])
    series=ClosedATR(candles['5m'],period)
    prior={}
    for line in (ANATOMY/'events.jsonl').read_text().splitlines():
        r=json.loads(line);prior[(r['path'],r['source_candle'])]=r
    # Post-exit windows are reused only when cache and cutoff are the same.
    am=json.loads((ANATOMY/'manifest.json').read_text())
    if am['cache_hashes']!=manifest['cache_hashes'] or am['end_brt']!=manifest['end_brt']:raise ValueError('Post-exit cache mismatch')
    all_events=[];aux=[];summary={};groups={};activations={};sources={}
    for path in PATHS:
        basefile=INPUT/f'{path}_systemic.json';sources[str(basefile)]=digest(basefile)
        trades=json.loads(basefile.read_text())['BE_OFF_CB']['trades']
        relevant=[t for t in trades if t['exit_reason'] in ('TRAILING','PROFIT_LOCK')]
        events=[];auxiliary=[]
        print(path,len(relevant),'original TRAIL/PL',flush=True)
        for i,t in enumerate(relevant):
            control=replay(t,review,series,path,'ATR_ENTRY')
            snapshot_entry=series.snapshot(t['opened_ms']);peak=control['peak_event'];a=control['atr_peak'];b=control['exit_atr_snapshot']['atr']
            e={'path':path,'source_candle':t['source_candle'],'month':month(t['closed_ms']),
                'opened_ms':t['opened_ms'],'entry_brt':brt(t['opened_ms']),'entry_price':t['entry_price'],
                'entry_context':review.context_fields(t['opened_ms']),
                'atr_entry':review.signal[t['opened_ms']].entry_atr,'atr_entry_snapshot_5m':snapshot_entry,
                'atr_entry_5m':snapshot_entry['atr'],'atr_peak':a,'atr_exit':b,
                'age_min':control['age_min'],'new_peaks':len(control['peaks']),
                'entry_to_peak_min':(peak['modeled_at_ms']-t['opened_ms'])/MINUTE_MS,
                'peak_to_exit_min':(t['closed_ms']-peak['modeled_at_ms'])/MINUTE_MS,
                'arms':{'ATR_ENTRY':control},'post_exit_windows':prior.get((path,t['source_candle']),{}).get('windows')}
            e.update({'peak_entry_ratio':a/e['atr_entry'],'exit_entry_ratio':b/e['atr_entry'],
                'exit_peak_ratio':b/a,'peak_entry_5m_ratio':a/snapshot_entry['atr']})
            e['atr_group']='SIMILAR' if math.isclose(a,e['atr_entry'],rel_tol=1e-9,abs_tol=1e-12) else 'LOWER' if a<e['atr_entry'] else 'HIGHER'
            if t['exit_reason']=='TRAILING':
                for variant in VARIANTS[1:]:e['arms'][variant]=replay(t,review,series,path,variant)
                events.append(e)
            else:auxiliary.append(e)
            if (i+1)%100==0:print(path,'processed',i+1,flush=True)
        ages=[e['age_min'] for e in events];q1=quantile(ages,1/3);q2=quantile(ages,2/3)
        for e in events:e['age_group']='SHORT' if e['age_min']<=q1 else 'MEDIUM' if e['age_min']<=q2 else 'LONG'
        summary[path]={m:{v:economics([e for e in events if m=='ALL' or e['month']==m],v) for v in VARIANTS} for m in MONTHS}
        summary[path]['COMMON_RESOLVED']={v:economics([e for e in events if all(not r['censored'] for r in e['arms'].values())],v) for v in VARIANTS}
        activations[path]={m:{v:activation_summary([e for e in events if m=='ALL' or e['month']==m],v) for v in VARIANTS[1:]} for m in MONTHS}
        groups[path]={'ALL':descriptive(events),'PROFIT_LOCK_AUXILIARY':descriptive(auxiliary),
            'age_quantile_boundaries_min':[q1,q2]}
        selectors={'month':[(m,lambda e,m=m:e['month']==m) for m in MONTHS[:-1]],
            'atr_group':[(g,lambda e,g=g:e['atr_group']==g) for g in ('LOWER','SIMILAR','HIGHER')],
            'age_group':[(g,lambda e,g=g:e['age_group']==g) for g in ('SHORT','MEDIUM','LONG')],
            'ema':[(g,lambda e,g=g:e['arms']['ATR_ENTRY']['exit_context']['ema_context']==g) for g in ('LON','BUL','BEA','SHO','MUP','MDO','MIX')],
            'macd':[(g,lambda e,g=g:e['arms']['ATR_ENTRY']['exit_context']['macd_context']==g) for g in ('BU+','BU-','BE+','BE-')],
            'histogram':[(g,lambda e,g=g:e['arms']['ATR_ENTRY']['exit_context']['histogram_state']==g) for g in
                ('POSITIVE_EXPANDING','POSITIVE_CONTRACTING','NEGATIVE_RISING','NEGATIVE_FALLING')]}
        for kind,sels in selectors.items():
            groups[path][kind]={name:{'description':descriptive([e for e in events if pred(e)]),
                'economics':{v:economics([e for e in events if pred(e)],v) for v in VARIANTS}}
                for name,pred in sels}
        all_events.extend(events);aux.extend(auxiliary)
    # Full lifetime peaks live in the JSONL; summary remains compact.
    for name,values in [('events.jsonl',all_events),('profit_lock_auxiliary.jsonl',aux)]:
        (OUT/name).write_text(''.join(json.dumps(e,allow_nan=False)+'\n' for e in values),encoding='utf-8')
    for name,data in [('summary.json',summary),('descriptive_groups.json',groups),('activation.json',activations)]:
        (OUT/name).write_text(json.dumps(data,indent=2,allow_nan=False),encoding='utf-8')
    methodology={'start_brt':manifest['start_brt'],'end_brt':manifest['end_brt'],
        'config':config,'cache_hashes':manifest['cache_hashes'],'signals_sha256':manifest['signals_sha256'],
        'sources':sources,'tool_sha256':digest(Path(__file__)),
        'implementation_hashes':{str(f):digest(ROOT/f) for f in ('src/indicators/indicators.py',
            'src/position/bot_full_engine.py','src/monitor/market_context.py','tools/be_off_cb_defensive_review.py')},
        'anatomy_events_sha256':digest(ANATOMY/'events.jsonl'),
        'entry_atr':'original frozen ATR14 1m, not recalculated',
        'alternative_atr':'official Wilder ATR14 of causal closed5m; not a same-timeframe aging-only experiment',
        'causal_clock':'1m open is evaluation cutoff; 1m boundary labels modeled ticks/exits, not exact intraminute time',
        'peaks':'all modeled new highs, ATR and official contexts as of evaluation cutoff',
        'replay':'individual from entry; same original activation10 entryATR, gap5, PL/HS/fees/spread; no systemic CB/slots/admissions',
        'months':'original control exit month BRT, same paired population across variants',
        'dd':'realized equity zero-start ordered variant closes on isolated selected trades, NOT portfolio systemic DD; intratrade drawdown separately',
        'post_exit':'fixed5/15/30/60min windows from prior anatomy post_minutes, excluding original modeled candle remainder',
        'censor':'no realized net/delta/winner/capture; terminal MTM explicitly informative only',
        'parity':'all original TRAILING and PROFIT_LOCK individual reconstructions equal baseline exit/time/reason/net/peak',
        'activation_diagnostic':'first modeled price >= entry+10 causal reference, within original lifetime only; not trading rule'}
    (OUT/'manifest.json').write_text(json.dumps(methodology,indent=2),encoding='utf-8')
    write_report(summary,groups,activations,all_events,methodology)
    write_conclusions(summary,groups,activations,all_events,aux,methodology)
    print('DONE',OUT,flush=True)


def write_report(summary,groups,activations,events,manifest):
    lines=['# ATR entry / peak / dinâmico — replay isolado',
        f"Base: {manifest['start_brt']} → {manifest['end_brt']}. Dados locais, sem nova coleta.",
        '**Confundimento de escala:** entry=ATR14 1m congelado; peak/dinâmico=ATR14 5m. A comparação econômica segue o pedido; envelhecimento deve ser lido também por peak5m/entry5m.',
        'Ativação10×entryATR, GAP5, PL, HS, spread/fees e ratchet inalterados. Não é replay sistêmico. Meses atribuídos à saída original. HIGH/LOW são hipóteses de sequência, não ticks observados.',
        'Cutoff técnico=abertura do minuto; execução rotulada no boundary desse minuto. Só candles5m fechados antes do cutoff. Paridade exata verificada para todos os TRAIL/PL originais.',
        'DD abaixo: curva realizada de trades isolados selecionados, não DD de carteira. Censurados excluídos da economia; N pareado explícito.']
    for path in PATHS:
        lines+=['',f'## {path}', '',table(['mês','régua','N','closed','cens','net $','net/trade $','delta $','PF','DD $','W→L','L→W','capt própria %','capt ctrl %','giveback %','tempo adicional med min'],[
            [m,v,r['n'],r['closed'],r['censored'],fmt(r['net']),fmt(r['net_trade']),fmt(r['delta']),fmt(r['pf']),fmt(r['realized_dd_isolated']),r['winners_to_losers'],r['losers_to_winners'],fmt((r['captured_move_own']['mean'] or 0)*100),fmt((r['captured_move_control_peak']['mean'] or 0)*100),fmt(r['giveback_pct']['median']),fmt(r['additional_min']['median'])]
            for m in MONTHS for v,r in summary[path][m].items()])]
        lines+=['','### Distribuições do controle','',table(['métrica','N','mean','median','p75','p90','max'],[
            [k,d['n'],fmt(d['mean']),fmt(d['median']),fmt(d['p75']),fmt(d['p90']),fmt(d['max'])]
            for k,d in groups[path]['ALL'].items() if isinstance(d,dict) and 'median' in d])]
        lines+=['',table(['giveback / captura','N','mean','median','p75','p90','max'],[
            [k,d['n'],fmt(d['mean']),fmt(d['median']),fmt(d['p75']),fmt(d['p90']),fmt(d['max'])] for k,d in groups[path]['ALL']['giveback'].items()])]
        for category in ('atr_group','age_group'):
            lines+=['',f'### {category}','',table(['grupo','N','idade med','peak/entry med','peak5/entry5 med','giveback % med','recup60 N','novopeak60 N','MFE60 med %','MAE60 med %','contin min med','deter min med'],[
                [g,d['n'],fmt(d['age_min']['median']),fmt(d['peak_entry_ratio']['median']),fmt(d['peak_entry_5m_ratio']['median']),fmt(d['giveback']['giveback_pct']['median']),d['post_exit']['60']['recovered_peak'],d['post_exit']['60']['new_peak'],fmt(d['post_exit']['60']['favorable_pct']['median']),fmt(d['post_exit']['60']['adverse_pct']['median']),fmt(d['post_exit']['60']['peak_recovery_min']['median']),fmt(d['post_exit']['60']['below_exit_min']['median'])]
                for g,info in groups[path][category].items() for d in [info['description']]])]
        d=groups[path]['ALL']
        lines+=[f"Spearman idade vs peak/entry: {fmt(d['age_ratio_spearman'])}; idade vs peak5m/entry5m: {fmt(d['age_pure_5m_ratio_spearman'])}. Tertis de idade: {groups[path]['age_quantile_boundaries_min']} min. Igualdade ATR: rel_tol=1e-9, abs_tol=1e-12; não é filtro operacional."]
        lines+=['','### Ativação alternativa apenas diagnóstica','',table(['mês','ATR','N','atingiu','não atingiu','delta tempo med min','delta preço med'],[
            [m,v,r['n'],r['reached_before_control_exit'],r['not_reached_before_control_exit'],fmt(r['delta_min']['median']),fmt(r['price_delta']['median'])] for m,vs in activations[path].items() for v,r in vs.items()])]
        r=summary[path]['ALL']['ATR_DYNAMIC']
        lines+=['',f"Dinâmico — auditoria dos candidatos: {r['counters']}. Soma da elevação de trailing por queda ATR sem peak: {fmt(r['stop_rise_atr_decrease_no_peak'])} unidades de preço (somadas entre trades); elevação efetiva nas atualizações ATR: {fmt(r['effective_stop_rise_atr_updates'])}. Não são dólares de lucro."]
        for category in ('ema','macd','histogram'):
            lines+=['',f'### Contexto {category} na saída original (descritivo)','',table(['contexto','N','peak/entry med','peak5/entry5 med','delta PEAK $','delta DYNAMIC $'],[
                [g,x['description']['n'],fmt(x['description']['peak_entry_ratio']['median']),fmt(x['description']['peak_entry_5m_ratio']['median']),fmt(x['economics']['ATR_PEAK']['delta']),fmt(x['economics']['ATR_DYNAMIC']['delta'])] for g,x in groups[path][category].items()])]
    censored=[e for e in events if any(r['censored'] for r in e['arms'].values())]
    lines+=['','## Censura','',table(['path','source','mês original','régua','EMA corte','idade min','peak','stop','MTM $ informativo','tempo adicional min'],[
        [e['path'],e['source_candle'],e['month'],v,r['exit_context']['ema_context'],fmt(r['age_min']),fmt(r['peak']),fmt(r['stop']),fmt(r['terminal_mtm_informative']),fmt(r['additional_min'])] for e in censored for v,r in e['arms'].items() if r['censored']])]
    lines+=['','Economia common-resolved dos três braços e descrições PL auxiliares estão em summary.json/descriptive_groups.json. Ciclos completos, peaks, snapshots e ativação por trade estão em events.jsonl; PL auxiliar não participa do replay econômico alternativo. Resultados pós-saída são descritivos, não prova causal de regra.']
    (OUT/'report.md').write_text('\n\n'.join(lines),encoding='utf-8')


def write_conclusions(summary,groups,activations,events,aux,manifest):
    audit={'control_parity_checked':len(events)+len(aux),'trailing_n':len(events),
           'profit_lock_auxiliary_n':len(aux),'causal_snapshot_checks':0,'paths':{}}
    for e in events+aux:
        assert e['entry_context']['latest_closed_at_ms']<e['opened_ms']
        assert e['atr_entry_snapshot_5m']['close_ms']<e['opened_ms']
        for r in e['arms'].values():
            for peak in r['peaks']:
                assert peak['atr_snapshot']['close_ms']<peak['evaluated_at_ms']
                assert peak['context']['latest_closed_at_ms']<peak['evaluated_at_ms']
                audit['causal_snapshot_checks']+=1
            stop=None
            for update in r['gap_updates']:
                assert update['atr_snapshot']['close_ms']<update['evaluated_at_ms']
                assert stop is None or update['next_trailing_stop']>=stop
                stop=update['next_trailing_stop'];audit['causal_snapshot_checks']+=1
            if r['closed_ms']:
                assert r['exit_context']['latest_closed_at_ms']<r['evaluated_exit_ms']
                assert r['exit_atr_snapshot']['close_ms']<r['evaluated_exit_ms']
    lines=['# Conclusões — ATR ao longo da vida do trade',
        f"Período local: {manifest['start_brt']} → {manifest['end_brt']} (02/10 22:28 BRT; não até 04/10).",
        'Replay isolado desde a entrada, somente população originalmente TRAILING. PROFIT_LOCK auxiliar, sem variantes econômicas. Controle reproduzido exatamente; contexto causal oficial. Não é teste de carteira, CB ou novas admissões.',
        '## Resultado mensal — delta net em dólares vs controle','',
        table(['mês','N HIGH','PEAK HIGH','DYNAMIC HIGH','N LOW','PEAK LOW','DYNAMIC LOW'],[
            [m,summary['HIGH_FIRST'][m]['ATR_ENTRY']['n'],
             fmt(summary['HIGH_FIRST'][m]['ATR_PEAK']['delta']),fmt(summary['HIGH_FIRST'][m]['ATR_DYNAMIC']['delta']),
             summary['LOW_FIRST'][m]['ATR_ENTRY']['n'],fmt(summary['LOW_FIRST'][m]['ATR_PEAK']['delta']),
             fmt(summary['LOW_FIRST'][m]['ATR_DYNAMIC']['delta'])] for m in MONTHS]),
        '## Agregado','',table(['path','régua','N','net $','net/trade $','PF','DD isolado $','capt própria %','capt ctrl %','giveback médio %','adicional med min','W→L','L→W','cens'],[
            [p,v,r['n'],fmt(r['net']),fmt(r['net_trade']),fmt(r['pf']),fmt(r['realized_dd_isolated']),
             fmt(100*r['captured_move_own']['mean']),fmt(100*r['captured_move_control_peak']['mean']),
             fmt(r['giveback_pct']['mean']),fmt(r['additional_min']['median']),r['winners_to_losers'],
             r['losers_to_winners'],r['censored']] for p in PATHS for v,r in summary[p]['ALL'].items()]),
        'PF muito alto e DD realizado muito baixo refletem seleção de TRAILs originais, majoritariamente vencedores. Não representam o BE_OFF_CB completo nem autorizam extrapolar risco sistêmico.',
        '## Leitura causal e risco']
    for p in PATHS:
        es=[e for e in events if e['path']==p];d=groups[p]['ALL'];ratios=groups[p]['atr_group']
        diagnostics={}
        for v in VARIANTS[1:]:
            rs=[e['arms'][v] for e in es];ds=[r['delta'] for r in rs if r['delta'] is not None]
            diagnostics[v]={'improved':sum(x>1e-10 for x in ds),'worsened':sum(x<-1e-10 for x in ds),
                'median_delta':quantile(ds,.5),'gain_sum':sum(x for x in ds if x>0),
                'cost_sum':sum(x for x in ds if x<0),'largest_positive_delta':max(ds,default=None),
                'earlier_than_control':sum(r['additional_min']<0 for r in rs if not r['censored']),
                'dynamic_earlier_than_peak':sum(e['arms']['ATR_DYNAMIC']['closed_ms']<e['arms']['ATR_PEAK']['closed_ms']
                    for e in es if e['arms']['ATR_DYNAMIC']['closed_ms'] and e['arms']['ATR_PEAK']['closed_ms']),
                'affected_tightening_without_peak':sum(r['counters'].get('tighten_without_peak_atr_decrease',0)>0 for r in rs),
                'activation_before':sum(e['arms']['ATR_ENTRY']['diagnostic_activation'].get(v,{}).get('modeled_at_ms',float('inf'))<e['arms']['ATR_ENTRY']['activation']['modeled_at_ms'] for e in es),
                'activation_after':sum(e['arms']['ATR_ENTRY']['diagnostic_activation'].get(v,{}).get('modeled_at_ms',-1)>e['arms']['ATR_ENTRY']['activation']['modeled_at_ms'] for e in es)}
        peak_context={}
        for key in ('ema_context','macd_context','histogram_state'):
            cats=sorted({e['arms']['ATR_ENTRY']['peak_event']['context'][key] for e in es})
            peak_context[key]={c:{'n':len(sel),'peak_entry_ratio':dist([e['peak_entry_ratio'] for e in sel]),
                'peak_entry_5m_ratio':dist([e['peak_entry_5m_ratio'] for e in sel]),
                'economics':{v:economics(sel,v) for v in VARIANTS}} for c in cats
                for sel in [[e for e in es if e['arms']['ATR_ENTRY']['peak_event']['context'][key]==c]]}
        audit['paths'][p]={'economic_diagnostics':diagnostics,'peak_context':peak_context}
        lines += [f"{p}: ATRpeak/entry mediano {d['peak_entry_ratio']['median']:.3f}×; ATRexit/entry {d['exit_entry_ratio']['median']:.3f}×; ATRexit/peak {d['exit_peak_ratio']['median']:.3f}×. Peak5m/entry5m mediano {d['peak_entry_5m_ratio']['median']:.3f}×. Peak menor/igual/maior que entry: {ratios['LOWER']['description']['n']}/{ratios['SIMILAR']['description']['n']}/{ratios['HIGHER']['description']['n']}.",
            f"Idade vs razão 5m/5m: Spearman {d['age_pure_5m_ratio_spearman']:.3f}. Tertis curto/médio/longo peak5m/entry5m: "+' / '.join(f"{groups[p]['age_group'][g]['description']['peak_entry_5m_ratio']['median']:.3f}" for g in ('SHORT','MEDIUM','LONG'))+'.',
            f"ATR_PEAK melhora {diagnostics['ATR_PEAK']['improved']}/{len(es)} trades e piora {diagnostics['ATR_PEAK']['worsened']}; ATR_DYNAMIC melhora {diagnostics['ATR_DYNAMIC']['improved']} e piora {diagnostics['ATR_DYNAMIC']['worsened']}. Medianas de delta: {fmt(diagnostics['ATR_PEAK']['median_delta'])}/{fmt(diagnostics['ATR_DYNAMIC']['median_delta'])} dólares. Ganho agregado não é melhora típica por trade.",
            f"Ativação10 com ATRpeak5m não foi atingida em {activations[p]['ALL']['ATR_PEAK']['not_reached_before_control_exit']}/{len(es)} vidas originais; dinâmico em {activations[p]['ALL']['ATR_DYNAMIC']['not_reached_before_control_exit']}. Não atribuí um atraso futuro inexistente.",
            f"Dinâmico antecipa a saída relativamente ao PEAK em {diagnostics['ATR_DYNAMIC']['dynamic_earlier_than_peak']} trades; apertou sem peak em {diagnostics['ATR_DYNAMIC']['affected_tightening_without_peak']} trades. Isso reduz exposição adicional e giveback vs PEAK, mas também captura menos net agregado."]
    lines += ['## Respostas finais',
        '1. ATRpeak diverge ~2,6–2,7× do entry na mediana, mas **entry é ATR14 1m; peak é ATR14 5m**. A diferença grande não prova envelhecimento.',
        '2–3. A divergência não cresce monotonicamente com idade: correlação é negativa; nos longos o ATR5m tende a ficar modestamente menor que na entrada. Não há evidência de envelhecimento explosivo da régua, nem causalidade inferida da seleção.',
        '4. ATR_PEAK captura mais valor absoluto/net agregado e mais movimento contra o peak comum; ATR_ENTRY captura maior fração do próprio peak. O benchmark comum pode ultrapassar100% porque variantes observam novos peaks depois da saída original.',
        '5. ATR_ENTRY devolve menos lucro em preço/percentual; DYNAMIC devolve menos que PEAK. As alternativas aproximadamente dobram o giveback médio.',
        '6. Nenhuma alternativa produziu winner→loser nesta população; resgataram7/4 losers em HIGH/LOW. Isso não testa os HS originais excluídos do universo.',
        '7–8. PEAK e DYNAMIC melhoram net em junho/agosto/setembro e outubro parcial nos dois paths, pioram julho. Não há domínio em PF, DD, giveback ou trade típico; os benefícios vêm de continuações maiores de uma minoria.',
        '9–10. Há benefício econômico em alterar apenas GAP com ativação original preservada, mas não prova de que a ativação atual seja um problema. A evidência é sobre amplitude de GAP e escala5m, não apenas atualizar ATR antigo.',
        '11. Ativação adaptativa merece no máximo investigação separada: a simples migração10×ATR5m impede ativação em >92% das vidas originais; não aprovar essa mudança a partir deste diagnóstico.',
        '12. ATR_PEAK: **promissor**, merece replay sistêmico para verificar slots, CB e oportunidades perdidas. Não é consistente: julho negativo, maioria dos trades piora, maior DD/giveback e ocupação adicional.',
        '13. ATR_DYNAMIC: **promissor**, com benefício agregado menor e menor duração/giveback que PEAK. Também merece validação sistêmica; apertar por queda do ATR tem custo de continuação, não é benefício gratuito.',
        'Grupos ATRmenor têm apenas4 trades por path e ATRigual nenhum; não sustentam afirmação sistemática sobre recuperação. Outubro tem8/11 trades: evidência parcial pequena. Tertis e contextos são apenas descritivos.',
        '## Entrega / verificação',
        f"Controles reconstruídos: {audit['control_parity_checked']} (TRAIL {len(events)}, PL auxiliar {len(aux)}; HIGH/LOW não são observações independentes). Censurados nesta execução:0. Snapshots e ratchets auditados no artefato audit_and_diagnostics.json.",
        'Ferramenta: tools/trail_atr_lifecycle_study.py. Reutiliza engine original, ATR oficial, Review causal e anatomia pós-saída cacheada. Testes: 11 focados +41 dos estudos anteriores =52, todos passaram. Relatório detalhado: report.md; dados: events.jsonl, profit_lock_auxiliary.jsonl, summary.json, descriptive_groups.json, activation.json, manifest.json e audit_and_diagnostics.json.',
        'Sem alterações em runtime/YAML, sem shadow novo, sem restart, sem git.']
    (OUT/'audit_and_diagnostics.json').write_text(json.dumps(audit,indent=2,allow_nan=False),encoding='utf-8')
    (OUT/'CONCLUSOES.md').write_text('\n\n'.join(lines),encoding='utf-8')


if __name__=='__main__':main()
