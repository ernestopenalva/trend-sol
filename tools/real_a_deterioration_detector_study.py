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
from src.trade_ledger import TradeLedger
from tools.ge_replay_study import load_ge_market_data
from tools.market_selection_study import BinancePublicClient

START='2026-05-29T00:00:00-03:00'; END='2026-09-01T00:00:00-03:00'

def main():
 p=argparse.ArgumentParser(description='READ-ONLY REAL_A deterioration detector study')
 p.add_argument('--since',default=START);p.add_argument('--until',default=END);p.add_argument('--capital',type=float,default=100);p.add_argument('--ledger',default=str(ROOT/'data/trades/trades_B.jsonl'));p.add_argument('--cache-dir',default=str(ROOT/'data/studies/real_a_deterioration/klines'));p.add_argument('--offline',action='store_true');p.add_argument('--market-data-url',default='https://api.binance.com');p.add_argument('--http-timeout-seconds',type=int,default=15);a=p.parse_args()
 start,end=_dt(a.since),_dt(a.until); sm,em=int(start.timestamp()*1000),int(end.timestamp()*1000)
 c=load_ge_market_data(BinancePublicClient(a.market_data_url,a.http_timeout_seconds),'SOLUSDT','5m',sm-200*300000,em,Path(a.cache_dir),a.offline)
 rows=_features(c,sm,em); trades=_trades(Path(a.ledger),start,end); t0=_t0(trades,a.capital,4); t0s=_t0(trades,a.capital,2)
 print('TREND-SOL | REAL_A deterioration detector study | READ-ONLY')
 print(f'Frozen window: {_fmt(start)} -> {_fmt(end)} | 5m closed candles={len(rows)} | REAL_A closed trades={len(trades)}')
 print('T0 primary (retrospective ground truth): first trade close whose NEXT 4h has >=2 closes and cumulative net <= -0.5% of capital. Sensitivity: same definition, NEXT 2h. T0 is detector-independent.')
 print(f'T0 episodes: primary={len(t0)} | sensitivity_2h={len(t0s)}')
 for name,raw in _candidates(rows).items():
  stable=_confirm(raw); _report(name,'RAW',rows,raw,t0,trades,a.capital); _report(name,'STABILIZED_2x5m',rows,stable,t0,trades,a.capital)
 print('\nECONOMIC SUBCOHORT: results above label each trade configuration; do not combine silent regimes. Uniform subset is largest signature among b_atr_v1.4 / HS=1.5 / NPE=false when available.')
 print('LIMITS: exploratory/in-sample; overlapping 5m observations are not independent; repricing preserves ledger entries and cannot model changed slots/admission.')

def _features(c,sm,em):
 closes=[x.close for x in c]; e=ema(closes,20); aa=atr([x.high for x in c],[x.low for x in c],closes,14);out=[]
 for i,x in enumerate(c):
  if not(sm<=x.boundary_ms<em) or i<20 or e[i] is None:continue
  above=sum(closes[j]>float(e[j]) for j in range(i-7,i+1) if e[j] is not None)/8
  out.append({'t':x.boundary_ms,'close':x.close,'slow_ge_down':closes[i]<closes[i-3],'ema_below':x.close<float(e[i]),'consistency_loss':above<.5,'local_dd':x.close/max(closes[i-11:i+1])<=.997,'atr_expand':aa[i] is not None and aa[i-3] is not None and float(aa[i])>float(aa[i-3])*1.15})
 return out
def _candidates(rows):return {'SLOW_GE_BASELINE':[x['slow_ge_down'] for x in rows],'PRICE_BELOW_EMA20':[x['ema_below'] for x in rows],'EMA20_CONSISTENCY_8':[x['consistency_loss'] for x in rows],'LOCAL_DD_12':[x['local_dd'] for x in rows],'ATR_EXPANSION':[x['atr_expand'] for x in rows]}
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
def _report(name,kind,rows,state,t0,trades,cap):
 starts=[rows[i]['t'] for i,v in enumerate(state) if v and (i==0 or not state[i-1])]; flips=sum(state[i]!=state[i-1] for i in range(1,len(state))); delays=[]
 for zero in t0:
  hit=next((x for x in starts if x>=int(zero.timestamp()*1000)),None)
  if hit:delays.append((hit-int(zero.timestamp()*1000))/60000)
 tagged=[]
 for o,_,p,x in trades:
  active=next((state[i] for i in range(len(rows)-1,-1,-1) if rows[i]['t']<=int(o.timestamp()*1000)),False);tagged.append((active,p,x))
 bad=[p for active,p,_ in tagged if active]; good=[p for active,p,_ in tagged if not active]; loss=sum(-p*.2 for a,p,_ in tagged if a and p<0); sacrificed=sum(p*.2*.5 for a,p,_ in tagged if a and p>0)
 delay_median = f'{statistics.median(delays):.1f}' if delays else 'n/a'; delay_mean = f'{statistics.fmean(delays):.1f}' if delays else 'n/a'
 bad_mean = f'{statistics.fmean(bad):+.3f}%' if bad else 'n/a'; good_mean = f'{statistics.fmean(good):+.3f}%' if good else 'n/a'
 print(f'{name} | {kind} | delay median/mean={delay_median}/{delay_mean}m | flips/day={flips/max(len(rows)/288,1):.2f} | state={sum(state)/len(state):.1%} | net/trade deteriorating/normal={bad_mean}/{good_mean} | sizing $15 avoided/sacrificed/net/ratio=${loss:.3f}/${sacrificed:.3f}/${loss-sacrificed:.3f}/{loss/sacrificed if sacrificed else 0:.2f} | N={len(tagged)}')
def _dt(v):return datetime.fromisoformat(v.replace('Z','+00:00')).astimezone(timezone.utc)
def _dt0(v):
 try:return _dt(str(v))
 except:return None
def _fmt(v):return v.astimezone(BRASILIA_TZ).strftime('%d/%m/%Y %H:%M BRT')
def _mean(v):return f'{statistics.fmean(v):.1f}' if v else 'n/a'
def _num(v):return f'{statistics.median(v):.1f}' if v else 'n/a'
if __name__=='__main__':main()
