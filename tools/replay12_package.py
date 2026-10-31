"""Local reproducibility archive with explicit inclusion list; no credentials or operational stores."""
from pathlib import Path
import hashlib,json,zipfile,sys,platform,subprocess
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_12bracos_20261021'
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def main():
 for n in ['VERIFICACAO.json','VERIFICACAO_TABELAS.json','VERIFICACAO_REPRODUCAO.json','VERIFICACAO_ECONOMICA.json']:
  assert json.loads((S/n).read_bytes())['passed'],n
 assert len(list((S/'runs').glob('*.json')))==72
 files=[p for p in S.rglob('*') if p.is_file() and p.suffix not in ['.log','.partial','.pyc'] and p.name not in ['PACOTE_MANIFESTO.json','PACOTE_SHA256.json'] and '__pycache__' not in p.parts]
 files+=list((ROOT/'tools').glob('replay12_*.py'))
 files.append(ROOT/'requirements.txt')
 for n in ['fontes_economicas.json.gz','captura_sem_economia.json.gz','TELEMETRIA_COMPLEMENTAR.json']:files.append(ROOT/'data/analysis/mare_onda_forward_20261021'/n)
 for p in files:assert p.is_relative_to(ROOT) and '.git' not in p.parts and p.name!='.env'
 meta={'python':sys.version,'platform':platform.platform(),'packages':subprocess.check_output([sys.executable,'-m','pip','freeze'],text=True).splitlines(),'study':'replay_12bracos_20261021','scientific_homologation':False,'files':[{'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(set(files))]}
 manifest=S/'PACOTE_MANIFESTO.json';manifest.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8');files.append(manifest)
 archive=S.parent/'replay_12bracos_20261021_revisao.zip'
 with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=5) as z:
  for p in sorted(set(files)):z.write(p,p.relative_to(ROOT).as_posix())
 out={'archive':str(archive),'bytes':archive.stat().st_size,'sha256':sha(archive),'files':len(set(files))};(S/'PACOTE_SHA256.json').write_text(json.dumps(out,indent=2),encoding='utf-8');print(json.dumps(out))
if __name__=='__main__':main()
