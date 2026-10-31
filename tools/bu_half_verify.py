"""Independent file-level verification of BU_HALF outputs and primary gates."""
from pathlib import Path
from collections import Counter
import sys,json,csv,math,hashlib
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_bu_half_20261027'
def read(n):return json.loads((S/n).read_bytes())
def main():
 m=read('market_manifest.json');assert hashlib.sha256((S/'PREREGISTRO_V1.md').read_bytes()).hexdigest()==m['preregister_sha256'];assert read('PARIDADE.json')['passed'];assert read('PREDICADO_BU_MINUS.json')['passed'];assert read('VIABILIDADE_10_USDT.json')['passed'];assert read('VIABILIDADE_UNIFORME.json')['passed'];assert read('VERIFICACAO_ECONOMICA.json')['passed']
 checks=['immutable prereg','exact predicate','both disabled parities','both sizing feasibility gates','economic checks']
 runs={};allrows=[];uniform_notional=read('UNIFORME_CALIBRACAO.json')['notional_uniforme']
 for p in (S/'runs').glob('*.json'):
  r=json.loads(p.read_bytes());label=r['export_label'];path=r['path'];runs[label,path]=r
  with p.with_suffix('.csv').open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
  assert len(rows)==len(r['records']) and math.isclose(sum(float(x['net']) for x in rows),r['stats']['net_total'],abs_tol=1e-8)
  assert all(x['arm']==label and x['path']==path for x in rows)
  assert all(k in rows[0] for k in ['notional','trend_open','wave_open','macd_context_entry','is_bu_minus','reduced_bu_minus','open_ms','close_ms','reason','net_realized','net_open_mtm'])
  pred={x['source']:x for x in read('PREDICADO_BU_MINUS.json')['rows']}
  for t in r['records']:
   assert t['is_bu_minus']==pred[t['source']]['is_bu_minus'];assert t['macd_context_entry']==pred[t['source']]['macd_context']
   assert t['notional']==(10 if label=='BU_HALF' and t['is_bu_minus'] else uniform_notional if label=='UNIFORME' else 20)
  allrows+=rows;checks.append(label+'/'+path+' csv/raw size, net and predicate')
 assert len(runs)==8
 with (S/'TRADES_TODOS.csv').open(encoding='utf-8-sig',newline='') as f:combined=list(csv.DictReader(f))
 assert len(combined)==len(allrows)
 assert Counter((x['arm'],x['path'],x['source'],x['net']) for x in combined)==Counter((x['arm'],x['path'],x['source'],x['net']) for x in allrows)
 checks.append('consolidated CSV equals eight CSV runs')
 cal=read('UNIFORME_CALIBRACAO.json');v=runs['BU_HALF','LOW_FIRST'];b=runs['BE_OFF_CB','LOW_FIRST']
 assert cal['notional_uniforme']==sum(t['notional'] for t in v['records'])/len(b['records'])
 for n,h in cal['notional_input_files_sha256'].items():assert hashlib.sha256((S/n).read_bytes()).hexdigest()==h
 checks.append('uniform one-time calibration and input hashes exact')
 for row in read('RESUMO.json'):
  r=runs[row['arm'],row['path']];cut=m['start_ms']+(m['end_ms']-m['start_ms'])//2;a,z={'TOTAL':(m['start_ms'],m['end_ms']),'METADE_1':(m['start_ms'],cut),'METADE_2':(cut,m['end_ms'])}[row['period']]
  rs=[t for t in r['records'] if a<=t['open_ms']<z];notional=sum(t['notional'] for t in rs);net=sum(t['net'] for t in rs)
  assert len(rs)==row['N'] and math.isclose(notional,row['notional_total'],abs_tol=1e-8);assert math.isclose(net,row['net_total'],abs_tol=1e-8);assert math.isclose(net/notional*100,row['net_por_100'],abs_tol=1e-9)
 checks.append('all24 economic rows reconcile cohorts/net/notional/efficiency')
 for c in read('COMPARACOES.json'):
  assert math.isclose(c['delta_net_total'],c['variant']['net_total']-c['control']['net_total'],abs_tol=1e-9)
  assert math.isclose(c['delta_net_por_100'],c['variant']['net_por_100']-c['control']['net_por_100'],abs_tol=1e-9)
 checks.append('all18 comparison rows reconcile')
 comps=read('COMPARACOES.json');rob=read('ROBUSTEZ.json');expected=[]
 for path in ['LOW_FIRST','HIGH_FIRST']:
  for control,key,threshold in [('BE_OFF_CB','delta_net_por_100',.025),('UNIFORME','delta_net_total',0.)]:
   values=[x[key] for x in comps if x['path']==path and x['control_arm']==control]
   values += [x[key] for x in next(r for r in rob if r['path']==path and r['control']==control)['trimmed_periods']]
   expected.extend(v is not None and (v>=threshold-1e-10 if control=='BE_OFF_CB' else v>1e-10) for v in values)
 assert len(expected)==24 and (read('CLASSIFICACAO.json')['classification']=='CANDIDATO')==all(expected)
 checks.append('all24 primary gates independently reconcile with delivered comparisons')
 groupcount=Counter()
 for block in read('BLOCOS_HS.json'):
  r=runs['BE_OFF_CB',block['path']];by={t['source']:t for t in r['records']};ts=[by[s] for s in block['defining_sources']]
  assert all(t['reason']=='HARD_STOP' for t in ts)
  assert block['start_ms']==min(t['open_ms'] for t in ts)//60000*60000;assert block['end_exclusive_ms']==max(t['close_ms'] for t in ts)//60000*60000+60000
  for arm,out in block['arms'].items():
   rs=runs[arm,block['path']]['records'];a,z=block['start_ms'],block['end_exclusive_ms']
   assert math.isclose(out['net_total'],sum(t['net'] for t in rs if a<=t['open_ms']<z),abs_tol=1e-8)
   assert out['HS_by_exit']==sum(t['reason']=='HARD_STOP' and t['close_ms'] is not None and a<=t['close_ms']<z for t in rs)
   assert out['BU_admitted']==sum(t['is_bu_minus'] and a<=t['open_ms']<z for t in rs)
   assert out['BU_reduced']==sum(t['reduced_bu_minus'] and a<=t['open_ms']<z for t in rs)
  groupcount[block['path']]+=len(ts)
 for path,n in groupcount.items():assert n==sum(t['reason']=='HARD_STOP' for t in runs['BE_OFF_CB',path]['records'])
 checks.append('HS blocks preserve base groups and exact shared entry/exit windows')
 result={'passed':True,'checks':checks,'runs_json':8,'runs_csv':8,'all_trades':len(combined),'blocks':len(read('BLOCOS_HS.json'))}
 (S/'AUDITORIA_ENTREGA.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)
if __name__=='__main__':main()
