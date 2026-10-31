"""Local independent replay processes; starts only after all saved-reference gates."""
from pathlib import Path
import subprocess,sys,json,concurrent.futures
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/marola_20261030'
def task(i):
 with (S/('FULL_'+str(i)+'.log')).open('w',encoding='utf-8') as f:
  p=subprocess.run([sys.executable,str(ROOT/'tools/marola_replay.py'),'full','--shard',str(i),'--shards','4'],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
 return {'shard':i,'returncode':p.returncode}
if __name__=='__main__':
 assert json.loads((S/'PARIDADE.json').read_bytes())['passed']
 assert not (S/'INTERRUPCAO.json').exists()
 with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(task,range(4)))
 (S/'EXECUCOES_PROCESSOS.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
 assert all(x['returncode']==0 for x in results),results
 print('ALL38_FULL_REPLAYS_COMPLETE',flush=True)
