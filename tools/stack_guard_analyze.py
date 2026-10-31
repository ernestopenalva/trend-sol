"""Descriptive comparison and reproducible delivery for STACK_GUARD_2NEG."""
from pathlib import Path
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
import sys,json,csv,math,bisect,random,statistics,hashlib,shutil,zipfile
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
import stack_guard_engine as g
S=g.S;M=60000;BRT=timezone(timedelta(hours=-3));MAN=json.loads((S/'market_manifest.json').read_bytes());START=MAN['start_ms'];END=MAN['end_ms'];CUT=START+(END-START)//2

def read(p):return json.loads((S/p).read_bytes())
def dump(p,x):g.e.dump(S/p,x)
def brt(t):return datetime.fromtimestamp(t/1000,BRT).strftime('%d/%m/%Y %H:%M:%S') if t is not None else 'OPEN'
def day(t):return datetime.fromtimestamp(t/1000,BRT).date().isoformat()
def total(rs):return sum(r['net'] for r in rs)
def mean(rs):return total(rs)/len(rs) if rs else None
def delta(v,b):return mean(v)-mean(b) if v and b else None
def fmt(x):return 'N/A' if x is None else f'{x:+.6f}' if isinstance(x,float) else str(x)
def table(headers,rows):return '\n'.join([' | '.join(headers),' | '.join(['---']*len(headers)),*[' | '.join(fmt(x) for x in r) for r in rows]])
def csv_write(name,rows):
 if not rows:return
 keys=list(dict.fromkeys(k for r in rows for k in r))
 with (S/name).open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=keys);w.writeheader()
  for r in rows:w.writerow({k:json.dumps(v,ensure_ascii=False,separators=(',',':')) if isinstance(v,(dict,list)) else v for k,v in r.items()})

def equity_at(r,t):
 i=(t-START)//M-1
 return r['equity_minute'][i] if i>=0 else 0.
def calendar(r,a,b):
 initial=equity_at(r,a);peak=initial;dd=0
 lo=(a-START)//M;hi=(b-START)//M
 for value in r['equity_minute'][lo:hi]:peak=max(peak,value);dd=max(dd,peak-value)
 events=defaultdict(lambda:[0.,0.]);time_weighted=0.;count_weighted=0.
 for t in r['records']:
  x=max(a,t['open_ms']);y=min(b,t['close_ms'] if t['close_ms'] is not None else END)
  if y<x or t['open_ms']>=b or (t['close_ms'] is not None and t['close_ms']<a):continue
  time_weighted+=t['notional']*(y-x);count_weighted+=y-x;events[x][0]+=t['notional'];events[y][1]+=t['notional']
 exposure=maximum=0.
 for stamp,(opened,closed) in sorted(events.items()):
  # Replay admits at the boundary before its first price tick; preserve tied peaks.
  exposure+=opened;maximum=max(maximum,exposure);exposure-=closed
 return {'DD_minute_mtm':dd,'net_calendar_mtm':equity_at(r,b)-initial,'exposure_mean_usdt':time_weighted/(b-a),'exposure_max_usdt':maximum,'positions_mean':count_weighted/(b-a),'positions_max':round(maximum/20),'notional_hours':time_weighted/3600000,'carry_in_N':sum(t['open_ms']<a and (t['close_ms'] is None or t['close_ms']>=a) for t in r['records'])}

def metric(rs):
 counts=Counter(r['reason'] for r in rs);closed=sum(r['close_ms'] is not None for r in rs)
 return {'N':len(rs),'closed':closed,'open':len(rs)-closed,'net_total':total(rs),'net_realized':sum(r['net'] for r in rs if r['close_ms'] is not None),'net_open_mtm':sum(r['net'] for r in rs if r['close_ms'] is None),'net_per_trade':mean(rs),'HS':counts['HARD_STOP'],'HS_pct':counts['HARD_STOP']/closed*100 if closed else None,'PL':counts['PROFIT_LOCK'],'TRAIL':counts['TRAILING'],'PL_pct':counts['PROFIT_LOCK']/closed*100 if closed else None,'TRAIL_pct':counts['TRAILING']/closed*100 if closed else None,'reasons':dict(counts)}

def bootstrap(v,b):
 days=list(range(START//86400000,END//86400000+1))
 first=datetime.fromtimestamp(START/1000,BRT).date();last=datetime.fromtimestamp((END-1)/1000,BRT).date()
 n=(last-first).days+1;keys=[(first+timedelta(days=i)).isoformat() for i in range(n)]
 groups=[]
 for i in range(0,n,3):
  dd=set(keys[i:i+3]);vr=[r for r in v if day(r['open_ms']) in dd];br=[r for r in b if day(r['open_ms']) in dd]
  groups.append((total(vr),len(vr),total(br),len(br)))
 rng=random.Random(20261026);samples=[]
 for _ in range(5000):
  vs=vn=bs=bn=0
  for _ in groups:
   x=rng.choice(groups);vs+=x[0];vn+=x[1];bs+=x[2];bn+=x[3]
  if vn and bn:samples.append(vs/vn-bs/bn)
 samples.sort()
 return {'method':'non-overlapping 72h entry-day blocks, trajectory conditional','blocks':len(groups),'replicates':5000,'seed':20261026,'CI95':[samples[int(.025*(len(samples)-1))],samples[int(.975*(len(samples)-1))]],'gate':False}

def augment(r):
 timeline=read('regime_timeline.json');times={k:[x['at'] for x in timeline[k]] for k in ['mare','onda']}
 for t in r['records']:
  t.update(status='OPEN' if t['close_ms'] is None else 'CLOSED',source_candle=brt(t['source']),entry_BRT=brt(t['open_ms']),exit_BRT=brt(t['close_ms']),mare_open=t['mare'],onda_open=t['onda'])
  for layer in ['mare','onda']:
   idx=bisect.bisect_right(times[layer],t['close_ms'])-1 if t['close_ms'] is not None else -1
   t[layer+'_close']='OPEN' if t['close_ms'] is None else timeline[layer][idx]['label'] if idx>=0 else 'UNAVAILABLE'
  t.update(trend_open=t['mare_open'],wave_open=t['onda_open'],trend_close=t['mare_close'],wave_close=t['onda_close'])
 return r

def main():
 assert read('PARIDADE.json')['passed'];assert all(x['returncode']==0 for x in read('EXECUCOES.json'))
 assert len(list((S/'runs').glob('*.json')))==16
 summaries=[];comparisons=[];blocks=[];activity=[];substitutes=[];classifications=[];verifications=[];audit_rows=[];all_trades=[]
 for path in g.PATHS:
  for base in g.BASES:
   b=augment(read('runs/'+base+'_'+path+'.json'));v=augment(read('runs/'+base+'_STACK_GUARD_2NEG_'+path+'.json'))
   for r,label in [(b,base),(v,base+'_STACK_GUARD_2NEG')]:
    r['export_label']=label;r['preregister_sha256']=MAN['preregister_sha256'];r['stack_engine_sha256']=hashlib.sha256((ROOT/'tools/stack_guard_engine.py').read_bytes()).hexdigest()
    assert len(r['records'])==sum(a['accepted'] for a in r['admissions'])
    assert len(set(t['source'] for t in r['records']))==len(r['records'])
    assert math.isclose(total(r['records']),r['stats']['net_total'],abs_tol=1e-8)
    assert r['stats']['max_positions']<=5
    assert len(r['equity_minute'])==(END-START)//M
    assert all(t['open_ms']>t['source'] and (t['close_ms'] is None or t['close_ms']>=t['open_ms']) for t in r['records'])
    assert all(t['reason'] in ['HARD_STOP','PROFIT_LOCK','TRAILING','OPEN'] for t in r['records'])
    epath='runs/'+label+'_'+path
    dump(epath+'.json',r);csv_write(epath+'.csv',r['records'])
    all_trades.extend({'arm':label,'path':path,**t} for t in r['records'])
    for period,a,z in [('TOTAL',START,END),('METADE_1',START,CUT),('METADE_2',CUT,END)]:
     rs=[t for t in r['records'] if a<=t['open_ms']<z];bs=[t for t in b['records'] if a<=t['open_ms']<z]
     met=metric(rs);bc=calendar(b,a,z);cal=calendar(r,a,z);bm=metric(bs)
     row={'base':base,'arm':label,'path':path,'period':period,'start_BRT':brt(a),'end_exclusive_BRT':brt(z),**met,**cal}
     for key in ['N','closed','open','net_total','net_realized','net_open_mtm','net_per_trade','HS','HS_pct','PL','TRAIL','PL_pct','TRAIL_pct']:row['delta_'+key]=met[key]-bm[key] if met[key] is not None and bm[key] is not None else None
     for key in cal:row['delta_'+key]=cal[key]-bc[key]
     summaries.append(row)
    assert math.isclose(calendar(r,START,END)['DD_minute_mtm'],r['stats']['DD_minute_mtm'],abs_tol=1e-9)
    assert calendar(r,START,END)['positions_max']==r['stats']['max_positions']
   br=b['records'];vr=v['records'];bi={t['source']:t for t in br};vi={t['source']:t for t in vr}
   common=set(bi)&set(vi);onlyb=set(bi)-set(vi);onlyv=set(vi)-set(bi)
   common_delta=sum(vi[s]['net']-bi[s]['net'] for s in common)
   delta_total=total(vr)-total(br)
   assert math.isclose(delta_total,common_delta+sum(vi[s]['net'] for s in onlyv)-sum(bi[s]['net'] for s in onlyb),abs_tol=1e-8)
   # Same entry and exit rules: truly common trades must match exactly economically.
   for s in common:
    for k in ['entry','quantity','atr','net','reason','close_ms']:assert vi[s][k]==bi[s][k],(base,path,s,k)
   blocked=[x for x in v['stack_audit'] if x['effective']];true=[x for x in v['stack_audit'] if x['condition']]
   # Independent reconstruction from final trades, prior to the signal's opening.
   ordered=sorted(vr,key=lambda t:t['open_ms']);pointer=0;active=[]
   for x in v['stack_audit']:
    while pointer<len(ordered) and ordered[pointer]['open_ms']<x['at']:
     active.append(ordered[pointer]);pointer+=1
    active=[t for t in active if t['close_ms'] is None or t['close_ms']>=x['at']]
    assert len(active)==x['open_N'],(base,path,x['at'],'reconstructed_open_count')
    assert sum(x['signal_close']<t['entry'] for t in active)==x['negative_N'],(base,path,x['at'],'reconstructed_negative_count')
   assert all(x['condition'] and not x['accepted'] and x['reason']=='ENTRY_BLOCKED_STACK_GUARD_2NEG' for x in blocked)
   assert all(x['open_N']>=3 and x['negative_N']>=2 for x in blocked)
   for x in v['stack_audit']:
    assert x['condition']==(x['open_N']>=3 and x['negative_N']>=2)
    if x['condition']:assert sum(p['negative'] for p in x['positions'])==x['negative_N'] and all(p['negative']==(x['signal_close']<p['entry']) for p in x['positions'])
   observed=[bi[x['source']] for x in blocked if x['source'] in bi]
   absent=[x for x in blocked if x['source'] not in bi]
   detail=[{'base':base,'path':path,**x,'signal_BRT':brt(x['at']),'base_admitted':x['source'] in bi,'base_exit_reason':bi[x['source']]['reason'] if x['source'] in bi else 'NOT_ADMITTED','base_net':bi[x['source']]['net'] if x['source'] in bi else None} for x in true]
   audit_rows+=detail
   act={'base':base,'path':path,'signals':len(v['stack_audit']),'condition_true_N':len(true),'effective_blocks_N':len(blocked),'true_but_other_block':dict(Counter(x['reason'] for x in true if not x['effective'])),'effective_base_admitted_N':len(observed),'effective_base_not_admitted_N':len(absent),'base_outcomes':metric(observed),'details':detail}
   activity.append(act)
   exclusive=[vi[s] for s in sorted(onlyv)];substitutes.append({'base':base,'path':path,**metric(exclusive),'sources':sorted(onlyv),'trades':exclusive})
   days=defaultdict(float)
   for t in vr:days[day(t['open_ms'])]+=t['net']
   for t in br:days[day(t['open_ms'])]-=t['net']
   top=sorted([(d,x) for d,x in days.items() if x>1e-10],key=lambda x:(-x[1],x[0]))[:3];excluded={d for d,x in top}
   vtrim=[t for t in vr if day(t['open_ms']) not in excluded];btrim=[t for t in br if day(t['open_ms']) not in excluded]
   halves=[{'period':name,'delta_mean':delta([t for t in vr if a<=t['open_ms']<z],[t for t in br if a<=t['open_ms']<z])} for name,a,z in [('METADE_1',START,CUT),('METADE_2',CUT,END)]]
   comp={'base':base,'path':path,'delta_mean':delta(vr,br),'delta_total':delta_total,'base_net':total(br),'variant_net':total(vr),'common_N':len(common),'common_delta':common_delta,'only_variant_N':len(onlyv),'only_variant_net':total(exclusive),'only_base_N':len(onlyb),'only_base_net':sum(bi[s]['net'] for s in onlyb),'halves':halves,'top3_days':[{'day':d,'delta_net':x} for d,x in top],'excluding_top3_delta_mean':delta(vtrim,btrim),'excluding_top3_delta_total':total(vtrim)-total(btrim),'bootstrap72h':bootstrap(vr,br),'effective_blocks_N':len(blocked)}
   comparisons.append(comp)
   # Fixed base-derived HS windows, never selected by variant outcomes.
   hs=sorted([t for t in br if t['reason']=='HARD_STOP'],key=lambda t:t['close_ms']);groups=[]
   for t in hs:
    if not groups or t['close_ms']-groups[-1][-1]['close_ms']>6*3600000:groups.append([])
    groups[-1].append(t)
   assert sum(len(x) for x in groups)==len(hs)
   for i,group in enumerate(groups):
    a=min(t['open_ms'] for t in group)//M*M;z=min(END,group[-1]['close_ms']//M*M+M)
    out={'base':base,'path':path,'block':i+1,'block_type':'ISOLADO_1' if len(group)==1 else 'MULTIPLO','start_ms':a,'end_exclusive_ms':z,'start_BRT':brt(a),'end_exclusive_BRT':brt(z),'base_defined_HS_N':len(group),'effective_blocks_N':sum(a<=x['at']<z for x in blocked),'condition_true_N':sum(a<=x['at']<z for x in true),'defining_sources':[t['source'] for t in group]}
    for name,r in [('base',b),('guard',v)]:
     closed=[t for t in r['records'] if t['close_ms'] is not None and a<=t['close_ms']<z]
     cohort=[t for t in r['records'] if a<=t['open_ms']<z]
     out[name+'_HS']=sum(t['reason']=='HARD_STOP' for t in closed);out[name+'_net_realized_exits']=total(closed);out[name+'_exit_reasons']=dict(Counter(t['reason'] for t in closed));out[name+'_net_window_mtm']=equity_at(r,z)-equity_at(r,a)
     out[name+'_entry_cohort']=metric(cohort)
     out[name+'_net_entry_cohort']=total(cohort);out[name+'_entry_cohort_N']=len(cohort)
    assert out['base_HS']>=len(group)
    out['delta_HS']=out['guard_HS']-out['base_HS'];out['delta_net_window_mtm']=out['guard_net_window_mtm']-out['base_net_window_mtm'];out['delta_net_entry_cohort']=out['guard_net_entry_cohort']-out['base_net_entry_cohort'];blocks.append(out)
   verifications.append({'base':base,'path':path,'economic_conservation':True,'source_uniqueness':True,'capacity':True,'common_trade_economics_exact':True,'decomposition':True,'block_HS_groups_conservation':True,'block_rule_causal_price':True,'all_signal_open_and_negative_counts_independently_reconstructed':True,'max_exposure_matches_engine':True,'drawdown_matches_engine':True,'csv_trades_N':len(br)+len(vr)})
   print('ANALYZED',base,path,flush=True)
 for base in g.BASES:
  pair=[x for x in comparisons if x['base']==base]
  gates={'threshold_both_paths':all(x['delta_mean'] is not None and x['delta_mean']>=.005-1e-10 for x in pair),'halves_positive_both_paths':all(q['delta_mean'] is not None and q['delta_mean']>1e-10 for x in pair for q in x['halves']),'excluding_top3_positive_both_paths':all(x['excluding_top3_delta_mean'] is not None and x['excluding_top3_delta_mean']>1e-10 for x in pair),'effective_blocks_both_paths':all(x['effective_blocks_N']>0 for x in pair)}
  positive_half=[all(x['halves'][i]['delta_mean'] is not None and x['halves'][i]['delta_mean']>1e-10 for x in pair) for i in [0,1]]
  good_only_one_half=sum(positive_half)==1
  group='CANDIDATA' if all(gates.values()) else 'INCONCLUSIVA' if good_only_one_half else 'SEM_EVIDENCIA_DE_BENEFICIO' if any(x['delta_mean'] is not None and x['delta_mean']<=1e-10 for x in pair) else 'INCONCLUSIVA'
  classifications.append({'base':base,'classification':group,'gates':gates,'good_only_one_half_human_exception':good_only_one_half,'mapping_errata':'ERRATA_MAPEAMENTO_CLASSIFICACAO.md','scientific_approval':False,'forward_authorized':False})
 for p,rows in [('RESUMO',summaries),('COMPARACOES',comparisons),('BLOQUEIOS',activity),('SUBSTITUTOS',substitutes),('BLOCOS_HS',blocks),('CLASSIFICACAO',classifications)]:dump(p+'.json',rows)
 csv_write('TABELA_METRICAS.csv',summaries);csv_write('COMPARACOES.csv',comparisons);csv_write('BLOQUEIOS_DETALHES.csv',audit_rows);csv_write('BLOCOS_HS.csv',blocks);csv_write('TRADES_TODOS.csv',all_trades)
 dump('VERIFICACAO_FINAL.json',{'passed':True,'checks':verifications,'runs_json':16,'runs_csv':16,'regime_cell_classification':False,'cut_ms':CUT,'regimes_diagnostic_only':True,'scientific_approval':False})
 report(summaries,comparisons,activity,substitutes,blocks,classifications)

def report(summaries,comparisons,activity,substitutes,blocks,classes):
 lines=['# STACK_GUARD_2NEG — relatório de replay','',
 'Status: PROPOSTO_PARA_REVISAO. Objeto REGRA_IDEALIZADA / REPLAY_SISTEMICO; revisão não independente; sem homologação científica ou autorização de forward.',
 f'Janela: {brt(START)} BRT até {brt(END-1)},999 BRT. END exclusivo: {brt(END)} BRT. Metades em {brt(CUT)} BRT. LOW_FIRST referência; HIGH_FIRST sensibilidade.',
 'Custos: 3,5bps de fricção por lado + taxa0,10% por lado; notional20USDT; max5posições; saídas/CB/spacing/bucket preservados. Capital100USDT nominal, sem cashgate; não é conta autofinanciada100. Aberto = MTM líquido com custos de saída estimados. Paths não são limites econômicos garantidos nem amostras independentes. Histórico reutilizado, mais25 configurações anteriores e4variantes novas; inventário total incompleto.',
 'Paridade: oito testes base/path, wrapper desligado EXATAMENTE igual à base na janela completa (trades/admissões/equity/eventos/estatísticas), e prefixo contra MULTI16. Nenhuma variante foi rodada antes de todos passarem. Dados1m sem lacunas; overlap12h exato; OHLCcauda5m/15m/1h igual ao nativo.',
 'Regra negativa usa close do candle source, comparado ao preço de entrada com fricção. Condição auditada em todos os sinais. Bloqueio efetivo apenas quando todos os critérios atuais passariam: inserido imediatamente antes de abrir, sem reservar vaga/bucket/spacing. Outras recusas mantêm motivo original.',
 'Critério congelado: delta médio ≥0,005USDT/admissão nos dois paths; delta positivo nas duas metades e sem3melhoresdias; bloqueios efetivos nos dois paths. O limiar0,005 é global, conforme convenção anterior; metades exigem sinal positivo. IC não é veto. Bom só numa metade é inconclusivo MESMO com média global não positiva, conforme mandato humano anterior. Fora dessa exceção, sem evidência se média global igual/pior em algum path. A precedência dessa exceção foi corrigida após a primeira rotina: ver ERRATA_MAPEAMENTO_CLASSIFICACAO.md e primeira classificação preservada. Nenhum resultado econômico ou gate de candidatura mudou. Candidatura é triagem, não validação independente nem lucro.',
 '## Resultado de triagem',table(['Base','Classificação','Gates'],[[x['base'],x['classification'],json.dumps(x['gates'],ensure_ascii=False)] for x in classes]),'',
 table(['Base','Path','Delta net','Delta USDT/trade','1ª metade','2ª metade','Sem top3','Bloqueios','IC95 blocos72h'],[[x['base'],x['path'],x['delta_total'],x['delta_mean'],x['halves'][0]['delta_mean'],x['halves'][1]['delta_mean'],x['excluding_top3_delta_mean'],x['effective_blocks_N'],str(x['bootstrap72h']['CI95'])] for x in comparisons]),
 '## Métricas completas e deltas',
 'N/net/nettrade/HS/PL/TRAIL das metades são COORTES POR ENTRADA, mantendo o resultado até END. DD/exposição e net_calendar_mtm são JANELAS CRONOLÓGICAS da carteira, com carry-over. Não somar o DD das metades; não interpretar DD cronológico como DD da coorte. %HS sobre fechados. Exposição é notional comprometido (não valor de mercado), integrada pelo tempo exato; DD é equity líquida amostrada ao fim1m, podendo omitir extremo intraminuto. Detalhes, realizado/MTM, percentuais e todos deltas em TABELA_METRICAS.csv.',
 table(['Braço','Path','Período','N fech/ab','Net','Δnet','Net/trade','Δmédia','HS','%HS','PL','TRAIL','DD','ΔDD','Exp média/max','Δexp média/max','Carry-in'],[[x['arm'],x['path'],x['period'],f"{x['N']} ({x['closed']}/{x['open']})",x['net_total'],x['delta_net_total'],x['net_per_trade'],x['delta_net_per_trade'],x['HS'],x['HS_pct'],x['PL'],x['TRAIL'],x['DD_minute_mtm'],x['delta_DD_minute_mtm'],f"{x['exposure_mean_usdt']:.4f}/{x['exposure_max_usdt']:.0f}",f"{x['delta_exposure_mean_usdt']:+.4f}/{x['delta_exposure_max_usdt']:+.0f}",x['carry_in_N']] for x in summaries]),
 '## Bloqueios e destinos reais na base',
 'Condição verdadeira não implica intervenção adicional. As entradas efetivas sem correspondente admitido na base são NOT_ADMITTED: a trajetória/CB/capacidade da base pode impedir a mesma source. Não atribuir a elas um resultado inventado. Net das entradas evitadas não representa sozinho a mudança de carteira.',
 table(['Base','Path','Condição verdadeira','Efetivos','Base admitiu/não','HS','PL','TRAIL','OPEN','Net observado base'],[[x['base'],x['path'],x['condition_true_N'],x['effective_blocks_N'],f"{x['effective_base_admitted_N']}/{x['effective_base_not_admitted_N']}",x['base_outcomes']['HS'],x['base_outcomes']['PL'],x['base_outcomes']['TRAIL'],x['base_outcomes']['open'],x['base_outcomes']['net_total']] for x in activity]),
 'Condição verdadeira barrada por outro critério: '+json.dumps([{k:x[k] for k in ['base','path','true_but_other_block']} for x in activity],ensure_ascii=False),
 '## Substitutos e decomposição',
 'Substitutos = sources admitidas somente na versão com trava, sem exigir relação um-a-um com um bloqueio. Os trades realmente comuns têm entrada,qty,ATR,saída,net exatamente iguais. Delta total = delta comuns + net exclusivos variante − net exclusivos base. Isso separa posições evitadas e novas admissões, sem atribuição causal independente.',
 table(['Base','Path','Substitutos','Net','HS','PL','TRAIL','OPEN'],[[x['base'],x['path'],x['N'],x['net_total'],x['HS'],x['PL'],x['TRAIL'],x['open']] for x in substitutes]),
 table(['Base','Path','Comuns','Δcomuns','Só variante N/net','Só base N/net','Δtotal'],[[x['base'],x['path'],x['common_N'],x['common_delta'],f"{x['only_variant_N']}/{x['only_variant_net']:+.6f}",f"{x['only_base_N']}/{x['only_base_net']:+.6f}",x['delta_total']] for x in comparisons]),
 '## Concentração e incerteza',
 'Top3 por dia BRT de ENTRADA, maior contribuição positiva ao delta net; retirar os mesmos dias das duas carteiras, recalcular médias sem rerodar. Não são os três melhores dias de cada carteira separadamente. Bootstrap descritivo em blocos consecutivos de3dias de entrada,5000réplicas,seed20261026. IC condicionado às trajetórias observadas; não corrige seleção por histórico reutilizado. Dias sem trades também entram nos blocos. Paths e combinações não são réplicas independentes.',
 table(['Base','Path','Dias removidos e contribuição delta'],[[x['base'],x['path'],str(x['top3_days'])] for x in comparisons]),
 '## Blocos HS da base',
 'Definição HUMANA no adendo anterior à análise: ordenar saídasHS da base; juntar intervalos consecutivos≤6h; incluir unitários marcados ISOLADO_1. Janelas começam no início do minuto da ENTRADA MAIS ANTIGA dentre as posições que deramHS no bloco e terminam após o minuto do últimoHS, exclusivas à direita. Mesma janela absoluta nos dois braços; cada base/path tem blocos próprios. HS contados pelo horário de SAÍDA na janela; NET principal = trades com ENTRADA na janela, resultado completo atéEND e MTM líquido dos abertos. Janela pode incluir outrosHS e sobrepor blocos: HS da janela pode exceder tamanho do grupo; não somar blocos como população independente. BLOCOS_HS.csv/JSON também trazem net realizado por saída e variaçãoMTM como complementos separados.',
 table(['Base','Path','Bloco/tipo','HSgrupo','InícioBRT','Fim exclusivoBRT','HSbase','HStrava','Nentrada base/trava','Netentradas base','Netentradas trava','Δnetentradas','Bloqueios','Condição'],[[x['base'],x['path'],str(x['block'])+'/'+x['block_type'],x['base_defined_HS_N'],x['start_BRT'],x['end_exclusive_BRT'],x['base_HS'],x['guard_HS'],str(x['base_entry_cohort_N'])+'/'+str(x['guard_entry_cohort_N']),x['base_net_entry_cohort'],x['guard_net_entry_cohort'],x['delta_net_entry_cohort'],x['effective_blocks_N'],x['condition_true_N']] for x in blocks]),
 '## Arquivos e limites',
 'runs/:16JSON +16CSV para planilha; MARÉ/ONDA entrada e saída (trend_open/wave_open/trend_close/wave_close), diagnósticos, sem classificação por célula. Mantido algoritmo de extremos das metades das janelas72h/12h. CSVs usam ponto decimal e UTF8BOM. TRADES_TODOS.csv consolida os16runs. BLOQUEIOS_DETALHES.csv preserva todas condições verdadeiras; BLOQUEIOS.json também registra posições presentes. SUBSTITUTOS.json detalha exclusivos. Métricas negativas absolutas serão explicitadas na leitura, sem confundir delta positivo com lucro. Não foi demonstrada equivalência ao runtime nem solvência estrita do denominador100USDT. Sem mudança operacional ou registro oficial.'
 ]
 negative=sum(x['net_total']<0 for x in summaries if x['period']=='TOTAL')
 lines.insert(7,f'**Net absoluto negativo em {negative}/16 rodadas.** Melhora relativa não é lucro absoluto.')
 (S/'RELATORIO_STACK_GUARD.md').write_text('\n\n'.join(lines)+'\n',encoding='utf-8')
 brief=['# Leitura científica — STACK_GUARD_2NEG','',f'Janela até {brt(END-1)},999 BRT; metades em {brt(CUT)} BRT. Oito braços, dois paths. Paridade desligada e prefixos passaram. Net absoluto negativo em {negative}/16 rodadas.',
 table(['Base','Classificação','ΔmédiaLF','ΔnetLF','ΔmédiaHF','ΔnetHF'],[[base,next(x['classification'] for x in classes if x['base']==base),*[next(x[k] for x in comparisons if x['base']==base and x['path']==p) for p in g.PATHS for k in ['delta_mean','delta_total']]] for base in g.BASES]),
 'Nenhuma candidata. O net total melhora nos quatro pares, mas o resultado por trade piora em BE_OFF_CB, MACD_BU_MINUS e EMA_MACD. HIST_1M ganha cerca0,00087USDT/trade, abaixo0,005, tem primeira metade negativa e perde vantagem semtop3. EMA_MACD tem segunda metade positiva nos dois paths e fica INCONCLUSIVA pela regra humana de uma metade favorável. A primeira rotina havia herdado o veto global anterior: corrigido e documentado na ERRATA_MAPEAMENTO_CLASSIFICACAO.md, sem mudar gates de candidata ou qualquer economia; primeira classificação preservada.',
 table(['Base','Path','Trades base/trava','Net base/trava','DD base/trava','Exposição média base/travaUSDT','Exposição máxima base/travaUSDT'],[[base,path,*[f"{next(x[k] for x in summaries if x['arm']==base and x['path']==path and x['period']=='TOTAL'):.6f} / {next(x[k] for x in summaries if x['arm']==base+'_STACK_GUARD_2NEG' and x['path']==path and x['period']=='TOTAL'):.6f}" for k in ['N','net_total','DD_minute_mtm','exposure_mean_usdt','exposure_max_usdt']]] for path in g.PATHS for base in g.BASES]),
 'O resultado mede mudança de carteira: entradas evitadas, CB/capacidade e substitutos. A trava pode reduzir HS apenas por reduzir exposição; conferir net/trade, net total, DD e exposição lado a lado. Destinos observados da base não representam um benefício independente somável à mudança dos substitutos.',
 'Candidatura segue os gates congelados, sem veto de IC ou benefício condicional extra. Bom apenas numa metade é inconclusivo. Histórico amplamente reutilizado; não há validação independente, homologação ou autorização de forward. Sem classificação por células MARÉ×ONDA. Relatório integral e CSVs contêm os números e limitações.'
 ]
 (S/'LEITURA_CIENTIFICA.md').write_text('\n\n'.join(brief)+'\n',encoding='utf-8')
 print('ANALYSIS_COMPLETE',json.dumps(classes),flush=True)

if __name__=='__main__':main()
