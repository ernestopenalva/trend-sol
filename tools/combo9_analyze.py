"""Predeclared combo metrics, counterfactual exits and classification; no trading IO."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from collections import Counter,defaultdict
import sys,json,math,random,statistics,hashlib
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import combo9_engine as c
S=c.S;B=c.B;M=c.MAN;START=M['start_ms'];END=M['end_ms'];CUT=M['old_half_cut_ms'];TZ=timezone(timedelta(hours=-3));E=c.e;STATES=['BULL','BEAR','EXPA','COMP','UNDEFINED','UNAVAILABLE']
def read(n):return json.loads((S/n).read_bytes())
def dump(n,x):(S/n).write_text(json.dumps(x,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
def day(t):return datetime.fromtimestamp(t/1000,TZ).date().isoformat()
def metrics(rr):
 n=len(rr);closed=[r for r in rr if r['close_ms'] is not None];reasons=Counter(r['reason'] for r in closed);total=sum(r['net'] for r in rr)
 return {'N':n,'closed':len(closed),'open':n-len(closed),'net_total':total,'net_realized':sum(r['net'] for r in closed),'net_open_mtm':sum(r['net'] for r in rr if r['close_ms'] is None),'net_per_trade':total/n if n else None,'net_per_closed':sum(r['net'] for r in closed)/len(closed) if closed else None,'exposure_notional_hours':sum(r['notional']*r['duration_minutes']/60 for r in rr),'exit_reasons':{k:{'N':v,'pct_closed':100*v/len(closed) if closed else None} for k,v in reasons.items()},'HS_pct':100*reasons['HARD_STOP']/len(closed) if closed else None,'PL_pct':100*reasons['PROFIT_LOCK']/len(closed) if closed else None,'Trail_pct':100*reasons['TRAILING']/len(closed) if closed else None,'days':len({day(r['open_ms']) for r in rr})}
def difference(a,b):
 x,y=metrics(a),metrics(b);return {'variant':x,'control':y,'delta_total':x['net_total']-y['net_total'],'delta_mean':x['net_per_trade']-y['net_per_trade'] if x['N'] and y['N'] else None,'delta_total_per_variant_trade':(x['net_total']-y['net_total'])/x['N'] if x['N'] else None}
def bootstrap(a,b,width):
 d=defaultdict(lambda:[0.,0,0.,0])
 for side,rr in [(0,a),(2,b)]:
  for r in rr:
   key=day(r['open_ms']) if width=='day' else str((r['open_ms']-START)//(72*3600000));d[key][side]+=r['net'];d[key][side+1]+=1
 values=[d[k] for k in sorted(d)];n=len(values)
 if n<10:return {'blocks':n,'CI95':None,'reason':'fewer than10blocks'}
 rng=random.Random(20261025);out=[]
 for _ in range(5000):
  sample=rng.choices(values,k=n);nv=sum(x[1] for x in sample);nc=sum(x[3] for x in sample)
  if nv and nc:out.append(sum(x[0] for x in sample)/nv-sum(x[2] for x in sample)/nc)
 out.sort();return {'blocks':n,'replicates_estimable':len(out),'CI95':[out[int(.025*(len(out)-1))],out[int(.975*(len(out)-1))]],'seed':20261025,'conditional_fixed_trajectories':True}
def compare(v,b):
 a,d=v['records'],b['records'];ai={r['source']:r for r in a};bi={r['source']:r for r in d};common=set(ai)&set(bi);daily=defaultdict(float)
 for sign,rr in [(1,a),(-1,d)]:
  for r in rr:daily[day(r['open_ms'])]+=sign*r['net']
 best=sorted([(k,x) for k,x in daily.items() if x>0],key=lambda x:x[1],reverse=True)[:3];worst=sorted([(k,x) for k,x in daily.items() if x<0],key=lambda x:x[1])[:3];excluded={k for k,x in best};result=difference(a,d);pair=sum(ai[s]['net']-bi[s]['net'] for s in common);exclusive_v=sum(ai[s]['net'] for s in set(ai)-set(bi));exclusive_b=sum(bi[s]['net'] for s in set(bi)-set(ai));assert math.isclose(pair+exclusive_v-exclusive_b,result['delta_total'],abs_tol=1e-8)
 halves=[difference([r for r in a if (r['open_ms']<CUT)==first],[r for r in d if (r['open_ms']<CUT)==first]) for first in [True,False]];equalcut=START+(END-START)//2;equal=[difference([r for r in a if (r['open_ms']<equalcut)==first],[r for r in d if (r['open_ms']<equalcut)==first]) for first in [True,False]]
 result.update(variant_arm=v['arm'],control_arm=b['arm'],path=v['path'],paired_real_N=len(common),paired_real_delta=pair,variant_only_N=len(set(ai)-set(bi)),control_only_N=len(set(bi)-set(ai)),variant_only_net=exclusive_v,control_only_net=exclusive_b,exclusive_residual=exclusive_v-exclusive_b,halves_primary=halves,halves_equal_diagnostic=equal,top3_positive_delta_days=best,top3_negative_delta_days=worst,excluding_top3=difference([r for r in a if day(r['open_ms']) not in excluded],[r for r in d if day(r['open_ms']) not in excluded]),lodo_delta_total_range=[min(result['delta_total']-v for v in daily.values()),max(result['delta_total']-v for v in daily.values())],CI95_delta_mean_day=bootstrap(a,d,'day'),CI95_delta_mean_72h=bootstrap(a,d,'72h'))
 return result
def funding(rr):
 ev=[]
 for r in rr:
  ev.append((r['open_ms'],1,-r['notional']-r['fee_est']/2))
  if r['close_ms'] is not None:ev.append((r['close_ms'],0 if r['reason']=='CIRCUIT_BREAKER_EXIT_ALL' and r['close_ms']%60000==0 else 2,r['quantity']*r['exit']-r['fee_est']/2))
 cash=minimum=100.
 for t,kind,v in sorted(ev):cash+=v;minimum=min(minimum,cash)
 return {'cash_gate':False,'initial_cash_required':100+max(0,-minimum),'minimum_cash_if100_initial':minimum,'approximate_fees_half_each_side':True}
class ExitCounterfactual:
 def __init__(self):
  self.minutes=[E.Bar(json.loads(l)) for l in (S/'market/1m.jsonl').open()];self.index={r['open_time_ms']:i for i,r in enumerate(self.minutes)};self.cfg=read('config.json');self.cache={}
 def one(self,trade,path):
  key=(path,trade['source'],trade['entry'],trade['atr']);old=self.cache.get(key)
  if old is not None:return old
  begin=trade['open_ms'];o=c.original_make(c.CONTROL,self.cfg,3.5,begin);o.now=begin;o.market_price=self.minutes[self.index[begin]]['open'];o.entry_regime={'mare':trade['mare'],'onda':trade['onda']};o.latest_market_context={'tf_5m':{}};o._open(E.EntrySignal('SOLUSDT',trade['entry'],E.iso(begin),trade['source'],trade['atr'],'1m',14),trade['source']//300000*300000);p=o.positions[0];assert math.isclose(p.entry_price,trade['entry'],abs_tol=1e-10);assert math.isclose(p.quantity,trade['quantity'],abs_tol=1e-10)
  for bar in self.minutes[self.index[begin]:]:
   at=bar['open_time_ms'];o.now=at;o.market_price=bar['open'];points=[bar['open'],bar['high'],bar['low'],bar['close']] if path=='HIGH_FIRST' else [bar['open'],bar['low'],bar['high'],bar['close']];times=[at,at+20000,at+40000,at+59999];previous=None;pt=None
   for value,t in zip(points,times):
    if previous is None:E.tick(o,value,t)
    elif value!=previous:E.segment(o,previous,value,pt,t)
    previous=value;pt=t
    if p.status=='CLOSED':break
   if p.status=='CLOSED':break
  q=E.record(p,END,self.minutes[-1]['close'],3.5,2*self.cfg['fees']['taker_fee_pct']);self.cache[key]=q;return q

def interventions(run,control,cf):
 records=run['records'];ci={r['source']:r for r in control['records']};isfilter=run['arm'] in c.FILTERS;details=[]
 if isfilter:
  rejected={r['source'] for r in run['admissions'] if r['reason'].startswith('ENTRY_BLOCKED_EMA_MACD_') or r['reason'] in ['ENTRY_BLOCKED_MACD_BU_MINUS','ENTRY_BLOCKED_MACD_UNAVAILABLE','ENTRY_BLOCKED_CONTEXT_UNAVAILABLE','ENTRY_BLOCKED_HISTOGRAM','ENTRY_BLOCKED_CONFIRMATION_1M']}
  for src in sorted(rejected&set(ci)):
   r=ci[src];details.append({'source':src,'kind':'EXPLICIT_SELECTION_REJECTION_CONTROL_OBSERVED','net_actual_variant_no_entry':0.,'net_without_filter_control_observed':r['net'],'delta':-r['net'],'control_reason':r['reason'],'real_control_pair':True})
 else:
  for r in records:
   if r['reason'] not in ['FAST_DROP','HS_BEAR_CLUSTER_EXIT','CIRCUIT_BREAKER_EXIT_ALL']:continue
   q=cf.one(r,run['path']);observed=ci.get(r['source'])
   if observed is not None:
    assert q['reason']==observed['reason'] and q['close_ms']==observed['close_ms'] and math.isclose(q['net'],observed['net'],abs_tol=1e-8),(run['arm'],r['source'],'conditional exit differs from real shared control')
   details.append({'source':r['source'],'kind':'FIXED_ENTRY_EXIT_COUNTERFACTUAL','actual_reason':r['reason'],'actual_close_ms':r['close_ms'],'counterfactual_close_ms':q['close_ms'],'actual_net':r['net'],'counterfactual_net':q['net'],'delta':r['net']-q['net'],'control_reason':q['reason'],'real_control_pair':observed is not None,'counterfactual_open_at_cut':q['close_ms'] is None,'entry_ms':r['open_ms'],'intervention_ms':r['close_ms']})
 byreason={reason:{'N':len(v:=[r for r in details if r['control_reason']==reason]),'delta':sum(r['delta'] for r in v),'mean_delta':statistics.mean(r['delta'] for r in v)} for reason in sorted({r['control_reason'] for r in details})};direct=sum(r['delta'] for r in details)
 return {'arm':run['arm'],'path':run['path'],'kind':'SELECTION' if isfilter else 'EXIT_REDUCER','N':len(details),'effective_exit_victims_N':0 if isfilter else len(details),'positive_N':sum(r['delta']>1e-10 for r in details),'negative_N':sum(r['delta']< -1e-10 for r in details),'delta_conditional':direct,'mean_delta':direct/len(details) if details else None,'real_shared_control_N':sum(r['real_control_pair'] for r in details),'by_control_reason':byreason,'details':details,'trigger_events_N':sum(e['event'] in ['FAST_DROP','HS_BEAR_CLUSTER_TRIGGERED','CB_EXIT_ALL_TRIGGERED'] for e in run['events']) if not isfilter else None}
def verify_prefix(r):
 p=B/'runs'/(r['arm']+'_BASE_'+r['path']+'.json');old=json.loads(p.read_bytes());n=len(old['equity_minute']);assert r['equity_minute'][:n]==old['equity_minute'],(r['arm'],r['path'],'old equity prefix mismatch');assert [x for x in r['admissions'] if x['at']<M['old_end_ms']]==old['admissions'];newby={x['source']:x for x in r['records']}
 for x in old['records']:
  if x['close_ms'] is not None:assert newby[x['source']]==x,(r['arm'],r['path'],x['source'],'closed old trade differs')
 return {'arm':r['arm'],'path':r['path'],'equity_prefix_minutes':n,'admission_prefix_exact':True,'closed_trade_prefix_exact':True}
def classify(two,act):
 gains=[x['delta_mean'] for x in two];g={'mean_threshold_both_paths':all(x is not None and x>=.005-1e-10 for x in gains),'halves_both_paths':all(x['delta_mean'] is not None and x['delta_mean']>1e-10 for r in two for x in r['halves_primary']),'excluding_top3_both_paths':all(r['excluding_top3']['delta_mean'] is not None and r['excluding_top3']['delta_mean']>1e-10 for r in two),'effective_interventions_both_paths':all(r['N']>0 for r in act),'favorable_conditional_pair_both_paths':all(r['delta_conditional']>1e-10 for r in act)}
 group='SEM_EVIDENCIA_DE_BENEFICIO' if any(x is not None and x<=1e-10 for x in gains) else 'CANDIDATA_A_FORWARD' if all(g.values()) else 'INCONCLUSIVA';return group,g

def main():
 assert all(not x['returncode'] for x in read('EXECUCOES.json'));assert len(list((S/'runs').glob('*.json')))==32
 summaries=[];comparisons=[];acts=[];mares=[];prefix=[];checks=[];cf=ExitCounterfactual()
 for path in ['HIGH_FIRST','LOW_FIRST']:
  controls={}
  for arm in c.ARMS:
   r=read('runs/'+arm+'_'+path+'.json');rr=r['records'];stat=metrics(rr);assert math.isclose(stat['net_total'],r['stats']['net_total'],abs_tol=1e-8);assert len(rr)==sum(x['accepted'] for x in r['admissions']);assert r['stats']['max_positions']<=5
   if arm not in c.COMBOS:prefix.append(verify_prefix(r))
   stat.update(arm=arm,path=path,DD_minute_mtm=r['stats']['DD_minute_mtm'],max_committed_usdt=r['stats']['max_committed_usdt'],funding=funding(rr),crises=r['stats']['crises']);summaries.append(stat)
   if arm==c.CONTROL:
    controls[arm]={k:r[k] for k in ['arm','path','records','admissions','events']};continue
   basearm=c.COMBOS[arm][0] if arm in c.COMBOS else c.CONTROL;base=controls[basearm];primary=compare(r,base);action=interventions(r,base,cf);primary['comparison_role']='PRIMARY';primary['direct_conditional_delta']=action['delta_conditional'];primary['trajectory_residual_vs_direct_conditional']=primary['delta_total']-action['delta_conditional'];comparisons.append(primary);acts.append(action)
   if arm in c.COMBOS:
    red=c.COMBOS[arm][1];informative=compare(r,controls[red]);informative['comparison_role']='REDUCER_INFORMATIVE_NOT_VETO';comparisons.append(informative)
   for state in STATES:
    sub=[x for x in rr if x['mare']==state];bsub=[x for x in base['records'] if x['mare']==state];mares.append({'arm':arm,'path':path,'mare':state,'primary_control':basearm,**difference(sub,bsub)})
   if arm not in c.COMBOS:controls[arm]={k:r[k] for k in ['arm','path','records','admissions','events']}
   checks.append({'arm':arm,'path':path,'economic_conservation':True,'admissions_and_capacity':True,'paired_decomposition':True,'conditional_exit_agrees_when_actual_control_common':True});print('ANALYZED',arm,path,'interventions',action['N'],flush=True)
 classification=[]
 for arm in c.ARMS:
  if arm==c.CONTROL:classification.append({'arm':arm,'group':'INCONCLUSIVA','reason':'REFERENCIA_SEM_COMPARADOR','gates':None,'effective_forward_recommendation':False});continue
  two=[x for x in comparisons if x['variant_arm']==arm and x['comparison_role']=='PRIMARY'];act=[x for x in acts if x['arm']==arm];assert len(two)==len(act)==2;group,g=classify(two,act);fast=arm==c.REDUCERS[-1] or arm in c.COMBOS and c.COMBOS[arm][1]==c.REDUCERS[-1];classification.append({'arm':arm,'group':group,'gates':g,'effective_forward_recommendation':group=='CANDIDATA_A_FORWARD' and not fast,'FAST_runtime_fidelity':'NAO_DEMONSTRADA' if fast else 'NAO_SE_APLICA_FAST','restriction':'BLOQUEADA_POR_FIDELIDADE_FAST' if fast else 'APENAS_TRIAGEM_PARA_REVISAO_NAO_AUTORIZA_FORWARD'})
 dump('RESUMO.json',summaries);dump('COMPARACOES.json',comparisons);dump('INTERVENCOES_E_CONTRAFACTUAIS.json',acts);dump('MARE_DIAGNOSTICO.json',mares);dump('CLASSIFICACAO.json',classification);dump('VERIFICACAO_PREFIXO_ECONOMICO.json',{'passed':len(prefix)==14,'checks':prefix});dump('VERIFICACAO_ECONOMICA.json',{'passed':True,'checks':checks,'counterfactual_unique_sources_paths':len(cf.cache)});print('ANALYSIS_COMPLETE',len(summaries),len(classification),flush=True)
if __name__=='__main__':main()
