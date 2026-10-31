"""Manifest and packaging only, no scientific readout."""
from pathlib import Path
from datetime import datetime,timezone
import json,hashlib,zipfile,sys
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/marola_20261030'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def main():
 delivery=read(S/'VERIFICACAO_ENTREGA.json');assert delivery['passed'] and delivery['runs']==38
 assert read(S/'PARIDADE.json')['passed'];assert not (S/'INTERRUPCAO.json').exists()
 man=read(S/'market_manifest.json')
 state={'status':'CONCLUIDO_DADOS_SEM_LEITURA','market_end_exclusive_ms':man['end_ms'],'half_cut_ms':man['half_cut_ms'],'historical_parities':46,'runs':38,'trades':delivery['trades'],'preregistered_mc':True,'no_recommendation':True}
 (S/'ESTADO.json').write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
 for name in ['marola_data.py','marola_labels.py','marola_replay.py','marola_package.py','marola_full_orchestrate.py','marola_deliver.py']:(S/'codigo'/('tools__'+name)).write_bytes((ROOT/'tools'/name).read_bytes())
 entries=[{'arquivo':p.relative_to(S).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)} for p in sorted(S.rglob('*')) if p.is_file() and p.name not in ['MANIFESTO_PACOTE.json','PACOTE_SHA256.json'] and not p.name.endswith('.log')]
 manifest={'study':'MAROLA','created_at_utc':datetime.now(timezone.utc).isoformat(),'data_only':True,'market':man,'historical_parity_proof_sha256':sha(S/'PARIDADE.json'),'delivery_verification_sha256':sha(S/'VERIFICACAO_ENTREGA.json'),'files':entries,'logs_excluded_from_integrity_manifest':True,'file_hashes_exclude_manifest_itself':True}
 (S/'MANIFESTO_PACOTE.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 for item in entries:assert sha(S/item['arquivo'])==item['sha256']
 target=S.parent/(S.name+'.zip')
 with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
  for p in sorted(S.rglob('*')):
   if p.is_file() and p.name!='PACOTE_SHA256.json' and not p.name.endswith('.log'):z.write(p,S.name+'/'+p.relative_to(S).as_posix())
 with zipfile.ZipFile(target) as z:assert z.testzip() is None
 receipt={'arquivo':str(target),'bytes':target.stat().st_size,'sha256':sha(target),'files':len(entries),'runs':38,'trades':delivery['trades'],'manifest_sha256':sha(S/'MANIFESTO_PACOTE.json')}
 (S/'PACOTE_SHA256.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(receipt),flush=True)
if __name__=='__main__':main()
