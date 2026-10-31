"""Two local replay workers, no services or operational processes."""
from pathlib import Path
import subprocess,sys,concurrent.futures,json
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_combinacoes9_20261025'
def task(path):
 with (S/(path+'.log')).open('a',encoding='utf-8') as f:r=subprocess.run([sys.executable,str(ROOT/'tools/combo9_engine.py'),'run','--path',path],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
 return {'path':path,'returncode':r.returncode}
if __name__=='__main__':
 assert json.loads((S/'VERIFICACAO_PREFIXO.json').read_bytes())['overlap12h_signals_exact'];assert json.loads((S/'COMPOSITION_CHECKS.json').read_bytes())['passed']
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as p:rr=list(p.map(task,['HIGH_FIRST','LOW_FIRST']))
 (S/'EXECUCOES.json').write_text(json.dumps(rr,indent=2),encoding='utf-8');print(rr,flush=True);assert all(not x['returncode'] for x in rr)
