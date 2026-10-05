"""Isolated checkpoint performance audit. Never points a registry at live state."""
import sys,json,time,inspect,os,shutil,tempfile,statistics,argparse
from pathlib import Path
from collections import defaultdict,Counter
from copy import deepcopy
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import yaml
from src.config_profiles import effective_config
from src.monitor import circuit_breaker_shadow as cb
from src.monitor.forward_experiment_shadows import ExperimentalRiskShadow,PolicyShadow,FastDropEmaShadow,EmaMacdHist1mShadow
from src.monitor.trail_activation_gap_shadow import TrailActivationGapShadow
from tools.market_bot_replay import NullLogger

HERE=ROOT/"data"/"analysis"/"cb_projection_benchmark"
SNAPSHOTS=None
ORIGINAL_JSON=json.dumps
ORIGINAL_PROJECT=cb.CircuitBreakerShadow._project_committed
ORIGINAL_TICK=cb.CircuitBreakerShadow._process_tick
KEYS=['circuit_breaker_shadow','be_off_cb_shadow','be_off_cb_act20_gap5_shadow','be_off_cb_act10_gap13_shadow',
      'hs_bull_elastic_shadow','hs_bear_cluster_exit_shadow','cb_exit_all_shadow','be_off_cb_macd_bu_minus_shadow',
      'be_off_cb_ema_macd_shadow','be_off_cb_fast_drop_ema_shadow','ema_macd_hist_1m_shadow']

def factory(root,cfg,key):
    args=(root,cfg,NullLogger(),None);cohort='2026-10-05T05:34:39+00:00'
    if key=='be_off_cb_fast_drop_ema_shadow':return FastDropEmaShadow(*args,cohort_started_at=cohort)
    if key=='ema_macd_hist_1m_shadow':return EmaMacdHist1mShadow(*args,cohort_started_at=cohort)
    kwargs=dict(settings_key=key,strategy=key.upper(),pair_prefix=key,cohort_started_at=cohort)
    if key in ('be_off_cb_act20_gap5_shadow','be_off_cb_act10_gap13_shadow'):return TrailActivationGapShadow(*args,**kwargs)
    if key in ('hs_bull_elastic_shadow','hs_bear_cluster_exit_shadow','cb_exit_all_shadow'):
        exp={'hs_bull_elastic_shadow':'HS_BULL_ELASTIC','hs_bear_cluster_exit_shadow':'HS_BEAR_CLUSTER_EXIT','cb_exit_all_shadow':'CB_EXIT_ALL'}[key]
        return ExperimentalRiskShadow(*args,experiment=exp,**kwargs)
    if key in ('be_off_cb_macd_bu_minus_shadow','be_off_cb_ema_macd_shadow'):
        return PolicyShadow(*args,policy='MACD_BU_MINUS' if 'bu_minus' in key else 'EMA_MACD',**kwargs)
    kwargs['shadow_kind']=key.upper();kwargs['be_off']=key!='circuit_breaker_shadow'
    return cb.CircuitBreakerShadow(*args,**kwargs)

def signature(s):
    return {'clock':s.clock.to_state(),'positions':[p.to_state() for p in s.positions],
            'closed_records':s.closed_records,'audit_events':s.audit_events,'pending_closes':s.pending_closes,
            'last_input_ms':s.last_input_ms,'sequence':s.sequence,'market_points':s.market_points,
            'extra':s._extra_state(),
            'projected_ledger':getattr(s, '_projected_'+str(s.ledger.path), None),
            'projected_events':getattr(s, '_projected_'+str(s.audit_path), None)}

def bench(cfg,mode,cache,n=8):
    samples=[];totals=defaultdict(float);counts=Counter();written=Counter();active=False;committed={}
    for name in ('ledger','events'):
        counts[name+'_dirty_marks']=0
        counts[name+'_serializations']=0
        counts[name+'_serializations_avoided']=0
    def dump(*a,**kw):
        frame=inspect.currentframe().f_back
        caller=frame.f_code.co_name
        if caller == '<genexpr>' and frame.f_back is not None:
            caller=frame.f_back.f_code.co_name
        label={'_run_input':'pending_json','_save_state':'checkpoint_json','_project_committed':'projection_json'}.get(caller,'other_json')
        begin=time.perf_counter();result=ORIGINAL_JSON(*a,**kw)
        if active:totals[label]+=time.perf_counter()-begin;counts[label]+=1
        return result
    def atomic(path,content):
        label='pending' if str(path).endswith('.pending') else 'checkpoint' if str(path).endswith('.json') else 'projection'
        if label=='checkpoint':committed[str(path)]=content
        if active:counts[label+'_writes']+=1;written[label]+=len(content.encode('utf-8'))
        if active and mode=='cpu_only':return
        begin=time.perf_counter();path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+'.tmp')
        with tmp.open('w',encoding='utf-8') as f:
            t=time.perf_counter();f.write(content);f.flush()
            if active:totals[label+'_write_flush']+=time.perf_counter()-t
            t=time.perf_counter();os.fsync(f.fileno())
            if active:totals[label+'_fsync']+=time.perf_counter()-t
        t=time.perf_counter();os.replace(tmp,path)
        if active:totals[label+'_rename']+=time.perf_counter()-t
        if os.name!='nt':
            fd=os.open(path.parent,os.O_RDONLY)
            try:
                t=time.perf_counter();os.fsync(fd)
                if active:totals[label+'_dir_fsync']+=time.perf_counter()-t
            finally:os.close(fd)
        if active:totals[label+'_atomic_total']+=time.perf_counter()-begin
    def process(s,*a,**kw):
        begin=time.perf_counter();r=ORIGINAL_TICK(s,*a,**kw)
        if active:totals['base_process_tick']+=time.perf_counter()-begin
        return r
    projection_sizes={}
    def project(s):
        if active:
            if not cache:
                # Same pre-patch body, with both projections always visited.
                s._projection_dirty.update(ledger=True, events=True)
            for name in ('ledger','events'):
                if s._projection_dirty[name]:
                    counts[name+'_serializations']+=1
                else:
                    counts[name+'_serializations_avoided']+=1
                    written[name+'_serialization_bytes_avoided']+=projection_sizes[id(s)][name]
        return ORIGINAL_PROJECT(s)
    def mark_method(name,original):
        def tracked(s,*a,**kw):
            if active:counts[name+'_dirty_marks']+=1
            return original(s,*a,**kw)
        return tracked
    with tempfile.TemporaryDirectory(dir=HERE) as tmp:
        root=Path(tmp)
        for key in KEYS:
            dest=root/cfg['instrumentation'][key]['state_file'];dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(SNAPSHOTS/f'{key}.json',dest)
        with patch.object(cb,'_atomic_json',atomic),patch.object(cb.json,'dumps',dump),patch.object(cb.CircuitBreakerShadow,'_process_tick',process),patch.object(cb.CircuitBreakerShadow,'_project_committed',project), \
             patch.object(cb.CircuitBreakerShadow,'_append_closed_record',mark_method('ledger',cb.CircuitBreakerShadow._append_closed_record)), \
             patch.object(cb.CircuitBreakerShadow,'_update_closed_record',mark_method('ledger',cb.CircuitBreakerShadow._update_closed_record)), \
             patch.object(cb.CircuitBreakerShadow,'_append_audit_event',mark_method('events',cb.CircuitBreakerShadow._append_audit_event)), \
             patch.object(cb.CircuitBreakerShadow,'_update_audit_event',mark_method('events',cb.CircuitBreakerShadow._update_audit_event)):
            shadows=[factory(root,cfg,key) for key in KEYS]
            base=max(s.last_input_ms or 0 for s in shadows)
            base=(base//60000+1)*60000
            prices=[s.market_points[-1][1] for s in shadows if s.market_points]
            price=prices[0]
            for s in shadows:s.on_tick(price,cb._iso(cb.datetime.fromtimestamp(base/1000,cb.timezone.utc)))
            before={s.settings_key:deepcopy(signature(s)) for s in shadows}
            projection_sizes={id(s):{name:len(''.join(ORIGINAL_JSON(x,ensure_ascii=False)+'\n' for x in records).encode('utf8'))
                for name,records in (('ledger',s.closed_records),('events',s.audit_events))} for s in shadows}
            active=True
            for i in range(n):
                stamp=cb._iso(cb.datetime.fromtimestamp((base+i+1)/1000,cb.timezone.utc));begin=time.perf_counter()
                for s in shadows:s.on_tick(price,stamp)
                samples.append((time.perf_counter()-begin)*1000)
            active=False
            after={s.settings_key:deepcopy(signature(s)) for s in shadows}
            for s in shadows:
                payload=json.loads(committed[str(s.state_path)])
                payload.pop('updated_at',None)
                after[s.settings_key]['committed_checkpoint']=payload
    changed={k:[field for field in before[k] if before[k][field]!=after[k][field]] for k in before}
    stages={k:v/n*1000 for k,v in totals.items()}
    additive=('pending_json','checkpoint_json','projection_json','other_json','base_process_tick','pending_atomic_total','checkpoint_atomic_total','projection_atomic_total')
    mean=statistics.mean(samples);stages['other_overhead']=mean-sum(stages.get(k,0) for k in additive)
    return {'mode':mode,'optimized_projection_tracking':cache,'n':n,'per_market_input_all_11_arms_ms':{'mean':mean,'median':statistics.median(samples),'min':min(samples),'max':max(samples),'p90':sorted(samples)[max(0,__import__('math').ceil(.9*n)-1)]},
            'stages_mean_ms':stages,'calls_per_input':{k:v/n for k,v in counts.items()},'bytes_per_input':{k:v/n for k,v in written.items()},'changed_fields':changed,'samples_ms':samples},after

def main():
    global SNAPSHOTS
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshots',type=Path,required=True,help='Offline copies only; never live state')
    parser.add_argument('--inputs',type=int,default=8)
    parser.add_argument('--rounds',type=int,default=3)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.inputs<1 or args.rounds<1:parser.error('inputs/rounds must be positive')
    SNAPSHOTS=args.snapshots.resolve()
    HERE.mkdir(parents=True,exist_ok=True)
    cfg=effective_config(yaml.safe_load((ROOT/'config/config.yaml').read_text()))
    results=[]
    for mode in ('cpu_only','local_real_io'):
        for round_no in range(args.rounds):
            ends={}
            for optimized in ((False,True) if round_no%2==0 else (True,False)):
                r,end=bench(cfg,mode,optimized,args.inputs)
                r['round']=round_no+1
                r['implementation']='after' if optimized else 'before'
                results.append(r);ends[optimized]=end
                print(ORIGINAL_JSON(r),flush=True)
            assert ends[False]==ends[True], 'Economic/persistence payload parity failed'
    summary={}
    for mode in ('cpu_only','local_real_io'):
        summary[mode]={}
        for optimized in (False,True):
            rows=[r for r in results if r['mode']==mode and r['implementation']==('after' if optimized else 'before')]
            samples=[s for r in rows for s in r['samples_ms']]
            summary[mode]['after' if optimized else 'before']={
                'median_ms':statistics.median(samples),
                'p90_ms':sorted(samples)[__import__('math').ceil(.9*len(samples))-1],
                'projection_json_mean_ms':statistics.mean(r['stages_mean_ms'].get('projection_json',0) for r in rows)}
        before=summary[mode]['before']['median_ms'];after=summary[mode]['after']['median_ms']
        summary[mode]['saving_ms']=before-after
        summary[mode]['saving_pct']=100*(before-after)/before
    output={'host':os.name,'limits':'Local disk timings are NOT VPS timings. cpu_only mocks atomic writes. Offline states, warm-up excluded. No recovery protocol change.',
            'end_state_parity':'PASS','summary':summary,'results':results}
    print(ORIGINAL_JSON({'summary':summary,'end_state_parity':'PASS'},indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(ORIGINAL_JSON(output,indent=2),encoding='utf8')

if __name__=='__main__':main()

