"""Two offline worker processes; gate references before any new economic run."""
from pathlib import Path
import subprocess,sys,json,concurrent.futures,math
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_redutores_multi_20261025';OLD=ROOT/'data/analysis/replay_combinacoes9_20261025'
def task(item):
 scope,path,shard=item
 with (S/(scope+'_'+path+'_'+str(shard)+'.log')).open('w',encoding='utf-8') as f:r=subprocess.run([sys.executable,str(ROOT/'tools/multi16_engine.py'),'--path',path,'--scope',scope,'--shard',str(shard),'--shards','2'],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
 return {'scope':scope,'path':path,'shard':shard,'returncode':r.returncode}
def verify():
 checks=[];cut=json.loads((OLD/'market_manifest.json').read_bytes())['end_ms']
 for p in (S/'runs').glob('*.json'):
  old=json.loads((OLD/'runs'/p.name).read_bytes());new=json.loads(p.read_bytes());n=len(old['equity_minute']);assert old['equity_minute']==new['equity_minute'][:n],p.name;assert old['admissions']==[x for x in new['admissions'] if x['at']<cut],p.name;nb={x['source']:x for x in new['records']}
  for r in old['records']:
   if r['close_ms'] is not None:assert r=={k:nb[r['source']][k] for k in r},(p.name,r['source'])
  checks.append({'run':p.name,'equity_minutes_exact':n,'admissions_exact':True,'closed_exact':True})
 assert len(checks)==32;(S/'VERIFICACAO_REFERENCIAS_PREFIXO.json').write_text(json.dumps({'passed':True,'checks':checks},indent=2),encoding='utf-8');print('REFERENCE_PREFIX_32_PASS',flush=True)
if __name__=='__main__':
 assert json.loads((S/'COMPOSITION_CHECKS.json').read_bytes())['passed'];result=[]
 for scope in ['references','new']:
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:rr=list(pool.map(task,[(scope,p,s) for p in ['HIGH_FIRST','LOW_FIRST'] for s in [0,1]]))
  result+=rr;assert all(x['returncode']==0 for x in rr),rr
  if scope=='references':verify()
 (S/'EXECUCOES.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print('ALL64_COMPLETE',flush=True)
