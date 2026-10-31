"""Independent invariants, projection equivalence and selected full-run reproduction."""
import sys,json,hashlib,math,csv,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import replay12_engine as e
S=e.S

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--reproduce',action='store_true');args=parser.parse_args();m=json.loads((S/'market_manifest.json').read_bytes());cfg=json.loads((S/'config.json').read_bytes());full=json.loads((S/'contexts.json').read_bytes());compact=json.loads((S/'contexts_runtime_projection.json').read_bytes());checks=[]
 def add(name,passed,detail=None):checks.append({'check':name,'passed':bool(passed),'detail':detail})
 add('Projection fields exactly match full snapshots',set(full)==set(compact) and all(all(v==full[t].get(k) for k,v in r.items()) for t,r in compact.items()))
 add('Preregister unchanged',hashlib.sha256((S/'PREREGISTRO_V1.md').read_bytes()).hexdigest()==m['preregister_sha256'])
 add('Config unchanged',hashlib.sha256((S/'config.json').read_bytes()).hexdigest()==m['config_sha256'])
 for file,d in m['granularities'].items():add('Market '+file+' unchanged',hashlib.sha256((S/'market'/(file+'.jsonl')).read_bytes()).hexdigest()==d['sha256'])
 for rel,h in m['frozen_code_files'].items():add('Frozen source '+rel,hashlib.sha256((S/'frozen'/rel).read_bytes()).hexdigest()==h)
 paths=[S/'runs'/'REAL_A_BASE_HIGH_FIRST.json',S/'runs'/'CB_EXIT_ALL_SHADOW_BASE_LOW_FIRST.json'] if args.reproduce else sorted((S/'runs').glob('*.json'))
 for p in paths:
  r=json.loads(p.read_bytes());rr=r['records'];add(p.name+' net conservation',math.isclose(sum(x['net'] for x in rr),r['stats']['net_total'],abs_tol=1e-8));add(p.name+' open/closed split',sum(x['close_ms'] is not None for x in rr)+r['stats']['open']==len(rr));add(p.name+' cash amount arithmetic',all(math.isclose(x['gross']-x['fee_est'],x['net'],abs_tol=1e-8) for x in rr));add(p.name+' no duplicate signal source',len({x['source'] for x in rr})==len(rr));add(p.name+' own capacity max5',r['stats']['max_positions']<=5)
  add(p.name+' version source preserved',(S/'adapter_versions'/(r['adapter_sha256']+'.py')).is_file())
 if args.reproduce:
  del full
  minutes=[e.Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()];signals=json.loads((S/'signals.json').read_bytes());regimes=json.loads((S/'regimes.json').read_bytes())
  # Selected prospectively for simple baseline and collective state transition coverage.
  for arm,path in [('REAL_A','HIGH_FIRST'),('CB_EXIT_ALL_SHADOW','LOW_FIRST')]:
   original=json.loads((S/'runs'/(arm+'_BASE_'+path+'.json')).read_bytes());cfg_before=json.dumps(cfg,sort_keys=True);r=e.run(arm,cfg,minutes,signals['REAL_A'],compact,regimes,'BASE',path,m['start_ms'],m['end_ms'])
   add('Configuration isolation '+arm,json.dumps(cfg,sort_keys=True)==cfg_before)
   for key in ['records','admissions','daily_contributions','equity_minute','stats']:add('Independent reexecution '+arm+'/'+path+'/'+key,r[key]==original[key])
 if (S/'VERIFICACAO_ECONOMICA.json').exists():add('Independent MTM event-sweep/all-case arithmetic',json.loads((S/'VERIFICACAO_ECONOMICA.json').read_bytes())['passed'])
 result={'passed':all(x['passed'] for x in checks),'checks_N':len(checks),'failures':[x for x in checks if not x['passed']],'checks':checks,'reproduced_selected_cases':args.reproduce,'scientific_homologation':False};e.dump(S/('VERIFICACAO_REPRODUCAO.json' if args.reproduce else 'VERIFICACAO.json'),result);print(json.dumps({k:v for k,v in result.items() if k!='checks'}),flush=True);assert result['passed']
if __name__=='__main__':main()
