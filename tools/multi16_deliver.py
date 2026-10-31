"""Technical audit and durable review ZIP, writes exclusively its new MC proposal."""
from pathlib import Path
from collections import Counter
import sys,json,hashlib,zipfile,shutil,math,platform
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import multi16_analyze as a
S=a.S;OLD=a.m.OLD;B=ROOT/'data/analysis/replay_12bracos_20261021';MC=Path('G:/Meu Drive/trend-sol/Memória_Científica');DEST=MC/'propostas/MC-PROP-20261025-MULTI16-HS-01'
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
 return h.hexdigest()
def read(n):return json.loads((S/n).read_bytes())
def dump(n,x):a.m.e.dump(S/n,x)
def verify():
 checks=[];assert read('CLASSIFICATION_CHECKS.json')['passed'];assert read('VERIFICACAO_REFERENCIAS_PREFIXO.json')['passed'];assert read('VERIFICACAO_ECONOMICA.json')['passed'];assert read('COMPOSITION_CHECKS.json')['passed'];assert read('VERIFICACAO_CANDLES_NATIVOS_CAUDA.json')['passed'];assert len(read('RESUMO.json'))==64;assert len(read('CLASSIFICACAO.json'))==32;assert len(read('MATRIZ_MARE_ONDA.json'))==1024
 assert sha(S/'PREREGISTRO_V1.md')==read('PRE_REGISTRO_HASH.json')['sha256']==sha(DEST/'PREREGISTRO_V1.md')
 summaries=read('RESUMO.json');classes=read('CLASSIFICACAO.json');comparisons=read('COMPARACOES.json');acts=read('INTERVENCOES.json')
 for p in (S/'runs').glob('*.json'):
  r=json.loads(p.read_bytes());rr=r['records'];net=sum(t['net'] for t in rr);assert math.isclose(net,r['stats']['net_total'],abs_tol=1e-8);assert len(rr)==len({t['source'] for t in rr});assert len(rr)==r['stats']['N'];assert len(rr)==sum(t['accepted'] for t in r['admissions']);assert r['stats']['max_positions']<=5
  for t in rr:
   assert t['open_ms']>=a.START and t['open_ms']<a.END;assert t['close_ms'] is None or t['open_ms']<=t['close_ms']<a.END;assert t['status']==('OPEN' if t['close_ms'] is None else 'CLOSED');assert math.isfinite(t['net']);assert t['reducer']==a.m.REASONS.get(t['reason']);assert t['mare'] and t['onda']
  for c in r['cascades']:
   assert not set(c['cluster_pair_ids'])&set(c['victim_pair_ids']);lookup={t['pair_id']:t for t in rr};assert all(lookup[i]['reason']=='HS_BEAR_CLUSTER_EXIT' for i in c['cluster_pair_ids']);assert all(lookup[i]['reason']=='CIRCUIT_BREAKER_EXIT_ALL' for i in c['victim_pair_ids']);assert all(lag>=0 for lag in c['lags_ms']);assert all(lag>=0 for lag in c['event_lags'])
  if r['arm'] in a.m.MULTI:assert len(r['concurrent'])==sum(t['reducer'] is not None for t in rr)
  checks.append({'run':p.name,'trade_fields_conservation_and_cascades':True})
 for x in comparisons:
  assert math.isclose(x['paired_real_delta']+x['variant_only_net']-x['control_only_net'],x['delta_total'],abs_tol=1e-8);checks.append({'comparison':x['variant_arm']+' vs '+x['control_arm']+' '+x['path'],'decomposition':True})
 for c in classes:
  if c.get('reason')=='REFERENCIA_SEM_COMPARADOR':continue
  two=[x for x in comparisons if x['variant_arm']==c['arm'] and x['comparison_role']=='PRIMARY'];ac=[x for x in acts if x['arm']==c['arm']];group,gates=a.a.classify(two,ac);assert group==c['group'] and gates==c['gates'];assert c['effective_forward_recommendation'] is False;checks.append({'arm':c['arm'],'classification_recomputed':True})
 sources=read('FONTES_MC.json');official={rel:sha(MC/rel)==h for rel,h in sources.items()};assert all(official.values());dump('OFICIAIS_INTACTOS.json',{'unchanged':True,'sources':official})
 result={'passed':True,'N_checks':len(checks),'composition_gates':read('COMPOSITION_CHECKS.json')['N'],'reference_prefix_cases':32,'runs':64,'trades':sum(x['N'] for x in summaries),'matrix_cells':1024,'checks':checks,'scientific_approval':False,'independent_scientific_review':False};dump('VERIFICACAO_FINAL.json',result);print('FINAL_TECHNICAL_PASS',result['N_checks'],flush=True)
def publish():
 assert read('VERIFICACAO_FINAL.json')['passed'];assert (DEST/'PREREGISTRO_V1.md').exists()
 # Keep raw numerical arrays inside ZIP; MC sidecar is a typed documentary object.
 for name in ['RELATORIO_MULTI16.md','NOTAS_EXECUCAO.md']:
  assert not (DEST/name).exists();shutil.copyfile(S/name,DEST/name)
 sidecar={'id':'MC-PROP-20261025-MULTI16-HS-01-ANEXOS','tipo':'APOIO_DOCUMENTAL','versao':1,'status_documental':'PROPOSTO_PARA_REVISAO','resumo_executivo':None,'dados':{'classification':read('CLASSIFICACAO.json'),'technical_checks':read('VERIFICACAO_FINAL.json'),'source_path':str(S),'scientific_approval':False}}
 (DEST/'ANEXOS_DOCUMENTAIS.json').write_text(json.dumps(sidecar,ensure_ascii=False,indent=2),encoding='utf-8')
 entries={p.relative_to(S).as_posix():p for p in S.rglob('*') if p.is_file() and p.suffix not in ['.log','.pyc'] and '__pycache__' not in p.parts and p.name not in ['MANIFESTO_PACOTE.json','PACOTE_SHA256.json']}
 dependencies=list((ROOT/'tools').glob('multi16_*.py'))+[ROOT/'tools'/n for n in ['replay12_engine.py','replay12_reconstruct.py','combo9_engine.py','combo9_analyze.py','combo9_prepare.py','combo9_reconstruct.py','combo9_native_tail.py']]+[ROOT/'requirements.txt']+list((B/'frozen/src').rglob('*.py'))
 for root in [B,OLD]:
  for n in ['config.json','market_manifest.json','signals.json','contexts_runtime_projection.json','regimes.json','regime_timeline.json','market/1m.jsonl','PREREGISTRO_V1.md']:dependencies.append(root/n)
 dependencies.append(OLD/'INTERVENCOES_E_CONTRAFACTUAIS.json');dependencies.extend((OLD/'runs').glob('*.json'))
 for p in dependencies:entries['repro/'+p.relative_to(ROOT).as_posix()]=p
 manifest={'id':'MC-PROP-20261025-MULTI16-HS-01-PACOTE','tipo':'APOIO_DOCUMENTAL','versao':1,'status_documental':'PROPOSTO_PARA_REVISAO','resumo_executivo':None,'python':sys.version,'platform':platform.platform(),'files':[{'zip_entry':name,'bytes':p.stat().st_size,'sha256':sha(p)} for name,p in sorted(entries.items())],'reconstruction':'Place ZIP study root in ROOT/data/analysis/replay_redutores_multi_20261025; overlay repro/ contents into ROOT; research scripts only. Frozen end preserved; do not refetch to reproduce. Run multi16_checks, multi16_orchestrate, multi16_analyze, multi16_report. Do not execute setup again or overwrite preregistration.','scientific_approval':False};dump('MANIFESTO_PACOTE.json',manifest);entries['MANIFESTO_PACOTE.json']=S/'MANIFESTO_PACOTE.json'
 archive=S.parent/'replay_redutores_multi_20261025_revisao.zip'
 with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED,compresslevel=5) as z:
  for name,p in sorted(entries.items()):assert p.name!='.env' and '.git' not in p.parts;z.write(p,name)
 with zipfile.ZipFile(archive) as z:assert z.testzip() is None;assert sum(n.startswith('runs/') and n.endswith('.json') for n in z.namelist())==64
 package={'id':'MC-PROP-20261025-MULTI16-HS-01-HASH','tipo':'APOIO_DOCUMENTAL','versao':1,'status_documental':'PROPOSTO_PARA_REVISAO','resumo_executivo':None,'arquivo':archive.name,'sha256':sha(archive),'bytes':archive.stat().st_size,'entries':len(entries),'report_sha256':sha(S/'RELATORIO_MULTI16.md'),'prereg_sha256':sha(S/'PREREGISTRO_V1.md')};dump('PACOTE_SHA256.json',package);shutil.copyfile(archive,DEST/archive.name);assert sha(DEST/archive.name)==package['sha256'];shutil.copyfile(S/'PACOTE_SHA256.json',DEST/'PACOTE_SHA256.json');print('DELIVERED',str(DEST),package,flush=True)
if __name__=='__main__':
 verify()
 if '--publish' in sys.argv:publish()
