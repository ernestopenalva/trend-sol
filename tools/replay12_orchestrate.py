"""Current-study local orchestration. At most two research subprocesses; no scheduling/service."""
from pathlib import Path
import subprocess,concurrent.futures,json,sys
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_12bracos_20261021'
def task(scenario,path):
 p=S/(scenario+'_'+path+'.log')
 with p.open('a',encoding='utf-8') as f:
  r=subprocess.run([sys.executable,str(ROOT/'tools/replay12_engine.py'),'run','--scenario',scenario,'--path',path],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
 return {'scenario':scenario,'path':path,'returncode':r.returncode}
def main():
 records=[]
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
  for group in [[('BASE','HIGH_FIRST'),('BASE','LOW_FIRST')],[('OPTIMISTIC','HIGH_FIRST'),('OPTIMISTIC','LOW_FIRST')],[('ADVERSE','HIGH_FIRST'),('ADVERSE','LOW_FIRST')]]:
   results=list(pool.map(lambda x:task(*x),group));records+=results;print(json.dumps(results),flush=True)
   (S/'EXECUCOES.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
   if any(x['returncode'] for x in results):raise RuntimeError('Execution failed; preserve completed units and inspect logs')
   if group[0][0]=='BASE':subprocess.run([sys.executable,str(ROOT/'tools/replay12_results.py'),'coverage'],cwd=ROOT,check=True)
if __name__=='__main__':main()
