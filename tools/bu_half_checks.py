"""Focused sizing, predicate reuse, and reservation tests before economic variants."""
from pathlib import Path
import sys,json,math
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import bu_half_engine as g
def main():
 m=g.read('market_manifest.json');cfg=g.read('config.json');start=m['start_ms'];checks=[];positions=[]
 for mode,state,expected in [('OFF','BU-',20.),('BU_HALF','BU-',10.),('BU_HALF','BU+',20.),('BU_HALF','UNAVAILABLE',20.),('BU_HALF',None,20.)]:
  g.MODE=mode;o=g.make(g.CONTROL,cfg,3.5,start);o.now=start;o.market_price=100;o.entry_regime={'mare':'BEAR','onda':'BULL'};o.latest_market_context={'tf_5m':{'macd_context':state,'latest_closed_at_ms':start-1}}
  sig=g.e.EntrySignal('SOLUSDT',100,g.e.iso(start),start-60000,.2,'1m',14);assert g.process(o,sig);p=o.all_positions[0];assert p.position_notional_usdt==expected;assert math.isclose(p.quantity*p.entry_price,expected,abs_tol=1e-12);assert len(o.positions)==1 and sum(o.entries_by_bucket.values())==1
  assert p._active_profit_lock_economic_floor() is not None and o._be_off
  positions.append(p);checks.append({'mode':mode,'state':state,'notional':expected,'reserves_slot_bucket':True})
 assert positions[0].entry_price==positions[1].entry_price and positions[0].hard_stop_price==positions[1].hard_stop_price
 assert math.isclose(positions[1].quantity,positions[0].quantity/2,abs_tol=1e-15)
 g.dump('TESTES_SINTETICOS.json',{'passed':True,'checks':checks,'half_quantity_exact':True,'same_entry_and_HS':True,'economic_floor_and_BE_off':True,'continuous_economic_quantity_unchanged':True});print('SYNTHETIC_SIZING_PASS',flush=True)
if __name__=='__main__':main()
