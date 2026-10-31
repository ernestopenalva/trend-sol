"""Pre-economic forced-entry audit of stop mechanics; no PnL aggregation."""
import sys,json,gzip,bisect,collections
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import replay12_engine as engine
S=engine.S

def main():
 cfg=json.loads((S/'config.json').read_bytes());m=json.loads((S/'market_manifest.json').read_bytes());ctx=json.loads((S/'contexts.json').read_bytes());ct=sorted(map(int,ctx));bars=[json.loads(l) for l in (S/'market/1m.jsonl').open()];starts=[x['open_time_ms'] for x in bars];f=json.loads(gzip.decompress((ROOT/'data/analysis/mare_onda_forward_20261021/fontes_economicas.json.gz').read_bytes()));B=engine.ms('2026-10-08T23:52:00-03:00');results={}
 for arm in engine.ARMS:
  path='data/trades/trades_B.jsonl' if arm=='REAL_A' else 'data/trades/trades_'+arm.lower()+'.jsonl';rows=[json.loads(l) for l in f['files'][path]['data'].splitlines()];rows=[r for r in rows if r.get('opened_at') and B<=engine.ms(r['opened_at'])<=m['end_ms'] and (arm!='REAL_A' or r.get('position_type')=='BOT_EXIT' and not r.get('phantom'))];detail=[]
  for r in rows:
   if arm in ['HS_BEAR_CLUSTER_EXIT_SHADOW','CB_EXIT_ALL_SHADOW']:continue
   op=engine.ms(r['opened_at']);cl=engine.ms(r['closed_at']);begin=(op+59999)//60000*60000;end=min(m['end_ms'],cl+15*60000);by_path={}
   for path in ['HIGH_FIRST','LOW_FIRST']:
    o=engine.make(arm,cfg,0,begin);o.market_price=float(r['entry_price']);o.now=op;o.entry_regime={'mare':'UNAVAILABLE','onda':'UNAVAILABLE'};o.latest_market_context={'tf_5m':ctx[str(ct[bisect.bisect_right(ct,op)-1])],'captured_at':engine.iso(op)};sig=engine.EntrySignal('SOLUSDT',float(r.get('signal_price') or r['entry_price']),r['opened_at'],r['source_candle_open_time'],r['entry_atr'],'1m',14);o._open(sig,r['source_candle_open_time']//300000*300000);p=o.positions[0];prevcontext=None
    for c in bars[bisect.bisect_left(starts,begin):bisect.bisect_left(starts,end)]:
     at=c['open_time_ms'];o.now=at;o.market_price=c['open'];co=ctx[str(ct[bisect.bisect_right(ct,at)-1])];snap={'tf_5m':co,'captured_at':engine.iso(at)}
     if co.get('latest_closed_at_ms')!=prevcontext:o.on_closed_5m(snap);prevcontext=co.get('latest_closed_at_ms')
     o.latest_market_context=snap
     if arm=='BE_OFF_CB_FAST_DROP_EMA_SHADOW':
      j=bisect.bisect_left(starts,at);o.minute_closes={z['close_time_ms']+1:z['close'] for z in bars[max(0,j-10):j]}
     vals=[c['open'],c['high'],c['low'],c['close']] if path=='HIGH_FIRST' else [c['open'],c['low'],c['high'],c['close']];t=[at,at+20000,at+40000,at+59999];engine.tick(o,vals[0],t[0])
     for j in range(1,4):
      if p.status=='CLOSED':break
      if vals[j]!=vals[j-1]:engine.segment(o,vals[j-1],vals[j],t[j-1],t[j])
     if p.status=='CLOSED':break
    by_path[path]={'reason':p.exit_reason if p.status=='CLOSED' else 'OPEN_AT_AUDIT_HORIZON','closed_ms':engine.ms(p.close_ts) if p.close_ts else None,'reason_matches':p.status=='CLOSED' and p.exit_reason==r['exit_reason']}
   detail.append({'pair_id':r['pair_id'],'source':r['source_candle_open_time'],'forward_reason':r['exit_reason'],'entry_partial_minute_omitted':op%60000!=0,'paths':by_path,'cause_of_difference_if_any':'OHLC_SEQUENCE_OR_PARTIAL_ENTRY_MINUTE_OR_DATA_CONTEXT_OR_RUNTIME_FEED; not uniquely attributable'})
  results[arm]={'N':len(detail),'reason_matches_either_path':sum(any(x['reason_matches'] for x in r['paths'].values()) for r in detail),'reason_matches_both_paths':sum(all(x['reason_matches'] for x in r['paths'].values()) for r in detail),'portfolio_dependency_not_individually_identifiable':arm in ['HS_BEAR_CLUSTER_EXIT_SHADOW','CB_EXIT_ALL_SHADOW'],'conditional_audit':'observed entry/ATR; synthetic OHLC; 0 friction only to inspect stop reasons; not actual execution reconciliation','details':detail}
 engine.dump(S/'FIDELIDADE_SAIDAS_CONDICIONAL.json',results);print(json.dumps({k:{x:y for x,y in v.items() if x!='details'} for k,v in results.items()}),flush=True)
if __name__=='__main__':main()
