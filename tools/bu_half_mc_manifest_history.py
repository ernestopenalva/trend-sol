"""Versioned historical provenance correction after an authorized MC update."""
from pathlib import Path
import json,hashlib
ROOT=Path(__file__).resolve().parents[1];MC=Path('G:/Meu Drive/trend-sol/Memória_Científica');W=ROOT/'data/analysis/replay_bu_half_20261027/incorporacao_mc'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
 plan=json.loads((W/'PLANO.json').read_bytes());paths={c['path']:c['version_preserved'] for c in plan['changes']};changed=[]
 oldrel='metodologia/MC-ADM-0014_INCORPORACAO_STACK_GUARD.json';p=MC/oldrel;raw=p.read_bytes();d=json.loads(raw);assert d['versao']==1
 historical='metodologia/versoes/MC-ADM-0014_INCORPORACAO_STACK_GUARD_v1_antes_BU_HALF.json';h=MC/historical;assert not h.exists()
 for item in d['arquivos_incorporados']:
  rel=item['arquivo']
  if rel in paths:
   assert sha(MC/paths[rel])==item['sha256'],('OLD_HASH_NOT_FOUND',rel)
   item['arquivo_atual_apos_incorporacao_BU_HALF']=rel;item['arquivo']=paths[rel];changed.append({'arquivo_atual':rel,'fonte_historica':paths[rel],'sha256_original_inalterado':item['sha256']})
 assert len(changed)==2
 h.parent.mkdir(parents=True,exist_ok=True);h.write_bytes(raw)
 d.update(versao=2,versao_anterior=historical,sha256_versao_anterior=hashlib.sha256(raw).hexdigest(),atualizacao_proveniencia_bu_half={'decisao':'MC-DEC-0016','autoridade':plan['authorization'],'escopo':'Somente apontar os hashes da incorporação STACK_GUARD para os bytes históricos preservados, após atualização dos documentos correntes por BU_HALF. Sem alterar valores, resultados ou decisões históricas.','correcoes':changed})
 p.write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
 # Make every current BU manifest digest explicitly resolvable by the official validator.
 rel='metodologia/MC-ADM-0015_INCORPORACAO_BU_HALF.json';p=MC/rel;raw=p.read_bytes();m=json.loads(raw);assert m['versao']==1
 history='metodologia/versoes/MC-ADM-0015_INCORPORACAO_BU_HALF_v1_antes_VALIDACAO.json';dest=MC/history;assert not dest.exists();dest.write_bytes(raw)
 for target,info in m['documentos'].items():info['arquivo']=target
 for target in [oldrel,historical,history]:m['documentos'][target]={'arquivo':target,'sha256':sha(MC/target),'bytes':(MC/target).stat().st_size}
 # Preserve earlier SHA values under the known historical schema and explicit targets.
 old=m.pop('anteriores_preservados')
 m['originais_preservados']=[{'arquivo':c['version_preserved'],'sha256':c['sha256_before'],'arquivo_corrente':c['path'],'versao_anterior':c['version_before'],'versao_atual':c['version_after']} for c in old]
 m.update(versao=2,versao_anterior=history,sha256_versao_anterior=hashlib.sha256(raw).hexdigest(),nota_correcao_documental='Caminhos explícitos para validação dos hashes e versionamento do manifesto histórico STACK_GUARD. Nenhuma alteração econômica ou de critérios.',proveniencia_historica_atualizada=changed)
 p.write_text(json.dumps(m,ensure_ascii=False,indent=2),encoding='utf-8')
 receipt={'manifesto_stack_guard_versao':2,'manifesto_bu_half_versao':2,'hash_values_preserved':True,'correcoes':changed,'manifesto_bu_half_sha256':sha(p)}
 (W/'RETIFICACAO_PROVENIENCIA.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(receipt,ensure_ascii=True),flush=True)
if __name__=='__main__':main()
