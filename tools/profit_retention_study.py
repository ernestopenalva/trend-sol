"""Bounded, causal fixed-admission study; never a live trading component."""
import bisect
import hashlib
import json
import math
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.winner_trajectory_study import INPUT,CACHE,OUT as PREVIOUS,dist,terminal,stage
from tools.winner_trajectory_readout import economics
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import _deduplicate
from tools.be_off_cb_defensive_closure import ms

OUT=ROOT/'data/studies/profit_retention/20261006'
# These eight points are immutable within this investigation. No adaptive search.
POLICIES={'affine':(.25,.5,.75,1.),'concave_budget':(.5,1.,2.,4.)}


def retention(family,parameter,gain,cost,atr_pct):
    """Net percentage frontier. None means not yet economically armed."""
    if gain<=0:return None
    value=parameter*gain-cost if family=='affine' else gain-parameter*math.sqrt(gain*atr_pct)
    return value if value>0 else None


def replay(row,bars,opens,end,family,parameter,fees,spread,notional):
    entry=row['entry'];atr=row['entry_atr'];peak=entry
    hard=entry*(1-.015);stop=hard;armed=None;exit_price=None;closed=None
    winner=None;owner='HARD_STOP';reason='OPEN';previous=None
    factor=1-spread/2/10000
    for c in bars[bisect.bisect_left(opens,row['opened_ms']):]:
        if c.boundary_ms>end:break
        points=_deduplicate((c.open,c.high,c.low,c.close) if row['path']=='HIGH_FIRST' else (c.open,c.low,c.high,c.close))
        previous=None
        for price in points:
            # Prior frontier applies to this segment. A new peak cannot protect its past.
            tick=stop if previous is not None and previous>stop and price<=stop else price
            if tick<=stop:
                exit_price=tick*factor;closed=c.boundary_ms;reason=owner;break
            peak=max(peak,tick)
            gain=(peak*factor/entry-1)*100-fees
            if gain>0 and winner is None:winner=c.boundary_ms
            retained=retention(family,parameter,gain,fees,atr/entry*100)
            if retained is not None:
                proposed=entry*(1+(fees+retained)/100)/factor
                if proposed>stop:
                    stop=proposed;owner='RETENTION'
                    if armed is None:armed=c.boundary_ms
            previous=price
        if closed is not None:break
    net=notional*((exit_price/entry-1)*100-fees)/100 if closed else None
    return {'source':row['source'],'opened_ms':row['opened_ms'],'closed_ms':closed,
        'month':row['month'],'reason':reason,'entry':entry,'entry_atr':atr,
        'peak':peak,'exit':exit_price,'winner_ms':winner,'armed_ms':armed,
        'original_reason':row['reason'],'original_net':row['net'],
        'original_closed_ms':row['closed_ms'],'original_mfe_atr':row['mfe_atr'],
        **terminal(entry,peak,exit_price,atr,net,fees,spread)}


def summarize(rows):
    closed=[r for r in rows if r['closed_ms'] is not None]
    paired=[r for r in closed if r['original_closed_ms'] is not None]
    delta=[r['net']-r['original_net'] for r in paired]
    return {'closed':len(closed),'open':len(rows)-len(closed),
        'economics_closed':economics(closed),
        'paired_N':len(paired),'paired_delta':sum(delta),
        'control_paired':economics([{**r,'net':r['original_net'],'closed_ms':r['original_closed_ms']} for r in paired]),
        'delta_without_top3_benefits':sum(delta)-sum(sorted([v for v in delta if v>0],reverse=True)[:3]),
        'delta_without_top3_costs':sum(delta)-sum(sorted([v for v in delta if v<0])[:3]),
        'changed':sum(abs(r['net']-r['original_net'])>1e-9 or r['closed_ms']!=r['original_closed_ms'] for r in paired),
        'delta_distribution':dist(delta),
        'giveback_atr':dist([r['giveback_atr'] for r in closed]),
        'giveback_fraction':dist([r['giveback_fraction'] for r in closed]),
        'captured_fraction':dist([r['captured_fraction'] for r in closed]),
        'unprotected_delay_min':dist([(r['armed_ms']-r['winner_ms'])/60000 for r in rows if r['winner_ms'] is not None and r['armed_ms'] is not None]),
        'positive_never_armed':sum(r['winner_ms'] is not None and r['armed_ms'] is None for r in rows),
        'by_original_exit':{k:{'N':len(s),'delta':sum(r['net']-r['original_net'] for r in s)}
            for k in sorted({r['original_reason'] for r in paired})
            for s in [[r for r in paired if r['original_reason']==k]]},
        'by_original_progress':{k:{'N':len(s),'delta':sum(r['net']-r['original_net'] for r in s)}
            for k in ('<5','5–10','10–20','20+')
            for s in [[r for r in paired if stage(r['original_mfe_atr'])==k]]},
        'monthly':{k:{'N':len(s),'delta':sum(r['net']-r['original_net'] for r in s),
                       'net':sum(r['net'] for r in s)}
            for k in sorted({r['month'] for r in paired}) for s in [[r for r in paired if r['month']==k]]}}


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    frozen=json.loads((INPUT/'manifest.json').read_text())
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    for tf,expected in frozen['cache_hashes'].items():
        if digest(CACHE/f'SOLUSDT_{tf}.jsonl')!=expected:raise ValueError('Frozen candles changed')
    plan={'families':POLICIES,'max_families':3,'max_points_per_family':4,
        'adaptive_extension':False,'prediction_of_continuation':False,
        'affine':'R=q*G-C; arm iff R>0',
        'concave_budget':'R=G-b*sqrt(G*A); arm iff R>0',
        'units':'G,R,C,A in percentage points; G net sell-at-known-peak, C roundtrip fees, A entry ATR/entry*100',
        'ratchet':'frontier non-decreasing; replaces PL and TRAIL; retains HS -1.5%',
        'systemic':False,'end_brt':frozen['end_brt'],
        'tool_sha256':digest(Path(__file__)),'baseline_manifest_sha256':digest(INPUT/'manifest.json')}
    plan_path=OUT/'predeclared_plan.json'
    if plan_path.exists():
        old=json.loads(plan_path.read_text())
        for key in ('families','affine','concave_budget','units','ratchet','end_brt'):
            if old[key]!=json.loads(json.dumps(plan[key])):raise ValueError('Do not change protocol after results')
    else:
        plan_path.write_text(json.dumps(plan,indent=2),encoding='utf8')
    bars=load_candle_cache(CACHE/'SOLUSDT_1m.jsonl');opens=[c.open_time_ms for c in bars]
    end=ms(frozen['end_brt']);fees=frozen['config']['fees']['taker_fee_pct']*2
    results={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        baseline=json.loads((PREVIOUS/f'{path}_trajectories.json').read_text())
        for row in baseline:row['path']=path
        results[path]={}
        for family,params in POLICIES.items():
            for param in params:
                key=f'{family}_{param:g}';print(path,key,flush=True)
                rows=[replay(r,bars,opens,end,family,param,fees,frozen['spread_bps'],frozen['notional']) for r in baseline]
                (OUT/f'{path}_{key}_trades.json').write_text(json.dumps(rows),encoding='utf8')
                results[path][key]=summarize(rows)
    (OUT/'summary.json').write_text(json.dumps(results,indent=2),encoding='utf8')


if __name__=='__main__':main()
