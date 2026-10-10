"""Single offline LADDER_V2 variant. Production engine remains untouched."""
import json, sys, hashlib
from pathlib import Path
from collections import Counter
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import trail_activation_gap_systemic_study as a
from tools.feed_trail_revalidation import dist, quantile
from tools.friction_pl_trail_study import accounting
g=a.g
OUT=g.ROOT/'data/analysis/ladder_v2_20261009'
BASE=g.ROOT/'data/analysis/feed_trail_revalidation_20261009'

class Position(a.Position):
    def _should_activate_trailing(self,pnl_pct,pnl_atr):
        if not self.study.v2:return super()._should_activate_trailing(pnl_pct,pnl_atr)
        return 'atr:1' in self.applied_steps
    def on_tick(self,price,ts=None):
        event=super().on_tick(price,ts)
        # Only the causal portion actually visited by this modeled tick.
        observed=self.exit_price if self.status=='CLOSED' else price
        self.study_observations.append(dict(at_ms=g.ms(ts),tick=self.tick_index,
            price=observed,peak=self.highest_price))
        return event

class Instrumentation(a.Instrumentation):
    def __init__(self,*args,v2=False):super().__init__(*args);self.v2=v2
    def factory(self,*args,**kwargs):
        p=Position(*args,**kwargs);p.study=self;p.tick_index=0;p.study_observations=[]
        p.activation=None;p.first_dominance=None;p.levels={};p.floor_events=[];p.peak_events=[];p.modeled_closed_ms=None
        p.floor_minutes=Counter();self.positions.append(p)
        if self.v2:
            assert p.profit_lock_mode=='atr' and len(p.profit_lock_atr_steps)==3
            assert p.profit_lock_atr_steps[0]==dict(trigger_atr=5,lock_atr=1.5)
            p.profit_lock_atr_steps=p.profit_lock_atr_steps[:1];p.trailing_gap_atr=13
        return p
    def details(self):
        return [{**d,'observations':p.study_observations} for d,p in zip(super().details(),self.positions)]

def run():
    OUT.mkdir(parents=True,exist_ok=True)
    m=json.loads((a.OUT/'manifest.json').read_text());start=g.ms(m['start_brt']);end=g.ms(m['end_brt'])
    assert end==g.ms('2026-10-02T22:28:00-03:00')
    for tf,sha in m['cache_hashes'].items():assert g.digest(g.CACHE/f'SOLUSDT_{tf}.jsonl')==sha
    assert g.digest(g.PRIOR/'signals.json')==m['signals_sha256']
    candles=g.load_candle_cache(g.CACHE/'SOLUSDT_1m.jsonl')
    signals=[g.SignalEvent(r['boundary_ms'],g.EntrySignal(**r['signal'])) for r in json.loads((g.PRIOR/'signals.json').read_text())]
    # Both paths must pass before any experimental run starts.
    for v2 in [False,True]:
        for path in g.PATHS:
            arm='LADDER_V2' if v2 else 'PARITY_ACT10_GAP5'
            print('START',path,arm,flush=True)
            inst=Instrumentation(start,end,m['notional'],v2=v2)
            with patch.object(g.systemic,'BotFullExitPosition',inst.factory),patch.object(g.systemic,'process_candle_systemic',inst.processor):
                result=g.systemic.run_systemic(name=arm,config=a.arm_config(m['config'],'ACT10_GAP5'),signals=signals,
                    candles=candles,contexts=[],start_ms=start,end_ms=end,path=path,spread_bps=m['spread_bps'],fast_enabled=False)
            payload=dict(run=g.serialize(result,signals,m['notional']),details=inst.details())
            if not v2:
                expected=json.loads((BASE/f'{path}_ACT10_GAP5.json').read_text())
                assert payload['run']==expected['run'],'Trading parity failed'
                stripped=[{k:v for k,v in d.items() if k!='observations'} for d in payload['details']]
                # Frozen JSON converts integer milestone keys to strings.
                canonical=json.loads(json.dumps(g.safe_json(stripped)))
                if canonical!=expected['details']:
                    (OUT/f'{path}_parity_mismatch.json').write_text(json.dumps(dict(actual=canonical,expected=expected['details'])),encoding='utf-8')
                assert canonical==expected['details'],'Stop owner / snapshot parity failed'
                print('FULL PARITY PASS',path,flush=True)
            (OUT/f'{path}_{arm}.json').write_text(json.dumps(g.safe_json(payload)),encoding='utf-8')
            print('DONE',path,arm,flush=True)
    (OUT/'manifest.json').write_text(json.dumps({**m,'variant':'single LADDER_V2','peak':'since entry',
        'v2_parameters':dict(hard_stop_pct=1.5,retained_PL='PL1 original economic floor and shifted trigger',
            removed_PL=['PL2','PL3'],trailing_gap_atr=13,trailing_enabled='same tick PL1 arms',ratchet='original engine'),
        'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2),encoding='utf-8')

if __name__=='__main__':run()
