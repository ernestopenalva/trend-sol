"""Entrega A: structural diagnostics only. Never evaluates financial results."""
import argparse
import json
import sys
from collections import Counter,defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.monitor.price_structure import WAVE,TIDE,STATES,HOUR,timestamp_ms,iso
from tools.price_structure_backfill import load_buffer,DEFAULT_SINCE
from tools.trend_structure_migrate import file_digest,files,read_rows,build_index

ZONE=ZoneInfo('America/Sao_Paulo');VALID=set(STATES);Q=WAVE.interval_ms


def brt(ms):return datetime.fromtimestamp(ms/1000,ZONE).strftime('%d/%m/%Y %H:%M')


def quantiles(values):
    xs=sorted(values)
    def q(p):
        x=(len(xs)-1)*p;i=int(x)
        return xs[i]+(xs[min(i+1,len(xs)-1)]-xs[i])*(x-i) if xs else None
    return dict(N=len(xs),p10=q(.1),p25=q(.25),median=q(.5),p75=q(.75),p90=q(.9),max=max(xs) if xs else None)


def timeline(buffer,start,end,source='REPLAY'):
    series=[];transitions=[];episodes=[];previous=None
    for at in range(start,end+1,Q):
        s=buffer.snapshot(at,source)
        if previous is None or s['label']!=previous['label']:
            if episodes:episodes[-1]['end_ms']=at;episodes[-1]['right_censored']=False
            episodes.append(dict(label=s['label'],start_ms=at,end_ms=end,
                                 left_censored=previous is None,right_censored=True))
            if previous:
                transfer=[key for key in ('topo_2','fundo_2') if previous.get(key)
                          and previous['midpoint_ms']<=previous[key]['open_ms']<s['midpoint_ms']]
                expiry=[key for key in ('topo_1','fundo_1') if previous.get(key) and previous[key]['open_ms']<s['window_start_ms']]
                transitions.append(dict(at_ms=at,old=previous['label'],new=s['label'],
                                        midpoint_transfer=transfer,window_expiry=expiry))
        series.append(s);previous=s
    return series,transitions,episodes


def trade_stability(index,tide,wave,cut,end):
    # Only identity/timestamps are projected by build_index; no economic inputs.
    out=[];groups=defaultdict(list);open_count=unavailable=outside=0;invalid_reasons=Counter();still_open=closed_after_cut=0
    for pair,r in sorted(index.items()):
        opened=timestamp_ms(r.get('opened_at') or r.get('open_ts'))
        close=r.get('closed_at') or r.get('close_ts')
        closed=timestamp_ms(close) if close else None
        if opened<cut and closed is not None and closed<cut:continue
        if opened>end:outside+=1;continue
        if closed is None or closed>end:
            open_count+=1;still_open+=closed is None;closed_after_cut+=closed is not None;continue
        values={}
        for prefix,b in [('trend',tide),('wave',wave)]:
            a=b.snapshot(opened,'BACKFILL');z=b.snapshot(closed,'BACKFILL')
            valid=a['label'] in VALID and z['label'] in VALID
            # Count intermediate changes too: equal endpoints need not mean no transitions.
            labels=[a['label']]+[b.snapshot(t,'BACKFILL')['label'] for t in range((opened//Q+1)*Q,closed+1,Q)]
            changes=sum(x!=y for x,y in zip(labels,labels[1:]))
            values[prefix]=dict(open=a['label'],close=z['label'],valid=valid,
                                same=a['label']==z['label'] if valid else None,transitions=changes)
            for phase,s in [('open',a),('close',z)]:
                if s['label'] not in VALID:invalid_reasons[f"{prefix}_{phase}:{s['label']}:{s.get('undefined_reason')}"]+=1
        age=(closed-opened)/60_000
        bucket='<1h' if age<60 else '1–3h' if age<180 else '3–6h' if age<360 else '>=6h'
        row=dict(pair_id=pair,opened_ms=opened,closed_ms=closed,duration_minutes=age,bucket=bucket,**values)
        out.append(row);groups[bucket].append(row)
        unavailable+=not (values['trend']['valid'] and values['wave']['valid'])
    def summarize(rows):
        ans=dict(N=len(rows),duration_minutes=quantiles([r['duration_minutes'] for r in rows]))
        for layer in ('trend','wave'):
            good=[r[layer] for r in rows if r[layer]['valid']]
            ans[layer]=dict(valid=len(good),invalid=len(rows)-len(good),same=sum(r['same'] for r in good),
                same_pct=100*sum(r['same'] for r in good)/len(good) if good else None,
                transitions=quantiles([r['transitions'] for r in good]))
        return ans
    return out,dict(closed=summarize(out),by_duration={k:summarize(groups[k]) for k in ('<1h','1–3h','3–6h','>=6h')},
                    unique_open_close_intervals=len({(r['opened_ms'],r['closed_ms']) for r in out}),
                    actually_open=still_open,closed_after_cut=closed_after_cut,
                    open_or_right_censored=open_count,invalid_pair=unavailable,invalid_reasons=dict(invalid_reasons),outside_cut=outside)


def plot_example(tide,wave,hour_rows,quarter_rows,path,reason,previous=None):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    fig,axes=plt.subplots(2,1,figsize=(14,11))
    for ax,s,rows,rule,title in [(axes[0],tide,hour_rows,TIDE,'MARE 72h / 1h'),(axes[1],wave,quarter_rows,WAVE,'ONDA 12h / 15m')]:
        cs=[r for r in rows if s['window_start_ms']<=r['open_time_ms']<s['window_end_ms']]
        n=rule.count;mid=n/2-.5
        ax.axvspan(-.5,mid,color='#edf3fc');ax.axvspan(mid,n-.5,color='#fff1df')
        ax.axvline(mid,color='gray',linestyle='--')
        for i,r in enumerate(cs):
            col='#12856a' if r['close']>=r['open'] else '#cf424b'
            ax.plot([i,i],[r['low'],r['high']],color=col,linewidth=1)
            ax.add_patch(Rectangle((i-.3,min(r['open'],r['close'])),.6,max(.002,abs(r['close']-r['open'])),facecolor=col))
        for key,col in [('highs','#ac2431'),('lows','#185ca5')]:
            pts=s[key];xx=[(p['open_ms']-s['window_start_ms'])/rule.interval_ms for p in pts]
            ax.plot(xx,[p['price'] for p in pts],marker='o',color=col,linewidth=2)
            for x,p in zip(xx,pts):
                ax.annotate(f"{p['price']:.2f}\n{brt(p['open_ms'])}",(x,p['price']),xytext=(0,12 if key=='highs' else -30),textcoords='offset points',ha='center',fontsize=7)
        ticks=[0,n//4,n//2,3*n//4,n-1]
        ax.set_xticks(ticks,[brt(cs[i]['open_time_ms']) for i in ticks],fontsize=8)
        ax.set_title(f"{title}: {s['label']} | {s['highs_class']} + {s['lows_class']}")
        ax.margins(y=.20)
        ax.set_ylabel('SOL/USDT');ax.grid(alpha=.2)
    detail=f"ONDA distance highs/lows: {wave['top_distance_minutes']:.0f}/{wave['bottom_distance_minutes']:.0f} min; conflict <45m: {wave['top_boundary_conflict_45m']}/{wave['bottom_boundary_conflict_45m']}"
    if previous:detail+=f"; prior quarter: {previous['label']} -> {wave['label']}"
    fig.suptitle(f"{brt(timestamp_ms(wave['at']))} BRT | {reason}\nCausal closed candles; labels are geometry, not forecasts",fontsize=13)
    fig.text(.06,.015,detail,fontsize=10);fig.tight_layout(rect=(0,.04,1,.94));fig.savefig(path,dpi=125);plt.close(fig)


def table(headers,rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+
                     ['| '+' | '.join(str(x) for x in row)+' |' for row in rows])


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--hourly',type=Path,required=True);p.add_argument('--quarters',type=Path,required=True)
    p.add_argument('--trade-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--render',action='store_true');a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    tide=load_buffer(a.hourly);wave=load_buffer(a.quarters,WAVE)
    end=min(max(tide.hours)+HOUR,max(wave.hours)+Q);start=timestamp_ms('2026-06-01T00:00:00-03:00')
    series,transitions,episodes=timeline(wave,start,end)
    counts=Counter(s['label'] for s in series);daily=Counter({brt(timestamp_ms(s['at']))[:10]:0 for s in series})
    for t in transitions:daily[brt(t['at_ms'])[:10]]+=1
    complete=[e for e in episodes if not e['left_censored'] and not e['right_censored']]
    observed=[(e['end_ms']-e['start_ms'])/HOUR for e in episodes]
    dwell=[(e['end_ms']-e['start_ms'])/HOUR for e in complete]
    matrix=Counter();tide_counts=Counter()
    for s in series:
        t=tide.snapshot(s['at'],'REPLAY');matrix[(t['label'],s['label'])]+=1;tide_counts[t['label']]+=1
    labels=(*STATES,'UNDEFINED','UNAVAILABLE')
    rows=[(s,counts[s],round(100*counts[s]/len(series),3)) for s in labels]
    matrix_dict={t:{w:matrix[t,w] for w in labels} for t in labels}
    conditional={t:{w:100*matrix[t,w]/tide_counts[t] if tide_counts[t] else None for w in labels} for t in labels}
    stability=dict(observations=len(series),start_brt=brt(start),end_brt=brt(end),counts=dict(counts),
        transitions=len(transitions),transitions_by_day=dict(daily),transitions_per_day=quantiles(list(daily.values())),
        observed_dwell_hours=quantiles(observed),complete_dwell_hours=quantiles(dwell),
        dwell_by_state={state:quantiles([(e['end_ms']-e['start_ms'])/HOUR for e in complete if e['label']==state]) for state in labels},
        boundary_related=sum(bool(t['midpoint_transfer']) for t in transitions),expiry_related=sum(bool(t['window_expiry']) for t in transitions),
        conflict_top=sum(s.get('top_boundary_conflict_45m') is True for s in series),
        conflict_bottom=sum(s.get('bottom_boundary_conflict_45m') is True for s in series),
        undefined_pct=100*counts['UNDEFINED']/len(series),unavailable_pct=100*counts['UNAVAILABLE']/len(series),
        partial_days='first/last day may be incomplete; dwell edges censored; 15m sampling')
    sourcefiles=files(a.trade_root)
    hashes={str(p):file_digest(p) for p in sourcefiles}
    index=build_index(doc for path in sourcefiles for _,doc in read_rows(path))
    trades,trade_summary=trade_stability(index,tide,wave,timestamp_ms(DEFAULT_SINCE),end)
    # Select first recent matching geometry, never by trade outcome.
    recent=[s for s in series if timestamp_ms(s['at'])>=timestamp_ms('2026-10-09T00:00:00-03:00')]
    examples=[]
    def choose(reason,predicate):
        for s in recent:
            t=tide.snapshot(s['at'],'REPLAY')
            if s['label'] in VALID and t['label'] in VALID and predicate(t,s):
                examples.append(dict(reason=reason,tide=t,wave=s));return
    choose('MARE BEAR / ONDA BULL',lambda t,w:t['label']=='BEAR' and w['label']=='BULL')
    choose('MARE BULL / ONDA BEAR',lambda t,w:t['label']=='BULL' and w['label']=='BEAR')
    for state in ('EXPA','COMP'):choose('ONDA '+state,lambda t,w,state=state:w['label']==state)
    choose('EXTREMES NEAR MIDPOINT',lambda t,w:w['top_boundary_conflict_45m'] or w['bottom_boundary_conflict_45m'])
    times={t['at_ms'] for t in transitions};choose('ONDA TRANSITION',lambda t,w:timestamp_ms(w['at']) in times)
    if a.render:
        hours=[json.loads(l) for l in a.hourly.read_text().splitlines() if l.strip()]
        quarters=[json.loads(l) for l in a.quarters.read_text().splitlines() if l.strip()]
        for i,e in enumerate(examples):plot_example(e['tide'],e['wave'],hours,quarters,a.output/f'example_{i+1}.png',e['reason'],wave.snapshot(timestamp_ms(e['wave']['at'])-Q,'REPLAY'))
    manifest=dict(rule=WAVE.version,tide_rule=TIDE.version,hourly_sha256=file_digest(a.hourly),quarters_sha256=file_digest(a.quarters),
        trade_source_sha256=hashes,cutoff_brt=brt(end),trade_source=str(a.trade_root),
        trade_selection='SOLUSDT unique pair identities, since08Oct23:52 including carryovers; actual open/close timestamps',
        no_financial_features=True,no_economic_analysis=True,no_replay_engine_run=True,
        historical_availability='closed boundary assumed; actual historical receipt latency is not inferred')
    for name,value in [('manifest',manifest),('stability',stability),('concordance',dict(counts=matrix_dict,conditional_percent=conditional)),
                       ('transitions',transitions),('episodes',episodes),('trade_stability',trade_summary),('trade_timestamps',trades),('examples',examples)]:
        (a.output/f'{name}.json').write_text(json.dumps(value,indent=2),encoding='utf-8')
    with (a.output/'wave_snapshots.jsonl').open('x',encoding='utf-8') as f:
        for s in series:f.write(json.dumps(s)+'\n')
    lines=['# Entrega A — ONDA 12h: diagnóstico estrutural, sem PnL',
        f'Histórico: {brt(start)} → {brt(end)} BRT. {len(series)} avaliações coincidentes a cada15m. Último candle15m: {brt(end-Q)}; último1h: {brt(end-HOUR)}. Não foi executado replay econômico.',
        'MARÉ permanece TREND_72H_2X36H_V1. ONDA usa48 candles15m fechados,24+24; extremos reais, empate intrametade mais antigo; igualdade entre metades=UNDEFINED. Gap/atraso=UNAVAILABLE, sem janela deslocada. Conflito<45m é somente telemetria.',
        '## A. Comportamento da ONDA',table(['estado','N','%'],rows),
        f"Transições: {len(transitions)}. Por dia: {json.dumps(stability['transitions_per_day'])}. Dias inicial/final parciais.",
        f'Permanência observada (h): {json.dumps(stability["observed_dwell_hours"])}. Episódios completos (h): {json.dumps(stability["complete_dwell_hours"])}. Primeiro/último censurados; resolução15m.',
        f"Transições associadas à passagem de extremo pela metade: {stability['boundary_related']}; à expiração da janela: {stability['expiry_related']}. Categorias podem se sobrepor, não provam causa única.",
        f"Conflitos de topo: {stability['conflict_top']} ({100*stability['conflict_top']/len(series):.3f}%); fundo: {stability['conflict_bottom']} ({100*stability['conflict_bottom']/len(series):.3f}%).",
        '## B. MARÉ × ONDA',table(['MARÉ \\ ONDA',*labels],[[t,*[matrix[t,w] for w in labels]] for t in labels]),
        'Distribuição da ONDA dentro de cada MARÉ (%)',table(['MARÉ \\ ONDA',*labels],[[t,*['N/A' if conditional[t][w] is None else f'{conditional[t][w]:.2f}' for w in labels]] for t in labels]),
        'As duas escalas podem divergir dentro da mesma MARÉ: recuperação curta dentro de estrutura BEAR, retração curta dentro de BULL, ampliação EXPA ou compressão COMP. São leituras geométricas descritivas; discordância não demonstra informação preditiva nem valor econômico.',
        '## C. Estabilidade durante trades',
        f"Fonte: cópia local {a.trade_root}; marco08/10 23:52 BRT, carryovers incluídos. Sem filtro de saída/PnL. Ainda abertos na cópia: {trade_summary['actually_open']}; fechados depois do corte (censurados): {trade_summary['closed_after_cut']}; pares inválidos: {trade_summary['invalid_pair']}. Motivos: {json.dumps(trade_summary['invalid_reasons'])}.",
        table(['duração','N','MARÉ válidos','MARÉ igual %','ONDA válidos','ONDA igual %','ONDA trocas mediana'],
              [[bucket,x['N'],x['trend']['valid'],x['trend']['same_pct'],x['wave']['valid'],x['wave']['same_pct'],x['wave']['transitions']['median']] for bucket,x in [('TOTAL',trade_summary['closed']),*trade_summary['by_duration'].items()]]),
        'Igualdade nos extremos não significa estabilidade intratrade: a contagem intermediária de mudanças está no JSON. Nenhuma estabilidade mínima é critério de aprovação. A amostra de trades forward é apenas outubro; não representa toda a história de mercado desde junho.',
        f"As posições incluem braços simultâneos e não são observações independentes: {trade_summary['unique_open_close_intervals']} intervalos distintos de abertura/fechamento. As porcentagens acima são por posição, não significância estatística.",
        '## D. Exemplos causais',
        'Selecionados pelo primeiro caso geométrico recente de cada categoria, sem observar resultado financeiro. Horários dos extremos são aberturas de candles, não instante intrabar da máxima/mínima.']
    for i,e in enumerate(examples):lines.append(f"### {e['reason']} — {brt(timestamp_ms(e['wave']['at']))} BRT\n\n![{e['reason']}](example_{i+1}.png)")
    lines+=['## Limitações e conclusão',
        'ONDA implementada como segunda escala geométrica, não como gate. Não se conclui vantagem econômica, previsão de continuação ou necessidade de qualquer mudança. Não executada Entrega B.',
        'BACKFILL/REPLAY assumem disponibilidade no fechamento público; não reconstroem a latência histórica do processo. LIVE registra primeira disponibilidade no cache e não usa um candle recebido depois do instante consultado. Gaps permanecem explícitos até os48 slots requeridos existirem; não há reparo de rede síncrono nos trades.',
        'Dados de duração/episódios e transições diárias completos em stability.json, episodes.json e transitions.json; evidências dos quatro extremos em examples.json e wave_snapshots.jsonl. Nenhum arquivo de origem é modificado.']
    (a.output/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    if hashes!={str(p):file_digest(p) for p in sourcefiles}:raise ValueError('Study inputs changed')
    print(json.dumps(dict(stability=stability,trade_stability=trade_summary,examples=len(examples)),indent=2))


if __name__=='__main__':main()
