"""Final arithmetic/hash checks, not scientific approval."""
from pathlib import Path
import json,hashlib,math,sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import combo9_engine as c
S=c.S;B=c.B
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(n):return json.loads((S/n).read_bytes())
def main():
 checks=[]
 def test(n,v):checks.append({'check':n,'passed':bool(v)})
 man=read('market_manifest.json');test('preregister unchanged',sha(S/'PREREGISTRO_V1.md')==man['preregister_sha256']);test('config unchanged from21',sha(S/'config.json')==man['config_sha256']==sha(B/'config.json'));test('base adapter unchanged',sha(ROOT/'tools/replay12_engine.py')==man['base_adapter_sha256']);test('native tail',read('VERIFICACAO_CANDLES_NATIVOS_CAUDA.json')['passed']);test('overlap causal',read('VERIFICACAO_PREFIXO.json')['overlap12h_signals_exact']);test('composition mechanical',read('COMPOSITION_CHECKS.json')['passed']);test('counterfactual mechanical',read('GATE_SONDA_CONTRAFACTUAL.json')['passed']);test('all14 prefixes economic exact',read('VERIFICACAO_PREFIXO_ECONOMICO.json')['passed']);test('allcase economics',read('VERIFICACAO_ECONOMICA.json')['passed'])
 old=json.loads((B/'market_manifest.json').read_bytes())
 for name,h in old['frozen_code_files'].items():test('frozen source '+name,sha(B/'frozen'/name)==h)
 for tf,x in man['granularities'].items():test('market '+tf,sha(S/'market'/(tf+'.jsonl'))==x['sha256'])
 paths=list((S/'runs').glob('*.json'));test('32 unique runs',len(paths)==32)
 for p in paths:
  r=json.loads(p.read_bytes());rr=r['records'];prefix=p.stem;test(prefix+'/net',math.isclose(sum(x['net'] for x in rr),r['stats']['net_total'],abs_tol=1e-8));test(prefix+'/fees',all(math.isclose(x['gross']-x['fee_est'],x['net'],abs_tol=1e-8) for x in rr));test(prefix+'/unique',len({x['source'] for x in rr})==len(rr));test(prefix+'/capacity',r['stats']['max_positions']<=5);test(prefix+'/cut',r['start_ms']==man['start_ms'] and r['end_ms']==man['end_ms']);test(prefix+'/hash',r['base_adapter_sha256']==man['base_adapter_sha256'] and r['preregister_sha256']==man['preregister_sha256']);test(prefix+'/admitted',len(rr)==sum(x['accepted'] for x in r['admissions']))
 sm=read('RESUMO.json');cc=read('CLASSIFICACAO.json');pa=read('COMPARACOES.json');act=read('INTERVENCOES_E_CONTRAFACTUAIS.json');mm=read('MARE_DIAGNOSTICO.json');test('tables counts',len(sm)==32 and len(cc)==16 and len(pa)==48 and len(act)==30 and len(mm)==180)
 for x in pa:test('pair decomposition '+str((x['variant_arm'],x['path'],x['comparison_role'])),math.isclose(x['paired_real_delta']+x['exclusive_residual'],x['delta_total'],abs_tol=1e-8))
 for x in sm:
  total=sum(r['pct_closed'] for r in x['exit_reasons'].values());test('reason denominator '+str((x['arm'],x['path'])),not x['closed'] or math.isclose(total,100,abs_tol=1e-8))
  if x['arm']!=c.CONTROL:
   z=[r for r in mm if r['arm']==x['arm'] and r['path']==x['path']];test('mare conservation '+str((x['arm'],x['path'])),sum(r['variant']['N'] for r in z)==x['N'] and math.isclose(sum(r['variant']['net_total'] for r in z),x['net_total'],abs_tol=1e-8))
 import combo9_analyze as a
 for x in cc:
  if x['arm']==c.CONTROL:continue
  pp=[r for r in pa if r['variant_arm']==x['arm'] and r['comparison_role']=='PRIMARY'];ii=[r for r in act if r['arm']==x['arm']];group,gates=a.classify(pp,ii);test('classifier same ruler '+x['arm'],group==x['group'] and gates==x['gates']);test('FAST cannot recommend '+x['arm'],x.get('FAST_runtime_fidelity')!='NAO_DEMONSTRADA' or not x['effective_forward_recommendation'])
 result={'passed':all(x['passed'] for x in checks),'checks_N':len(checks),'failures':[x for x in checks if not x['passed']],'checks':checks,'scientific_approval':False};(S/'VERIFICACAO_FINAL.json').write_text(json.dumps(result,separators=(',',':')),encoding='utf-8');print({k:v for k,v in result.items() if k!='checks'});assert result['passed']
if __name__=='__main__':main()
