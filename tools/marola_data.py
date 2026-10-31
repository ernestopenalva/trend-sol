"""Public market extension and unchanged causal reconstruction for MAROLA."""
from pathlib import Path
import sys,json,hashlib
ROOT=Path(__file__).resolve().parents[1]
S=ROOT/'data/analysis/marola_20261030'
B=ROOT/'data/analysis/replay_bu_half_20261027'
sys.path.insert(0,str(ROOT/'tools'))
def main():
 receipt=json.loads((S/'RECIBO_PREREGISTRO_MC.json').read_text(encoding='utf-8-sig'))
 assert hashlib.sha256((S/'PREREGISTRO_V1.md').read_bytes()).hexdigest()==receipt['preregister_sha256']
 if not (S/'config.json').exists():(S/'config.json').write_bytes((B/'config.json').read_bytes())
 for name in ['prepare','reconstruct','native_tail']:
  source=(ROOT/'tools'/('combo9_'+name+'.py')).read_text(encoding='utf-8')
  source=source.replace('replay_combinacoes9_20261025',S.name).replace('replay_12bracos_20261021',B.name)
  source=source.replace("old['code_head']","old.get('code_head',old.get('frozen_code_head'))")
  source=source.replace("end=min(server//M*M,int(datetime.fromisoformat('2026-10-26T00:00:00-03:00').timestamp()*1000))","end=server//M*M;assert datetime.fromtimestamp(server/1000,timezone(__import__('datetime').timedelta(hours=-3))).date().isoformat()=='2026-10-30', 'Capture must be on study date BRT'")
  if name=='native_tail':source=source.replace('from combo9_prepare import get,S',"from combo9_prepare import get\nS=ROOT/'data/analysis/"+S.name+"'")
  ns={'__file__':str(ROOT/'tools'/('combo9_'+name+'.py')),'__name__':'marola_'+name}
  exec(compile(source,'<marola_'+name+'>','exec'),ns);ns['main']()
 man=json.loads((S/'market_manifest.json').read_bytes());man['half_cut_ms']=(man['start_ms']+man['end_ms'])//2
 man['preregister_receipt_sha256']=hashlib.sha256((S/'RECIBO_PREREGISTRO_MC.json').read_bytes()).hexdigest()
 (S/'market_manifest.json').write_text(json.dumps(man,ensure_ascii=False,indent=2),encoding='utf-8')
 print('MAROLA_DATA_READY',flush=True)
if __name__=='__main__':main()
