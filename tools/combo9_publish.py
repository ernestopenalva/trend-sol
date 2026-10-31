"""Explicit review bundle, then copy only to the authorized NEW proposal folder."""
from pathlib import Path
import json,hashlib,zipfile,shutil,sys,platform
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_combinacoes9_20261025';B=ROOT/'data/analysis/replay_12bracos_20261021';MC=Path('G:/Meu Drive/trend-sol/Memória_Científica');DEST=MC/'propostas/MC-PROP-20261025-COMB9-FILTROS-HS-01'
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for v in iter(lambda:f.read(1024*1024),b''):h.update(v)
 return h.hexdigest()
def main():
 assert json.loads((S/'VERIFICACAO_FINAL.json').read_bytes())['passed'];assert DEST.is_dir();assert sha(DEST/'PREREGISTRO_V1.md')==sha(S/'PREREGISTRO_V1.md')
 consulted=json.loads((S/'MC_FONTES_CONSULTADAS.json').read_bytes());official={rel:sha(MC/rel)==h for rel,h in consulted['official_sources'].items()};(S/'MC_OFICIAL_VERIFICACAO.json').write_text(json.dumps({'unchanged':all(official.values()),'sources':official,'note':'Concurrent changes by humans/other agents would require inspection; this study only writes its new proposal.'},indent=2),encoding='utf-8');assert all(official.values())
 include=[p for p in S.rglob('*') if p.is_file() and p.suffix not in ['.log','.pyc'] and '__pycache__' not in p.parts and p.name not in ['MANIFESTO_ENTREGA.json','PACOTE_SHA256.json']];include+=list((ROOT/'tools').glob('combo9_*.py'));include += [ROOT/'tools/replay12_engine.py',ROOT/'tools/replay12_reconstruct.py',ROOT/'requirements.txt'];include+=list((B/'frozen/src').rglob('*.py'))
 for n in ['config.json','market_manifest.json','signals.json','contexts_runtime_projection.json','regimes.json','regime_timeline.json','market/1m.jsonl']:include.append(B/n)
 import combo9_engine as c
 for arm in [c.CONTROL,*c.FILTERS,*c.REDUCERS]:
  for path in ['HIGH_FIRST','LOW_FIRST']:include.append(B/'runs'/(arm+'_BASE_'+path+'.json'))
 files=sorted(set(include));manifest={'id':'MC-PROP-20261025-COMB9-FILTROS-HS-01','status_documental':'PROPOSTO_PARA_REVISAO','python':sys.version,'platform':platform.platform(),'study_directory':str(S),'MC_proposal_directory':str(DEST),'scientific_approval':False,'source_version':'6a9ebff338857440abe12a30a71fd524f78ffc00','files':[{'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in files],'durable_copy':'Review ZIP copied to same proposal folder; local path alone not claimed backup'};(S/'MANIFESTO_ENTREGA.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');files.append(S/'MANIFESTO_ENTREGA.json');archive=S.parent/'replay_combinacoes9_20261025_revisao.zip'
 with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=5) as z:
  for p in files:
   assert p.name!='.env' and '.git' not in p.parts;z.write(p,p.relative_to(ROOT).as_posix())
 with zipfile.ZipFile(archive) as z:assert z.testzip() is None
 package={'archive':str(archive),'sha256':sha(archive),'bytes':archive.stat().st_size,'entries':len(files)};(S/'PACOTE_SHA256.json').write_text(json.dumps(package,indent=2),encoding='utf-8')
 for name in ['RELATORIO_COMBINACOES9.md','NOTAS_PRE_EXECUCAO.md','NOTA_REFERENCIA_NATIVA.md','MC_EXP3_FONTE_CONSULTADA.txt','CLASSIFICACAO.json','RESUMO.json','COMPARACOES.json','INTERVENCOES_E_CONTRAFACTUAIS.json','MARE_DIAGNOSTICO.json','VERIFICACAO_FINAL.json','MANIFESTO_ENTREGA.json','PACOTE_SHA256.json','MC_OFICIAL_VERIFICACAO.json']:
  target=DEST/name
  if target.exists():raise RuntimeError('Refusing overwrite '+str(target))
  shutil.copyfile(S/name,target);assert sha(target)==sha(S/name)
 target=DEST/archive.name;assert not target.exists();shutil.copyfile(archive,target);assert sha(target)==package['sha256'];print('PROPOSAL_DELIVERED',str(DEST),package,flush=True)
if __name__=='__main__':main()
