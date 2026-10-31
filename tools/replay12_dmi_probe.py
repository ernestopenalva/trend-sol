"""Conditional DMI PL-floor probe, not an additional portfolio arm."""
import sys,json,bisect,math,types
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import replay12_engine as e
S=e.S

def main():
 cfg=json.loads((S/'config.json').read_bytes());m=json.loads((S/'market_manifest.json').read_bytes());bars=[e.Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()];starts=[x['open_time_ms'] for x in bars];out=[]
 for path in ['HIGH_FIRST','LOW_FIRST']:
  principal=json.loads((S/'runs'/('DMI15_TRAJECTORY_CONTEXT_SHADOW_BASE_'+path+'.json')).read_bytes())
  for j,r in enumerate(principal['records']):
   pair={}
   for floor in [False,True]:
    o=e.make('DMI15_TRAJECTORY_CONTEXT_SHADOW',cfg,3.5,r['open_ms']);o.now=r['open_ms'];o.market_price=r['entry']/(1+3.5/10000);o.entry_regime={'mare':r['mare'],'onda':r['onda']};sig=e.EntrySignal('SOLUSDT',o.market_price,e.iso(o.now),r['source'],r['atr'],'1m',14);o._open(sig,r['source']//300000*300000);p=o.positions[0]
    if floor:
     original=p._active_profit_lock_economic_floor
     def active(self,original=original):
      kind=self.shadow_kind
      try:self.shadow_kind=None;return original()
      finally:self.shadow_kind=kind
     p._active_profit_lock_economic_floor=types.MethodType(active,p)
    for c in bars[bisect.bisect_left(starts,r['open_ms']):]:
     at=c['open_time_ms']
     if c['close_time_ms']>=m['end_ms']:break
     vals=[c['open'],c['high'],c['low'],c['close']] if path=='HIGH_FIRST' else [c['open'],c['low'],c['high'],c['close']];ts=[at,at+20000,at+40000,at+59999];e.tick(o,vals[0],ts[0])
     for k in range(1,4):
      if p.status=='CLOSED':break
      if vals[k]!=vals[k-1]:e.segment(o,vals[k-1],vals[k],ts[k-1],ts[k])
     if p.status=='CLOSED':break
    z=e.record(p,m['end_ms'],bars[-1]['close'],3.5,.2);pair[str(floor)]=z
   assert pair['False']['reason']==r['reason'] and math.isclose(pair['False']['net'],r['net'],abs_tol=1e-8),'Conditional probe failed to reproduce primary DMI trade'
   out.append({'path':path,'source':r['source'],'mare':r['mare'],'onda':r['onda'],'primary_net':r['net'],'PL_floor_probe_net':pair['True']['net'],'delta_floor_only':pair['True']['net']-r['net'],'primary_reason':r['reason'],'floor_reason':pair['True']['reason'],'fixed_entries_not_portfolio_counterfactual':True})
  print('DMI floor probe done',path,'N',len(principal['records']),flush=True)
 e.dump(S/'DIAGNOSTICO_DMI_PISO.json',{'scope':'fixed primary BASE admissions; direct PL-floor mechanism only, no new portfolio strategy','trades':out,'primary_reproduction_passed':True})
if __name__=='__main__':main()
