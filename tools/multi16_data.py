"""Re-use causal preparation functions without changing their original files."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
def main():
 for name in ['prepare','reconstruct','native_tail']:
  source=(ROOT/'tools'/('combo9_'+name+'.py')).read_text(encoding='utf-8')
  source=source.replace("replay_combinacoes9_20261025","replay_redutores_multi_20261025").replace("replay_12bracos_20261021","replay_combinacoes9_20261025").replace("old['code_head']","old.get('code_head',old.get('frozen_code_head'))")
  if name=='native_tail':source=source.replace('from combo9_prepare import get,S','from combo9_prepare import get\nS=ROOT/\'data/analysis/replay_redutores_multi_20261025\'')
  ns={'__file__':str(ROOT/'tools'/('combo9_'+name+'.py')),'__name__':'multi16_data_'+name};exec(compile(source,'<multi16_'+name+'>','exec'),ns);ns['main']()
if __name__=='__main__':main()
