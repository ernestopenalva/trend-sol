"""Freeze the post-analysis reading and final documentary diagnostics separately."""
from pathlib import Path
import json,hashlib
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_redutores_multi_20261025';MC=Path('G:/Meu Drive/trend-sol/Memória_Científica');D=MC/'propostas/MC-PROP-20261025-MULTI16-HS-01'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 x=json.loads((S/'VALIDACAO_DOCUMENTAL_FINAL.json').read_bytes());own=[q for q in x['ocorrencias'] if 'MC-PROP-20261025-MULTI16-HS-01' in q.get('arquivo','')];errors=[q for q in own if q['nivel']=='ERRO'];assert not errors
 doc={'id':'MC-PROP-20261025-MULTI16-HS-01-VALIDACAO-FINAL','tipo':'APOIO_DOCUMENTAL','versao':1,'status_documental':'PROPOSTO_PARA_REVISAO','resumo_executivo':None,'global_findings':x['resumo'],'own_errors':errors,'own_findings':own,'technical_pass':True,'scientific_approval':False,'warning_explanation':'Population heuristic warning in integral report; temporal window,32portfolios,64paths-runs and trade counts are explicitly declared. Frozen report/ZIP not rewritten to silence heuristic.301globalerrors preexist; no new own objective error.'}
 for p in [S/'VALIDACAO_DOCUMENTAL_FINAL_RESUMO.json',D/'VALIDACAO_DOCUMENTAL_FINAL_RESUMO.json']:p.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')
 package=json.loads((S/'PACOTE_SHA256.json').read_bytes());assert sha(D/package['arquivo'])==package['sha256'];assert sha(D/'RELATORIO_MULTI16.md')==package['report_sha256'];assert sha(D/'PREREGISTRO_V1.md')==package['prereg_sha256'];assert sha(D/'LEITURA_CIENTIFICA.md')==sha(S/'LEITURA_CIENTIFICA.md')
 manifest={'id':'MC-PROP-20261025-MULTI16-HS-01-COMPLEMENTO-HASH','tipo':'APOIO_DOCUMENTAL','versao':1,'status_documental':'PROPOSTO_PARA_REVISAO','resumo_executivo':None,'files':[{'arquivo':name,'sha256':sha(D/name),'bytes':(D/name).stat().st_size} for name in ['LEITURA_CIENTIFICA.md','VALIDACAO_DOCUMENTAL_FINAL_RESUMO.json']],'unchanged_archive_sha256':package['sha256'],'note':'Post-analysis reading and final documentary diagnostics accompany original immutable ZIP; no economic rerun, reclassification or preregistration change.'}
 for p in [S/'MANIFESTO_COMPLEMENTO.json',D/'MANIFESTO_COMPLEMENTO.json']:p.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 print('FINAL_COPY_HASHES_PASS; OWN_DOCUMENTARY_ERRORS=0; SCIENCE_NOT_APPROVED')
if __name__=='__main__':main()
