"""Observational labels only; no replay or economic changes."""
from pathlib import Path
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import importlib.util,sys,json,hashlib
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/marola_20261030'
spec=importlib.util.spec_from_file_location('marola_official_structure',ROOT/'src/monitor/price_structure.py');p=importlib.util.module_from_spec(spec);sys.modules[spec.name]=p;spec.loader.exec_module(p)
def main():
 candles=[json.loads(x) for x in (S/'market/5m.jsonl').read_text().splitlines()]
 hours=[p.Hour(c['open_time_ms'],c['high'],c['low'],True,c['close_time_ms']+1) for c in candles]
 rules={f'MAROLA_{n*5}':replace(p.WAVE,interval_ms=300000,count=n) for n in [12,18,24]}
 buffers={k:p.StructureBuffer(hours,rule=r) for k,r in rules.items()}
 signals=json.loads((S/'signals.json').read_bytes())['REAL_A'];labels={}
 with (S/'MAROLA_SNAPSHOTS.jsonl').open('x',encoding='utf-8') as f:
  for row in signals:
   at=row['at'];snaps={k:b.snapshot(at,'IDEALIZED_PUBLIC') for k,b in buffers.items()}
   for k,snap in snaps.items():
    assert snap['window_end_ms']<=at
    assert all(e['closed_ms']<at for name in ['highs','lows'] for e in snap[name])
    assert snap['label'] in ['BULL','BEAR','EXPA','COMP','UNDEFINED','UNAVAILABLE']
   labels[str(at)]={k:v['label'] for k,v in snaps.items()};f.write(json.dumps({'at':at,'snapshots':snaps},separators=(',',':'))+'\n')
 (S/'MAROLA_LABELS.json').write_text(json.dumps(labels,separators=(',',':')),encoding='utf-8')
 proof={'passed':True,'signals_N':len(signals),'classification_source_sha256':hashlib.sha256((ROOT/'src/monitor/price_structure.py').read_bytes()).hexdigest(),'rule_changes_only':['interval_ms','count'],'rules':{k:r.__dict__ for k,r in rules.items()},'snapshot_sha256':hashlib.sha256((S/'MAROLA_SNAPSHOTS.jsonl').read_bytes()).hexdigest(),'economic_replay_started':False}
 (S/'VERIFICACAO_ROTULOS.json').write_text(json.dumps(proof,indent=2),encoding='utf-8');print(json.dumps(proof),flush=True)
if __name__=='__main__':main()
