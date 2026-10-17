"""Prepare ONDA candles/backfill in an analysis directory, never production writes."""
import argparse
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.monitor.price_structure import WAVE,fields,timestamp_ms
from tools.price_structure_backfill import load_buffer,DEFAULT_SINCE,verify_parity
from tools.trend_structure_migrate import files,read_rows,build_index,file_digest


def prepare_candles(base,until,output):
    from tools.market_selection_study import BinancePublicClient
    if output.exists() or output.with_suffix('.manifest.json').exists():raise ValueError('Output exists')
    audit=base.parent.parent/'market_manifest.json'
    if audit.exists():
        expected=json.loads(audit.read_text(encoding='utf-8')).get('15m',{}).get('sha256')
        if expected and file_digest(base)!=expected:raise ValueError('Audited seed hash mismatch')
    rows={}
    with base.open(encoding='utf-8') as f:
        for line in f:
            r=json.loads(line);at=int(r['open_time_ms'])
            if at in rows:raise ValueError('Duplicate base candle')
            rows[at]=r
    width=WAVE.interval_ms
    end=until//width*width
    client=BinancePublicClient('https://api.binance.com',30)
    # Only missing ranges; original audited candles are never replaced.
    cursor=min(rows)
    while cursor<end:
        if cursor in rows:cursor+=width;continue
        stop=cursor+width
        while stop<end and stop not in rows and (stop-cursor)//width<1000:stop+=width
        batch=client.get('/api/v3/klines',dict(symbol='SOLUSDT',interval='15m',startTime=cursor,endTime=stop-1,limit=1000))
        for k in batch:
            at=int(k[0])
            if cursor<=at<stop and at not in rows:
                rows[at]=dict(open_time_ms=at,close_time_ms=int(k[6]),open=float(k[1]),
                              high=float(k[2]),low=float(k[3]),close=float(k[4]))
        if any(t not in rows for t in range(cursor,stop,width)):raise ValueError('Incomplete public candle response')
        cursor=stop
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as f:
        for at,r in sorted(rows.items()):
            if at<end:f.write(json.dumps(r)+'\n')
    load_buffer(output,WAVE)
    return dict(base_sha256=file_digest(base),output_sha256=file_digest(output),end_ms=end,candles=sum(at<end for at in rows))


def reconstruct(root,candles,output,since=DEFAULT_SINCE):
    if output.resolve().is_relative_to((root/'data/trades').resolve()) or output.resolve().is_relative_to((root/'data/state').resolve()):
        raise ValueError('Use analysis directory')
    sources=files(root)
    before={str(p):file_digest(p) for p in sources}
    manifest_path=output.with_suffix('.manifest.json')
    if output.exists() or manifest_path.exists():
        if output.exists() and manifest_path.exists():
            m=json.loads(manifest_path.read_text(encoding='utf-8'))
            if (m.get('source_sha256')==before and m.get('candles_sha256')==file_digest(candles)
                and m.get('since')==since and m.get('rule_version')==WAVE.version
                and m.get('output_sha256')==file_digest(output)):return m
        raise ValueError('Existing artifact differs/incomplete; choose a new audit path')
    index=build_index(doc for p in sources for _,doc in read_rows(p))
    buffer=load_buffer(candles,WAVE);cut=timestamp_ms(since)
    parity=verify_parity(buffer)
    count=closed=unavailable=0
    output.parent.mkdir(parents=True,exist_ok=True)
    with output.open('x',encoding='utf-8') as f:
        for pair,r in sorted(index.items()):
            opened=r.get('opened_at') or r.get('open_ts');end=r.get('closed_at') or r.get('close_ts')
            if timestamp_ms(opened)<cut and end and timestamp_ms(end)<cut:continue
            row=dict(pair_id=pair,symbol=r['symbol'],opened_at=opened,closed_at=end)
            for phase,at in [('open',opened),('close',end)]:
                if at:
                    snap=buffer.snapshot(at,'BACKFILL')
                    unavailable+=snap['label']=='UNAVAILABLE'
                    row.update(fields(snap,phase,WAVE))
            f.write(json.dumps(row)+'\n');count+=1;closed+=bool(end)
    if before!={str(p):file_digest(p) for p in sources}:raise ValueError('Source changed during offline backfill')
    manifest=dict(rule_version=WAVE.version,since=since,rows=count,closed=closed,open=count-closed,parity_snapshots=parity,
                  unavailable_snapshots=unavailable,candles_sha256=file_digest(candles),
                  output_sha256=file_digest(output),
                  source_sha256=before,originals_unchanged=True,
                  historical_availability='closed boundary assumed; historical delivery latency not reconstructed')
    manifest_path.write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    return manifest


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT);p.add_argument('--candles',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--since',default=DEFAULT_SINCE)
    p.add_argument('--extend-until',help='Prepare a NEW 15m candle cache through this aware timestamp, no backfill')
    a=p.parse_args()
    result=prepare_candles(a.candles,timestamp_ms(a.extend_until),a.output) if a.extend_until else reconstruct(a.root,a.candles,a.output,a.since)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
