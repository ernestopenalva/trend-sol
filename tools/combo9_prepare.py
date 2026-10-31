"""Extend frozen public market for offline combo study; never trading clients."""
from pathlib import Path
from datetime import datetime,timezone
import json,hashlib,urllib.request,urllib.parse,collections,shutil
ROOT=Path(__file__).resolve().parents[1];B=ROOT/'data/analysis/replay_12bracos_20261021';S=ROOT/'data/analysis/replay_combinacoes9_20261025';M=60000

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def get(path,params=None):
 u='https://api.binance.com'+path+('?' +urllib.parse.urlencode(params) if params else '')
 with urllib.request.urlopen(u,timeout=30) as r:return json.load(r)
def dump(n,x):(S/n).write_text(json.dumps(x,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
def main():
 (S/'market').mkdir(exist_ok=True);manpath=S/'market_manifest.json';old=json.loads((B/'market_manifest.json').read_bytes())
 if not manpath.exists():
  server=int(get('/api/v3/time')['serverTime']);end=min(server//M*M,int(datetime.fromisoformat('2026-10-26T00:00:00-03:00').timestamp()*1000));dump('market_manifest.json',{'start_ms':old['start_ms'],'end_ms':end,'server_time_ms':server,'old_end_ms':old['end_ms'],'old_half_cut_ms':old['start_ms']+(old['end_ms']-old['start_ms'])//2,'config_sha256':sha(S/'config.json'),'preregister_sha256':sha(S/'PREREGISTRO_V1.md'),'base_adapter_sha256':sha(ROOT/'tools/replay12_engine.py'),'base_market_manifest_sha256':sha(B/'market_manifest.json'),'frozen_code_head':old['code_head'],'data_source':'preserved1m prefix plus public Binance REST tail'})
 man=json.loads(manpath.read_bytes());end=man['end_ms'];target=S/'market/1m.jsonl'
 if not target.exists():
  prefix=(B/'market/1m.jsonl').read_bytes();rows=[json.loads(l) for l in prefix.splitlines()];cursor=rows[-1]['open_time_ms']+M;tail=[]
  while cursor<end:
   chunk=get('/api/v3/klines',{'symbol':'SOLUSDT','interval':'1m','startTime':cursor,'endTime':end-1,'limit':1000})
   if not chunk:raise RuntimeError('Empty market tail')
   for k in chunk:
    if k[0]<end:tail.append({'open_time_ms':int(k[0]),'close_time_ms':int(k[6]),'open':float(k[1]),'high':float(k[2]),'low':float(k[3]),'close':float(k[4]),'quote_volume':float(k[7]),'trades':int(k[8])})
   cursor=int(chunk[-1][0])+M;print('TAIL',cursor,len(tail),flush=True)
  with target.open('xb') as f:
   f.write(prefix)
   for row in tail:f.write((json.dumps(row,separators=(',',':'))+'\n').encode())
 rows=[json.loads(l) for l in target.open()];assert all(rows[i]['open_time_ms']==rows[0]['open_time_ms']+i*M for i in range(len(rows)));assert rows[-1]['close_time_ms']+1==end
 for tf,n in [('5m',5),('15m',15),('1h',60)]:
  dest=S/'market'/(tf+'.jsonl')
  if dest.exists():continue
  groups=collections.defaultdict(list)
  for r in rows:groups[r['open_time_ms']//(n*M)*n*M].append(r)
  with dest.open('x',encoding='utf-8') as f:
   for t,rr in sorted(groups.items()):
    if len(rr)==n and all(r['open_time_ms']==t+i*M for i,r in enumerate(rr)):
     f.write(json.dumps({'open_time_ms':t,'close_time_ms':t+n*M-1,'open':rr[0]['open'],'high':max(r['high'] for r in rr),'low':min(r['low'] for r in rr),'close':rr[-1]['close'],'quote_volume':sum(r['quote_volume'] for r in rr),'trades':sum(r['trades'] for r in rr)},separators=(',',':'))+'\n')
 man['granularities']={}
 for tf in ['1m','5m','15m','1h']:
  p=S/'market'/(tf+'.jsonl');rr=[json.loads(l) for l in p.open()];man['granularities'][tf]={'N':len(rr),'first_open':rr[0]['open_time_ms'],'last_close':rr[-1]['close_time_ms'],'sha256':sha(p)}
 man['internal_missing_1m']=0;man['old_prefix_bytes_exact']=(S/'market/1m.jsonl').read_bytes().startswith((B/'market/1m.jsonl').read_bytes());assert man['old_prefix_bytes_exact'];dump('market_manifest.json',man);print('MARKET_FROZEN',json.dumps(man),flush=True)
if __name__=='__main__':main()
