"""Causal signal/context reconstruction and forward reconciliation; no economics."""
import sys,json,hashlib,collections,gzip,bisect
from pathlib import Path
from dataclasses import asdict
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_12bracos_20261021';sys.path.insert(0,str(S/'frozen'))
from src.monitor.entry_engine import EntryEngine,EntrySignal
from src.monitor.context_shadow import ContextGateEntryEngine
from src.monitor.context_predicates import passes_dmi15_trajectory
from src.monitor.market_context import MarketContextEngine
from src.monitor.price_structure import StructureBuffer,Hour,TIDE,WAVE
M=60000
class Logger:
 def decision(self,*a,**k):pass
 def trade(self,*a,**k):pass
 def system(self,*a,**k):pass
class Row:
 def __init__(self,r):self.__dict__.update(r)
 @property
 def boundary_ms(self):return self.close_time_ms+1
def load(tf):return [Row(json.loads(l)) for l in (S/'market'/(tf+'.jsonl')).open()]
def iso(t):return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
def payload(r):return {'k':{'t':r.open_time_ms,'T':r.close_time_ms,'o':str(r.open),'h':str(r.high),'l':str(r.low),'c':str(r.close),'v':str(r.quote_volume),'x':True}}
def dump(n,x):(S/n).write_text(json.dumps(x,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
def main():
 cfg=json.loads((S/'config.json').read_bytes());man=json.loads((S/'market_manifest.json').read_bytes());start,end=man['start_ms'],man['end_ms'];clock=[0];log=Logger();e=EntryEngine('SOLUSDT',cfg,log,monotonic_clock=lambda:clock[0]/1000);d=ContextGateEntryEngine('SOLUSDT',cfg,log,lambda engine,snap:passes_dmi15_trajectory((snap or {}).get('tf_5m',{})),lambda unavailable:None);d._monotonic_clock=lambda:clock[0]/1000;ctx=MarketContextEngine(e,cfg);bars={tf:load(tf) for tf in ['1m','5m','15m','1h']};idx={'5m':0,'15m':0};contexts={};signals={'REAL_A':[],'DMI15_TRAJECTORY_CONTEXT_SHADOW':[]};current={};diagnostics=collections.Counter()
 for i,c in enumerate(bars['1m']):
  at=c.boundary_ms;clock[0]=at
  for tf in ['15m','5m']:
   while idx[tf]<len(bars[tf]) and bars[tf][idx[tf]].boundary_ms<=at:
    b=bars[tf][idx[tf]];idx[tf]+=1
    for engine in [e,d]:engine.on_kline('solusdt@kline_'+tf,payload(b))
    if tf=='5m':
     snap=ctx._timeframe_snapshot(e._candles_for('5m'),'5m');snap['closed']=True;current={'tf_5m':snap,'captured_at':iso(at)};contexts[str(b.boundary_ms)]=snap
  d.set_context_snapshot(current)
  for arm,engine in [('REAL_A',e),('DMI15_TRAJECTORY_CONTEXT_SHADOW',d)]:
   sig=engine.on_kline('solusdt@kline_1m',payload(c))
   if sig is not None and start<=at<end:signals[arm].append(dict(at=at,signal={**asdict(sig),'ts':iso(at)}))
  if i%25000==0:print('reconstruct',i,'signals',len(signals['REAL_A']),len(signals['DMI15_TRAJECTORY_CONTEXT_SHADOW']),flush=True)
 dump('contexts.json',contexts);dump('signals.json',signals)
 buffers={p:StructureBuffer([Hour(c.open_time_ms,c.high,c.low,True,c.boundary_ms) for c in bars[tf]],rule=rule) for p,tf,rule in [('mare','1h',TIDE),('onda','15m',WAVE)]};reg={};timelines={}
 for pref,rule in [('mare',TIDE),('onda',WAVE)]:
  runs=[];previous=None;run=0
  for t in range(start//rule.interval_ms*rule.interval_ms,end,rule.interval_ms):
   snap=buffers[pref].snapshot(t,'IDEALIZED_PUBLIC');label=snap['label'] if snap.get('status') in ('OK','EQUAL_EXTREME') else 'UNAVAILABLE'
   if previous!=label:run+=1
   runs.append({'at':t,'label':label,'run':run});previous=label
  timelines[pref]=runs
 for t in sorted(set([r['at'] for rr in signals.values() for r in rr])):
  reg[str(t)]={}
  for pref in buffers:
   snap=buffers[pref].snapshot(t,'IDEALIZED_PUBLIC');reg[str(t)][pref]=snap['label'] if snap.get('status') in ('OK','EQUAL_EXTREME') else 'UNAVAILABLE'
 dump('regimes.json',reg);dump('regime_timeline.json',timelines)
 # Independent forward evidence, not treated as homologated economic result.
 old=ROOT/'data/analysis/mare_onda_forward_20261021';fw=json.loads(gzip.decompress((old/'fontes_economicas.json.gz').read_bytes()));ev=json.loads(gzip.decompress((old/'captura_sem_economia.json.gz').read_bytes()));cuts=sorted(map(int,contexts));sm={a:{r['signal']['source_candle_open_time']:r for r in rr} for a,rr in signals.items()};fidelity={};B=int(datetime.fromisoformat('2026-10-08T23:52:00-03:00').timestamp()*1000)
 for arm in ['REAL_A','BE_OFF_CB_SHADOW','BE_OFF_CB_ACT20_GAP5_SHADOW','BE_OFF_CB_ACT10_GAP13_SHADOW','BE_OFF_CB_FAST_DROP_EMA_SHADOW','HS_BULL_ELASTIC_SHADOW','HS_BEAR_CLUSTER_EXIT_SHADOW','CB_EXIT_ALL_SHADOW','BE_OFF_CB_MACD_BU_MINUS_SHADOW','BE_OFF_CB_EMA_MACD_SHADOW','EMA_MACD_HIST_1M_SHADOW','DMI15_TRAJECTORY_CONTEXT_SHADOW']:
  path='data/trades/trades_B.jsonl' if arm=='REAL_A' else 'data/trades/trades_'+arm.lower()+'.jsonl';rr=[json.loads(l) for l in fw['files'][path]['data'].splitlines()];rr=[r for r in rr if r.get('opened_at') and B<=int(datetime.fromisoformat(r['opened_at']).timestamp()*1000)<=end and (arm!='REAL_A' or (not r.get('phantom') and r.get('position_type')=='BOT_EXIT'))];sourcearm=arm if arm=='DMI15_TRAJECTORY_CONTEXT_SHADOW' else 'REAL_A';details=[]
  for r in rr:
   src=r.get('source_candle_open_time');t=src+M if isinstance(src,int) else None;found=sm[sourcearm].get(src);j=bisect.bisect_right(cuts,t)-1 if t else -1;co=contexts[str(cuts[j])] if j>=0 else {};ob=(r.get('market_context_entry') or {}).get('tf_5m',{});details.append({'pair_id':r['pair_id'],'source':src,'signal_found':found is not None,'ema_match':co.get('ema_context')==ob.get('ema_context'),'macd_match':co.get('macd_context')==ob.get('macd_context'),'observed_ema':ob.get('ema_context'),'ideal_ema':co.get('ema_context'),'observed_macd':ob.get('macd_context'),'ideal_macd':co.get('macd_context'),'entry_time':r['opened_at'],'ideal_signal_time':iso(t) if t else None,'entry_atr_match':found is not None and abs(float(found['signal']['entry_atr'])-float(r.get('entry_atr') or 0))<=1e-8})
  n=len(details);fidelity[arm]={'N_forward_closed':n,'signal_recall':sum(x['signal_found'] for x in details)/n if n else None,'ema_match_rate':sum(x['ema_match'] for x in details)/n if n else None,'macd_match_rate':sum(x['macd_match'] for x in details)/n if n else None,'atr_match_rate':sum(x['entry_atr_match'] for x in details)/n if n else None,'class':'IDEALIZED_NOT_RUNTIME_EQUIVALENT','details':details}
 dump('fidelity_signals.json',fidelity);dump('AUDITORIA_PRE_ECONOMIA.json',{'status':'PRE_ECONOMIC','market_gaps':man['internal_missing_1m'],'signals':{k:len(v) for k,v in signals.items()},'fidelity':{k:{x:y for x,y in v.items() if x!='details'} for k,v in fidelity.items()},'DMI_mechanical_difference':'DMI BotFullExitPosition shadow_kind disables PL economic floor; REAL_A and CB-position clones apply floor. Preserve implemented behavior; do not silently correct.','restrictions':'Admission/exit audit still required before interpreting economics; no exact runtime equivalence presumed.'});print(json.dumps({k:{x:y for x,y in v.items() if x!='details'} for k,v in fidelity.items()}),flush=True)
if __name__=='__main__':main()
