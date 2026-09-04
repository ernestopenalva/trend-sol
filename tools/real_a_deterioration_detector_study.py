"""READ-ONLY: fast deterioration features for future REAL_A sizing research."""
from __future__ import annotations
import argparse, statistics, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from src.console_utils import BRASILIA_TZ
from src.indicators.indicators import atr, ema
from src.monitor.context_predicates import passes_slow_ge45
from src.trade_ledger import TradeLedger
from tools.ge_replay_study import load_ge_market_data
from tools.market_selection_study import BinancePublicClient

START='2026-05-29T00:00:00-03:00'; END='2026-09-01T00:00:00-03:00'

def main():
 p=argparse.ArgumentParser(description='READ-ONLY REAL_A deterioration detector study')
 p.add_argument('--since',default=START);p.add_argument('--until',default=END);p.add_argument('--capital',type=float,default=100);p.add_argument('--ledger',default=str(ROOT/'data/trades/trades_B.jsonl'));p.add_argument('--cache-dir',default=str(ROOT/'data/studies/real_a_deterioration/klines'));p.add_argument('--offline',action='store_true');p.add_argument('--market-data-url',default='https://api.binance.com');p.add_argument('--http-timeout-seconds',type=int,default=15);a=p.parse_args()
 start,end=_dt(a.since),_dt(a.until); sm,em=int(start.timestamp()*1000),int(end.timestamp()*1000)
 client=BinancePublicClient(a.market_data_url,a.http_timeout_seconds)
 c=load_ge_market_data(client,'SOLUSDT','5m',sm-200*300000,em,Path(a.cache_dir),a.offline)
 c15=load_ge_market_data(client,'SOLUSDT','15m',sm-20*900000,em,Path(a.cache_dir),a.offline)
 rows=_features(c,c15,sm,em); trades=_trades(Path(a.ledger),start,end); t0=_t0(trades,a.capital,4); t0s=_t0(trades,a.capital,2)
 print('TREND-SOL | REAL_A deterioration detector study | READ-ONLY')
 print(f'Frozen window: {_fmt(start)} -> {_fmt(end)} | 5m closed candles={len(rows)} | 15m closed candles={len(c15)} | REAL_A closed trades={len(trades)}')
 print('Feature identity: SLOW_GE45_ADVERSE is NOT the shared runtime GE45 predicate (15m high[t]>high[t-3] AND low[t]>low[t-3]). CLOSE_5M_DOWN is a separate descriptive 5m close[t]<close[t-3] feature.')
 print('T0 primary (retrospective ground truth): first trade close whose NEXT 4h has >=2 closes and cumulative net <= -0.5% of capital. Sensitivity: same definition, NEXT 2h. T0 is detector-independent.')
 print(f'T0 episodes: primary={len(t0)} | sensitivity_2h={len(t0s)}')
 print('\nPRIMARY T0 4H')
 for name,raw in _candidates(rows).items():
  stable=_confirm(raw); _report(name,'RAW',rows,raw,t0,trades,a.capital); _report(name,'STABILIZED_2x5m',rows,stable,t0,trades,a.capital)
 print('\nSENSITIVITY T0 2H (only delay changes; economic repricing remains entry-state based)')
 for name,raw in _candidates(rows).items():
  _report(name,'RAW',rows,raw,t0s,trades,a.capital,brief=True); _report(name,'STABILIZED_2x5m',rows,_confirm(raw),t0s,trades,a.capital,brief=True)
 uniform=[item for item in trades if str(item[3].get('strategy_version'))=='b_atr_v1.4' and float(item[3].get('hard_stop_pct') or 0)==1.5 and item[3].get('no_progress_enabled') is False]
 print(f'\nUNIFORM ECONOMIC SUBCOHORT | b_atr_v1.4 / HS=1.5 / NPE=false | N={len(uniform)}')
 for name in ('SLOW_GE45_ADVERSE','CLOSE_5M_DOWN','LOCAL_DD_12'):
  raw=_candidates(rows)[name]; stable=_confirm(raw); _report(name,'RAW',rows,raw,t0,uniform,a.capital); _report(name,'STABILIZED_2x5m',rows,stable,t0,uniform,a.capital); _audit_mapping(name+' STABILIZED',rows,stable,uniform)
 print('\nWEEKLY ECONOMIC STABILITY | SLOW_GE45, CLOSE_5M_DOWN and LOCAL_DD (stabilized)')
 for name in ('SLOW_GE45_ADVERSE','CLOSE_5M_DOWN','LOCAL_DD_12'):_weekly(name,rows,_confirm(_candidates(rows)[name]),trades,a.capital)
 print('LIMITS: exploratory/in-sample; overlapping 5m observations are not independent; repricing preserves ledger entries and cannot model changed slots/admission.')

def _features(c,c15,sm,em):
 closes=[x.close for x in c]; e=ema(closes,20); aa=atr([x.high for x in c],[x.low for x in c],closes,14);out=[]
 index15=0; available15=[]
 for i,x in enumerate(c):
  while index15<len(c15) and c15[index15].boundary_ms<=x.boundary_ms:
   available15.append(c15[index15]);index15+=1
  if not(sm<=x.boundary_ms<em) or i<20 or e[i] is None:continue
  above=sum(closes[j]>float(e[j]) for j in range(i-7,i+1) if e[j] is not None)/8
  slow_ge45=passes_slow_ge45(available15)
  out.append({'t':x.boundary_ms,'close':x.close,'slow_ge45_adverse':not slow_ge45,'close_5m_down':closes[i]<closes[i-3],'ema_below':x.close<float(e[i]),'consistency_loss':above<.5,'local_dd':x.close/max(closes[i-11:i+1])<=.997,'atr_expand':aa[i] is not None and aa[i-3] is not None and float(aa[i])>float(aa[i-3])*1.15})
 return out
def _candidates(rows):return {'SLOW_GE45_ADVERSE':[x['slow_ge45_adverse'] for x in rows],'CLOSE_5M_DOWN':[x['close_5m_down'] for x in rows],'PRICE_BELOW_EMA20':[x['ema_below'] for x in rows],'EMA20_CONSISTENCY_8':[x['consistency_loss'] for x in rows],'LOCAL_DD_12':[x['local_dd'] for x in rows],'ATR_EXPANSION':[x['atr_expand'] for x in rows]}
def _confirm(v):return [False]+[bool(v[i] and v[i-1]) for i in range(1,len(v))]
def _trades(path,start,end):
 out=[]
 for x in TradeLedger(ROOT,path).load():
  o,z=_dt0(x.get('opened_at')),_dt0(x.get('closed_at'))
  if o and z and start<=o<end and not x.get('phantom') and not x.get('shadow_kind') and x.get('position_type')=='BOT_EXIT':out.append((o,z,float(x.get('net_pnl_pct') or 0),x))
 return sorted(out,key=lambda x:x[1])
def _t0(trades,cap,h):
 out=[]
 for _,close,_,_ in trades:
  w=[p for _,z,p,_ in trades if close<z<=close+timedelta(hours=h)]
  if len(w)>=2 and sum(w)*.2<=-.005*cap: out.append(close)
 return [x for i,x in enumerate(out) if not i or x-out[i-1]>timedelta(hours=4)]
def _report(name,kind,rows,state,t0,trades,cap,brief=False):
 starts=[rows[i]['t'] for i,v in enumerate(state) if v and (i==0 or not state[i-1])]; flips=sum(state[i]!=state[i-1] for i in range(1,len(state))); delays=[]
 for zero in t0:
  hit=next((x for x in starts if x>=int(zero.timestamp()*1000)),None)
  if hit:delays.append((hit-int(zero.timestamp()*1000))/60000)
 tagged=[]
 for o,_,p,x in trades:
  active=next((state[i] for i in range(len(rows)-1,-1,-1) if rows[i]['t']<=int(o.timestamp()*1000)),False);tagged.append((active,p,x))
 bad=[p for active,p,_ in tagged if active]; good=[p for active,p,_ in tagged if not active]
 def sizing(new_notional):
  reduction=(20-new_notional)/100
  avoided=sum(-p*reduction for active,p,_ in tagged if active and p<0)
  sacrificed=sum(p*reduction for active,p,_ in tagged if active and p>0)
  return avoided,sacrificed
 loss,sacrificed=sizing(15); loss10,sacrificed10=sizing(10)
 delay_median = f'{statistics.median(delays):.1f}' if delays else 'n/a'; delay_mean = f'{statistics.fmean(delays):.1f}' if delays else 'n/a'
 bad_mean = f'{statistics.fmean(bad):+.3f}%' if bad else 'n/a'; good_mean = f'{statistics.fmean(good):+.3f}%' if good else 'n/a'
 if brief: print(f'{name} | {kind} | delay median/mean={delay_median}/{delay_mean}m | p25/p75={statistics.quantiles(delays,n=4)[0]:.1f}/{statistics.quantiles(delays,n=4)[2]:.1f}m' if len(delays)>=4 else f'{name} | {kind} | delay median/mean={delay_median}/{delay_mean}m'); return
 print(f'{name} | {kind} | delay median/mean={delay_median}/{delay_mean}m | flips/day={flips/max(len(rows)/288,1):.2f} | state={sum(state)/len(state):.1%} | trades deteriorating/normal={len(bad)}/{len(good)} | net/trade deteriorating/normal={bad_mean}/{good_mean} | $15 avoided/sacrificed/net/ratio=${loss:.3f}/${sacrificed:.3f}/${loss-sacrificed:.3f}/{loss/sacrificed if sacrificed else 0:.2f} | $10 avoided/sacrificed/net/ratio=${loss10:.3f}/${sacrificed10:.3f}/${loss10-sacrificed10:.3f}/{loss10/sacrificed10 if sacrificed10 else 0:.2f} | N={len(tagged)}')
def _weekly(name,rows,state,trades,cap):
 groups=defaultdict(list)
 for item in trades: groups[item[0].isocalendar()[:2]].append(item)
 for week,items in sorted(groups.items()):
  selected=[(row,value) for row,value in zip(rows,state) if datetime.fromtimestamp(row['t']/1000,timezone.utc).isocalendar()[:2]==week]
  local_rows,local_state=zip(*selected) if selected else ([],[])
  print(f'{name} | {week[0]}-W{week[1]:02d}'); _report(name,'STABLE',list(local_rows),list(local_state),[],items,cap)
def _audit_mapping(name,rows,state,trades):
 mapped=[]
 for opened,_,_,_ in trades:
  options=[i for i,row in enumerate(rows) if row['t']<=int(opened.timestamp()*1000)]
  if options:
   i=options[-1]; mapped.append((state[i],(opened.timestamp()*1000-rows[i]['t'])/60000))
 print(f'MAPPING AUDIT | {name} | last closed 5m <= opened_at | trades={len(trades)} | deteriorating={sum(x[0] for x in mapped)} | normal={sum(not x[0] for x in mapped)} | candle-age min/median/max={min(x[1] for x in mapped):.2f}/{statistics.median(x[1] for x in mapped):.2f}/{max(x[1] for x in mapped):.2f}m')
def _dt(v):return datetime.fromisoformat(v.replace('Z','+00:00')).astimezone(timezone.utc)
def _dt0(v):
 try:return _dt(str(v))
 except:return None
def _fmt(v):return v.astimezone(BRASILIA_TZ).strftime('%d/%m/%Y %H:%M BRT')
def _mean(v):return f'{statistics.fmean(v):.1f}' if v else 'n/a'
def _num(v):return f'{statistics.median(v):.1f}' if v else 'n/a'
if __name__=='__main__':main()
