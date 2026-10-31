"""Offline research preparation; never constructs operational registries."""
import sys,json,hashlib,subprocess,zipfile,io,urllib.request,urllib.parse,collections
from pathlib import Path
from datetime import datetime,timezone
import yaml
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_12bracos_20261021';M=60000
START=int(datetime.fromisoformat('2026-06-01T00:00:00-03:00').timestamp()*1000)
def dump(p,x):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(x,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def get(path,params=None):
 u='https://api.binance.com'+path+('?' +urllib.parse.urlencode(params) if params else '')
 with urllib.request.urlopen(u,timeout=30) as r:return json.load(r)
def main():
 S.mkdir(exist_ok=True);frozen=S/'frozen';frozen.mkdir(exist_ok=True)
 if not (frozen/'src').exists():
  p=subprocess.run(['git','archive','HEAD','src'],cwd=ROOT,capture_output=True,check=True)
  import tarfile
  with tarfile.open(fileobj=io.BytesIO(p.stdout)) as t:t.extractall(frozen,filter='data')
 sys.path.insert(0,str(frozen));from src.config_profiles import effective_config
 cfg=effective_config(yaml.safe_load((ROOT/'config/config.yaml').read_text()));assert not cfg['entry']['require_volume_drying']
 if not (S/'config.json').exists():dump(S/'config.json',cfg)
 else:assert json.loads((S/'config.json').read_bytes())==cfg
 if not (S/'market_manifest.json').exists():
  server=int(get('/api/v3/time')['serverTime']);end=min(server//M*M,int(datetime.fromisoformat('2026-10-22T03:00:00+00:00').timestamp()*1000));manifest={'start_ms':START,'end_ms':end,'server_time_ms':server,'code_head':subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,check=True).stdout.strip(),'preregister_sha256':sha(S/'PREREGISTRO_V1.md'),'config_sha256':sha(S/'config.json'),'snapshot':'latest fully closed 1m; fixed cutoff for all runs'};dump(S/'market_manifest.json',manifest)
 manifest=json.loads((S/'market_manifest.json').read_bytes());end=manifest['end_ms'];market=S/'market';market.mkdir(exist_ok=True)
 if not (market/'1m.jsonl').exists():
  old=ROOT/'data/analysis/fast_drop_selectors_20261018/idealized/market/SOLUSDT_1m.jsonl';by={}
  for l in old.open():
   x=json.loads(l)
   if x['close_time_ms']<end:by[x['open_time_ms']]=x
  warm=min(by);missing=[];lo=None
  for t in range(warm,end,M):
   if t not in by and lo is None:lo=t
   if t in by and lo is not None:missing.append((lo,t-M));lo=None
  if lo is not None:missing.append((lo,end-M))
  for left,right in missing:
   cursor=left
   while cursor<=right:
    chunk=get('/api/v3/klines',dict(symbol='SOLUSDT',interval='1m',startTime=cursor,endTime=right+M-1,limit=1000))
    if not chunk:raise RuntimeError('Missing candles remain')
    for x in chunk:by[x[0]]={'open_time_ms':x[0],'close_time_ms':x[6],'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),'close':float(x[4]),'quote_volume':float(x[7]),'trades':int(x[8])}
    cursor=chunk[-1][0]+M;print('download',cursor,flush=True)
  with (market/'1m.jsonl').open('x',encoding='utf-8') as f:
   for t in sorted(by):f.write(json.dumps(by[t],separators=(',',':'))+'\n')
 rows=[json.loads(l) for l in (market/'1m.jsonl').open()];warm=rows[0]['open_time_ms'];gaps=[t for t in range(warm,end,M) if t not in {r['open_time_ms'] for r in rows}] if False else []
 starts={r['open_time_ms'] for r in rows};missing=sum(t not in starts for t in range(warm,end,M));assert not missing
 for tf,n in [('5m',5),('15m',15),('1h',60)]:
  if (market/(tf+'.jsonl')).exists():continue
  groups=collections.defaultdict(list)
  for r in rows:groups[r['open_time_ms']//(n*M)*(n*M)].append(r)
  with (market/(tf+'.jsonl')).open('x',encoding='utf-8') as f:
   for t,rr in sorted(groups.items()):
    if len(rr)!=n or any(r['open_time_ms']!=t+i*M for i,r in enumerate(rr)):continue
    x=dict(open_time_ms=t,close_time_ms=t+n*M-1,open=rr[0]['open'],high=max(r['high'] for r in rr),low=min(r['low'] for r in rr),close=rr[-1]['close'],quote_volume=sum(r['quote_volume'] for r in rr),trades=sum(r['trades'] for r in rr));f.write(json.dumps(x,separators=(',',':'))+'\n')
 manifest.update(internal_missing_1m=missing,granularities={})
 for tf in ['1m','5m','15m','1h']:
  p=market/(tf+'.jsonl');rr=[json.loads(l) for l in p.open()];manifest['granularities'][tf]={'first_open_ms':rr[0]['open_time_ms'],'last_close_ms':rr[-1]['close_time_ms'],'N':len(rr),'sha256':sha(p)}
 manifest['frozen_code_files']={str(p.relative_to(frozen)).replace('\\','/'):sha(p) for p in frozen.rglob('*.py')};manifest['volume_convention']='quote volume cache; volume-dependent entry disabled and asserted; DMI/EMA/MACD independent of volume'
 dump(S/'market_manifest.json',manifest);print(json.dumps({k:v for k,v in manifest.items() if k!='frozen_code_files'}),flush=True)
if __name__=='__main__':main()
