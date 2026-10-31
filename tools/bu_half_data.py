"""Reuse the unchanged public-market preparation for BU_HALF."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
def main():
 source=(ROOT/'tools/stack_guard_data.py').read_text(encoding='utf-8')
 source=source.replace('replay_stack_guard_2neg_20261026','replay_bu_half_20261027').replace('replay_redutores_multi_20261025','replay_stack_guard_2neg_20261026')
 ns={'__file__':str(ROOT/'tools/stack_guard_data.py'),'__name__':'bu_data'}
 exec(compile(source,'<bu_half_data>','exec'),ns);ns['main']()
if __name__=='__main__':main()
