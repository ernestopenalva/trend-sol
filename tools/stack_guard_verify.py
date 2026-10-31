"""Independent delivered-file checks; no trading model changes."""
from pathlib import Path
from collections import Counter
import sys,json,csv,math,hashlib
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_stack_guard_2neg_20261026'
def read(n):return json.loads((S/n).read_bytes())
def main():
 checks=[];man=read('market_manifest.json')
 assert hashlib.sha256((S/'PREREGISTRO_V1.md').read_bytes()).hexdigest()==man['preregister_sha256'];checks.append('immutable prereg hash')
 assert read('PARIDADE.json')['passed'] and len(read('PARIDADE.json')['checks'])==8
 assert read('VERIFICACAO_FINAL.json')['passed'];checks.append('eight disabled parities and sixteen economic checks')
 runs={}
 for p in (S/'runs').glob('*.json'):
  r=json.loads(p.read_bytes());runs[(r['export_label'],r['path'])]=r
  with p.with_suffix('.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
  assert len(rows)==len(r['records'])
  assert math.isclose(sum(float(t['net']) for t in rows),r['stats']['net_total'],abs_tol=1e-8)
  for t,u in zip(rows,r['records']):
   assert int(t['source'])==u['source'] and t['status']==u['status']
   assert all(t[k]==u[k] for k in ['mare_open','onda_open','mare_close','onda_close','trend_open','wave_open','trend_close','wave_close'])
  checks.append(p.name+' CSV N/net/regimes exact')
 assert len(runs)==16
 for row in read('RESUMO.json'):
  r=runs[(row['arm'],row['path'])];start=man['start_ms'];end=man['end_ms'];cut=start+(end-start)//2
  a,z={'TOTAL':(start,end),'METADE_1':(start,cut),'METADE_2':(cut,end)}[row['period']]
  ts=[t for t in r['records'] if a<=t['open_ms']<z]
  assert len(ts)==row['N'] and math.isclose(sum(t['net'] for t in ts),row['net_total'],abs_tol=1e-8)
  assert row['HS']==sum(t['reason']=='HARD_STOP' for t in ts)
 checks.append('all48 summary rows reconcile with entry cohorts')
 groups=Counter()
 for row in read('BLOCOS_HS.json'):
  base=runs[(row['base'],row['path'])];guard=runs[(row['base']+'_STACK_GUARD_2NEG',row['path'])]
  by={t['source']:t for t in base['records']};ds=[by[s] for s in row['defining_sources']]
  assert all(t['reason']=='HARD_STOP' for t in ds)
  assert all(ds[i]['close_ms']-ds[i-1]['close_ms']<=6*3600000 for i in range(1,len(ds)))
  assert row['start_ms']==min(t['open_ms'] for t in ds)//60000*60000
  assert row['end_exclusive_ms']==max(t['close_ms'] for t in ds)//60000*60000+60000
  for name,r in [('base',base),('guard',guard)]:
   a,z=row['start_ms'],row['end_exclusive_ms']
   assert row[name+'_HS']==sum(t['reason']=='HARD_STOP' and t['close_ms'] is not None and a<=t['close_ms']<z for t in r['records'])
   assert math.isclose(row[name+'_net_entry_cohort'],sum(t['net'] for t in r['records'] if a<=t['open_ms']<z),abs_tol=1e-8)
  assert row['effective_blocks_N']==sum(x['effective'] and row['start_ms']<=x['at']<row['end_exclusive_ms'] for x in guard['stack_audit'])
  groups[(row['base'],row['path'])]+=len(ds)
 for key,n in groups.items():assert n==sum(t['reason']=='HARD_STOP' for t in runs[key]['records'])
 checks.append('all base-defined HS groups partition HS; windows/entry net/exit HS/blocks match human definition')
 classes=read('CLASSIFICACAO.json');assert not any(x['classification']=='CANDIDATA' for x in classes)
 for x in classes:
  if x['good_only_one_half_human_exception']:assert x['classification']=='INCONCLUSIVA'
 checks.append('human one-half exception respected; no candidate gates passed')
 result={'passed':True,'checks':checks,'check_groups':len(checks),'blocks':len(read('BLOCOS_HS.json')),'runs':len(runs)}
 (S/'AUDITORIA_ENTREGA.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
if __name__=='__main__':main()
