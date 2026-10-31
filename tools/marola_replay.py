"""MAROLA: unchanged economic replay, historical saved-reference gates, data only."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
import sys,json,hashlib,argparse,time,csv,shutil
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import combo9_engine as c
e=c.e;S=ROOT/'data/analysis/marola_20261030';c.S=e.S=S
PATHS=['LOW_FIRST','HIGH_FIRST']
ARMS={'REAL_A':'REAL_A','BE_OFF_CB':c.CONTROL,'ACT20':'BE_OFF_CB_ACT20_GAP5_SHADOW','ACT10':'BE_OFF_CB_ACT10_GAP13_SHADOW','FAST_DROP_EMA':c.REDUCERS[2],'HS_BEAR_CLUSTER':c.REDUCERS[0],'CB_EXIT_ALL':c.REDUCERS[1],'MACD_BU_MINUS':c.FILTERS[0],'EMA_MACD':c.FILTERS[1],'HIST_1M':c.FILTERS[2],**{k[:3]:k for k in c.COMBOS}}
STACK=['BE_OFF_CB','MACD_BU_MINUS','EMA_MACD','HIST_1M']
KEYS=['scenario','path','start_ms','end_ms','fidelity','admissions','events','daily_contributions','daily_equity','equity_minute','stats']
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def dump(p,x):e.dump(S/p,x)
def firstdiff(a,b,path=''):
 if isinstance(a,dict) and isinstance(b,dict):
  if a.keys()!=b.keys():return {'field':path,'candidate_keys':list(a),'saved_keys':list(b)}
  for k in a:
   d=firstdiff(a[k],b[k],path+'/'+str(k))
   if d:return d
 elif isinstance(a,list) and isinstance(b,list):
  if len(a)!=len(b):return {'field':path,'candidate_len':len(a),'saved_len':len(b)}
  for i,(x,y) in enumerate(zip(a,b)):
   d=firstdiff(x,y,path+'/'+str(i))
   if d:return d
 elif a!=b:return {'field':path,'candidate':a,'saved':b}
 return None

def load():
 man=read(S/'market_manifest.json');cfg=read(S/'config.json')
 assert sha(S/'PREREGISTRO_V1.md')==man['preregister_sha256']
 assert sha(ROOT/'tools/replay12_engine.py')==man['base_adapter_sha256']
 assert sha(S/'ADENDO_PARIDADE_V1.md')==read(S/'RECIBO_ADENDO_MC.json')['adendo_sha256']
 assert read(S/'VERIFICACAO_CANDLES_NATIVOS_CAUDA.json')['passed']
 assert read(S/'VERIFICACAO_ROTULOS.json')['passed']
 return man,cfg,[e.Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()],read(S/'signals.json')['REAL_A'],read(S/'contexts_runtime_projection.json'),read(S/'regimes.json')

def inputs_gate():
 man,cfg,minutes,signals,contexts,regimes=load();proof=[]
 for name in ['replay_12bracos_20261021','replay_combinacoes9_20261025','replay_stack_guard_2neg_20261026']:
  b=S.parent/name;old=read(b/'market_manifest.json');end=old['end_ms']
  assert (S/'market/1m.jsonl').read_bytes().startswith((b/'market/1m.jsonl').read_bytes()),name+'/market'
  assert (S/'config.json').read_bytes()==(b/'config.json').read_bytes(),name+'/config'
  assert [x for x in signals if x['at']<end]==read(b/'signals.json')['REAL_A'],name+'/signals'
  assert all(contexts[k]==v for k,v in read(b/'contexts_runtime_projection.json').items()),name+'/contexts'
  assert all(regimes[k]==v for k,v in read(b/'regimes.json').items()),name+'/regimes'
  proof.append({'package':name,'end_ms':end,'market_bytes_exact':True,'signals_exact':True,'contexts_exact':True,'regimes_exact':True,'manifest_sha256':sha(b/'market_manifest.json')})
 c.checks(cfg,man['start_ms'])
 dump('VERIFICACAO_INPUTS_HISTORICOS.json',{'passed':True,'packages':proof})
 print('INPUTS_AND_MECHANICS_PASS',flush=True)

def parity():
 inputs_gate();man,cfg,minutes,signals,contexts,regimes=load();checks=[]
 cases=[(label,path,'replay_12bracos_20261021' if label in ['REAL_A','ACT20','ACT10'] else 'replay_combinacoes9_20261025') for label in ARMS for path in PATHS]
 cases +=[(label,path,'replay_stack_guard_2neg_20261026') for label in STACK for path in PATHS]
 for label,path,pkg in cases:
  target='parity/'+label+'_'+path+'_'+pkg+'.json'
  if (S/target).exists():
   proof=read(S/target);assert proof['passed'];checks.append(proof);continue
  b=S.parent/pkg;arm=ARMS[label]
  file=label+'_'+path+'.json' if 'stack_guard' in pkg else arm+('_BASE_' if '12bracos' in pkg else '_')+path+'.json'
  reference=b/'runs'/file;old=read(reference);end=old['end_ms']
  print('PARITY_START',label,path,pkg,flush=True)
  r=e.run(arm,cfg,minutes,signals,contexts,regimes,'BASE',path,man['start_ms'],end)
  difference=None
  for key in KEYS:
   difference=firstdiff(r[key],old[key],key)
   if difference:break
  if not difference:
   projected=[{k:row[k] for k in r['records'][i]} for i,row in enumerate(old['records'])] if len(r['records'])==len(old['records']) else old['records']
   difference=firstdiff(r['records'],projected,'records')
  proof={'passed':difference is None,'label':label,'path':path,'reference':str(reference),'reference_sha256':sha(reference),'reference_end_ms':end,'checked_keys':KEYS+['records/all_candidate_fields'],'ignored_documental_fields':sorted(set(old['records'][0])-set(r['records'][0])) if old['records'] and r['records'] else [],'candidate_records':len(r['records']),'saved_records':len(old['records']),'difference':difference}
  dump(target,proof);checks.append(proof)
  dump('PARIDADE.json',{'passed':False,'completed':len(checks),'expected':len(cases),'checks':checks,'status':'RUNNING' if not difference else 'DIVERGENCE'})
  if difference:
   dump('INTERRUPCAO.json',{'stage':'historical_parity','proof':proof,'full_window_replays_started':False,'instruction':'Stop and report before continuing'})
   print('PARITY_DIVERGENCE',json.dumps(proof),flush=True);raise SystemExit(2)
  print('PARITY_PASS',label,path,pkg,len(checks),'/',len(cases),flush=True)
 dump('PARIDADE.json',{'passed':True,'completed':len(checks),'expected':len(cases),'checks':checks,'status':'PASS'})
 print('ALL_46_SAVED_REFERENCE_PARITIES_PASS',flush=True)

def full(shard=0,shards=1):
 assert read(S/'PARIDADE.json')['passed'];assert not (S/'INTERRUPCAO.json').exists()
 man,cfg,minutes,signals,contexts,regimes=load();executions=[]
 for label,arm in ARMS.items():
  for path in PATHS:
   if (list(ARMS).index(label)*2+PATHS.index(path))%shards!=shard:continue
   target='runs/'+label+'_'+path+'.json'
   if (S/target).exists():continue
   print('FULL_START',label,path,flush=True)
   r=e.run(arm,cfg,minutes,signals,contexts,regimes,'BASE',path,man['start_ms'],man['end_ms'])
   r['export_label']=label;r['preregister_sha256']=man['preregister_sha256'];r['adendo_sha256']=sha(S/'ADENDO_PARIDADE_V1.md');r['base_adapter_sha256']=sha(ROOT/'tools/replay12_engine.py');r['composer_sha256']=sha(ROOT/'tools/combo9_engine.py')
   dump(target,r);executions.append({'label':label,'path':path,'records':len(r['records']),'sha256':sha(S/target)});dump('EXECUCOES_SHARD_'+str(shard)+'.json',executions)
   print('FULL_DONE',label,path,len(r['records']),flush=True)

def export():
 assert read(S/'PARIDADE.json')['passed'];assert not (S/'INTERRUPCAO.json').exists()
 man=read(S/'market_manifest.json');labels=read(S/'MAROLA_LABELS.json');tz=timezone(timedelta(hours=-3));cut=(man['start_ms']+man['end_ms'])/2
 def brt(t):return datetime.fromtimestamp(t/1000,tz).isoformat(timespec='milliseconds') if t is not None else ''
 fields=['braco','path','abertura','fechamento','metade','Maré','Onda','MAROLA_60','MAROLA_90','MAROLA_120','motivo_saida','net']
 counts=[];keys=set();n=0;raw_net=csv_net=0
 with (S/'TRADES_TODOS.csv').open('w',encoding='utf-8-sig',newline='') as f,(S/'TRADES_AUDITORIA.jsonl').open('w',encoding='utf-8') as audit:
  w=csv.DictWriter(f,fieldnames=fields);w.writeheader()
  for label,arm in ARMS.items():
   for path in PATHS:
    r=read(S/'runs'/(label+'_'+path+'.json'));assert r['arm']==arm and r['end_ms']==man['end_ms']
    for t in r['records']:
     key=(label,path,t['source']);assert key not in keys;keys.add(key);assert man['start_ms']<=t['open_ms']<man['end_ms']
     labs=labels[str(t['open_ms'])];row={'braco':label,'path':path,'abertura':brt(t['open_ms']),'fechamento':brt(t['close_ms']),'metade':1 if t['open_ms']<cut else 2,'Maré':t['mare'],'Onda':t['onda'],**labs,'motivo_saida':t['reason'],'net':t['net']};w.writerow(row)
     assert all(v in ['BULL','BEAR','EXPA','COMP','UNDEFINED','UNAVAILABLE'] for v in labs.values())
     audit.write(json.dumps({'braco':label,'path':path,'source':t['source'],'pair_id':t['pair_id'],'open_ms':t['open_ms'],'close_ms':t['close_ms'],'half_cut_ms':cut,'metade':row['metade'],'marola':labs,'record':t},ensure_ascii=False,separators=(',',':'))+'\n');n+=1;raw_net+=t['net']
    counts.append({'braco':label,'path':path,'trades':len(r['records'])})
 with (S/'TRADES_TODOS.csv').open(encoding='utf-8-sig',newline='') as f:
  rows=list(csv.DictReader(f));assert len(rows)==n
  for row in rows:csv_net+=float(row['net'])
 assert csv_net==raw_net
 dump('VERIFICACAO_ENTREGA.json',{'passed':True,'runs':len(counts),'trades':n,'unique_keys':len(keys),'counts':counts,'no_rows_discarded':True,'csv_roundtrip_net_exact':True,'half_cut_ms':cut,'observational_only':True,'no_economic_readout':True})
 print('EXPORT_COMPLETE',n,'trades',len(counts),'runs',flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['parity','full','export','all']);p.add_argument('--shard',type=int,default=0);p.add_argument('--shards',type=int,default=1);args=p.parse_args()
 if args.stage in ['parity','all']:parity()
 if args.stage in ['full','all']:full(args.shard,args.shards)
 if args.stage in ['export','all']:export()
