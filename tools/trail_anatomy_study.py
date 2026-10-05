"""Fixed baseline TRAIL anatomy; isolated no-TRAIL diagnosis, no live writes."""
from __future__ import annotations
import bisect
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.pl_lon_elastic_study import (OUT as INPUT, CACHE, PRIOR, MONTHS, Study,
    SignalEvent, EntrySignal, Review, systemic, serialize, iso)
from tools.pl_lon_protection_study import diagnose, distribution
from tools.be_off_cb_defensive_closure import digest, table, fmt, month, ms
from tools.be_off_cb_exit_context_study import brt
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import MINUTE_MS, NullLogger, ReplayExecutionClient, _deduplicate
from src.position.bot_full_engine import BotFullExitPosition

OUT=ROOT/'data/studies/trail_anatomy/20261003'
PATHS=('HIGH_FIRST','LOW_FIRST');HORIZONS=(5,15,30,60)
CLASSES=('PROTECTION','EARLY','MIXED','NEUTRAL')


class TrailPosition(BotFullExitPosition):
    def _close_at_market(self,price,reason,ts,trigger_reference):
        if reason=='TRAILING':
            s=self.study
            s.events.append({'source_candle':self.source_candle_open_time,
                'opened_ms':ms(self.open_ts),'touch_ms':s.boundary,'causal_at_ms':s.at,
                'touch_price':price,'entry':self.entry_price,'entry_atr':self.entry_atr,
                'economic_floor':self._active_profit_lock_economic_floor(),
                'snapshot':s.review.context_fields(s.at),'state':deepcopy(self.to_state()),
                'remaining':s.remaining[:]})
        return super()._close_at_market(price,reason,ts,trigger_reference)


class TrailStudy(Study):
    def factory(self,*args,**kwargs):
        p=TrailPosition(*args,**kwargs);p.study=self
        p.elastic_started=None;p.elastic_floor=None
        return p


class NoTrailPosition(BotFullExitPosition):
    # Only trailing is removed. PL, HS, review and all other methods unchanged.
    def _should_activate_trailing(self,*args):return False
    def _update_trailing_stop(self):return None


def restore_without_trail(event,config,client):
    p=NoTrailPosition.from_state(deepcopy(event['state']),config,client,NullLogger())
    p.trailing_active=False;p.trailing_stop=None
    # The engine intentionally ratchets the previous effective stop. That
    # previous stop came from TRAIL: remove it in this isolated counterfactual,
    # then rebuild exclusively from the unchanged remaining exit levels.
    p.effective_stop=None;p._refresh_effective_stop()
    return p


def follow_points(p,client,points,at):
    previous=None
    for point in points:
        stop=p.effective_stop
        crossed=previous is not None and previous>stop and point<=stop
        tick=stop if crossed else point
        client.current_price=tick;p.on_tick(tick,iso(at));previous=point
        if p.status=='CLOSED':return True
    return False


def no_trail(event,review,path,real_net):
    client=ReplayExecutionClient(review.spread/2)
    config=review.new_position(event['opened_ms']).position.config
    p=restore_without_trail(event,config,client)
    # Seed the crossing from the original trigger, not an already-lower endpoint.
    closed=event['touch_ms'] if follow_points(p,client,
        _deduplicate([event['touch_price']]+event['remaining']),event['touch_ms']) else None
    if closed is None:
        lo=bisect.bisect_left(review.opens,event['touch_ms'])
        for c in review.minute[lo:]:
            if c.boundary_ms>review.end:break
            points=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST'
                                else (c.open,c.low,c.high,c.close))
            if follow_points(p,client,points,c.boundary_ms):closed=c.boundary_ms;break
    net=review.notional*(p.pnl_pct(p.exit_price)-review.fees)/100 if closed is not None else None
    return {'closed_ms':closed,'exit_brt':brt(closed) if closed is not None else 'OPEN',
        'reason':p.exit_reason or 'OPEN','price':p.exit_price,'net':net,
        'delta':net-real_net if net is not None else None,
        'additional_min':((closed or review.end)-event['touch_ms'])/MINUTE_MS,
        'censored':closed is None}


def classify_window(points,exit_price,peak,floor,hs,atr,at,complete=True):
    w=diagnose(points,exit_price,peak,floor,hs,atr,at,complete)
    def first(test):return next(((i,t) for i,(t,p) in enumerate(points) if test(p)),None)
    f=first(lambda p:p<floor);r=first(lambda p:p>=peak)
    minor=first(lambda p:p<exit_price)
    category='MIXED' if f and r else 'PROTECTION' if f else 'EARLY' if r else 'NEUTRAL'
    w.update({'class':category if complete else 'CENSORED','observed_class':category,
        'mixed_order':('FLOOR_THEN_RECOVERY' if f[0]<r[0] else 'RECOVERY_THEN_FLOOR') if f and r else None,
        'mixed_gap_min':abs(f[1]-r[1])/MINUTE_MS if f and r else None,
        'below_exit_min':(minor[1]-at)/MINUTE_MS if minor else None,
        'floor_at_zero':bool(f and f[1]==at),'recovery_at_zero':bool(r and r[1]==at),
        'new_high_at_zero':w['new_high_min']==0,'below_exit_at_zero':bool(minor and minor[1]==at)})
    return w


def magnitude(peak_atr,pl):
    edges=[float(s['trigger_atr']) for s in pl['steps']]
    boundaries=sorted(set(edges+[10.]))
    for i,b in enumerate(boundaries):
        if peak_atr<b:return f'{boundaries[i-1] if i else 0:g}–{b:g} ATR'
    return f'>={boundaries[-1]:g} ATR'


def giveback(entry,exit_price,peak,atr,stop):
    advance=peak-entry;returned=peak-exit_price
    return {'peak_atr':advance/atr,'exit_atr':(exit_price-entry)/atr,
        'peak_gross_pct':advance/entry*100,'exit_gross_pct':(exit_price-entry)/entry*100,
        'giveback_abs':returned,'giveback_pct_points':returned/entry*100,
        'giveback_pct_of_peak_price':returned/peak*100,'giveback_atr':returned/atr,
        'fraction_peak_returned_pct':returned/advance*100 if advance>0 else None,
        'effective_gap_atr':(peak-stop)/atr}


def event_row(e,path,trade,review,pl):
    at=e['touch_ms'];peak=e['state']['highest_price'];atr=e['entry_atr']
    # A TRAIL can activate before any PL step is armed (small entry ATR).
    # The cached PL field is then None, although the official floor exists.
    floor=e.get('economic_floor')
    if floor is None:
        original=review.new_position(e['opened_ms']).position
        if abs(original.entry_price-e['entry'])>1e-9:raise AssertionError('Entry reconstruction mismatch')
        floor=original._active_profit_lock_economic_floor()
    if floor is None:raise ValueError('Economic floor unavailable; do not invent deterioration threshold')
    real=trade['exit_price'];hs=e['state']['hard_stop_price'];windows={}
    for h in HORIZONS:
        stop=min(at+h*MINUTE_MS,review.end);post=[];missing=[]
        for t in range(at+MINUTE_MS,stop+1,MINUTE_MS):
            c=review.index.get(t)
            if c is None:missing.append(t);continue
            seq=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST'
                else (c.open,c.low,c.high,c.close))
            post.extend((t,p) for p in seq)
        remainder=[(at,p) for p in e['remaining']]
        complete=at+h*MINUTE_MS<=review.end and not missing
        all_=classify_window(remainder+post,real,peak,floor,hs,atr,at,complete)
        post_=classify_window(post,real,peak,floor,hs,atr,at,complete)
        for w in (all_,post_):
            # Future context changes only at closed 5m boundaries.
            changed=next((c.boundary_ms for c in review.candles['5m'] if at<c.boundary_ms<=stop
                and review.context(c.boundary_ms)['ema_context']!=e['snapshot']['ema_context']),None)
            w['context_change_min']=(changed-at)/MINUTE_MS if changed is not None else None
            # Ex-post theoretical gross capture, not a realizable policy.
            ceiling=max(peak,w['best_price'])
            w['theoretical_ceiling_price']=ceiling
            w['exit_capture_pct']=(real-e['entry'])/(ceiling-e['entry'])*100
            w['exit_to_ceiling_pct_points']=(ceiling-real)/e['entry']*100
        windows[str(h)]={'with_remainder':all_,'post_minutes':post_,
            'classification_changed_without_remainder':all_['class']!=post_['class'],
            'mixed_depends_on_remainder':all_['class']=='MIXED' and
                (post_['class']!='MIXED' or all_['mixed_order']!=post_['mixed_order']),
            'missing_minutes':missing}
    armed=[x for x in e['state']['applied_steps'] if x.startswith('atr:')]
    g=giveback(e['entry'],real,peak,atr,e['state']['trailing_stop'])
    alternative=no_trail(e,review,path,trade['net_usd'])
    return {'path':path,'source_candle':e['source_candle'],'month':month(at),
        'exit_brt':brt(at),'causal_at_brt':brt(e['causal_at_ms']),'opened_brt':brt(e['opened_ms']),
        'entry':e['entry'],'entry_atr':atr,'exit_price':real,'market_touch':e['touch_price'],
        'peak_before_exit':peak,'trailing_stop':e['state']['trailing_stop'],
        'economic_floor':floor,'hs_price':hs,'age_min':(at-e['opened_ms'])/MINUTE_MS,
        'pl_armed':['PL'+x.split(':')[1] for x in armed],'pl3_armed':'atr:3' in armed,
        'snapshot':e['snapshot'],'net':trade['net_usd'],'giveback':g,
        'magnitude':magnitude(g['peak_atr'],pl),'no_trail':alternative,
        'same_candle':classify_window([(at,p) for p in e['remaining']],real,peak,floor,hs,atr,at),
        'windows':windows}


def summarize(events,h,mode):
    valid=[e for e in events if e['windows'][str(h)][mode]['complete']]
    ws=[e['windows'][str(h)][mode] for e in valid]
    resolved=[e for e in valid if not e['no_trail']['censored']]
    deltas=[e['no_trail']['delta'] for e in resolved]
    return {'n':len(events),'valid':len(valid),'censored':len(events)-len(valid),
        'classes':{c:sum(w['class']==c for w in ws) for c in CLASSES},
        'mixed_orders':dict(Counter(w['mixed_order'] for w in ws if w['mixed_order'])),
        'floor_zero':sum(w['floor_at_zero'] for w in ws),
        'below_exit_zero':sum(w['below_exit_at_zero'] for w in ws),
        'recovery_zero':sum(w['recovery_at_zero'] for w in ws),
        'new_high_zero':sum(w['new_high_at_zero'] for w in ws),
        'mixed_depends_on_remainder':sum(e['windows'][str(h)]['mixed_depends_on_remainder'] for e in valid),
        'classification_changed_without_remainder':sum(e['windows'][str(h)]['classification_changed_without_remainder'] for e in valid),
        'zero_by_class':{c:{'n':sum(w['class']==c for w in ws),
            'below_exit':sum(w['class']==c and w['below_exit_at_zero'] for w in ws),
            'floor':sum(w['class']==c and w['floor_at_zero'] for w in ws),
            'recovery':sum(w['class']==c and w['recovery_at_zero'] for w in ws),
            'new_high':sum(w['class']==c and w['new_high_at_zero'] for w in ws)} for c in CLASSES},
        'mixed_first_event_in_remainder':sum(w['class']=='MIXED' and
            (w['floor_at_zero'] or w['recovery_at_zero']) for w in ws),
        'recovered_peak':sum(w['peak_recovered'] for w in ws),'new_high':sum(w['new_high'] for w in ws),
        'hs_path':sum(w['hs_hit'] for w in ws),
        'original_net':sum(e['net'] for e in events),
        'no_trail':{'resolved':len(resolved),'censored':len(valid)-len(resolved),
            'delta_total':sum(deltas),'delta':distribution(deltas),
            'better':sum(d>1e-10 for d in deltas),'worse':sum(d<-1e-10 for d in deltas),
            'reasons':dict(Counter(e['no_trail']['reason'] for e in resolved)),
            'additional_min':distribution([e['no_trail']['additional_min'] for e in resolved])},
        'giveback':{k:{**distribution([e['giveback'][k] for e in events]),
            'max':max((e['giveback'][k] for e in events),default=None)} for k in
            ('peak_gross_pct','exit_gross_pct','giveback_abs','giveback_pct_points',
             'giveback_atr','fraction_peak_returned_pct','peak_atr','exit_atr','effective_gap_atr')},
        'path_metrics':{k:distribution([w[k] for w in ws]) for k in
            ('favorable_pct','adverse_pct','favorable_atr','adverse_atr','above_previous_peak_pct',
             'exit_capture_pct','exit_to_ceiling_pct_points')},
        'mixed_groups':{o:{'n':len(sel),'gap_min':distribution([w['mixed_gap_min'] for w in sel])}
            for o in ('FLOOR_THEN_RECOVERY','RECOVERY_THEN_FLOOR')
            for sel in [[w for w in ws if w['mixed_order']==o]]}}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    m=json.loads((INPUT/'manifest.json').read_text());config=m['config'];start=ms(m['start_brt']);end=ms(m['end_brt'])
    candles={i:load_candle_cache(CACHE/f'SOLUSDT_{i}.jsonl') for i in ('1m','5m','15m')}
    for i in candles:
        if digest(CACHE/f'SOLUSDT_{i}.jsonl')!=m['cache_hashes'][i]:raise ValueError('Frozen cache changed')
    if candles['1m'][-1].boundary_ms!=end:raise ValueError('Baseline not current to latest local candle')
    if digest(PRIOR/'signals.json')!=m['signals_sha256']:raise ValueError('Frozen signals changed')
    signals=[SignalEvent(int(r['boundary_ms']),EntrySignal(**r['signal'])) for r in json.loads((PRIOR/'signals.json').read_text())]
    review=Review(config,candles,signals,end);events=[];summary={};groups={}
    for path in PATHS:
        expected=json.loads((INPUT/f'{path}_systemic.json').read_text())['BE_OFF_CB']
        study=TrailStudy(review)
        print('Baseline',path,flush=True)
        with patch.object(systemic,'BotFullExitPosition',study.factory),patch.object(systemic,'process_candle_systemic',study.processor):
            run=systemic.run_systemic(name='BE_OFF_CB',config=config,signals=signals,
                candles=candles['1m'],contexts=[],start_ms=start,end_ms=end,path=path,
                spread_bps=review.spread,fast_enabled=False)
        control=serialize(run,signals,review.notional)
        if control['trades']!=expected['trades']:raise AssertionError('Baseline parity failure')
        trades={t['source_candle']:t for t in control['trades']}
        expected_trails=sum(t['exit_reason']=='TRAILING' for t in control['trades'])
        if expected_trails!=len(study.events):raise AssertionError('TRAIL capture count mismatch')
        (OUT/f'{path}_raw_events.json').write_text(json.dumps(study.events,indent=2),encoding='utf-8')
        for i,e in enumerate(study.events):
            if i%100==0:print('Isolated',path,i,'/',len(study.events),flush=True)
            if e['snapshot']['latest_closed_at_ms']>=e['causal_at_ms']:
                raise AssertionError('Open 5m context')
            events.append(event_row(e,path,trades[e['source_candle']],review,config['risk']['profit_lock']))
        subset=[e for e in events if e['path']==path]
        summary[path]={mo:{str(h):{mode:summarize([e for e in subset if mo=='ALL' or e['month']==mo],h,mode)
            for mode in ('with_remainder','post_minutes')} for h in HORIZONS} for mo in MONTHS}
        groups[path]={}
        selectors={'ema':lambda e:e['snapshot']['ema_context'],
            'macd':lambda e:e['snapshot']['macd_context'],
            'histogram':lambda e:e['snapshot']['histogram_state'],
            'magnitude':lambda e:e['magnitude'],'pl3':lambda e:'ARMED' if e['pl3_armed'] else 'NOT_ARMED'}
        for field,fn in selectors.items():
            groups[path][field]={v:{mo:{str(h):summarize([e for e in subset if fn(e)==v and
                (mo=='ALL' or e['month']==mo)],h,'post_minutes') for h in HORIZONS} for mo in MONTHS}
                for v in sorted({fn(e) for e in subset})}
    payload={'monthly':summary,'groups':groups}
    (OUT/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
    (OUT/'summary.json').write_text(json.dumps(payload,indent=2),encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'start_brt':m['start_brt'],'end_brt':m['end_brt'],
        'config':config['risk'],'fees':config['fees'],'spread_bps':review.spread,
        'cache_hashes':m['cache_hashes'],'input_manifest_hash':digest(INPUT/'manifest.json'),
        'tool_hash':digest(Path(__file__)),'baseline_parity':True,
        'source_hashes':{p:digest(ROOT/p) for p in ('src/position/bot_full_engine.py',
            'src/monitor/market_context.py','tools/pl_lon_elastic_study.py','tools/be_off_cb_fast_drop_systemic_replay.py')},
        'deterioration':'Strict breach of existing economic floor; below-exit small pullback separately recorded. Recovery >= known peak.',
        'scope':'No-TRAIL isolated from original pre-close state; identical pre-exit history because trailing had not exited earlier. PL/HS/other exits unchanged; no new admissions/CB recalculation.',
        'causality':'Context uses last closed 5m at modeled minute open. OHLC order is hypothetical, not ticks.',
        'magnitude':'Raw existing PL trigger boundaries and original activation, not optimized bins.',
        'ceiling':'max(pre-exit known peak, highest subsequent market point) for each horizon; hindsight, non-executable.'},indent=2),encoding='utf-8')
    lines=['# Anatomia do TRAIL atual', '',f"Base {m['start_brt']} → {m['end_brt']}",
        'A proteção útil; B precoce; C misto; D neutro. Deterioração importante = piso econômico existente; recuperação >= pico.',
        'Resultados principais excluem restante do candle de saída; comparação inclusiva explicita sensibilidade OHLC.',
        'Sem TRAIL é isolado, não sistêmico. Teto futuro não é PnL executável.', '']
    header=['grupo','min','modo','N válido','cens','A','B','C','D','pico rec','novo pico','HS trajetória','MAE p50 %','MFE p50 %','sem TRAIL delta $','N resolvido','giveback ATR p50','fração pico % p50']
    def row(label,h,mode,r):return [label,h,mode,r['valid'],r['censored'],*[r['classes'][c] for c in CLASSES],
        r['recovered_peak'],r['new_high'],r['hs_path'],fmt(r['path_metrics']['adverse_pct']['median']),
        fmt(r['path_metrics']['favorable_pct']['median']),fmt(r['no_trail']['delta_total']),r['no_trail']['resolved'],
        fmt(r['giveback']['giveback_atr']['median']),fmt(r['giveback']['fraction_peak_returned_pct']['median'])]
    for path in PATHS:
        lines += [f'## {path}', '']
        lines += table(header,[row(mo,h,mode,r) for mo,hs in summary[path].items() for h,ds in hs.items() for mode,r in ds.items()])
        lines += ['### Restante OHLC / tempo 0', '']
        lines += table(['mês','min','N','abaixo exit t0','piso t0','rec pico t0','novo pico t0','misto depende resto','classe muda sem resto'],
            [[mo,h,r['valid'],r['below_exit_zero'],r['floor_zero'],r['recovery_zero'],r['new_high_zero'],
              r['mixed_depends_on_remainder'],r['classification_changed_without_remainder']]
             for mo,hs in summary[path].items() for h,ds in hs.items() for r in [ds['with_remainder']]])
        lines += ['### Grupos marginais — apenas minutos posteriores (sempre N)', '']
        lines += table(header,[row(f'{field}:{v}',h,'post',r) for field,vs in groups[path].items()
            for v,months in vs.items() for h,r in months['ALL'].items()])
        lines += ['### Giveback e contrafactual mensal', '']
        lines += table(['mês','N','sem TRAIL resolvidos','cens','delta total $','delta médio $','melhor','pior','tempo extra p50 min','motivos alternativos'],
            [[mo,r['valid'],r['no_trail']['resolved'],r['no_trail']['censored'],fmt(r['no_trail']['delta_total']),
              fmt(r['no_trail']['delta']['mean']),r['no_trail']['better'],r['no_trail']['worse'],
              fmt(r['no_trail']['additional_min']['median']),json.dumps(r['no_trail']['reasons'])]
             for mo,hs in summary[path].items() for r in [hs['5']['post_minutes']]])
        lines += table(['mês','medida','N','média','p50','p75','p90','máximo'],
            [[mo,k,r['n'],*[fmt(d[q]) for q in ('mean','median','p75','p90','max')]]
             for mo,hs in summary[path].items() for r in [hs['5']['post_minutes']] for k,d in r['giveback'].items()])
    lines += ['', 'Detalhes completos por evento: events.jsonl. Meses × grupos, ordens mistas/tempos, distribuições e teto: summary.json.',
        'Grupos pequenos são documentação, não evidência suficiente. Nenhuma combinação EMA × momentum criada automaticamente.']
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print('DONE',OUT,flush=True)


if __name__=='__main__':main()
