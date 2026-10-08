"""Economic/readout companion to winner_trajectory_study, offline only.

One diagnostic intervention: liquidate at the first observed one-ATR landmark.
This is NOT a calibrated candidate or systemic policy; admissions remain fixed.
"""
import argparse
import json
import sys
from collections import Counter,defaultdict
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.winner_trajectory_study import OUT,INPUT,terminal,aggregate,dist,stage
from tools.be_off_cb_exit_context_study import brt
from tools import forward_experiment_report as report
from tools.forward_matrix_audit import FREEZE_START,FREEZE_END


def economics(rows,net_field='net',time_field='closed_ms'):
    values=[r[net_field] for r in rows]
    win=sum(max(0,n) for n in values);loss=-sum(min(0,n) for n in values)
    equity=peak=dd=0.
    for r in sorted(rows,key=lambda r:(r[time_field],r['source'])):
        equity+=r[net_field];peak=max(peak,equity);dd=max(dd,peak-equity)
    return {'N':len(rows),'net':sum(values),'PF':win/loss if loss else None,'realized_DD':dd,
            'net_dist':dist(values),'net_without_top3':sum(values)-sum(sorted([n for n in values if n>0],reverse=True)[:3])}


def intervention(rows,fees,spread,notional):
    pairs=[]
    for r in rows:
        events=[e for e in r['episodes'] if e['landmark']]
        if not events or r['closed_ms'] is None:continue
        e=events[0];l=e['landmark'];fill=l['price']*(1-spread/2/10000)
        net=notional*((fill/r['entry']-1)*100-fees)/100
        pairs.append({'source':r['source'],'net':r['net'],'closed_ms':r['closed_ms'],
            'cf_net':net,'cf_ms':l['at_ms'],'delta':net-r['net'],
            'original_reason':r['reason'],'advance_atr':l['advance_atr'],
            'original_mfe_atr':r['mfe_atr'],'entry':r['entry'],'cf_fill':fill,
            'cf_giveback_atr':(l['peak']-fill)/r['entry_atr'],
            'month':r['month'],'first_landmark_depth_atr':l['depth_atr']})
    return {'control_same_admissions':economics(pairs),
        'landmark_liquidation_fixed_admissions':economics(pairs,'cf_net','cf_ms'),
        'delta':sum(r['delta'] for r in pairs),
        'better':sum(r['delta']>1e-9 for r in pairs),'worse':sum(r['delta']<-1e-9 for r in pairs),
        'delta_without_top3_benefits':sum(r['delta'] for r in pairs)-sum(sorted([r['delta'] for r in pairs if r['delta']>0],reverse=True)[:3]),
        'delta_by_original_exit':{reason:{'N':len(sel),'delta':sum(r['delta'] for r in sel)}
            for reason in sorted({r['original_reason'] for r in pairs})
            for sel in [[r for r in pairs if r['original_reason']==reason]]},
        'delta_by_month':{m:{'N':len(sel),'delta':sum(r['delta'] for r in sel)}
            for m in sorted({r['month'] for r in pairs}) for sel in [[r for r in pairs if r['month']==m]]},
        'rows':pairs}


def forward(path):
    state=json.loads(path.read_text());rows=[];invalid=[];unavailable=[]
    for raw in state.get('closed_records',[])+state.get('positions',[]):
        opened=report.parse_time(raw.get('opened_at') or raw.get('open_ts'))
        closed=report.parse_time(raw.get('closed_at'))
        if opened is not None and opened<FREEZE_END and (closed is None or closed>FREEZE_START):
            invalid.append(raw.get('pair_id'));continue
        entry=report._number(raw.get('entry_price'));atr=report._number(raw.get('entry_atr'))
        peak=report._number(raw.get('peak_price',raw.get('highest_price')))
        if not entry or not atr or atr<=0 or peak is None or opened is None:
            unavailable.append(raw.get('pair_id'));continue
        fees=report._number(raw.get('estimated_fees_pct'))
        if fees is None and closed:
            unavailable.append(raw.get('pair_id'));continue
        exit_price=report._number(raw.get('exit_price')) if closed else None
        row={'source':report._source(raw),'opened_ms':int(opened.timestamp()*1000),
            'closed_ms':int(closed.timestamp()*1000) if closed else None,
            'reason':raw.get('exit_reason') or 'OPEN','activation':None,'first_dominance':None,
            **terminal(entry,peak,exit_price,atr,report._net_dollars(raw) if closed else None,fees or 0),
            'entry_context':(raw.get('market_context_entry') or {}).get('tf_5m') or {},
            'exit_context':report._recorded_exit_context(raw) if closed else {}}
        if fees is None:
            row['eligible']=None
            row['mfe_net_potential_pct']=None
        rows.append(row)
    eligible=[r for r in rows if r['eligible']]
    def observed_aggregate(selected):
        result=aggregate(selected)
        for key in ('activated','trail_dominated','activated_never_dominated'):
            result[key]=None  # Control checkpoint has no historical milestone telemetry.
        return result
    return {'capture':state.get('updated_at'),'coverage_start':brt(min(r['opened_ms'] for r in rows)),
        'all_available':observed_aggregate(rows),'eligible':observed_aggregate(eligible),'invalid_freeze_ids':invalid,
        'open_censored':sum(r['closed_ms'] is None for r in rows),
        'unavailable_ids':unavailable,'STALE_exit':sum(r['exit_context'].get('ema_context')=='STALE' for r in rows),
        'UNAVAILABLE_exit':sum(r['exit_context'].get('ema_context')=='UNAVAILABLE' for r in rows),
        'eligible_by_exit':{reason:observed_aggregate([r for r in eligible if r['reason']==reason]) for reason in sorted({r['reason'] for r in eligible})},
        'rows':rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=OUT)
    parser.add_argument('--forward-state',type=Path,default=ROOT/'data/analysis/ema_macd_matrix_audit_20261006/raw/be_off_cb_shadow.json')
    args=parser.parse_args();manifest=json.loads((INPUT/'manifest.json').read_text());summary={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        rows=json.loads((args.input/f'{path}_trajectories.json').read_text())
        eligible=[r for r in rows if r['eligible']]
        detail=json.loads((INPUT/f'{path}_ACT10_GAP5.json').read_text())['details']
        by={d['source_candle']:d for d in detail}
        hs=[r for r in eligible if r['reason']=='HARD_STOP']
        summary[path]={'eligible_by_exit':{reason:aggregate([r for r in eligible if r['reason']==reason]) for reason in sorted({r['reason'] for r in eligible})},
            'eligible_by_final_mfe':{band:aggregate([r for r in eligible if stage(r['mfe_atr'])==band]) for band in ('<5','5–10','10–20','20+')},
            'HS_after_cashable_peak':{'N':len(hs),'net':sum(r['net'] for r in hs),
                'peak_gross_pct':dist([r['mfe_gross_pct'] for r in hs]),
                'peak_cashable_pct':dist([r['mfe_net_potential_pct'] for r in hs]),
                'had_PL_armed':sum(any(s['PL_armed'] for s in by[r['source']]['floor_events']) for r in hs),
                'months':dict(Counter(r['month'] for r in hs))},
            'PL_delays':dist([min(s['at_ms'] for s in by[r['source']]['floor_events'] if s['PL_armed'])/60000-r['winner_at']['at_ms']/60000
                for r in eligible if r['winner_at'] and any(s['PL_armed'] for s in by[r['source']]['floor_events'])]),
            'owner_hours':{owner:sum(r['owner_minutes'].get('total_'+owner,0) for r in eligible)/60 for owner in ('HARD_STOP','PL1','PL2','PL3','TRAIL')},
            'landmark_intervention':intervention(eligible,manifest['config']['fees']['taker_fee_pct']*2,manifest['spread_bps'],manifest['notional'])}
    summary['forward']=forward(args.forward_state)
    (args.input/'economic_readout.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
    print(json.dumps({p:{k:v for k,v in d.items() if k!='landmark_intervention'} for p,d in summary.items() if p!='forward'},indent=2))
    for p in ('HIGH_FIRST','LOW_FIRST'):
        c=summary[p]['landmark_intervention']
        print(p,'DIAGNOSTIC CF',{k:v for k,v in c.items() if k!='rows'})
    print('FORWARD',{k:v for k,v in summary['forward'].items() if k not in ('rows','eligible_by_exit')})


if __name__=='__main__':main()
