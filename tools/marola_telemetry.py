"""Observational MAROLA preparation, offline migration and LIVE reconciliation.

No trading/replay engine is called. Migration requires a cleanly stopped bot.
Only entry MAROLA fields change in ledgers and CB ledger-history projections.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.monitor.price_structure import (MAROLAS, MAROLA_KEYS, StructureBuffer, Hour,
    timestamp_ms, marola_fields)
from src.monitor.cb_persistence import sync_directory
from tools.trend_structure_migrate import (read_rows, rows_fingerprint, sqlite_identity,
    file_digest, durable_copy, atomic_json, rollback)

WIDTH = 300_000


def state_rows(root):
    def positions(value):
        if isinstance(value,list):
            for item in value:yield from positions(item)
        elif isinstance(value,dict):
            if entry(value) and value.get('pair_id'):yield value
            for key,v in value.items():
                if key not in MAROLA_KEYS:yield from positions(v)
    for path in (root/'data/state').glob('*.json'):
        for row in positions(json.loads(path.read_text(encoding='utf-8'))):
            yield str(path.relative_to(root)),row


def targets(root):
    return sorted([*(root/'data/trades').glob('*.jsonl'),
                   *(root/'data/state').glob('*.history.sqlite')])


def trade_rows(path):
    if path.suffix == '.sqlite':
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
            for rid, payload in db.execute("SELECT rowid,payload FROM history WHERE kind='ledger'"):
                yield rid, json.loads(payload)
    else:
        for i, row in read_rows(path):
            yield i, row


def entry(row):
    if not isinstance(row, dict):
        return None
    at = row.get('opened_at') or row.get('open_ts')
    return (str(row['symbol']), timestamp_ms(at)) if at and row.get('symbol') else None


def load_buffers(folder, symbols):
    result = {}
    for symbol in symbols:
        if not symbol.isalnum():
            raise ValueError('Invalid symbol')
        path = folder/(symbol+'_5m.jsonl')
        candles = []
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            at = int(row['open_time_ms'])
            if at % WIDTH or int(row['close_time_ms']) != at+WIDTH-1:
                raise ValueError('Invalid closed 5m candle')
            candles.append(Hour(at, float(row['high']), float(row['low']), True, at+WIDTH))
        result[symbol] = {r.prefix: StructureBuffer(candles, rule=r) for r in MAROLAS}
    return result


def annotate(row, buffers):
    key = entry(row)
    if key is None:
        raise ValueError('Ledger trade missing symbol/opened_at; no row silently skipped')
    symbol, at = key
    result = dict(row)
    for rule in MAROLAS:
        # Native prospective provenance must never be replaced by backfill.
        prior = row.get(rule.prefix+'_open_details') or {}
        if prior.get('version') == rule.version and prior.get('source') == 'ao_vivo':
            continue
        snap = buffers[symbol][rule.prefix].snapshot(at, 'reclassificado')
        if snap.get('missing_candles') or snap.get('status') == 'INVALID_CANDLES':
            raise ValueError(f'Missing/invalid candles: {symbol} {at} {rule.prefix}')
        snap['availability_assumption'] = 'PUBLIC_CANDLE_CLOSE; historical feed latency not reconstructed'
        result.update(marola_fields(snap, rule))
    return result


def prepare(root, folder, client=None, seed=None):
    """Fetch only required missing public 5m slots, independently per symbol."""
    if seed:
        audit=json.loads((seed.parent.parent/'market_manifest.json').read_text(encoding='utf-8'))
        expected=(audit.get('granularities',audit).get('5m') or {}).get('sha256')
        if not expected or file_digest(seed)!=expected:
            raise ValueError('Audited SOL 5m seed hash mismatch')
    if client is None:
        from tools.market_selection_study import BinancePublicClient
        client = BinancePublicClient('https://api.binance.com', 30)
    needed = defaultdict(set)
    # Preparation may run while the bot is live: use JSONL projections, not
    # immutable SQLite reads that would ignore an active WAL. The stopped
    # migration validates the authoritative history too and refuses any gap.
    all_rows = (row for path in targets(root) if path.suffix!='.sqlite' for _,row in trade_rows(path))
    from itertools import chain
    for row in chain(all_rows, (r for _,r in state_rows(root))):
        key = entry(row)
        if key is None:
            raise ValueError('Missing opening timestamp')
        symbol, stamp = key
        end = stamp//WIDTH*WIDTH
        needed[symbol].update(range(end-18*WIDTH, end, WIDTH))
    folder.mkdir(parents=True, exist_ok=True)
    for symbol, slots in sorted(needed.items()):
        if not symbol.isalnum():
            raise ValueError('Invalid symbol')
        path = folder/(symbol+'_5m.jsonl')
        inputs = [path] if path.exists() else []
        if seed and symbol == 'SOLUSDT':
            inputs.append(seed)
        rows = {}
        for source in inputs:
            for line in source.read_text(encoding='utf-8-sig').splitlines():
                if not line.strip():continue
                row = json.loads(line); at = int(row['open_time_ms'])
                if at in rows and (rows[at]['high'], rows[at]['low']) != (row['high'], row['low']):
                    raise ValueError('Conflicting seed/public candle')
                rows[at] = row
        missing = sorted(slots-set(rows))
        while missing:
            first = missing[0]; stop = first+WIDTH
            while stop in slots and stop not in rows and stop-first < 1000*WIDTH:
                stop += WIDTH
            batch = client.get('/api/v3/klines', dict(symbol=symbol, interval='5m',
                startTime=first, endTime=stop-1, limit=1000))
            for k in batch:
                at = int(k[0])
                if first <= at < stop:
                    rows[at] = dict(open_time_ms=at, close_time_ms=int(k[6]),
                        high=float(k[2]), low=float(k[3]))
            # Persist progress even on incomplete responses; never touch ledgers.
            tmp = path.with_suffix('.tmp')
            with tmp.open('w', encoding='utf-8') as f:
                for at in sorted(rows):f.write(json.dumps(rows[at])+'\n')
                f.flush();os.fsync(f.fileno())
            os.replace(tmp,path)
            if any(t not in rows for t in range(first,stop,WIDTH)):
                raise ValueError(f'Incomplete Binance history: {symbol}; migration not permitted')
            missing = sorted(slots-set(rows))
        if not path.exists():
            with path.open('x',encoding='utf-8') as f:
                for at in sorted(rows):f.write(json.dumps(rows[at])+'\n')
        print(f'candles ready | {symbol} | {len(slots)} required',flush=True)
    return dict(symbols=len(needed), source='Binance public Spot',
                caches={s:file_digest(folder/(s+'_5m.jsonl')) for s in needed})


def migrate(root, candles, backup, *, stopped=False, fail_after=None):
    if not stopped:
        raise ValueError('Stop the bot cleanly; explicit --stopped confirmation required')
    root=root.resolve();backup=backup.resolve()
    if backup.is_relative_to(root/'data') or backup.exists():
        raise ValueError('Use a NEW backup outside data/')
    paths=targets(root)
    for path in paths:
        if path.suffix=='.sqlite' and any(path.with_name(path.name+s).exists() for s in ('-wal','-shm')):
            raise ValueError('WAL/SHM present; clean shutdown required')
    symbols={entry(row)[0] for p in paths for _,row in trade_rows(p) if entry(row)}
    buffers=load_buffers(candles,symbols)
    cache_hashes={s:file_digest(candles/(s+'_5m.jsonl')) for s in symbols}
    # Full validation precedes all writes/backups. Missing market history fails closed.
    inputs={p:file_digest(p) for p in paths}; changed=[]; counts=Counter()
    for p in paths:
        for _,row in trade_rows(p):
            new=annotate(row,buffers)
            if ({k:v for k,v in new.items() if k not in MAROLA_KEYS} !=
                {k:v for k,v in row.items() if k not in MAROLA_KEYS}):
                raise ValueError('A non-MAROLA field changed during preflight')
            counts['rows']+=1
            if row!=new and p not in changed:changed.append(p)
    protected={str(p.relative_to(root)):file_digest(p) for p in
               [*(root/'data/state').glob('*.json'),*(root/'data/state').glob('*.pending')]}
    backup.mkdir(parents=True)
    manifest=dict(root=str(root),status='PREPARING',rule_version='MAROLA_ENTRY_V1',allowed_fields=sorted(MAROLA_KEYS),
        rules={r.prefix:r.version for r in MAROLAS},candles=cache_hashes,
        protected_states=protected,files=[],counts=dict(counts))
    for p in changed:
        if file_digest(p)!=inputs[p]:raise ValueError('Source advanced while preparing')
        rel=p.relative_to(root);original=backup/'original'/rel;stage=backup/'staged'/rel
        original.parent.mkdir(parents=True,exist_ok=True);stage.parent.mkdir(parents=True,exist_ok=True)
        durable_copy(p,original);durable_copy(p,stage)
        identity=sqlite_identity(p) if p.suffix=='.sqlite' else None
        if identity is not None:
            with closing(sqlite3.connect(stage)) as db:
                for rid,row in trade_rows(p):
                    db.execute('UPDATE history SET payload=? WHERE rowid=?',
                        (json.dumps(annotate(row,buffers),ensure_ascii=False),rid))
                db.commit()
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('SQLite integrity')
            if sqlite_identity(stage)!=identity:raise ValueError('SQLite identity changed')
        else:
            with stage.open('w',encoding='utf-8') as f:
                for _,row in trade_rows(p):f.write(json.dumps(annotate(row,buffers),ensure_ascii=False)+'\n')
                f.flush();os.fsync(f.fileno())
        original_economic=rows_fingerprint(read_rows(p),MAROLA_KEYS)
        if rows_fingerprint(read_rows(stage),MAROLA_KEYS)!=original_economic:
            raise ValueError('A non-MAROLA field changed')
        item=dict(path=str(rel),before=inputs[p],after=file_digest(stage),economic_sha256=original_economic)
        if identity is not None:
            item['sqlite_identity_sha256']=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        manifest['files'].append(item)
    for folder in sorted([p for p in backup.rglob('*') if p.is_dir()],key=lambda p:len(p.parts),reverse=True):
        sync_directory(folder)
    manifest['status']='PREPARED';atomic_json(backup/'manifest.json',manifest)
    try:
        for symbol,digest in cache_hashes.items():
            if file_digest(candles/(symbol+'_5m.jsonl'))!=digest:raise ValueError('Market cache changed')
        for i,item in enumerate(manifest['files']):
            p=root/item['path']
            if file_digest(p)!=item['before']:raise ValueError('Ledger/history advanced; bot may be running')
            tmp=p.with_name(p.name+'.marola-tmp');durable_copy(backup/'staged'/item['path'],tmp)
            os.replace(tmp,p);sync_directory(p.parent)
            if fail_after is not None and i+1>=fail_after:raise RuntimeError('Injected failure')
        for rel,digest in protected.items():
            if file_digest(root/rel)!=digest:raise ValueError('Checkpoint/pending state changed')
        manifest['status']='APPLIED';atomic_json(backup/'manifest.json',manifest)
    except BaseException:
        if any(file_digest(root/rel)!=digest for rel,digest in protected.items()):
            manifest['status']='RECOVERY_REQUIRED_EXTERNAL_STATE_ADVANCED'
            atomic_json(backup/'manifest.json',manifest)
            raise RuntimeError('External state advanced; refusing historical rollback')
        rollback(root,backup)
        raise
    return manifest


def check_protected(root, manifest):
    if str(root.resolve())!=manifest['root']:
        raise ValueError('Root mismatch')
    for rel,digest in manifest['protected_states'].items():
        if file_digest(root/rel)!=digest:
            raise ValueError('Checkpoint/pending advanced; stop and audit before proceeding')
    for item in manifest['files']:
        p=root/item['path']
        if p.suffix=='.sqlite' and any(p.with_name(p.name+s).exists() for s in ('-wal','-shm')):
            raise ValueError('Live WAL/SHM present; clean stop required')


def verify_migration(root, backup):
    from tools.trend_structure_migrate import verify
    manifest=json.loads((backup/'manifest.json').read_text())
    check_protected(root,manifest)
    result=verify(root,backup)
    result['checkpoints_pending_unchanged']=True
    return result


def compare(root, candles, since):
    cut=timestamp_ms(since);rows=[]
    for p in (root/'data/trades').glob('*.jsonl'):
        rows.extend((str(p.relative_to(root)),r) for _,r in trade_rows(p))
    rows.extend(state_rows(root))
    recent=[(p,r) for p,r in rows if entry(r) and entry(r)[1]>=cut]
    buffers=load_buffers(candles,{entry(r)[0] for _,r in recent});differences=[];checked=0
    for path,row in recent:
        symbol,at=entry(row)
        for rule in MAROLAS:
            actual=row.get(rule.prefix+'_open_details') or {}
            expected=buffers[symbol][rule.prefix].snapshot(at,'reclassificado');checked+=1
            if (actual.get('source')!='ao_vivo' or actual.get('version')!=rule.version or
                row.get(rule.prefix+'_open_source')!='ao_vivo' or
                row.get(rule.prefix+'_rule_version')!=rule.version or
                row.get(rule.prefix+'_open_at')!=expected['at'] or
                row.get(rule.prefix+'_open')!=expected['label'] or
                any(actual.get(k)!=expected.get(k) for k in
                    ('at','window_end_ms','latest_closed_ms','highs','lows','highs_class','lows_class'))):
                differences.append(dict(path=path,pair_id=row.get('pair_id'),opened_at=at,
                    rule=rule.version,actual=actual,expected=expected))
    return dict(checked=checked,differences=differences,
                match_pct=100*(checked-len(differences))/checked if checked else None,
                passed=checked>0 and not differences,observational_only=True,
                note='Public closed-candle reconstruction; LIVE gaps/late delivery are reported, never hidden')


def study_parity(study):
    """Technical reproduction of saved label snapshots, never economic replay."""
    buffers=load_buffers_from_study(study)
    checked=0;differences=[]
    for line in (study/'MAROLA_SNAPSHOTS.jsonl').read_text(encoding='utf-8').splitlines():
        saved=json.loads(line)
        for rule in MAROLAS:
            expected=saved['snapshots'][rule.prefix.upper()]
            actual=buffers[rule.prefix].snapshot(saved['at'],'ao_vivo')
            excluded={'version','rule_version','source'}
            checked+=1
            if ({k:v for k,v in actual.items() if k not in excluded} !=
                {k:v for k,v in expected.items() if k not in excluded}):
                differences.append(dict(at=saved['at'],rule=rule.version,
                                        actual=actual,expected=expected))
    return dict(checked=checked,differences=differences,passed=checked>0 and not differences,
                match_pct=100*(checked-len(differences))/checked if checked else None,
                kind='Label geometry only; no economic engine invoked')


def load_buffers_from_study(study):
    path=study/'market/5m.jsonl'
    values=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    candles=[Hour(int(v['open_time_ms']),float(v['high']),float(v['low']),True,
                  int(v['close_time_ms'])+1) for v in values]
    return {r.prefix:StructureBuffer(candles,rule=r) for r in MAROLAS}


def state_snapshot(root):
    """Read-only evidence; run before migration and immediately after restart."""
    states={str(p.relative_to(root)):json.loads(p.read_text(encoding='utf-8'))
            for p in (root/'data/state').glob('*.json')}
    return dict(states=states,note='Full checkpoints for audit; no writes to operational state')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','migrate','verify','rollback','compare','status','study-parity','snapshot'])
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--candles',type=Path)
    parser.add_argument('--seed',type=Path,help='Optional audited SOLUSDT 5m JSONL cache')
    parser.add_argument('--study',type=Path)
    parser.add_argument('--backup',type=Path)
    parser.add_argument('--since',help='Aware ISO timestamp of the LIVE marker')
    parser.add_argument('--output',type=Path,help='NEW audit JSON report')
    parser.add_argument('--stopped',action='store_true')
    args=parser.parse_args()
    if args.output and args.output.exists():parser.error('--output already exists')
    if args.action in ('prepare','migrate','compare') and not args.candles:parser.error('--candles required')
    if args.action in ('migrate','verify','rollback') and not args.backup:parser.error('--backup required')
    if args.action=='prepare':result=prepare(args.root,args.candles,seed=args.seed)
    elif args.action=='migrate':result=migrate(args.root,args.candles,args.backup,stopped=args.stopped)
    elif args.action=='verify':
        result=verify_migration(args.root,args.backup)
    elif args.action=='rollback':
        if not args.stopped:parser.error('Clean stop and --stopped required')
        check_protected(args.root,json.loads((args.backup/'manifest.json').read_text()))
        rollback(args.root,args.backup);result={'status':'ROLLED_BACK'}
    elif args.action=='compare':
        if not args.since:parser.error('--since required')
        result=compare(args.root,args.candles,args.since)
    elif args.action=='study-parity':
        if not args.study:parser.error('--study required')
        result=study_parity(args.study)
    elif args.action=='snapshot':
        result=state_snapshot(args.root)
    else:
        marks=[]
        log=args.root/'logs/system.log'
        for line in log.read_text(encoding='utf-8').splitlines() if log.exists() else ():
            try:r=json.loads(line)
            except ValueError:continue
            if r.get('event')=='marola_live_enabled':marks.append(r)
        live=[row for path in targets(args.root) if path.suffix!='.sqlite' for _,row in trade_rows(path)
              if row.get('marola_90_open_source')=='ao_vivo']
        live.extend(row for _,row in state_rows(args.root) if row.get('marola_90_open_source')=='ao_vivo')
        first=min((entry(row)[1] for row in live),default=None)
        from datetime import datetime,timezone
        from src.console_utils import BRASILIA_TZ
        for row in marks:
            stamp=row.get('enabled_at') or row.get('ts')
            row['enabled_at_brt']=datetime.fromtimestamp(timestamp_ms(stamp)/1000,timezone.utc).astimezone(BRASILIA_TZ).isoformat(timespec='milliseconds')
        result={'live_markers':marks,'rules':{r.prefix:r.version for r in MAROLAS},
            'first_persisted_live_entry_brt':datetime.fromtimestamp(first/1000,timezone.utc).astimezone(BRASILIA_TZ).isoformat(timespec='milliseconds') if first is not None else None}
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    shown = result
    if args.output:
        shown = dict(output=str(args.output),status=result.get('status'),
                     passed=result.get('passed'),checked=result.get('checked'),
                     differences=len(result.get('differences',[])))
    print(json.dumps(shown,ensure_ascii=False,indent=2))
    if args.action in ('compare','study-parity') and not result['passed']:raise SystemExit(1)


if __name__=='__main__':main()
