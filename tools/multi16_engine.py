"""Offline combined reducer convention authorized by Ernesto; never runtime wiring."""
from pathlib import Path
from copy import deepcopy
from datetime import datetime,timezone
import sys,json,inspect,argparse,hashlib
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import combo9_engine as c
e=c.e;S=ROOT/'data/analysis/replay_redutores_multi_20261025';OLD=c.S;MAN=json.loads((S/'market_manifest.json').read_bytes());M=e.M
CONTROL=c.CONTROL;FILTERS=c.FILTERS;REDUCERS=c.REDUCERS
BASES=[CONTROL,*FILTERS];LABELS=['BE','MACD','EMA','HIST'];SETS={'A':('FAST','CLUSTER','CB'),'B':('FAST','CLUSTER'),'C':('FAST','CB'),'D':('CLUSTER','CB')}
MULTI={f'M{1+i*4+j:02d}_{label}_{v}':(base,flags) for i,(base,label) in enumerate(zip(BASES,LABELS)) for j,(v,flags) in enumerate(SETS.items())};ARMS=[*c.ARMS,*MULTI]
REASONS={'FAST_DROP':'FAST','HS_BEAR_CLUSTER_EXIT':'CLUSTER','CIRCUIT_BREAKER_EXIT_ALL':'CB'}

class CombinedCore(e.Core):
 _close_and_record=e.Memory._close_and_record
 def _advance_clock(self,moment,price,from_signal=False):
  boundary=int(moment.timestamp()*1000)//M*M
  if self.clock.last_boundary is None or boundary>self.clock.last_boundary:self._clock_before_advance=deepcopy(self.clock)
  return e.Memory._advance_clock(self,moment,price,from_signal=from_signal)
 def _observe_cb_minute(self,boundary,due,events):
  clusters=[x for x in due if self.by_pair.get(x['pair_id']) is not None and self.by_pair[x['pair_id']].exit_reason=='HS_BEAR_CLUSTER_EXIT']
  if not any(x['event']=='CIRCUIT_BREAKER_TRIGGERED' for x in events):return
  before=deepcopy(self._clock_before_advance)
  if before.last_boundary is None:before.last_boundary=boundary-M
  assert before.last_boundary==boundary-M
  no_cluster=before.minute(boundary,[(boundary,x['net']) for x in due if x not in clusters])
  needed=bool(clusters) and not any(x['event']=='CIRCUIT_BREAKER_TRIGGERED' for x in no_cluster)
  times=[e.ms(self.by_pair[x['pair_id']].close_ts) for x in clusters]
  self._cb_edge={'boundary':boundary,'activation_eligible':not self._clock_before_advance.paused,'cluster_pair_ids':[x['pair_id'] for x in clusters],'cluster_closes_N':len(clusters),'cluster_net':sum(x['net'] for x in clusters),'cluster_necessary_for_this_boundary':needed,'classification':'NECESSARIA_LOCAL' if needed else 'CONCOMITANTE' if clusters else 'SEM_CLUSTER_NO_BOUNDARY','cluster_close_times':times,'cluster_event_indices':[self._close_event_index.get(x['pair_id']) for x in clusters]}
  self.cb_edges.append(deepcopy(self._cb_edge))
 def liquidate_cb(self,price,observed_at):
  edge=self._cb_edge
  if 'CB' not in self.flags or not edge or edge.get('consumed') or not edge['activation_eligible']:return
  if getattr(self,'_event_stamp',None)!=e.ms(observed_at):self.eligibility(price,observed_at)
  edge['consumed']=True;victims=list(self.open_positions)
  self._event('CB_EXIT_ALL_TRIGGERED',victim_pair_ids=[p.pair_id for p in victims])
  for p in victims:
   self._close_and_record(p,price,observed_at,'CIRCUIT_BREAKER_EXIT_ALL',price);self._close_event_index[p.pair_id]=self.event_index
  if edge['cluster_closes_N']:
   cascade={**deepcopy(edge),'liquidation_at':self.now,'victim_pair_ids':[p.pair_id for p in victims],'victims_N':len(victims),'event_index':self.event_index,'event_lags':[self.event_index-x for x in edge['cluster_event_indices'] if x is not None],'lags_ms':[self.now-x for x in edge['cluster_close_times']]};self.cascades.append(cascade)
  self.positions=[p for p in self.positions if p.status=='OPEN']
 def eligibility(self,price,observed_at):
  stamp=e.ms(observed_at)
  if getattr(self,'_event_stamp',None)!=stamp:self.event_index+=1
  self._event_stamp=stamp;context=self.latest_market_context.get('tf_5m',{}).get('ema_context');snapshot=[]
  from src.monitor.fast_drop_semantics import fast_drop_boundary,fast_drop_values,fast_drop_allowed,normal_stop_precedes_fast,loss_reached
  ref=self.minute_closes.get(fast_drop_boundary(stamp)-5*M)
  hs=any(p.effective_stop is not None and price<=p.effective_stop and p.stop_type=='hard_stop' for p in self.open_positions)
  for p in self.open_positions:
   target,velocity=fast_drop_values(p.entry_price,ref) if ref else (None,None)
   fast=bool('FAST' in self.flags and not p.fast_drop_evaluated and loss_reached(p.entry_price,price) and ref and not normal_stop_precedes_fast(p.effective_stop,target) and fast_drop_allowed(velocity,context))
   cluster=bool('CLUSTER' in self.flags and hs and context=='SHO' and p.pnl_pct(price)<0 and not (p.effective_stop is not None and price<=p.effective_stop))
   cb=bool('CB' in self.flags and self._cb_edge and self._cb_edge['activation_eligible'] and not self._cb_edge.get('consumed'))
   snapshot.append({'pair_id':p.pair_id,'at':stamp,'event_index':self.event_index,'FAST':fast,'CLUSTER_potential_before_normal':cluster,'CB_transition_pending':cb,'normal_stop_at_snapshot':p.effective_stop is not None and price<=p.effective_stop,'note':'pre-event predicates; CLUSTER requires actual surviving own HS after FAST/normal'})
  self._eligibility_snapshot={x['pair_id']:x for x in snapshot}
 def _append_closed_record(self,r):
  e.Memory._append_closed_record(self,r)
  if r.get('exit_reason') in REASONS or self.by_pair[r['pair_id']].exit_reason in REASONS:
   self.concurrent.append({**self._eligibility_snapshot.get(r['pair_id'],{'pair_id':r['pair_id'],'at':self.now,'note':'minute-boundary CB before admissions'}),'actual_reason':self.by_pair[r['pair_id']].exit_reason})
   self._close_event_index[r['pair_id']]=self.event_index
 def _process_tick(self,price,observed_at):
  prior=list(self.open_positions);e.Core._process_tick(self,price,observed_at)
  hard=[p for p in prior if p.status=='CLOSED' and p.exit_reason=='HARD_STOP']
  if 'CLUSTER' in self.flags and hard and self.latest_market_context.get('tf_5m',{}).get('ema_context')=='SHO':
   victims=[p for p in self.open_positions if p.pnl_pct(price)<0];self._event('HS_BEAR_CLUSTER_TRIGGERED',victim_pair_ids=[p.pair_id for p in victims])
   for p in victims:self._close_and_record(p,price,observed_at,'HS_BEAR_CLUSTER_EXIT',price)
   self.positions=[p for p in self.positions if p.status=='OPEN']
  self.liquidate_cb(price,observed_at)
class Multi(CombinedCore):
 def _process_tick(self,price,observed_at):self.eligibility(price,observed_at);return super()._process_tick(price,observed_at)
class MultiFast(e.FastDropEmaShadow,CombinedCore):
 def _process_tick(self,price,observed_at):self.eligibility(price,observed_at);return super()._process_tick(price,observed_at)
class PolicyMulti(e.PolicyShadow,Multi):pass
class HistMulti(e.EmaMacdHist1mShadow,Multi):pass
class PolicyMultiFast(e.PolicyShadow,MultiFast):pass
class HistMultiFast(e.EmaMacdHist1mShadow,MultiFast):pass

def make(arm,cfg,bps,start):
 if arm not in MULTI:return c.make(arm,cfg,bps,start)
 base,flags=MULTI[arm];o=c.original_make(base,cfg,bps,start);hist=base==FILTERS[-1];policy=base in FILTERS[:-1];fast='FAST' in flags
 o.__class__=(HistMultiFast if hist else PolicyMultiFast if policy else MultiFast) if fast else (HistMulti if hist else PolicyMulti if policy else Multi)
 o.arm=o.strategy=o.shadow_kind=o.pair_prefix=arm;o.flags=flags;o.experiment=None;o._cb_edge=None;o.cascades=[];o.cb_edges=[];o.concurrent=[];o.event_index=0;o._close_event_index={};o._eligibility_snapshot={};return o

source=c.run_source.replace("getattr(o,'experiment',None)=='CB_EXIT_ALL'","(getattr(o,'experiment',None)=='CB_EXIT_ALL' or 'CB' in getattr(o,'flags',()))")
oldblock="""victims=list(o.open_positions);o._event('CB_EXIT_ALL_TRIGGERED',victim_pair_ids=[p.pair_id for p in victims])
    for p in victims:p.client.set_price(c['open']);o._close_and_record(p,c['open'],iso(at),'CIRCUIT_BREAKER_EXIT_ALL',c['open'])
    o.positions=[p for p in o.positions if p.status=='OPEN']"""
newblock="""if hasattr(o,'flags'):o.liquidate_cb(c['open'],iso(at))
    else:
     victims=list(o.open_positions);o._event('CB_EXIT_ALL_TRIGGERED',victim_pair_ids=[p.pair_id for p in victims])
     for p in victims:p.client.set_price(c['open']);o._close_and_record(p,c['open'],iso(at),'CIRCUIT_BREAKER_EXIT_ALL',c['open'])
     o.positions=[p for p in o.positions if p.status=='OPEN']"""
assert oldblock in source;source=source.replace(oldblock,newblock)
source=source.replace("'records':records,'admissions'","'cascades':getattr(o,'cascades',[]),'cb_edges':getattr(o,'cb_edges',[]),'concurrent':getattr(o,'concurrent',[]),'records':records,'admissions'")
e.make=make;e.S=S;exec(compile(source,'<multi16_run>','exec'),e.__dict__)

def run(path,arm=None,scope='all',shard=0,shards=1):
 cfg=json.loads((S/'config.json').read_bytes());minutes=[e.Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()];signals=json.loads((S/'signals.json').read_bytes())['REAL_A'];contexts=json.loads((S/'contexts_runtime_projection.json').read_bytes());regimes=json.loads((S/'regimes.json').read_bytes())
 selected=[arm] if arm else ARMS[:16] if scope=='references' else ARMS[16:] if scope=='new' else ARMS
 for name in selected[shard::shards]:
  target=S/'runs'/(name+'_'+path+'.json')
  if target.exists():continue
  r=e.run(name,cfg,minutes,signals,contexts,regimes,'BASE',path,MAN['start_ms'],MAN['end_ms'])
  for q in r['records']:q['status']='OPEN' if q['close_ms'] is None else 'CLOSED';q['reducer']=REASONS.get(q['reason'])
  r['composer_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();r['preregister_sha256']=MAN['preregister_sha256'];e.dump(target,r);print('DONE',name,path,r['stats']['N'],flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--path',required=True);p.add_argument('--arm');p.add_argument('--scope',default='all');p.add_argument('--shard',type=int,default=0);p.add_argument('--shards',type=int,default=1);a=p.parse_args();run(a.path,a.arm,a.scope,a.shard,a.shards)
