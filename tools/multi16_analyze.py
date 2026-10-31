"""Frozen primary rule, intervention decomposition and descriptive regime cells."""
from pathlib import Path
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
import sys,json,math,statistics
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import multi16_engine as m;import combo9_analyze as a
S=m.S;MAN=m.MAN;START=MAN['start_ms'];END=MAN['end_ms'];CUT=START+(END-START)//2
a.S=S;a.M=MAN;a.START=START;a.END=END;a.CUT=CUT
PATHS=['HIGH_FIRST','LOW_FIRST'];STATES=['BULL','BEAR','EXPA','COMP']
def read(n):return json.loads((S/n).read_bytes())
def dump(n,x):m.e.dump(S/n,x)
def descriptor(arm):
 if arm in m.MULTI:return m.MULTI[arm]
 if arm in m.c.COMBOS:
  base,red=m.c.COMBOS[arm];return base,(dict(zip(m.REDUCERS,['CLUSTER','CB','FAST']))[red],)
 if arm in m.REDUCERS:return m.CONTROL,(dict(zip(m.REDUCERS,['CLUSTER','CB','FAST']))[arm],)
 return arm,()
DES={k:descriptor(k) for k in m.ARMS};LOOKUP={(base,frozenset(flags)):arm for arm,(base,flags) in DES.items()}
def short(arm):
 if arm in m.MULTI:
  base,flags=DES[arm];return short(base)+' + '+' + '.join(flags)
 if arm in m.c.COMBOS:
  base,flags=DES[arm];return short(base)+' + '+flags[0]
 return {m.CONTROL:'BE_OFF_CB',m.FILTERS[0]:'MACD_BU_MINUS',m.FILTERS[1]:'EMA_MACD',m.FILTERS[2]:'HIST_1M',m.REDUCERS[0]:'CLUSTER',m.REDUCERS[1]:'CB_EXIT_ALL',m.REDUCERS[2]:'FAST'}.get(arm,arm)
def quick(v,b):
 # Same descriptive comparison; do not spend additional bootstrap replicates on secondary contrasts.
 fn=a.bootstrap;a.bootstrap=lambda *args:{'CI95':None,'reason':'secondary descriptive contrast, no added statistical test'}
 try:return a.compare(v,b)
 finally:a.bootstrap=fn
def main():
 assert len(list((S/'runs').glob('*.json')))==64
 assert read('VERIFICACAO_REFERENCIAS_PREFIXO.json')['passed']
 summaries=[];comparisons=[];acts=[];pieces=[];cells=[];extras=[];cascade=[];concurrent=[];checks=[];cf=a.ExitCounterfactual()
 # Reuse already computed identical-entry standalone-exit probes, never historical portfolio totals.
 oldacts=json.loads((m.OLD/'INTERVENCOES_E_CONTRAFACTUAIS.json').read_bytes())
 for old in oldacts:
  if old['kind']!='EXIT_REDUCER':continue
  oldrun=json.loads((m.OLD/'runs'/(old['arm']+'_'+old['path']+'.json')).read_bytes());rr={r['source']:r for r in oldrun['records']}
  for d in old['details']:
   if d['counterfactual_close_ms'] is None:continue
   t=rr[d['source']];key=(old['path'],t['source'],t['entry'],t['atr']);q={'net':d['counterfactual_net'],'reason':d['control_reason'],'close_ms':d['counterfactual_close_ms']}
   if key in cf.cache:assert cf.cache[key]==q
   cf.cache[key]=q
 for path in PATHS:
  runs={arm:read('runs/'+arm+'_'+path+'.json') for arm in m.ARMS}
  for arm in m.ARMS:
   r=runs[arm];rr=r['records'];base,flags=DES[arm];basearm=base if flags else m.CONTROL;b=runs[basearm];stat=a.metrics(rr)
   assert len(rr)==sum(x['accepted'] for x in r['admissions']);assert math.isclose(stat['net_total'],r['stats']['net_total'],abs_tol=1e-8);assert r['stats']['max_positions']<=5
   for t in rr:assert t['status']==('OPEN' if t['close_ms'] is None else 'CLOSED') and t['reducer']==m.REASONS.get(t['reason']);assert 'mare' in t and 'onda' in t
   stat.update(arm=arm,label=short(arm),path=path,base=basearm,reducers=list(flags),new=arm in m.MULTI,DD_minute_mtm=r['stats']['DD_minute_mtm'],crises=r['stats']['crises'],funding=a.funding(rr));summaries.append(stat)
   if arm!=m.CONTROL:
    primary=a.compare(r,b);primary['comparison_role']='PRIMARY';action=a.interventions(r,b,cf);primary['direct_conditional_delta']=action['delta_conditional'];primary['trajectory_residual_vs_direct_conditional']=primary['delta_total']-action['delta_conditional'];comparisons.append(primary);acts.append(action)
    ci={q['source']:q for q in b['records']};ri={q['source']:q for q in rr}
    for flag in flags:
     dd=[d for d in action['details'] if m.REASONS.get(d.get('actual_reason'))==flag];victims=[t for t in rr if t['reducer']==flag];common=[t for t in victims if t['source'] in ci];exclusive=[t for t in victims if t['source'] not in ci]
     pieces.append({'arm':arm,'path':path,'reducer':flag,'N':len(victims),'share_all_trades':len(victims)/len(rr) if rr else 0,'conditional_fixed_entry_delta':sum(d['delta'] for d in dd),'actual_common_N':len(common),'actual_common_delta':sum(t['net']-ci[t['source']]['net'] for t in common),'exclusive_victims_N':len(exclusive),'exclusive_victims_actual_net':sum(t['net'] for t in exclusive),'by_counterfactual_reason':{reason:{'N':len(q:=[d for d in dd if d['control_reason']==reason]),'delta':sum(d['delta'] for d in q)} for reason in sorted({d['control_reason'] for d in dd})},'portfolio_delta_not_attributable_to_piece':primary['delta_total'],'portfolio_exclusive_residual_shared_across_pieces':primary['exclusive_residual'],'trajectory_residual_total_minus_all_direct':primary['trajectory_residual_vs_direct_conditional']})
    if arm in m.MULTI:
     for remove in flags:
      control=LOOKUP[(base,frozenset(f for f in flags if f!=remove))];q=quick(r,runs[control]);q.update(comparison_role='REMOVE_ONE_PORTFOLIO',removed=remove);comparisons.append(q)
    elif arm in m.c.COMBOS:
     reducer=m.c.COMBOS[arm][1];q=quick(r,runs[reducer]);q['comparison_role']='REDUCER_ONLY_INFORMATIVE';comparisons.append(q)
   for mare in STATES:
    for onda in STATES:cells.append({'arm':arm,'path':path,'mare':mare,'onda':onda,**a.metrics([t for t in rr if t['mare']==mare and t['onda']==onda])})
   unknown=[t for t in rr if t['mare'] not in STATES or t['onda'] not in STATES];extras.append({'arm':arm,'path':path,**a.metrics(unknown),'labels':dict(Counter(t['mare']+' x '+t['onda'] for t in unknown))});assert sum(x['N'] for x in cells[-16:])+len(unknown)==len(rr)
   if arm in m.MULTI:
    assert len(r['cb_edges'])==r['stats']['crises'],(arm,path,'CB diagnostic missing')
    rows=r['cascades'];cascade.append({'arm':arm,'path':path,'necessary_cascades_with_liquidation':sum(x['cluster_necessary_for_this_boundary'] and x['victims_N']>0 for x in rows),'concomitant_with_liquidation':sum(not x['cluster_necessary_for_this_boundary'] and x['victims_N']>0 for x in rows),'necessary_victims':sum(x['victims_N'] for x in rows if x['cluster_necessary_for_this_boundary']),'cluster_transition_without_survivors':sum(x['victims_N']==0 for x in rows),'same_event_victims':sum(x['victims_N'] for x in rows if x['event_lags'] and max(x['event_lags'])==0),'next_event_victims':sum(x['victims_N'] for x in rows if x['event_lags'] and max(x['event_lags'])==1),'later_event_victims':sum(x['victims_N'] for x in rows if x['event_lags'] and max(x['event_lags'])>1),'details':rows})
    concurrent.append({'arm':arm,'path':path,'records':r['concurrent'],'N':len(r['concurrent']),'potential_overlap_N':sum(x.get('FAST',False)+x.get('CLUSTER_potential_before_normal',False)+x.get('CB_transition_pending',False)>1 for x in r['concurrent'])})
   checks.append({'arm':arm,'path':path,'economic_conservation':True,'admissions_and_status':True,'regime_cells_conserve_N':True,'capacity':True});print('ANALYZED',arm,path,flush=True)
 classes=[];redundancy=[]
 for piece in pieces:
  run=read('runs/'+piece['arm']+'_'+piece['path']+'.json');event={'FAST':'FAST_DROP','CLUSTER':'HS_BEAR_CLUSTER_TRIGGERED','CB':'CB_EXIT_ALL_TRIGGERED'}[piece['reducer']];piece['trigger_events_N']=sum(x['event']==event for x in run['events'])
 for arm in m.ARMS:
  if arm==m.CONTROL:classes.append({'arm':arm,'label':short(arm),'group':'INCONCLUSIVA','reason':'REFERENCIA_SEM_COMPARADOR','gates':None});continue
  two=[x for x in comparisons if x['variant_arm']==arm and x['comparison_role']=='PRIMARY'];ac=[x for x in acts if x['arm']==arm];assert len(two)==len(ac)==2;group,gates=a.classify(two,ac);flags=DES[arm][1]
  classes.append({'arm':arm,'label':short(arm),'base':DES[arm][0] if flags else m.CONTROL,'new':arm in m.MULTI,'group':group,'gates':gates,'FAST_runtime_fidelity':'NAO_DEMONSTRADA' if 'FAST' in flags else 'NAO_SE_APLICA','effective_forward_recommendation':False,'restriction':'FAST_NAO_RECOMENDAVEL_FORWARD_COM_ESTE_REPLAY' if 'FAST' in flags else 'APENAS_TRIAGEM_PARA_REVISAO_HUMANA'})
  for flag in flags:
   pp=[p for p in pieces if p['arm']==arm and p['reducer']==flag];assert len(pp)==2;low=all(p['N']<5 or p['share_all_trades']<.01 for p in pp);redundancy.append({'arm':arm,'reducer':flag,'low_activity_both_paths':low,'classification':'REDUNDANTE_BAIXA_ATUACAO_NESTA_AMOSTRA' if low else 'ATUACAO_MATERIAL_EM_AO_MENOS_UM_PATH','paths':[{k:p[k] for k in ['path','N','share_all_trades']} for p in pp],'further_piece_analysis':'STOP' if low else 'NO_ADDITIONAL_OPTIMIZATION'})
 for row in classes:row['validade_proposta_no_objeto_idealizado']='VALIDO_COM_RESSALVAS';row['homologacao_cientifica']=False;row['equivalencia_runtime_demonstrada']=False
 for n,x in [('RESUMO.json',summaries),('COMPARACOES.json',comparisons),('INTERVENCOES.json',acts),('PECAS.json',pieces),('MATRIZ_MARE_ONDA.json',cells),('REGIMES_FORA_16.json',extras),('CLASSIFICACAO.json',classes),('REDUNDANCIA.json',redundancy),('CASCATAS.json',cascade),('CONCORRENCIA.json',concurrent)]:dump(n,x)
 dump('VERIFICACAO_ECONOMICA.json',{'passed':True,'checks':checks,'counterfactual_unique_sources_paths':len(cf.cache),'cut_ms':CUT,'scientific_approval':False});print('ANALYSIS64_COMPLETE',flush=True)
if __name__=='__main__':main()
