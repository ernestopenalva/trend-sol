"""Exact causal tail reconstruction, verified over12h overlap; no economic replay."""
from pathlib import Path
from datetime import datetime,timezone
from dataclasses import asdict
import sys,json,bisect,hashlib
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_combinacoes9_20261025';B=ROOT/'data/analysis/replay_12bracos_20261021';sys.path.insert(0,str(ROOT/'tools'));import replay12_reconstruct as r
M=60000;K=['latest_open_at_ms','latest_closed_at_ms','previous_open_at_ms','previous_closed_at_ms','close','closed','ema_context','macd_context','macd_line','macd_line_previous','macd_histogram','macd_histogram_previous','macd_signal','macd_signal_previous']
def dump(n,x):(S/n).write_text(json.dumps(x,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
def main():
 man=json.loads((S/'market_manifest.json').read_bytes());cfg=json.loads((S/'config.json').read_bytes());cut=man['old_end_ms'];seed=cut-12*3600000;end=man['end_ms'];r.S=S;bars={tf:r.load(tf) for tf in ['1m','5m','15m','1h']};clock=[seed];engine=r.EntryEngine('SOLUSDT',cfg,r.Logger(),monotonic_clock=lambda:clock[0]/1000)
 for tf in ['1m','5m','15m']:
  prior=[c for c in bars[tf] if c.boundary_ms<seed][-300:];engine.load_history(tf,[[c.open_time_ms,c.open,c.high,c.low,c.close,c.quote_volume,c.close_time_ms] for c in prior],seed-1)
 ctx=r.MarketContextEngine(engine,cfg);indexes={tf:bisect.bisect_left([c.boundary_ms for c in bars[tf]],seed) for tf in ['5m','15m']};oldc=json.loads((B/'contexts_runtime_projection.json').read_bytes());current=ctx._timeframe_snapshot(engine._candles_for('5m'),'5m');current['closed']=True;contexts={};signals=[];tailfull={}
 for c in bars['1m']:
  at=c.boundary_ms
  if at<seed:continue
  clock[0]=at
  for tf in ['15m','5m']:
   while indexes[tf]<len(bars[tf]) and bars[tf][indexes[tf]].boundary_ms<=at:
    b=bars[tf][indexes[tf]];indexes[tf]+=1;engine.on_kline('solusdt@kline_'+tf,r.payload(b))
    if tf=='5m':
     current=ctx._timeframe_snapshot(engine._candles_for('5m'),'5m');current['closed']=True;contexts[str(b.boundary_ms)]={k:current[k] for k in K if k in current}
     if b.boundary_ms>=cut:tailfull[str(b.boundary_ms)]=current
  sig=engine.on_kline('solusdt@kline_1m',r.payload(c))
  if sig is not None and at<end:signals.append({'at':at,'signal':{**asdict(sig),'ts':r.iso(at)}})
 old=json.loads((B/'signals.json').read_bytes());assert [x for x in old['REAL_A'] if seed<=x['at']<cut]==[x for x in signals if seed<=x['at']<cut],'overlap signals differ';overlap=[t for t in contexts if int(t)<cut];assert all(contexts[t]==oldc[t] for t in overlap),'overlap contexts differ';new=old['REAL_A']+[x for x in signals if x['at']>=cut];combinedc={**oldc,**{t:v for t,v in contexts.items() if int(t)>=cut}};dump('signals.json',{'REAL_A':new});dump('contexts_runtime_projection.json',combinedc);dump('contexts_tail_full.json',tailfull)
 buffers={p:r.StructureBuffer([r.Hour(c.open_time_ms,c.high,c.low,True,c.boundary_ms) for c in bars[tf]],rule=rule) for p,tf,rule in [('mare','1h',r.TIDE),('onda','15m',r.WAVE)]};tl=json.loads((B/'regime_timeline.json').read_bytes());reg=json.loads((B/'regimes.json').read_bytes())
 for p,rule in [('mare',r.TIDE),('onda',r.WAVE)]:
  for at in range(tl[p][-1]['at']+rule.interval_ms,end,rule.interval_ms):
   snap=buffers[p].snapshot(at,'IDEALIZED_PUBLIC');label=snap['label'] if snap.get('status') in ['OK','EQUAL_EXTREME'] else 'UNAVAILABLE';prior=tl[p][-1];tl[p].append({'at':at,'label':label,'run':prior['run']+int(label!=prior['label'])})
 for x in new:
  if str(x['at']) in reg:continue
  reg[str(x['at'])]={}
  for p in buffers:
   snap=buffers[p].snapshot(x['at'],'IDEALIZED_PUBLIC');reg[str(x['at'])][p]=snap['label'] if snap.get('status') in ['OK','EQUAL_EXTREME'] else 'UNAVAILABLE'
 dump('regimes.json',reg);dump('regime_timeline.json',tl);proof={'old_signal_prefix_exact':old['REAL_A']==[x for x in new if x['at']<cut],'old_context_projection_exact':all(combinedc[t]==v for t,v in oldc.items()),'overlap12h_signals_exact':True,'overlap12h_contexts_exact':True,'overlap_signals_N':sum(seed<=x['at']<cut for x in signals),'overlap_contexts_N':len(overlap),'signals_N':len(new),'new_end_ms':end,'method':'reuse immutable prefix, causal rolling300 warm state plus12h overlap exact; source functions unchanged','source_reconstruction_sha256':hashlib.sha256((ROOT/'tools/replay12_reconstruct.py').read_bytes()).hexdigest()};dump('VERIFICACAO_PREFIXO.json',proof);print('RECONSTRUCTION_COMPLETE',proof,flush=True)
if __name__=='__main__':main()
