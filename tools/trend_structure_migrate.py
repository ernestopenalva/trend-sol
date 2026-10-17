"""Offline, reversible telemetry-only migration. Never run against a running bot."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.monitor.price_structure import VERSION, fields, timestamp_ms, TIDE, WAVE
from tools.price_structure_backfill import load_buffer, DEFAULT_SINCE
from src.monitor.cb_persistence import sync_directory

KEYS={f'trend_{p}{s}' for p in ('open','close') for s in ('','_at','_details')}
WAVE_KEYS={f'wave_{p}{s}' for p in ('open','close') for s in ('','_at','_details')}|{'wave_rule_version'}


def economic(value,excluded=KEYS):
    if isinstance(value,list):return [economic(x,excluded) for x in value]
    if isinstance(value,dict):return {k:economic(v,excluded) for k,v in value.items() if k not in excluded}
    return value


def fingerprint(value,excluded=KEYS):
    return hashlib.sha256(json.dumps(economic(value,excluded),sort_keys=True,separators=(',',':')).encode()).hexdigest()


def file_digest(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()


def rows_fingerprint(rows,excluded=KEYS):
    # Same canonical list hash as before, without retaining history payloads.
    h=hashlib.sha256();h.update(b'[')
    for i,(_,row) in enumerate(rows):
        if i:h.update(b',')
        h.update(json.dumps(economic(row,excluded),sort_keys=True,separators=(',',':')).encode())
    h.update(b']');return h.hexdigest()


def sqlite_identity(path):
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    try:
        return dict(schema=db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall(),
            history_keys=db.execute('SELECT rowid,kind,ordinal,revision FROM history ORDER BY rowid').fetchall(),
            pending_keys=db.execute('SELECT rowid,singleton FROM pending ORDER BY rowid').fetchall(),
            metadata=db.execute('SELECT key,value FROM metadata ORDER BY key').fetchall())
    finally:db.close()


def read_rows(path):
    if path.suffix=='.sqlite':
        # The offline preflight rejects WAL/SHM. Immutable read avoids creating
        # new WAL/SHM files simply by inspecting a clean WAL-mode database image.
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'history','pending','metadata'}<=tables:raise ValueError(f'Unknown SQLite schema: {path}')
            for table in ('history','pending'):
                for rid,payload in db.execute(f'SELECT rowid,payload FROM {table}'):
                    yield (table,rid),json.loads(payload)
        db.close()
    elif path.suffix=='.jsonl':
        with path.open(encoding='utf-8') as f:
            for i,line in enumerate(f):
                if line.strip():yield i,json.loads(line)
    else:
        yield 0,json.loads(path.read_text(encoding='utf-8'))


def records(value):
    if isinstance(value,list):
        for row in value:yield from records(row)
    elif isinstance(value,dict):
        if value.get('pair_id') and (value.get('opened_at') or value.get('open_ts')):yield value
        for k,v in value.items():
            if k not in KEYS:yield from records(v)


def build_index(documents):
    index={}
    for doc in documents:
        for r in records(doc):
            if r.get('symbol')!='SOLUSDT':continue
            key=str(r['pair_id']);opened=r.get('opened_at') or r.get('open_ts')
            prior=index.get(key)
            if prior and timestamp_ms(prior.get('opened_at') or prior.get('open_ts'))!=timestamp_ms(opened):
                raise ValueError(f'Ambiguous pair timestamp: {key}')
            # Resolved authoritative record wins over a non-atomic OPEN snapshot.
            if not prior or r.get('closed_at') or r.get('close_ts'):
                index[key]={k:r[k] for k in ('pair_id','symbol','opened_at','open_ts','closed_at','close_ts') if k in r}
    return index


def transform(value,buffer,since,index,rule=TIDE):
    keys=KEYS if rule==TIDE else WAVE_KEYS
    if isinstance(value,list):return [transform(r,buffer,since,index,rule) for r in value]
    if not isinstance(value,dict):return value
    result={k:transform(v,buffer,since,index,rule) for k,v in value.items() if k not in keys}
    direct=value.get('opened_at') or value.get('open_ts')
    prior=index.get(str(value.get('pair_id')), {})
    is_event=value.get('event') in ('OPEN','CLOSE','EXPERIMENTAL_CLOSE')
    opened=direct or ((prior.get('opened_at') or prior.get('open_ts')) if is_event or keys.intersection(value) else None)
    closed=value.get('closed_at') or value.get('close_ts')
    if not closed and value.get('event') in ('CLOSE','EXPERIMENTAL_CLOSE'):
        closed=prior.get('closed_at') or prior.get('close_ts')
    symbol=value.get('symbol') or prior.get('symbol')
    if not opened or symbol!='SOLUSDT':return result
    if timestamp_ms(opened)<since and closed and timestamp_ms(closed)<since:return result
    # An OPEN projection of a trade closed before the marker isn't a carry-over.
    prior_close=prior.get('closed_at') or prior.get('close_ts')
    if not direct and prior_close and timestamp_ms(prior_close)<since:return result
    for phase,at in [('open',opened),('close',closed)]:
        if at:
            snap=buffer.snapshot(at,'BACKFILL')
            if snap.get('missing_hours',snap.get('missing_candles')):raise ValueError(f'Missing market data for {value.get("pair_id")}:{phase}')
            result.update(fields(snap,phase,rule))
    return result


def files(root):
    found=[]
    for folder,patterns in [('data/trades',('*.jsonl',)),('data/state',('*.json','*.history.sqlite','*.pending')),
                            ('data/telemetry',('*events.jsonl','price_structure_backfill.jsonl','wave_structure_backfill.jsonl'))]:
        for pattern in patterns:found.extend((root/folder).glob(pattern))
    return sorted(set(p for p in found if p.is_file()))


def atomic_json(path,value):
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(value,f,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    sync_directory(path.parent)


def durable_copy(source,target):
    shutil.copy2(source,target)
    with target.open('r+b') as f:os.fsync(f.fileno())
    sync_directory(target.parent)


def rollback(root,backup):
    m=json.loads((backup/'manifest.json').read_text())
    excluded=set(m.get('allowed_fields',KEYS))
    if str(root.resolve())!=m['root']:raise ValueError('Rollback root mismatch')
    for item in m['files']:
        target=root/item['path']
        source=backup/'original'/item['path']
        if not target.resolve().is_relative_to(root.resolve()) or not source.resolve().is_relative_to(backup.resolve()):
            raise ValueError('Invalid backup path')
        if file_digest(source)!=item['before']:
            raise ValueError('Backup checksum mismatch')
        if target.suffix=='.sqlite' and any(target.with_name(target.name+s).exists() for s in ('-wal','-shm')):
            raise ValueError('Rollback requires cleanly stopped SQLite')
        if rows_fingerprint(read_rows(target),excluded)!=item['economic_sha256']:
            raise ValueError('Economic state advanced; refusing destructive historical rollback')
        if item.get('sqlite_identity_sha256') and fingerprint(sqlite_identity(target))!=item['sqlite_identity_sha256']:
            raise ValueError('SQLite identity advanced; refusing historical rollback')
    # Caller must stop the process for both apply and rollback.
    for item in m['files']:
        target=root/item['path']
        tmp=target.with_name(target.name+'.trend-rollback-tmp')
        durable_copy(backup/'original'/item['path'],tmp)
        os.replace(tmp,target);sync_directory(target.parent)
    m['status']='ROLLED_BACK';atomic_json(backup/'manifest.json',m)


def migrate(root,candles,backup,*,since=DEFAULT_SINCE, fail_after=None,rule=TIDE):
    root=root.resolve();backup=backup.resolve()
    excluded=KEYS if rule==TIDE else WAVE_KEYS
    if backup.is_relative_to(root/'data'):raise ValueError('Backup must be outside operational data/')
    if backup.exists():
        m=json.loads((backup/'manifest.json').read_text())
        if m.get('root')!=str(root) or m.get('since')!=since or m.get('candles_sha256')!=file_digest(candles):
            raise ValueError('Backup belongs to different migration inputs')
        if m['status']=='APPLIED' and m['rule_version']==rule.version:
            if all(file_digest(root/x['path'])==x['after'] for x in m['files']):return m
        raise ValueError('Existing migration differs or incomplete; rollback before retry')
    targets=files(root)
    # A live WAL means a raw filesystem copy is unsafe. Never truncate/checkpoint it here.
    for p in targets:
        if p.suffix=='.sqlite' and (p.with_name(p.name+'-wal').exists() or p.with_name(p.name+'-shm').exists()):
            raise ValueError(f'SQLite WAL/SHM present; stop cleanly before offline migration: {p}')
    index=build_index(doc for p in targets for _,doc in read_rows(p))
    buffer=load_buffer(candles,rule);cut=timestamp_ms(since)
    changes={}
    # Validate every record before creating a backup or touching production.
    for p in targets:
        changed=False
        for _,doc in read_rows(p):
            updated=transform(doc,buffer,cut,index,rule)
            if fingerprint(doc,excluded)!=fingerprint(updated,excluded):raise ValueError('Economic invariance failed')
            changed=changed or doc!=updated
        if changed:changes[p]=file_digest(p)
    backup.mkdir(parents=True)
    m=dict(root=str(root),rule_version=rule.version,since=since,status='PREPARING',allowed_fields=sorted(excluded),
           candles_sha256=file_digest(candles),files=[])
    for p,before in changes.items():
        if file_digest(p)!=before:raise ValueError('Input changed during preparation')
        rel=p.relative_to(root);original=backup/'original'/rel;stage=backup/'staged'/rel
        original.parent.mkdir(parents=True,exist_ok=True);stage.parent.mkdir(parents=True,exist_ok=True)
        durable_copy(p,original);durable_copy(p,stage)
        if p.suffix=='.sqlite':
            identity=sqlite_identity(p)
            with sqlite3.connect(stage) as db:
                for (table,rid),original_doc in read_rows(p):
                    doc=transform(original_doc,buffer,cut,index,rule)
                    db.execute(f'UPDATE {table} SET payload=? WHERE rowid=?',(json.dumps(doc,ensure_ascii=False),rid))
                if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('SQLite integrity failed')
            db.close()
            with stage.open('r+b') as f:os.fsync(f.fileno())
            if sqlite_identity(stage)!=identity:raise ValueError('SQLite schema/keys/metadata changed')
        else:
            with stage.open('w',encoding='utf-8') as f:
                if p.suffix=='.jsonl':
                    for _,doc in read_rows(p):f.write(json.dumps(transform(doc,buffer,cut,index,rule),ensure_ascii=False)+'\n')
                else:json.dump(transform(next(read_rows(p))[1],buffer,cut,index,rule),f,ensure_ascii=False)
                f.flush();os.fsync(f.fileno())
        original_hash=rows_fingerprint(read_rows(p),excluded)
        if rows_fingerprint(read_rows(stage),excluded)!=original_hash:raise ValueError('Staged economic invariance failed')
        m['files'].append(dict(path=str(rel),before=file_digest(p),
                              after=file_digest(stage),
                              economic_sha256=original_hash))
        if p.suffix=='.sqlite':m['files'][-1]['sqlite_identity_sha256']=fingerprint(identity)
    # Persist every newly created parent directory before touching operational files.
    for folder in sorted([p for p in backup.rglob('*') if p.is_dir()],key=lambda p:len(p.parts),reverse=True):
        sync_directory(folder)
    sync_directory(backup);sync_directory(backup.parent)
    m['status']='PREPARED';atomic_json(backup/'manifest.json',m)
    try:
        for i,x in enumerate(m['files']):
            p=root/x['path']
            if file_digest(p)!=x['before']:raise ValueError('Input changed; process may be running')
            tmp=p.with_name(p.name+'.trend-migration-tmp')
            durable_copy(backup/'staged'/x['path'],tmp)
            if file_digest(tmp)!=x['after']:raise ValueError('Staged file checksum mismatch')
            os.replace(tmp,p)
            sync_directory(p.parent)
            if fail_after is not None and i+1>=fail_after:raise RuntimeError('Injected failure')
        m['status']='APPLIED';atomic_json(backup/'manifest.json',m)
    except BaseException:
        rollback(root,backup)
        raise
    return m


def verify(root,backup):
    m=json.loads((backup/'manifest.json').read_text())
    excluded=set(m.get('allowed_fields',KEYS))
    if m['root']!=str(root.resolve()) or m['status']!='APPLIED':raise ValueError('Migration not applied to this root')
    for item in m['files']:
        p=root/item['path']
        if file_digest(p)!=item['after']:raise ValueError(f'Post-migration file changed: {p}')
        if rows_fingerprint(read_rows(p),excluded)!=item['economic_sha256']:raise ValueError('Economic check failed')
        if item.get('sqlite_identity_sha256') and fingerprint(sqlite_identity(p))!=item['sqlite_identity_sha256']:
            raise ValueError('SQLite keys/metadata check failed')
    return dict(status='VERIFIED',files=len(m['files']),rule_version=m['rule_version'],economic_invariance=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,required=True);p.add_argument('--backup',type=Path,required=True)
    p.add_argument('--candles',type=Path);p.add_argument('--since',default=DEFAULT_SINCE)
    p.add_argument('--layer',choices=('trend','wave'),default='trend')
    p.add_argument('--offline-confirmed',action='store_true',required=True)
    mode=p.add_mutually_exclusive_group()
    mode.add_argument('--rollback',action='store_true');mode.add_argument('--verify',action='store_true');args=p.parse_args()
    if args.rollback:rollback(args.root,args.backup);print('ROLLED_BACK');return
    if args.verify:print(json.dumps(verify(args.root,args.backup),indent=2));return
    if not args.candles:p.error('--candles required for apply')
    print(json.dumps(migrate(args.root,args.candles,args.backup,since=args.since,rule=WAVE if args.layer=='wave' else TIDE),indent=2))


if __name__=='__main__':main()
