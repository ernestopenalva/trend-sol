"""One-off diagnostic reclassification of frozen replay; no strategy replay/run."""
import argparse
import hashlib
import json
import math
import sys
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
from html import escape

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.monitor.price_structure import VERSION,STATES,HOUR,iso,timestamp_ms
from tools.price_structure_backfill import load_buffer

ZONE=timezone(timedelta(hours=-3))
def brt(at):return datetime.fromtimestamp(at/1000,ZONE).strftime('%d/%m/%Y %H:%M')
def q(xs,p):
    xs=sorted(xs)
    if not xs:return None
    i=(len(xs)-1)*p;left=int(i)
    return xs[left]+(xs[min(left+1,len(xs)-1)]-xs[left])*(i-left)
def quantiles(xs):return {str(p):q(xs,p) for p in (0,.1,.25,.5,.75,.9,1)}
def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','|'+'|'.join(['---']*len(headers))+'|']+
        ['| '+' | '.join(str(x) for x in r)+' |' for r in rows])
def f(x):return 'N/A' if x is None else f'{x:.4f}' if isinstance(x,float) else str(x)


def metrics(rows,opportunities):
    closed=[r for r in rows if r.get('closed_ms') is not None]
    values=[r['net_usd'] for r in closed];win=sum(max(0,v) for v in values);loss=-sum(min(0,v) for v in values)
    episodes=defaultdict(float);periods=defaultdict(lambda:dict(N=0,net=0.))
    gross=fees=spread=0.
    for r in closed:
        episodes[r['episode']]+=r['net_usd']
        month=datetime.fromtimestamp(r['opened_ms']/1000,ZONE).strftime('%Y-%m')
        periods[month]['N']+=1;periods[month]['net']+=r['net_usd']
        # Frozen replay: 20 USDT notional; half-spread 2.5bps each side, fees0.20%.
        qty=20/r['entry_price'];rawentry=r['entry_price']/1.00025;rawexit=r['exit_price']/.99975
        g=qty*(rawexit-rawentry);cost=qty*((r['entry_price']-rawentry)+(rawexit-r['exit_price']))
        fee=20*(r['gross_pct']-r['net_pct'])/100
        if abs(g-cost-fee-r['net_usd'])>1e-8:raise ValueError('Frozen accounting mismatch')
        gross+=g;spread+=cost;fees+=fee
    absolute=sorted([abs(v) for v in episodes.values()],reverse=True);den=sum(absolute)
    concentration={str(frac):next((i for i in range(1,len(absolute)+1) if sum(absolute[:i])>=den*frac),0) for frac in (.5,.8)}
    hs=sum(r['exit_reason']=='HARD_STOP' for r in closed)
    equity=peak=dd=0.
    for r in sorted(closed,key=lambda r:r['closed_ms']):
        equity+=r['net_usd'];peak=max(peak,equity);dd=max(dd,peak-equity)
    return dict(opportunities=opportunities,entries=len(rows),N=len(closed),open=len(rows)-len(closed),
        gross=gross,fees=fees,spread=spread,net=sum(values),net_trade=sum(values)/len(values) if values else None,
        PF=win/loss if loss else None,HS=hs,HS_pct=100*hs/len(closed) if closed else None,
        DD_illustrative=dd,episodes=len(episodes),episode_absolute_concentration=concentration,
        episode_net=dict(episodes),monthly=dict(periods),exits=dict(Counter(r['exit_reason'] for r in closed)))


def plot_example(snapshot,hours,path,previous=None):
    rows=[c for c in hours if snapshot['window_start_ms']<=c['open_time_ms']<snapshot['window_end_ms']]
    w,h=1100,600;left,right,top,bottom=75,1065,70,410
    lo=min(c['low'] for c in rows);hi=max(c['high'] for c in rows);pad=(hi-lo)*.08 or 1;lo-=pad;hi+=pad
    def x(at):return left+(at-snapshot['window_start_ms'])/HOUR*(right-left)/72+(right-left)/144
    def y(v):return bottom-(v-lo)/(hi-lo)*(bottom-top)
    mid=(left+right)/2
    svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
         '<rect width="1100" height="600" fill="white"/>',
         f'<text x="40" y="28" font-family="sans-serif" font-size="19">{VERSION} | {brt(timestamp_ms(snapshot["at"]))} BRT | {snapshot["label"]}</text>',
         f'<rect x="{left}" y="{top}" width="{mid-left}" height="{bottom-top}" fill="#eaf2fb"/>',
         f'<rect x="{mid}" y="{top}" width="{right-mid}" height="{bottom-top}" fill="#fff4e4"/>',
         f'<line x1="{mid}" y1="50" x2="{mid}" y2="{bottom}" stroke="#777" stroke-dasharray="5 4"/>',
         f'<text x="{left}" y="55">H1:36h</text><text x="{mid+5}" y="55">H2:36h</text>']
    for i in range(6):
        v=lo+(hi-lo)*i/5
        svg.append(f'<text x="10" y="{y(v)+4}" font-size="12">{v:.2f}</text><line x1="{left}" x2="{right}" y1="{y(v)}" y2="{y(v)}" stroke="#ddd"/>')
    for c in rows:
        col='#17856a' if c.get('close',c['low'])>=c.get('open',c['high']) else '#d64f51'
        svg.append(f'<line x1="{x(c["open_time_ms"])}" x2="{x(c["open_time_ms"])}" y1="{y(c["high"])}" y2="{y(c["low"])}" stroke="{col}"/>')
        if 'open' in c and 'close' in c:
            yy=min(y(c['open']),y(c['close']));height=max(1,abs(y(c['open'])-y(c['close'])))
            svg.append(f'<rect x="{x(c["open_time_ms"])-3}" y="{yy}" width="6" height="{height}" fill="{col}"/>')
    for kind,color in [('highs','#b82028'),('lows','#175bc1')]:
        a,b=snapshot[kind]
        svg.append(f'<line x1="{x(a["open_ms"])}" y1="{y(a["price"])}" x2="{x(b["open_ms"])}" y2="{y(b["price"])}" stroke="{color}" stroke-width="3"/>')
        for p in (a,b):svg.append(f'<circle cx="{x(p["open_ms"])}" cy="{y(p["price"])}" r="4" fill="{color}"/>')
    for i in (0,18,36,54,71):svg.append(f'<text x="{x(rows[i]["open_time_ms"])-40}" y="435" font-size="11">{brt(rows[i]["open_time_ms"])[0:5]} {brt(rows[i]["open_time_ms"])[11:16]}</text>')
    lines=[]
    for k in ('topo_1','topo_2','fundo_1','fundo_2'):
        p=snapshot[k];lines.append(f'{k}: {p["price"]:.4f} @ {brt(p["open_ms"])} BRT (candle open)')
    lines.append(f'{snapshot["highs_class"]}+{snapshot["lows_class"]}; distances top/bottom {snapshot["top_distance_hours"]}h / {snapshot["bottom_distance_hours"]}h; conflicts {snapshot["top_boundary_conflict_4h"]}/{snapshot["bottom_boundary_conflict_4h"]}')
    if previous:lines.append(f'Previous hour: {previous["label"]} -> {snapshot["label"]}')
    for i,line in enumerate(lines):svg.append(f'<text x="45" y="{458+i*21}" font-family="sans-serif" font-size="13">{escape(line)}</text>')
    svg.append('</svg>');path.write_text('\n'.join(svg),encoding='utf-8')


def plot_png(snapshot,hours,path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    rows=[r for r in hours if snapshot['window_start_ms']<=r['open_time_ms']<snapshot['window_end_ms']]
    fig,ax=plt.subplots(figsize=(13,7))
    ax.axvspan(-.5,35.5,color='#eaf2fb');ax.axvspan(35.5,71.5,color='#fff4e4');ax.axvline(35.5,color='gray',linestyle='--')
    for i,r in enumerate(rows):
        color='#16856a' if r.get('close',0)>=r.get('open',0) else '#cb444c'
        ax.plot([i,i],[r['low'],r['high']],color=color,linewidth=1)
        if 'open' in r:ax.add_patch(Rectangle((i-.28,min(r['open'],r['close'])),.56,max(abs(r['close']-r['open']),.001),facecolor=color))
    for kind,color in [('highs','#b82028'),('lows','#175bc1')]:
        a,b=snapshot[kind];xx=[(p['open_ms']-snapshot['window_start_ms'])/HOUR for p in (a,b)]
        ax.plot(xx,[a['price'],b['price']],color=color,marker='o',linewidth=2.4)
    ax.set_xticks([0,18,36,54,71],[brt(rows[i]['open_time_ms']) for i in [0,18,36,54,71]],fontsize=9)
    ax.set_title(f'{VERSION} | {brt(timestamp_ms(snapshot["at"]))} BRT | {snapshot["label"]}\nH1:36h (blue) / H2:36h (orange)');ax.set_ylabel('SOL/USDT');ax.grid(alpha=.2)
    details='\n'.join(f'{k}: {snapshot[k]["price"]:.4f} @ {brt(snapshot[k]["open_ms"])} BRT' for k in ('topo_1','topo_2','fundo_1','fundo_2'))
    details+=f'\n{snapshot["highs_class"]}+{snapshot["lows_class"]}; top/bottom distance: {snapshot["top_distance_hours"]}/{snapshot["bottom_distance_hours"]}h; conflict<4h: {snapshot["top_boundary_conflict_4h"]}/{snapshot["bottom_boundary_conflict_4h"]}'
    fig.subplots_adjust(bottom=.3);fig.text(.08,.04,details,fontsize=10);fig.savefig(path,dpi=130);plt.close(fig)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--candles',type=Path,required=True)
    p.add_argument('--replay-dir',type=Path,default=ROOT/'data/analysis/price_structure_72h_20261010')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--render-png',action='store_true');args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    buffer=load_buffer(args.candles);hours=[json.loads(l) for l in args.candles.read_text().splitlines()]
    start=timestamp_ms('2026-06-01T00:00:00-03:00');last=max(buffer.hours)+HOUR
    series=[];episode=-1;previous=None;transitions=[]
    for at in range(start,last+1,HOUR):
        snap=buffer.snapshot(at,'REPLAY')
        if previous is None or previous['label']!=snap['label']:episode+=1
        snap['episode']=episode;series.append(snap)
        if previous and previous['label']!=snap['label']:
            transferred=[k for k in ('topo_2','fundo_2') if previous.get(k) and
                         previous[k]['open_ms']<snap['midpoint_ms'] and previous[k]['open_ms']>=previous['midpoint_ms']]
            expired=[k for k in ('topo_1','fundo_1') if previous.get(k) and previous[k]['open_ms']<snap['window_start_ms']]
            transitions.append(dict(at=at,old=previous['label'],new=snap['label'],boundary_transfer=transferred,window_expiry=expired))
        previous=snap
    episode_counts=Counter(s['episode'] for s in series)
    daily={brt(timestamp_ms(s['at']))[:10]:0 for s in series}
    for t in transitions:daily[brt(t['at'])[:10]]+=1
    stability=dict(hours=len(series),transitions=len(transitions),
        transitions_by_day=daily,
        dwell_observed_hours_quantiles=quantiles(list(episode_counts.values())),
        dwell_complete_only_hours_quantiles=quantiles([n for e,n in episode_counts.items() if e not in (0,episode)]),
        dwell_by_state={state:quantiles([n for e,n in episode_counts.items() if next(s['label'] for s in series if s['episode']==e)==state]) for state in (*STATES,'UNDEFINED')},
        boundary_related_transitions=sum(bool(t['boundary_transfer']) for t in transitions),
        expiry_related_transitions=sum(bool(t['window_expiry']) for t in transitions),
        top_conflict_hours=sum(s['top_boundary_conflict_4h'] is True for s in series),
        bottom_conflict_hours=sum(s['bottom_boundary_conflict_4h'] is True for s in series),
        top_conflict_pct=100*sum(s['top_boundary_conflict_4h'] is True for s in series)/len(series),
        bottom_conflict_pct=100*sum(s['bottom_boundary_conflict_4h'] is True for s in series)/len(series),
        observations_left_right_censored=True,transition_detail=transitions)
    bytime={timestamp_ms(s['at']):s for s in series}
    def lookup(at):return bytime[at//HOUR*HOUR]
    archive=json.loads((args.replay_dir/'classified_trades.json').read_text())['COMPLETE']
    signals=json.loads((args.replay_dir/'signals.json').read_text())
    opportunities=Counter(lookup(r['boundary_ms'])['label'] for r in signals)
    results={};concordance={};classified={};input_hashes={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        results[path]={};concordance[path]={};classified[path]={}
        for arm in ('REAL_A','BE_OFF_CB_SHADOW','BE_OFF_CB_ACT20_GAP5_SHADOW','BE_OFF_CB_ACT10_GAP13_SHADOW'):
            file=args.replay_dir/f'{path}_{arm}.json';raw=file.read_bytes();input_hashes[file.name]=hashlib.sha256(raw).hexdigest()
            trades=json.loads(raw)['trades'];old={t['source_candle']:t['context'] for t in archive[path][arm]}
            rows=[];counts=Counter()
            for t in trades:
                s=lookup(t['opened_ms']);row={**t,'trend_open':s['label'],'episode':s['episode'],'rule_version':VERSION}
                rows.append(row);counts[(old[t['source_candle']],s['label'])]+=1
            results[path][arm]={state:metrics([r for r in rows if state=='TOTAL' or r['trend_open']==state],sum(opportunities.values()) if state=='TOTAL' else opportunities[state]) for state in (*STATES,'UNDEFINED','TOTAL')}
            concordance[path][arm]={a:{b:counts[(a,b)] for b in (*STATES,'UNDEFINED')} for a in ('BULL','BEAR','MIXED','UNDEFINED')}
            classified[path][arm]=rows
            if abs(results[path][arm]['TOTAL']['net']-sum(t['net_usd'] for t in trades if t['closed_ms'] is not None))>1e-8:raise ValueError('Global net changed')
            if file.read_bytes()!=raw:raise ValueError('Replay artifact mutated')
    examples=[];recent=[s for s in series if timestamp_ms('2026-10-09T00:00:00-03:00')<=timestamp_ms(s['at'])<timestamp_ms('2026-10-17T00:00:00-03:00')]
    for state in STATES:
        match=next((s for s in recent if s['label']==state),None)
        if match and match not in examples:examples.append(match)
    for kind in ('top_boundary_conflict_4h','bottom_boundary_conflict_4h'):
        match=next((s for s in recent if s[kind]),None)
        if match and match not in examples:examples.append(match)
    for t in transitions:
        match=next((s for s in recent if timestamp_ms(s['at'])==t['at']),None)
        if match and match not in examples:examples.append(match);break
    on_16=next((s for s in recent if timestamp_ms(s['at'])==timestamp_ms('2026-10-16T12:00:00-03:00')),None)
    if on_16 and on_16 not in examples:examples.append(on_16)
    for i,s in enumerate(examples):
        plot_example(s,hours,args.output/f'example_{i+1}.svg',bytime.get(timestamp_ms(s['at'])-HOUR))
        if args.render_png:plot_png(s,hours,args.output/f'example_{i+1}.png')
    manifest=dict(rule_version=VERSION,candles_sha256=hashlib.sha256(args.candles.read_bytes()).hexdigest(),
        replay_sha256=input_hashes,replay_end=json.loads((args.replay_dir/'market_manifest.json').read_text())['end_brt'],
        stability_end=brt(last),economic_replay_rerun=False,economic_invariance='exact original trades/prices/costs; aggregate tolerance1e-8',
        opportunity_timestamp='boundary_ms, not historical signal.ts runner wall-clock',old_labels='archived study only')
    for name,value in [('manifest',manifest),('stability',stability),('concordance',concordance),('economics',results),('classified_replay',classified),('hourly',series),('examples',examples)]:
        (args.output/f'{name}.json').write_text(json.dumps(value,indent=2),encoding='utf-8')
    lines=['# TREND_72H_2X36H_V1 — exploração, não OOS',
        'Extremos das duas metades36h; empates exatosUNDEFINED; desempate intrametade pelo candle mais antigo. Sem k/fractal/indicadores. Conflito<4h é somente telemetria.',
        f'Replay congelado: 01/06 até {manifest["replay_end"]}. Apenas reclassificação, nenhuma nova simulação/otimização. Estabilidade hora a hora até {manifest["stability_end"]} BRT. Custos congelados:20USDT, fees0,20%, spread5bps.',
        'Oportunidades são sinais REAL_A aprovados já armazenados, não todos os candles nem oportunidades exclusivas de outro braço. Usado boundary_ms causal, pois signal.ts do artefato reflete wall-clock da execução histórica.',
        '## Estabilidade',json.dumps({k:v for k,v in stability.items() if k!='transition_detail'},ensure_ascii=False,indent=2),
        'Permanências inicial/final censuradas: quantis de duração observada, não estimativa de duração completa. Relação com fronteira/expiração é coincidência geométrica documentada, não afirmação de causa única.',
        '## Controle BE_OFF_CB por tendência de abertura',
        table(['path','trend_open','opportunities','entries','closed','gross','fees','spread','net','net/trade','PF','HS','HS%','episodes'],
          [[path,state,*[f(results[path]['BE_OFF_CB_SHADOW'][state][k]) for k in ('opportunities','entries','N','gross','fees','spread','net','net_trade','PF','HS','HS_pct','episodes')]] for path in results for state in (*STATES,'UNDEFINED','TOTAL')]),
        '## Controle: distribuição temporal e concentração',
        table(['path','state','month','N','net'],[[path,state,month,v['N'],f(v['net'])] for path in results for state in STATES for month,v in results[path]['BE_OFF_CB_SHADOW'][state]['monthly'].items()]),
        table(['path','state','episodes for50% absolute net','episodes for80%'],[[path,state,m['episode_absolute_concentration']['0.5'],m['episode_absolute_concentration']['0.8']] for path in results for state,m in results[path]['BE_OFF_CB_SHADOW'].items()]),
        '## Comparadores factuais: delta net vs controle (amostras de entradas podem divergir)',
        table(['path','arm','state','N','net','delta vs control'],[[path,arm,state,m['N'],f(m['net']),f(m['net']-results[path]['BE_OFF_CB_SHADOW'][state]['net'])] for path,arms in results.items() for arm,states in arms.items() for state,m in states.items() if arm!='BE_OFF_CB_SHADOW']),
        'Os resultados por período, composição de saídas e concentração absoluta por episódios estão em economics.json. Concentração usa soma dos valores absolutos dos nets por episódio, não porcentagem de um net quase zero.',
        '## Matriz pontual de concordância (único artefato comparativo antigo×novo)']
    for path,arms in concordance.items():
        for arm,m in arms.items():
            lines += [f'### {path} / {arm}',table(['old \\ new',*STATES,'UNDEFINED'],[[old,*[counts[x] for x in (*STATES,'UNDEFINED')]] for old,counts in m.items()])]
    old_summary=json.loads((args.replay_dir/'summary.json').read_text())['COMPLETE']
    lines+=['## Dependência da definição: deltas condicionais antigos e novos',
        table(['path','arm','label name','old delta','new delta'],[[path,arm,state,
            f(old_summary[path]['comparisons'][arm][state]['delta']),
            f(results[path][arm][state]['net']-results[path]['BE_OFF_CB_SHADOW'][state]['net'])]
            for path in results for arm in results[path] if arm!='BE_OFF_CB_SHADOW' for state in ('BULL','BEAR')]),
        'BULL/BEAR mantêm nomes mas não as populações. Nos dois paths, o sinal dos deltas factuais destes comparadores em BULL/BEAR permanece igual; a magnitude muda. Isso não transfere os ICs nem a conclusão de consistência do estudo anterior. EXPA/COMP não têm equivalentes antigos individuais.']
    lines+=['## Leitura científica inicial',
        'Sim, há diferenças descritivas nesta amostra: BULL tem HS perto de32,05% e COMP21,54% nos dois paths; o net/trade de BULL é −0,0574/−0,0485 e de COMP−0,0178/−0,0079 (HIGH/LOW). Isso não é uma seleção de melhor estado: os quatro estados têm net agregado negativo nos dois paths. EXPA alterna net mensal positivo em junho/agosto e negativo em julho/setembro/outubro; COMP só tem mês positivo em setembro. Portanto não há separação econômica estável demonstrada em todos os períodos.',
        'A concentração também limita leitura: em BEAR quatro episódios explicam50% da soma dos nets absolutos por episódio, nos dois paths. Não se pode atribuir causalidade nem poder preditivo suficiente à classificação. Não houve teste de thresholds nem escolha de rótulo por retorno.',
        'Conclusões globais econômicas dos comparadores permanecem exatamente iguais, pois os trades não mudaram. Conclusões condicionadas aos rótulos antigos não são automaticamente transferíveis: comparar a redistribuição na matriz e os novos deltas factuais. Critérios/IC do estudo antigo não foram reaproveitados como validação para os novos grupos.',
        'Não há validação OOS independente: histórico usado para definir/investigar a regra é exploratório. Reservar observações futuras para confirmação. Sem indicação de filtro, sizing ou preferência por braço.',
        '## Exemplos visuais (horários dos candles, não do extremo intrahora)']
    for i,s in enumerate(examples):lines.append(f'![{brt(timestamp_ms(s["at"]))} {s["label"]}](example_{i+1}.svg)')
    (args.output/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    print(json.dumps(dict(transitions=stability['transitions'],examples=len(examples),rule=VERSION,control={p:{s:(m['N'],m['net'],m['PF']) for s,m in results[p]['BE_OFF_CB_SHADOW'].items()} for p in results}),indent=2))


if __name__=='__main__':main()
