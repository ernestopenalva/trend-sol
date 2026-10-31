"""Independent conservation and table-denominator checks, no trading simulation."""
from pathlib import Path
import json,math,collections,csv
S=Path(__file__).resolve().parents[1]/'data/analysis/replay_12bracos_20261021'
def read(n):return json.loads((S/n).read_bytes())
def main():
 sm=read('RESUMO_ECONOMICO.json');mt=read('METRICAS_REGIMES.json');pa=read('COMPARACOES.json');ov=read('SOBREPOSICAO.json');checks=[]
 def test(name,value):checks.append({'check':name,'passed':bool(value)})
 test('72 unique arm/cost/path results',len(sm)==72 and len({(x['arm'],x['scenario'],x['path']) for x in sm})==72)
 for x in sm:
  key=(x['arm'],x['scenario'],x['path']);suffix='/'.join(key)
  for dim,expected in [('MARE',6),('ONDA',6),('MARE_ONDA',36)]:
   rr=[r for r in mt if (r['arm'],r['scenario'],r['path'])==key and r['dimension']==dim]
   test(suffix+'/'+dim+'/all states',len(rr)==expected)
   for field in ['closed','open','net_total','net_realized','net_open_mtm','opportunities_own','notional_hours']:
    test(suffix+'/'+dim+'/'+field,math.isclose(sum(r['stats'][field] for r in rr),x[field],abs_tol=1e-7))
 for x in mt:
  z=x['stats'];rs=z['exit_reasons'];suffix='/'.join(str(x[k]) for k in ['arm','scenario','path','dimension','mare','onda'])
  test(suffix+'/reason count',sum(v['N'] for v in rs.values())==z['closed'])
  test(suffix+'/percent denominator',all(v['pct_closed'] is None if not z['closed'] else math.isclose(v['pct_closed'],100*v['N']/z['closed'],abs_tol=1e-9) for v in rs.values()))
 for x in pa:test('pair decomposition/'+str((x['arm'],x['scenario'],x['path'],x['dimension'],x['mare'],x['onda'])),math.isclose(x['paired_delta']+x['exclusive_and_censoring_residual'],x['delta_total'],abs_tol=1e-8))
 for x in ov:
  y=next(z for z in ov if z['path']==x['path'] and z['arm_a']==x['arm_b'] and z['arm_b']==x['arm_a'])
  test('symmetric overlap/'+str((x['path'],x['arm_a'],x['arm_b'])),x['trade_intersection']==y['trade_intersection'] and x['trade_union']==y['trade_union'])
 out={'passed':all(x['passed'] for x in checks),'checks_N':len(checks),'failures':[x for x in checks if not x['passed']],'checks':checks};(S/'VERIFICACAO_TABELAS.json').write_text(json.dumps(out,separators=(',',':')),encoding='utf-8');print(json.dumps({k:v for k,v in out.items() if k!='checks'}));assert out['passed']
if __name__=='__main__':main()
