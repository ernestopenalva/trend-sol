"""Package reproducible public-data research, excluding operational secrets."""
from pathlib import Path
import json,hashlib,shutil,zipfile
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_bu_half_20261027'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 assert json.loads((S/'AUDITORIA_ENTREGA.json').read_bytes())['passed'];code=S/'codigo';code.mkdir(exist_ok=True)
 for n in ['bu_half_data.py','bu_half_prechecks.py','bu_half_checks.py','bu_half_engine.py','bu_half_analyze.py','bu_half_verify.py','bu_half_package.py','stack_guard_data.py','stack_guard_analyze.py','stack_guard_engine.py','replay12_engine.py','replay12_reconstruct.py','combo9_prepare.py','combo9_reconstruct.py','combo9_native_tail.py']:
  shutil.copyfile(ROOT/'tools'/n,code/n)
 frozen=ROOT/'data/analysis/replay_12bracos_20261021/frozen'
 if not (code/'frozen').exists():shutil.copytree(frozen,code/'frozen',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 refs=S/'referencias_prefixo';refs.mkdir(exist_ok=True)
 for label in ['BE_OFF_CB','MACD_BU_MINUS']:
  for path in ['LOW_FIRST','HIGH_FIRST']:shutil.copyfile(ROOT/'data/analysis/replay_stack_guard_2neg_20261026/runs'/(label+'_'+path+'.json'),refs/(label+'_'+path+'.json'))
 shutil.copyfile(ROOT/'data/analysis/replay_stack_guard_2neg_20261026/market_manifest.json',refs/'market_manifest.json')
 manifest=[{'path':p.relative_to(S).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(S.rglob('*')) if p.is_file() and p.name not in ['MANIFESTO_PACOTE.json','PACOTE_SHA256.json']]
 (S/'MANIFESTO_PACOTE.json').write_text(json.dumps({'status':'PROPOSTO_PARA_REVISAO','files':manifest},indent=2),encoding='utf-8')
 target=S.parent/(S.name+'.zip')
 with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
  for p in sorted(S.rglob('*')):
   if p.is_file() and p.name!='PACOTE_SHA256.json':z.write(p,p.relative_to(S))
 with zipfile.ZipFile(target) as z:
  assert z.testzip() is None;assert len([n for n in z.namelist() if n.startswith('runs/') and n.endswith('.json')])==8;assert len([n for n in z.namelist() if n.startswith('runs/') and n.endswith('.csv')])==8
 info={'path':str(target),'bytes':target.stat().st_size,'sha256':sha(target),'zip_test':'PASS','runs_json':8,'runs_csv':8}
 (S/'PACOTE_SHA256.json').write_text(json.dumps(info,indent=2),encoding='utf-8');print(json.dumps(info),flush=True)
if __name__=='__main__':main()
