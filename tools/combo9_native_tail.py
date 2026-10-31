"""Public native-bar check for newly extended tail; no financial calculations."""
from pathlib import Path
import sys,json,math
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));from combo9_prepare import get,S

def main():
 man=json.loads((S/'market_manifest.json').read_bytes());checks=[]
 for tf,width in [('5m',300000),('15m',900000),('1h',3600000)]:
  begin=man['old_end_ms']//width*width-width;end=man['end_ms']//width*width;cursor=begin;native=[]
  while cursor<end:
   batch=get('/api/v3/klines',{'symbol':'SOLUSDT','interval':tf,'startTime':cursor,'endTime':end-1,'limit':1000})
   if not batch:raise RuntimeError('Incomplete native tail')
   native.extend(k for k in batch if int(k[0])<end);cursor=int(batch[-1][0])+width
  existing={r['open_time_ms']:r for r in map(json.loads,(S/'market'/(tf+'.jsonl')).read_text().splitlines())};bad=[]
  for k in native:
   r=existing[int(k[0])]
   if any(not math.isclose(float(k[i]),r[f],abs_tol=1e-9,rel_tol=0) for i,f in [(1,'open'),(2,'high'),(3,'low'),(4,'close')]):bad.append(int(k[0]))
  (S/'market'/('native_tail_'+tf+'.json')).write_text(json.dumps(native),encoding='utf-8');checks.append({'timeframe':tf,'N':len(native),'OHLC_differences':bad});assert not bad
 (S/'VERIFICACAO_CANDLES_NATIVOS_CAUDA.json').write_text(json.dumps({'passed':True,'checks':checks},indent=2),encoding='utf-8');print(checks)
if __name__=='__main__':main()
