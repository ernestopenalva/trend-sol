"""Finalize data-only MAROLA delivery after all replay processes succeeded."""
from pathlib import Path
import json
from marola_replay import export
import marola_package
S=Path(__file__).resolve().parents[1]/'data/analysis/marola_20261030'
if __name__=='__main__':
 assert all(x['returncode']==0 for x in json.loads((S/'EXECUCOES_PROCESSOS.json').read_bytes()))
 assert len(list((S/'runs').glob('*.json')))==38
 export()
 marola_package.main()
