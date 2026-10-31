"""Freeze local research artifacts and deliver a ZIP; never operational changes."""
from pathlib import Path
import sys,json,hashlib,shutil,zipfile
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_stack_guard_2neg_20261026';OLD=ROOT/'data/analysis/replay_redutores_multi_20261025';FROZEN=ROOT/'data/analysis/replay_12bracos_20261021/frozen'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 assert json.loads((S/'VERIFICACAO_FINAL.json').read_bytes())['passed']
 code=S/'codigo';code.mkdir(exist_ok=True)
 files=['stack_guard_data.py','stack_guard_engine.py','stack_guard_analyze.py','stack_guard_verify.py','stack_guard_package.py','replay12_engine.py','replay12_reconstruct.py','combo9_prepare.py','combo9_reconstruct.py','combo9_native_tail.py']
 for name in files:shutil.copyfile(ROOT/'tools'/name,code/name)
 if not (code/'frozen').exists():shutil.copytree(FROZEN,code/'frozen',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
 refs=S/'referencias_prefixo';refs.mkdir(exist_ok=True)
 bases=['BE_OFF_CB_SHADOW','EMA_MACD_HIST_1M_SHADOW','BE_OFF_CB_MACD_BU_MINUS_SHADOW','BE_OFF_CB_EMA_MACD_SHADOW']
 for base in bases:
  for path in ['LOW_FIRST','HIGH_FIRST']:shutil.copyfile(OLD/'runs'/(base+'_'+path+'.json'),refs/(base+'_'+path+'.json'))
 shutil.copyfile(OLD/'market_manifest.json',refs/'market_manifest.json')
 manifest=[]
 for p in sorted(S.rglob('*')):
  if p.is_file() and p.name not in ['MANIFESTO_PACOTE.json','PACOTE_SHA256.json'] and p.suffix!='.zip':manifest.append({'path':p.relative_to(S).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)})
 (S/'MANIFESTO_PACOTE.json').write_text(json.dumps({'status':'PROPOSTO_PARA_REVISAO','files':manifest},ensure_ascii=False,indent=2),encoding='utf-8')
 zipfile_path=S.parent/(S.name+'.zip')
 with zipfile.ZipFile(zipfile_path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
  for p in sorted(S.rglob('*')):
   if p.is_file() and p.name!='PACOTE_SHA256.json':z.write(p,p.relative_to(S))
 with zipfile.ZipFile(zipfile_path) as z:
  assert z.testzip() is None
  assert len([n for n in z.namelist() if n.startswith('runs/') and n.endswith('.json')])==16
  assert len([n for n in z.namelist() if n.startswith('runs/') and n.endswith('.csv')])==16
 info={'path':str(zipfile_path),'bytes':zipfile_path.stat().st_size,'sha256':sha(zipfile_path),'runs_json':16,'runs_csv':16,'zip_test':'PASS'}
 (S/'PACOTE_SHA256.json').write_text(json.dumps(info,indent=2),encoding='utf-8');print(json.dumps(info),flush=True)
if __name__=='__main__':main()
