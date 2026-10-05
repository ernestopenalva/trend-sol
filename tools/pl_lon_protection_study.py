"""Descriptive fixed-horizon PL protection audit. Never writes live state."""
from __future__ import annotations
import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import patch
_ROOT=Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:sys.path.insert(0,str(_ROOT))
from tools.pl_lon_elastic_study import (ROOT, OUT as INPUT, CACHE, PRIOR, MONTHS,
    Study, quantile, serialize, SignalEvent, EntrySignal, Review, systemic)
from tools.be_off_cb_defensive_closure import digest, table, fmt, month, ms
from tools.be_off_cb_exit_context_study import brt
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import MINUTE_MS, NullLogger, _deduplicate

OUT=ROOT/'data/studies/pl_lon_protection/20261003'
PATHS=('HIGH_FIRST','LOW_FIRST')
CLASSES=('PROTECTION','EARLY','MIXED','NEUTRAL')


def distribution(values):
    return {'mean':statistics.fmean(values) if values else None,
            'median':statistics.median(values) if values else None,
            'p75':quantile(values,.75),'p90':quantile(values,.9)}


def diagnose(points,exit_price,peak,floor,hard_stop,atr,at,complete=True):
    """Strict economic floor breach / strict new high; ordering by OHLC index.

    Times label modeled minutes, not actual ticks. Equal-minute events retain
    path ordering even when their modeled elapsed time is zero.
    """
    if floor is None or atr<=0:
        raise ValueError('Existing economic floor and frozen entry ATR required')
    low=min([exit_price]+[p for _,p in points]);high=max([exit_price]+[p for _,p in points])
    def passage(predicate):
        return next(((i,t) for i,(t,p) in enumerate(points) if predicate(p)),None)
    f=passage(lambda p:p<floor);n=passage(lambda p:p>peak)
    r=passage(lambda p:p>=peak);hs=passage(lambda p:p<=hard_stop)
    category='MIXED' if f and n else 'PROTECTION' if f else 'EARLY' if n else 'NEUTRAL'
    order=('FLOOR_THEN_PEAK' if f[0]<n[0] else 'PEAK_THEN_FLOOR') if f and n else None
    elapsed=lambda hit:(hit[1]-at)/MINUTE_MS if hit else None
    # Extrema timings concern observed path points, not the synthetic exit anchor.
    time_extreme=lambda value:next(((t-at)/MINUTE_MS for t,p in points if p==value),0)
    return {'complete':complete,'class':category if complete else 'CENSORED',
        'observed_class':category,'mixed_order':order,
        'mixed_gap_min':abs(f[1]-n[1])/MINUTE_MS if f and n else None,
        'floor_breached':bool(f),'peak_recovered':bool(r),'new_high':bool(n),
        'floor_min':elapsed(f),'peak_recovery_min':elapsed(r),'new_high_min':elapsed(n),
        'hs_hit':bool(hs),'hs_min':elapsed(hs),
        'worst_price':low,'best_price':high,'final_price':points[-1][1] if points else None,
        'favorable_abs':high-exit_price,'adverse_abs':exit_price-low,
        'favorable_pct':(high/exit_price-1)*100,'adverse_pct':(1-low/exit_price)*100,
        'favorable_atr':(high-exit_price)/atr,'adverse_atr':(exit_price-low)/atr,
        'above_previous_peak_abs':max(0,high-peak),
        'above_previous_peak_pct':max(0,high/peak-1)*100,
        'above_previous_peak_atr':max(0,high-peak)/atr,
        'below_floor_abs':max(0,floor-low),'below_floor_pct':max(0,1-low/floor)*100,
        'below_floor_atr':max(0,floor-low)/atr,
        'max_favorable_min':time_extreme(high),'max_adverse_min':time_extreme(low)}


def event_row(e,path,trade,minutes,end):
    at=e['touch_ms'];rows={}
    for h in (15,30):
        stop=min(at+h*MINUTE_MS,end)
        # Only the descending segment remainder after the original PL touch.
        points=[(at,p) for p in e['remaining']]
        missing=[]
        for t in range(at+MINUTE_MS,stop+1,MINUTE_MS):
            c=minutes.get(t)
            if c is None:missing.append(t);continue
            seq=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST'
                             else (c.open,c.low,c.high,c.close))
            points.extend((t,p) for p in seq)
        rows[str(h)]=diagnose(points,trade['exit_price'],e['state']['highest_price'],
            e['existing_net_floor'],e['state']['hard_stop_price'],e['entry_atr'],at,
            at+h*MINUTE_MS<=end and not missing)
        lost=e['benchmark']['lon_lost_ms']
        rows[str(h)]['lon_lost']=lost is not None and at<=lost<=stop
        rows[str(h)]['lon_lost_min']=(lost-at)/MINUTE_MS if rows[str(h)]['lon_lost'] else None
        rows[str(h)]['missing_minutes']=missing
    return {'path':path,'source_candle':e['source_candle'],'month':month(at),
        'exit_brt':brt(at),'causal_at_brt':brt(e['causal_at_ms']),
        'exit_price':trade['exit_price'],'market_pl_touch':e['touch_price'],
        'entry':e['entry_price'],'entry_atr':e['entry_atr'],'step':e['pl_step'],
        'peak_before_exit':e['state']['highest_price'],'pl_floor':e['pl_stop'],
        'economic_floor':e['existing_net_floor'],'snapshot':e['snapshot'],'windows':rows}


def summarize(events,h):
    valid=[e for e in events if e['windows'][str(h)]['complete']]
    wins=[e['windows'][str(h)] for e in valid]
    protected=[w for w in wins if w['class'] in ('PROTECTION','MIXED')]
    return {'n':len(events),'valid':len(valid),'censored':len(events)-len(valid),
        'classes':{k:sum(w['class']==k for w in wins) for k in CLASSES},
        'mixed_order':dict(Counter(w['mixed_order'] for w in wins if w['mixed_order'])),
        'peak_recovered':sum(w['peak_recovered'] for w in wins),
        'new_high':sum(w['new_high'] for w in wins),'lon_lost':sum(w['lon_lost'] for w in wins),
        'hs_counterfactual':sum(w['hs_hit'] for w in protected),
        'floor_breach_exit_minute':sum(w['floor_breached'] and w['floor_min']==0 for w in wins),
        'floor_breach_without_hs':sum(not w['hs_hit'] for w in protected),
        'hs_min':distribution([w['hs_min'] for w in protected if w['hs_hit']]),
        'distributions':{k:distribution([w[k] for w in wins]) for k in
            ('favorable_pct','adverse_pct','favorable_atr','adverse_atr','below_floor_pct',
             'below_floor_atr','max_favorable_min','max_adverse_min')},
        'mixed_groups':{order:{'n':len(sel),
            'gap_min':distribution([w['mixed_gap_min'] for w in sel]),
            'favorable_pct':distribution([w['favorable_pct'] for w in sel]),
            'adverse_pct':distribution([w['adverse_pct'] for w in sel])}
            for order in ('FLOOR_THEN_PEAK','PEAK_THEN_FLOOR')
            for sel in [[w for w in wins if w['mixed_order']==order]]}}


class ArmingLogger(NullLogger):
    def __init__(self,study):self.study=study
    def trade(self,event):
        name=event.get('event','')
        if name.startswith('PROFIT_LOCK_ATR_'):
            self.study.armings.append({'step':'PL'+name.rsplit('_',1)[-1],
                'at_ms':self.study.boundary,'source_candle':event.get('source_candle_open_time'),
                'position':event.get('pair_id'),'event':event})


class ArmingStudy(Study):
    def __init__(self,review):super().__init__(review);self.armings=[]
    def factory(self,*args,**kwargs):
        p=super().factory(*args,**kwargs);p.logger=ArmingLogger(self);return p


def count_armings(config,candles,signals,review,start,end,path,expected):
    study=ArmingStudy(review)
    with patch.object(systemic,'BotFullExitPosition',study.factory),patch.object(
            systemic,'process_candle_systemic',study.processor):
        run=systemic.run_systemic(name='BE_OFF_CB',config=config,signals=signals,
            candles=candles['1m'],contexts=[],start_ms=start,end_ms=end,path=path,
            spread_bps=review.spread,fast_enabled=False)
    serialized=serialize(run,signals,review.notional)
    if serialized['trades']!=expected['trades']:
        raise AssertionError('Baseline parity failed: no arming metrics published')
    return study.armings


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((INPUT/'manifest.json').read_text());config=manifest['config']
    candles={i:load_candle_cache(CACHE/f'SOLUSDT_{i}.jsonl') for i in ('1m','5m','15m')}
    for i in candles:
        if digest(CACHE/f'SOLUSDT_{i}.jsonl')!=manifest['cache_hashes'][i]:
            raise ValueError('Frozen candle hash mismatch; update base explicitly')
    start=ms(manifest['start_brt']);end=ms(manifest['end_brt'])
    if candles['1m'][-1].boundary_ms!=end:
        raise ValueError('Prior baseline does not cover latest local candle')
    signals=[SignalEvent(int(r['boundary_ms']),EntrySignal(**r['signal']))
             for r in json.loads((PRIOR/'signals.json').read_text())]
    if digest(PRIOR/'signals.json')!=manifest['signals_sha256']:
        raise ValueError('Frozen signal hash mismatch')
    review=Review(config,candles,signals,end)
    events=[];summary={};steps={};groups={};armings={}
    for path in PATHS:
        original=json.loads((INPUT/f'{path}_events.json').read_text())
        control=json.loads((INPUT/f'{path}_systemic.json').read_text())['BE_OFF_CB']
        trades={t['source_candle']:t for t in control['trades']}
        print('Arming audit baseline',path,flush=True)
        armings[path]=count_armings(config,candles,signals,review,start,end,path,control)
        for e in original:
            if e['snapshot']['ema_context']=='LON':
                causal=review.context_fields(e['causal_at_ms'])
                if causal!=e['snapshot']:raise AssertionError('Causal context parity failed')
                events.append(event_row(e,path,trades[e['source_candle']],review.index,end))
        subset=[e for e in events if e['path']==path]
        summary[path]={m:{str(h):summarize([e for e in subset if m=='ALL' or e['month']==m],h)
            for h in (15,30)} for m in MONTHS}
        steps[path]={}
        for step in ['PL'+str(i+1) for i in range(len(config['risk']['profit_lock']['steps']))]:
            closed=[e for e in original if e['pl_step']==step]
            steps[path][step]={'armed':sum(a['step']==step for a in armings[path]),
                'armed_by_month':{m:sum(a['step']==step and month(a['at_ms'])==m for a in armings[path]) for m in MONTHS if m!='ALL'},
                'all_pl_exits':len(closed),'all_pl_exit_pct':100*len(closed)/len(original),
                'lon_n':sum(e['snapshot']['ema_context']=='LON' for e in closed),
                'months':{m:{str(h):summarize([e for e in subset if e['step']==step and
                    (m=='ALL' or e['month']==m)],h) for h in (15,30)} for m in MONTHS}}
        groups[path]={}
        for field in ('macd_context','histogram_state'):
            groups[path][field]={v:{str(h):summarize([e for e in subset if e['snapshot'][field]==v],h)
                for h in (15,30)} for v in sorted({e['snapshot'][field] for e in subset})}
        print(path,'PL',len(original),'LON',len(subset),'armings',len(armings[path]),flush=True)
    payload={'monthly':summary,'steps':steps,'momentum':groups}
    (OUT/'summary.json').write_text(json.dumps(payload,indent=2),encoding='utf-8')
    (OUT/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    (OUT/'armings.json').write_text(json.dumps(armings,indent=2),encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'start_brt':manifest['start_brt'],
        'end_brt':manifest['end_brt'],'config_profit_lock':config['risk']['profit_lock'],
        'fees':config['fees'],'spread_bps':review.spread,'source_hash':digest(Path(__file__)),
        'input_manifest_hash':digest(INPUT/'manifest.json'),'cache_hashes':manifest['cache_hashes'],
        'event_hashes':{p:digest(INPUT/f'{p}_events.json') for p in PATHS},
        'baseline_hashes':{p:digest(INPUT/f'{p}_systemic.json') for p in PATHS},
        'core_hashes':{p:digest(ROOT/p) for p in ('src/position/bot_full_engine.py',
            'src/monitor/market_context.py','tools/pl_lon_elastic_study.py',
            'tools/be_off_cb_fast_drop_systemic_replay.py')},
        'arming_baseline_parity':True,
        'scope':'Latest local frozen baseline; descriptive, no alternative strategy.',
        'price_convention':'Excursions from actual baseline exit fill against market OHLC; floors/HS are market trigger levels. Not realizable PnL.',
        'classification':'Strict < economic floor and strict > prior peak; recovery >= peak. Full window required.',
        'time_convention':'OHLC order with minute labels; same-minute mixed order determined by point index; zero-minute gaps not actual tick times.',
        'causality':'Contexts rebuilt with same official functions at minute open; excludes open 5m candle.'},indent=2),encoding='utf-8')
    lines=['# PL em LON: proteção útil ou saída precoce?', '',
        f"Base local: {manifest['start_brt']} → {manifest['end_brt']}",
        'Diagnóstico retrospectivo; excursões não são PnL executável. Contexto causal revalidado; sem exit-context histórico persistido.',
        'A=PROTECTION; B=EARLY; C=MIXED; D=NEUTRAL. Piso estritamente rompido; novo pico estritamente superado.',
        'OHLC HIGH/LOW modelado, não ticks: ordem no mesmo minuto usa sequência; tempos iguais não implicam simultaneidade.', '']
    def row(label,h,r):
        return [label,h,r['n'],r['valid'],r['censored'],*[r['classes'][k] for k in CLASSES],
            r['peak_recovered'],r['new_high'],r['hs_counterfactual'],r['floor_breach_without_hs'],
            r['mixed_order'].get('FLOOR_THEN_PEAK',0),r['mixed_order'].get('PEAK_THEN_FLOOR',0),
            fmt(r['distributions']['adverse_pct']['median']),fmt(r['distributions']['favorable_pct']['median'])]
    header=['grupo','min','N','válidos','censurados','A','B','C','D','recuperou pico','novo pico','HS','piso sem HS','piso→pico','pico→piso','MAE % p50','MFE % p50']
    for path in PATHS:
        lines += [f'## {path}', '']
        lines += table(header,[row(m,h,r) for m,hs in summary[path].items() for h,r in hs.items()])
        lines += ['### Degraus', '']
        lines += table(['degrau','armado','saídas PL totais','% saídas PL','N LON'],
            [[k,r['armed'],r['all_pl_exits'],fmt(r['all_pl_exit_pct']),r['lon_n']] for k,r in steps[path].items()])
        lines += table(header,[row(k,h,r) for k,s in steps[path].items() for h,r in s['months']['ALL'].items()])
        lines += ['### Momentum (grupos descritivos; sempre N)', '']
        lines += table(header,[row(f'{field}:{v}',h,r) for field,vs in groups[path].items() for v,hs in vs.items() for h,r in hs.items()])
        lines += ['### Distribuições mensais / agregado', '']
        lines += table(['mês','min','N válido','medida','média','mediana','p75','p90'],
            [[m,h,r['valid'],k,*[fmt(d[q]) for q in ('mean','median','p75','p90')]]
             for m,hs in summary[path].items() for h,r in hs.items() for k,d in r['distributions'].items()])
        lines += ['### Casos mistos por mês e degrau', '']
        lines += table(['grupo','min','ordem','N','gap p50 min','gap p90 min','MAE média %','MFE média %'],
            [[label,h,o,g['n'],fmt(g['gap_min']['median']),fmt(g['gap_min']['p90']),
              fmt(g['adverse_pct']['mean']),fmt(g['favorable_pct']['mean'])]
             for label,hs in list(summary[path].items())+[(k,s['months']['ALL']) for k,s in steps[path].items()]
             for h,r in hs.items() for o,g in r['mixed_groups'].items()])
    lines += ['', 'Dados por evento e tempos completos: events.jsonl. Contagens de armamento verificadas contra baseline: armings.json.',
        'Quebra mensal por degrau, HS/tempos e grupos completos: summary.json. Nada somado como PnL; sem regra nova/runtime/YAML/restart.']
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    event_lines=['# Eventos PL em LON — diagnóstico posterior', '',
        'A proteção; B saída precoce; C misto; D neutro. Tempos modelados OHLC, não ticks. Censura explícita.', '']
    for path in PATHS:
        event_lines += [f'## {path}', '']
        event_lines += table(['source','PL BRT','degrau','entry','ATR entrada','exit fill','pico anterior',
            'PL piso','piso econômico','MACD','histograma','min','classe','ordem misto','gap min',
            'MFE %','MAE %','MFE ATR','MAE ATR','final','pico rec min','novo pico min','HS min',
            'pior preço','abaixo piso %','máx favorável min','máx adverso min','perda LON min'],
            [[e['source_candle'],e['exit_brt'],e['step'],fmt(e['entry']),fmt(e['entry_atr']),
              fmt(e['exit_price']),fmt(e['peak_before_exit']),fmt(e['pl_floor']),fmt(e['economic_floor']),
              e['snapshot']['macd_context'],e['snapshot']['histogram_state'],h,w['class'],w['mixed_order'],
              *[fmt(w[k]) for k in ('mixed_gap_min','favorable_pct','adverse_pct','favorable_atr','adverse_atr',
                'final_price','peak_recovery_min','new_high_min','hs_min','worst_price','below_floor_pct',
                'max_favorable_min','max_adverse_min','lon_lost_min')]]
             for e in events if e['path']==path for h,w in e['windows'].items()])
    (OUT/'events.md').write_text('\n'.join(event_lines),encoding='utf-8')
    print('DONE',OUT,flush=True)


if __name__=='__main__':main()
