"""Frozen price-label diagnostics. No trading policy or production DB access."""
import json,sys,bisect,random,math
from pathlib import Path
from collections import Counter,defaultdict
from datetime import datetime,timedelta,timezone
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.price_structure_study import OUT,START,OLD_END,ARMS,label,g,a
from tools.friction_pl_trail_study import accounting
from tools.feed_trail_revalidation import quantile
LABELS=('BULL','BEAR','MIXED','UNDEFINED','TOTAL');ZONE=timezone(timedelta(hours=-3))

def context_series(hours,k):
    rows=[];previous=None;episode=-1
    for i,c in enumerate(hours):
        window=hours[max(0,i-71):i+1];name,hi,lo=label(window,k)
        if name!=previous:episode+=1
        rows.append(dict(available_ms=c.boundary_ms,label=name,episode=episode,
            window_start_ms=window[0].open_time_ms,latest_closed_ms=c.close_time_ms,
            tops=[dict(open_ms=window[j].open_time_ms,value=window[j].high,confirmed_ms=window[j+k].close_time_ms) for j in hi],
            bottoms=[dict(open_ms=window[j].open_time_ms,value=window[j].low,confirmed_ms=window[j+k].close_time_ms) for j in lo]))
        previous=name
    return rows

def at_entry(series,boundaries,at):
    i=bisect.bisect_right(boundaries,at)-1
    if i<0:return dict(label='UNDEFINED',episode=-1)
    value=series[i]
    assert value['latest_closed_ms']<at
    assert all(p['confirmed_ms']<at for p in value['tops']+value['bottoms'])
    return value

def scoped(trades,series,end):
    times=[s['available_ms'] for s in series];out=[]
    for t in trades:
        if not START<=t['opened_ms']<=end:continue
        closed=t['closed_ms'] is not None and t['closed_ms']<=end
        context=at_entry(series,times,t['opened_ms'])
        row={**t,'resolved':closed,'context':context['label'],'episode':context['episode'],
            'entry_day':datetime.fromtimestamp(t['opened_ms']/1000,ZONE).strftime('%Y-%m-%d')}
        if not closed:
            row.update(closed_ms=None,net_usd=None,exit_reason='OPEN')
            # A later resolved trade is censored at the old cutoff. Do not
            # expose its eventual prices/peaks as if known inside that window.
            for field in ['exit_price','gross_pct','net_pct','peak_price','trough_price']:
                row.pop(field,None)
        out.append(row)
    return out

def metrics(rows):
    closed=sorted([t for t in rows if t['resolved']],key=lambda t:(t['closed_ms'],t['source_candle']))
    values=[t['net_usd'] for t in closed];gains=sum(max(0,v) for v in values);loss=-sum(min(0,v) for v in values)
    equity=peak=dd=0.
    for v in values:equity+=v;peak=max(peak,equity);dd=max(dd,peak-equity)
    costs=[accounting(t,20,5) for t in closed]
    return dict(N=len(closed),entries=len(rows),open=len(rows)-len(closed),net=sum(values),
        net_trade=sum(values)/len(values) if values else None,PF=gains/loss if loss else math.inf if gains else None,
        DD_illustrative=dd if closed else None,exits=dict(Counter(t['exit_reason'] for t in closed)),
        gross=sum(c['gross'] for c in costs),fees=sum(c['fees'] for c in costs),spread=sum(c['spread'] for c in costs),
        episodes=len({t['episode'] for t in rows}))

def contrast(left,right,end):
    ls={t['source_candle']:t for t in left};rs={t['source_candle']:t for t in right}
    common=set(ls)&set(rs);paired=[]
    for source in sorted(common):
        c=ls[source];v=rs[source];assert c['opened_ms']==v['opened_ms'] and c['context']==v['context']
        paired.append(dict(source_candle=source,opened_ms=c['opened_ms'],resolved=c['resolved'] and v['resolved'],
            comparator_reason=c['exit_reason'],arm_reason=v['exit_reason'],
            delta=v['net_usd']-c['net_usd'] if c['resolved'] and v['resolved'] else None))
    cursor=datetime.fromtimestamp(START/1000,ZONE).date();last=datetime.fromtimestamp(end/1000,ZONE).date();days=[]
    while cursor<=last:days.append(str(cursor));cursor+=timedelta(days=1)
    daily={d:0. for d in days};episodes=defaultdict(float)
    midpoint=(START+end)//2;halves=[0.,0.]
    for sign,rows in [(-1,left),(1,right)]:
        for t in rows:
            if not t['resolved']:continue
            amount=sign*t['net_usd'];daily[t['entry_day']]+=amount;episodes[t['episode']]+=amount
            halves[int(t['opened_ms']>=midpoint)]+=amount
    rng=random.Random(20261010);samples=[];n=math.ceil(len(days)/7)
    # Last sampled block may be truncated; preserve the exact sample horizon.
    dayblocks=[[daily[d] for d in days[i:i+7]] for i in range(len(days)-6)]
    for _ in range(5000):samples.append(sum([v for block in rng.choices(dayblocks,k=n) for v in block][:len(days)]))
    delta=sum(daily.values());direction=1 if delta>=0 else -1
    positive=sorted([direction*v for v in episodes.values() if direction*v>0],reverse=True)
    concentration={str(f):next((n for n in range(1,len(positive)+1) if sum(positive[:n])>=f*abs(delta)),None) if abs(delta)>1e-12 else None for f in [.5,.8]}
    resolved=[p for p in paired if p['resolved']]
    return dict(delta=delta,ci95=[quantile(samples,.025),quantile(samples,.975)],half_deltas=halves,
        overlap=dict(common=len(common),arm_only=len(set(rs)-common),baseline_only=len(set(ls)-common)),
        common_resolved=len(resolved),common_censored=len(common)-len(resolved),common_delta=sum(p['delta'] for p in resolved),
        pairs=paired,episode_concentration=concentration,episode_contributions=dict(episodes))

def main():
    end=json.loads((OUT/'market_manifest.json').read_text())['end_ms'];hours=g.load_candle_cache(OUT/'market/SOLUSDT_1h.jsonl')
    series={k:context_series(hours,k) for k in [2,3,4]}
    (OUT/'context_hourly.json').write_text(json.dumps(series),encoding='utf-8')
    runs={p:{arm:json.loads((OUT/f'{p}_{arm}.json').read_text()) for arm in ARMS} for p in g.PATHS}
    result={};pairs_export={};rows_export={}
    for window,cut in [('COMPLETE',end),('THROUGH_02OCT',OLD_END)]:
        result[window]={};pairs_export[window]={};rows_export[window]={}
        for path in g.PATHS:
            rows={arm:scoped(runs[path][arm]['trades'],series[3],cut) for arm in ARMS};rows_export[window][path]=rows
            cells={arm:{name:metrics([t for t in rows[arm] if name=='TOTAL' or t['context']==name]) for name in LABELS} for arm in ARMS}
            comparisons={arm:{name:contrast([t for t in rows['BE_OFF_CB_SHADOW'] if name=='TOTAL' or t['context']==name],
                [t for t in rows[arm] if name=='TOTAL' or t['context']==name],cut) for name in LABELS} for arm in ARMS if arm!='BE_OFF_CB_SHADOW'}
            pairs_export[window][path]={arm:{name:c.pop('pairs') for name,c in cs.items()} for arm,cs in comparisons.items()}
            sensitivity={k:{arm:{name:metrics([t for t in scoped(runs[path][arm]['trades'],series[k],cut) if name=='TOTAL' or t['context']==name]) for name in LABELS} for arm in ARMS} for k in [2,4]}
            result[window][path]=dict(cells=cells,comparisons=comparisons,sensitivity=sensitivity)
        consistency={}
        for arm in ARMS:
            if arm=='BE_OFF_CB_SHADOW':continue
            consistency[arm]={}
            for name in LABELS:
                cs=[result[window][p]['comparisons'][arm][name] for p in g.PATHS]
                concordant=all(c['delta']>0 for c in cs) or all(c['delta']<0 for c in cs)
                excludes=all(c['ci95'][0]>0 if c['delta']>0 else c['ci95'][1]<0 if c['delta']<0 else False for c in cs)
                halves=all(all(h*c['delta']>0 for h in c['half_deltas']) for c in cs)
                consistency[arm][name]=dict(concordant=concordant,ci_excludes_zero_both=excludes,half_stability_both=halves,
                    consistently_better=concordant and excludes and halves and all(c['delta']>0 for c in cs),
                    consistently_worse=concordant and excludes and halves and all(c['delta']<0 for c in cs))
        result[window]['consistency']=consistency
    for name,data in [('summary',result),('paired_trades',pairs_export),('classified_trades',rows_export)]:
        (OUT/f'{name}.json').write_text(json.dumps(g.safe_json(data),indent=2),encoding='utf-8')
    report(result,end)
    print(json.dumps({w:r['consistency'] for w,r in result.items()}),flush=True)

def report(result,end):
    fmt=g.fmt;lines=['# Estrutura de preço dos 3 dias anteriores × braço',
        'Primário k3; 72 candles1h completos, pivôs estritos e confirmação causal. Janela 01/06/2026 → '+a.brt(end)+'. Custo congelado fees0,20%, spread5bps, notional20USDT. Nenhum indicador no rótulo. Baseline comparador factual. REAL_A é replay OHLC, não reprodução Testnet.',
        'N=fechados resolvidos; OPEN separado. Net/trade e PF usam somente resolvidos até o corte. DD ilustrativo é a curva realizada do subconjunto, não DD do portfólio. Episódios conta segmentos horários com entradas. Metades definidas por entrada e midpoint temporal de cada janela; bootstrap moving blocks7dias,5000replicações,seed20261010. Sem correção de multiplicidade: resultados exploratórios. Nenhuma regra ou winner automático.']
    for window,r in result.items():
        cut=end if window=='COMPLETE' else OLD_END
        lines += ['## '+window,'Corte: '+a.brt(cut)+'; divisão temporal: '+a.brt((START+cut)//2)+'.']
        for path in g.PATHS:
            v=r[path];lines += ['### '+path,g.table(['arm','context','N','OPEN','net','net/trade','PF','DD ilustrativo','gross','fees','spread','episódios','exits'],
                [[arm,name,x['N'],x['open'],fmt(x['net']),fmt(x['net_trade']),fmt(x['PF']),fmt(x['DD_illustrative']),fmt(x['gross']),fmt(x['fees']),fmt(x['spread']),x['episodes'],json.dumps(x['exits'])] for arm,cells in v['cells'].items() for name,x in cells.items()]),
                g.table(['arm','context','delta vsBE_OFF_CB','IC95','1ª metade','2ª metade','pares resolvidos','delta pares','common/arm-only/base-only','episódios50/80'],
                [[arm,name,fmt(x['delta']),str(x['ci95']),*[fmt(h) for h in x['half_deltas']],x['common_resolved'],fmt(x['common_delta']),str(x['overlap']),str(x['episode_concentration'])] for arm,cs in v['comparisons'].items() for name,x in cs.items()])]
        lines += ['### Critério conjunto — ambos os paths',g.table(['arm','context','sinal concordante','IC exclui0 ambos','metades concordantes','melhor consistente','pior consistente'],
            [[arm,name,*x.values()] for arm,cells in r['consistency'].items() for name,x in cells.items()])]
    lines += ['## Sensibilidade k2/k4 — somente tabelas, sem seleção',g.table(['janela','path','k','arm','context','N','net','net/trade','episódios'],
        [[window,path,k,arm,name,x['N'],fmt(x['net']),fmt(x['net_trade']),x['episodes']] for window,r in result.items() for path in g.PATHS for k,arms in r[path]['sensitivity'].items() for arm,cells in arms.items() for name,x in cells.items()]),
        '## Síntese — não é escolha de winner',
        'ACT20_GAP5 e ACT10_GAP13 têm deltas pontuais positivos em BULL/BEAR/MIXED, mas nenhum contexto satisfaz os três critérios juntos. Os IC95% por contexto incluem zero nos dois paths. No TOTAL, o IC HIGH exclui zero e o LOW não; não há confirmação conjunta. Os gains são concentrados: em MIXED, um único episódio explica50%/80% do saldo ACT20 nos dois paths; GAP13 depende de1–2 episódios para80% em MIXED/BEAR e2–3 em BULL. Compensação entre episódios torna pequenos esses números de concentração; não prova mecanismo causal.',
        'REAL_A tem delta negativo consistente em BULL e MIXED, e no TOTAL; BEAR tem ICs incluindo zero. Em BULL,5/9 episódios explicam50%/80% da desvantagem nos dois paths; em MIXED,11/23 HIGH e7/14 LOW. O TOTAL−52,6159/−40,5563 é muito maior que o delta common-only−6,4988/−4,0929: boa parte decorre de admissões/volume diferentes, não apenas destino dos mesmos trades. Isso não valida automaticamente BE_OFF, CB, nem outra arquitetura.',
        'A sensibilidade até02/10 mantém os mesmos vereditos conjuntos. A extensão reduz o delta agregado ACT20 de+10,5997/+8,9157 para+10,1921/+8,5514; GAP13 de+16,0916/+11,0277 para+15,1063/+9,9307. Todos os braços têm net negativo e PF<1 no TOTAL. UNDEFINED teve zero entradas na amostra primária, permanecendo explicitamente N0/N/A. Sensibilidades k2/k4 não modificam nem validam o primário.',
        '## Limites','ACT20/GAP13 são mudanças de saída, mas slots/CB/admissões podem divergir sistemicamente. Pares por source estão em paired_trades.json; sources exclusivos não são apagados do delta total. Censura na janela antiga impede usar suas saídas posteriores. A extensão também desloca o midpoint temporal usado no teste das metades: uma mudança de classificação pode decorrer disso, além dos novos trades. REAL_A não tem CB e mantém BE: contraste é entre máquinas completas, não efeito isolado. Forward secundário em arquivo separado; não certifica superioridade. Sensibilidades não escolhem k. Nenhuma mudança operacional.']
    (OUT/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')

if __name__=='__main__':main()
