"""Offline STACK_GUARD_2NEG composition, with mandatory disabled parity gate."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from types import MethodType
import sys,json,inspect,hashlib,bisect,math,argparse,subprocess,concurrent.futures
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import replay12_engine as e
S=ROOT/'data/analysis/replay_stack_guard_2neg_20261026'
OLD=ROOT/'data/analysis/replay_redutores_multi_20261025'
BASES={'BE_OFF_CB':'BE_OFF_CB_SHADOW','HIST_1M':'EMA_MACD_HIST_1M_SHADOW','MACD_BU_MINUS':'BE_OFF_CB_MACD_BU_MINUS_SHADOW','EMA_MACD':'BE_OFF_CB_EMA_MACD_SHADOW'}
PATHS=['LOW_FIRST','HIGH_FIRST']
ORIGINAL_MAKE=e.make;ORIGINAL_RUN=e.run
class GuardBlocked(Exception):pass

def process(o,sig,bars):
 row=bars[sig.source_candle_open_time]
 assert row['close_time_ms']<o.now
 positions=list(o.open_positions);price=row['close'];negative=sum(price<p.entry_price for p in positions)
 item={'at':o.now,'source':sig.source_candle_open_time,'signal_close':price,'execution_open':o.market_price,'open_N':len(positions),'negative_N':negative,'condition':len(positions)>=3 and negative>=2,'effective':False,'enabled':o.guard_enabled}
 o.guard_current=item
 try:result=o._process_signal(sig)
 except GuardBlocked:result=False
 item.update(accepted=bool(result),reason='ADMITTED' if result else o.last_block or 'DUPLICATE_OR_DISABLED')
 if item['condition']:
  item['positions']=[{'source':p.source_candle_open_time,'entry':p.entry_price,'negative':price<p.entry_price} for p in positions]
 o.stack_audit.append(item)
 return result

def guard_open(self,sig,bucket):
 if self.guard_enabled and self.guard_current['condition']:
  before=(len(self.all_positions),dict(self.entries_by_bucket),tuple(p.source_candle_open_time for p in self.open_positions))
  self.guard_current['effective']=True;self.last_block='ENTRY_BLOCKED_STACK_GUARD_2NEG'
  after=(len(self.all_positions),dict(self.entries_by_bucket),tuple(p.source_candle_open_time for p in self.open_positions))
  assert before==after
  raise GuardBlocked
 return self.original_open(sig,bucket)

def make(arm,cfg,bps,start):
 o=ORIGINAL_MAKE(arm,cfg,bps,start);o.guard_enabled=ENABLED;o.stack_audit=[];o.guard_current={};o.original_open=o._open;o._open=MethodType(guard_open,o)
 return o

ENABLED=False
source=inspect.getsource(ORIGINAL_RUN)
needle='admitted=o._process_signal(sig) if o.is_cb else o.on_signal(sig)'
assert source.count(needle)==1
source=source.replace(needle,'admitted=stack_process(o,sig,sourcebars)')
source=source.replace("'records':records,'admissions'","'stack_audit':o.stack_audit,'records':records,'admissions'")
ns=dict(e.__dict__);ns.update(make=make,stack_process=process)
exec(compile(source,'<stack_guard_run>','exec'),ns)
GUARD_RUN=ns['run']

def load():
 man=json.loads((S/'market_manifest.json').read_bytes());cfg=json.loads((S/'config.json').read_bytes())
 assert hashlib.sha256((S/'PREREGISTRO_V1.md').read_bytes()).hexdigest()==man['preregister_sha256']
 assert hashlib.sha256((ROOT/'tools/replay12_engine.py').read_bytes()).hexdigest()==man['base_adapter_sha256']
 return man,cfg,[e.Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()],json.loads((S/'signals.json').read_bytes())['REAL_A'],json.loads((S/'contexts_runtime_projection.json').read_bytes()),json.loads((S/'regimes.json').read_bytes())

def run_one(label,path,stage):
 global ENABLED
 man,cfg,minutes,signals,contexts,regimes=load();arm=BASES[label]
 args=(arm,cfg,minutes,signals,contexts,regimes,'BASE',path,man['start_ms'],man['end_ms'])
 if stage=='parity':
  base=ORIGINAL_RUN(*args)
  previous=json.loads((OLD/'runs'/(arm+'_'+path+'.json')).read_bytes());cut=previous['end_ms'];n=len(previous['equity_minute'])
  assert base['equity_minute'][:n]==previous['equity_minute'],(label,path,'BASE_EQUITY_PREFIX')
  assert [a for a in base['admissions'] if a['at']<cut]==previous['admissions'],(label,path,'BASE_ADMISSIONS_PREFIX')
  oldby={r['source']:r for r in previous['records'] if r['close_ms'] is not None}
  newby={r['source']:r for r in base['records']}
  for src,r in oldby.items():assert all(newby[src][k]==v for k,v in r.items() if k in newby[src]),(label,path,src,'BASE_CLOSED_PREFIX')
  ENABLED=False;off=GUARD_RUN(*args)
  for key in base:assert base[key]==off[key],(label,path,'DISABLED_PARITY',key)
  e.dump(S/'parity'/(label+'_'+path+'.json'),{'passed':True,'base':label,'path':path,'disabled_exact_keys':list(base),'prefix_minutes':n,'prefix_closed':len(oldby),'guard_truths_disabled':sum(x['condition'] for x in off['stack_audit'])})
  e.dump(S/'runs'/(label+'_'+path+'.json'),base)
  print('PARITY_PASS',label,path,flush=True)
 else:
  assert json.loads((S/'PARIDADE.json').read_bytes())['passed']
  ENABLED=True;result=GUARD_RUN(*args)
  result['arm']=label+'_STACK_GUARD_2NEG'
  e.dump(S/'runs'/(label+'_STACK_GUARD_2NEG_'+path+'.json'),result)
  print('GUARD_DONE',label,path,result['stats']['N'],flush=True)

def checks():
 from types import SimpleNamespace as P
 class Fake:
  def __init__(self,entries):
   self.open_positions=[P(entry_price=x,source_candle_open_time=i) for i,x in enumerate(entries)]
   self.stack_audit=[];self.now=60000;self.market_price=120;self.guard_enabled=True;self.last_block=None
  def _process_signal(self,sig):return not self.guard_current['condition']
 sig=P(source_candle_open_time=0);bars={0:{'close_time_ms':59999,'close':100}}
 cases=[([101,102],False),([101,100,99],False),([101,102,100],True),([100,100,100],False),([101,102,103,99],True)]
 for entries,truth in cases:
  o=Fake(entries);process(o,sig,bars);assert o.stack_audit[0]['condition']==truth
 # Actual frozen admission chain: guard rejection changes no reservation state.
 cfg=json.loads((S/'config.json').read_bytes());start=json.loads((S/'market_manifest.json').read_bytes())['start_ms']
 global ENABLED
 ENABLED=True;o=make(BASES['BE_OFF_CB'],cfg,0,start);o.now=start;o.entry_regime={'mare':'BULL','onda':'COMP'};o.latest_market_context={'tf_5m':{}}
 for i,price in enumerate([101.,102.,103.]):
  o.market_price=price;o.original_open(e.EntrySignal('SOLUSDT',price,e.iso(start),start-(i+2)*60000,.2,'1m',14),(start//300000-10-i)*300000)
 o.market_price=100;sig=e.EntrySignal('SOLUSDT',100,e.iso(start),start-60000,.2,'1m',14)
 before=(len(o.all_positions),dict(o.entries_by_bucket));assert not process(o,sig,{start-60000:{'close_time_ms':start-1,'close':99}})
 assert o.stack_audit[-1]['effective'] and before==(len(o.all_positions),dict(o.entries_by_bucket))
 # Next eligible source within same 5m bucket is still admitted after recovery.
 o.now=start+60000;o.market_price=105;sig=e.EntrySignal('SOLUSDT',105,e.iso(o.now),start,.2,'1m',14)
 assert process(o,sig,{start:{'close_time_ms':o.now-1,'close':105}})
 assert len(o.open_positions)==4
 # Conditions true but spacing rejects first: not an effective guard block.
 o.now+=60000;o.market_price=101;sig=e.EntrySignal('SOLUSDT',101,e.iso(o.now),start+60000,.2,'1m',14)
 assert not process(o,sig,{start+60000:{'close_time_ms':o.now-1,'close':99}})
 assert not o.stack_audit[-1]['effective']
 e.dump(S/'TESTES_SINTETICOS.json',{'passed':True,'checks':['2 positions do not block','3 positions 1negative do not block','3 positions 2negative block','equality not negative','4positions supported','signal close distinct from execution open','real frozen admission rejects with no reservations','later eligible signal admitted','earlier spacing/bucket reason not attributed to guard']})

def worker(item):
 label,path,stage=item
 with (S/(stage+'_'+label+'_'+path+'.log')).open('w',encoding='utf-8') as f:
  r=subprocess.run([sys.executable,__file__,stage,'--base',label,'--path',path],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
 return {'base':label,'path':path,'stage':stage,'returncode':r.returncode}

def orchestrate():
 checks();executions=[]
 for stage in ['parity','guard']:
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
   results=list(pool.map(worker,[(label,path,stage) for path in PATHS for label in BASES]))
  executions+=results;e.dump(S/'EXECUCOES.json',executions)
  if any(r['returncode'] for r in results):
   e.dump(S/'INTERRUPCAO.json',{'stage':stage,'reason':'Failure; no subsequent stage authorized','results':results});raise RuntimeError(results)
  if stage=='parity':
   proofs=[json.loads(p.read_bytes()) for p in (S/'parity').glob('*.json')];assert len(proofs)==8 and all(x['passed'] for x in proofs)
   e.dump(S/'PARIDADE.json',{'passed':True,'checks':proofs,'economic_guard_runs_started':False})
   print('ALL_8_DISABLED_PARITIES_PASS',flush=True)
 print('ALL16_COMPLETE',flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['parity','guard','orchestrate','checks']);p.add_argument('--base',choices=list(BASES));p.add_argument('--path',choices=PATHS);a=p.parse_args()
 if a.stage=='orchestrate':orchestrate()
 elif a.stage=='checks':checks()
 else:run_one(a.base,a.path,a.stage)
