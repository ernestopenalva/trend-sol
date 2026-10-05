"""Fixed replay-only PL/LON elasticity study; never imports or writes live state."""
from __future__ import annotations
import bisect
import hashlib
import json
import math
import statistics
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from src.config_profiles import effective_config
from src.monitor.entry_engine import EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from tools.be_off_cb_defensive_review import Review, iso
from tools.be_off_cb_defensive_closure import serialize, metrics, compare, table, fmt, month, ms, digest
from tools.be_off_cb_exit_context_study import brt
from tools.cohort_study import _load_config
from tools.ge_replay_study import SignalEvent, OpenPosition
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import NullLogger, ReplayExecutionClient, MINUTE_MS, _deduplicate
import tools.be_off_cb_fast_drop_systemic_replay as systemic

OUT=ROOT/'data/studies/pl_lon_elastic/20261003_v2'
CACHE=ROOT/'data/studies/be_off_cb_deterioration/klines'
PRIOR=ROOT/'data/studies/be_off_cb_defensive_closure/20261002'
MONTHS=('2026-06','2026-07','2026-08','2026-09','2026-10','ALL')
VARIANTS=('A_NET_FLOOR','C_025_ATR','C_050_ATR')


def elastic_floor(p,variant):
    if variant=='A_NET_FLOOR':
        floor=p._active_profit_lock_economic_floor()
        if floor is None: raise ValueError('Existing economic floor unavailable: do not invent a protection')
        return floor
    if variant not in VARIANTS: raise ValueError(variant)
    return p.profit_lock_stop-(.25 if variant=='C_025_ATR' else .50)*p.entry_atr


class Study:
    def __init__(self,review,variant=None):
        self.review=review; self.variant=variant; self.events=[]
        self.at=0; self.boundary=0; self.remaining=[]

    def factory(self,*args,**kwargs):
        p=ElasticPosition(*args,**kwargs)
        p.study=self; p.elastic_started=None; p.elastic_floor=None
        return p

    def processor(self,positions,trades,candle,path,fees,*unused):
        self.at=candle.open_time_ms; self.boundary=candle.boundary_ms
        points=_deduplicate((candle.open,candle.high,candle.low,candle.close) if path=='HIGH_FIRST'
                            else (candle.open,candle.low,candle.high,candle.close))
        for rp in list(positions):
            p=rp.position
            if p.status!='OPEN': continue
            if p.elastic_started is not None and self.review.context(self.at)['ema_context']!='LON':
                rp.client.current_price=candle.open
                p._close_at_market(candle.open,'PL_ELASTIC_CONTEXT_LOST',iso(self.at),candle.open)
                systemic._append_trade(p,rp.opened_ms,self.at,fees,trades)
                continue
            previous=None
            for i,point in enumerate(points):
                self.remaining=points[i:]
                stop=p.effective_stop
                crossed=previous is not None and previous>stop and point<=stop
                tick=stop if crossed else point
                rp.client.current_price=tick
                p.on_tick(tick,iso(candle.boundary_ms))
                # After a suppressed PL touch, execute the rest of the same
                # descending segment. A newly elastic floor is not skipped.
                if crossed and p.status=='OPEN' and p.elastic_started is not None:
                    floor=p.effective_stop
                    tick=floor if stop>floor and point<=floor else point
                    rp.client.current_price=tick
                    p.on_tick(tick,iso(candle.boundary_ms))
                previous=point
                if p.status=='CLOSED':
                    systemic._append_trade(p,rp.opened_ms,candle.boundary_ms,fees,trades)
                    break


class ElasticPosition(BotFullExitPosition):
    def _refresh_effective_stop(self):
        if getattr(self,'elastic_started',None) is None:
            return super()._refresh_effective_stop()
        choices=[('hard_stop',self.hard_stop_price),('review',self.review_stop),
                 ('breakeven',self.breakeven_stop),('trailing',self.trailing_stop),
                 ('profit_lock',self.elastic_floor)]
        self.stop_type,self.effective_stop=max(((k,v) for k,v in choices if v is not None),key=lambda kv:kv[1])
        self.stop_price=self.effective_stop

    def _close_at_market(self,price,reason,ts,trigger_reference):
        study=self.study
        if reason=='PROFIT_LOCK' and self.elastic_started is None:
            ctx=study.review.context_fields(study.at)
            event={'source_candle':self.source_candle_open_time,'opened_ms':ms(self.open_ts),
                   'touch_ms':study.boundary,'causal_at_ms':study.at,'touch_price':price,
                   'entry_price':self.entry_price,'entry_atr':self.entry_atr,
                   'pl_step':self.profit_lock_step,'pl_stop':self.profit_lock_stop,
                   'existing_net_floor':self._active_profit_lock_economic_floor(),
                   'snapshot':ctx,'state':deepcopy(self.to_state()),'remaining':study.remaining[:],
                   'variant':study.variant}
            study.events.append(event)
            if study.variant and ctx['ema_context']=='LON':
                self.elastic_started=study.boundary
                self.elastic_floor=elastic_floor(self,study.variant)
                self._refresh_effective_stop()
                event['elastic_floor']=self.elastic_floor
                if price>self.effective_stop:
                    return None
                reason='PL_ELASTIC_FLOOR'
        elif reason=='PROFIT_LOCK' and self.elastic_started is not None:
            reason='PL_ELASTIC_FLOOR'
        return super()._close_at_market(price,reason,ts,trigger_reference)


def replay(config,candles,signals,review,start,end,path,variant):
    study=Study(review,variant)
    with patch.object(systemic,'BotFullExitPosition',study.factory),patch.object(systemic,'process_candle_systemic',study.processor):
        run=systemic.run_systemic(name=variant or 'BE_OFF_CB',config=config,signals=signals,
            candles=candles['1m'],contexts=[],start_ms=start,end_ms=end,path=path,
            spread_bps=review.spread,fast_enabled=False)
    return serialize(run,signals,review.notional),study.events


def benchmark(event,review):
    at=event['touch_ms']; touch=event['touch_price']; end=review.end
    lost=next((c.boundary_ms for c in review.candles['5m']
               if c.boundary_ms>=at and review.context(c.boundary_ms)['ema_context']!='LON'),None)
    windows={}
    for label,target in [(str(n),at+n*MINUTE_MS) for n in (5,15,30,60)]+[('until_lost',lost)]:
        stop=min(target or end,end)
        # Exclude pre-touch high/low; include only the known modeled remainder.
        points=[(at,p) for p in event['remaining']]
        expected=range(at+MINUTE_MS,stop+1,MINUTE_MS)
        missing=any(b not in review.index for b in expected)
        for b in expected:
            c=review.index.get(b)
            if c: points.extend(((b,c.high),(b,c.low)))
        vals=[p for _,p in points] or [touch]
        gain=max(0,max(vals)-touch)
        windows[label]={'complete':not missing and target is not None and target<=end,
            'favorable_pct':gain/touch*100,'adverse_pct':min(0,min(vals)/touch-1)*100,
            'ceiling_usd':review.notional/event['entry_price']*gain,
            'first_recovery_min':next(((b-at)/MINUTE_MS for b,p in points if p>touch),None),
            'first_deterioration_min':next(((b-at)/MINUTE_MS for b,p in points if p<touch),None)}
    return {'lon_lost_ms':lost,'lon_duration_censored':lost is None,'windows':windows}


def isolated(event,variant,review,path):
    study=Study(review,variant); client=ReplayExecutionClient(review.spread/2)
    p=ElasticPosition.from_state(deepcopy(event['state']),review.new_position(event['opened_ms']).position.config,client,NullLogger())
    p.study=study; p.elastic_started=None; p.elastic_floor=None
    study.at=event['causal_at_ms'];study.boundary=event['touch_ms'];study.remaining=event['remaining'][:]
    client.current_price=event['touch_price']
    p._close_at_market(event['touch_price'],'PROFIT_LOCK',iso(event['touch_ms']),event['pl_stop'])
    rp=OpenPosition(p,client,event['opened_ms'],review.notional)
    if p.status=='OPEN':
        for point in event['remaining']:
            tick=p.effective_stop if point<=p.effective_stop else point
            client.current_price=tick; p.on_tick(tick,iso(event['touch_ms']))
            if p.status=='CLOSED':break
    closed=event['touch_ms'] if p.status=='CLOSED' else None
    trades=[]
    lo=bisect.bisect_left(review.opens,event['touch_ms'])
    for c in review.minute[lo:]:
        if p.status=='CLOSED' or c.boundary_ms>review.end:break
        study.processor([rp],trades,c,path,review.fees)
    if trades: closed=trades[-1].closed_ms
    net=review.notional*(p.pnl_pct(p.exit_price)-review.fees)/100 if closed is not None else None
    return {'variant':variant,'closed_ms':closed,'exit_price':p.exit_price,'exit_reason':p.exit_reason or 'OPEN',
            'net_usd':net,'additional_min':((closed or review.end)-event['touch_ms'])/MINUTE_MS,
            'duration_censored':closed is None,'floor':p.elastic_floor}


def quantile(values,q):
    if not values:return None
    vals=sorted(values);i=(len(vals)-1)*q;lo=int(i)
    return vals[lo]+(vals[min(lo+1,len(vals)-1)]-vals[lo])*(i-lo)


def isolated_metrics(events,variant,window):
    selected=[e for e in events if window=='ALL' or month(e['touch_ms'])==window]
    rows=[(e,e['isolated'][variant]) for e in selected]
    closed=[(e,r) for e,r in rows if r['net_usd'] is not None]
    vals=[r['net_usd'] for _,r in closed];gains=sum(v for v in vals if v>0);losses=-sum(v for v in vals if v<0)
    equity=peak=dd=0
    for _,r in sorted(closed,key=lambda er:er[1]['closed_ms']):
        equity+=r['net_usd'];peak=max(peak,equity);dd=max(dd,peak-equity)
    deltas=[r['net_usd']-e['control_net'] for e,r in closed]
    durations=[r['additional_min'] for _,r in rows]
    caps={k:sum(e['benchmark']['windows'][k]['ceiling_usd'] for e,r in closed
                if e['benchmark']['windows'][k]['complete']) for k in ('5','15','30','60','until_lost')}
    fractions={k:(sum(r['net_usd']-e['control_net'] for e,r in closed if e['benchmark']['windows'][k]['complete'])/v if v else None) for k,v in caps.items()}
    return {'n':len(rows),'closed':len(closed),'open':len(rows)-len(closed),'net':sum(vals),
            'net_event':sum(vals)/len(vals) if vals else None,'pf':gains/losses if losses else math.inf if gains else None,
            'dd':dd,'delta':sum(deltas),'captured':sum(max(0,d) for d in deltas),
            'given_back':-sum(min(0,d) for d in deltas),'better':sum(d>1e-9 for d in deltas),
            'worse':sum(d<-1e-9 for d in deltas),'trail':sum(r['exit_reason']=='TRAILING' for _,r in closed),
            'hs_or_loss':sum(r['exit_reason']=='HARD_STOP' or r['net_usd']<0 for _,r in closed),
            'winners_to_losers':sum(e['control_net']>0 and r['net_usd']<0 for e,r in closed),
            'slot_minutes':sum(durations),'duration':{str(q):quantile(durations,q) for q in (.5,.75,.9,.95,1)},
            'censored':sum(r['duration_censored'] for _,r in rows),'ceiling_fraction':fractions}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    config=deepcopy(effective_config(_load_config(ROOT/'config/config.yaml')))
    config['risk']['breakeven']={'mode':'off'}
    candles={i:load_candle_cache(CACHE/f'SOLUSDT_{i}.jsonl') for i in ('1m','5m','15m')}
    start=ms('2026-06-01T00:00:00-03:00');end=max(c.boundary_ms for c in candles['1m'])
    signals=[SignalEvent(s['boundary_ms'],EntrySignal(**s['signal'])) for s in json.loads((PRIOR/'signals.json').read_text()) if start<=s['boundary_ms']<=end]
    old=json.loads((PRIOR/'manifest.json').read_text())
    if old['end_ms']<end:raise ValueError('Cached signal coverage shorter than candles; regenerate signals explicitly')
    review=Review(config,candles,signals,end)
    manifest={'start_brt':brt(start),'end_brt':brt(end),'config':config,
              'source_hashes':{p:digest(ROOT/p) for p in ('tools/pl_lon_elastic_study.py','src/position/bot_full_engine.py','src/monitor/market_context.py','tools/be_off_cb_fast_drop_systemic_replay.py')},
              'cache_hashes':{i:digest(CACHE/f'SOLUSDT_{i}.jsonl') for i in candles},
              'signals_sha256':digest(PRIOR/'signals.json'),'isolated_month_basis':'original PL touch BRT',
              'systemic_month_basis':'actual exit BRT','data_scope':'latest local available candles; no market data fetch',
              'causality':'5m closed at minute open; OHLC order modeled HIGH/LOW; tick timestamps unavailable',
              'B':'degenerate: current tick already <= unchanged PL floor; discarded without replay'}
    frozen=OUT/'manifest.json'
    if frozen.exists() and json.loads(frozen.read_text())!=manifest:raise ValueError('Inputs changed; do not mix checkpoints')
    frozen.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    summaries={};runs={};selections={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        print('Baseline',path,flush=True)
        control,events=replay(config,candles,signals,review,start,end,path,None)
        expected=json.loads((PRIOR/f'{path}_BE_OFF_CB.json').read_text())
        (OUT/f'{path}_baseline_parity.json').write_text(json.dumps({'current':control,'prior':expected},indent=2),encoding='utf-8')
        if control['trades']!=expected['trades']:raise AssertionError('Baseline parity failed; do not publish variant metrics')
        print('Baseline parity OK;',len(events),'PL events',flush=True)
        by_source={t['source_candle']:t for t in control['trades']}
        lon=[e for e in events if e['snapshot']['ema_context']=='LON']
        for j,e in enumerate(lon):
            e['control_net']=by_source[e['source_candle']]['net_usd']; e['benchmark']=benchmark(e,review)
            e['isolated']={'ORIGINAL':{'closed_ms':e['touch_ms'],'exit_price':by_source[e['source_candle']]['exit_price'],
                'exit_reason':'PROFIT_LOCK','net_usd':e['control_net'],'additional_min':0,'duration_censored':False}}
            for variant in VARIANTS:e['isolated'][variant]=isolated(e,variant,review,path)
            if j%50==0:print('Isolated',path,j,'/',len(lon),flush=True)
        (OUT/f'{path}_events.json').write_text(json.dumps(events,indent=2),encoding='utf-8')
        summaries[path]={w:{v:isolated_metrics(lon,v,w) for v in ('ORIGINAL',*VARIANTS)} for w in MONTHS}
        # Fixed survival criterion, not parameter optimization: both paths later
        # must show positive aggregate, >=3 positive full months, no net losers.
        runs[path]={'BE_OFF_CB':control}
    survivors=[]
    for v in VARIANTS:
        if all(summaries[p]['ALL'][v]['delta']>0 and summaries[p]['ALL'][v]['winners_to_losers']==0 and
               sum(summaries[p][m][v]['delta']>0 for m in MONTHS[:4])>=3 for p in summaries):survivors.append(v)
    # At most two, prioritize simpler economic floor, then smaller fixed buffer;
    # not the largest aggregate outcome. Selection and rejected results retained.
    survivors=survivors[:2];print('Systemic survivors',survivors,flush=True)
    for path in runs:
        for v in survivors:
            print('Systemic',path,v,flush=True)
            runs[path][v],_events=replay(config,candles,signals,review,start,end,path,v)
        (OUT/f'{path}_systemic.json').write_text(json.dumps(runs[path],indent=2),encoding='utf-8')
    result={'isolated':summaries,'systemic_survivors':survivors,
            'systemic':{p:{w:{v:{**metrics(r,w),'comparison':compare(rs['BE_OFF_CB'],r,w)} for v,r in rs.items()} for w in MONTHS} for p,rs in runs.items()}}
    (OUT/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    lines=['# PL_LON_ELASTIC — variantes fixas', '',f"BRT: {brt(start)} → {brt(end)}; último dado local disponível.",
        'B descartada: no próprio toque, o piso PL já foi atingido. Sem regra nova, saída imediata.',
        'Diagnóstico isolado mantém admissões do controle; sistêmico recalcula slots, spacing, equity, CB/cooldown.',
        'A mantém o economic_floor existente (fees + net_margin_pct); C congela o PL no toque menos 0,25/0,50 entry ATR. PL não rearma durante elasticidade; TRAIL/HS normais continuam ativos.',
        'Causalidade: snapshot 5m fechado disponível no início do minuto. Perda LON liquida no primeiro open 1m disponível. OHLC não resolve ticks intraminuto. Benchmark futuro NÃO executável, fora do replay.',
        'Teto medido em variação de preço, quantidade original; fração = delta líquido / teto bruto, pode ser negativa. Janelas censuradas excluídas da fração. Durações censuradas são limites inferiores.',
        'Meses do isolado pelo toque PL original; sistêmico pelo fechamento real. Equity realizada inicia zero em cada tabela mensal; sem MTM. OPEN excluído da economia, explicitamente contado.', '']
    for p in summaries:
        lines += [f'## {p} — isolado', '']
        lines+=table(['mês','variante','N','closed','open','net','net/event','PF','DD','delta','capturado','devolvido','melhor','pior','TRAIL','HS/perda','winner→loser','slot min','p50','p75','p90','p95','max','censored'],
            [[w,v,*[fmt(r[k]) if isinstance(r[k],float) else r[k] for k in ('n','closed','open','net','net_event','pf','dd','delta','captured','given_back','better','worse','trail','hs_or_loss','winners_to_losers','slot_minutes')],*[fmt(r['duration'][str(q)]) for q in (.5,.75,.9,.95,1)],r['censored']] for w,vs in summaries[p].items() for v,r in vs.items()])
        lines+=['### Fração do teto favorável (por horizonte)', '']
        lines+=table(['mês','variante','5m','15m','30m','60m','até perder LON'],[[w,v,*[fmt(r['ceiling_fraction'][k]) for k in ('5','15','30','60','until_lost')]] for w,vs in summaries[p].items() for v,r in vs.items() if v!='ORIGINAL'])
        lines += ['### Sistêmico', '']
        lines+=table(['mês','arm','closed','net','PF','DD','HS','PL','TRAIL','CB','cooldown h','blocked CB','max sim','delta','common delta','control-only net','variant-only net'],
            [[w,v,*[fmt(r[k]) for k in ('closed','net','pf','dd','HARD_STOP','PROFIT_LOCK','TRAILING','crises','cooldown_h','blocked_cb','max_sim')],*[fmt(r['comparison'][k]) for k in ('delta','common_delta','control_only_net','variant_only_net')]] for w,vs in result['systemic'][p].items() for v,r in vs.items()])
    lines+=['',f'Variantes que passaram o diagnóstico e receberam replay sistêmico: {survivors or "nenhuma"}.',
            'Nenhum shadow, runtime, YAML, ledger ou estado operacional foi alterado. Nenhum restart/deploy/commit.',
            'Detalhes por evento, snapshots, pisos e censura nos JSON; classificação/conclusão prática devem considerar ambos os caminhos e custo de slot, não apenas agregado.']
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print('DONE',OUT,flush=True)

if __name__=='__main__':main()
