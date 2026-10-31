"""Independent native-kline OHLC checks against closed 1m aggregates."""
import json,urllib.request,urllib.parse,hashlib,math,time
from pathlib import Path
S=Path(__file__).resolve().parents[1]/'data/analysis/replay_12bracos_20261021'
def main():
 m=json.loads((S/'market_manifest.json').read_bytes());out={}
 for tf,step in [('5m',300000),('15m',900000),('1h',3600000)]:
  p=S/'market'/('native_'+tf+'.json');expected=[json.loads(l) for l in (S/'market'/(tf+'.jsonl')).open()]
  if p.exists():rows=json.loads(p.read_bytes())
  else:
   cursor=expected[0]['open_time_ms'];end=expected[-1]['close_time_ms'];rows=[]
   while cursor<=end:
    qs=urllib.parse.urlencode(dict(symbol='SOLUSDT',interval=tf,startTime=cursor,endTime=end,limit=1000))
    with urllib.request.urlopen('https://api.binance.com/api/v3/klines?'+qs,timeout=25) as response:chunk=json.load(response)
    if not chunk:raise RuntimeError('Native source unexpectedly incomplete')
    rows.extend(chunk);cursor=int(chunk[-1][0])+step
   p.write_text(json.dumps(rows,separators=(',',':')),encoding='utf-8')
  index={int(x[0]):x for x in rows};diff=[]
  for r in expected:
   x=index.get(r['open_time_ms'])
   if x is None:diff.append({'open_ms':r['open_time_ms'],'kind':'MISSING_NATIVE'});continue
   for key,j in [('open',1),('high',2),('low',3),('close',4)]:
    if not math.isclose(r[key],float(x[j]),abs_tol=1e-9,rel_tol=1e-12):diff.append({'open_ms':r['open_time_ms'],'field':key,'aggregated':r[key],'native':float(x[j])})
  out[tf]={'N_aggregated':len(expected),'N_native':len(rows),'OHLC_differences':diff,'native_sha256':hashlib.sha256(p.read_bytes()).hexdigest()};print('Native OHLC check',tf,'rows',len(expected),'differences',len(diff),flush=True)
 result={'by_granularity':out,'all_aggregated_OHLC_match_native':all(not x['OHLC_differences'] for x in out.values()),'limitation':'verifies public closed OHLC, not historical received feed availability'};(S/'VERIFICACAO_CANDLES_NATIVOS.json').write_text(json.dumps(result,separators=(',',':')),encoding='utf-8')
if __name__=='__main__':main()
