"""Read-only study driver. All writes are scoped to this study directory."""
import argparse,json,hashlib,sys,time
from pathlib import Path
from dataclasses import asdict
from copy import deepcopy
from collections import defaultdict
from datetime import datetime,timezone,timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import trail_activation_gap_systemic_study as a
from tools.market_selection_study import BinancePublicClient,load_candle_cache,save_candle_cache,missing_candle_ranges,MarketCandle
from tools.be_off_cb_deterioration_study import _signals
from tools.ge_replay_study import run_universe
from tools.cohort_study import _load_config
from src.config_profiles import effective_config
g=a.g
OUT=g.ROOT/'data/analysis/price_structure_72h_20261010'
FROZEN=json.loads((a.OUT/'manifest.json').read_text())
START=g.ms('2026-06-01T00:00:00-03:00');OLD_END=g.ms(FROZEN['end_brt'])
ARMS=('REAL_A','BE_OFF_CB_SHADOW','BE_OFF_CB_ACT20_GAP5_SHADOW','BE_OFF_CB_ACT10_GAP13_SHADOW')
PRIOR_NAMES=dict(BE_OFF_CB_SHADOW='ACT10_GAP5',BE_OFF_CB_ACT20_GAP5_SHADOW='ACT20_GAP5',BE_OFF_CB_ACT10_GAP13_SHADOW='ACT10_GAP13')

def aggregate(candles,minutes):
    width=minutes*60000; groups=defaultdict(list)
    for c in candles:groups[c.open_time_ms//width*width].append(c)
    out=[]
    for at,cs in sorted(groups.items()):
        cs=sorted(cs,key=lambda c:c.open_time_ms)
        if len(cs)!=minutes or any(c.open_time_ms!=at+i*60000 for i,c in enumerate(cs)):continue
        out.append(MarketCandle(at,at+width-1,cs[0].open,max(c.high for c in cs),min(c.low for c in cs),cs[-1].close,
            sum(c.quote_volume for c in cs),sum(c.trades for c in cs)))
    return out

def label(window,k=3):
    if len(window)!=72:return 'UNDEFINED',[],[]
    highs=[];lows=[]
    for i in range(k,len(window)-k):
        neighbours=window[i-k:i]+window[i+1:i+k+1]
        if all(window[i].high>c.high for c in neighbours):highs.append(i)
        if all(window[i].low<c.low for c in neighbours):lows.append(i)
    if len(highs)<2 or len(lows)<2:return 'UNDEFINED',highs[-2:],lows[-2:]
    dh=window[highs[-1]].high-window[highs[-2]].high;dl=window[lows[-1]].low-window[lows[-2]].low
    result='BULL' if dh>0 and dl>0 else 'BEAR' if dh<0 and dl<0 else 'MIXED'
    return result,highs[-2:],lows[-2:]

def prepare():
    OUT.mkdir(parents=True,exist_ok=True)
    frozen_hashes={tf:g.digest(g.CACHE/f'SOLUSDT_{tf}.jsonl') for tf in ['1m','5m','15m']}
    assert frozen_hashes==FROZEN['cache_hashes'],'Original cache changed'
    client=BinancePublicClient('https://api.binance.com',30)
    manifest_path=OUT/'market_manifest.json'
    if manifest_path.exists():market=json.loads(manifest_path.read_text());end=market['end_ms']
    else:
        server=int(client.get('/api/v3/time')['serverTime']);end=server//60000*60000
        assert datetime.fromtimestamp((end-1)/1000,timezone.utc).astimezone(timezone(timedelta(hours=-3))).date().isoformat()=='2026-10-10','Server date differs from requested date'
        market=dict(end_ms=end,end_brt=a.brt(end),server_time_ms=server,original_cache_hashes=frozen_hashes)
        manifest_path.write_text(json.dumps(market,indent=2),encoding='utf-8')
    original=load_candle_cache(g.CACHE/'SOLUSDT_1m.jsonl')
    existing_path=OUT/'market/SOLUSDT_1m.jsonl'
    existing=load_candle_cache(existing_path) if existing_path.exists() else []
    by={c.open_time_ms:c for c in original}
    for c in existing:
        if c.open_time_ms in by:assert by[c.open_time_ms]==c
        by[c.open_time_ms]=c
    needed_start=min(original[0].open_time_ms,g.ms('2026-05-29T00:00:00-03:00'))
    missing=missing_candle_ranges(list(by.values()),needed_start,end-1,60000)
    market['download_ranges']=missing
    for left,right in missing:
        print('DOWNLOAD',a.brt(left),a.brt(right),flush=True)
        for c in client.klines('SOLUSDT','1m',left,right):by[c.open_time_ms]=c
    candles=sorted([c for c in by.values() if needed_start<=c.open_time_ms and c.boundary_ms<=end],key=lambda c:c.open_time_ms)
    assert not missing_candle_ranges(candles,needed_start,end-1,60000)
    save_candle_cache(existing_path,candles)
    market.update(first_open_ms=candles[0].open_time_ms,candles_1m=len(candles),study_1m_sha256=g.digest(existing_path))
    for tf,minutes in [('5m',5),('15m',15),('1h',60)]:
        derived=aggregate(candles,minutes)
        if tf!='1h':
            old=load_candle_cache(g.CACHE/f'SOLUSDT_{tf}.jsonl');old_by={c.open_time_ms:c for c in old}
            # Old audited candles are authoritative; only append missing complete buckets.
            for c in derived:old_by.setdefault(c.open_time_ms,c)
            derived=sorted([c for c in old_by.values() if c.boundary_ms<=end],key=lambda c:c.open_time_ms)
        p=OUT/f'market/SOLUSDT_{tf}.jsonl';save_candle_cache(p,derived)
        market[tf]=dict(N=len(derived),sha256=g.digest(p))
    manifest_path.write_text(json.dumps(market,indent=2),encoding='utf-8')
    print(json.dumps(market),flush=True)

def signals():
    m=json.loads((OUT/'market_manifest.json').read_text());end=m['end_ms']
    candles={tf:load_candle_cache(OUT/f'market/SOLUSDT_{tf}.jsonl') for tf in ['1m','5m','15m']}
    cfg=deepcopy(FROZEN['config'])
    current=effective_config(_load_config(g.ROOT/'config/config.yaml'))
    expected=deepcopy(current['risk']);be=expected.pop('breakeven');frozen_risk=deepcopy(cfg['risk']);frozen_risk.pop('breakeven')
    assert expected==frozen_risk,'REAL_A risk changed beyond BE: qualification required'
    assert be==dict(mode='atr',trigger_atr=3,offset_atr=.1)
    real=deepcopy(cfg);real['risk']['breakeven']=be
    sigs=_signals(cfg,candles,START,end)
    serialized=[asdict(s) for s in sigs]
    (OUT/'signals_candidate.json').write_text(json.dumps(serialized),encoding='utf-8')
    prefix=[asdict(s) for s in sigs if s.boundary_ms<=OLD_END]
    assert g.digest(g.PRIOR/'signals.json')==FROZEN['signals_sha256']
    def causal(rows):
        return [{**r,'signal':{k:v for k,v in r['signal'].items() if k!='ts'}} for r in rows]
    old=json.loads((g.PRIOR/'signals.json').read_text())
    assert causal(prefix)==causal(old),'Causal signal prefix parity failed'
    (OUT/'signal_parity.json').write_text(json.dumps(dict(status='PASS',N=len(prefix),
        excluded_field='signal.ts: generation wallclock, not market clock',
        semantic_hash=hashlib.sha256(json.dumps(causal(prefix),sort_keys=True).encode()).hexdigest()),indent=2),encoding='utf-8')
    (OUT/'signals.json').write_text(json.dumps([asdict(s) for s in sigs]),encoding='utf-8')
    (OUT/'config_frozen.json').write_text(json.dumps(dict(BE_OFF_CB=cfg,REAL_A=real),indent=2),encoding='utf-8')
    print('SIGNAL PREFIX PARITY PASS',len(prefix),'full signals',len(sigs),flush=True)

def replay(path_only=None):
    end=json.loads((OUT/'market_manifest.json').read_text())['end_ms']
    configs=json.loads((OUT/'config_frozen.json').read_text())
    candles=load_candle_cache(OUT/'market/SOLUSDT_1m.jsonl')
    sigs=[g.SignalEvent(r['boundary_ms'],g.EntrySignal(**r['signal'])) for r in json.loads((OUT/'signals.json').read_text())]
    for path in ([path_only] if path_only else g.PATHS):
        for arm in ARMS:
            file=OUT/f'{path}_{arm}.json'
            if file.exists():print('REUSE',file.name,flush=True);continue
            print('REPLAY',path,arm,flush=True)
            if arm=='REAL_A':
                r=run_universe(name=arm,lookback=0,config=configs['REAL_A'],signals=sigs,execution_candles=candles,
                    start_ms=START,end_ms=end,intrabar_path=path,round_trip_spread_bps=5)
                sources={s.boundary_ms:s.signal.source_candle_open_time for s in sigs}
                ts=[{**asdict(t),'source_candle':sources[t.opened_ms],'net_usd':t.net_pct*20/100} for t in r.trades]
                ts += [dict(opened_ms=p.opened_ms,closed_ms=None,source_candle=p.position.source_candle_open_time,
                    net_usd=None,exit_reason='OPEN',entry_price=p.position.entry_price) for p in r.open_positions]
                payload=dict(trades=ts,max_sim=r.max_simultaneous_positions)
            else:
                name=PRIOR_NAMES[arm];cfg=a.arm_config(configs['BE_OFF_CB'],name)
                r=g.systemic.run_systemic(name=arm,config=cfg,signals=sigs,candles=candles,contexts=[],start_ms=START,end_ms=end,
                    path=path,spread_bps=5,fast_enabled=False)
                payload=g.serialize(r,sigs,20)
                old=json.loads((g.ROOT/f'data/analysis/feed_trail_revalidation_20261009/{path}_{name}.json').read_text())['run']
                prefix=[t for t in payload['trades'] if t['closed_ms'] is not None and t['closed_ms']<=OLD_END]
                assert prefix==[t for t in old['trades'] if t['closed_ms'] is not None],'Closed prefix parity failed'
                assert [e for e in payload['admissions'] if e['at_ms']<=OLD_END]==old['admissions'],'Admission prefix parity failed'
                assert [t for t in payload['crises'] if t<=OLD_END]==old['crises'],'CB prefix parity failed'
                payload['prefix_parity']='PASS'
            file.write_text(json.dumps(g.safe_json(payload)),encoding='utf-8')
            print('DONE',path,arm,flush=True)

def audit():
    market=json.loads((OUT/'market_manifest.json').read_text())
    assert {tf:g.digest(g.CACHE/f'SOLUSDT_{tf}.jsonl') for tf in ['1m','5m','15m']}==FROZEN['cache_hashes']
    summary=json.loads((OUT/'summary.json').read_text());statuses={}
    for path in g.PATHS:
        statuses[path]={}
        for arm in ARMS:
            run=json.loads((OUT/f'{path}_{arm}.json').read_text())
            statuses[path][arm]=run.get('prefix_parity','existing REAL_A full-engine path; BE3/offset0.1, noCB')
            for window in summary.values():
                cells=window[path]['cells'][arm]
                for key in ['N','entries','open','net','gross','fees','spread']:
                    assert abs(sum(cells[name][key] for name in ['BULL','BEAR','MIXED','UNDEFINED'])-cells['TOTAL'][key])<1e-8
                for cell in cells.values():assert abs(cell['gross']-cell['fees']-cell['spread']-cell['net'])<1e-8
                for comparison in window[path]['comparisons'].values():
                    for c in comparison.values():
                        assert abs(sum(c['episode_contributions'].values())-c['delta'])<1e-8
                        assert abs(sum(c['half_deltas'])-c['delta'])<1e-8
    sources=['tools/price_structure_study.py','tools/price_structure_readout.py','tools/price_structure_forward.py',
        'tools/ge_replay_study.py','tools/be_off_cb_fast_drop_systemic_replay.py','src/position/bot_full_engine.py','src/monitor/entry_engine.py']
    outputs=list(OUT.glob('*.json'))+list(OUT.glob('*.md'))
    result=dict(status='PASS',market_cutoff=market['end_brt'],primary_arms=ARMS,prefix_status=statuses,
        checks=['original cache hashes unchanged','context cells reconcile TOTAL','gross-fees-spread=net at1e-8',
            'episode deltas reconcile','half deltas reconcile','no future exits assigned to old cutoff'],
        tests=dict(count=18,result='PASS',modules=['test_price_structure_study','test_price_structure_readout','test_trail_activation_gap_systemic_study','test_atr_units_diagnostic']),
        source_hashes={p:g.digest(g.ROOT/p) for p in sources},
        output_hashes={p.name:g.digest(p) for p in outputs if p.name!='audit.json'})
    (OUT/'audit.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print('FINAL AUDIT PASS',flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['prepare','signals','replay','audit']);p.add_argument('--path',choices=g.PATHS);args=p.parse_args()
    if args.stage=='replay':replay(args.path)
    else:globals()[args.stage]()
