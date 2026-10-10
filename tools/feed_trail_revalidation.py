"""Offline diagnostic; consumes snapshots, never touches live bot state."""
import hashlib
import json
import math
import random
import sys
from collections import Counter
from datetime import datetime
from datetime import timedelta, timezone
from itertools import combinations
from bisect import bisect_left
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/analysis/feed_trail_revalidation_20261009'


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    a = (len(xs)-1)*q
    i = int(a)
    return xs[i]+(xs[min(i+1,len(xs)-1)]-xs[i])*(a-i)


def dist(xs):
    return dict(n=len(xs), **{k:quantile(xs,q) for k,q in
                             [('median',.5),('p75',.75),('p90',.9),('p95',.95),('max',1)]})


def stamp(v):
    return datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()


def net_value(r):
    for key in ('net_pnl','net_pnl_usd','net_usd'):
        if r.get(key) is not None:return float(r[key])
    if r.get('net_pnl_pct') is not None and r.get('position_notional_usdt') is not None:
        return float(r['net_pnl_pct'])*float(r['position_notional_usdt'])/100
    raise ValueError('Closed record has no valid net amount')


def temporal_blocks(windows, seconds):
    """Overlapping real-time blocks, as preregistered; not independent windows."""
    ats=[w['at'] for w in windows]
    return [windows[i:bisect_left(ats,at+seconds)] for i,at in enumerate(ats)
            if at+seconds<=ats[-1]]


def feed():
    start=stamp('2026-10-08T23:52:00-03:00')
    windows=[]; connections=[]; anomalies=[]
    for line in (OUT/'raw/system.log').open(encoding='utf-8'):
        try:r=json.loads(line)
        except ValueError:continue
        if stamp(r['ts'])<start:continue
        e=r.get('event','')
        if e=='websocket_input_metrics':
            windows.append(dict(at=stamp(r['emitted_at']),inputs=r['inputs'],
                rate=r['inputs']/r['window_seconds'],lag=r['lag_ms'],callback=r['callback_ms'],
                duration=r['window_seconds'],errors=r.get('callback_errors',0)))
        elif e=='websocket_input_lag':anomalies.append(r)
        elif e.startswith('websocket_'):connections.append(r)
    episodes=[]
    previous_degraded=False
    for w in windows:
        w['degraded']=w['lag']['p50'] is not None and w['lag']['p50']>5000
        if not w['degraded']:
            previous_degraded=False
            continue
        if not episodes or not previous_degraded or w['at']-episodes[-1]['end']>20:
            episodes.append(dict(start=w['at']-w['duration'],end=w['at'],windows=[]))
        episodes[-1]['end']=w['at'];episodes[-1]['windows'].append(w)
        previous_degraded=True
    for e in episodes:
        ws=e.pop('windows');e.update(n=len(ws),duration_s=e['end']-e['start'],
            max_lag_p99_ms=max(w['lag']['p99'] for w in ws),
            max_callback_p99_ms=max(w['callback']['p99'] for w in ws),max_rate=max(w['rate'] for w in ws))
        individual=[r for r in anomalies if e['start']<=stamp(r['receive_at'])<=e['end']]
        e['individual_lag_records']=len(individual)
        e['max_recorded_lag_ms']=max((r['lag_ms'] for r in individual),default=None)
        e['max_recorded_callback_conditional_ms']=max((r['callback_ms'] for r in individual),default=None)
    comparisons={}
    for key in ['rate','callback_p50','callback_p90']:
        value=lambda w:w['rate'] if key=='rate' else w['callback'][key.split('_')[1]]
        eligible=[w for w in windows if value(w) is not None]
        groups={k:[value(w) for w in eligible if w['degraded']==flag] for k,flag in [('normal',False),('degraded',True)]}
        differences=[];rng=random.Random(20261009)
        # Fixed real-time blocks; sample whole blocks, never independent windows.
        bs=temporal_blocks(eligible,1800)
        sample_blocks=math.ceil((eligible[-1]['at']-eligible[0]['at'])/1800)
        for _ in range(2000):
            sample=[w for b in rng.choices(bs,k=sample_blocks) for w in b][:len(eligible)]
            a=[value(w) for w in sample if w['degraded']];b=[value(w) for w in sample if not w['degraded']]
            if a and b:differences.append(quantile(a,.5)-quantile(b,.5))
        comparisons[key]={k:dist(v) for k,v in groups.items()}
        comparisons[key].update(delta_median=quantile(groups['degraded'],.5)-quantile(groups['normal'],.5),
            block_bootstrap_ci95=[quantile(differences,.025),quantile(differences,.975)],
            overlapping_blocks=len(bs),nonoverlap_blocks=int((eligible[-1]['at']-eligible[0]['at'])/1800))
    reconnects=[]
    for r in connections:
        if r['event']!='websocket_reconnect_first_input':continue
        at=stamp(r['ts']);ws=[w for w in windows if at-300<=w['at']<at]
        reconnects.append(dict(event=r,pre5m={k:dist([f(w) for w in ws]) for k,f in
            [('rate',lambda w:w['rate']),('lag_p50_ms',lambda w:w['lag']['p50']),('callback_p90_ms',lambda w:w['callback']['p90'])] if all(f(w) is not None for w in ws)}))
    result=dict(start=start,end=windows[-1]['at'],windows=len(windows),degraded=sum(w['degraded'] for w in windows),
        inputs=sum(w['inputs'] for w in windows),callback_errors=sum(w['errors'] for w in windows),
        episodes=episodes,comparisons=comparisons,reconnects=reconnects,connections=Counter(r['event'] for r in connections),
        price_metrics='N/A: no complete price series in feed metrics; selective anomaly logs contain no price',
        input_composition='N/A: stream_health is last observation, not counts per stream')
    (OUT/'feed_windows.json').write_text(json.dumps(windows),encoding='utf-8')
    (OUT/'feed_results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('episodes','reconnects')}),flush=True)


def provenance():
    cache=ROOT/'data/studies/be_off_cb_deterioration/klines'
    expected=json.loads((ROOT/'data/studies/trail_activation_gap_systemic/20261005/manifest.json').read_text())['cache_hashes']
    result={}
    for tf,step in [('1m',60000),('5m',300000),('15m',900000)]:
        p=cache/f'SOLUSDT_{tf}.jsonl';raw=p.read_bytes();rows=[json.loads(l) for l in raw.splitlines()]
        times=[r['open_time_ms'] for r in rows];diff=[b-a for a,b in zip(times,times[1:])]
        sha=hashlib.sha256(raw).hexdigest()
        result[tf]=dict(file=str(p),n=len(rows),first=times[0],last=times[-1],sha256=sha,
            hash_matches_manifest=sha==expected[tf],duplicates=len(times)-len(set(times)),out_of_order=sum(d<0 for d in diff),
            gaps=sum(d>step for d in diff),missing_intervals=sum(max(0,d//step-1) for d in diff),
            bad_close=sum(r['close_time_ms']!=r['open_time_ms']+step-1 for r in rows))
    inventory=[]
    for p in sorted((ROOT/'data/studies').rglob('*manifest.json')):
        try:m=json.loads(p.read_text(encoding='utf-8-sig'))
        except ValueError:continue
        # Preserve original metadata; do not invent provenance from folder names.
        inventory.append(dict(study=str(p.parent.relative_to(ROOT)),manifest=str(p),
            metadata={k:v for k,v in m.items() if any(s in k.lower() for s in ['start','end','cache','source','input','period','data','coverage'])}))
    (OUT/'provenance.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    (OUT/'inventory_metadata.json').write_text(json.dumps(inventory,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)
    return all(r['hash_matches_manifest'] and not(r['gaps'] or r['duplicates'] or r['out_of_order'] or r['bad_close']) for r in result.values())


def replay():
    from tools import trail_activation_gap_systemic_study as a
    g=a.g
    m=json.loads((a.OUT/'manifest.json').read_text());start=g.ms(m['start_brt']);end=g.ms(m['end_brt'])
    candles=g.load_candle_cache(g.CACHE/'SOLUSDT_1m.jsonl')
    signals=[g.SignalEvent(r['boundary_ms'],g.EntrySignal(**r['signal'])) for r in json.loads((g.PRIOR/'signals.json').read_text())]
    result={}
    for path in g.PATHS:
        result[path]={}
        for arm in ['ACT10_GAP5','ACT20_GAP5','ACT10_GAP13']:
            print('REPLAY',path,arm,flush=True);inst=a.Instrumentation(start,end,m['notional'])
            with patch.object(g.systemic,'BotFullExitPosition',inst.factory),patch.object(g.systemic,'process_candle_systemic',inst.processor):
                run=g.systemic.run_systemic(name=arm,config=a.arm_config(m['config'],arm),signals=signals,candles=candles,
                    contexts=[],start_ms=start,end_ms=end,path=path,spread_bps=m['spread_bps'],fast_enabled=False)
            serialized=g.serialize(run,signals,m['notional'])
            prior=json.loads((a.OUT/f'{path}_{arm}.json').read_text())['run']
            payload={'run':serialized,'details':inst.details(),'prior_full_parity':serialized==prior}
            (OUT/f'{path}_{arm}.json').write_text(json.dumps(g.safe_json(payload),allow_nan=False),encoding='utf-8')
            result[path][arm]={mo:g.metrics(serialized,mo) for mo in g.MONTHS}
            print(path,arm,'PRIOR PARITY',serialized==prior,flush=True)
    (OUT/'replay_summary.json').write_text(json.dumps(g.safe_json(result),indent=2,allow_nan=False),encoding='utf-8')


def comparisons():
    arms=['ACT10_GAP5','ACT20_GAP5','ACT10_GAP13']
    brt=timezone(timedelta(hours=-3))
    day=lambda ms:datetime.fromtimestamp(ms/1000,brt).strftime('%Y-%m-%d')
    all_results={}
    for path in ['HIGH_FIRST','LOW_FIRST']:
        data={a:json.loads((OUT/f'{path}_{a}.json').read_text()) for a in arms}
        by={a:{t['source_candle']:t for t in data[a]['run']['trades']} for a in arms}
        details={a:{d['source_candle']:d for d in data[a]['details']} for a in arms}
        sources=sorted(set().union(*(set(x) for x in by.values())))
        divergences=[];episodes=[]
        end=max(t['closed_ms'] or t['opened_ms'] for a in arms for t in by[a].values())
        for source in sources:
            ts={a:by[a].get(source) for a in arms};existing=[t for t in ts.values() if t]
            opened=min(t['opened_ms'] for t in existing);closed=max(t['closed_ms'] or end for t in existing)
            if not episodes or opened>episodes[-1]['end']:
                episodes.append({'start':opened,'end':closed,'sources':[]})
            episodes[-1]['end']=max(episodes[-1]['end'],closed);episodes[-1]['sources'].append(source)
            if len({json.dumps(t,sort_keys=True) for t in ts.values()})>1:
                base=ts.get('ACT10_GAP5');effect={}
                for candidate,label in [('ACT20_GAP5','ACT20'),('ACT10_GAP13','GAP13')]:
                    t=ts.get(candidate)
                    if base and t and base['closed_ms'] and t['closed_ms']:
                        d=t['net_usd']-base['net_usd']
                        effect[label]={'delta':d,'label':label+(' ajudou' if d>1e-8 else ' piorou' if d< -1e-8 else ' econômico igual')}
                    else:effect[label]={'label':'SLOT/PATH_OR_CENSORED; no resolved pair'}
                divergences.append({'source_candle':source,'trades':ts,'geometry':{a:details[a].get(source) for a in arms},
                    'effects':effect,'classification':'SLOT/PATH_DEPENDENCE' if any(t is None for t in ts.values()) else 'COMMON_PRICE/EXIT_DIVERGENCE'})
        contrasts={}
        first=min(day(t['closed_ms']) for a in arms for t in by[a].values() if t['closed_ms'])
        last=max(day(t['closed_ms']) for a in arms for t in by[a].values() if t['closed_ms'])
        days=[];cursor=datetime.fromisoformat(first)
        while cursor.strftime('%Y-%m-%d')<=last:
            days.append(cursor.strftime('%Y-%m-%d'));cursor+=timedelta(days=1)
        daily={a:Counter() for a in arms}
        for a in arms:
            for t in by[a].values():
                if t['closed_ms']:daily[a][day(t['closed_ms'])]+=t['net_usd']
        for left,right in combinations(arms,2):
            deltas=[daily[right][d]-daily[left][d] for d in days]
            rng=random.Random(20261009);boot=[]
            for _ in range(5000):
                indices=[]
                while len(indices)<len(days):
                    i=rng.randrange(max(1,len(days)-6));indices.extend(range(i,min(i+7,len(days))))
                boot.append(sum(deltas[i] for i in indices[:len(days)]))
            contributions=[]
            for source in sources:
                l=by[left].get(source);r=by[right].get(source)
                if l is None and r is None:continue
                net=lambda t:t['net_usd'] if t and t['closed_ms'] else 0
                contributions.append({'source_candle':source,'delta':net(r)-net(l),
                    'kind':'COMMON' if l and r else 'ONLY_'+right if r else 'ONLY_'+left})
            episode_deltas=[sum(r['delta'] for r in contributions if r['source_candle'] in e['sources']) for e in episodes]
            total=sum(deltas);direction=1 if total>=0 else -1
            positives=sorted([direction*r['delta'] for r in contributions if direction*r['delta']>0],reverse=True)
            counts={}
            for share in [.5,.8]:
                cumulative=0;n=0
                for x in positives:
                    n+=1;cumulative+=x
                    if cumulative>=share*abs(total):break
                counts[str(share)]=n if total else None
            ci=[quantile(boot,.008333333333),quantile(boot,.991666666667)]
            low_n=len(days)//7<10 or sum(abs(x)>1e-8 for x in episode_deltas)<10
            label='INCONCLUSIVE_LOW_N' if low_n else 'EVIDENCE_PATH_SPECIFIC' if ci[0]>0 or ci[1]<0 else 'INDISTINGUISHABLE_THIS_SAMPLE'
            contrasts[right+' - '+left]=dict(delta=total,ci98_333=ci,classification=label,
                n_days=len(days),nonoverlap_7day_blocks=len(days)//7,episodes=len(episodes),
                nonzero_episodes=sum(abs(x)>1e-8 for x in episode_deltas),episode_deltas=episode_deltas,
                daily_deltas=dict(zip(days,deltas)),concentration=counts,contributions=contributions,
                decomposition={kind:sum(x['delta'] for x in contributions if x['kind']==kind)
                               for kind in set(x['kind'] for x in contributions)},
                episode_concentration={str(share):next((i for i in range(1,len(episode_deltas)+1)
                    if sum(sorted([direction*x for x in episode_deltas if direction*x>0],reverse=True)[:i])>=share*abs(total)),None)
                                       for share in [.5,.8]},
                common_resolved=sum(bool(by[left].get(s) and by[right].get(s) and by[left][s]['closed_ms'] and by[right][s]['closed_ms']) for s in sources))
        all_results[path]=dict(contrasts=contrasts,episodes=episodes,divergences=divergences)
    (OUT/'comparisons.json').write_text(json.dumps(all_results,indent=2),encoding='utf-8')
    print(json.dumps({p:{k:{i:v for i,v in c.items() if i not in ['contributions','daily_deltas','episode_deltas']} for k,c in r['contrasts'].items()} for p,r in all_results.items()}),flush=True)


def feed_sensitivity():
    windows=json.loads((OUT/'feed_windows.json').read_text())
    start=stamp('2026-10-08T23:52:00-03:00');result={}
    for key in ['rate','callback_p50','callback_p90']:
        value=lambda w:w['rate'] if key=='rate' else w['callback'][key.split('_')[1]]
        eligible=[w for w in windows if value(w) is not None]
        bs=temporal_blocks(eligible,3600);rng=random.Random(20261009);deltas=[]
        sample_blocks=math.ceil((eligible[-1]['at']-eligible[0]['at'])/3600)
        for _ in range(2000):
            sample=[w for b in rng.choices(bs,k=sample_blocks) for w in b][:len(eligible)]
            a=[value(w) for w in sample if w['degraded']];b=[value(w) for w in sample if not w['degraded']]
            if a and b:deltas.append(quantile(a,.5)-quantile(b,.5))
        result[key]={'overlapping_blocks':len(bs),'nonoverlap_blocks':int((eligible[-1]['at']-eligible[0]['at'])/3600),
                     'ci95':[quantile(deltas,.025),quantile(deltas,.975)]}
    (OUT/'feed_sensitivity60min.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)


def forward():
    start=stamp('2026-10-08T23:52:00-03:00')
    names={'ACT10_GAP5':'be_off_cb','ACT20_GAP5':'be_off_cb_act20_gap5','ACT10_GAP13':'be_off_cb_act10_gap13'}
    out={};by={}
    for arm,name in names.items():
        rows=[json.loads(l) for l in (OUT/f'raw/trades_{name}_shadow.jsonl').open(encoding='utf-8')]
        rows=[r for r in rows if r.get('opened_at') and stamp(r['opened_at'])>=start]
        by[arm]={r.get('source_candle_open_time'):r for r in rows}
        closed=[r for r in rows if r.get('closed_at')]
        reasons=Counter(r.get('exit_reason') for r in closed)
        net=net_value
        state_path=OUT/f'raw/{name}_shadow.json'
        opened=[]
        if state_path.exists():
            state=json.loads(state_path.read_text())
            positions=state.get('positions',[])
            if isinstance(positions,dict):positions=list(positions.values())
            opened=[p for p in positions
                    if p.get('open_ts',p.get('opened_at')) and stamp(p.get('open_ts',p.get('opened_at')))>=start]
            for p in opened:
                by[arm][p['source_candle_open_time']]={**p,'closed_at':None}
        out[arm]=dict(closed=len(closed),open=len(opened) if state_path.exists() else None,net=sum(net(r) for r in closed),exits=reasons,
            median_age_min=quantile([(stamp(r['closed_at'])-stamp(r['opened_at']))/60 for r in closed],.5))
    pairs={}
    for l,r in combinations(names,2):
        common=set(by[l])&set(by[r]);resolved=[s for s in common if by[l][s].get('closed_at') and by[r][s].get('closed_at')]
        pairs[r+' - '+l]=dict(common=len(common),resolved=len(resolved),
            delta=sum(net(by[r][s])-net(by[l][s]) for s in resolved),
            divergent=sum((by[l][s].get('exit_reason'),by[l][s].get('exit_price'))!=(by[r][s].get('exit_reason'),by[r][s].get('exit_price')) for s in common))
    result={'arms':out,'pairs':pairs}
    (OUT/'forward.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result),flush=True)


def render():
    feed_data=json.loads((OUT/'feed_results.json').read_text())
    for e in feed_data['episodes']:
        e['max_recorded_lag_ms']=None;e['max_recorded_callback_conditional_ms']=None
    for line in (OUT/'raw/system.log').open(encoding='utf-8'):
        try:r=json.loads(line)
        except ValueError:continue
        if r.get('event')!='websocket_input_lag':continue
        at=stamp(r['receive_at'])
        for e in feed_data['episodes']:
            if e['start']<=at<=e['end']:
                for target,source in [('max_recorded_lag_ms','lag_ms'),('max_recorded_callback_conditional_ms','callback_ms')]:
                    e[target]=max(e[target] or 0,r[source])
                break
    (OUT/'feed_results.json').write_text(json.dumps(feed_data,indent=2),encoding='utf-8')
    comparisons_data=json.loads((OUT/'comparisons.json').read_text())
    summary=json.loads((OUT/'replay_summary.json').read_text())
    fw=json.loads((OUT/'forward.json').read_text())
    zone=timezone(timedelta(hours=-3))
    brt=lambda sec:datetime.fromtimestamp(sec,zone).strftime('%d/%m/%Y %H:%M:%S')
    table=lambda headers,rows:'\n'.join(['| '+' | '.join(headers)+' |','|'+'|'.join(['---']*len(headers))+'|']+
        ['| '+' | '.join(str(v) for v in row)+' |' for row in rows])
    fmt=lambda v:'N/A' if v is None else f'{v:.4f}' if isinstance(v,float) else str(v)
    lines=['# Feed residual e revalidação dos três TRAIL',
        'Diagnóstico/replay local, sem trading/YAML/deploy/restart/Git. Pré-registro em PREREGISTER.md. Nenhum candidato é referência privilegiada.',
        '## A — feed',f"Corte de feed: {brt(feed_data['start'])} → {brt(feed_data['end'])} BRT. {feed_data['windows']} resumos, {feed_data['degraded']} degradados, {len(feed_data['episodes'])} episódios, {feed_data['inputs']} inputs, {feed_data['callback_errors']} erros de callback.",
        'Quantis abaixo são distribuições dos resumos10s; não são quantis globais de todos os inputs. Inputs/s é taxa PROCESSADA no callback, não taxa externa de chegada. Sete resumos sem duração de callback são excluídos somente dessa métrica.',
        table(['métrica','grupo','N','mediana','p75','p90','p95','max'],[[k,group,*[fmt(r[x]) for x in ['n','median','p75','p90','p95','max']]] for k,c in feed_data['comparisons'].items() for group,r in c.items() if group in ['normal','degraded']]),
        table(['métrica','delta de medianas','IC95% bloco30min'],[[k,fmt(c['delta_median']),str(c['block_bootstrap_ci95'])] for k,c in feed_data['comparisons'].items()]),
        'Sensibilidade pré-declarada com blocos60min em feed_sensitivity60min.json: associação de taxa e callback p50 permanece; IC de callback p90 ainda inclui zero. Blocos sobrepostos são candidatos de reamostragem, NÃO milhares de unidades independentes.',
        'Range/retorno10s e composição por input: N/A. Os arquivos existentes não contêm série completa de preços nem counts por stream. Não completar com preços de logs seletivos ou tratar candle1m como resolução10s.',
        '### Reconexões — cinco minutos anteriores',
        table(['BRT primeiro input','duração detectada s','lag antes ms','lag depois ms','mediana inputs/s','mediana lag p50 ms','mediana callback p90 ms'],
            [[brt(stamp(r['event']['receive_at'])),fmt(r['event']['disconnect_duration_seconds']),fmt(r['event']['lag_before_disconnect_ms']),fmt(r['event']['lag_after_reconnect_ms']),fmt(r['pre5m']['rate']['median']),fmt(r['pre5m']['lag_p50_ms']['median']),fmt(r['pre5m']['callback_p90_ms']['median'])] for r in feed_data['reconnects']]),
        'Evidência: carga processada maior nas janelas degradadas. O efeito em callback p90 não é distinguível de zero no IC; callback p50 muda com a mistura de inputs. Não identifica causa exclusivamente upstream/rede, nem prova burst externo: catch-up de fila pode elevar a taxa. Classificação causal: INCONCLUSIVO; contribuição de carga/processamento é plausível, mas volatilidade não foi mensurável nesta resolução.',
        'Cada episódio e seus extremos está em feed_results.json; cada resumo e respectivas métricas em feed_windows.json. Máximos p99 por episódio NÃO são máximos individuais verdadeiros.',
        '## B — procedência e integridade',
        'Os três replays são OHLC1m HIGH_FIRST/LOW_FIRST, não tick replay. Cache K proveniente do downloader REST Binance, independente da captura do bot. 182.968 candles1m / 36.593 candles5m / 12.197 candles15m; sem gaps/duplicações/inversões/close inválido. Todos hashes batem com os manifestos congelados. Conferência pontual REST dos dois primeiros candles1m coincidiu. Não foi feita comparação preço a preço de todo o cache.',
        'Conclusão de procedência: replay de trailing independente da infraestrutura degradada. Isso enfraquece especificamente a hipótese de calibração em ticks capturados sob lag; não elimina in-sample, path OHLC, concentração ou problemas de causalidade do motor.',
        'B1 não concluído: captura original de aggTrades das seis janelas não localizada. Déficit de ticks degradadas versus limpas é INDETERMINADO, não zero. Inventário detalhado e dívida científica em INVENTARIO.md / inventory_metadata.json. Não houve comprovação de contaminação material de K; B3 não acionado.',
        '## C — reexecução exata e incerteza',
        'Histórico congelado: 01/06/2026 → 02/10/2026 22:28 BRT, maior cache já disponível nesta cadeia. Notional $20, fees round-trip0,20%, spread5bps; seis máquinas independentes. Não foi escolhido outro fim após observar desempenho.',
        'Métrica principal: delta sistêmico de net por dia; block bootstrap7 dias, 5000 replicações. Três contrastes simétricos, intervalos98,333% por contraste. N episódios por sobreposição de posições é descritivo, não contagem de trades independentes. Não há margem arbitrária em dólares.']
    for path,arms in summary.items():
        lines += [f'### {path}',table(['mês','arm','closed','net','net/trade','PF','DD','HS','PL','TRAIL','max simultaneous'],
            [[month,arm,*[fmt(r.get(k)) for k in ['closed','net','net_trade','pf','dd','HARD_STOP','PROFIT_LOCK','TRAILING','max_sim']]] for arm,months in arms.items() for month,r in months.items()]),
            table(['contraste','delta $','IC98,333%','classe','dias','blocos7d','episódios nãozero','N para50% /80% delta'],
            [[k,fmt(c['delta']),str(c['ci98_333']),c['classification'],c['n_days'],c['nonoverlap_7day_blocks'],c['nonzero_episodes'],str(c['concentration'])] for k,c in comparisons_data[path]['contrasts'].items()])]
        diverged=[]
        for row in comparisons_data[path]['divergences']:
            prices=row['trades'];geometry=row['geometry'];base=prices.get('ACT10_GAP5')
            delta=lambda t:((t['net_usd'] if t and t['closed_ms'] else 0)-(base['net_usd'] if base and base['closed_ms'] else 0))
            for arm,t in prices.items():
                d=geometry.get(arm);activation=d.get('activation') if d else None
                diverged.append([brt(row['source_candle']/1000),arm,
                    brt(t['opened_ms']/1000) if t else 'NO MATCH',fmt(t['entry_price']) if t else 'NO MATCH',
                    brt(activation['at_ms']/1000) if activation else 'NOT ACTIVATED',
                    fmt((d['terminal_peak']-d['entry_price'])/d['entry_atr']) if d else 'N/A',
                    brt(t['closed_ms']/1000) if t and t['closed_ms'] else 'OPEN' if t else 'NO MATCH',
                    fmt(t['exit_price']) if t and t['closed_ms'] else 'N/A',t['exit_reason'] if t else 'NO MATCH',
                    fmt(t['net_usd']) if t and t['closed_ms'] else 'UNRESOLVED',fmt(delta(t)),row['classification']])
        (OUT/f'DIVERGENCIAS_{path}.md').write_text('# '+path+' — todas as fontes divergentes\n\n'+
            'Os contrastes econômicos de fontes exclusivas são contribuições sistêmicas, não pares de trade executado. Arm/disarm/floor transitions completos em comparisons.json.\n\n'+
            table(['source BRT','arm','entry BRT','entry','activation BRT','peak ATR','exit BRT','exit','reason','net','delta vs10/5','classe'],diverged),encoding='utf-8')
    lines += ['## D/E — divergências e concentração',
        'comparisons.json contém cada source divergente, todos os trades dos três braços e geometria completa: entrada, ativação, máximos conhecidos, floor/owner transitions e saídas. Somente diferenças de preço/tempo/motivo são rotuladas common; sources exclusivos explicitam path dependence. Bootstrap não remove esses efeitos de slots/CB da métrica principal.',
        'Concentração50%/80% conta contribuições no sentido do delta líquido. Como há compensações negativas, não equivale à parcela do ganho bruto. Deltas diários e por episódio estão no mesmo arquivo; fontes exclusivas e common ficam separadas.',
        table(['path','contraste','N episódios para50%/80%','delta common','contribuições sources exclusivos'],
            [[p,k,str(c['episode_concentration']),fmt(c['decomposition'].get('COMMON')),
              str({a:v for a,v in c['decomposition'].items() if a!='COMMON'})]
             for p,r in comparisons_data.items() for k,c in r['contrasts'].items()]),
        'O engine arma trailing uma vez e não possui transição normal de disarm durante o trade. Registro de disarm é N/A, não inventado. Não classificar uma diferença como puramente intrabar sem prova: HIGH/LOW são cenários separados.',
        '## F — forward pós-restart, sem posições herdadas',
        'opened_at >= 08/10/2026 23:52 BRT. Ledger de fechados e checkpoints vivos coletados em momentos próximos, não snapshot transacional simultâneo. Não comparar OPEN como net zero resolvido.',
        table(['arm','closed','open','net','PL','TRAIL','HS','median age min'],[[a,r['closed'],r['open'],fmt(r['net']),r['exits'].get('PROFIT_LOCK',0),r['exits'].get('TRAILING',0),r['exits'].get('HARD_STOP',0),fmt(r['median_age_min'])] for a,r in fw['arms'].items()]),
        table(['contraste','common','resolved','divergentes','delta common resolved'],[[k,c['common'],c['resolved'],c['divergent'],fmt(c['delta'])] for k,c in fw['pairs'].items()]),
        'Forward tem14 fontes comuns,12 resolvidas. Mesmo pós-patch há lag residual; “limpo” é corte de coorte, não certificação de execução perfeita. N curto não permite promover/descartar nem transformar discordância pontual em refutação histórica.',
        '## G — limites e decisão',
        'Preço do replay independente: SIM para K. Perda de ticks no feed próprio: NÃO DETERMINADA. Estudos sob suspeita: populações forward de episódios degradados e runs cuja procedência não foi recuperada; não todos os replays indiscriminadamente.',
        'Interpretar os três contrastes simetricamente. Evidência exige IC excluir zero e sinal concordar nos dois paths; intervalo cruzando zero significa indistinguível nesta amostra, não equivalência. Poucos episódios nãozero significam inconclusivo por N pequeno. Nenhum candidato merece descarte automático nesta auditoria.',
        'Resultado: todos os seis ICs corrigidos incluem zero; os três candidatos são INDISTINGUÍVEIS NESTA AMOSTRA pelo protocolo. ACT20 e GAP13 continuam melhores no ponto estimado em ambos paths, mas superioridade robusta não foi demonstrada. Todos os três nets agregados são negativos; melhorar relativamente não torna a política lucrativa.',
        'A melhora relativa concentra-se em agosto/setembro. Em julho ambos candidatos pioram em ambos paths; em junho o sinal muda entre HIGH/LOW. Não é benefício uniforme por período. Dependência de regime específico permanece indeterminada: não foi criado rótulo de regime novo nesta rodada.',
        'O forward curto está na direção contrária aos pontos estimados históricos, mas N12 resolvidos e lag residual não sustentam refutação. Nenhum braço é descartado ou privilegiado por status quo. O estudo não decide automaticamente implantação.',
        'Próxima pergunta científica: a diferença sistêmica se reproduz em episódios independentes, sem concentração nas mesmas poucas trajetórias? Não é proposta de novo parâmetro ou braço.',
        '## Ferramentas e testes',
        'Criados tools/feed_trail_revalidation.py e tests/test_feed_trail_revalidation.py. Reutilizado motor/Instrumentation de trail_activation_gap_systemic_study.py sem alterar qualquer arquivo de trading. Artefatos brutos, pré-registro, inventário, hashes, resumos e seis runs ficam nesta pasta. Comando de testes: python -m unittest tests.test_feed_trail_revalidation tests.test_trail_activation_gap_systemic_study tests.test_trail_gap_systemic_study. Resultado final informado no handoff.']
    (OUT/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    manifest={'tools':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [Path(__file__),ROOT/'tools/trail_activation_gap_systemic_study.py',ROOT/'tools/trail_gap_systemic_study.py',
         ROOT/'tools/be_off_cb_fast_drop_systemic_replay.py',ROOT/'src/position/bot_full_engine.py']},
        'snapshot_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (OUT/'raw').iterdir() if p.is_file()},
        'replay_parity':{p:{a:json.loads((OUT/f'{p}_{a}.json').read_text())['prior_full_parity'] for a in
                           ['ACT10_GAP5','ACT20_GAP5','ACT10_GAP13']} for p in ['HIGH_FIRST','LOW_FIRST']},
        'seed':20261009,'bootstrap_repetitions_feed':2000,'bootstrap_repetitions_replay':5000,
        'limitations':['B1 raw capture missing','A prices10s absent','inventory unresolved provenance for some historical runs'],
        'tests':{'command':'python -m unittest tests.test_feed_trail_revalidation tests.test_trail_activation_gap_systemic_study tests.test_trail_gap_systemic_study','passed':23}}
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')


if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True)
    if '--feed' in sys.argv:feed()
    ok=provenance()
    if '--replay' in sys.argv:
        if not ok:raise SystemExit('STOP: integrity checks failed')
        replay()
    if '--compare' in sys.argv:comparisons()
    if '--forward' in sys.argv:forward()
    if '--render' in sys.argv:render()
    if '--sensitivity' in sys.argv:feed_sensitivity()
