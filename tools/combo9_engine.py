"""Offline composition of unchanged frozen entry/exit predicates; no operational constructors."""
from pathlib import Path
from datetime import datetime,timezone
import sys,json,inspect,hashlib,argparse,importlib.util
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];B=ROOT/'data/analysis/replay_12bracos_20261021';S=ROOT/'data/analysis/replay_combinacoes9_20261025';sys.path.insert(0,str(ROOT/'tools'));import replay12_engine as e
BASE_SOURCE=(ROOT/'tools/replay12_engine.py').read_bytes();MAN=json.loads((S/'market_manifest.json').read_bytes());assert hashlib.sha256(BASE_SOURCE).hexdigest()==MAN['base_adapter_sha256']
FILTERS=['BE_OFF_CB_MACD_BU_MINUS_SHADOW','BE_OFF_CB_EMA_MACD_SHADOW','EMA_MACD_HIST_1M_SHADOW'];REDUCERS=['HS_BEAR_CLUSTER_EXIT_SHADOW','CB_EXIT_ALL_SHADOW','BE_OFF_CB_FAST_DROP_EMA_SHADOW'];CONTROL='BE_OFF_CB_SHADOW';COMBOS={f'C{1+i*3+j:02d}_{f}__{r}':(f,r) for i,f in enumerate(FILTERS) for j,r in enumerate(REDUCERS)};ARMS=[CONTROL,*FILTERS,*REDUCERS,*COMBOS]
class PolicyRisk(e.PolicyShadow,e.ExperimentalRiskShadow,e.Core):
 _close_and_record=e.Risk._close_and_record
class HistRisk(e.EmaMacdHist1mShadow,e.ExperimentalRiskShadow,e.Core):
 _close_and_record=e.Risk._close_and_record
class PolicyFast(e.PolicyShadow,e.FastDropEmaShadow,e.Core):pass
class HistFast(e.EmaMacdHist1mShadow,e.FastDropEmaShadow,e.Core):pass
original_make=e.make

def make(arm,cfg,bps,start):
 if arm not in COMBOS:return original_make(arm,cfg,bps,start)
 selector,reducer=COMBOS[arm];o=original_make(selector,cfg,bps,start);hist=selector==FILTERS[-1];fast=reducer==REDUCERS[-1];o.__class__=(HistFast if hist else PolicyFast) if fast else (HistRisk if hist else PolicyRisk);o.arm=o.strategy=o.shadow_kind=o.pair_prefix=arm;o.experiment=None if fast else 'HS_BEAR_CLUSTER_EXIT' if reducer==REDUCERS[0] else 'CB_EXIT_ALL';return o
# Generalize only identity dispatch; financial methods, order, prices and numeric params unchanged.
run_source=inspect.getsource(e.run).replace("arm=='CB_EXIT_ALL_SHADOW'","getattr(o,'experiment',None)=='CB_EXIT_ALL'").replace("arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW'","isinstance(o,FastDropEmaShadow)")
segment_source=inspect.getsource(e.segment).replace("o.arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW'","isinstance(o,FastDropEmaShadow)")
e.make=make;e.S=S;exec(compile(segment_source,'<combo9_segment_dispatch>','exec'),e.__dict__);exec(compile(run_source,'<combo9_run_dispatch>','exec'),e.__dict__)

def checks(cfg,start):
 e.checks(cfg,start)
 def opened(name,price=100,at=None):
  o=make(name,cfg,0,start);o.now=start if at is None else at;o.market_price=price;o.entry_regime={'mare':'BULL','onda':'BEAR'};o.latest_market_context={'tf_5m':{'ema_context':'BEA','macd_context':'BE+','macd_histogram':.1,'macd_histogram_previous':.05,'latest_closed_at_ms':o.now-1}};o.tactical_candles=[{'open_time':o.now-2*e.M,'close_time':o.now-e.M-1,'open':99.8,'close':99.9},{'open_time':o.now-e.M,'close_time':o.now-1,'open':99.9,'close':100.}];sig=e.EntrySignal('SOLUSDT',price,e.iso(o.now),o.now-e.M,.2,'1m',14);assert o._process_signal(sig),name;return o,o.positions[0]
 passed=[]
 for name,(f,r) in COMBOS.items():
  o,p=opened(name);assert p._active_profit_lock_economic_floor() is not None;assert o._be_off
  blocked=make(name,cfg,0,start);blocked.now=start;blocked.market_price=100;blocked.entry_regime={'mare':'BEAR','onda':'BULL'};blocked.latest_market_context={'tf_5m':{'ema_context':'BUL','macd_context':'BU-','latest_closed_at_ms':start-1,'macd_histogram':.1,'macd_histogram_previous':.05}};blocked.tactical_candles=o.tactical_candles;assert not blocked._process_signal(e.EntrySignal('SOLUSDT',100,e.iso(start),start-e.M,.2,'1m',14));assert not blocked.positions
  if r==REDUCERS[-1]:
   o.minute_closes={start-4*e.M:100.1};e.segment(o,100,99.4,start,start+40000);assert p.exit_reason=='FAST_DROP';passed.append(name+'/FAST first crossing')
  elif r==REDUCERS[0]:
   o.now=start+5*e.M;o.market_price=99;o.tactical_candles=[{'open_time':o.now-2*e.M,'close_time':o.now-e.M-1,'open':98.8,'close':98.9},{'open_time':o.now-e.M,'close_time':o.now-1,'open':98.9,'close':99.}];o.latest_market_context['tf_5m']['latest_closed_at_ms']=o.now-1;assert o._process_signal(e.EntrySignal('SOLUSDT',99,e.iso(o.now),o.now-e.M,.2,'1m',14));victim=o.positions[-1];o.latest_market_context['tf_5m']['ema_context']='SHO';e.tick(o,98.5,o.now+1);assert p.exit_reason=='HARD_STOP' and victim.exit_reason=='HS_BEAR_CLUSTER_EXIT';passed.append(name+'/cluster negative survivor')
  else:
   o.pending_closes=[{'boundary':start+e.M,'net':-1,'pair_id':'synthetic1'},{'boundary':start+e.M,'net':-1,'pair_id':'synthetic2'}];e.tick(o,99.9,start+e.M);assert p.exit_reason=='CIRCUIT_BREAKER_EXIT_ALL';passed.append(name+'/CB crisis')
  passed.append(name+'/filter rejects BU- and PL floor retained')
 e.dump(S/'COMPOSITION_CHECKS.json',{'passed':True,'checks':passed,'MRO':{k:[c.__name__ for c in make(k,cfg,0,start).__class__.__mro__] for k in COMBOS},'new_combo_economic_results_consulted':False});print('COMPOSITION_CHECKS_PASS',len(passed),flush=True)

def run_case(arm,path):
 cfg=json.loads((S/'config.json').read_bytes());minutes=[e.Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()];signals=json.loads((S/'signals.json').read_bytes())['REAL_A'];contexts=json.loads((S/'contexts_runtime_projection.json').read_bytes());regimes=json.loads((S/'regimes.json').read_bytes());r=e.run(arm,cfg,minutes,signals,contexts,regimes,'BASE',path,MAN['start_ms'],MAN['end_ms']);r['composer_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();r['base_adapter_sha256']=MAN['base_adapter_sha256'];r['preregister_sha256']=MAN['preregister_sha256'];e.dump(S/'runs'/(arm+'_'+path+'.json'),r);print('DONE',arm,path,'N',r['stats']['N'],flush=True)

def main():
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['checks','run']);p.add_argument('--path',choices=['HIGH_FIRST','LOW_FIRST']);p.add_argument('--arm',choices=ARMS);args=p.parse_args();cfg=json.loads((S/'config.json').read_bytes())
 if args.stage=='checks':
  checks(cfg,MAN['start_ms']);(S/'BASE_ADAPTER_PRESERVADO.py').write_bytes(BASE_SOURCE);(S/'RUN_DISPATCH.py.txt').write_text(run_source,encoding='utf-8');(S/'SEGMENT_DISPATCH.py.txt').write_text(segment_source,encoding='utf-8');return
 for arm in [args.arm] if args.arm else ARMS:
  if (S/'runs'/(arm+'_'+args.path+'.json')).exists():print('EXISTS',arm,args.path,flush=True);continue
  run_case(arm,args.path)
if __name__=='__main__':main()
