"""Outcome-independent convention gates before new economic results."""
import sys,json,math
from pathlib import Path
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import multi16_engine as m
e=m.e;S=m.S;start=m.MAN['start_ms'];cfg=json.loads((S/'config.json').read_bytes())
def opened(arm,price=100):
 o=m.make(arm,cfg,0,start);o.now=start;o.market_price=price;o.entry_regime={'mare':'BULL','onda':'BEAR'};o.latest_market_context={'tf_5m':{'ema_context':'BEA','macd_context':'BE+','macd_histogram':.1,'macd_histogram_previous':.05,'latest_closed_at_ms':start-1}};o.tactical_candles=[{'open_time':start-2*e.M,'close_time':start-e.M-1,'open':99.8,'close':99.9},{'open_time':start-e.M,'close_time':start-1,'open':99.9,'close':100}];assert o._process_signal(e.EntrySignal('SOLUSDT',price,e.iso(start),start-e.M,.2,'1m',14));return o,o.positions[0]
def main():
 e.checks(cfg,start);done=[]
 for arm,(base,flags) in m.MULTI.items():
  o,p=opened(arm);assert p._active_profit_lock_economic_floor() is not None and o._be_off
  if 'FAST' in flags:
   o.minute_closes={start-4*e.M:100.1};e.segment(o,100,99.4,start,start+40000);assert p.exit_reason=='FAST_DROP';done.append(arm+'/FAST first crossing')
   o,p=opened(arm);p.hard_stop_price=99.6;p._refresh_effective_stop();o.minute_closes={start-4*e.M:100.1};e.segment(o,100,99.4,start,start+40000);assert p.exit_reason=='HARD_STOP';done.append(arm+'/normal precedes FAST')
  if 'CLUSTER' in flags:
   o,p=opened(arm);o.latest_market_context['tf_5m']['ema_context']='SHO';o.now=start+5*e.M;o.market_price=99;o._open(e.EntrySignal('SOLUSDT',99,e.iso(o.now),o.now-e.M,.2,'1m',14),o.now);q=o.positions[-1];e.tick(o,98.5,o.now+1);assert p.exit_reason=='HARD_STOP' and q.exit_reason=='HS_BEAR_CLUSTER_EXIT';assert not o.clock.paused;done.append(arm+'/normal then cluster no instantaneous CB')
  if 'CB' in flags:
   o,p=opened(arm);o.pending_closes=[{'boundary':start+e.M,'net':-1,'pair_id':'synthetic1'},{'boundary':start+e.M,'net':-1,'pair_id':'synthetic2'}];e.tick(o,99.9,start+e.M);assert p.exit_reason=='CIRCUIT_BREAKER_EXIT_ALL';assert len(o.cb_edges)==1;done.append(arm+'/CB exact boundary')
 # Necessary cluster cascade with a profitable surviving position.
 arm=next(k for k,v in m.MULTI.items() if v==(m.CONTROL,('CLUSTER','CB')));o,p=opened(arm);o.latest_market_context['tf_5m']['ema_context']='SHO';o.now=start+5*e.M;o.market_price=99.2;o._open(e.EntrySignal('SOLUSDT',99.2,e.iso(o.now),o.now-e.M,.2,'1m',14),o.now);q=o.positions[-1];o.market_price=98;o._open(e.EntrySignal('SOLUSDT',98,e.iso(o.now),o.now-2*e.M,.2,'1m',14),o.now+1);survivor=o.positions[-1];o.clock.equity=98.8;o.clock.peak=100;e.tick(o,98.5,o.now+1);assert q.exit_reason=='HS_BEAR_CLUSTER_EXIT' and survivor.status=='OPEN';assert not o.cascades;e.tick(o,98.5,start+6*e.M);assert survivor.exit_reason=='CIRCUIT_BREAKER_EXIT_ALL';assert len(o.cascades)==1 and o.cascades[0]['cluster_necessary_for_this_boundary'];assert o.cascades[0]['victims_N']==1;done.append('necessary cluster next-boundary cascade, profitable survivor, one victim')
 # Entry filters remain attached, not bypassed by reducer mix.
 # A downward gap can make FAST preempt the HS that would otherwise seed CLUSTER.
 arm=next(k for k,v in m.MULTI.items() if v==(m.CONTROL,('FAST','CLUSTER','CB')));o,p=opened(arm);o.latest_market_context['tf_5m']['ema_context']='SHO';o.now=start+5*e.M;o.market_price=99.2;o._open(e.EntrySignal('SOLUSDT',99.2,e.iso(o.now),o.now-e.M,.2,'1m',14),o.now);q=o.positions[-1];o.minute_closes={o.now-4*e.M:100.1};e.tick(o,98.5,o.now+1);assert p.exit_reason==q.exit_reason=='FAST_DROP';assert not any(x['event']=='HS_BEAR_CLUSTER_TRIGGERED' for x in o.events);done.append('FAST preceding own HS removes cluster seed without double intervention')
 # A cluster close can coincide with a CB edge without being necessary for that edge.
 arm=next(k for k,v in m.MULTI.items() if v==(m.CONTROL,('CLUSTER','CB')));o,p=opened(arm);o.latest_market_context['tf_5m']['ema_context']='SHO';o.now=start+5*e.M;o.market_price=99.2;o._open(e.EntrySignal('SOLUSDT',99.2,e.iso(o.now),o.now-e.M,.2,'1m',14),o.now);o.market_price=98;o._open(e.EntrySignal('SOLUSDT',98,e.iso(o.now),o.now-2*e.M,.2,'1m',14),o.now+1);o.clock.equity=98.8;o.clock.peak=100;e.tick(o,98.5,o.now+1);o.pending_closes.append({'boundary':start+6*e.M,'net':-1,'pair_id':'prior_other_close'});e.tick(o,98.5,start+6*e.M);assert len(o.cascades)==1 and not o.cascades[0]['cluster_necessary_for_this_boundary'];done.append('concomitant cluster/CB edge distinguished from necessary cascade')
 o,p=opened(arm);o.clock.pause_until=start+6*3600000;o.circuit_breaker_active=True;o.pending_closes=[{'boundary':start+e.M,'net':-1,'pair_id':'synthetic1'},{'boundary':start+e.M,'net':-1,'pair_id':'synthetic2'}];e.tick(o,99.9,start+e.M);assert p.status=='OPEN' and not o.cb_edges[-1]['activation_eligible'];done.append('new crisis while already paused is not new CB liquidation, preserving runtime predicate')
 for arm,(base,flags) in m.MULTI.items():
  if base not in m.FILTERS:continue
  o,p=opened(arm);o.positions=[];o.all_positions=[];o.latest_market_context['tf_5m'].update(ema_context='BUL',macd_context='BU-',macd_histogram=-.1);o.now=start+5*e.M;assert not o._process_signal(e.EntrySignal('SOLUSDT',101,e.iso(o.now),o.now-e.M,.2,'1m',14));assert not o.positions;done.append(arm+'/filter rejects BU-')
 e.dump(S/'COMPOSITION_CHECKS.json',{'passed':True,'economic_results_consulted':False,'N':len(done),'checks':done});(S/'RUN_DISPATCH.py.txt').write_text(m.source,encoding='utf-8');print('CONVENTION_GATES_PASS',len(done),flush=True)
if __name__=='__main__':main()
