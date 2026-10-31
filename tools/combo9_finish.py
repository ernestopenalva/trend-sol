"""Finish local analysis only after all32 replay cases succeed."""
from pathlib import Path
import time,json,subprocess,sys
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_combinacoes9_20261025'
if __name__=='__main__':
 while not (S/'EXECUCOES.json').exists():time.sleep(2)
 assert all(not r['returncode'] for r in json.loads((S/'EXECUCOES.json').read_bytes()))
 for stage in ['analyze','report']:
  with (S/(stage.upper()+'.log')).open('w',encoding='utf-8') as f:r=subprocess.run([sys.executable,str(ROOT/'tools'/('combo9_'+stage+'.py'))],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
  if r.returncode:raise RuntimeError(stage+' failed; inspect log, do not classify missing results')
 print('ANALYSIS_REPORT_READY',flush=True)
