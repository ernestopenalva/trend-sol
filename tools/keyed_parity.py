"""Offline independent proposal reference and runtime-adapter parity. No clients."""
import argparse
from pathlib import Path
import sys, json, hashlib, inspect
from copy import deepcopy

ROOT = Path(__file__).resolve().parents[1]
S = ROOT/'data/analysis/keyed_20261101'
U = ROOT/'data/analysis/marola_20261030'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tools'))

def read(p):
    return json.loads(p.read_text(encoding='utf-8-sig'))

def save(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf8')

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def provenance(stage, kind, path, engine, **extra):
    files = {str(Path(inspect.getfile(cls)).resolve()) for cls in (
        engine.Core, engine.CircuitBreakerShadow, engine.PolicyShadow,
        engine.EmaMacdHist1mShadow, engine.ExperimentalRiskShadow,
        engine.BotFullExitPosition, engine.CBReplayClock)}
    files.add(str(Path(engine.__file__).resolve()))
    proof = {'stage':stage, 'kind':kind, 'path':path,
             'module_sha256':{f:sha(Path(f)) for f in sorted(files)}, **extra}
    save(S/'provenance'/f'{stage}_{kind}_{path}.json', proof)

def reference(kind, path):
    file = 'ladder_run2.py' if kind == 'CLUSTER_ACT10' else 'switch_run.py'
    src = (S/'reference'/file).read_text(encoding='utf8')
    src = src.replace("ROOT = Path(__file__).resolve().parents[1]", 'ROOT = Path('+repr(str(ROOT))+')')
    src = src.replace("Path('/home/claude/w/marola_20261030')", 'Path('+repr(str(U))+')')
    for old in ('/home/claude/w/runs2', '/home/claude/w/runs3'):
        src = src.replace('Path('+repr(old)+')', 'Path('+repr(str(S/'reference_runs'))+')')
    for old, new in [('/home/claude/w/out/rotulos_continuos.json','rotulos_continuos.json'),
                     ('/home/claude/w/out/rotulos_continuos_v2.json','rotulos_continuos_v2.json')]:
        src = src.replace('Path('+repr(old)+')', 'Path('+repr(str(S/'reference'/new))+')')
    args = ['C04+ACT10', path] if kind == 'CLUSTER_ACT10' else [
        'MAPA_FINAL', path, 'VIVO', 'NOVO', *(['VERDE', 'ESTRITO'] if kind == 'VERDE' else [])]
    sys.argv = [file, *args]
    namespace = {'__name__':'__main__', '__file__':str(S/'reference'/file)}
    exec(compile(src, str(S/'reference'/file), 'exec'), namespace)
    save(S/'reference_full'/f'{kind}_{path}.json', namespace['r'])
    provenance('reference',kind,path,namespace['e'],
        reference_script_sha256=sha(S/'reference'/file),
        adapted_script_sha256=hashlib.sha256(src.encode('utf8')).hexdigest(),
        adaptations='local paths only; full return value captured without economic changes')

def labels():
    from src.monitor.price_structure import Hour, classify, WAVE_V2, MAROLA_90_V2
    official = read(S/'reference/rotulos_continuos_v2.json')
    results = {}
    for tf, rule, key in [('15m', WAVE_V2,'onda_15m'),('5m',MAROLA_90_V2,'marola90_5m')]:
        candles = [json.loads(line) for line in (U/'market'/f'{tf}.jsonl').open(encoding='utf8')]
        by = {int(c['open_time_ms']): Hour(int(c['open_time_ms']),c['high'],c['low']) for c in candles}
        values = {}
        for k, expected in official[key].items():
            at = int(k)
            window = [by[t] for t in range(at-rule.count*rule.interval_ms,at,rule.interval_ms) if t in by]
            actual = classify(window,at,'REPLAY',rule)['label']
            if actual != expected:
                raise ValueError(f'Label divergence {key} {at}: {actual} != {expected}')
            values[k] = actual
        results[key] = values
    save(S/'labels_verified.json', results)
    save(S/'LABEL_PARITY.json', {'passed':True,'counts':{k:len(v) for k,v in results.items()}})

def candidate(kind, path):
    # Import runtime first: replay12's historical sys.path must not bind frozen src.
    from src.monitor.keyed_shadow import KeyedShadow, EmaMacdClusterAct10Shadow
    from src.monitor.forward_experiment_shadows import FastDropEmaShadow
    import replay12_engine as e
    lab = read(S/'labels_verified.json')
    cfg = read(U/'config.json')
    import yaml
    current = yaml.safe_load((ROOT/'config/config.yaml').read_text(encoding='utf8'))
    cfg['instrumentation'].update(current['instrumentation'])
    original = e.make

    class Runtime(KeyedShadow, e.Core):
        _close_and_record = e.Risk._close_and_record
        def _house(self):
            self._decision = {'wave_close':lab['onda_15m'].get(str(self.now//900000*900000)),
                              'marola_90_close':lab['marola90_5m'].get(str(self.now//300000*300000))}
            return KeyedShadow._house(self)
        def _open(self, signal, bucket):
            e.Memory._open(self,signal,bucket)
            p = self.positions[-1]
            p.entry_house = deepcopy(self._entry_house)

    class Cluster(EmaMacdClusterAct10Shadow, e.Core):
        _close_and_record = e.Risk._close_and_record

    def make(arm, config, bps, start):
        o = original('EMA_MACD_HIST_1M_SHADOW',config,bps,start)
        o.__class__ = Cluster if kind == 'CLUSTER_ACT10' else Runtime
        key = {'CLUSTER_ACT10':'be_off_cb_ema_macd_cluster_act10_shadow',
               'VIVO':'chaveado_vivo_shadow','VERDE':'chaveado_vivo_verde_shadow'}[kind]
        o.settings = deepcopy(config['instrumentation'][key])
        o.arm=o.strategy=o.shadow_kind=o.pair_prefix=kind
        o.experiment = 'HS_BEAR_CLUSTER_EXIT' if kind == 'CLUSTER_ACT10' else None
        o._entry_house=None; o._decision={}; o._ready_for_input=True
        return o
    e.make = make
    e.FastDropEmaShadow=FastDropEmaShadow
    run_src = inspect.getsource(e.run).replace("arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW'",'isinstance(o,FastDropEmaShadow)')
    seg_src = inspect.getsource(e.segment).replace("o.arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW'",'isinstance(o,FastDropEmaShadow)')
    exec(compile(run_src,'<candidate-run>','exec'),e.__dict__)
    exec(compile(seg_src,'<candidate-segment>','exec'),e.__dict__)
    man=read(U/'market_manifest.json')
    result=e.run(kind,cfg,[e.Bar(json.loads(line)) for line in (U/'market/1m.jsonl').open()],
                 read(U/'signals.json')['REAL_A'],read(U/'contexts_runtime_projection.json'),
                 read(U/'regimes.json'),'BASE',path,man['start_ms'],man['end_ms'])
    save(S/'candidate_runs'/f'{kind}_{path}.json',result)
    provenance('candidate',kind,path,e,
        keyed_source_sha256=sha(ROOT/'src/monitor/keyed_shadow.py'),
        structure_source_sha256=sha(ROOT/'src/monitor/price_structure.py'),
        yaml_sha256=sha(ROOT/'config/config.yaml'),
        run_adapter_sha256=hashlib.sha256(run_src.encode('utf8')).hexdigest(),
        segment_adapter_sha256=hashlib.sha256(seg_src.encode('utf8')).hexdigest(),
        adapters='in-memory replay positions/ledger; runtime admission, ladder and reducer methods; FAST first-crossing sampling enabled; verified causal labels supplied',
        offline_enabled=True, live_activation_changed=False)

def compare(kind,path):
    file = ('C04+ACT10' if kind=='CLUSTER_ACT10' else
            'MAPA_FINAL_VIVO'+('_VERDE' if kind=='VERDE' else '')+'_NOVO'+('_ESTRITO' if kind=='VERDE' else ''))
    a=read(S/'candidate_runs'/f'{kind}_{path}.json')
    b=read(S/'reference_full'/f'{kind}_{path}.json') if (S/'reference_full'/f'{kind}_{path}.json').exists() else read(S/'reference_runs'/f'{file}_{path}.json')
    def normalized(rows):
        return [{k:v for k,v in r.items() if k!='pair_id'} for r in rows]
    aa,bb=normalized(a['records']), normalized(b['records'])
    diff=None
    for i,(x,y) in enumerate(zip(aa,bb)):
        if x!=y:
            diff={'trade':i,'source':x['source'],'fields':{k:[x[k],y.get(k)] for k in x if x[k]!=y.get(k)}}
            break
    if diff is None and len(aa)!=len(bb):
        diff={'count_candidate':len(aa),'count_reference':len(bb)}
    expected = {'VIVO':{'LOW_FIRST':1340,'HIGH_FIRST':1341},
                'VERDE':{'LOW_FIRST':274,'HIGH_FIRST':274},
                'CLUSTER_ACT10':{'LOW_FIRST':1020,'HIGH_FIRST':1020}}[kind][path]
    if diff is None and len(bb)!=expected:
        diff={'expected_count_from_errata':expected,'reference_count':len(bb)}
    additional = {}
    for key in ('stats', 'admissions', 'equity_minute', 'daily_equity', 'daily_contributions'):
        if key in b:
            additional[key] = a[key] == b[key]
            if not additional[key] and diff is None:
                diff={'field':key,'candidate_count':len(a[key]),'reference_count':len(b[key])}
    if diff and 'admissions' in b:
        diff['first_admission_difference'] = next(({'candidate':x,'reference':y}
            for x,y in zip(a['admissions'],b['admissions']) if x!=y), None)
        diff['first_economic_admission_difference'] = next(({'candidate':x,'reference':y}
            for x,y in zip(a['admissions'],b['admissions']) if x['accepted']!=y['accepted']), None)
    proof={'passed':diff is None,'kind':kind,'path':path,'candidate_count':len(aa),
           'reference_count':len(bb),'difference':diff,'excluded_field':'pair_id (wallet identity)',
           'expected_count_from_errata':expected,'all_trade_fields_exact_except_pair_id':aa==bb,
           'checked_trade_fields':sorted(set().union(*(r.keys() for r in aa))),
           'additional_checks':additional,
           'candidate_sha256':sha(S/'candidate_runs'/f'{kind}_{path}.json'),
           'reference_full_sha256':sha(S/'reference_full'/f'{kind}_{path}.json') if (S/'reference_full'/f'{kind}_{path}.json').exists() else None,
           'spec_sha256':sha(S/'reference/ESPEC_BOT_20261101_v2.md'),
           'errata_sha256':sha(S/'reference/ERRATA_20261101.md') if (S/'reference/ERRATA_20261101.md').exists() else None}
    save(S/f'PARITY_{kind}_{path}.json',proof)
    print(json.dumps(proof),flush=True)
    if diff: raise SystemExit(2)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['labels','reference','candidate','compare'])
    p.add_argument('--kind',choices=['VIVO','VERDE','CLUSTER_ACT10']);p.add_argument('--path',choices=['LOW_FIRST','HIGH_FIRST'])
    p.add_argument('--output-root', type=Path, default=S)
    args=p.parse_args()
    S=args.output_root.resolve()
    if args.stage=='labels':labels()
    else:globals()[args.stage](args.kind,args.path)
