"""Isolated sizing-only replay with exact frozen MACD predicate and parity gates."""
from pathlib import Path
from types import MethodType
from datetime import datetime,timezone
import sys,json,inspect,textwrap,hashlib,argparse,subprocess,concurrent.futures
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import replay12_engine as e
from bu_half_prechecks import bu_predicate,viability
S=ROOT/'data/analysis/replay_bu_half_20261027';OLD=ROOT/'data/analysis/replay_stack_guard_2neg_20261026'
PATHS=['LOW_FIRST','HIGH_FIRST'];CONTROL='BE_OFF_CB_SHADOW';FILTER='BE_OFF_CB_MACD_BU_MINUS_SHADOW';MODE='OFF';UNIFORM=20.
ORIGINAL_RUN=e.run;ORIGINAL_MAKE=e.make
size_source=textwrap.dedent(inspect.getsource(e.Memory._open))
old="notional=float(self.config['capital']['operational_balance_usdt'])*float(self.config['capital']['trade_size_pct'])/100"
assert size_source.count(old)==1
size_source=size_source.replace(old,'notional=self.research_notional')
size_ns=dict(e.__dict__);exec(compile(size_source,'<bu_sized_open>','exec'),size_ns);SIZED_OPEN=size_ns['_open']

def wrapped_open(self,signal,bucket):
 n=10. if MODE=='BU_HALF' and self.current_bu else UNIFORM if MODE=='UNIFORME' else 20.
 self.research_notional=n
 if MODE in ['OFF','MACD_BU_MINUS']:result=self.original_open(signal,bucket)
 else:result=SIZED_OPEN(self,signal,bucket)
 self.current_audit['admitted_notional']=self.all_positions[-1].position_notional_usdt
 assert self.current_audit['admitted_notional']==n
 return result

def advance(self,moment,price,from_signal=False):
 result=self.original_advance(moment,price,from_signal=from_signal)
 if self.clock.last_boundary is not None and (not self.cb_trace or self.cb_trace[-1][0]!=self.clock.last_boundary):
  c=self.clock;self.cb_trace.append([c.last_boundary,c.equity,c.peak,c.pause_until,c.crises,c.was_true,c.paused])
 return result

def make(arm,cfg,bps,start):
 o=ORIGINAL_MAKE(arm,cfg,bps,start);o.sizing_audit=[];o.cb_trace=[];o.current_bu=False;o.current_audit={};o.research_notional=20.
 o.original_open=o._open;o._open=MethodType(wrapped_open,o);o.original_advance=o._advance_clock;o._advance_clock=MethodType(advance,o)
 return o

def process(o,sig):
 co=o._context_fields();isbu=bu_predicate(co);o.current_bu=isbu
 row={'at':o.now,'source':sig.source_candle_open_time,'macd_context':co.get('macd_context'),'is_bu_minus':isbu,'context_closed_at_ms':co.get('latest_closed_at_ms'),'admitted_notional':None}
 o.current_audit=row;result=o._process_signal(sig);row.update(accepted=bool(result),reason='ADMITTED' if result else o.last_block or 'DUPLICATE_OR_DISABLED');o.sizing_audit.append(row)
 return result

source=inspect.getsource(ORIGINAL_RUN);needle='admitted=o._process_signal(sig) if o.is_cb else o.on_signal(sig)';assert source.count(needle)==1
source=source.replace(needle,'admitted=bu_process(o,sig)').replace("'records':records,'admissions'","'sizing_audit':o.sizing_audit,'cb_trace':o.cb_trace,'records':records,'admissions'")
ns=dict(e.__dict__);ns.update(make=make,bu_process=process);exec(compile(source,'<bu_half_run>','exec'),ns);SIZING_RUN=ns['run']

def read(n):return json.loads((S/n).read_bytes())
def dump(n,x):e.dump(S/n,x)
def inputs():
 m=read('market_manifest.json');assert hashlib.sha256((S/'PREREGISTRO_V1.md').read_bytes()).hexdigest()==m['preregister_sha256'];assert hashlib.sha256((ROOT/'tools/replay12_engine.py').read_bytes()).hexdigest()==m['base_adapter_sha256']
 return m,read('config.json'),[e.Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()],read('signals.json')['REAL_A'],read('contexts_runtime_projection.json'),read('regimes.json')

def check_prefix(r,label):
 old=json.loads((OLD/'runs'/(label+'_'+r['path']+'.json')).read_bytes());cut=old['end_ms'];n=len(old['equity_minute']);assert r['equity_minute'][:n]==old['equity_minute'];assert [x for x in r['admissions'] if x['at']<cut]==old['admissions'];idx={t['source']:t for t in r['records']}
 for t in old['records']:
  if t['close_ms'] is not None:assert all(idx[t['source']][k]==v for k,v in t.items() if k in idx[t['source']]),(label,r['path'],t['source'])
 return {'prefix_minutes':n,'closed_prefix_N':sum(t['close_ms'] is not None for t in old['records'])}

def enrich(r,label):
 pred={x['source']:x for x in read('PREDICADO_BU_MINUS.json')['rows']}
 for t in r['records']:
  p=pred[t['source']];t.update(macd_context_entry=p['macd_context'],is_bu_minus=p['is_bu_minus'],reduced_bu_minus=label=='BU_HALF' and p['is_bu_minus'],status='OPEN' if t['close_ms'] is None else 'CLOSED')
  assert t['notional']==(10. if label=='BU_HALF' and p['is_bu_minus'] else UNIFORM if label=='UNIFORME' else 20.)
 r['export_label']=label;r['preregister_sha256']=read('market_manifest.json')['preregister_sha256'];r['sizing_adapter_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
 return r

def one(stage,path):
 global MODE,UNIFORM
 m,cfg,bars,signals,contexts,regimes=inputs();arm=FILTER if stage=='MACD_BU_MINUS' else CONTROL;args=(arm,cfg,bars,signals,contexts,regimes,'BASE',path,m['start_ms'],m['end_ms'])
 if stage=='parity':
  base=ORIGINAL_RUN(*args);prefix=check_prefix(base,'BE_OFF_CB');MODE='OFF';off=SIZING_RUN(*args)
  for k in base:assert off[k]==base[k],('DISABLED_PARITY_FAIL',path,k)
  assert len(off['cb_trace'])==len(base['equity_minute'])
  dump('parity/'+path+'.json',{'passed':True,'path':path,'exact_keys':list(base),'cb_trace_boundaries':len(off['cb_trace']),**prefix})
  # Keep audit/CB trace from the exactly-equal OFF run as delivered BE reference.
  dump('runs/BE_OFF_CB_'+path+'.json',enrich(off,'BE_OFF_CB'));print('DISABLED_PARITY_PASS',path,flush=True);return
 assert read('PARIDADE.json')['passed'] and read('PREDICADO_BU_MINUS.json')['passed'] and read('VIABILIDADE_10_USDT.json')['passed']
 if stage=='UNIFORME':
  assert read('VIABILIDADE_UNIFORME.json')['passed'];UNIFORM=read('UNIFORME_CALIBRACAO.json')['notional_uniforme']
 MODE=stage;r=SIZING_RUN(*args)
 if stage=='MACD_BU_MINUS':dump('PREFIXO_MACD_'+path+'.json',{'passed':True,**check_prefix(r,'MACD_BU_MINUS')})
 dump('runs/'+stage+'_'+path+'.json',enrich(r,stage));print('DONE',stage,path,len(r['records']),flush=True)

def worker(task):
 stage,path=task
 with (S/(stage+'_'+path+'.log')).open('w',encoding='utf-8') as f:r=subprocess.run([sys.executable,__file__,stage,'--path',path],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
 return {'stage':stage,'path':path,'returncode':r.returncode}

def orchestrate():
 assert read('PREDICADO_BU_MINUS.json')['passed'] and read('VIABILIDADE_10_USDT.json')['passed'] and read('VERIFICACAO_CANDLES_NATIVOS_CAUDA.json')['passed']
 results=[]
 def batch(tasks):
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:rr=list(pool.map(worker,tasks))
  results.extend(rr);dump('EXECUCOES.json',results)
  if any(x['returncode'] for x in rr):dump('INTERRUPCAO.json',{'failed':rr,'next_stage_started':False});raise RuntimeError(rr)
 batch([('parity',p) for p in PATHS]);proof=[read('parity/'+p+'.json') for p in PATHS];assert all(x['passed'] for x in proof);dump('PARIDADE.json',{'passed':True,'checks':proof,'active_economic_sizing_started_at_gate':False});print('BOTH_DISABLED_PARITIES_PASS',flush=True)
 batch([('MACD_BU_MINUS',p) for p in PATHS])
 batch([('BU_HALF','LOW_FIRST')])
 v=read('runs/BU_HALF_LOW_FIRST.json');b=read('runs/BE_OFF_CB_LOW_FIRST.json');volume=sum(t['notional'] for t in v['records']);n=len(b['records']);notional=volume/n
 dump('UNIFORME_CALIBRACAO.json',{'notional_uniforme':notional,'sum_notional_BU_HALF_LOW_FIRST':volume,'N_BE_OFF_CB_LOW_FIRST':n,'formula':'sum_notional_BU_HALF_LOW_FIRST/N_BE_OFF_CB_LOW_FIRST','fixed_paths':PATHS,'fixed_for_all_periods':True,'recalibration_allowed':False,'calibrated_in_sample':True,'notional_input_files_sha256':{k:hashlib.sha256((S/k).read_bytes()).hexdigest() for k in ['runs/BU_HALF_LOW_FIRST.json','runs/BE_OFF_CB_LOW_FIRST.json']}})
 viability(notional,'UNIFORME');print('UNIFORM_FIXED_AND_FEASIBLE',notional,flush=True)
 batch([('BU_HALF','HIGH_FIRST'),('UNIFORME','LOW_FIRST')]);batch([('UNIFORME','HIGH_FIRST')]);print('ALL8_COMPLETE',flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['parity','MACD_BU_MINUS','BU_HALF','UNIFORME','orchestrate']);p.add_argument('--path',choices=PATHS);a=p.parse_args()
 if a.stage=='orchestrate':orchestrate()
 else:one(a.stage,a.path)
