"""Audit existing systemic admissions after PL/LON; no alternative replay."""
import bisect
import json
import sys
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.pl_lon_elastic_study import OUT as INPUT, CACHE, PRIOR, MONTHS, quantile
from tools.pl_lon_recovery_path_study import first_passage
from tools.be_off_cb_defensive_closure import table, fmt, digest, ms, month
from tools.be_off_cb_defensive_review import Review
from tools.market_selection_study import load_candle_cache, BinancePublicClient
from tools.market_bot_replay import MINUTE_MS, _deduplicate, _round_trip_fees_pct
OUT=ROOT/'data/studies/pl_lon_natural_reentry/20261003'
HORIZONS=('15','30','60','until_lost')

def ownership(events,at):
    eligible=[e for e in events if e['touch_ms']<at]
    if not eligible:return []
    latest=max(e['touch_ms'] for e in eligible)
    return [e['source_candle'] for e in eligible if e['touch_ms']==latest]

def economics(trades):
    closed=sorted((t for t in trades if t['closed_ms'] is not None),key=lambda t:t['closed_ms'])
    vals=[t['net_usd'] for t in closed];eq=peak=dd=0
    for v in vals:eq+=v;peak=max(peak,eq);dd=max(dd,peak-eq)
    gain=sum(v for v in vals if v>0);loss=-sum(v for v in vals if v<0)
    return {'n':len(trades),'closed':len(closed),'open':len(trades)-len(closed),'net':sum(vals),
        'net_trade':sum(vals)/len(vals) if vals else None,'pf':gain/loss if loss else float('inf') if gain else None,
        'dd':dd,**{reason:sum(t['exit_reason']==reason for t in closed) for reason in ('HARD_STOP','PROFIT_LOCK','TRAILING')}}

def valid_split(quantity,price,fraction,min_qty,step,min_notional):
    q,p,f,mq,s,mn=map(lambda x:Decimal(str(x)),(quantity,price,fraction,min_qty,step,min_notional))
    sold=(q*f/s).to_integral_value(rounding=ROUND_DOWN)*s;remaining=q-sold
    return {'sold_qty':str(sold),'remaining_qty':str(remaining),
            'valid':sold>=mq and remaining>=mq and sold*p>=mn and remaining*p>=mn,
            'sold_notional':str(sold*p),'remaining_notional':str(remaining*p)}

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    meta=json.loads((INPUT/'manifest.json').read_text());config=meta['config'];end=ms(meta['end_brt'])
    candles={i:load_candle_cache(CACHE/f'SOLUSDT_{i}.jsonl') for i in ('1m','5m','15m')}
    for i in candles:
        if digest(CACHE/f'SOLUSDT_{i}.jsonl')!=meta['cache_hashes'][i]:raise ValueError('Frozen candle mismatch')
    review=Review(config,candles,[],end);fees=_round_trip_fees_pct(config);exit_spread=review.spread/2/10000
    rows=[];summary={};all_runs={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        raw=json.loads((INPUT/f'{path}_events.json').read_text())
        events=sorted((e for e in raw if e['snapshot']['ema_context']=='LON'),key=lambda e:e['touch_ms'])
        run=json.loads((INPUT/f'{path}_systemic.json').read_text())['BE_OFF_CB'];all_runs[path]=run
        times=sorted(set(e['touch_ms'] for e in events));trades=run['trades']
        for e in events:
            at=e['touch_ms'];i=bisect.bisect_right(times,at)
            next_pl=times[i] if i<len(times) else end+1
            lost=e['benchmark']['lon_lost_ms'] or end+1
            episode_end=min(lost,next_pl,end)
            tied=[x['source_candle'] for x in events if x['touch_ms']==at]
            for h in HORIZONS:
                stop=min(episode_end,at+int(h)*MINUTE_MS) if h!='until_lost' else episode_end
                points=[(at,p) for p in e['remaining']]
                missing=False
                for b in range(at+MINUTE_MS,stop+1,MINUTE_MS):
                    c=review.index.get(b)
                    if c is None:missing=True;continue
                    pts=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST' else (c.open,c.low,c.high,c.close))
                    points.extend((b,p) for p in pts)
                recovery=first_passage(points,e['state']['highest_price'],e['touch_price'],e['entry_atr'],at)
                eligible=[t for t in trades if at<t['opened_ms']<stop and ownership(events,t['opened_ms'])==tied]
                admissions=[a for a in run['admissions'] if at<a['at_ms']<stop]
                matched=[]
                max_total=max((p for _,p in points),default=e['touch_price'])
                control_exit=next(t['exit_price'] for t in trades if t['source_candle']==e['source_candle'])
                total_potential=max(0,max_total-control_exit)*review.notional/e['entry_price']
                for t in eligible:
                    after=[p for b,p in points if b>t['opened_ms']]
                    # Strict boundary excludes the candle completed before entry.
                    high=max(after,default=t['entry_price']);low=min(after,default=t['entry_price'])
                    potential=max(0,high-t['entry_price'])*review.notional/t['entry_price']
                    net_ceiling=review.notional*((high*(1-exit_spread)/t['entry_price']-1)-fees/100)
                    matched.append({**t,'wait_min':(t['opened_ms']-at)/MINUTE_MS,
                        'relative_pl_pct':(t['entry_price']/control_exit-1)*100,
                        'relative_peak_pct':(t['entry_price']/e['state']['highest_price']-1)*100,
                        'remaining_usd':potential,'remaining_fraction':potential/total_potential if total_potential else None,
                        'remaining_net_ceiling':net_ceiling,'adverse_pct':min(0,low/t['entry_price']-1)*100,
                        'realized_fraction':t['net_usd']/potential if t['net_usd'] is not None and potential else None,
                        'late_by_cost':net_ceiling<=0})
                reason=None
                if recovery['recovered'] and not matched:
                    if admissions:
                        reason=' + '.join(sorted(set(a['decision'] for a in admissions)))
                    else:
                        available=any(sum(t['opened_ms']<b and (t['closed_ms'] is None or t['closed_ms']>b) for t in trades)
                            <int(config['capital']['max_open_positions']) and b not in run['cb_paused']
                            for b in range(at+MINUTE_MS,stop,MINUTE_MS))
                        reason='AVAILABLE_NO_APPROVED_SIGNAL_FILTER_REASON_UNAUDITABLE' if available else 'NO_APPROVED_SIGNAL_AVAILABILITY_CONSTRAINED'
                rec_at=at+int(recovery['minutes']*MINUTE_MS) if recovery['recovered'] else None
                label=('NOT_RECOVERED' if not recovery['recovered'] else
                       'B_NO_ENTRY' if not matched else 'C_LATE_COST' if matched[0]['late_by_cost'] else 'A_CAPTURED')
                rows.append({'path':path,'source_candle':e['source_candle'],'month':month(at),'horizon':h,
                    'pl_at_ms':at,'pl_exit_price':control_exit,'touch_price':e['touch_price'],
                    'peak_known':e['state']['highest_price'],'snapshot':e['snapshot'],
                    'episode_end_ms':episode_end,'window_end_ms':stop,'next_pl_ms':next_pl,'lon_lost_ms':lost,
                    'anchor_sources':tied,'ambiguous_anchor':len(tied)>1,'incomplete':missing,
                    'recovery':recovery,'recovery_at_ms':rec_at,
                    'recovery_context':review.context_fields(max(at,rec_at-MINUTE_MS)) if rec_at is not None else None,
                    'total_favorable_usd':total_potential,'adverse_pct':min(0,min((p for _,p in points),default=e['touch_price'])/control_exit-1)*100,
                    'admissions':admissions,'reentries':matched,'classification':label,'missing_reason':reason,
                    'already_open_at_pl':sum(t['opened_ms']<at and (t['closed_ms'] is None or t['closed_ms']>at) for t in trades)})
        summary[path]={}
        for m in MONTHS:
            summary[path][m]={}
            for h in HORIZONS:
                selected=[r for r in rows if r['path']==path and r['horizon']==h and (m=='ALL' or r['month']==m)]
                valid=[r for r in selected if not r['ambiguous_anchor'] and not r['incomplete']]
                resumed=[r for r in valid if r['recovery']['recovered']]
                capture=[r for r in resumed if r['reentries']]
                # Source identities deduplicate economics; do not multiply one
                # reentry by multiple PLs or horizons in a monthly table.
                unique={t['source_candle']:t for r in capture for t in r['reentries']}
                first=[r['reentries'][0] for r in capture]
                nets=economics(list(unique.values()))
                summary[path][m][h]={'events':len(selected),'valid':len(valid),
                    'ambiguous':sum(r['ambiguous_anchor'] for r in selected),'resumed':len(resumed),
                    'captured':len(capture),'capture_pct':100*len(capture)/len(resumed) if resumed else None,
                    'missed':sum(r['classification']=='B_NO_ENTRY' for r in resumed),
                    'late':sum(r['classification']=='C_LATE_COST' for r in resumed),
                    'before_peak':sum(t['opened_ms']<r['recovery_at_ms'] for r in capture for t in [r['reentries'][0]]),
                    'after_or_same_minute_peak':sum(t['opened_ms']>=r['recovery_at_ms'] for r in capture for t in [r['reentries'][0]]),
                    'timing':{str(q):quantile([t['wait_min'] for t in first],q) for q in (.5,.75,.9)},
                    'first_relative_pl_pct_p50':quantile([t['relative_pl_pct'] for t in first],.5),
                    'first_relative_peak_pct_p50':quantile([t['relative_peak_pct'] for t in first],.5),
                    'remaining_usd_p50':quantile([t['remaining_usd'] for t in first],.5),
                    'remaining_fraction_p50':quantile([t['remaining_fraction'] for t in first if t['remaining_fraction'] is not None],.5),
                    'realized_fraction_p50':quantile([t['realized_fraction'] for t in first if t['realized_fraction'] is not None],.5),
                    'missing_reasons':{reason:sum(r['missing_reason']==reason for r in resumed)
                                      for reason in sorted({r['missing_reason'] for r in resumed if r['missing_reason']})},
                    'economics':nets}
    (OUT/'events.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    # Read-only public TESTNET metadata, never an order or authenticated client.
    partial={'notional':review.notional,'status':'UNAVAILABLE'}
    try:
        from tools.cohort_study import _load_config
        current=_load_config(ROOT/'config/config.yaml')
        url=current['execution']['testnet_url']
        recorded=OUT/'public_testnet_filters.json'
        data=json.loads(recorded.read_text()) if recorded.exists() else BinancePublicClient(url,15).get('/api/v3/exchangeInfo',{'symbol':'SOLUSDT'})
        filters={f['filterType']:f for f in data['symbols'][0]['filters']}
        lot=filters['LOT_SIZE'];notional=filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL')
        partial.update({'status':'VERIFIED_PUBLIC_TESTNET_SNAPSHOT' if recorded.exists() else 'VERIFIED_PUBLIC_TESTNET',
                       'url':url,'exchange_info':data,'snapshot_sha256':digest(recorded) if recorded.exists() else None,
                       'limit':'current filter snapshot applied to study price range; not historical filters or guarantee of future fills; NOTIONAL uses 5m average'})
        partial['fractions']={}
        prices=[t['entry_price'] for run in all_runs.values() for t in run['trades']]
        step=Decimal(lot['stepSize'])
        for f in (.25,.4,.5,.6,.75):
            checks=[]
            for price in prices:
                q=(Decimal(str(review.notional))/Decimal(str(price))/step).to_integral_value(rounding=ROUND_DOWN)*step
                checks.append(valid_split(q,price,f,lot['minQty'],lot['stepSize'],notional['minNotional']))
            partial['fractions'][str(f)]={'valid':sum(c['valid'] for c in checks),'n':len(checks),'examples':checks[:2]}
    except Exception as exc:partial['error']=str(exc)
    (OUT/'partial_execution.json').write_text(json.dumps(partial,indent=2),encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'input_sha256':digest(INPUT/'manifest.json'),'tool_sha256':digest(Path(__file__)),
        'start_brt':meta['start_brt'],'end_brt':meta['end_brt'],
        'pairing':'latest strictly prior LON PL, expires on next LON PL or LON loss; tied PL anchors ambiguous, excluded from rates/economics',
        'late':'remaining maximum favorable cannot cover existing round-trip costs, diagnostic only',
        'limitation':'admission audit starts at approved normal signals; pre-signal filter failure not recorded; do not attribute absence to missing algorithm signal',
        'scope':'isolated association of existing baseline replay; no new replay or rule'},indent=2),encoding='utf-8')
    lines=['# Reentrada natural pós-PL LON', '',f"{meta['start_brt']} → {meta['end_brt']}; base local congelada.",
        'Último PL estritamente anterior é a âncora; próximo PL LON ou perda LON encerra episódio. Empates não forçados. Economia deduplicada por source.',
        'Retomada relevante = recuperação do pico conhecido. C = máximo futuro restante não cobre custos existentes, não um novo threshold de trading.',
        'Razões auditadas vêm dos logs de admissão do replay. Sem oportunidade aprovada não permite distinguir ausência de sinal bruto de reprovação por filtros internos.', '']
    for p,months in summary.items():
        lines += [f'## {p} — lacuna e bloqueios primeiro', '']
        lines += table(['mês','janela','PL','ambiguous','retomadas','capturadas','%','sem entrada','tarde','motivos'],
            [[m,h,r['events'],r['ambiguous'],r['resumed'],r['captured'],fmt(r['capture_pct']),r['missed'],r['late'],json.dumps(r['missing_reasons'])] for m,hs in months.items() for h,r in hs.items()])
        lines += ['### Economia/timing das reentradas associadas', '']
        lines += table(['mês','janela','N único','closed','open','net','net/trade','PF','DD','HS','PL','TRAIL','antes pico','depois/mesmo min','wait p50','p75','p90','entry vs PL %','entry vs pico %','restante $ p50','restante fração p50','net/restante p50'],
            [[m,h,*[fmt(r['economics'][k]) for k in ('n','closed','open','net','net_trade','pf','dd','HARD_STOP','PROFIT_LOCK','TRAILING')],r['before_peak'],r['after_or_same_minute_peak'],
              *[fmt(r['timing'][str(q)]) for q in (.5,.75,.9)],*[fmt(r[k]) for k in ('first_relative_pl_pct_p50','first_relative_peak_pct_p50','remaining_usd_p50','remaining_fraction_p50','realized_fraction_p50')]] for m,hs in months.items() for h,r in hs.items()])
    lines += ['', 'Partial execution metadata: '+partial['status'],
              'Economia: destino FINAL do trade, pode ultrapassar episódio; net/restante é atribuição descritiva, não captura causal garantida. Tabelas por mês do PL, sem somar horizontes.',
              'Sem implementação/replay das famílias A/B/C/D. Sem runtime/YAML/estado/restart/git.']
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    print('DONE',OUT,partial['status'])

if __name__=='__main__':main()
