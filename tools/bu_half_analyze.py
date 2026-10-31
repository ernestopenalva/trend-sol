"""BU_HALF economic tables, paired decomposition, HS windows and frozen gates."""
from pathlib import Path
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
import sys,json,math,csv,bisect,random,statistics,hashlib
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import bu_half_engine as g
import stack_guard_analyze as old
S=g.S;MAN=g.read('market_manifest.json');START=MAN['start_ms'];END=MAN['end_ms'];CUT=START+(END-START)//2;BRT=timezone(timedelta(hours=-3));M=60000
old.S=S;old.START=START;old.END=END;old.CUT=CUT
ARMS=['BE_OFF_CB','MACD_BU_MINUS','BU_HALF','UNIFORME'];PATHS=g.PATHS
PERIODS=[('TOTAL',START,END),('METADE_1',START,CUT),('METADE_2',CUT,END)]
read=g.read;dump=g.dump;table=old.table;csv_write=old.csv_write;brt=old.brt;day=old.day
MARKET=None
def metric(rs):
 out=old.metric(rs);notional=sum(t['notional'] for t in rs)
 out.update(notional_total=notional,net_por_100=out['net_total']/notional*100 if notional else None,mean_notional=notional/len(rs) if rs else None,mean_duration_minutes=statistics.mean(t['duration_minutes'] for t in rs) if rs else None)
 return out
def diff(v,b):
 vm=metric(v);bm=metric(b)
 return {'delta_net_total':vm['net_total']-bm['net_total'],'delta_net_por_100':vm['net_por_100']-bm['net_por_100'] if vm['net_por_100'] is not None and bm['net_por_100'] is not None else None,'delta_net_per_trade':vm['net_per_trade']-bm['net_per_trade'] if vm['net_per_trade'] is not None and bm['net_per_trade'] is not None else None,'variant':vm,'control':bm}
def price_at(t,path):
 at=t//M*M;c=MARKET.get(at)
 if c is None:return MARKET[(END-M)]['close']
 points=[c['open'],c['low'],c['high'],c['close']] if path=='LOW_FIRST' else [c['open'],c['high'],c['low'],c['close']]
 xs=[0,20000,40000,59999];offset=t-at
 for i in range(3):
  if offset<=xs[i+1]:return points[i]+(points[i+1]-points[i])*(offset-xs[i])/(xs[i+1]-xs[i])
 return points[-1]
def equity_at(r,t):
 if t<=START:return 0.
 if t>=END:return r['equity_minute'][-1]
 # Use frozen 1m values whenever directly available, exact path MTM at the split.
 if t%M==59999:return r['equity_minute'][(t-START)//M]
 price=price_at(t,r['path']);realized=sum(q['net'] for q in r['records'] if q['close_ms'] is not None and q['close_ms']<=t)
 unreal=sum((price*.99965/q['entry']-1-.002)*q['notional'] for q in r['records'] if q['open_ms']<=t and (q['close_ms'] is None or q['close_ms']>t))
 return realized+unreal
def calendar(r,a,b):
 initial=equity_at(r,a);peak=initial;dd=0.;lo=max(0,(a-START)//M);hi=min(len(r['equity_minute']),(b-START+M-1)//M)
 vals=[v for i,v in enumerate(r['equity_minute'][lo:hi],lo) if a<=START+i*M+59999<b]
 vals.append(equity_at(r,b))
 for value in vals:peak=max(peak,value);dd=max(dd,peak-value)
 events=defaultdict(lambda:[0.,0.,0,0]);weight=0.;countweight=0.
 for t in r['records']:
  x=max(a,t['open_ms']);y=min(b,t['close_ms'] if t['close_ms'] is not None else END)
  if y<x or t['open_ms']>=b or (t['close_ms'] is not None and t['close_ms']<a):continue
  weight+=t['notional']*(y-x);countweight+=y-x;events[x][0]+=t['notional'];events[x][2]+=1;events[y][1]+=t['notional'];events[y][3]+=1
 current=maximum=0.;count=maxcount=0
 for stamp,(add,remove,nadd,nremove) in sorted(events.items()):
  current+=add;count+=nadd;maximum=max(maximum,current);maxcount=max(maxcount,count);current-=remove;count-=nremove
 return {'DD_minute_mtm':dd,'net_calendar_mtm':equity_at(r,b)-initial,'exposure_mean_usdt':weight/(b-a),'exposure_integrated_usdt_hours':weight/3600000,'exposure_max_usdt':maximum,'positions_mean':countweight/(b-a),'positions_max':maxcount,'carry_in_N':sum(t['open_ms']<a and (t['close_ms'] is None or t['close_ms']>=a) for t in r['records'])}

def robust(v,b,kind):
 vn=sum(t['notional'] for t in v);bn=sum(t['notional'] for t in b);contrib=defaultdict(float)
 for t in v:contrib[day(t['open_ms'])]+=t['net']*(100/vn if kind=='EFFICIENCY' else 1)
 for t in b:contrib[day(t['open_ms'])]-=t['net']*(100/bn if kind=='EFFICIENCY' else 1)
 tops=sorted([(d,x) for d,x in contrib.items() if x>1e-10],key=lambda x:(-x[1],x[0]))[:3];removed={x[0] for x in tops}
 trimmed=[]
 for name,a,z in PERIODS:
  vv=[t for t in v if a<=t['open_ms']<z and day(t['open_ms']) not in removed];bb=[t for t in b if a<=t['open_ms']<z and day(t['open_ms']) not in removed]
  trimmed.append({'period':name,**diff(vv,bb)})
 assert math.isclose(sum(contrib.values()),diff(v,b)['delta_net_por_100' if kind=='EFFICIENCY' else 'delta_net_total'],abs_tol=1e-8)
 return {'ranking_metric':kind,'attribution':'BRT entry day; same days removed from both portfolios; no replay or uniform recalibration','top3':[{'day':d,'contribution':x} for d,x in tops],'trimmed_periods':trimmed,'daily_contributions':dict(contrib)}

def bootstrap(v,b):
 first=datetime.fromtimestamp(START/1000,BRT).date();last=datetime.fromtimestamp((END-1)/1000,BRT).date();days=[(first+timedelta(days=i)).isoformat() for i in range((last-first).days+1)];groups=[]
 for i in range(0,len(days),3):
  keys=set(days[i:i+3]);vv=[t for t in v if day(t['open_ms']) in keys];bb=[t for t in b if day(t['open_ms']) in keys];groups.append((sum(t['net'] for t in vv),sum(t['notional'] for t in vv),sum(t['net'] for t in bb),sum(t['notional'] for t in bb)))
 rng=random.Random(20261027);eff=[];net=[]
 for _ in range(5000):
  vs=vn=bs=bn=0.
  for _ in groups:
   q=rng.choice(groups);vs+=q[0];vn+=q[1];bs+=q[2];bn+=q[3]
  if vn and bn:eff.append(vs/vn*100-bs/bn*100);net.append(vs-bs)
 def ci(xs):
  xs.sort();return [xs[int(.025*(len(xs)-1))],xs[int(.975*(len(xs)-1))]]
 return {'seed':20261027,'replicates':5000,'blocks_72h':len(groups),'delta_efficiency_CI95':ci(eff),'delta_net_total_CI95':ci(net),'conditional_on_observed_trajectories':True,'candidate_veto':False}

def divergence(v,b,label):
 va={x['source']:x for x in v['admissions']};ba={x['source']:x for x in b['admissions']};dif=[s for s in ba if ba[s]['accepted']!=va[s]['accepted']]
 first=min(dif,key=lambda s:ba[s]['at']) if dif else None
 # Boundary tuple: timestamp, realized equity, peak, pause_until, crises, was_true, paused.
 assert [x[0] for x in v['cb_trace']]==[x[0] for x in b['cb_trace']]
 money=next(({'at':x[0],'BRT':brt(x[0]),'base':y,'variant':x} for x,y in zip(v['cb_trace'],b['cb_trace']) if abs(x[1]-y[1])>1e-9),None)
 pause=next(({'at':x[0],'BRT':brt(x[0]),'base':y,'variant':x} for x,y in zip(v['cb_trace'],b['cb_trace']) if x[6]!=y[6] or x[3]!=y[3] or x[4]!=y[4]),None)
 threshold=next(({'at':x[0],'BRT':brt(x[0]),'base':y,'variant':x} for x,y in zip(v['cb_trace'],b['cb_trace']) if x[5]!=y[5]),None)
 bi={t['source']:t for t in b['records']};sized=[t for t in v['records'] if t['source'] in bi and t['notional']!=bi[t['source']]['notional']]
 first_size=min(sized,key=lambda t:t['open_ms']) if sized else None
 if label=='BU_HALF' and first is not None:
  assert pause is not None and pause['at']<=ba[first]['at'],('UNEXPLAINED_ADMISSION_DIVERGENCE',v['path'],first)
  assert money is not None and money['at']<=pause['at']
 return {'arm':label,'path':v['path'],'first_size_difference':{'source':first_size['source'],'entry_BRT':brt(first_size['open_ms']),'base_notional':bi[first_size['source']]['notional'],'variant_notional':first_size['notional']} if first_size else None,'first_financial_CB_input_state_difference':money,'first_CB_condition_difference':threshold,'first_CB_pause_or_crisis_difference':pause,'first_admission_difference':{'source':first,'at':ba[first]['at'],'BRT':brt(ba[first]['at']),'base':ba[first],'variant':va[first]} if first is not None else None,'admission_difference_N':len(dif),'BU_HALF_pre_divergence_explanation':'admissions identical until financially-dependent CB divergence' if label=='BU_HALF' else 'existing MACD predicate blocking' if label=='MACD_BU_MINUS' else 'uniform sizing changes financial CB inputs','CB_trace_columns':['at','realized_equity','peak','pause_until','crises','condition','paused']}

def main():
 global MARKET
 assert read('PARIDADE.json')['passed'];assert read('VIABILIDADE_UNIFORME.json')['passed'];assert all(x['returncode']==0 for x in read('EXECUCOES.json'));assert len(list((S/'runs').glob('*.json')))==8
 MARKET={x['open_time_ms']:x for x in map(json.loads,(S/'market/1m.jsonl').open())};summaries=[];comparisons=[];robustness=[];decompositions=[];reduced=[];divergences=[];blocks=[];trades=[];checks=[];exposure_notes=[]
 allruns={};uniform_notional=read('UNIFORME_CALIBRACAO.json')['notional_uniforme']
 for path in PATHS:
  runs={arm:old.augment(read('runs/'+arm+'_'+path+'.json')) for arm in ARMS};allruns[path]=runs;base=runs['BE_OFF_CB']
  for arm,r in runs.items():
   rr=r['records'];assert len(rr)==sum(x['accepted'] for x in r['admissions']);assert math.isclose(sum(t['net'] for t in rr),r['stats']['net_total'],abs_tol=1e-8);assert r['stats']['max_positions']<=5
   audits={x['source']:x for x in r['sizing_audit']};pred={x['source']:x for x in read('PREDICADO_BU_MINUS.json')['rows']}
   assert len(r['cb_trace'])==len(r['equity_minute'])
   for t in rr:
    assert t['is_bu_minus']==pred[t['source']]['is_bu_minus']==audits[t['source']]['is_bu_minus'];assert t['notional']==audits[t['source']]['admitted_notional']
    assert math.isclose(t['quantity']*t['entry'],t['notional'],abs_tol=1e-10)
    assert t['notional']==(10. if arm=='BU_HALF' and t['is_bu_minus'] else uniform_notional if arm=='UNIFORME' else 20.)
    t.update(arm=arm,path=path,net_realized=t['net'] if t['close_ms'] is not None else None,net_open_mtm=t['net'] if t['close_ms'] is None else None)
   csv_write('runs/'+arm+'_'+path+'.csv',rr);trades.extend(rr)
   for name,a,z in PERIODS:
    cohort=[t for t in rr if a<=t['open_ms']<z];summaries.append({'arm':arm,'path':path,'period':name,'start_BRT':brt(a),'end_exclusive_BRT':brt(z),**metric(cohort),**calendar(r,a,z),'cb_crises_window':sum(x['event']=='CIRCUIT_BREAKER_TRIGGERED' and a<=x['at']<z for x in r['events'])})
   full=calendar(r,START,END);assert math.isclose(full['DD_minute_mtm'],r['stats']['DD_minute_mtm'],abs_tol=1e-8);assert full['positions_max']==r['stats']['max_positions'];assert math.isclose(full['exposure_integrated_usdt_hours'],r['stats']['notional_hours'],abs_tol=1e-7)
   exposure_notes.append({'arm':arm,'path':path,'adapter_legacy_max_committed_usdt':r['stats']['max_committed_usdt'],'correct_max_exposure_usdt':full['exposure_max_usdt'],'legacy_counter_description':'original adapter reports max_positions*20; not actual variable-notional exposure; use delivered economic table'})
   if arm!='BE_OFF_CB':
    bi={t['source']:t for t in base['records']};vi={t['source']:t for t in rr};common=set(bi)&set(vi);ov=set(vi)-set(bi);ob=set(bi)-set(vi);direct=0.;details=[]
    for src in sorted(common):
     t=vi[src];q=bi[src];ratio=t['notional']/q['notional'];expected=q['net']*ratio
     assert t['entry']==q['entry'] and t['close_ms']==q['close_ms'] and t['reason']==q['reason'],(arm,path,src,'exit_non_sizing_difference')
     assert math.isclose(t['net'],expected,abs_tol=1e-8),(arm,path,src,'nonlinear_trade_PnL')
     d=(ratio-1)*q['net'];direct+=d;details.append({'source':src,'entry_BRT':brt(t['open_ms']),'is_bu_minus':t['is_bu_minus'],'base_notional':q['notional'],'variant_notional':t['notional'],'base_net':q['net'],'variant_net':t['net'],'direct_size_delta':d,'reason':t['reason']})
    exclusive_v=[vi[s] for s in sorted(ov)];exclusive_b=[bi[s] for s in sorted(ob)];actual_delta=sum(t['net'] for t in rr)-sum(t['net'] for t in base['records']);residual=sum(t['net'] for t in exclusive_v)-sum(t['net'] for t in exclusive_b)
    assert math.isclose(actual_delta,direct+residual,abs_tol=1e-8)
    decompositions.append({'arm':arm,'path':path,'common_N':len(common),'common_base':metric([bi[s] for s in common]),'common_variant':metric([vi[s] for s in common]),'common_direct_size_delta':direct,'exclusive_base':metric(exclusive_b),'exclusive_variant':metric(exclusive_v),'trajectory_exclusives_residual':residual,'delta_total':actual_delta,'common_details':details,'exclusive_variant_trades':exclusive_v,'exclusive_base_trades':exclusive_b})
    divergences.append(divergence(r,base,arm))
   checks.append({'arm':arm,'path':path,'economic_conservation':True,'capacity':True,'exact_predicate_all_admissions':True,'exact_notional_all_admissions':True,'DD_and_integrated_exposure_match_engine':True,'common_exits_equal_and_PnL_linear':True})
  bu=runs['BU_HALF'];red=[t for t in bu['records'] if t['reduced_bu_minus']];bi={t['source']:t for t in base['records']};paired=[t for t in red if t['source'] in bi];unpaired=[t for t in red if t['source'] not in bi]
  reduced.append({'path':path,**metric(red),'common_with_BE_N':len(paired),'common_base':metric([bi[t['source']] for t in paired]),'common_BU':metric(paired),'direct_common_size_delta':sum(t['net']-bi[t['source']]['net'] for t in paired),'exclusive_BU_reduced':metric(unpaired),'trades':red})
  for control in ['BE_OFF_CB','MACD_BU_MINUS','UNIFORME']:
   for period,a,z in PERIODS:
    vv=[t for t in bu['records'] if a<=t['open_ms']<z];bb=[t for t in runs[control]['records'] if a<=t['open_ms']<z];comparisons.append({'variant_arm':'BU_HALF','control_arm':control,'path':path,'period':period,**diff(vv,bb),'delta_DD':calendar(bu,a,z)['DD_minute_mtm']-calendar(runs[control],a,z)['DD_minute_mtm'],'delta_exposure_mean':calendar(bu,a,z)['exposure_mean_usdt']-calendar(runs[control],a,z)['exposure_mean_usdt']})
   kind='NET_TOTAL' if control=='UNIFORME' else 'EFFICIENCY';robustness.append({'control':control,'path':path,**robust(bu['records'],runs[control]['records'],kind),'bootstrap72h':bootstrap(bu['records'],runs[control]['records'])})
  # Same BE-defined intervals across all four policies within each path.
  hs=sorted([t for t in base['records'] if t['reason']=='HARD_STOP'],key=lambda t:t['close_ms']);groups=[]
  for t in hs:
   if not groups or t['close_ms']-groups[-1][-1]['close_ms']>6*3600000:groups.append([])
   groups[-1].append(t)
  assert sum(map(len,groups))==len(hs)
  btrace={x[0]:x for x in base['cb_trace']};badm={x['source']:x for x in base['admissions']}
  for i,group in enumerate(groups):
   a=min(t['open_ms'] for t in group)//M*M;z=group[-1]['close_ms']//M*M+M
   block={'path':path,'block':i+1,'type':'ISOLADO_1' if len(group)==1 else 'MULTIPLO','base_defined_HS_N':len(group),'start_ms':a,'end_exclusive_ms':z,'start_BRT':brt(a),'end_exclusive_BRT':brt(z),'defining_sources':[t['source'] for t in group],'arms':{}}
   for arm,r in runs.items():
    cohort=[t for t in r['records'] if a<=t['open_ms']<z];closed=[t for t in r['records'] if t['close_ms'] is not None and a<=t['close_ms']<z]
    cb=r['cb_trace'][max(0,(a-START)//M):min(len(r['cb_trace']),(z-START)//M)];ad=[x for x in r['admissions'] if a<=x['at']<z]
    block['arms'][arm]={'HS_by_exit':sum(t['reason']=='HARD_STOP' for t in closed),**metric(cohort),**calendar(r,a,z),'BU_admitted':sum(t['is_bu_minus'] for t in cohort),'BU_reduced':sum(t['reduced_bu_minus'] for t in cohort),'CB_pause_difference_minutes_vs_BE':sum(x[6]!=btrace[x[0]][6] for x in cb),'CB_condition_difference_minutes_vs_BE':sum(x[5]!=btrace[x[0]][5] for x in cb),'admission_differences_vs_BE':sum(x['accepted']!=badm[x['source']]['accepted'] for x in ad)}
   blocks.append(block)
  print('ANALYZED_PATH',path,flush=True)
 # Complementary non-overlapping unions allow additive concentration accounting.
 unions=[]
 for path in PATHS:
  windows=sorted((b['start_ms'],b['end_exclusive_ms']) for b in blocks if b['path']==path);merged=[]
  for a,z in windows:
   if merged and a<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],z)
   else:merged.append([a,z])
  episode_rows=[];counts=Counter();nets=defaultdict(float)
  for i,(a,z) in enumerate(merged):
   out={'episode':i+1,'start_BRT':brt(a),'end_exclusive_BRT':brt(z),'start_ms':a,'end_exclusive_ms':z,'arms':{}}
   for arm,r in allruns[path].items():
    ts=[t for t in r['records'] if a<=t['open_ms']<z];out['arms'][arm]=metric(ts);counts[arm]+=len(ts);nets[arm]+=sum(t['net'] for t in ts)
   out['delta_BU_vs_BE']=out['arms']['BU_HALF']['net_total']-out['arms']['BE_OFF_CB']['net_total'];out['delta_BU_vs_UNIFORME']=out['arms']['BU_HALF']['net_total']-out['arms']['UNIFORME']['net_total'];episode_rows.append(out)
  outside={arm:{'N':len(r['records'])-counts[arm],'net':sum(t['net'] for t in r['records'])-nets[arm]} for arm,r in allruns[path].items()}
  unions.append({'path':path,'method':'merge overlapping BE HS windows; each entry assigned at most once; descriptive only','episodes':episode_rows,'outside_unions':outside,'best3_BU_vs_BE':[x['episode'] for x in sorted(episode_rows,key=lambda x:-x['delta_BU_vs_BE']) if x['delta_BU_vs_BE']>0][:3],'best3_BU_vs_UNIFORME':[x['episode'] for x in sorted(episode_rows,key=lambda x:-x['delta_BU_vs_UNIFORME']) if x['delta_BU_vs_UNIFORME']>0][:3],'sample_independence_claimed':False})
 dump('CONCENTRACAO_UNIOES.json',unions)
 # Primary strict gates: full, both halves, then same tests after each top3 set.
 gates={}
 for path in PATHS:
  for control,metric_key,threshold in [('BE_OFF_CB','delta_net_por_100',.025),('UNIFORME','delta_net_total',0.)]:
   for period,a,z in PERIODS:
    c=next(x for x in comparisons if x['path']==path and x['control_arm']==control and x['period']==period);value=c[metric_key]
    gates[path+'/'+control+'/'+period]=value is not None and (value>=threshold-1e-10 if control=='BE_OFF_CB' else value>1e-10)
   rob=next(x for x in robustness if x['path']==path and x['control']==control)
   for t in rob['trimmed_periods']:
    value=t[metric_key];gates[path+'/'+control+'/SEM_TOP3/'+t['period']]=value is not None and (value>=threshold-1e-10 if control=='BE_OFF_CB' else value>1e-10)
 classification={'classification':'CANDIDATO' if all(gates.values()) else 'INCONCLUSIVA','gates':gates,'threshold_net_por_100':.025,'all_primary_conditions_apply_both_paths_and_halves':True,'trimmed_half_gates_predeclared_conservative':True,'IC_veto':False,'scientific_approval':False,'independent_validation':False}
 calibration=read('UNIFORME_CALIBRACAO.json');funding=[]
 for path in PATHS:
  v=allruns[path]['BU_HALF']['records'];u=allruns[path]['UNIFORME']['records'];bv=allruns[path]['BE_OFF_CB']['records'];nv=sum(t['notional'] for t in v);nu=sum(t['notional'] for t in u);expected=calibration['notional_uniforme']*len(bv)
  funding.append({'path':path,'uniform_notional_fixed':calibration['notional_uniforme'],'N_BE':len(bv),'N_BU':len(v),'N_UNIFORME':len(u),'notional_BU':nv,'notional_UNIFORME':nu,'calibration_target_for_BE_trade_count':expected,'actual_UNIFORME_minus_target':nu-expected,'actual_UNIFORME_minus_BU':nu-nv,'BU_exposure':calendar(allruns[path]['BU_HALF'],START,END),'UNIFORME_exposure':calendar(allruns[path]['UNIFORME'],START,END),'uniform_calibrated_in_sample':True})
 for name,data in [('RESUMO',summaries),('COMPARACOES',comparisons),('ROBUSTEZ',robustness),('PAREADOS_EXCLUSIVOS',decompositions),('BU_REDUZIDOS',reduced),('DIVERGENCIAS',divergences),('BLOCOS_HS',blocks),('CLASSIFICACAO',classification),('NOTIONAL_VS_EXPOSICAO',funding),('CONTADOR_LEGADO_EXPOSICAO',exposure_notes)]:dump(name+'.json',data)
 csv_write('TRADES_TODOS.csv',trades);csv_write('TABELA_ECONOMICA.csv',summaries);csv_write('COMPARACOES.csv',comparisons)
 blockrows=[{k:v for k,v in b.items() if k!='arms'}|{'arm':arm,**row} for b in blocks for arm,row in b['arms'].items()];csv_write('BLOCOS_HS.csv',blockrows)
 dump('VERIFICACAO_ECONOMICA.json',{'passed':True,'checks':checks,'runs':8,'summary_rows':len(summaries),'blocks':len(blocks),'midpoint_ms':CUT,'midpoint_BRT':brt(CUT),'no_regime_cell_classification':True,'no_silent_raw_run_rewrite':True})
 report(summaries,comparisons,robustness,decompositions,reduced,divergences,blocks,classification,funding)

def reading(summaries,comparisons,robustness,decompositions,classification,funding):
 def comp(control,path):return next(x for x in comparisons if x['control_arm']==control and x['path']==path and x['period']=='TOTAL')
 lines=['# BU_HALF — leitura científica','Status PROPOSTO_PARA_REVISAO; revisão não independente; sem homologação ou alteração operacional.',f'Janela até {brt(END-1)},999 BRT; ponto médio {brt(CUT)} BRT. Paridade desligada e predicado exato passaram. UNIFORME fixo em {read("UNIFORME_CALIBRACAO.json")["notional_uniforme"]:.12f}USDT, calibrado no próprio histórico.',
 '**Classificação exploratória: '+classification['classification']+'.** Critérios: '+str(sum(classification['gates'].values()))+'/'+str(len(classification['gates']))+' atendidos. As duas condições são exigidas na janela completa, nas duas metades, em ambos os paths e após retirar os três dias mais contributivos.',
 table(['Path','Δnet/100 vsBE','Δnet vsBE','Δnet/100 vsMACD','Δnet vsMACD','Δnet vsUNIFORME'],[[p,comp('BE_OFF_CB',p)['delta_net_por_100'],comp('BE_OFF_CB',p)['delta_net_total'],comp('MACD_BU_MINUS',p)['delta_net_por_100'],comp('MACD_BU_MINUS',p)['delta_net_total'],comp('UNIFORME',p)['delta_net_total']] for p in PATHS]),
 '## Respostas às seis perguntas',
 '1. **BU_HALF supera o bloqueio integral?** As colunas contra MACD acima distinguem net total e eficiência. '+('Sim, na janela completa dos dois paths e nas duas métricas; conferir estabilidade nas metades e na robustez antes de generalizar.' if all(comp('MACD_BU_MINUS',p)['delta_net_por_100']>0 and comp('MACD_BU_MINUS',p)['delta_net_total']>0 for p in PATHS) else 'Não há superioridade simultânea de net total e eficiência nos dois paths na janela completa. O relatório apresenta os recortes e suas diferenças.'),
 '2. **Melhora a eficiência versus BE_OFF_CB?** '+('Atinge o limiar de +0,025 na janela completa dos dois paths.' if all(comp('BE_OFF_CB',p)['delta_net_por_100']>=.025-1e-10 for p in PATHS) else 'Não atinge o limiar de +0,025 USDT por 100 USDT na janela completa em ambos os paths. Ganho menor não equivale ao cumprimento do critério.'),
 '3. **Supera UNIFORME?** '+('O net do BU_HALF é superior nos dois paths completos.' if all(comp('UNIFORME',p)['delta_net_total']>1e-10 for p in PATHS) else 'O net do BU_HALF não é superior ao UNIFORME nos dois paths completos.')+' A comparação mede carteiras com CB próprio e uma referência calibrada no histórico; não isola automaticamente o efeito da escolha das posições BU−.',
 '4. **Consistência?** Gates não atendidos: '+', '.join(k for k,v in classification['gates'].items() if not v)+'. Top3 e IC são exibidos no relatório, sem IC como veto. BlocosHS e rankings podem sobrepor; a análiseaditiva usauniões quando apresentada.',
 '5. **Tamanho direto ou trajetória?** Decomposição por path do BU_HALF contra BE_OFF_CB: '+str([{'path':x['path'],'direct_common_size_delta':x['common_direct_size_delta'],'exclusive_trajectory_residual':x['trajectory_exclusives_residual'],'delta_total':x['delta_total']} for x in decompositions if x['arm']=='BU_HALF'])+'. Os trades comuns mantiveram saídas iguais e PnL proporcional ao tamanho. As primeiras mudanças financeiras, de CB e de admissão estão em DIVERGENCIAS.json. Os exclusivos medem mudanças de trajetória; não são um benefício independente a somar novamente.',
 '6. **Justifica validação independente?** '+('Passa a triagem pré-declarada e pode ser submetido à revisão para eventual validação independente, que ainda precisa de decisão humana e desenho próprio.' if classification['classification']=='CANDIDATO' else 'A versão geral não passa a triagem pré-declarada. Este estudo não sustenta sua promoção a candidata. Resultados parciais e contextuais permanecem inconclusivos para revisão, sem criar uma política por regime.'),
 '## Capital operado e risco',table(['Arm','Path','N','Notional','Net','Net/100','DD','Exp média','Exp integradaUSDT*h'],[[x['arm'],x['path'],x['N'],x['notional_total'],x['net_total'],x['net_por_100'],x['DD_minute_mtm'],x['exposure_mean_usdt'],x['exposure_integrated_usdt_hours']] for x in summaries if x['period']=='TOTAL']),
 'Net negativo em '+str(sum(x['net_total']<0 for x in summaries if x['period']=='TOTAL'))+'/8 runs. Melhorar relativamente não significa lucro absoluto. Quantidades econômicas contínuas mantidas; filtros/rounding verificados separadamente. Capital100USDT nominal, sem solvência estrita. UNIFORME não iguala exposição temporal nem necessariamente volume efetivo apósCB; diferenças estão emNOTIONAL_VS_EXPOSICAO.json.',
 'Histórico amplamentereutilizado, escolhasanterioresconhecidas e referênciaUNIFORME calibradain-sample. Nenhuma validação independente ou equivalência aoruntime demonstrada. Classificação não homologa hipótese nem autoriza mudançaoperacional. Sem classificação por célulaMARÉ×ONDA.'
 ]
 (S/'LEITURA_CIENTIFICA.md').write_text('\n\n'.join(lines)+'\n',encoding='utf-8')

def report(summaries,comparisons,robustness,decompositions,reduced,divergences,blocks,classification,funding):
 negative=sum(x['net_total']<0 for x in summaries if x['period']=='TOTAL')
 lines=['# BU_HALF — relatório científico','Status PROPOSTO_PARA_REVISAO. REGRA_IDEALIZADA / REPLAY_SISTEMICO; revisão não independente; sem homologação ou autorização operacional.',f'Janela {brt(START)} até {brt(END-1)},999 BRT. Corteexclusivo {brt(END)}. Metades em {brt(CUT)} BRT. LOW_FIRST principal, HIGH_FIRST sensibilidade. Net absoluto negativo em {negative}/8 runs.',
 'Custos BASE congelados:fricção3,5bps/lado+taxa0,10%/lado. CB e saídas existentes preservados. Capital100 nominal sem cashgate; não conta autofinanciada100. Quantidades contínuas do motor preservadas; viabilidade com arredondamento é verificação separada de filtros atuais da SpotTestnet, não execução histórica real. Nenhuma ordem enviada. Dados reais públicos; contextoMACD congelado; pendência t−1/t−3 não reinterpretada.',
 '## Pré-validações e calibração',
 'Predicado exato nas6.903 oportunidades, código PolicyShadow._entry_policy reutilizado. Paridade sizingOFF versusBE exata nosdoispaths, incluindo admissões/saídas/eventos/CB/equity/stats. Prefixos BE eMACD reproduzemSTACK. Viabilidade10 antesBUativo; UNIFORME calibrado umaúnicavez apósBU_LOW_FIRST e validado antesde rodar. Evidências:PREDICADO_BU_MINUS,PARIDADE,VIABILIDADE_10_USDT,VIABILIDADE_UNIFORME eTESTNET_*JSON.',
 'Filtros e interpretação oficial: [Binance Spot filters](https://developers.binance.com/en/docs/products/spot/filters). Captura Testnet registrou NOTIONAL mínimo5USDT aplicadoMARKET, LOT_SIZE passo/minqty0,001SOL, MARKET_LOT_SIZE passo0/desativado e teto próprio. Exemplos atuais e limites conservadores históricos, arredondamentos e venda após taxaemSOL estão nos gates. Não certifica filtros Testnet históricos nem fills futuros.',
 f'**UNIFORME fixo: {read("UNIFORME_CALIBRACAO.json")["notional_uniforme"]:.12f} USDT por entrada**, ambospaths, sem recalibração. Denominador notional negociado = soma do notional de ENTRADA incluindo abertos; não compra+venda duasvezes. Calibração in-sample não é validação independente.',
 '## Classificação exploratória',json.dumps(classification,ensure_ascii=False,indent=2),
 'O limiar de +0,025 USDT por 100 USDT contra BE_OFF_CB e net BU_HALF > UNIFORME são exigidos na janela completa, nas duas metades, nos dois paths e após retirar os três dias de cada comparação. A exigência conservadora nas metades após a retirada também foi pré-declarada. Nenhuma condição foi flexibilizada. Os casos que não passam ficam INCONCLUSIVOS, inclusive os favoráveis em apenas uma metade. IC é diagnóstico.',
 '## Tabela econômica completa',
 'N, notional, net e duração das metades são coortes por ENTRADA, com resultado até END. DD e exposição seguem a carteira na janela cronológica, incluindo posições herdadas. DD é amostrado a cada minuto; no ponto médio de 30 segundos, acrescenta-se o MTM do path naquele instante, sem mudar trades ou critérios. A exposição é ponderada pelo tempo exato. A duração dos abertos é observada até END. Todos os deltas estão em COMPARACOES.csv.',
 table(['Arm','Path','Período','Fech/ab','Notional','Net','Realizado','MTM','Net/100','Net/trade','HS/%HS','PL','TRAIL','DD','Exp média','Exp USDT*h','Duração média min'],[[x['arm'],x['path'],x['period'],str(x['closed'])+'/'+str(x['open']),x['notional_total'],x['net_total'],x['net_realized'],x['net_open_mtm'],x['net_por_100'],x['net_per_trade'],str(x['HS'])+'/'+str(round(x['HS_pct'],2)),x['PL'],x['TRAIL'],x['DD_minute_mtm'],x['exposure_mean_usdt'],x['exposure_integrated_usdt_hours'],x['mean_duration_minutes']] for x in summaries]),
 'Aviso de compatibilidade: rawJSON runs mantêm o campo legado stats.max_committed_usdt=max_positions*20 do adaptador original. Ele não mede exposição máxima real quando tamanho varia. As tabelas entregues calculam corretamente notional real ponderado e máximo; CONTADOR_LEGADO_EXPOSICAO.json explicita cada diferença. RawJSON não foi silenciosamente reescrito, preservando hashes da calibração.',
 '## BU_HALF versus cada comparador',table(['Controle','Path','Período','Δnet/100','Δnet','Δnet/trade','ΔDD','Δexp média'],[[x['control_arm'],x['path'],x['period'],x['delta_net_por_100'],x['delta_net_total'],x['delta_net_per_trade'],x['delta_DD'],x['delta_exposure_mean']] for x in comparisons]),
 '## Entradas BU− efetivamente reduzidas',table(['Path','N','Notional','HS','PL','TRAIL','Abertos','Net','Net/100','ComunsBE','Δdireto comuns','ExclusivasBU N/net'],[[x['path'],x['N'],x['notional_total'],x['HS'],x['PL'],x['TRAIL'],x['open'],x['net_total'],x['net_por_100'],x['common_with_BE_N'],x['direct_common_size_delta'],str(x['exclusive_BU_reduced']['N'])+'/'+str(x['exclusive_BU_reduced']['net_total'])] for x in reduced]),
 'Pareamento só quandoBE admitiu mesma source. Sem inventar admissãoBE emsourcesexclusivas. Trades comuns tiveram entry/saída/motivo iguais e net escalou linearmente comnotional. Efeito direto dos comuns=(ratio_notional−1)*netBE. Mudança de trajetórias está separada nos exclusivos; não extrapolar efeito direto para fontes ausentes.',
 '## Comuns, exclusivos e primeira divergência',table(['Arm','Path','Comuns','NetcomumBE','Netcomumvar','Δdireto','SóBE N/net','Sóvar N/net','Residualtrajetória','Δtotal'],[[x['arm'],x['path'],x['common_N'],x['common_base']['net_total'],x['common_variant']['net_total'],x['common_direct_size_delta'],str(x['exclusive_base']['N'])+'/'+str(x['exclusive_base']['net_total']),str(x['exclusive_variant']['N'])+'/'+str(x['exclusive_variant']['net_total']),x['trajectory_exclusives_residual'],x['delta_total']] for x in decompositions]),
 'Delta total=efeito direto dos comuns+netexclusivosvar−netexclusivosBE. Não é atribuição causal independente de cada mecanismo. Detalhes por source em PAREADOS_EXCLUSIVOS.json.',
 *[f"### {x['arm']} / {x['path']}\n\n"+json.dumps(x,ensure_ascii=False,indent=2) for x in divergences],
 '## Robustez, top3 e IC',
 'Dias BRT de entrada, mesmaatribuição nos doisbraços. BUvsBE ordena contribuição à diferença de net/100 usando denominadoresglobais, que soma exatamente ao deltaeficiência. BUvsUNIFORME ordena contribuição ao deltaNET. Remove mesmosdias dos dois, recalcula razões, semrerodar ourecalibrar. Cada comparação tem suasdatas, não escolherdias apósresultados. IC bootstrap blocos72h de diasentrada,5000seed20261027, trajetóriasobservadas; sem veto e sem independente. Histórico amplamentereutilizado, mais25configuraçõesanteriores, STACK e novaBU/UNIFORME; inventário incompleto.',
 table(['Controle','Path','Top3','IC95Δnet100','IC95Δnet'],[[x['control'],x['path'],str(x['top3']),str(x['bootstrap72h']['delta_efficiency_CI95']),str(x['bootstrap72h']['delta_net_total_CI95'])] for x in robustness]),
 table(['Controle','Path','Sem top3 período','Δnet100','Δnet','NotionalBU/ctrl'],[[x['control'],x['path'],t['period'],t['delta_net_por_100'],t['delta_net_total'],str(t['variant']['notional_total'])+'/'+str(t['control']['notional_total'])] for x in robustness for t in x['trimmed_periods']]),
 '## Notional negociado e exposição temporal',json.dumps(funding,ensure_ascii=False,indent=2),
 'UNIFORME iguala um alvo de turnover baseado no N da BE_LOW_FIRST, não garante número de trades nem exposição integrada iguais. CB próprio reage ao PnL emUSDT e pode alterar ambos. Não apresentar referência calibrada in-sample comoOOS.',
 '## Blocos HARD_STOP definidos pela BE',
 'Gapentre saídasHS<=6h, unitáriosincluídos. Começo=entrada maisantiga dentre osHSdogrupo; fim=apósminutoúltimoHS. Mesmosintervalos4braços porpath. HSporSAÍDA, netporENTRADAna janela comresultadoatéEND/MTM. Exposição cronológica; BU/reduzidosporENTRADA, divergênciaCBporboundary e admissão porsinal. Janelaspodem sobrepor, incluindoHSdeoutrogrupo; não somar como episódiosindependentes. Concentração: todasjanelas abaixo, ranking descritivo porΔnetcoorte noarquivoCONCENTRACAO_EPISODIOS.json, união deintervalossobrepostos para contabilizaçãoúnica emCONCENTRACAO_UNIOES.json e diasúnicos emROBUSTEZ. Asuniões são complemento, não redefinição dosblocos; ocomplementoforadaunião é mostrado. Rankear não classifica regime nem altera seleção.',
 table(['Path','Bloco/tipo','Início','Fim exclusivo','Arm','HSsaída','Nentrada','Netentradas','Exp média','BU','Reduzidas','CBpausa diferente min','Admissõesdif'],[[b['path'],str(b['block'])+'/'+b['type'],b['start_BRT'],b['end_exclusive_BRT'],arm,x['HS_by_exit'],x['N'],x['net_total'],x['exposure_mean_usdt'],x['BU_admitted'],x['BU_reduced'],x['CB_pause_difference_minutes_vs_BE'],x['admission_differences_vs_BE']] for b in blocks for arm,x in b['arms'].items()]),
 '## Artefatos',
 'TRADES_TODOS.csv e runs/*.csv incluemnotional,MACDcausal,is_bu_minus,reduced_bu_minus,trend_open,wave_open,trend_close,wave_close,arm,path,source,timestamps,preços,quantity,ATR,motivo,status,netrealizado/MTM. Não classificar por célulaMARÉ×ONDA. runs/*.json são saídasoriginais imutáveis com eventoseCBtrace. Métricas/pareados/robustez/CB/blocos, filtrosatuais, fontesMC, código e hashes preservados. Sem alteraçõesoperacionais ou homologação.'
 ]
 (S/'RELATORIO_BU_HALF.md').write_text('\n\n'.join(lines)+'\n',encoding='utf-8')
 concentration=[]
 for path in PATHS:
  rows=[b for b in blocks if b['path']==path];values=[{'path':path,'block':b['block'],'start_BRT':b['start_BRT'],'end_exclusive_BRT':b['end_exclusive_BRT'],'delta_net_BU_vs_BE':b['arms']['BU_HALF']['net_total']-b['arms']['BE_OFF_CB']['net_total'],'delta_net_BU_vs_UNIFORME':b['arms']['BU_HALF']['net_total']-b['arms']['UNIFORME']['net_total']} for b in rows]
  concentration.append({'path':path,'windows_overlap':True,'not_additive_or_independent':True,'best_BU_vs_BE':sorted(values,key=lambda x:-x['delta_net_BU_vs_BE'])[:10],'worst_BU_vs_BE':sorted(values,key=lambda x:x['delta_net_BU_vs_BE'])[:10],'best_BU_vs_UNIFORME':sorted(values,key=lambda x:-x['delta_net_BU_vs_UNIFORME'])[:10]})
 dump('CONCENTRACAO_EPISODIOS.json',concentration)
 reading(summaries,comparisons,robustness,decompositions,classification,funding)
 print('ANALYSIS_COMPLETE',classification['classification'],flush=True)

if __name__=='__main__':main()
