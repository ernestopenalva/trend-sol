"""Read frozen LADDER_V2 runs; bootstrap accounting, not trading simulation."""
import json, random, math, sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from collections import Counter
from datetime import datetime,timedelta,timezone
from tools.ladder_v2_study import OUT,BASE,g,a
from tools.feed_trail_revalidation import dist,quantile
from tools.friction_pl_trail_study import accounting
from tools.market_bot_replay import _deduplicate
ZONE=timezone(timedelta(hours=-3))
def day(ms):return datetime.fromtimestamp(ms/1000,ZONE).strftime('%Y-%m-%d')
def band(x):return '<8' if x<8 else '8–10' if x<10 else '10–13' if x<13 else '13–20' if x<20 else '>=20'

def observations(d,t,path,candles,end):
    if 'observations' in d:
        rows=[dict(o) for o in d['observations']]
        if rows and d['closed_ms'] is not None:rows[-1]['price']=d['floor_events'][-1]['price']
        return rows or [dict(at_ms=d['opened_ms'],tick=0,price=d['entry_price'],peak=d['entry_price'])]
    # Reconstruct visited OHLC points by the persisted terminal tick index.
    # No decision engine. Final fill comes from the original trade.
    rows=[];tick=0;peak=d['entry_price'];last=d['floor_events'][-1]['tick_index']
    for at in range(d['opened_ms']+60000,(d['closed_ms'] or end)+1,60000):
        c=candles.get(at)
        if c is None:continue
        points=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST' else (c.open,c.low,c.high,c.close))
        for price in points:
            tick+=1
            if tick>last:break
            if tick==last and d['closed_ms'] is not None:price=d['floor_events'][-1]['price']
            peak=max(peak,price);rows.append(dict(at_ms=at,tick=tick,price=price,peak=peak))
        if tick>=last:break
    assert rows[-1]['tick']==last
    assert abs(peak-d['terminal_peak'])<1e-8,'Visited-path peak does not match frozen artifact'
    return rows

def describe(payload,path,candles,end,m):
    by={d['source_candle']:d for d in payload['details']};rows=[]
    for t in payload['run']['trades']:
        d=by[t['source_candle']];obs=observations(d,t,path,candles,end)
        entry=t['entry_price'];atr=d['entry_atr'];peak=d['terminal_peak'];closed=t['closed_ms'] is not None
        costs=accounting(t,m['notional'],m['spread_bps']) if closed else None
        after={}
        for level in [0,8,10,13,20]:
            first=next((i for i,o in enumerate(obs) if (o['peak']-entry)/atr>=level),None)
            after[level]=min((o['price']-entry)/entry*100 for o in obs[first:]) if first is not None else None
            if level==0 and after[level] is not None:after[level]=min(0.,after[level])
        rows.append(dict(**t,entry_atr=atr,owner=d['terminal_owner'],mfe_atr=(peak-entry)/atr,
            economic_floor_atr=.0025*entry/atr,
            solvency_trigger_atr=max(1.5,.0025*entry/atr)+3.5,
            activation_ms=d['activation']['at_ms'] if d['activation'] else None,
            giveback_pct=(peak-t['exit_price'])/entry*100 if closed else None,
            giveback_atr=(peak-t['exit_price'])/atr if closed else None,
            mae_pct=min(0.,min((o['price']-entry)/entry*100 for o in obs)),
            intra_dd_pct=max((o['peak']-o['price'])/entry*100 for o in obs),
            after_band_mae_pct=after,costs=costs))
    return rows

def bootstrap(left,right,start,end):
    days=[];cursor=datetime.fromtimestamp(start/1000,ZONE).date();last=datetime.fromtimestamp(end/1000,ZONE).date()
    while cursor<=last:days.append(str(cursor));cursor+=timedelta(days=1)
    values={d:0. for d in days}
    for sign,rows in [(-1,left),(1,right)]:
        for t in rows:
            if t['closed_ms'] is not None:values[day(t['closed_ms'])]+=sign*t['net_usd']
    blocks=[days[i:i+7] for i in range(len(days)-6)];rng=random.Random(20261009);xs=[]
    for _ in range(5000):
        sample=[d for block in rng.choices(blocks,k=math.ceil(len(days)/7)) for d in block][:len(days)]
        xs.append(sum(values[d] for d in sample))
    return dict(delta=sum(values.values()),ci95=[quantile(xs,.025),quantile(xs,.975)],daily_delta=values)

def contrast(left,right,end):
    l={t['source_candle']:t for t in left};r={t['source_candle']:t for t in right};pairs=[];contrib=[]
    for source in sorted(set(l)|set(r)):
        c=l.get(source);v=r.get(source);resolved=bool(c and v and c['closed_ms'] is not None and v['closed_ms'] is not None)
        net=lambda x:x['net_usd'] if x and x['closed_ms'] is not None else 0
        if c and v:
            assert c['opened_ms']==v['opened_ms']
            if 'entry_price' in c and 'entry_price' in v:assert abs(c['entry_price']-v['entry_price'])<1e-8
        kind='COMMON_RESOLVED' if resolved else 'COMMON_CENSORED' if c and v else 'VARIANT_ONLY' if v else 'COMPARATOR_ONLY'
        delta=net(v)-net(c);categories=[]
        if kind!='COMMON_RESOLVED':categories.append('slot/admission/path or censoring: '+kind)
        elif v['closed_ms']>c['closed_ms']:
            categories.append('held longer, earned more' if delta>1e-8 else 'held longer, gave back more' if delta<-1e-8 else 'held longer, equal net')
        if c and v and c['exit_reason']=='PROFIT_LOCK' and (v['closed_ms'] is None or v['closed_ms']>c['closed_ms']):
            categories.append('comparator '+c['owner']+' exit avoided, joint mechanism')
        if c and v and c.get('solvency_trigger_atr',0)>10 and c['exit_reason']=='TRAILING' and c['closed_ms'] is not None and (v.get('activation_ms') is None or c['closed_ms']<v['activation_ms']):
            categories.append('comparator TRAIL protected before solvency armed; variant activation delayed')
        if v:categories.append('trail13 owner' if v['owner']=='TRAIL' else 'solvency owner' if v['owner']=='PL1' else 'other')
        row=dict(source_candle=source,kind=kind,delta=delta if resolved else None,realized_contribution=delta,
            control=c,variant=v,categories=categories)
        pairs.append(row);contrib.append(dict(source=source,delta=delta,kind=kind,
            start=min(t['opened_ms'] for t in [c,v] if t),end=max(t['closed_ms'] or end for t in [c,v] if t)))
    episodes=[]
    for t in sorted(contrib,key=lambda x:x['start']):
        if not episodes or t['start']>episodes[-1]['end']:episodes.append(dict(start=t['start'],end=t['end'],delta=0,N=0))
        e=episodes[-1];e['end']=max(e['end'],t['end']);e['delta']+=t['delta'];e['N']+=1
    total=sum(t['delta'] for t in contrib);direction=1 if total>=0 else -1
    positive=sorted([direction*e['delta'] for e in episodes if direction*e['delta']>0],reverse=True)
    concentration={str(f):next((n for n in range(1,len(positive)+1) if sum(positive[:n])>=f*abs(total)),None) for f in [.5,.8]}
    continued=[p for p in pairs if p['control'] and p['variant'] and p['control']['exit_reason']=='PROFIT_LOCK'
        and (p['variant']['closed_ms'] is None or p['variant']['closed_ms']>p['control']['closed_ms'])]
    resolved=[p for p in continued if p['delta'] is not None];worse=[p for p in resolved if p['delta']<0]
    return dict(pairs=pairs,categories=dict(Counter(k for p in pairs for k in p['categories'])),
        category_delta={k:sum(p['delta'] for p in pairs if k in p['categories'] and p['delta'] is not None) for k in set(k for p in pairs for k in p['categories'])},
        decomposition={k:sum(t['delta'] for t in contrib if t['kind']==k) for k in set(t['kind'] for t in contrib)},
        episodes=episodes,concentration=concentration,continued_PL=dict(N=len(continued),resolved=len(resolved),
            censored=len(continued)-len(resolved),better=sum(p['delta']>1e-8 for p in resolved),worse=len(worse),
            comparator_net=sum(p['control']['net_usd'] for p in resolved),v2_net=sum(p['variant']['net_usd'] for p in resolved),
            delta=sum(p['delta'] for p in resolved),worse_delta=dist([p['delta'] for p in worse]),
            worse_loss_magnitude=dist([-p['delta'] for p in worse]),destinations=dict(Counter(p['variant']['exit_reason'] for p in continued)),
            rows=continued))

def main():
    m=json.loads((OUT/'manifest.json').read_text());start=g.ms(m['start_brt']);end=g.ms(m['end_brt'])
    candles={c.boundary_ms:c for c in g.load_candle_cache(g.CACHE/'SOLUSDT_1m.jsonl')}
    arms=['ACT10_GAP5','ACT10_GAP13','LADDER_V2'];result={};individual={}
    for path in g.PATHS:
        payload={arm:json.loads(((OUT/f'{path}_LADDER_V2.json') if arm=='LADDER_V2' else
            (OUT/f'{path}_PARITY_ACT10_GAP5.json') if arm=='ACT10_GAP5' else BASE/f'{path}_{arm}.json').read_text()) for arm in arms}
        assert payload['ACT10_GAP13']['prior_full_parity']
        rows={arm:describe(payload[arm],path,candles,end,m) for arm in arms};individual[path]=rows
        metrics={}
        for arm in arms:
            metrics[arm]={}
            for month in g.MONTHS:
                selected=[t for t in rows[arm] if t['closed_ms'] is not None and (month=='ALL' or g.month(t['closed_ms'])==month)]
                x=g.metrics(payload[arm]['run'],month)
                x.update(costs={k:sum(t['costs'][k] for t in selected) for k in ['gross','fees','spread','net']},
                    open_end=sum(t['closed_ms'] is None for t in rows[arm]) if month=='ALL' else None,
                    giveback_pct=dist([t['giveback_pct'] for t in selected]),intra_dd_pct=dist([t['intra_dd_pct'] for t in selected]),
                    mae_pct=dist([t['mae_pct'] for t in selected]),owners=dict(Counter(t['owner'] for t in selected)))
                x['mae_pct']['min']=min((t['mae_pct'] for t in selected),default=None)
                metrics[arm][month]=x
        contrasts={}
        for left,right in [('ACT10_GAP5','LADDER_V2'),('ACT10_GAP5','ACT10_GAP13'),('ACT10_GAP13','LADDER_V2')]:
            c=contrast(rows[left],rows[right],end);c['bootstrap']=bootstrap(rows[left],rows[right],start,end)
            c['monthly_delta']={month:metrics[right][month]['net']-metrics[left][month]['net'] for month in g.MONTHS}
            c['bands']={b:dict(N=len(xs),net=sum(t['net_usd'] for t in xs if t['closed_ms'] is not None),
                exits=dict(Counter(t['exit_reason'] for t in xs)),giveback_pct=dist([t['giveback_pct'] for t in xs if t['giveback_pct'] is not None]),
                after_band_mae_pct=dist([t['after_band_mae_pct'][level] for t in xs if t['after_band_mae_pct'][level] is not None]),
                paired_delta=sum(p['delta'] for p in c['pairs'] if p['delta'] is not None and band(p['control']['mfe_atr'])==b),
                paired_N=sum(p['delta'] is not None and band(p['control']['mfe_atr'])==b for p in c['pairs']))
                for b,level in [('<8',0),('8–10',8),('10–13',10),('13–20',13),('>=20',20)]
                for xs in [[t for t in rows[right] if band(t['mfe_atr'])==b]]}
            contrasts[right+' - '+left]=c
        result[path]=dict(metrics=metrics,contrasts=contrasts)
    (OUT/'summary.json').write_text(json.dumps(g.safe_json(result),indent=2),encoding='utf-8')
    (OUT/'trades.json').write_text(json.dumps(g.safe_json(individual),indent=2),encoding='utf-8')
    table=lambda h,r:'\n'.join(g.table(h,r)) if isinstance(g.table(h,r),list) else g.table(h,r)
    fmt=g.fmt
    lines=['# LADDER_V2 — variante única',
        'Janela congelada 01/06 → 02/10/2026 22:28 BRT. Comparadores factuais, nunca normativos. Paridade integral de runs e snapshots/owners ACT10_GAP5 nos dois paths PASS antes de executar V2. Peak máximo desde entrada. Mesmos sinais, custos, CB, capacity/spacing; trajetórias independentes.',
        'Gross antes de taxas/spread; identidade gross−fees−spread=net. DD realizado, equity inicial zero; intratrade DD é queda desde pico conhecido em % de entry. Distribuições censuradas apenas até saída ou fim disponível. MAE pós-faixa é mínimo PnL após primeiro toque causal do limite inferior, não mínimo de uma continuação inventada. Faixa <8 usa entrada como início. Custos e net não imputados a OPEN.']
    for path,v in result.items():
        lines += ['## '+path,table(['arm','closed','open/cens','net','net/trade','gross','fees','spread','PF','DD','age med','HS','PL','TRAIL'],
            [[arm,x['closed'],x['open_end'],fmt(x['net']),fmt(x['net_trade']),*[fmt(x['costs'][k]) for k in ['gross','fees','spread']],fmt(x['pf']),fmt(x['dd']),fmt(x['median_age']),x['HARD_STOP'],x['PROFIT_LOCK'],x['TRAILING']] for arm,ms in v['metrics'].items() for x in [ms['ALL']]]),
            table(['arm','med GB%','p75','p90','p95','max','pior intra DD%'],[[arm,*[fmt(x['giveback_pct'][k]) for k in ['median','p75','p90','p95','max']],fmt(x['intra_dd_pct']['max'])] for arm,ms in v['metrics'].items() for x in [ms['ALL']]]),
            table(['mês','ACT10_GAP5 net','ACT10_GAP13 net','V2 net','V2−GAP13'],[[month,*[fmt(v['metrics'][arm][month]['net']) for arm in arms],fmt(v['contrasts']['LADDER_V2 - ACT10_GAP13']['monthly_delta'][month])] for month in g.MONTHS])]
        for name,c in v['contrasts'].items():
            lines += ['### '+name,table(['delta net','IC95%','50% episódios','80% episódios','decomposição'],[[fmt(c['bootstrap']['delta']),c['bootstrap']['ci95'],c['concentration']['0.5'],c['concentration']['0.8'],c['decomposition']]]),
                'Categorias observadas (podem sobrepor, não somar como efeitos independentes): '+json.dumps(c['categories']),
                'Delta dos pares resolvidos por categoria sobreposta: '+json.dumps(c['category_delta']),
                table(['MFE do braço direito','N','net','pares por MFE comparador N','delta pares','GB med%','p90','MAE pós-limite med%'],[[b,x['N'],fmt(x['net']),x['paired_N'],fmt(x['paired_delta']),fmt(x['giveback_pct']['median']),fmt(x['giveback_pct']['p90']),fmt(x['after_band_mae_pct']['median'])] for b,x in c['bands'].items()])]
            if name=='LADDER_V2 - ACT10_GAP13':
                x=c['continued_PL'];lines += ['### GAP13 saiu PL, V2 continuou',
                    table(['N','resolved','cens','melhor','pior','GAP13 net pares','V2 net pares','delta','destinos'],[[x[k] for k in ['N','resolved','censored','better','worse','comparator_net','v2_net','delta','destinations']]]),
                    'Distribuição das perdas relativas (magnitudes positivas): '+json.dumps(x['worse_loss_magnitude']),
                    table(['source BRT','PL GAP13','net GAP13','destino V2','net V2','delta','MFE V2 ATR','GB V2%'],[[a.brt(p['source_candle']),p['control']['owner'],p['control']['net_usd'],p['variant']['exit_reason'],p['variant']['net_usd'],p['delta'],p['variant']['mfe_atr'],p['variant']['giveback_pct']] for p in x['rows']])]
    principal=[result[p]['contrasts']['LADDER_V2 - ACT10_GAP13']['bootstrap'] for p in g.PATHS]
    strong=all(x['ci95'][0]>0 for x in principal) or all(x['ci95'][1]<0 for x in principal)
    lines += ['## Veredito do contraste principal',
        'Evidência forte segundo o pré-registro: '+str(strong)+'. IC95% exclui zero com sinal concordante nos dois paths somente se True. Caso False, não houve evidência suficiente para diferenciar esta variante dos comparadores neste contraste/amostra; ponto estimado não valida componentes.',
        'O ponto estimado V2−GAP13 é negativo nos dois paths, enquanto V2−GAP5 é positivo; os IC95% desses dois contrastes V2 incluem zero nos dois paths. Contra GAP13, DD realizado sobe de 54,6243 para 56,8468 (HIGH) e de 47,7348 para 49,3804 (LOW). Devolução máxima sobe de 3,8239% para 4,4575%; pior DD intra-trade de 3,7986% para 4,4323%. Esta variante não mostrou melhoria diferenciável e aceita mais devolução; não há validação normativa dos comparadores.',
        'Na população GAP13 PL→V2 continuação, 44/259 ficaram melhores em HIGH e 39/269 em LOW. Deltas +0,7351 e −0,0075. A continuação após PL2 contribuiu −1,0801/−1,7953; após PL3 +1,8152/+1,7878. São associações da intervenção conjunta, não efeitos isolados de cada degrau. Nos pares completos comuns, delta −0,4212/−0,6284; os sources exclusivos completam o saldo sistêmico. 3/4 episódios explicam 50%/80% da desvantagem HIGH e 2/3 LOW; isso é concentração contábil por componentes de vidas sobrepostas, não prova de mecanismo causal.',
        '## Limites de inferência',
        'A remoção de PL2/PL3 e a habilitação junto com PL1 são uma intervenção conjunta. Quando trigger PL1 <=10, habilitar TRAIL cedo não o torna dominante: peak−13 continua abaixo do piso; a fronteira piso+13 é maior que10. Quando trigger PL1 >10, a V2 pode atrasar proteção que GAP13 já exercia antes de PL1. Esse caso é contado separadamente. A decomposição por owner/source mostra caminhos observados, não efeitos causais isolados dos componentes. Sources exclusivos e pares censurados explicam parte do delta sistêmico. Melhor net não torna aceitável automaticamente a cauda/DD. Pior V2 significa somente que esta variante específica não melhorou; indistinguível significa evidência insuficiente nesta amostra. Nenhum resultado valida automaticamente PL2/PL3, activation10/gap5 ou componentes V2. Não foi testada outra variante.']
    (OUT/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    print(json.dumps({p:{name:c['bootstrap']|{'daily_delta':'see JSON'} for name,c in v['contrasts'].items()} for p,v in result.items()}))

def split_case_tables():
    """Presentation only: keep the main report readable, preserve every case."""
    old='GAP13 protected before solvency armed; V2 activation delayed'
    new='comparator TRAIL protected before solvency armed; variant activation delayed'
    lines=(OUT/'RELATORIO.md').read_text(encoding='utf-8').replace(old,new).splitlines()
    main_lines=[];cases=['# GAP13 PL → V2 continuação: casos individuais','']
    path='';i=0
    while i<len(lines):
        line=lines[i]
        if line in ['## HIGH_FIRST','## LOW_FIRST']:path=line
        if line.startswith('| source BRT | PL GAP13 |'):
            cases.extend([path,''])
            while i<len(lines) and lines[i].startswith('|'):
                cases.append(lines[i]);i+=1
            cases.append('');main_lines.append('Casos individuais completos: [CASOS.md](CASOS.md).')
        else:main_lines.append(line);i+=1
    if len(cases)>2:
        (OUT/'CASOS.md').write_text('\n'.join(cases),encoding='utf-8')
    (OUT/'RELATORIO.md').write_text('\n'.join(main_lines),encoding='utf-8')
    summary_path=OUT/'summary.json'
    summary_path.write_text(summary_path.read_text(encoding='utf-8').replace(old,new),encoding='utf-8')

if __name__=='__main__':
    main();split_case_tables()
