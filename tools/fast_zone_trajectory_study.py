"""Causal 15/30/60m landmark diagnosis; no trading rule or model fitting."""
import argparse
import bisect
import hashlib
import json
import math
import subprocess
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.winner_trajectory_study import INPUT,CACHE,SIGNALS
from tools.fast_drop_v2_study import OUT as PREVIOUS
from tools.be_off_cb_defensive_review import Review,iso
from tools.be_off_cb_defensive_closure import ms
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import _deduplicate
from tools.ge_replay_study import SignalEvent
from src.monitor.entry_engine import EntrySignal
from src.monitor.fast_drop_semantics import loss_reached
from tools.be_off_cb_exit_context_study import brt
from tools.fast_drop_post_trigger_study import distribution

OUT=ROOT/'data/studies/fast_zone_trajectory/20261007'
WINDOWS=(15,30,60)
FEATURES=('worst_pnl','best_pnl','best_recovery_after_first_bottom',
    'recovered_FAST','recovered_minus025','returned_zero','exceeded_entry',
    'minutes_below050','minutes_below075','minutes_below100',
    'fall_speed_median','fall_speed_max','recovery_speed_median','recovery_speed_max',
    'local_bottom_count','recovery_count','recovery_amplitude_median','recovery_amplitude_max',
    'new_bottom_after_recovery_count','bottom_distance_pct_median','bottom_distance_minutes_median',
    'bottom_slope_pct_per_min','bottoms_descending','bottoms_flat','bottoms_ascending',
    'largest_recovery_lost_again','end_pnl')
SINCE=ms('2026-10-06T12:00:00-03:00')


def median(values):
    s=sorted(values)
    if not s:return None
    n=len(s);return (s[(n-1)//2]+s[n//2])/2


def freeze_protocol():
    OUT.mkdir(parents=True,exist_ok=True)
    plan={'windows_min':WINDOWS,'features':FEATURES,'zone':'first causal visited quote PnL <= -0.50%, no speed/EMA filtering',
        'anchor':'minute-end label of first modeled crossing; ignore remaining entry-zone candle',
        'extrema_and_levels':'ordered OHLC of later full 1m bars; only prices visited before control exit',
        'structure':'zone quote then subsequent 1m closes; direction reversals confirmed within prefix',
        'bottom_initial_direction':'descending upon loss-zone crossing; a subsequent rise confirms first bottom',
        'stable_bottoms':'slope zero within 1e-12 numerical tolerance, no tuned economic tolerance',
        'time_below':'count of full observed 1m closes below level, one minute each; proxy, not tick duration',
        'velocities':'maximal close-series monotone legs, percentage points/minute; no zero-duration division',
        'lost_recovery':'max observed rebound from a confirmed bottom minus deepest subsequent drawdown from that rebound peak, capped by rebound amplitude',
        'risk_sets':'OPEN_AT_CUT primary; CLOSED_BEFORE/AT_CUT separate, never complete after exit',
        'analysis':'all metrics, quantiles, median differences, Cliff delta, KS D, central-90% cross-containment; no fitted classifier or feature ranking',
        'exceptions':'for every metric, three HS nearest other median and vice versa; not handpicked after ranking',
        'period_stability':'monthly effects with N, no assertions for sparse groups',
        'OOS':'opened_at >= 06/10 12 BRT; public 1m market reconstruction separate from observed runtime ticks; no changes to features',
        'threshold_search':False,'combined_predictive_model':False}
    plan=json.loads(json.dumps(plan));p=OUT/'protocol.json'
    if p.exists() and json.loads(p.read_text())!=plan:raise ValueError('Protocol frozen: do not change features after results')
    if not p.exists():p.write_text(json.dumps(plan,indent=2),encoding='utf8')
    return plan


def features(quotes,closes,entry,zone_quote):
    """Only prefix inputs; no outcome, final minimum or total lifetime arguments."""
    quote_pnl=[(p/entry-1)*100 for _,p in quotes]
    series=[(t,(p/entry-1)*100) for t,p in closes]
    anchor=(zone_quote/entry-1)*100
    reached=lambda level: int(any(v>=level-1e-12 for v in quote_pnl[1:]))
    result={'worst_pnl':min(quote_pnl),'best_pnl':max(quote_pnl),
        'recovered_FAST':reached(-.5),'recovered_minus025':reached(-.25),
        'returned_zero':reached(0),'exceeded_entry':int(any(v>0+1e-12 for v in quote_pnl[1:])),
        'minutes_below050':sum(v<-.5-1e-12 for _,v in series[1:]),
        'minutes_below075':sum(v<-.75-1e-12 for _,v in series[1:]),
        'minutes_below100':sum(v<-1-1e-12 for _,v in series[1:]),'end_pnl':series[-1][1]}
    bottoms=[];rebounds=[];legs=[];direction=-1;start=0
    for i in range(1,len(series)):
        diff=series[i][1]-series[i-1][1]
        sign=1 if diff>1e-12 else -1 if diff<-1e-12 else 0
        if not sign:continue
        if sign!=direction:
            pivot=i-1
            if pivot>start:legs.append((start,pivot,direction))
            if sign==1:bottoms.append(pivot)
            else:
                if bottoms:rebounds.append((bottoms[-1],pivot))
            start=pivot;direction=sign
    if len(series)-1>start:legs.append((start,len(series)-1,direction))
    # A recovery leg still progressing at the cut is observable but not a confirmed peak.
    if direction==1 and bottoms and (not rebounds or rebounds[-1][0]!=bottoms[-1]):rebounds.append((bottoms[-1],len(series)-1))
    speeds={-1:[],1:[]}
    for a,b,d in legs:
        elapsed=(series[b][0]-series[a][0])/60000
        if elapsed>0:speeds[d].append(abs(series[b][1]-series[a][1])/elapsed)
    amps=[series[b][1]-series[a][1] for a,b in rebounds]
    first=bottoms[0] if bottoms else None
    result['best_recovery_after_first_bottom']=max(v for _,v in series[first:])-series[first][1] if first is not None else None
    result.update(fall_speed_median=median(speeds[-1]),fall_speed_max=max(speeds[-1]) if speeds[-1] else None,
        recovery_speed_median=median(speeds[1]),recovery_speed_max=max(speeds[1]) if speeds[1] else None,
        local_bottom_count=len(bottoms),recovery_count=len(rebounds),
        recovery_amplitude_median=median(amps),recovery_amplitude_max=max(amps) if amps else None,
        new_bottom_after_recovery_count=sum(series[b][1]<series[a][1]-1e-12 for a,b in zip(bottoms,bottoms[1:])),
        bottom_distance_pct_median=median([series[b][1]-series[a][1] for a,b in zip(bottoms,bottoms[1:])]),
        bottom_distance_minutes_median=median([(series[b][0]-series[a][0])/60000 for a,b in zip(bottoms,bottoms[1:])]))
    slope=None
    if len(bottoms)>=2:
        elapsed=(series[bottoms[-1]][0]-series[bottoms[0]][0])/60000
        if elapsed>0:slope=(series[bottoms[-1]][1]-series[bottoms[0]][1])/elapsed
    result.update(bottom_slope_pct_per_min=slope,
        bottoms_descending=None if slope is None else int(slope<-1e-12),
        bottoms_flat=None if slope is None else int(abs(slope)<=1e-12),
        bottoms_ascending=None if slope is None else int(slope>1e-12))
    lost=[]
    for a,b in rebounds:
        amplitude=series[b][1]-series[a][1]
        drawdown=series[b][1]-min(v for _,v in series[b:])
        lost.append(min(amplitude,max(0,drawdown)))
    result['largest_recovery_lost_again']=max(lost,default=0)
    result['details']={'bottoms':[(series[i][0],series[i][1]) for i in bottoms],
        'recovery_amplitudes':amps,'fall_speeds':speeds[-1],'recovery_speeds':speeds[1]}
    return result


def windows(zone_ms,zone_quote,entry,quotes,closes,closed_ms,available_end):
    out=[]
    for minutes in WINDOWS:
        cut=zone_ms+minutes*60000
        status='OPEN_AT_CUT' if closed_ms is None or closed_ms>cut else 'CLOSED_AT_CUT' if closed_ms==cut else 'CLOSED_BEFORE_CUT'
        observed=min(cut,closed_ms or available_end,available_end)
        if available_end<cut and status=='OPEN_AT_CUT':status='DATA_END'
        q=[(zone_ms,zone_quote)]+[(t,p) for t,p in quotes if zone_ms<t<=observed]
        c=[(zone_ms,zone_quote)]+[(t,p) for t,p in closes if zone_ms<t<=observed]
        expected=(observed-zone_ms)//60000
        # Fully observed landmark cuts require every next minute close.
        full=[t for t,_ in c[1:]]
        if status=='OPEN_AT_CUT' and (len(full)!=minutes or any(t!=zone_ms+(i+1)*60000 for i,t in enumerate(full))):status='DATA_GAP'
        out.append({'minutes':minutes,'cut_ms':cut,'status':status,'observed_minutes':(observed-zone_ms)/60000,
            'features':features(q,c,entry,zone_quote)})
    return out


def reconstruct(row,review,path):
    rp=review.new_position(row['opened_ms'],row['entry']);p=rp.position
    zone_ms=None;zone_quote=None;quotes=[];closes=[];closed=None
    for c in review.minute[bisect.bisect_left(review.opens,row['opened_ms']):]:
        if c.boundary_ms>review.end:break
        if zone_ms is not None and c.boundary_ms>zone_ms+60*60000:break
        previous=None
        points=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST' else (c.open,c.low,c.high,c.close))
        for point in points:
            stop=p.effective_stop;tick=stop if previous is not None and previous>stop and point<=stop else point
            if zone_ms is None and loss_reached(p.entry_price,tick):
                zone_ms=c.boundary_ms
                zone_quote=p.entry_price*.995 if previous is not None and previous>p.entry_price*.995 else tick
            rp.client.current_price=tick;p.on_tick(tick,iso(c.boundary_ms));previous=point
            if zone_ms is not None and c.boundary_ms>zone_ms:quotes.append((c.boundary_ms,tick))
            if p.status=='CLOSED':closed=c.boundary_ms;break
        if zone_ms is not None and c.boundary_ms>zone_ms:closes.append((c.boundary_ms,tick))
        if p.status=='CLOSED':break
    if closed is not None and (closed!=row['closed_ms'] or p.exit_reason!=row['reason'] or abs(p.exit_price-row['exit'])>1e-8):raise AssertionError('Prefix exit parity')
    if zone_ms is None:return None
    return {'source':row['source'],'opened_ms':row['opened_ms'],'zone_ms':zone_ms,'zone_quote':zone_quote,
        'entry':row['entry'],'label':row['reason'],'month':brt(zone_ms)[:7],
        'windows':windows(zone_ms,zone_quote,row['entry'],quotes,closes,closed if closed is not None else row['closed_ms'],review.end)}


def effect(a,b):
    if not a or not b:return {'N_HS':len(a),'N_other':len(b),'median_difference':None,'cliff_delta':None,'KS_D':None}
    a=sorted(a);b=sorted(b)
    gt=sum(bisect.bisect_left(b,x) for x in a);lt=sum(len(b)-bisect.bisect_right(b,x) for x in a)
    ks=max(abs(bisect.bisect_right(a,x)/len(a)-bisect.bisect_right(b,x)/len(b)) for x in sorted(set(a+b)))
    qa=distribution(a);qb=distribution(b)
    return {'N_HS':len(a),'N_other':len(b),'median_difference':median(a)-median(b),
        'cliff_delta':(gt-lt)/(len(a)*len(b)),'KS_D':ks,
        'HS_inside_other_central90_pct':sum(qb['p05']<=x<=qb['p95'] for x in a)/len(a)*100,
        'other_inside_HS_central90_pct':sum(qa['p05']<=x<=qa['p95'] for x in b)/len(b)*100}


def comparisons(rows):
    result={}
    for w in WINDOWS:
        rows_w=[{**r,'snapshot':next(x for x in r['windows'] if x['minutes']==w)} for r in rows]
        alive=[r for r in rows_w if r['snapshot']['status']=='OPEN_AT_CUT']
        result[str(w)]={'status_counts':{label:dict(Counter(r['snapshot']['status'] for r in rows_w if r['label']==label)) for label in ('HARD_STOP','PROFIT_LOCK','TRAILING')},'metrics':{}}
        for name in FEATURES:
            groups={label:[r for r in alive if r['label']==label and r['snapshot']['features'][name] is not None] for label in ('HARD_STOP','PROFIT_LOCK','TRAILING')}
            values={label:[r['snapshot']['features'][name] for r in rr] for label,rr in groups.items()}
            d={'distribution':{label:distribution(v) for label,v in values.items()},
               'missing':{label:sum(r['label']==label and r['snapshot']['features'][name] is None for r in alive) for label in groups},'comparisons':{}}
            for label in ('PROFIT_LOCK','TRAILING'):
                ea=values['HARD_STOP'];eb=values[label]
                exceptions={}
                for origin,target in (('HARD_STOP',label),(label,'HARD_STOP')):
                    center=median(values[target])
                    exceptions[origin]=[] if center is None else [{'source':r['source'],'zone_ms':r['zone_ms'],'value':r['snapshot']['features'][name]} for r in sorted(groups[origin],key=lambda r:abs(r['snapshot']['features'][name]-center))[:3]]
                d['comparisons'][label]={'overall':effect(ea,eb),'exceptions_near_other_median':exceptions,
                    'monthly':{m:effect([r['snapshot']['features'][name] for r in groups['HARD_STOP'] if r['month']==m],
                        [r['snapshot']['features'][name] for r in groups[label] if r['month']==m]) for m in sorted({r['month'] for r in alive})}}
            result[str(w)]['metrics'][name]=d
    return result


def fetch_recent():
    script=r'''
import json,datetime,urllib.request,urllib.parse
since='2026-10-06T15:00:00'
state=json.load(open('data/state/be_off_cb_shadow.json'))
rows=[]
for original in state['closed_records']+state['positions']:
 r=dict(original);r['opened_at']=r.get('opened_at') or r.get('open_ts')
 if r.get('opened_at','')>=since:rows.append(r)
now=int(datetime.datetime.fromisoformat(state['updated_at']).timestamp()*1000)//60000*60000
start=min(int(datetime.datetime.fromisoformat(r['opened_at']).timestamp()*1000) for r in rows)//60000*60000 if rows else now
result={'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'state_updated_at':state['updated_at'],'rows':rows,'start_ms':start,'end_ms':now,'public_1m':[],'errors':[]}
try:
 while start<now:
  query=urllib.parse.urlencode({'symbol':'SOLUSDT','interval':'1m','startTime':start,'endTime':now-1,'limit':1000})
  data=json.load(urllib.request.urlopen('https://api.binance.com/api/v3/klines?'+query,timeout=30))
  if not data:break
  result['public_1m'].extend([r for r in data if int(r[6])+1<=now]);start=int(data[-1][0])+60000
except Exception as exc:result['errors'].append(str(exc))
print(json.dumps(result))
'''
    r=subprocess.run(['ssh','-o','BatchMode=yes','root@207.154.197.12','cd /root/trend-sol && venv/bin/python -'],input=script,text=True,capture_output=True,check=True)
    (OUT/'recent_raw.json').write_text(r.stdout,encoding='utf8')


def recent_analysis(raw,path):
    bars=raw.get('public_1m',[]);rows=[]
    for trade in raw['rows']:
        opened=ms(trade['opened_at']);closed=ms(trade['closed_at']) if trade.get('closed_at') else None
        entry=float(trade['entry_price']);target=entry*.995
        selected=[c for c in bars if int(c[0])>=opened and (closed is None or int(c[6])+1<=closed)]
        partial=[c for c in bars if int(c[0])<opened<int(c[6])+1]
        base={'source':trade['source_candle_open_time'],'opened_ms':opened,'entry':entry,
            'label':trade.get('exit_reason') or 'OPEN','month':None,'windows':[],
            'entry_partial_minute_omitted':bool(partial),'source_kind':'public_1m_not_received_runtime_ticks'}
        if partial and float(partial[0][3])<=target:
            rows.append({**base,'quality':'ZONE_ANCHOR_UNCERTAIN_PARTIAL_ENTRY'});continue
        zone=None;zone_quote=None;quotes=[];closes=[]
        for c in selected:
            at=int(c[6])+1
            pts=_deduplicate((float(c[1]),float(c[2]),float(c[3]),float(c[4])) if path=='HIGH_FIRST' else (float(c[1]),float(c[3]),float(c[2]),float(c[4])))
            previous=None
            for price in pts:
                if zone is None and loss_reached(entry,price):zone=at;zone_quote=target if previous is not None and previous>target else price
                if zone is not None and at>zone:quotes.append((at,price))
                previous=price
            if zone is not None and at>zone:closes.append((at,float(c[4])))
        if zone is None:rows.append({**base,'quality':'NO_OBSERVED_ZONE_OR_MISSING_DATA'});continue
        if closed and trade.get('exit_trigger_price') is not None and closed>zone:
            quotes.append((closed,float(trade['exit_trigger_price'])))
        rows.append({**base,'quality':'MODELED_PUBLIC_PATH','zone_ms':zone,'zone_quote':zone_quote,'month':brt(zone)[:7],
            'windows':windows(zone,zone_quote,entry,quotes,closes,closed,raw['end_ms'])})
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--fetch-recent',action='store_true');parser.add_argument('--recent-only',action='store_true')
    args=parser.parse_args();freeze_protocol()
    if not args.recent_only:
        frozen=json.loads((INPUT/'manifest.json').read_text());digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
        for tf,h in frozen['cache_hashes'].items():
            if digest(CACHE/f'SOLUSDT_{tf}.jsonl')!=h:raise ValueError('Frozen cache changed')
        if digest(ROOT/'src/position/bot_full_engine.py')!=frozen['source_hashes']['src/position/bot_full_engine.py']:raise ValueError('Frozen engine changed')
        if digest(SIGNALS)!=frozen['signals_sha256']:raise ValueError('Frozen signals changed')
        candles={tf:load_candle_cache(CACHE/f'SOLUSDT_{tf}.jsonl') for tf in ('1m','5m','15m')}
        signals=[SignalEvent(s['boundary_ms'],EntrySignal(**s['signal'])) for s in json.loads(SIGNALS.read_text())]
        review=Review(frozen['config'],candles,signals,ms(frozen['end_brt']));summary={}
        for path in ('HIGH_FIRST','LOW_FIRST'):
            base=json.loads((PREVIOUS/f'{path}_CONTROL_trades.json').read_text())['closed'];rows=[]
            for i,r in enumerate(base):
                if i%200==0:print(path,i,'/',len(base),flush=True)
                reconstructed=reconstruct(r,review,path)
                if reconstructed:rows.append(reconstructed)
            (OUT/f'{path}_historical.json').write_text(json.dumps(rows,indent=2),encoding='utf8')
            summary[path]=comparisons(rows)
        (OUT/'historical_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
        (OUT/'historical_manifest.json').write_text(json.dumps({'start':frozen['start_brt'],'end':frozen['end_brt'],
            'protocol_sha256':digest(OUT/'protocol.json'),'tool_sha256':digest(Path(__file__)),
            'signals_sha256':digest(SIGNALS),'frozen_manifest_sha256':digest(INPUT/'manifest.json'),
            'cache_sha256':frozen['cache_hashes']},indent=2),encoding='utf8')
    if args.fetch_recent:
        if not (OUT/'historical_summary.json').exists():raise ValueError('Complete history before recent inspection')
        fetch_recent()
    if (OUT/'recent_raw.json').exists():
        raw=json.loads((OUT/'recent_raw.json').read_text());history=json.loads((OUT/'historical_summary.json').read_text());recent={}
        for path in ('HIGH_FIRST','LOW_FIRST'):
            rr=recent_analysis(raw,path)
            for r in rr:
                for w in r['windows']:
                    w['historical_positions']={}
                    if w['status']!='OPEN_AT_CUT':
                        w['comparison_not_applicable']='Incomplete/closed prefix must not be compared as a full landmark window'
                        continue
                    for name in FEATURES:
                        value=w['features'][name]
                        position={}
                        for label in ('HARD_STOP','PROFIT_LOCK','TRAILING'):
                            distribution_=history[path][str(w['minutes'])]['metrics'][name]['distribution'][label]
                            samples=distribution_['all_sorted']
                            position[label]={'N':len(samples),'empirical_percentile':None if value is None or not samples else (bisect.bisect_left(samples,value)+bisect.bisect_right(samples,value))/(2*len(samples))*100,
                                'outside_observed_range':None if value is None or not samples else value<samples[0] or value>samples[-1]}
                        w['historical_positions'][name]=position
            recent[path]=rr
        (OUT/'recent_analysis.json').write_text(json.dumps(recent,indent=2),encoding='utf8')


if __name__=='__main__':main()
