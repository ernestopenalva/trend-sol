"""Local-only preparation of the STACK_GUARD_2NEG replay; no trading clients."""
from pathlib import Path
import sys,json,hashlib,shutil
ROOT=Path(__file__).resolve().parents[1]
S=ROOT/'data/analysis/replay_stack_guard_2neg_20261026'
OLD=ROOT/'data/analysis/replay_redutores_multi_20261025'
sys.path.insert(0,str(ROOT/'tools'))

def main():
 S.mkdir(exist_ok=True)
 if not (S/'config.json').exists():shutil.copyfile(OLD/'config.json',S/'config.json')
 for name in ['prepare','reconstruct','native_tail']:
  source=(ROOT/'tools'/('combo9_'+name+'.py')).read_text(encoding='utf-8')
  source=source.replace('replay_combinacoes9_20261025',S.name).replace('replay_12bracos_20261021',OLD.name)
  source=source.replace("old['code_head']","old.get('code_head',old.get('frozen_code_head'))")
  source=source.replace("end=min(server//M*M,int(datetime.fromisoformat('2026-10-26T00:00:00-03:00').timestamp()*1000))","end=server//M*M")
  if name=='native_tail':source=source.replace('from combo9_prepare import get,S',"from combo9_prepare import get\nS=ROOT/'data/analysis/"+S.name+"'")
  ns={'__file__':str(ROOT/'tools'/('combo9_'+name+'.py')),'__name__':'stack_data_'+name}
  exec(compile(source,'<stack_'+name+'>','exec'),ns);ns['main']()
 print('DATA_READY',flush=True)
if __name__=='__main__':main()
