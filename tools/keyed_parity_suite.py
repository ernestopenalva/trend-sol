"""Run six isolated offline parity cases; stop all owned children on divergence."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'data/analysis/keyed_20261101_errata'
CASES = [(kind,path) for kind in ('VIVO','VERDE','CLUSTER_ACT10')
         for path in ('LOW_FIRST','HIGH_FIRST')]
STOP = threading.Event()
LOCK = threading.Lock()
CHILDREN = set()

def dump(name, value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')

def launch(stage, kind, path):
    log = OUT/f'{stage}_{kind}_{path}.log'
    with log.open('w',encoding='utf8') as stream:
        with LOCK:
            if STOP.is_set():
                return None
            child = subprocess.Popen([sys.executable,str(ROOT/'tools/keyed_parity.py'),stage,
                '--kind',kind,'--path',path,'--output-root',str(OUT)],
                cwd=ROOT,stdout=stream,stderr=subprocess.STDOUT)
            CHILDREN.add(child)
        try:
            return child.wait()
        finally:
            with LOCK:
                CHILDREN.discard(child)

def halt():
    with LOCK:
        STOP.set()
        for child in list(CHILDREN):
            if child.poll() is None:
                child.terminate()

def run_case(case):
    kind,path = case
    if STOP.is_set():
        return {'kind':kind,'path':path,'status':'NOT_STARTED'}
    print('CASE_START',kind,path,flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(launch,stage,kind,path) for stage in ('reference','candidate')]
        codes = [future.result() for future in futures]
    if any(code != 0 for code in codes):
        halt()
        return {'kind':kind,'path':path,'status':'EXECUTION_FAILED','return_codes':codes}
    code = launch('compare',kind,path)
    if code != 0:
        halt()
        return {'kind':kind,'path':path,'status':'DIVERGENCE','return_code':code}
    proof = json.loads((OUT/f'PARITY_{kind}_{path}.json').read_text(encoding='utf8'))
    print('CASE_PASS',kind,path,proof['candidate_count'],flush=True)
    return {'kind':kind,'path':path,'status':'PASS','proof':f'PARITY_{kind}_{path}.json'}

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    files = [ROOT/'tools/keyed_parity.py',ROOT/'tools/keyed_parity_suite.py',
             ROOT/'src/monitor/keyed_shadow.py',ROOT/'src/monitor/price_structure.py',
             ROOT/'config/config.yaml',*(OUT/'reference').iterdir()]
    # Frozen evidence for this run; no secrets/config credentials are included.
    hashes = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    dump('INPUT_CODE_HASHES.json',hashes)
    results=[]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(run_case,case) for case in CASES]
            for future in concurrent.futures.as_completed(futures):
                results.append(future.result())
                dump('SUITE_STATUS.json',{'started_utc':started,'passed':False,
                    'status':'STOPPED' if STOP.is_set() else 'RUNNING','cases':results})
    except BaseException:
        halt()
        raise
    unchanged = all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==value for p,value in hashes.items())
    passed = len(results)==6 and all(r['status']=='PASS' for r in results) and unchanged
    dump('PARIDADE_COMPLETA.json',{'started_utc':started,
        'finished_utc':datetime.now(timezone.utc).isoformat(),'passed':passed,
        'expected_cases':6,'completed_passes':sum(r['status']=='PASS' for r in results),
        'source_files_unchanged_during_suite':unchanged,'cases':results,
        'deployment_performed':False,'restart_performed':False,'git_commands_performed':False})
    print('SUITE_DONE',passed,flush=True)
    return 0 if passed else 2

if __name__=='__main__':
    raise SystemExit(main())
