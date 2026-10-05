"""Frozen 26/18 audit: reconstruct original entry gates, never change a gate."""
import bisect
import json
import statistics
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from src.monitor.entry_engine import EntryEngine
from tools.pl_lon_natural_reentry_study import OUT as PRIOR, INPUT, CACHE, MONTHS
from tools.be_off_cb_defensive_review import Review
from tools.be_off_cb_defensive_closure import digest, fmt, table, ms
from tools.be_off_cb_exit_context_study import brt
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import MINUTE_MS, NullLogger, _kline_payload
OUT=ROOT/'data/studies/pl_lon_entry_chain_audit/20261003'
FOCUS_REASON='AVAILABLE_NO_APPROVED_SIGNAL_FILTER_REASON_UNAUDITABLE'
GATES=('trend','pullback','exhaustion','recovery')

def select_focus(rows):
    selected=[r for r in rows if r['horizon']=='60' and not r['ambiguous_anchor'] and
              r['missing_reason']==FOCUS_REASON and r['recovery']['recovered']]
    counts=Counter(r['path'] for r in selected)
    if counts!={'HIGH_FIRST':26,'LOW_FIRST':18}:
        raise ValueError(f'Universe mismatch: expected 26/18, got {dict(counts)}; STOP')
    return selected

def classify(minutes):
    if not minutes or any(not m.get('evaluated') for m in minutes):return 'E'
    raw=[m for m in minutes if m['all_gates']['recovery']['passed']]
    if not raw:return 'A'
    if any(m['all_pass'] for m in raw):return 'E' # parity/admission audit required
    return 'B'

class Capture(NullLogger):
    def __init__(self):self.events=[]
    def decision(self,event):self.events.append(deepcopy(event))

def independent_gates(engine):
    """Methods read candle/config data; restore logger and diagnostics afterward."""
    old_logger=engine.logger;old_diagnostic=deepcopy(engine.last_diagnostic)
    engine.logger=Capture();engine.last_diagnostic=engine._empty_diagnostic()
    try:
        for method in ('_gate_trend','_gate_pullback','_gate_exhaustion','_gate_reversal'):
            getattr(engine,method)()
        return deepcopy(engine.last_diagnostic['gates'])
    finally:
        engine.logger=old_logger;engine.last_diagnostic=old_diagnostic

def main():
    rows=[json.loads(line) for line in (PRIOR/'events.jsonl').read_text().splitlines()]
    focus=select_focus(rows) # Exact count before running any reconstruction.
    print('Universe confirmed: 26 HIGH / 18 LOW',flush=True)
    meta=json.loads((INPUT/'manifest.json').read_text());config=meta['config']
    start=ms(meta['start_brt']);end=ms(meta['end_brt'])
    candles={i:load_candle_cache(CACHE/f'SOLUSDT_{i}.jsonl') for i in ('1m','5m','15m')}
    for i in candles:
        if digest(CACHE/f'SOLUSDT_{i}.jsonl')!=meta['cache_hashes'][i]:raise ValueError('Frozen candle mismatch')
    # Comparison population only: same PL/LON setting, no approved entry and no
    # subsequent peak recovery in its episode. Not an enlarged focus universe.
    controls=[r for r in rows if r['horizon']=='60' and not r['ambiguous_anchor'] and
              not r['incomplete'] and not r['recovery']['recovered'] and not r['reentries']]
    targets=set(b for r in focus+controls for b in range(r['pl_at_ms']+MINUTE_MS,r['window_end_ms'],MINUTE_MS))
    log=Capture();engine=EntryEngine(config['symbol'],config,log)
    indices={'5m':0,'15m':0};trace={};signals=[]
    cached_manifest=OUT/'manifest.json'
    reused=False
    if cached_manifest.exists():
        cached=json.loads(cached_manifest.read_text())
        if (cached.get('approved_signal_parity') and cached.get('frozen_config')==config and
            cached.get('cache_hashes')==meta['cache_hashes'] and
            cached.get('natural_reentry_input_sha256')==digest(PRIOR/'events.jsonl') and
            cached['source_hashes']['src/monitor/entry_engine.py']==digest(ROOT/'src/monitor/entry_engine.py')):
            trace={r['at_ms']:r for line in (OUT/'minutes.jsonl').read_text().splitlines() for r in [json.loads(line)]}
            reused=set(trace)==targets
    for c in ([] if reused else candles['1m']):
        b=c.boundary_ms
        if b>end:break
        for interval in ('15m','5m'):
            while indices[interval]<len(candles[interval]) and candles[interval][indices[interval]].boundary_ms<=b:
                engine.on_kline(f"solusdt@kline_{interval}",_kline_payload(candles[interval][indices[interval]]))
                indices[interval]+=1
        log.events=[]
        result=engine.on_kline('solusdt@kline_1m',_kline_payload(c))
        if result is not None and b>=start:signals.append({'boundary_ms':b,'source':result.source_candle_open_time})
        if b in targets:
            normal=deepcopy(engine.last_diagnostic)
            gates=independent_gates(engine)
            trace[b]={'at_ms':b,'at_brt':brt(b),'candle_closed_at_ms':c.close_time_ms,
                'evaluated':engine.last_evaluated_entry_open_time==c.open_time_ms,
                'approved':result is not None,'normal_diagnostic':normal,'normal_events':deepcopy(log.events),
                'all_gates':gates,'all_pass':all(gates[k]['passed'] is True for k in GATES),
                'raw_candidate_reversal':gates['recovery']['passed'] is True}
    expected=[{'boundary_ms':s['boundary_ms'],'source':s['signal']['source_candle_open_time']}
              for s in json.loads((ROOT/'data/studies/be_off_cb_defensive_closure/20261002/signals.json').read_text()) if start<=s['boundary_ms']<=end]
    if reused:signals=expected # Previously verified parity, same frozen engine/config/candles.
    if signals!=expected:raise ValueError('Approved-signal parity failed: do not publish classifications')
    print('Full approved-signal parity OK',len(signals),flush=True)
    OUT.mkdir(parents=True,exist_ok=True)
    review=Review(config,candles,[],end)
    original={p:{e['source_candle']:e for e in json.loads((INPUT/f'{p}_events.json').read_text())}
              for p in ('HIGH_FIRST','LOW_FIRST')}
    audited=[];comparison=[]
    for role,population in (('FOCUS',focus),('NON_RECOVERY_COMPARATOR',controls)):
        for r in population:
            times=list(range(r['pl_at_ms']+MINUTE_MS,r['window_end_ms'],MINUTE_MS))
            minutes=[trace.get(b,{'at_ms':b,'evaluated':False}) for b in times]
            category=classify(minutes)
            raw=[m for m in minutes if m.get('raw_candidate_reversal')]
            rejects=[]
            for m in raw:
                first=next((k for k in GATES if m['all_gates'][k]['passed'] is False),None)
                if first is not None:
                    rejects.append({'at_ms':m['at_ms'],'gate':first,'reason':m['all_gates'][first]['reason'],
                        'gate_values':m['all_gates'][first],
                        'normal_evaluated':m['normal_diagnostic']['gates'][first]['passed'] is not None,
                        'all_gates':m['all_gates'],
                        'context':review.context_fields(m['at_ms'])})
            counter=Counter((x['gate'],x['reason']) for x in rejects)
            e=original[r['path']][r['source_candle']]
            at=r['pl_at_ms'];stop=r['window_end_ms']
            future=[c for c in candles['1m'] if at<=c.open_time_ms and c.boundary_ms<=stop]
            favorable=max((c.high for c in future),default=e['touch_price'])
            adverse=min((c.low for c in future),default=e['touch_price'])
            last_reject=rejects[0]['at_ms'] if rejects else None
            base_price=review.price_at(last_reject) if last_reject else None
            after_reject=[c for c in future if last_reject is not None and c.open_time_ms>=last_reject]
            recovery_at=r['recovery_at_ms']
            rc=review.context_fields(max(at,recovery_at-MINUTE_MS)) if recovery_at is not None else None
            one=review.index.get(recovery_at) if recovery_at is not None else None
            def recent(n):
                prev=review.index.get(recovery_at-n*MINUTE_MS) if recovery_at is not None else None
                return (one.close/prev.close-1)*100 if one and prev else None
            result={'role':role,'path':r['path'],'source_candle':r['source_candle'],'month':r['month'],
                'pl_brt':brt(at),'end_brt':brt(stop),'category':category,'minute_count':len(minutes),
                'candidate_count':len(raw),'first_candidate_brt':brt(raw[0]['at_ms']) if raw else None,
                'all_pass_count':sum(m.get('all_pass',False) for m in minutes),'approved_count':sum(m.get('approved',False) for m in minutes),
                'first_block':rejects[0] if rejects else None,'reject_sequence':rejects,
                'filters':[{'gate':k,'reason':v,'attempts':n,
                            'first_at_brt':brt(next(x['at_ms'] for x in rejects if (x['gate'],x['reason'])==(k,v)))} for (k,v),n in counter.items()],
                'pl_step':e['pl_step'],'pl_context':e['snapshot'],
                'posterior':{'favorable_pct':max(0,(favorable/e['touch_price']-1)*100),
                    'adverse_pct':min(0,(adverse/e['touch_price']-1)*100),
                    'time_to_maximum_high_min':next(((c.boundary_ms-at)/MINUTE_MS for c in future if c.high==favorable),None),
                    'time_to_peak_min':r['recovery']['minutes'],'previous_peak_recovered':r['recovery']['recovered'],
                    'first_lower_close_min':next(((c.boundary_ms-at)/MINUTE_MS for c in future if c.close<e['touch_price']),None),
                    'lon_lost_brt':brt(r['lon_lost_ms']) if r['lon_lost_ms']<=end else None,
                    'after_first_rejection_favorable_pct':max(0,(max(c.high for c in after_reject)/base_price-1)*100) if after_reject and base_price else None,
                    'after_first_rejection_adverse_pct':min(0,(min(c.low for c in after_reject)/base_price-1)*100) if after_reject and base_price else None},
                'recovery_context':rc,'recovery_close_1m':one.close if one else None,
                'return_1m_pct':recent(1),'return_5m_pct':recent(5),
                'availability_evidence':r['missing_reason'],'pre_entry_admissions':r['admissions']}
            (audited if role=='FOCUS' else comparison).append(result)
    summary={p:{m:{'n':len(es),'categories':dict(Counter(e['category'] for e in es)),
                 'candidate_attempts':sum(e['candidate_count'] for e in es),
                 'filters':{f'{k}/{v}':{'episodes':sum(any(f['gate']==k and f['reason']==v for f in e['filters']) for e in es),
                             'attempts':sum(f['attempts'] for e in es for f in e['filters'] if f['gate']==k and f['reason']==v)}
                            for k,v in sorted({(f['gate'],f['reason']) for e in es for f in e['filters']})}}
              for m in MONTHS for es in [[e for e in audited if e['path']==p and (m=='ALL' or e['month']==m)]]}
             for p in ('HIGH_FIRST','LOW_FIRST')}
    # Comparators matched on pre-outcome features only: month, path, PL step,
    # MACD at PL, and same first impediment. No nearest-price/time fitting.
    for e in audited:
        f=e['first_block'];matches=[]
        if f:
            matches=[c for c in comparison if c['category']=='B' and c['path']==e['path'] and c['month']==e['month'] and
                c['pl_step']==e['pl_step'] and c['pl_context']['macd_context']==e['pl_context']['macd_context'] and
                c['first_block'] and (c['first_block']['gate'],c['first_block']['reason'])==(f['gate'],f['reason'])]
        e['similar_non_recovery_sources']=[c['source_candle'] for c in matches]
    for name,data in (('summary.json',summary),('episodes.json',audited),('comparison.json',comparison)):
        (OUT/name).write_text(json.dumps(data,indent=2),encoding='utf-8')
    (OUT/'minutes.jsonl').write_text(''.join(json.dumps(trace[b])+'\n' for b in sorted(trace)),encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'start_brt':meta['start_brt'],'end_brt':meta['end_brt'],
        'confirmed_universe':{'HIGH_FIRST':26,'LOW_FIRST':18},'approved_signal_parity':True,
        'entry_trace_reused_from_verified_frozen_inputs':reused,
        'source_hashes':{p:digest(ROOT/p) for p in ('src/monitor/entry_engine.py','tools/pl_lon_entry_chain_audit.py')},
        'natural_reentry_input_sha256':digest(PRIOR/'events.jsonl'),'frozen_config':config,'cache_hashes':meta['cache_hashes'],
        'raw_definition':'Existing gate 4 reversal predicate reconstructed independently; NOT an external signal generator. Normal gates remain short-circuiting.',
        'classification':'A=no reversal candidate; B=reversal candidate exists but existing upstream gate rejects; C/D require approved opportunity and admission evidence; E=missing reconstruction or unresolved parity.'},indent=2),encoding='utf-8')
    lines=['# PL_LON sem oportunidade aprovada — cadeia de entrada', '',
        'Universo confirmado: 26 HIGH / 18 LOW. Mesma base congelada; paridade integral de oportunidades aprovadas.',
        'Não existe sinal externo ao EntryEngine. A/B referem-se à condição existente do gate reversal, observada isoladamente sem mudar a decisão normal.',
        'Gates: trend → pullback → exhaustion → recovery/reversal → buy_signal. Predicados isolados são reconstrução, não tentativas realmente emitidas em runtime.', '']
    for p,months in summary.items():
        lines += [f'## {p}', '']
        lines += table(['mês','N','A/B/C/D/E','candidatos','filtros (episódios/tentativas)'],
            [[m,r['n'],json.dumps(r['categories']),r['candidate_attempts'],json.dumps(r['filters'])] for m,r in months.items()])
        lines += table(['PL BRT','source','cat','primeiro candidato','N candidatos','primeiro impedimento','all-pass','MFE %','MAE %','pico min','comparadores'],
            [[e['pl_brt'],e['source_candle'],e['category'],e['first_candidate_brt'],e['candidate_count'],
              f"{e['first_block']['gate']}/{e['first_block']['reason']} @ {brt(e['first_block']['at_ms'])}" if e['first_block'] else 'NONE',
              e['all_pass_count'],fmt(e['posterior']['favorable_pct']),fmt(e['posterior']['adverse_pct']),
              fmt(e['posterior']['time_to_peak_min']),len(e['similar_non_recovery_sources'])] for e in audited if e['path']==p])
    lines+=['','Comparadores não aprovados/sem recuperação não provam precisão do filtro: são comparação retrospectiva, estratificada por features conhecidas no PL. Ausência de match é explícita. Não é estudo de bypass nem teste fora da amostra.',
            'Mais detalhes: episodes.json (sequência/campos), minutes.jsonl (normal vs independente), comparison.json (trajetórias sem recuperar pico). Sem regra, runtime/YAML/estado/restart/git.']
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print('DONE',OUT,flush=True)

if __name__=='__main__':main()
