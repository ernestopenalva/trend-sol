"""All-arm economic/robustness analysis after coverage; no strategy changes."""
import sys,json,collections,bisect,statistics,math,hashlib,csv
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import replay12_results as q
S=q.S

def compare(v,c,dim,t,w):
 def chosen(r):return [x for x in r['records'] if (t is None or x['mare']==t) and (w is None or x['onda']==w)]
 a,b=chosen(v),chosen(c);ai={x['source']:x for x in a};bi={x['source']:x for x in b};common=set(ai)&set(bi);deltas={s:ai[s]['net']-bi[s]['net'] for s in common};contributions={s:(ai[s]['net'] if s in ai else 0)-(bi[s]['net'] if s in bi else 0) for s in set(ai)|set(bi)};admit={x['source']:x['open_ms'] for x in a+b};days=collections.defaultdict(float);blocks=collections.defaultdict(float);monthly=collections.defaultdict(float);eps={p:collections.defaultdict(float) for p in ['mare','onda']}
 for src in sorted(contributions):
  value=contributions[src];at=admit[src];days[q.date(at)]+=value;monthly[q.date(at)[:7]]+=value;blocks[q.block(at)]+=value
  for pref in eps:eps[pref][q.episode(pref,at)]+=value
 total=sum(x['net'] for x in a)-sum(x['net'] for x in b);paired=sum(deltas.values());same_entry=sum(math.isclose(ai[s]['entry'],bi[s]['entry'],abs_tol=1e-12) for s in common);mid=q.START+(q.END-q.START)//2
 return {'arm':v['arm'],'control':c['arm'],'scenario':v['scenario'],'path':v['path'],'dimension':dim,'mare':t,'onda':w,'arm_N':len(a),'control_N':len(b),'arm_total':sum(x['net'] for x in a),'control_total':sum(x['net'] for x in b),'delta_total':total,'paired_N':len(common),'paired_closed_both_N':sum(ai[s]['close_ms'] is not None and bi[s]['close_ms'] is not None for s in common),'paired_same_entry_N':same_entry,'paired_delta':paired,'paired_delta_per_trade':paired/len(common) if common else None,'paired_closed_delta':sum(deltas[s] for s in common if ai[s]['close_ms'] is not None and bi[s]['close_ms'] is not None),'exclusive_and_censoring_residual':total-paired,'arm_only_N':len(set(ai)-set(bi)),'control_only_N':len(set(bi)-set(ai)),'delta_exposure_notional_hours':sum(x['notional']*x['duration_minutes']/60 for x in a)-sum(x['notional']*x['duration_minutes']/60 for x in b),'halves_entry_cohort_delta':[sum(contributions[s] for s in contributions if admit[s]<mid),sum(contributions[s] for s in contributions if admit[s]>=mid)],'monthly_entry_cohort_delta':dict(monthly),'day_concentration_delta':q.top(days),'episode_concentration_delta':{p:q.top(x) for p,x in eps.items()},'lodo_range_delta':[min((total-x for x in days.values()),default=None),max((total-x for x in days.values()),default=None)],'CI_day_delta':q.bootstrap(list(days.values())) if v['scenario']=='BASE' else None,'CI72h_delta':q.bootstrap(list(blocks.values())) if v['scenario']=='BASE' else None,'interpretation':'direct paired + admission/trajectory; not all residual attributable to experimental predicate'}

def forward_portfolio(r):
 # Compare only identifiable observed admissions; inherited state differs from June flat replay.
 import gzip
 f=json.loads(gzip.decompress((ROOT/'data/analysis/mare_onda_forward_20261021/fontes_economicas.json.gz').read_bytes()));arm=r['arm'];p='data/trades/trades_B.jsonl' if arm=='REAL_A' else 'data/trades/trades_'+arm.lower()+'.jsonl';B=q.datetime.fromisoformat('2026-10-08T23:52:00-03:00').timestamp()*1000;FEND=min(q.END,int(q.datetime.fromisoformat(f['capture_start_utc']).timestamp()*1000))
 obs=[x for x in map(json.loads,f['files'][p]['data'].splitlines()) if x.get('opened_at') and B<=q.datetime.fromisoformat(x['opened_at']).timestamp()*1000<=FEND and (arm!='REAL_A' or x.get('position_type')=='BOT_EXIT' and not x.get('phantom'))];o={x['source_candle_open_time']:x for x in obs};model={x['source']:x for x in r['records'] if B<=x['open_ms']<=FEND};common=set(o)&set(model);reasons=[s for s in common if model[s]['close_ms'] is not None and model[s]['reason']==o[s]['exit_reason']]
 return {'arm':arm,'path':r['path'],'observed_closed_N':len(o),'comparison_end_brt':q.iso(FEND),'model_window_admissions':len(model),'common_sources':len(common),'admission_recall':len(common)/len(o) if o else None,'admission_precision':len(common)/len(model) if model else None,'reason_matches_common':len(reasons),'not_direct_errors':'different June initial portfolio, actual inherited forward state, feed/ATR/intrabar/fill differences; no automatic equivalence'}

def funding(r):
 events=[]
 for x in r['records']:
  events.append((x['open_ms'],1,-x['notional']-x['fee_est']/2))
  if x['close_ms'] is not None:
   priority=0 if x['reason'] in ['HARD_STOP_ELASTIC_CONTEXT_LOST','CIRCUIT_BREAKER_EXIT_ALL'] and x['close_ms']%60000==0 else 2
   events.append((x['close_ms'],priority,x['quantity']*x['exit']-x['fee_est']/2))
 cash=minimum=100.
 for t,kind,flow in sorted(events):cash+=flow;minimum=min(minimum,cash)
 return {'cash_initial_reference':100.,'minimum_cash_without_new_funding':minimum,'additional_initial_cash_required':max(0.,-minimum),'initial_cash_required_to_support_trajectory':100.+max(0.,-minimum),'cash_final_excluding_open_positions_value':cash,'cash_gate_simulated':False,'note':'fixed nominal runtime budget, not claimed self-financed wallet; estimated half fees each side'}

def load_completed(arm,scenario,path):
 import time
 p=S/'runs'/(arm+'_'+scenario+'_'+path+'.json')
 while True:
  try:return json.loads(p.read_bytes())
  except (FileNotFoundError,json.JSONDecodeError):
   status=S/'EXECUCOES.json'
   if status.exists():
    rows=json.loads(status.read_bytes())
    if any(x['returncode'] for x in rows):raise RuntimeError('Replay failed; analysis cannot fabricate missing results')
   time.sleep(1)

class RunShelf:
 def __init__(self,scenario,path):self.scenario=scenario;self.path=path;self.slim={}
 def __getitem__(self,a):return self.slim[a]
 def items(self):
  for a in q.ARMS:
   r=load_completed(a,self.scenario,self.path)
   yield a,r
   self.slim[a]={k:r[k] for k in ['arm','scenario','path','records','admissions']}
   del r

def main():
 if not (S/'NIVEL_0.md').exists():raise RuntimeError('Coverage delivery must precede economic analysis')
 prices=[]
 for l in (S/'market/1m.jsonl').open():
  x=json.loads(l)
  if q.START<=x['open_time_ms'] and x['close_time_ms']<q.END:prices.append((x['close_time_ms'],x['close']))
 summary=[];metrics=[];pairs=[];exits=[];robust=[];forward=[];events=[];overlap=[];checks=[];destinies=[];blocking=[];quality=[]
 audit_real=json.loads((S/'AUDITORIA_ADMISSAO_REAL_A.json').read_bytes());assert audit_real['complete_6_cases'] and not audit_real['mismatches']
 real_reasons={x['case']:{z['source']:z['registry_reason'] for z in x['details']} for x in audit_real['cases']}
 upstream=[q.REG[str(x['at'])] for x in q.SIGNALS['REAL_A']]
 assert json.loads((S/'DMI_UNIVERSO_COMUM_VERIFICADO.json').read_bytes())['predicate_subset_equals_independent_DMI_engine']
 for scenario in q.COSTS:
  for path in ['HIGH_FIRST','LOW_FIRST']:
   runs=RunShelf(scenario,path);print('ANALYZE',scenario,path,flush=True)
   for arm,r in runs.items():
    dds=q.attributed_minute_dd(r,prices);rr=r['records'];ops=r['admissions'];base=q.summarize(rr,ops,dds.get(('TOTAL',None,None),0),scenario,path,primary=True);base['DD_engine_minute']=r['stats']['DD_minute_mtm'];base['max_committed_usdt']=r['stats']['max_committed_usdt'];base['crises']=r['stats']['crises'];base.update(arm=arm,scenario=scenario,path=path);
    opreasons=real_reasons.get(arm+'_'+scenario+'_'+path+'.json',{})
    for dim in ['TOTAL','MARE','ONDA','MARE_ONDA']:
     combos=[(None,None)] if dim=='TOTAL' else [(t,None) for t in q.STATES] if dim=='MARE' else [(None,w) for w in q.STATES] if dim=='ONDA' else q.cells()
     for tt,ww in combos:
      oo=[x for x in ops if (tt is None or x['mare']==tt) and (ww is None or x['onda']==ww)];counts=collections.Counter(opreasons.get(x['source'],x['reason']) for x in oo)
      for reason,n in counts.items():blocking.append({'arm':arm,'scenario':scenario,'path':path,'dimension':dim,'mare':tt,'onda':ww,'reason':reason,'N':n,'own_opportunity_denominator':len(oo),'pct':100*n/len(oo),'REAL_reason_source':'actual registry nominal predicate' if arm=='REAL_A' else 'runtime shadow predicate'})
    base['funding_diagnostic']=funding(r);base['potential_REAL_opportunities']=len(upstream);base['PnL_per_potential_REAL_opportunity']=base['net_total']/len(upstream);summary.append(base)
    checks.append({'arm':arm,'case':scenario+'/'+path,'equity_conservation':math.isclose(sum(x['net'] for x in rr),r['stats']['net_total'],abs_tol=1e-8),'DD_independent_event_sweep':math.isclose(base['DD_engine_minute'],base['DD_minute_mtm_attributed'],abs_tol=1e-7),'admissions_conservation':len(rr)==sum(x['accepted'] for x in ops),'unique_sources':len({x['source'] for x in rr})==len(rr),'capacity':r['stats']['max_positions']<=5,'no_future_trade':all(q.START<=x['open_ms']<q.END and (x['close_ms'] is None or x['open_ms']<=x['close_ms']<q.END) for x in rr)})
    daily=r['daily_contributions'];ddays=q.top(daily);blocks=collections.defaultdict(float);months=collections.defaultdict(float);mid=q.START+(q.END-q.START)//2
    for day,value in daily.items():at=int(q.datetime.fromisoformat(day+'T00:00:00-03:00').timestamp()*1000);blocks[q.block(at)]+=value;months[day[:7]]+=value
    robust.append({'arm':arm,'scenario':scenario,'path':path,'daily_MTM_flow_concentration':ddays,'monthly_MTM_flow':dict(months),'half_cut_brt':q.iso(mid),'half_MTM_flow':[r['equity_minute'][max(0,int((mid-q.START)//60000)-1)],r['stats']['net_total']-r['equity_minute'][max(0,int((mid-q.START)//60000)-1)]],'CI_day_MTM_flow':q.bootstrap(list(daily.values())),'CI72h_MTM_flow':q.bootstrap(list(blocks.values())),'LODO_MTM_flow_range':[min(sum(daily.values())-v for v in daily.values()),max(sum(daily.values())-v for v in daily.values())],'trade_cohort_concentration':base['day_concentration']})
    for dimension in ['MARE','ONDA','MARE_ONDA']:
     combos=[(t,None) for t in q.STATES] if dimension=='MARE' else [(None,w) for w in q.STATES] if dimension=='ONDA' else q.cells()
     for t,w in combos:
      cell=[x for x in rr if (t is None or x['mare']==t) and (w is None or x['onda']==w)];oo=[x for x in ops if (t is None or x['mare']==t) and (w is None or x['onda']==w)];z=q.summarize(cell,oo,dds.get((dimension,t,w),0),scenario,path,primary=scenario=='BASE');up=sum((t is None or x['mare']==t) and (w is None or x['onda']==w) for x in upstream);z['potential_REAL_opportunities']=up;z['PnL_per_potential_REAL_opportunity']=z['net_total']/up if up else None;metrics.append({'arm':arm,'scenario':scenario,'path':path,'dimension':dimension,'mare':t,'onda':w,'stats':z})
      for reason,v in z['exit_reasons'].items():exits.append({'arm':arm,'scenario':scenario,'path':path,'dimension':dimension,'mare':t,'onda':w,'reason':reason,'N':v['N'],'closed_denominator':z['closed'],'pct_closed':v['pct_closed']})
    if scenario=='BASE':
     forward.append(forward_portfolio(r))
     for e in r['events']:
      at=e.get('at')
      if at is None and e.get('ts'):
       try:at=int(q.datetime.fromisoformat(e['ts']).timestamp()*1000)
       except Exception:continue
      if at is None:continue
      reported_at=at
      if e.get('intelligence')=='HS_BULL_ELASTIC' and (e.get('decision') in ['HOLD','REARM_NORMAL_HS'] or 'CONTEXT_LOST' in (e.get('reasons') or [])):at+=1
      labels={p:q.TL[p][bisect.bisect_right(q.TIMES[p],at)-1]['label'] for p in q.TL};events.append({'arm':arm,'scenario':scenario,'path':path,**e,'decision_or_activation_at_ms':at,'reported_event_at_ms':reported_at,'mare':labels['mare'],'onda':labels['onda'],'availability':'idealized closed public candles at event; not retrospective exit context'})
   for arm in q.ARMS:
    if arm in ['REAL_A','BE_OFF_CB_SHADOW']:continue
    control='REAL_A' if arm=='DMI15_TRAJECTORY_CONTEXT_SHADOW' else 'BE_OFF_CB_SHADOW';v,c=runs[arm],runs[control]
    for dim in ['TOTAL','MARE','ONDA','MARE_ONDA']:
     combos=[(None,None)] if dim=='TOTAL' else [(t,None) for t in q.STATES] if dim=='MARE' else [(None,w) for w in q.STATES] if dim=='ONDA' else q.cells()
     for t,w in combos:pairs.append(compare(v,c,dim,t,w))
    if scenario=='BASE':
     if arm in ['BE_OFF_CB_MACD_BU_MINUS_SHADOW','BE_OFF_CB_EMA_MACD_SHADOW','EMA_MACD_HIST_1M_SHADOW','DMI15_TRAJECTORY_CONTEXT_SHADOW']:
      admitted={x['source'] for x in v['records']};rejected={x['source'] for x in v['admissions'] if x['reason'].startswith('ENTRY_BLOCKED_EMA_MACD') or x['reason'] in ['ENTRY_BLOCKED_MACD_BU_MINUS','ENTRY_BLOCKED_HISTOGRAM','ENTRY_BLOCKED_CONFIRMATION_1M']}
      if arm=='DMI15_TRAJECTORY_CONTEXT_SHADOW':rejected={x['signal']['source_candle_open_time'] for x in q.SIGNALS['REAL_A']}-{x['signal']['source_candle_open_time'] for x in q.SIGNALS['DMI15_TRAJECTORY_CONTEXT_SHADOW']}
      for group,ss in [('explicit_filter_rejection',rejected),('variant_admitted',admitted)]:
       xx=[x for x in c['records'] if x['source'] in ss];quality.append({'arm':arm,'control':control,'path':path,'group':group,'signals_N':len(ss),'control_observable_N':len(xx),'control_total':sum(x['net'] for x in xx),'control_mean':statistics.mean(x['net'] for x in xx) if xx else None,'scope':'only control admissions legitimately observed; not all rejected candidates'})
     ci={x['source']:x for x in c['records']};vi={x['source']:x for x in v['records']}
     for src in sorted(set(ci)&set(vi)):
      a,b=vi[src],ci[src];destinies.append({'arm':arm,'control':control,'path':path,'source':src,'mare':a['mare'],'onda':a['onda'],'variant_reason':a['reason'],'control_reason':b['reason'],'variant_net':a['net'],'control_net':b['net'],'delta':a['net']-b['net'],'both_closed':a['close_ms'] is not None and b['close_ms'] is not None,'changed_exit':a['reason']!=b['reason'] or a['close_ms']!=b['close_ms']})
   if scenario=='BASE':
    for a in q.ARMS:
     aa={x['source'] for x in runs[a]['records']};oa={x['source'] for x in runs[a]['admissions']}
     for b in q.ARMS:
      bb={x['source'] for x in runs[b]['records']};ob={x['source'] for x in runs[b]['admissions']};overlap.append({'path':path,'arm_a':a,'arm_b':b,'trade_intersection':len(aa&bb),'trade_union':len(aa|bb),'opportunity_intersection_own':len(oa&ob),'opportunity_union_own':len(oa|ob)})
 q.dump('QUALIDADE_SELECAO_OBSERVAVEL.json',quality);q.csvout('BLOQUEIOS_ADMISSAO.csv',blocking);q.dump('DESTINOS_CONTROLE_PARES.json',destinies);q.dump('RESUMO_ECONOMICO.json',summary);q.dump('METRICAS_REGIMES.json',metrics);q.dump('COMPARACOES.json',pairs);q.dump('ROBUSTEZ.json',robust);q.dump('FIDELIDADE_CARTEIRAS.json',forward);q.dump('INTERVENCOES_REGIMES.json',events);q.dump('SOBREPOSICAO.json',overlap);q.dump('VERIFICACAO_ECONOMICA.json',{'checks':checks,'passed':all(all(v for k,v in x.items() if k not in ['arm','case']) for x in checks)})
 q.csvout('MOTIVOS_SAIDA.csv',exits);flat=[]
 for x in metrics:flat.append({k:v for k,v in x.items() if k!='stats'}|{k:v for k,v in x['stats'].items() if not isinstance(v,(dict,list))})
 q.csvout('METRICAS_REGIMES.csv',flat);q.csvout('COMPARACOES.csv',[{k:v for k,v in x.items() if not isinstance(v,(dict,list))} for x in pairs]);assert all(all(v for k,v in x.items() if k not in ['arm','case']) for x in checks),'Independent arithmetic/causality gate failed';print('ANALYSIS_COMPLETE',len(summary),len(metrics),len(pairs),flush=True)
if __name__=='__main__':main()
