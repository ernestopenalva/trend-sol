"""Reclassify persisted ACT10/GAP5 trades; no trajectory simulation."""
import json,math,random,sys,hashlib
from collections import Counter
from pathlib import Path
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.friction_pl_trail_study import accounting
from tools.feed_trail_revalidation import quantile
ROOT=Path(__file__).resolve().parents[1]
INPUT=ROOT/'data/analysis/feed_trail_revalidation_20261009'
OUT=ROOT/'data/analysis/atr_units_20261009'
ZONE=timezone(timedelta(hours=-3))

def distribution(xs):
    return {'n':len(xs),**{k:quantile(xs,q) for k,q in [('min',0),('p10',.1),('p25',.25),('median',.5),('p75',.75),('p90',.9),('max',1)]}}

def quartile(x,cuts):return 1+sum(x>c for c in cuts)

def summary(rows):
    closed=[r for r in rows if r['closed_ms'] is not None];nets=[r['net_usd'] for r in closed]
    loss=-sum(min(0,n) for n in nets)
    return {'N':len(rows),'closed':len(closed),'open':len(rows)-len(closed),'net':sum(nets),
        'net_trade':sum(nets)/len(nets) if nets else None,
        'gross_edge_bps':sum(r['gross_before_costs'] for r in closed)/len(closed)/20*10000 if closed else None,
        'PF':sum(max(0,n) for n in nets)/loss if loss else None,
        'exits':dict(Counter(r['exit_group'] for r in closed)),
        'mfe_atr_median':quantile([r['mfe_atr'] for r in rows],.5),
        'PL1_PL2_floor_coincident':sum(r['economic_floor_atr']>=3-1e-9 for r in rows),
        'both_armed_N':sum(all(k in r['armed_steps'] for k in [1,2]) for r in rows),
        'both_armed_coincident':sum(all(k in r['armed_steps'] for k in [1,2]) and r['economic_floor_atr']>=3-1e-9 for r in rows)}

def bootstrap(rows,label_a,label_b,kind='quartile',alpha=.05):
    closed=[r for r in rows if r['closed_ms'] is not None]
    dates=sorted({r['day'] for r in closed});cursor=datetime.fromisoformat(dates[0]);days=[]
    while cursor.strftime('%Y-%m-%d')<=dates[-1]:
        days.append(cursor.strftime('%Y-%m-%d'));cursor+=timedelta(days=1)
    ag={d:{label_a:[0.,0],label_b:[0.,0]} for d in days}
    for r in closed:
        label=r[kind]
        if label in [label_a,label_b]:
            ag[r['day']][label][0]+=r['net_usd'];ag[r['day']][label][1]+=1
    blocks=[days[i:i+7] for i in range(len(days)-6)];rng=random.Random(20261009);vals=[]
    n_a=sum(ag[d][label_a][1] for d in days);n_b=sum(ag[d][label_b][1] for d in days)
    estimate=(sum(ag[d][label_b][0] for d in days)/n_b-sum(ag[d][label_a][0] for d in days)/n_a) if n_a and n_b else None
    for _ in range(5000):
        sample=[d for b in rng.choices(blocks,k=math.ceil(len(days)/7)) for d in b][:len(days)]
        sa=sum(ag[d][label_a][0] for d in sample);sb=sum(ag[d][label_b][0] for d in sample)
        na=sum(ag[d][label_a][1] for d in sample);nb=sum(ag[d][label_b][1] for d in sample)
        if na and nb:vals.append(sb/nb-sa/na)
    shared=sum(any(ag[d][label_a][1] for d in days[i:i+7]) and any(ag[d][label_b][1] for d in days[i:i+7]) for i in range(0,len(days),7))
    ci=[quantile(vals,alpha/2),quantile(vals,1-alpha/2)]
    return dict(delta_net_trade=estimate,ci=ci,N_a=n_a,N_b=n_b,shared_week_blocks=shared,
                low_n=min(n_a,n_b)<10 or shared<10)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    data={p:json.loads((INPUT/f'{p}_ACT10_GAP5.json').read_text()) for p in ['HIGH_FIRST','LOW_FIRST']}
    unique={}
    for x in data.values():
        for d in x['details']:
            value=100*d['entry_atr']/d['entry_price']
            if d['source_candle'] in unique:assert abs(unique[d['source_candle']]-value)<1e-10
            unique[d['source_candle']]=value
    cuts=[quantile(list(unique.values()),q) for q in [.25,.5,.75]]
    result={'quartile_cuts_atr_pct':cuts,'unique_sources':len(unique),'paths':{}};armed=[]
    for path,x in data.items():
        detail={d['source_candle']:d for d in x['details']};rows=[]
        for t in x['run']['trades']:
            d=detail[t['source_candle']];atr=d['entry_atr'];atr_pct=100*atr/t['entry_price'];floor=.25/atr_pct
            first={}
            for e in d['floor_events']:
                for key in e['PL_armed']:
                    step=int(key.split(':')[1])
                    if step not in first:
                        first[step]=e['at_ms'];lock={1:1.5,2:3,3:6}[step]
                        armed.append({'path':path,'source_candle':t['source_candle'],'armed_ms':e['at_ms'],
                            'step':step,'atr_pct':atr_pct,'raw_lock_atr':lock,'economic_floor_atr':floor,
                            'effective_plan_floor_atr':max(lock,floor),'binding':'ECONOMIC' if floor>lock+1e-9 else 'TIE' if abs(floor-lock)<=1e-9 else 'ATR',
                            'observed_stop_owner':e['owner'],'observed_effective_stop':e['effective_stop']})
            terminal_step=d['floor_events'][-1]['PL_step'] if d['floor_events'] else None
            costs=accounting(t,20,5) if t['closed_ms'] else None
            rows.append({**t,'atr_pct':atr_pct,'quartile':quartile(atr_pct,cuts),'economic_floor_atr':floor,
                'hs_distance_atr':1.5/atr_pct,'cost_total_atr':(costs['fees']+costs['spread'])/(20/t['entry_price'])/atr if costs else None,
                'gross_before_costs':costs['gross'] if costs else None,'mfe_atr':(t['peak_price']-t['entry_price'])/atr,
                'armed_steps':sorted(first),'exit_group':terminal_step if t['exit_reason']=='PROFIT_LOCK' else 'HS' if t['exit_reason']=='HARD_STOP' else 'TRAIL' if t['exit_reason']=='TRAILING' else t['exit_reason'],
                'day':datetime.fromtimestamp(t['opened_ms']/1000,ZONE).strftime('%Y-%m-%d')})
        r={'distributions':{f:distribution([t[f] for t in rows if t[f] is not None]) for f in ['atr_pct','hs_distance_atr','cost_total_atr','mfe_atr']},
            'quartiles':{q:summary([t for t in rows if t['quartile']==q]) for q in range(1,5)},
            'PL1_PL2_economics':{q:bootstrap([t for t in rows if t['quartile']==q],'PL1','PL2','exit_group',.0125) for q in range(1,5)},
            'regime_test':bootstrap(rows,1,4),
            'monthly':{m:{q:summary([t for t in rows if t['day'].startswith(m) and t['quartile']==q]) for q in range(1,5)} for m in ['2026-06','2026-07','2026-08','2026-09','2026-10']}}
        r['steps']={step:{'N_armed':len(rs),'binding':dict(Counter(t['binding'] for t in rs)),
                          'economic_binding_fraction':sum(t['binding']=='ECONOMIC' for t in rs)/len(rs) if rs else None}
                    for step in [1,2,3] for rs in [[t for t in armed if t['path']==path and t['step']==step]]}
        result['paths'][path]=r
        (OUT/f'{path}_trades.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    (OUT/'armed_steps.json').write_text(json.dumps(armed,indent=2),encoding='utf-8')
    (OUT/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)

def readout():
    s=json.loads((OUT/'summary.json').read_text());armed=json.loads((OUT/'armed_steps.json').read_text())
    fmt=lambda v:'N/A' if v is None else f'{v:.6f}' if isinstance(v,float) else str(v)
    table=lambda h,rs:'\n'.join(['| '+' | '.join(h)+' |','|'+'|'.join(['---']*len(h))+'|']+
        ['| '+' | '.join(fmt(v) for v in row)+' |' for row in rs])
    cuts=s['quartile_cuts_atr_pct'];paths=s['paths']
    lines=['# ACT10/GAP5 — regra efetiva em unidades comparáveis',
        '01/06/2026 → 02/10/2026 22:28 BRT. Apenas reclassificação dos artefatos existentes, nenhuma construção de posição/execução de engine/simulação nova. Critério registrado antes do cálculo em PREREGISTER.md.',
        '## Definições e preservação da semântica',
        'ATR% de entrada =100×entry_ATR/entry_fill. HS teórico =1,5%/ATR%; piso econômico PL =(0,20% taxas+0,05% margem)/ATR%. Essa margem não é spread. Custo total em ATR usa fees + spread modelado exato sobre a quantidade original, dividido por qty×entry_ATR. Valores parecidos de custo e piso têm significado diferente. MFE=(peak−entry)/entry_ATR.',
        'Locks operacionais permaneceram1,5/3/6ATR, activation10/gap5, HS1,5%. Não mudamos unidades ou parâmetros operacionais. “ECONOMIC vigente” significa binding do plano PL individual: max(lock bruto,piso econômico). Não implica que esse plano governava o effective_stop da posição: o owner real também foi preservado em armed_steps.json.',
        'Degrau armado = primeira aparição em PL_armed dos snapshots persistidos. Se várias chaves aparecem no mesmo tick-modelo, recebem o mesmo timestamp; owner armazenado é o estado ao final daquele tick, não cada instrução intermediária. Não inventamos timestamps intraminuto.',
        '## 1 — o que efetivamente definiu cada trava',
        table(['path','PL','N armado','econ binding N','econ binding%','ATR binding N','empates'],
            [[p,k,r['N_armed'],r['binding'].get('ECONOMIC',0),100*(r['economic_binding_fraction'] or 0),r['binding'].get('ATR',0),r['binding'].get('TIE',0)] for p,pr in paths.items() for k,r in pr['steps'].items()]),
        'Todas as ocorrências por trade/degrau, timestamp, ATR%, raw lock ATR, piso econômico ATR, trava efetiva ATR e owner estão em armed_steps.json / DEGRAUS.md. Fração tem como denominador os trades que efetivamente armaram aquele degrau, não a coorte inteira.',
        '## 2 — distribuições em ATR',
        table(['path','variável','N','min','p10','p25','mediana','p75','p90','max'],
            [[p,f,*[r[k] for k in ['n','min','p10','p25','median','p75','p90','max']]] for p,pr in paths.items() for f,r in pr['distributions'].items()]),
        'HS é distância teórica desde entry, não perda líquida realizada com fees/gap. CustoATR é remarcação contábil dos custos observados no modelo, não outro replay zero-fricção. Excursão é observada até o encerramento original.',
        '## 3 — quatro quartis fixos compartilhados',
        f'Cortes ATR%: p25={cuts[0]:.9f}%, p50={cuts[1]:.9f}%, p75={cuts[2]:.9f}%. Construídos sobre{ s["unique_sources"] }sources únicos da união HIGH/LOW, com ATR/entry idêntico por source. Limite igual ao corte pertence ao quartil inferior. Paths não dobram N independente.',
        table(['path','Q','N','net','net/trade','gross bps/trade','PF','HS','PL1','PL2','PL3','TRAIL','MFE med ATR'],
            [[p,q,r['N'],r['net'],r['net_trade'],r['gross_edge_bps'],r['PF'],*[r['exits'].get(k,0) for k in ['HS','PL1','PL2','PL3','TRAIL']],r['mfe_atr_median']] for p,pr in paths.items() for q,r in pr['quartiles'].items()]),
        'Gross edge acima é antes de fees/spread, pela identidade contábil do estudo anterior. Não é gross pós-spread salvo no run. Net/PF usam somente closed; nesta população todos estão resolvidos.',
        '## 4 — dependência de volatilidade: descritivo versus evidência',
        'Pré-registro: Q4−Q1 net/trade, moving block bootstrap7dias,5000 replicações,IC95%; evidência diagnóstica exigiria IC excluir zero nos dois paths e sinal estável em3/4 meses completos. Isso não testa causalidade nem declara todos os contrastes possíveis equivalentes.',
        table(['path','Q4−Q1 net/trade','IC95%','blocos semanais compartilhados'],[[p,r['regime_test']['delta_net_trade'],str(r['regime_test']['ci']),r['regime_test']['shared_week_blocks']] for p,r in paths.items()]),
        table(['path','mês','Q4−Q1 net/trade'],[[p,m,x['4']['net_trade']-x['1']['net_trade'] if x['4']['net_trade'] is not None and x['1']['net_trade'] is not None else None] for p,pr in paths.items() for m,x in pr['monthly'].items()]),
        'Q3 é o pior e Q4 o melhor ponto estimado agregado em ambos paths; todos os quartis têm net negativo. Não há gradiente monotônico. O contraste principal inclui zero em ambos paths; Q4−Q1 é negativo em junho/julho e positivo em agosto/setembro. O critério de dependência estável não foi satisfeito. Há heterogeneidade descritiva e interação temporal, não evidência suficiente de uma vantagem de regime robusta. Quartil de ATR de entrada não equivale automaticamente a regime de mercado.',
        '## 5 — PL1 e PL2 são realmente distintos?',
        table(['path','Q','N','pisos coincidentes N/%','ambos armados N','ambos armados e coincidentes'],[[p,q,r['N'],str(r['PL1_PL2_floor_coincident'])+' / '+fmt(100*r['PL1_PL2_floor_coincident']/r['N']),r['both_armed_N'],r['both_armed_coincident']] for p,pr in paths.items() for q,r in pr['quartiles'].items()]),
        'Geometria: piso econômico >=3ATR torna os floors PL1/PL2 iguais. Q1:100%; Q2:79,2746%; Q3/Q4:0%. Para os que armaram ambos, a mesma conclusão se mantém, com denominadores explícitos acima. Mesmo floor não significa mesmo trigger: quando presos pelo mesmo piso, PL1 arma em floor+3,5ATR e PL2 em floor+5ATR. São marcos de admissão da proteção diferentes, não duas travas diferentes.',
        table(['path','Q','PL1 exits N','PL2 exits N','PL2−PL1 net/trade','IC98,75%','LOW_N'],[[p,q,r['N_a'],r['N_b'],r['delta_net_trade'],str(r['ci']),r['low_n']] for p,pr in paths.items() for q,r in pr['PL1_PL2_economics'].items()]),
        'Economia dos grupos encerrados: Q1 não distinguível e LOW_N (só6 blocos semanais com presença de ambos;9/10 PL2). Q2 apresenta diferença estatística concordante, mas só~$0,00024/trade, num quartil misto em que a maioria dos pisos coincide. Q3 e Q4 têm diferenças concordantes e floors distintos; delta~$0,0111 e~$0,0431/trade respectivamente. São populações que atingiram estágios diferentes, não prova causal de vantagem de trocar PL1 por PL2. Não confundir significância numérica com relevância econômica.',
        '## Entrega e limites',
        'Nenhum parâmetro, unidade operacional, regra ou caminho de trading foi alterado. Nenhum novo replay, YAML, deploy/restart/commit/push. Ferramenta tools/atr_units_diagnostic.py; quatro testes matemáticos em tests/test_atr_units_diagnostic.py, todos PASS. Records por trade em HIGH_FIRST/LOW_FIRST_trades.json; registros individuais dos degraus e summary.json preservam números completos.']
    from tools.be_off_cb_exit_context_study import brt
    event_table=table(['path','source BRT','arm BRT','PL','ATR% entrada','raw lock ATR','econ floor ATR','effective PL ATR','binding','owner observado'],
        [[r['path'],brt(r['source_candle']),brt(r['armed_ms']),r['step'],r['atr_pct'],r['raw_lock_atr'],r['economic_floor_atr'],r['effective_plan_floor_atr'],r['binding'],r['observed_stop_owner']] for r in armed])
    (OUT/'DEGRAUS.md').write_text('# Degraus individuais — somente eventos já persistidos\n\n'+event_table,encoding='utf-8')
    (OUT/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'inputs_sha256':{p:hashlib.sha256((INPUT/f'{p}_ACT10_GAP5.json').read_bytes()).hexdigest() for p in paths},
        'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'simulation':'NONE',
        'tests':4,'cutoffs':cuts,'seed':20261009,'bootstrap_repetitions':5000},indent=2),encoding='utf-8')

if __name__=='__main__':
    if '--readout' in sys.argv:readout()
    else:main()
