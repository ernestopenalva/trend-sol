"""Coverage and economic analysis of preregistered replay artifacts; no new trading simulation."""
import sys,json,csv,collections,bisect,math,statistics,random,argparse,hashlib
from pathlib import Path
from datetime import datetime,timezone,timedelta
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_12bracos_20261021';sys.path.insert(0,str(ROOT/'tools'));from replay12_engine import ARMS,COSTS
TZ=timezone(timedelta(hours=-3));STATES=['BULL','BEAR','EXPA','COMP','UNDEFINED','UNAVAILABLE'];MAN=json.loads((S/'market_manifest.json').read_bytes());START,END=MAN['start_ms'],MAN['end_ms'];TL=json.loads((S/'regime_timeline.json').read_bytes());TIMES={k:[r['at'] for r in rr] for k,rr in TL.items()};SIGNALS=json.loads((S/'signals.json').read_bytes());REG=json.loads((S/'regimes.json').read_bytes())
def read(p):return json.loads(p.read_bytes())
def dump(name,x):(S/name).write_text(json.dumps(x,ensure_ascii=False,separators=(',',':')),encoding='utf-8')
def date(t):return datetime.fromtimestamp(t/1000,TZ).date().isoformat()
def iso(t):return datetime.fromtimestamp(t/1000,TZ).isoformat()
def run(arm,scenario,path):return read(S/'runs'/(arm+'_'+scenario+'_'+path+'.json'))
def episode(pref,t):return TL[pref][bisect.bisect_right(TIMES[pref],t)-1]['run']
def block(t):return int((t-START)//(72*3600000))
def cells():return [(t,w) for t in STATES for w in STATES]
def csvout(name,rows):
 with (S/name).open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def coverage():
 out=[];summary=[]
 for path in ['HIGH_FIRST','LOW_FIRST']:
  for arm in ARMS:
   r=run(arm,'BASE',path);rr=r['records'];ops=r['admissions'];summary.append({'arm':arm,'path':path,'opportunities_own':len(ops),'upstream_potential_REAL':len(SIGNALS['REAL_A']),'admitted':len(rr),'closed':sum(x['close_ms'] is not None for x in rr),'open':sum(x['close_ms'] is None for x in rr),'days':len({date(x['open_ms']) for x in rr}),'runs_mare':len({episode('mare',x['open_ms']) for x in rr}),'runs_onda':len({episode('onda',x['open_ms']) for x in rr}),'blocks72h':len({block(x['open_ms']) for x in rr})})
   for t,w in cells():
    subset=[x for x in rr if x['mare']==t and x['onda']==w];op=[x for x in ops if x['mare']==t and x['onda']==w];mr={episode('mare',x['open_ms']) for x in subset};wr={episode('onda',x['open_ms']) for x in subset};bb={block(x['open_ms']) for x in subset};out.append({'arm':arm,'path':path,'mare':t,'onda':w,'opportunities_own':len(op),'admitted':len(subset),'closed':sum(x['close_ms'] is not None for x in subset),'open':sum(x['close_ms'] is None for x in subset),'days':len({date(x['open_ms']) for x in subset}),'mare_runs_occupied':len(mr),'onda_runs_occupied':len(wr),'blocks72h_occupied':len(bb),'ten_runs_both':len(mr)>=10 and len(wr)>=10,'first_entry_brt':iso(min((x['open_ms'] for x in subset),default=START)) if subset else None,'last_entry_brt':iso(max((x['open_ms'] for x in subset),default=END)) if subset else None,'context_complete_N':sum(x['mare'] in STATES[:4] and x['onda'] in STATES[:4] for x in subset)})
 csvout('NIVEL_0_MATRIZ.csv',out);dump('NIVEL_0.json',{'summary':summary,'cells':out,'warning':'Runs structural, not independent. 72h blocks reduce but do not prove independence. Counts require internally evolving exits/CB; no economic outcome selected or reported here.'});lines=['# Nível 0 — replay histórico, sem resultados econômicos','',f'Universo comum: {iso(START)} → {iso(END)} (corte exclusivo, último minuto completo). Sem gaps de1m; warm-up anterior. As regras congeladas foram aplicadas inclusive antes da criação dos shadows.','', '| Braço | Caminho | Oportunidades próprias | Admitidas | Fech./ab. | Dias | Runs MARÉ/ONDA | Blocos72h |','| --- | --- | --- | --- | --- | --- | --- | --- |']
 for x in summary:lines.append('| '+x['arm']+' | '+x['path']+' | '+str(x['opportunities_own'])+' | '+str(x['admitted'])+' | '+str(x['closed'])+'/'+str(x['open'])+' | '+str(x['days'])+' | '+str(x['runs_mare'])+'/'+str(x['runs_onda'])+' | '+str(x['blocks72h'])+' |')
 eligible=sum(x['ten_runs_both'] for x in out);lines+=['',f'{eligible} células/caminhos atingem referência mínima de dez runs ocupados de ambos, dentre864 células incluindo estados especiais. Isto não prova poder nem independência. Contagens e cobertura completas por célula em NIVEL_0_MATRIZ.csv; não transformar192cruzamentos em192 hipóteses independentes.','', 'Oportunidades próprias DMI são emitidas por sua engine independente (773); todos os seus sinais pertencem ao universo potencial REAL_A (6597) verificado. Denominadores próprios e upstream separados. Nos dez braços CB, os candidatos são os mesmos do REAL_A antes de suas admissões; sua seleção/capacidade/CB gera trajetórias próprias.','', 'Nível0 contém somente contagens. O estado interno precisa calcular fechamentos e CB para determinar quantos trades podem ser admitidos; nenhuma regra/custo foi escolhida por resultados. Auditar numeração não exige supor ausência de aritmética interna do detectorCB.','', 'Sinais/saídas e limitações em AUDITORIA_FIDELIDADE.md. Todos continuam idealizadosOHLC; nenhuma equivalência runtime ou resultado forward homologado.']
 (S/'NIVEL_0.md').write_text('\n'.join(lines)+'\n',encoding='utf-8');print('NIVEL_0_READY',len(summary),'arm/path entries',eligible,'cells/path with ten runs both')

def bootstrap(v):
 if len(v)<10:return {'n':len(v),'CI95':None,'reason':'fewer than 10 blocks'}
 rng=random.Random(20261021);n=len(v);s=sorted(sum(rng.choices(v,k=n)) for _ in range(5000));return {'n':n,'CI95':[s[124],s[4874]],'replicates':5000,'conditional_fixed_trajectories':True}
def top(d):
 pos=sorted([(str(k),v) for k,v in d.items() if v>0],key=lambda x:x[1],reverse=True);neg=sorted([(str(k),v) for k,v in d.items() if v<0],key=lambda x:x[1]);return {'positive3':pos[:3],'negative3':neg[:3],'sum_positive':sum(v for k,v in pos),'positive_top3_fraction':sum(v for k,v in pos[:3])/sum(v for k,v in pos) if pos else None,'sum_all':sum(d.values()),'excluding_positive3':sum(d.values())-sum(v for k,v in pos[:3])}
def dd_closes(rr):
 c=peak=dd=0
 for x in sorted([r for r in rr if r['close_ms'] is not None],key=lambda x:(x['close_ms'],x['source'])):c+=x['net'];peak=max(peak,c);dd=max(dd,peak-c)
 return dd

def attributed_minute_dd(r,prices):
 # Event sweep reproduces minute-close MTM from positions, without rerunning admissions.
 events=[];groups={};bps=COSTS[r['scenario']];fees=.002
 for x in r['records']:
  keys=[('TOTAL',None,None),('MARE',x['mare'],None),('ONDA',None,x['onda']),('MARE_ONDA',x['mare'],x['onda'])]
  for k in keys:groups.setdefault(k,{'realized':0.,'qty':0.,'basis':0.,'peak':0.,'dd':0.})
  events.append((x['open_ms'],1,x,keys))
  if x['close_ms'] is not None:events.append((x['close_ms'],0,x,keys))
 events.sort(key=lambda e:(e[0],e[1],e[2]['source']));i=0;active=set();changed=set()
 for close,price in prices:
  changed.clear()
  while i<len(events) and events[i][0]<=close:
   ts,kind,x,keys=events[i];i+=1;sign=1 if kind==1 else -1
   for k in keys:
    g=groups[k];g['qty']+=sign*x['quantity'];g['basis']+=sign*x['notional']*(1+fees)
    if kind==0:g['realized']+=x['net']
    changed.add(k)
    if abs(g['qty'])>1e-10:active.add(k)
    else:active.discard(k)
  for k in active|changed:
   g=groups[k];eq=g['realized']+g['qty']*price*(1-bps/10000)-g['basis'];g['peak']=max(g['peak'],eq);g['dd']=max(g['dd'],g['peak']-eq)
 return {k:g['dd'] for k,g in groups.items()}

def summarize(rr,ops,dd,scenario,path,primary=True):
 closed=[x for x in rr if x['close_ms'] is not None];op=[x for x in rr if x['close_ms'] is None];vals=[x['net'] for x in closed];pos=sum(x for x in vals if x>0);neg=-sum(x for x in vals if x<0);n=len(closed);days=collections.defaultdict(float);months=collections.defaultdict(float);blocks=collections.defaultdict(float);eps={p:collections.defaultdict(float) for p in ['mare','onda']};mid=START+(END-START)//2
 for x in rr:
  days[date(x['open_ms'])]+=x['net'];months[date(x['open_ms'])[:7]]+=x['net'];blocks[block(x['open_ms'])]+=x['net']
  for p in eps:eps[p][episode(p,x['open_ms'])]+=x['net']
 reasons=collections.Counter(x['reason'] for x in closed);total=sum(x['net'] for x in rr);dur=[x['duration_minutes'] for x in rr]
 return {'closed':n,'open':len(op),'net_realized':sum(vals),'net_open_mtm':sum(x['net'] for x in op),'net_total':total,'gross_after_friction_total':sum(x['gross'] for x in rr),'estimated_fees_total':sum(x['fee_est'] for x in rr),'net_per_closed':sum(vals)/n if n else None,'net_per_admission_total':total/len(rr) if rr else None,'PF_closed':pos/neg if neg else None,'PF_no_losses':bool(pos and not neg),'DD_minute_mtm_attributed':dd,'DD_realized':dd_closes(rr),'opportunities_own':len(ops),'admission_rate_own':len(rr)/len(ops) if ops else None,'PnL_per_own_opportunity':total/len(ops) if ops else None,'notional_admitted':sum(x['notional'] for x in rr),'notional_hours':sum(x['notional']*x['duration_minutes']/60 for x in rr),'mean_duration_min':statistics.mean(dur) if dur else None,'median_duration_min':statistics.median(dur) if dur else None,'exit_reasons':{k:{'N':reasons[k],'pct_closed':reasons[k]/n*100 if n else None} for k in ['HARD_STOP','PROFIT_LOCK','TRAILING']+sorted(set(reasons)-{'HARD_STOP','PROFIT_LOCK','TRAILING'})},'days':len(days),'runs_mare':len(eps['mare']),'runs_onda':len(eps['onda']),'blocks72h':len(blocks),'halves_entry_cohort_net':[sum(x['net'] for x in rr if x['open_ms']<mid),sum(x['net'] for x in rr if x['open_ms']>=mid)],'monthly_entry_cohort_net':dict(months),'day_concentration':top(days),'episode_concentration':{p:top(v) for p,v in eps.items()},'lodo_entry_cohort_range':[min((total-v for v in days.values()),default=None),max((total-v for v in days.values()),default=None)],'CI_day':bootstrap(list(days.values())) if primary else None,'CI_72h':bootstrap(list(blocks.values())) if primary else None,'extremes_positive':sorted([{'source':x['source'],'net':x['net']} for x in rr],key=lambda x:x['net'],reverse=True)[:5],'extremes_negative':sorted([{'source':x['source'],'net':x['net']} for x in rr],key=lambda x:x['net'])[:5]}
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['coverage']);args=parser.parse_args();coverage()
