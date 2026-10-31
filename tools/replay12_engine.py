"""Isolated runtime-rule adapters for preregistered OHLC scenarios. No real clients/state constructors."""
import sys,json,math,bisect,collections,statistics,argparse,time,hashlib
from pathlib import Path
from copy import deepcopy
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_12bracos_20261021';sys.path.insert(0,str(S/'frozen'))
from src.monitor.entry_engine import EntrySignal
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow,CircuitBreakerPosition
from src.monitor.forward_experiment_shadows import FastDropEmaShadow,PolicyShadow,EmaMacdHist1mShadow,ExperimentalRiskShadow,ElasticPosition
from src.monitor.trail_activation_gap_shadow import TrailActivationGapShadow
from src.monitor.cb_replay_clock import CBReplayClock
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.trade_ledger import TradeLedger
M=60000
ARMS=['REAL_A','BE_OFF_CB_SHADOW','BE_OFF_CB_ACT20_GAP5_SHADOW','BE_OFF_CB_ACT10_GAP13_SHADOW','BE_OFF_CB_FAST_DROP_EMA_SHADOW','HS_BULL_ELASTIC_SHADOW','HS_BEAR_CLUSTER_EXIT_SHADOW','CB_EXIT_ALL_SHADOW','BE_OFF_CB_MACD_BU_MINUS_SHADOW','BE_OFF_CB_EMA_MACD_SHADOW','EMA_MACD_HIST_1M_SHADOW','DMI15_TRAJECTORY_CONTEXT_SHADOW']
COSTS={'OPTIMISTIC':1.,'BASE':3.5,'ADVERSE':10.}
def iso(t):return datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
def ms(t):return int(datetime.fromisoformat(t.replace('Z','+00:00')).timestamp()*1000)
def dump(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
class Bar:
 __slots__=('t','o','h','l','c')
 def __init__(self,r):self.t=r['open_time_ms'];self.o=r['open'];self.h=r['high'];self.l=r['low'];self.c=r['close']
 def __getitem__(self,key):
  if key=='open_time_ms':return self.t
  if key=='close_time_ms':return self.t+59999
  return {'open':self.o,'high':self.h,'low':self.l,'close':self.c}[key]

class Log:
 def __init__(self,owner=None):self.owner=owner
 def decision(self,e):
  if self.owner and e.get('event')=='HS_INTELLIGENCE_EVALUATION':self.owner.events.append({k:e.get(k) for k in ['event','ts','intelligence','decision','pair_id','reasons']})
 def trade(self,*a,**k):pass
 def system(self,*a,**k):pass
class Client(PhantomExecutionClient):
 def __init__(self,bps):super().__init__();self.bps=bps
 def market_sell(self,symbol,quantity,client_order_id):
  p=self._price*(1-self.bps/10000);return {'status':'FILLED','executedQty':str(quantity),'cummulativeQuoteQty':str(quantity*p),'fills':[{'price':str(p),'qty':str(quantity),'commission':'0'}],'phantom':True}
class RealPosition(BotFullExitPosition):
 def on_tick(self,price,market_ts=None):self._cb_market_ts=market_ts;return super().on_tick(price,market_ts)
 def _close_at_market(self,price,reason,ts,trigger_reference):return super()._close_at_market(price,reason,self._cb_market_ts or ts,trigger_reference)
class Memory:
 def _save_state(self):pass
 def _record_market_point(self,*a):return False
 def _event_at(self,moment,event,**fields):self._event(event,**fields)
 def _event(self,event,**fields):
  if event in ['FAST_DROP','HS_ELASTIC_STARTED','HS_ELASTIC_EXIT_CONTEXT_LOST','HS_ELASTIC_ENDED_RECOVERED','HS_BEAR_CLUSTER_TRIGGERED','CB_EXIT_ALL_TRIGGERED','TRAIL_ACTIVATED','TRAIL_FIRST_DOMINANCE','CIRCUIT_BREAKER_TRIGGERED','CIRCUIT_BREAKER_RELEASED']:
   self.events.append({'event':event,'at':self.now,'pair_id':fields.get('pair_id'),'source':fields.get('source_candle_open_time'),'victim_pair_ids':fields.get('victim_pair_ids')})
 def _append_closed_record(self,r):
  self.closed_records.append(r);self._just_closed.append(self.by_pair[r['pair_id']])
 def _update_closed_record(self,r,fields):r.update(fields)
 def _block(self,event,signal,bucket,**kwargs):self.last_block=event;return False
 def _open(self,signal,bucket):
  price=self.market_price*(1+self.bps/10000);notional=float(self.config['capital']['operational_balance_usdt'])*float(self.config['capital']['trade_size_pct'])/100
  cls=self._position_type() if self.is_cb else RealPosition
  p=cls(pair_id=self.arm+'-'+str(signal.source_candle_open_time),symbol='SOLUSDT',entry_price=price,quantity=notional/price,entry_order={'research':True},open_ts=signal.ts,config=self._exit_config(),client=Client(self.bps),logger=self.logger,entry_atr=signal.entry_atr,atr_timeframe=signal.atr_timeframe,atr_period=signal.atr_period,source_candle_open_time=signal.source_candle_open_time,position_notional_usdt=notional,no_progress_enabled=False,no_progress_tolerance_seconds=None,no_progress_tolerance_source='DISABLED')
  p.phantom=True;p.shadow_kind=None if self.arm=='REAL_A' else self.arm;p._research_open=self.now;p.market_context_entry=self.latest_market_context;p._research_regime=self.entry_regime
  self.positions.append(p);self.all_positions.append(p);self.by_pair[p.pair_id]=p;self.entries_by_bucket[bucket]=self.entries_by_bucket.get(bucket,0)+1;self.max_simultaneous_positions=max(self.max_simultaneous_positions,len(self.open_positions))
 def _advance_clock(self,moment,price,from_signal=False):
  if not self.is_cb:return
  # Exact runtime clock/detector; audit-only price history has been removed.
  return CircuitBreakerShadow._advance_clock(self,moment,price,from_signal=from_signal)
 def _close_and_record(self,p,price,observed_at,reason,reference):
  # Closed-5m context decisions become available at boundary, never fill in the past.
  if observed_at and ms(observed_at)<self.now:observed_at=iso(self.now);price=self.market_price
  p.client.set_price(price)
  return ExperimentalRiskShadow._close_and_record(self,p,price,observed_at,reason,reference)
class Core(Memory,CircuitBreakerShadow):
 def _process_tick(self,price,observed_at):
  self._event_moment=datetime.fromisoformat(observed_at);self._advance_clock(self._event_moment,price)
  for p in list(self.open_positions):
   p.client.set_price(price);event=p.on_tick(price,market_ts=observed_at)
   if event and p.status=='CLOSED':
    p.market_context_exit=self.latest_market_context;r=self.ledger._record(p,self.config,'CIRCUIT_BREAKER_SHADOW');self._append_closed_record(r);stamp=ms(observed_at);self.pending_closes.append({'boundary':stamp//M*M+M,'net':float(r['net_pnl_pct'])*p.position_notional_usdt/100,'pair_id':p.pair_id})
  self.positions=[p for p in self.positions if p.status=='OPEN']
class CB(Core):pass
class Fast(FastDropEmaShadow,Core):pass
class Policy(PolicyShadow,Core):pass
class Hist(EmaMacdHist1mShadow,Core):pass
class Trail(TrailActivationGapShadow,Core):pass
class Risk(ExperimentalRiskShadow,Core):
 def _close_and_record(self,p,price,observed_at,reason,reference):
  return Memory._close_and_record(self,p,price,observed_at,reason,reference)
class Real(Memory,RealAContextShadow):
 def tick(self,price,observed_at):
  for p in list(self.open_positions):
   p.client.set_price(price);p.on_tick(price,observed_at)
   if p.status=='CLOSED':self._just_closed.append(p)
  self.positions=[p for p in self.positions if p.status=='OPEN']
def make(arm,cfg,bps,start):
 cls=Real if arm in ['REAL_A','DMI15_TRAJECTORY_CONTEXT_SHADOW'] else Fast if arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW' else Hist if arm=='EMA_MACD_HIST_1M_SHADOW' else Policy if arm in ['BE_OFF_CB_MACD_BU_MINUS_SHADOW','BE_OFF_CB_EMA_MACD_SHADOW'] else Trail if 'ACT20' in arm or 'ACT10' in arm else Risk if arm in ['HS_BULL_ELASTIC_SHADOW','HS_BEAR_CLUSTER_EXIT_SHADOW','CB_EXIT_ALL_SHADOW'] else CB
 o=object.__new__(cls);o.is_cb=cls!=Real;o.arm=o.shadow_kind=o.strategy=arm;o.pair_prefix=arm;o.config=cfg;o.settings={**cfg['instrumentation'].get('be_off_cb_shadow',{}),**cfg['instrumentation'].get(arm.lower(),{})};o.bps=bps;o.enabled=o.accept_new_entries=True;o._be_off=o.is_cb;o.policy='MACD_BU_MINUS' if arm=='BE_OFF_CB_MACD_BU_MINUS_SHADOW' else 'EMA_MACD';o.experiment={'HS_BULL_ELASTIC_SHADOW':'HS_BULL_ELASTIC','HS_BEAR_CLUSTER_EXIT_SHADOW':'HS_BEAR_CLUSTER_EXIT','CB_EXIT_ALL_SHADOW':'CB_EXIT_ALL'}.get(arm)
 o.positions=[];o.all_positions=[];o.by_pair={};o._just_closed=[];o.events=[];o.closed_records=[];o.entries_by_bucket={};o.max_simultaneous_positions=0;o.clock=CBReplayClock(100,100,100);o.last_signal_source=o.last_input_ms=None;o.circuit_breaker_active=False;o.circuit_breaker_started_at=o.circuit_breaker_until=None;o.crises_triggered=0;o.blocked_circuit_breaker=o.blocked_capacity=o.blocked_same_5m=o.blocked_spacing=o.blocked_context=o.blocked_context_unavailable=0;o.latest_market_context={};o._exit_context_history=[];o.tactical_candles=[];o.admission_audit={};o.minute_closes={};o.context_history=[];o.pending_closes=[];o.market_points=[];o.trigger_price=None;o.cohort_started_at=iso(start);o._event_moment=None;o.logger=Log(o);o.telemetry=None;o.ledger=object.__new__(TradeLedger);o.last_block=None;o.now=start;o.market_price=0;o.entry_regime={}
 return o

def tick(o,price,t):
 o.now=t;o.market_price=price
 prior={p.pair_id:(p.profit_lock_step,p.trailing_active,p.be_armed_at) for p in o.open_positions}
 if o.is_cb:o._process_tick(price,iso(t))
 else:o.tick(price,iso(t))
 for p in o.open_positions:
  state=(p.profit_lock_step,p.trailing_active,p.be_armed_at)
  if state!=prior.get(p.pair_id):o.events.append({'event':'EXIT_STATE_ACTIVATION','at':t,'pair_id':p.pair_id,'profit_lock_step':p.profit_lock_step,'trailing_active':p.trailing_active,'be_armed_at':p.be_armed_at})

def segment(o,left,right,t0,t1):
 # Runtime is sampled at global first crossing, so collective policies see the same market event.
 if right<left:
  previous=left
  while True:
   levels=[]
   for p in o.open_positions:
    candidates=[p.effective_stop]
    if o.arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW' and not p.fast_drop_evaluated:candidates.append(p.entry_price*.995)
    if isinstance(p,ElasticPosition) and not p.hs_elastic:candidates.append(p._elastic_hard_stop_price)
    levels.extend(float(v) for v in candidates if v is not None and right<=v<previous)
   if not levels:break
   value=max(levels);when=round(t0+(left-value)/(left-right)*(t1-t0));tick(o,value,when);previous=value
  if right!=previous:tick(o,right,t1)
 else:tick(o,right,t1)

def record(p,end,last,bps,fees):
 closed=p.status=='CLOSED';exit=float(p.exit_price) if closed else last*(1-bps/10000);net=(exit/p.entry_price-1-fees/100)*p.position_notional_usdt
 return {'source':p.source_candle_open_time,'pair_id':p.pair_id,'open_ms':p._research_open,'close_ms':ms(p.close_ts) if closed else None,'entry':p.entry_price,'exit':exit if closed else None,'mark':last if not closed else None,'net':net,'gross':(exit-p.entry_price)*p.quantity,'fee_est':p.position_notional_usdt*fees/100,'reason':str(p.exit_reason) if closed else 'OPEN','notional':p.position_notional_usdt,'quantity':p.quantity,'atr':p.entry_atr,'mare':p._research_regime['mare'],'onda':p._research_regime['onda'],'duration_minutes':((ms(p.close_ts) if closed else end)-p._research_open)/M,'fast_evaluated':getattr(p,'fast_drop_evaluated',None),'hs_elastic_started_at':getattr(p,'hs_elastic_started_at',None),'PL_economic_floor_active':p._active_profit_lock_economic_floor() is not None}
def run(arm,cfg,minutes,signals,contexts,regimes,scenario,path,start,end):
 bps=COSTS[scenario];o=make(arm,cfg,bps,start);fees=2*cfg['fees']['taker_fee_pct'];smap={r['at']:r for r in signals};ct=sorted(map(int,contexts));ci=-1;admissions=[];curve=[];daily={};lastcontext=None;sourcebars={c['open_time_ms']:c for c in minutes};last=None;peak=dd=0;last_equity=0;closed_seen=set();realized=0
 for i,c in enumerate(minutes):
  at=c['open_time_ms']
  if at<start:continue
  if c['close_time_ms']+1>end:break
  o.now=at;o.market_price=c['open']
  while ci+1<len(ct) and ct[ci+1]<=at:ci+=1
  co=contexts[str(ct[ci])] if ci>=0 else {};snap={'tf_5m':co,'captured_at':iso(at)}
  if co.get('latest_closed_at_ms')!=lastcontext:
   o.on_closed_5m(snap);lastcontext=co.get('latest_closed_at_ms')
  o.latest_market_context=snap
  if o.is_cb:
   prior=o.circuit_breaker_active;o._advance_clock(datetime.fromtimestamp(at/1000,timezone.utc),c['open'])
   if arm=='CB_EXIT_ALL_SHADOW' and not prior and o.circuit_breaker_active:
    victims=list(o.open_positions);o._event('CB_EXIT_ALL_TRIGGERED',victim_pair_ids=[p.pair_id for p in victims])
    for p in victims:p.client.set_price(c['open']);o._close_and_record(p,c['open'],iso(at),'CIRCUIT_BREAKER_EXIT_ALL',c['open'])
    o.positions=[p for p in o.positions if p.status=='OPEN']
  # Only already closed 1m candles are supplied to HIST and FAST.
  o.tactical_candles=[{'open_time':x['open_time_ms'],'close_time':x['close_time_ms'],'open':x['open'],'close':x['close']} for t in (at-2*M,at-M) if (x:=sourcebars.get(t)) is not None]
  if arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW':o.minute_closes={t+M:sourcebars[t]['close'] for t in range(at-10*M,at,M) if t in sourcebars}
  if at in smap:
   sig=EntrySignal(**smap[at]['signal']);o.last_block=None;o.entry_regime=regimes[str(at)]
   admitted=o._process_signal(sig) if o.is_cb else o.on_signal(sig)
   admissions.append({'source':sig.source_candle_open_time,'at':at,'accepted':bool(admitted),'reason':'ADMITTED' if admitted else o.last_block or 'DUPLICATE_OR_DISABLED',**o.entry_regime})
  points=[c['open'],c['high'],c['low'],c['close']] if path=='HIGH_FIRST' else [c['open'],c['low'],c['high'],c['close']];times=[at,at+20000,at+40000,at+59999]
  previous=None;previous_time=None
  for value,stamp in zip(points,times):
   if previous is None:tick(o,value,stamp)
   elif value!=previous:segment(o,previous,value,previous_time,stamp)
   previous=value;previous_time=stamp
  # Incrementally account for closed records (no synthetic liquidation of open positions).
  for p in o._just_closed:
   assert p.pair_id not in closed_seen
   realized+=record(p,end,c['close'],bps,fees)['net'];closed_seen.add(p.pair_id)
  o._just_closed=[]
  unreal=sum(((c['close']*(1-bps/10000)/p.entry_price-1)-fees/100)*p.position_notional_usdt for p in o.open_positions);equity=realized+unreal;peak=max(peak,equity);dd=max(dd,peak-equity);last_equity=equity;last=c['close'];day=datetime.fromtimestamp(c['close_time_ms']/1000,timezone.utc).astimezone(__import__('datetime').timezone(__import__('datetime').timedelta(hours=-3))).date().isoformat();daily[day]=equity
  # A compact minute MTM series supports equity DD and temporal block analyses.
  curve.append(equity)
  if len(curve)%50000==0:print('progress',arm,scenario,path,len(curve),'admitted',len(o.all_positions),flush=True)
 records=[record(p,end,last,bps,fees) for p in o.all_positions];prev=0;contributions={}
 for day,eq in daily.items():contributions[day]=eq-prev;prev=eq
 assert len(records)==sum(a['accepted'] for a in admissions)
 assert len({r['source'] for r in records})==len(records)
 return {'arm':arm,'scenario':scenario,'path':path,'start_ms':start,'end_ms':end,'fidelity':'IDEALIZED_OHLC_NOT_RUNTIME_EQUIVALENT','records':records,'admissions':admissions,'events':o.events,'daily_contributions':contributions,'daily_equity':daily,'equity_minute':curve,'stats':{'N':len(records),'closed':sum(r['close_ms'] is not None for r in records),'open':sum(r['close_ms'] is None for r in records),'net_realized':sum(r['net'] for r in records if r['close_ms'] is not None),'net_open_mtm':sum(r['net'] for r in records if r['close_ms'] is None),'net_total':last_equity,'DD_minute_mtm':dd,'capital_nominal':100,'max_positions':o.max_simultaneous_positions,'max_committed_usdt':o.max_simultaneous_positions*20,'notional_hours':sum(r['notional']*r['duration_minutes']/60 for r in records),'crises':o.clock.crises if o.is_cb else None}}

def checks(cfg,start):
 # Direct, outcome-independent tests of the adapter's mechanics.
 def opened(arm='BE_OFF_CB_SHADOW',price=100,context='SHO'):
  o=make(arm,cfg,0,start);o.market_price=price;o.latest_market_context={'tf_5m':{'ema_context':context,'latest_closed_at_ms':start-1}};o.entry_regime={'mare':'BEAR','onda':'COMP'};sig=EntrySignal('SOLUSDT',price,iso(start),start-M,.2,'1m',14);assert (o._process_signal(sig) if o.is_cb else o.on_signal(sig));return o,o.positions[0]
 o,p=opened();segment(o,100,97,start,start+40000);assert p.status=='CLOSED' and p.exit_reason=='HARD_STOP' and abs(p.exit_price-98.5)<1e-9
 o,p=opened();tick(o,97,start);assert p.status=='CLOSED' and p.exit_price==97
 o,p=opened('BE_OFF_CB_FAST_DROP_EMA_SHADOW');o.minute_closes={start-4*M:100.1};tick(o,99.5,start+1);assert p.status=='CLOSED' and p.exit_reason=='FAST_DROP'
 o,p=opened('BE_OFF_CB_FAST_DROP_EMA_SHADOW',context='LON');o.minute_closes={start-4*M:100.1};tick(o,99.5,start+1);assert p.fast_drop_evaluated and p.status=='OPEN';o.latest_market_context['tf_5m']['ema_context']='SHO';tick(o,99.4,start+2);assert p.status=='OPEN'
 o,p=opened('BE_OFF_CB_FAST_DROP_EMA_SHADOW');p.hard_stop_price=99.6;p._refresh_effective_stop();o.minute_closes={start-4*M:100.1};segment(o,100,99.4,start,start+40000);assert p.exit_reason=='HARD_STOP'
 o,p=opened('REAL_A');assert p._active_profit_lock_economic_floor() is not None
 o,p=opened('DMI15_TRAJECTORY_CONTEXT_SHADOW');assert p._active_profit_lock_economic_floor() is None
 o,p=opened('HS_BULL_ELASTIC_SHADOW',context='LON');tick(o,98.5,start+1);assert p.hs_elastic and p.status=='OPEN';o.now=start+5*M;o.market_price=96;o.on_closed_5m({'tf_5m':{'ema_context':'SHO','close':98,'latest_closed_at_ms':start+5*M-1},'captured_at':iso(start+5*M)});assert p.status=='CLOSED' and math.isclose(p.exit_price,96,abs_tol=1e-10)
 clock=CBReplayClock();assert not clock.minute(start,[]);events=clock.minute(start+M,[(start+M,-1),(start+M,-1)]);assert clock.paused and any(e['event']=='CIRCUIT_BREAKER_TRIGGERED' for e in events)
 for arm in ARMS:
  o=make(arm,cfg,0,start);assert len(o.positions)==0 and not hasattr(o,'_history_store')
 # Compare lightweight core with the original runtime tick method on identical states.
 a,pa=opened();b,pb=opened()
 for i,price in enumerate([100,100.6,101.5,101.2,100.4,98]):
  t=start+i*1000;tick(a,price,t);b.now=t;b.market_price=price;CircuitBreakerShadow._process_tick(b,price,iso(t))
  assert [(p.status,p.exit_reason,p.exit_price,p.effective_stop) for p in a.all_positions]==[(p.status,p.exit_reason,p.exit_price,p.effective_stop) for p in b.all_positions]
  assert a.pending_closes==b.pending_closes
 for name in ['REAL_A','BE_OFF_CB_SHADOW']:
  o=make(name,cfg,0,start);o.latest_market_context={'tf_5m':{}};o.entry_regime={'mare':'BULL','onda':'BULL'}
  allowed=[]
  for i in range(6):
   o.now=start+i*5*M;o.market_price=100+i;sig=EntrySignal('SOLUSDT',100+i,iso(o.now),o.now-M,.2,'1m',14);allowed.append(o._process_signal(sig) if o.is_cb else o.on_signal(sig))
  assert allowed==[True]*5+[False] and len(o.open_positions)==5
 o,p=opened('HS_BEAR_CLUSTER_EXIT_SHADOW',context='SHO');o.now=start+5*M;o.market_price=99;sig=EntrySignal('SOLUSDT',99,iso(o.now),o.now-M,.2,'1m',14);assert o._process_signal(sig);q=o.positions[-1];tick(o,98.5,o.now+1);assert p.exit_reason=='HARD_STOP' and q.exit_reason=='HS_BEAR_CLUSTER_EXIT'
 o,p=opened('CB_EXIT_ALL_SHADOW');o.pending_closes=[{'boundary':start+M,'net':-1,'pair_id':'synthetic1'},{'boundary':start+M,'net':-1,'pair_id':'synthetic2'}];tick(o,99.9,start+M);assert p.exit_reason=='CIRCUIT_BREAKER_EXIT_ALL' and o.clock.crises==1
 dump(S/'ENGINE_CHECKS.json',{'status':'PASS','checks':['continuous first stop touch','gap observed fill','FAST first eligible trigger','one-shot consumed','normal stop precedence','REAL/DMI actual PL floor difference','CB next-boundary detector','12 isolated registries without persistence stores','lightweight core agrees with original runtime ticks','nominal capacity five positions','HS elastic context-loss executes observable gap open','HS bear shared-event negative-neighbor liquidation','CB exit all on exact crisis transition'],'economic_results_consulted':False})

def main():
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['checks','run']);p.add_argument('--scenario',choices=list(COSTS),default='BASE');p.add_argument('--path',choices=['HIGH_FIRST','LOW_FIRST'],default='HIGH_FIRST');p.add_argument('--arm',choices=ARMS);args=p.parse_args();cfg=json.loads((S/'config.json').read_bytes());man=json.loads((S/'market_manifest.json').read_bytes());checks(cfg,man['start_ms'])
 if args.stage=='checks':print('ENGINE_CHECKS_PASS');return
 signals=json.loads((S/'signals.json').read_bytes());contexts=json.loads((S/'contexts_runtime_projection.json').read_bytes());regimes=json.loads((S/'regimes.json').read_bytes());minutes=[Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()]
 for arm in ([args.arm] if args.arm else ARMS):
  target=S/'runs'/(arm+'_'+args.scenario+'_'+args.path+'.json')
  if target.exists():print('EXISTS',target.name,flush=True);continue
  print('RUN',arm,args.scenario,args.path,flush=True);r=run(arm,cfg,minutes,signals[arm if arm=='DMI15_TRAJECTORY_CONTEXT_SHADOW' else 'REAL_A'],contexts,regimes,args.scenario,args.path,man['start_ms'],man['end_ms']);r['adapter_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();r['preregister_sha256']=man['preregister_sha256'];versions=S/'adapter_versions';versions.mkdir(exist_ok=True);version=versions/(r['adapter_sha256']+'.py');
  if not version.exists():version.write_bytes(Path(__file__).read_bytes())
  dump(target,r);print('DONE',arm,'N',len(r['records']),flush=True)
if __name__=='__main__':main()
