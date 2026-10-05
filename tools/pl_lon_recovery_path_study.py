"""Descriptive recovery-path audit of existing PL/LON events; no strategy search."""
from __future__ import annotations
import bisect
import json
import statistics
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.pl_lon_elastic_study import OUT as INPUT, CACHE, MONTHS, quantile
from tools.be_off_cb_defensive_closure import digest, table, fmt, month, ms
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import MINUTE_MS, _deduplicate
from tools.be_off_cb_exit_context_study import brt
OUT=ROOT/'data/studies/pl_lon_recovery_path/20261003'
HORIZONS=('5','15','30','60','until_lost')


def first_passage(points,target,touch,atr,at,strict=False):
    """Interpolate only the target crossing, never include the later endpoint.

    Timestamps are OHLC minute labels, not invented intraminute tick times.
    """
    worst=touch
    previous=touch
    for t,price in points:
        hit=price>target if strict else price>=target
        if hit:
            # Starting above a target is a gap: use the actual observed price.
            crossing=max(previous,target) if previous>=target else target
            worst=min(worst,crossing)
            return {'recovered':True,'minutes':(t-at)/MINUTE_MS,
                    'required_atr':max(0,touch-worst)/atr,
                    'required_pct':max(0,1-worst/touch)*100,'lowest_price':worst}
        worst=min(worst,price);previous=price
    return {'recovered':False,'minutes':None,'required_atr':None,'required_pct':None,
            'lowest_price':worst}


def diagnose(event,points,stop,complete):
    touch=event['touch_price'];atr=event['entry_atr'];at=event['touch_ms']
    peak=event['state']['highest_price'];floor=event['existing_net_floor']
    values=[p for _,p in points] or [touch]
    rebound=first_passage(points,touch,touch,atr,at,True)
    restored=first_passage(points,peak,touch,atr,at)
    def below(level):
        if touch<=level:return 0
        return next(((t-at)/MINUTE_MS for t,p in points if p<=level),None)
    floor_time=below(floor) if floor is not None else None
    hs_time=below(event['state']['hard_stop_price'])
    # Check prefix through the exact peak crossing, not a whole candle's low.
    floor_before_peak=(restored['recovered'] and floor is not None and
                       restored['lowest_price']<=floor)
    return {'complete':complete,'observed_min':(stop-at)/MINUTE_MS,'rebound':rebound,'peak_restored':restored,
            'economic_floor_touched_min':floor_time,'hs_touched_min':hs_time,
            'peak_restored_without_floor_breach':restored['recovered'] and floor is not None and not floor_before_peak,
            'floor_breached_before_peak_recovery':floor_before_peak,
            'mfe_atr':max(0,max(values)-touch)/atr,'mae_atr':max(0,touch-min(values))/atr,
            'mfe_pct':max(0,max(values)/touch-1)*100,'mae_pct':max(0,1-min(values)/touch)*100}


def event_row(e,path,minutes,end):
    at=e['touch_ms'];lost=e['benchmark']['lon_lost_ms']
    max_stop=min(max(at+60*MINUTE_MS,lost or end),end)
    future=[(at,p) for p in e['remaining']]
    missing=[]
    for t in range(at+MINUTE_MS,max_stop+1,MINUTE_MS):
        c=minutes.get(t)
        if c is None:missing.append(t);continue
        seq=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST' else (c.open,c.low,c.high,c.close))
        future.extend((t,p) for p in seq)
    windows={}
    for h in HORIZONS:
        target=lost if h=='until_lost' else at+int(h)*MINUTE_MS
        stop=min(target or end,end)
        complete=target is not None and target<=end and not any(t<=stop for t in missing)
        points=[(t,p) for t,p in future if t<=stop]
        windows[h]=diagnose(e,points,stop,complete)
    return {'path':path,'source_candle':e['source_candle'],'month':month(at),
            'touch_brt':brt(at),'touch_ms':at,'snapshot':e['snapshot'],'entry_atr':e['entry_atr'],
            'pl_step':e['pl_step'],'control_net':e['control_net'],
            'touch_price':e['touch_price'],'previous_peak':e['state']['highest_price'],
            'lon_lost_brt':brt(lost) if lost else None,'windows':windows,
            'features':{'macd':e['snapshot']['macd_context'],
                'histogram':e['snapshot']['histogram_state'],'pl_step':e['pl_step'],
                'age_min':(at-e['opened_ms'])/MINUTE_MS,
                'pullback_from_peak_atr':(e['state']['highest_price']-e['touch_price'])/e['entry_atr'],
                'economic_cushion_atr':(e['touch_price']-e['existing_net_floor'])/e['entry_atr'] if e['existing_net_floor'] is not None else None}}


def summarize(rows,h):
    valid=[r for r in rows if r['windows'][h]['complete']]
    wins=[r['windows'][h] for r in valid]
    rec=[w['peak_restored'] for w in wins if w['peak_restored']['recovered']]
    rebound=[w['rebound'] for w in wins if w['rebound']['recovered']]
    def pct(n):return 100*n/len(valid) if valid else None
    def dist(vals):return {str(q):quantile(vals,q) for q in (.5,.75,.9,.95,1)}
    return {'n':len(rows),'valid':len(valid),'censored':len(rows)-len(valid),
            'rebound_pct':pct(len(rebound)),'peak_restored_pct':pct(len(rec)),
            'protected_peak_recovery_pct':pct(sum(w['peak_restored_without_floor_breach'] for w in wins)),
            'floor_touch_pct':pct(sum(w['economic_floor_touched_min'] is not None for w in wins)),
            'hs_touch_pct':pct(sum(w['hs_touched_min'] is not None for w in wins)),
            'peak_recovery_required_atr':dist([r['required_atr'] for r in rec]),
            'peak_recovery_required_pct':dist([r['required_pct'] for r in rec]),
            'peak_recovery_min':dist([r['minutes'] for r in rec]),
            'rebound_required_atr':dist([r['required_atr'] for r in rebound]),
            'rebound_min':dist([r['minutes'] for r in rebound]),
            'mae_atr':dist([w['mae_atr'] for w in wins]),'mfe_atr':dist([w['mfe_atr'] for w in wins])}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    manifest=json.loads((INPUT/'manifest.json').read_text());end=ms(manifest['end_brt'])
    minutes={c.boundary_ms:c for c in load_candle_cache(CACHE/'SOLUSDT_1m.jsonl')}
    if digest(CACHE/'SOLUSDT_1m.jsonl')!=manifest['cache_hashes']['1m']:raise ValueError('Candle inputs changed')
    rows=[];summary={};groups={};features={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        events=[e for e in json.loads((INPUT/f'{path}_events.json').read_text()) if e['snapshot']['ema_context']=='LON']
        for e in events:rows.append(event_row(e,path,minutes,end))
        subset=[r for r in rows if r['path']==path]
        summary[path]={m:{h:summarize([r for r in subset if m=='ALL' or r['month']==m],h) for h in HORIZONS} for m in MONTHS}
        # Natural existing categories only. No optimized threshold, classifier,
        # strategy or joint-condition grid. Monthly evidence retained separately.
        groups[path]={}
        for key in ('macd','histogram','pl_step'):
            groups[path][key]={value:{m:summarize([r for r in subset if r['features'][key]==value and (m=='ALL' or r['month']==m)],'60') for m in MONTHS}
                               for value in sorted({r['features'][key] for r in subset})}
        features[path]={}
        valid=[r for r in subset if r['windows']['60']['complete']]
        for key in ('age_min','pullback_from_peak_atr','economic_cushion_atr'):
            features[path][key]={label:{'n':len(sel),'median':statistics.median(v) if v else None}
                for label,sel in [('peak_recovered',[r for r in valid if r['windows']['60']['peak_restored']['recovered']]),
                                  ('peak_not_recovered',[r for r in valid if not r['windows']['60']['peak_restored']['recovered']])]
                for v in [[r['features'][key] for r in sel if r['features'][key] is not None]]}
    payload={'windows':summary,'available_at_touch_categories':groups,'available_at_touch_numeric':features}
    (OUT/'summary.json').write_text(json.dumps(payload,indent=2),encoding='utf-8')
    (OUT/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'input_manifest_sha256':digest(INPUT/'manifest.json'),
        'event_hashes':{p:digest(INPUT/f'{p}_events.json') for p in summary},
        'candle_sha256':digest(CACHE/'SOLUSDT_1m.jsonl'),'tool_sha256':digest(Path(__file__)),
        'start_brt':manifest['start_brt'],'end_brt':manifest['end_brt'],
        'definitions':{'rebound':'first price strictly above PL touch; not a new economic profit',
            'peak_recovery':'first return to previous peak already known at PL touch',
            'slack':'worst excursion below touch before exact first target crossing',
            'protected_recovery':'previous peak recovered without touching existing economic floor',
            'time':'modeled minute labels; same-minute recovery is 0 minutes, not instantaneous ticks',
            'features':'only context/position state already available at causal trigger; no future labels as predictors'},
        'scope':'descriptive isolated paths; no alternative rule or portfolio replay'},indent=2),encoding='utf-8')
    lines=['# PL em LON — caminho até a recuperação', '',
        f"{manifest['start_brt']} → {manifest['end_brt']}; mesmos eventos/candles do estudo anterior, sem novo replay.",
        'Rebote = primeiro preço acima do toque PL; recuperação relevante = retorno ao pico anterior conhecido no toque.',
        'Folga necessária = pior excursão antes do PRIMEIRO cruzamento do alvo, não low de todo o candle. Protegida = sem tocar piso econômico existente.',
        'Análise retrospectiva descritiva, não regra executável. Ordem OHLC HIGH/LOW modelada; não há timestamps intraminuto reais. Censura explícita.', '']
    for p,months in summary.items():
        lines += [f'## {p}', '']
        lines+=table(['mês','janela','N válido','censored','rebote %','pico %','pico protegido %','floor %','HS %','folga ATR p50','p90','p95','max','tempo p50','p90','p95','max'],
            [[m,h,r['valid'],r['censored'],*[fmt(r[k]) for k in ('rebound_pct','peak_restored_pct','protected_peak_recovery_pct','floor_touch_pct','hs_touch_pct')],
              *[fmt(r['peak_recovery_required_atr'][str(q)]) for q in (.5,.9,.95,1)],
              *[fmt(r['peak_recovery_min'][str(q)]) for q in (.5,.9,.95,1)]] for m,hs in months.items() for h,r in hs.items()])
        lines+=['### Informações disponíveis no toque — grupos naturais (60m)', '']
        lines+=table(['campo','valor','mês','N','pico %','pico protegido %','floor %'],
            [[k,v,m,r['valid'],fmt(r['peak_restored_pct']),fmt(r['protected_peak_recovery_pct']),fmt(r['floor_touch_pct'])]
             for k,vs in groups[p].items() for v,ms_ in vs.items() for m,r in ms_.items()])
        lines+=['### Medianas disponíveis no toque — recuperou pico / não recuperou (60m)', '']
        lines+=table(['variável','recuperou N','mediana','não recuperou N','mediana'],
            [[k,d['peak_recovered']['n'],fmt(d['peak_recovered']['median']),d['peak_not_recovered']['n'],fmt(d['peak_not_recovered']['median'])] for k,d in features[p].items()])
    lines+=['','Não estima classificação fora da amostra nem taxa de acerto de filtro; categorias pequenas exigem cautela. Sem grid, otimização, shadow, mudanças de runtime/YAML/estados ou restart.']
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print('DONE',OUT,flush=True)

if __name__=='__main__':main()
