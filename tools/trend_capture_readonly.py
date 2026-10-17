"""Read-only SSH collection into a NEW local directory; no remote files written."""
import argparse
import base64
import json
import subprocess
from pathlib import Path

REMOTE = r'''
import base64,json,sqlite3,pathlib
root=pathlib.Path('/root/trend-sol')
paths=[]
for folder,pattern in [('data/trades','*.jsonl'),('data/state','*.json'),('data/telemetry','price_structure_backfill*')]:
 paths.extend(sorted((root/folder).glob(pattern)))
for p in paths:
 print(json.dumps(dict(path=str(p.relative_to(root)),data=base64.b64encode(p.read_bytes()).decode())),flush=True)
for p in sorted((root/'data/state').glob('*.history.sqlite')):
 db=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True)
 db.execute('PRAGMA query_only=ON');db.execute('BEGIN');db.execute('SELECT COUNT(*) FROM history').fetchone()
 blob=db.serialize();db.rollback();db.close()
 print(json.dumps(dict(path=str(p.relative_to(root)),data=base64.b64encode(blob).decode())),flush=True)
'''


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    proc=subprocess.Popen(['ssh','-o','BatchMode=yes','root@207.154.197.12','python3','-'],
                          stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    proc.stdin.write(REMOTE);proc.stdin.close()
    count=0
    for line in proc.stdout:
        row=json.loads(line);target=(args.output/row['path']).resolve()
        if not target.is_relative_to(args.output.resolve()):raise ValueError('Invalid path')
        target.parent.mkdir(parents=True,exist_ok=True)
        with target.open('xb') as f:f.write(base64.b64decode(row['data']))
        count+=1;print(row['path'],flush=True)
    if proc.wait():raise RuntimeError('SSH snapshot incomplete')
    print('Read-only per-file copies; not globally atomic:',count)


if __name__=='__main__':main()
