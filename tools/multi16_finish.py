"""Wait locally for scheduled offline workers, then complete authorized review delivery."""
from pathlib import Path
import subprocess,sys,time,json
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_redutores_multi_20261025';MC=Path('G:/Meu Drive/trend-sol/Memória_Científica');DEST=MC/'propostas/MC-PROP-20261025-MULTI16-HS-01'
def command(name,args=()):
 with (S/(name.upper()+'.log')).open('w',encoding='utf-8') as f:r=subprocess.run([sys.executable,str(ROOT/'tools'/('multi16_'+name+'.py')),*args],cwd=ROOT,stdout=f,stderr=subprocess.STDOUT)
 if r.returncode:raise RuntimeError(name+' failed; inspect '+str(S/(name.upper()+'.log')))
def main():
 while not (S/'EXECUCOES.json').exists():time.sleep(2)
 assert all(x['returncode']==0 for x in json.loads((S/'EXECUCOES.json').read_bytes()))
 for name,args in [('analyze',()),('report',()),('deliver',('--publish',))]:command(name,args)
 with (S/'VALIDACAO_DOCUMENTAL_DEPOIS.log').open('w',encoding='utf-8') as f:r=subprocess.run([sys.executable,str(MC/'ferramentas/validar_mc.py'),'--raiz',str(MC),'--modo','propostas','--json',str(S/'VALIDACAO_DOCUMENTAL_DEPOIS.json')],stdout=f,stderr=subprocess.STDOUT)
 assert r.returncode in [0,1]
 after=json.loads((S/'VALIDACAO_DOCUMENTAL_DEPOIS.json').read_bytes());before=json.loads((S/'VALIDACAO_DOCUMENTAL_ANTES.json').read_bytes());own=[x for x in after['ocorrencias'] if 'MC-PROP-20261025-MULTI16-HS-01' in x.get('arquivo','')];errors=[x for x in own if x['nivel']=='ERRO'];summary={'id':'MC-PROP-20261025-MULTI16-HS-01-VALIDACAO','tipo':'APOIO_DOCUMENTAL','versao':1,'status_documental':'PROPOSTO_PARA_REVISAO','resumo_executivo':None,'baseline':before['resumo'],'global_after':after['resumo'],'own_errors':errors,'own_findings':own,'technical_pass':True,'scientific_approval':False,'global_documentary_pass':r.returncode==0}
 (S/'VALIDACAO_DOCUMENTAL_RESUMO.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');(DEST/'VALIDACAO_DOCUMENTAL_RESUMO.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');assert not errors,errors
 print('ALL_FINISHED_REVIEW_ONLY',str(DEST),flush=True)
if __name__=='__main__':main()
