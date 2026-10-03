"""Final fixed systemic validation and original-ledger audit; no runtime writes."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config_profiles import effective_config
from src.monitor.entry_engine import EntrySignal
from tools.be_off_cb_defensive_review import Review, hs_clusters
from tools.be_off_cb_deterioration_study import _signals, build_context_index
from tools.be_off_cb_exit_context_study import brt, exit_context
from tools.be_off_cb_fast_drop_systemic_replay import run_systemic
from tools.cohort_study import _load_config
from tools.ge_replay_study import SignalEvent, WARMUP_CANDLES, load_ge_market_data
from tools.market_bot_replay import MINUTE_MS
from tools.market_selection_study import BinancePublicClient, load_candle_cache

OUT = ROOT / 'data/studies/be_off_cb_defensive_closure/20261002'
CACHE = ROOT / 'data/studies/be_off_cb_deterioration/klines'
MONTHS = ('2026-06', '2026-07', '2026-08', '2026-09', '2026-10')
ARMS = ('BE_OFF_CB', 'FAST_DROP_EMA', 'HS_PAUSE_1H')


def month(at):
    return brt(at)[:7]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ms(text):
    return int(datetime.fromisoformat(text).timestamp()*1000)


def table(headers, rows):
    return ['| '+' | '.join(headers)+' |', '|'+'|'.join('---' for _ in headers)+'|',
            *['| '+' | '.join(str(v) for v in row)+' |' for row in rows], '']


def fmt(value):
    if value is None:
        return 'N/A'
    return 'inf' if math.isinf(value) else f'{value:.4f}'


def audit_forward(candles, settings):
    """Keep persisted and reconstructed versions distinct; never rewrite a ledger."""
    original = OUT/'forward_input'
    boundaries = [c.boundary_ms for c in candles]
    rows = []
    for filename in ('trades_be_off_cb_shadow.jsonl', 'trades_hs_bear_cluster_exit_shadow.jsonl'):
        path = original/filename
        for line in path.read_text(encoding='utf-8').splitlines():
            r = json.loads(line)
            if r.get('exit_reason') != 'HARD_STOP' or not str(r.get('closed_at', '')).startswith('2026-10-01'):
                continue
            at = ms(r['closed_at'])
            entry = r['market_context_entry']['tf_5m']
            recorded = r['market_context_exit']['tf_5m']
            current = exit_context(candles, boundaries, at+MINUTE_MS, settings)
            keys = ('ema_context', 'macd_context', 'latest_open_at_ms', 'latest_closed_at_ms',
                    'previous_open_at_ms', 'previous_closed_at_ms', 'ema50', 'ema100', 'ema200',
                    'ema50_direction', 'ema100_direction', 'ema200_direction', 'macd_line', 'macd_line_previous')
            rows.append({'file':filename, 'file_sha256':digest(path), 'pair_id':r['pair_id'],
                         'source_candle':r['source_candle_open_time'], 'opened_at':brt(ms(r['opened_at'])),
                         'closed_at':brt(at), 'entry':{k:entry.get(k) for k in keys},
                         'recorded_exit':{k:recorded.get(k) for k in keys},
                         'reconstructed_exit':{k:current.get(k) for k in keys},
                         'recorded_is_latest':recorded['latest_closed_at_ms']==current['latest_closed_at_ms']})
    (OUT/'forward_context_audit.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
    lines = ['# Sanidade do cluster forward 01/10', '',
             'Fontes originais copiadas em leitura da VPS; hashes no JSON. Horários BRT.',
             'BUL/BU− na entrada está correto. BUL/BU− na saída do controle é um snapshot desatualizado, NÃO o contexto vigente.',
             'Os três registros HS_BEAR e a reconstrução oficial com candles fechados confirmam BEA/BE− nas três saídas.', '',
             '## Causa e impacto', '',
             '`src/app.py` atualiza `on_closed_5m` somente em `forward_experiment_shadows`; BE_OFF_CB é instância separada.',
             '`CircuitBreakerShadow.on_kline` não atualiza contexto; `_run_input(signal)` atualiza somente quando chega uma oportunidade. `_process_tick` copia `latest_market_context` para o exit.',
             'No controle, o snapshot copiado nos três exits é 03:15–03:20. O relatório forward lê esse campo persistido e verifica ausência de futuro, mas não verifica se ele é o mais recente.',
             'O estudo anterior `be_off_cb_defensive_review_report.forward_case` também leu esse campo sem verificar recência, reproduzindo BUL/BU− em `defensive_audit.md`, `forward_oct01.json` e `CONCLUSOES.md`.',
             'Esses artefatos anteriores devem ser considerados retificados por ESTA auditoria; não foram sobrescritos. O bug é de atualização/persistência da telemetria do controle, não da fórmula EMA/MACD.',
             'Não altera o HS de preço nem o CB realizado do controle, cuja admissão não usa esses contextos. Pode invalidar análises históricas de contexto de saída que confiem cegamente nesse campo.',
             'HS_BEAR recebe atualizações contínuas e não disparou porque BEA não é SHO. A afirmação anterior de que SHO+BEA também não capturaria este caso era incorreta.',
             'Não foi localizado um artefato original que atribua MDO a estes três source_candles nas saídas exatas; não é possível atribuir sua origem sem esse arquivo. BEA/BE− é a versão reproduzida e confirmada aqui.', '',
             '## Trades e snapshots', '']
    lines += table(['arquivo','source BRT','entry BRT','exit BRT','entry EMA/MACD','entry 5m open','exit persistido','exit 5m open persistido','exit vigente','exit 5m open vigente'],
                   [[r['file'],brt(r['source_candle']),r['opened_at'],r['closed_at'],
                     r['entry']['ema_context']+'/'+r['entry']['macd_context'],brt(r['entry']['latest_open_at_ms']),
                     r['recorded_exit']['ema_context']+'/'+r['recorded_exit']['macd_context'],brt(r['recorded_exit']['latest_open_at_ms']),
                     r['reconstructed_exit']['ema_context']+'/'+r['reconstructed_exit']['macd_context'],brt(r['reconstructed_exit']['latest_open_at_ms'])] for r in rows])
    (OUT/'forward_context_audit.md').write_text('\n'.join(lines),encoding='utf-8')
    return rows


def serialize(run, signals, notional):
    sources = {s.boundary_ms:s.signal.source_candle_open_time for s in signals}
    if len(sources) != len(signals):
        raise ValueError('This source pairing requires unique signal boundaries')
    trades = [{**asdict(t), 'source_candle':sources[t.opened_ms], 'net_usd':t.net_pct*notional/100}
              for t in run.result.trades]
    for p in run.result.open_positions:
        trades.append({'opened_ms':p.opened_ms,'closed_ms':None,'source_candle':p.position.source_candle_open_time,
                       'net_usd':None,'exit_reason':'OPEN','entry_price':p.position.entry_price})
    return {'trades':trades,'crises':run.guard.crisis_starts,'cb_paused':sorted(run.guard.paused_boundaries),
            'hs_paused':sorted(run.hs_pause_boundaries),'hs_pause_intervals':run.hs_pause_intervals,
            'admissions':run.admission_audit,'max_sim':run.result.max_simultaneous_positions,
            'max_sim_month':run.simultaneous_by_month,
            'blocked_slots':run.result.blocked_slots,'blocked_spacing':run.result.blocked_spacing,
            'blocked_candle':run.result.blocked_candle_limit}


def metrics(run, window):
    selected = lambda at: window == 'ALL' or month(at) == window
    trades = sorted((t for t in run['trades'] if t['closed_ms'] is not None and selected(t['closed_ms'])),
                    key=lambda t:t['closed_ms'])
    values = [t['net_usd'] for t in trades]
    equity = peak = dd = 0.
    for v in values:
        equity += v
        peak = max(peak, equity)
        dd = max(dd, peak-equity)
    gains = sum(v for v in values if v>0)
    losses = -sum(v for v in values if v<0)
    counts = Counter(t['exit_reason'] for t in trades)
    decisions = [a for a in run['admissions'] if selected(a['at_ms'])]
    cb_paused = set(run['cb_paused'])
    return {'closed':len(trades),'net':sum(values),'net_trade':sum(values)/len(values) if values else None,
            'pf':gains/losses if losses else math.inf if gains else None,'dd':dd,
            **{reason:counts[reason] for reason in ('HARD_STOP','PROFIT_LOCK','TRAILING','FAST_DROP')},
            'crises':sum(selected(t) for t in run['crises']),
            'cooldown_h':sum(selected(t) for t in run['cb_paused'])/60,
            'hs_pause_h':sum(selected(t) for t in run['hs_paused'])/60,
            'hs_pause_exclusive_h':sum(selected(t) and t not in cb_paused for t in run['hs_paused'])/60,
            'blocked_cb':sum(a['decision']=='CB' for a in decisions),
            'blocked_pause':sum(a['decision']=='HS_PAUSE' for a in decisions),
            'pause_overlap_cb_signals':sum(a['decision']=='CB' and a['hs_pause_active'] for a in decisions),
            'blocked_capacity':sum(a['decision']=='CAPACITY' for a in decisions),
            'blocked_spacing':sum(a['decision']=='SPACING' for a in decisions),
            'max_sim':run['max_sim'] if window=='ALL' else run['max_sim_month'].get(window,0),
            'median_age':statistics.median((t['closed_ms']-t['opened_ms'])/60000 for t in trades) if trades else None,
            'hs_clusters':len(hs_clusters([t for t in run['trades'] if t['closed_ms'] is not None and selected(t['closed_ms'])]))}


def compare(control, variant, window):
    # Paired attribution by source, monthly contribution by each arm's real exit
    # month. This keeps decomposition exact even if an exit crosses a month.
    left={r['source_candle']:r for r in control['trades']}
    right={r['source_candle']:r for r in variant['trades']}
    common=set(left)&set(right)
    value=lambda r: (r['net_usd'] or 0) if r['closed_ms'] is not None and (window=='ALL' or month(r['closed_ms'])==window) else 0
    common_delta=sum(value(right[s])-value(left[s]) for s in common)
    control_only=sum(value(left[s]) for s in set(left)-set(right))
    variant_only=sum(value(right[s]) for s in set(right)-set(left))
    disappeared=[left[s] for s in set(left)-set(right) if left[s]['closed_ms'] is not None and (window=='ALL' or month(left[s]['closed_ms'])==window)]
    new=[right[s] for s in set(right)-set(left) if right[s]['closed_ms'] is not None and (window=='ALL' or month(right[s]['closed_ms'])==window)]
    fast=[r for r in variant['trades'] if r['exit_reason']=='FAST_DROP' and (window=='ALL' or month(r['closed_ms'])==window)]
    return {'common':len(common),'control_only':len(set(left)-set(right)),'variant_only':len(set(right)-set(left)),
            'common_delta':common_delta,'control_only_net':control_only,'variant_only_net':variant_only,
            'delta':common_delta+variant_only-control_only,
            'control_only_destinations':dict(Counter(r['exit_reason'] for r in disappeared)),
            'variant_only_destinations':dict(Counter(r['exit_reason'] for r in new)),
            'fast_control_destinations':dict(Counter(left[r['source_candle']]['exit_reason'] if r['source_candle'] in left else 'NO_MATCH' for r in fast))}


def blocked_destinations(run, control, review, path):
    admitted={t['source_candle']:t for t in control['trades']}
    output=[]
    for a in run['admissions']:
        if a['decision']!='HS_PAUSE':
            continue
        corresponding=admitted.get(a['source_candle'])
        if corresponding:
            destination=corresponding['exit_reason']
            net=corresponding['net_usd']
            origin='CONTROL_ADMITTED'
        else:
            hypothetical=review.standalone(a['at_ms'],path)
            destination=hypothetical['exit_reason']
            net=hypothetical['net_usd']
            origin='ISOLATED_SIGNAL_NO_CONTROL_ADMISSION'
        output.append({**a,'destination':destination,'net':net,'origin':origin})
    return output


def report(results, metadata, outcomes):
    lines=['# Fechamento defensivo — replays sistêmicos fixos', '',
           f"Período BRT: {metadata['start_brt']} → {metadata['end_brt']}. Outubro parcial.",
           'Controle e variantes contínuos, sem resets mensais; mesmas oportunidades exógenas e configuração efetiva atual.',
           'Cada braço reexecuta posições, slots, spacing, CB e admissões. Capital/sizing e PL/TRAIL iguais ao controle.',
           'FAST usa as funções corrigidas já compartilhadas com o forward. Pausa pós-HS existe apenas no replay; exatamente 60min, começa no closed_ms do HS modelado (resolução OHLC 1m).',
           'Contexto intraminuto congelado no início do minuto; fills FAST teóricos no cruzamento, spread/fees do modelo. Não reproduz overshoot/latência de ticks reais.',
           'Meses das métricas econômicas são meses do fechamento BRT. DD mensal inicia em zero; DD agregado é equity realizada contínua, sem MTM.',
           'Horas CB/pausa são uniões de minutos; pausa pode se sobrepor ao CB. Contagem específica de sinais pausa exclui os já bloqueados pelo CB.',
           'HIGH/LOW são sensibilidades da mesma amostra. Nenhum threshold ou duração alternativa foi testado.', '']
    summaries={}
    for path,runs in results.items():
        summaries[path]={}
        lines += [f'## {path}', '']
        rows=[]
        for window in (*MONTHS,'ALL'):
            summaries[path][window]={}
            for arm,run in runs.items():
                m=metrics(run,window);summaries[path][window][arm]=m
                rows.append([window,arm,m['closed'],fmt(m['net']),fmt(m['net_trade']),fmt(m['pf']),fmt(m['dd']),
                             m['HARD_STOP'],m['PROFIT_LOCK'],m['TRAILING'],m['FAST_DROP'],m['crises'],
                             f"{m['cooldown_h']:.2f}",m['blocked_cb'],f"{m['hs_pause_h']:.2f}",m['blocked_pause'],m['max_sim']])
        lines += table(['mês','arm','closed','net $','net/trade $','PF','DD $','HS','PL','TRAIL','FAST','CB crises','CB h','CB signals','HS pause h','pause signals','max sim'],rows)
        rows=[]
        for arm in ARMS[1:]:
            for window in (*MONTHS,'ALL'):
                c=compare(runs['BE_OFF_CB'],runs[arm],window)
                summaries[path][window][arm]['comparison']=c
                rows.append([window,arm,fmt(c['delta']),fmt(c['common_delta']),fmt(c['control_only_net']),
                             fmt(c['variant_only_net']),json.dumps(c['fast_control_destinations']),
                             json.dumps(c['control_only_destinations']),json.dumps(c['variant_only_destinations'])])
        lines += ['### Atribuição por source_candle', '',
                  'delta = common delta + variant-only net − control-only net. NO MATCH não é HS evitado. Control-only HS não prova sozinho causalidade: admissões/CB divergem.', '']
        lines += table(['mês','arm','delta $','common delta $','control-only net $','variant-only net $','FAST destino controle','control-only destinos','variant-only destinos'],rows)
        lines += ['### Pausa: destino dos sinais negados', '',
                  'CONTROL_ADMITTED é pareamento real no replay controle. Demais sinais são clones isolados sem slots/CB; seus nets NÃO são adicionados ao delta sistêmico.', '']
        rows=[]
        for window in (*MONTHS,'ALL'):
            for origin in ('CONTROL_ADMITTED','ISOLATED_SIGNAL_NO_CONTROL_ADMISSION'):
                selected=[a for a in outcomes[path] if a['origin']==origin and (window=='ALL' or month(a['at_ms'])==window)]
                rows.append([window,origin,len(selected),json.dumps(dict(Counter(a['destination'] for a in selected))),
                             sum(a.get('otherwise_admissible',False) for a in selected)])
        lines += table(['mês sinal','origem','N','destinos','passaria slots/spacing sem pausa'],rows)
        rows=[]
        for window in (*MONTHS,'ALL'):
            for arm in ARMS:
                m=summaries[path][window][arm]
                rows.append([window,arm,m['hs_clusters'],m['blocked_capacity'],m['blocked_spacing'],
                             f"{m['hs_pause_exclusive_h']:.2f}",m['pause_overlap_cb_signals']])
        lines += ['### Trajetória e sobreposição', '']
        lines += table(['mês','arm','clusters HS','capacity blocks','spacing blocks','pause h fora CB','signals pause∩CB'],rows)
    (OUT/'summary.json').write_text(json.dumps(summaries,indent=2),encoding='utf-8')
    (OUT/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    return summaries


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline',action='store_true')
    args=parser.parse_args()
    OUT.mkdir(parents=True,exist_ok=True)
    config=deepcopy(effective_config(_load_config(ROOT/'config/config.yaml')))
    config.setdefault('risk',{})['breakeven']={'mode':'off'}
    start=ms('2026-06-01T00:00:00-03:00')
    manifest=OUT/'manifest.json'
    end=json.loads(manifest.read_text())['end_ms'] if manifest.exists() else int(datetime.now(timezone.utc).timestamp()*1000)//MINUTE_MS*MINUTE_MS
    paths=['tools/be_off_cb_defensive_closure.py','tools/be_off_cb_fast_drop_systemic_replay.py',
           'tools/ge_replay_study.py','tools/be_off_cb_fast_drop_audit.py','tools/real_a_circuit_breaker_replay.py',
           'tools/be_off_cb_deterioration_study.py','src/monitor/fast_drop_semantics.py','src/monitor/market_context.py',
           'src/position/bot_full_engine.py','src/indicators/indicators.py']
    metadata={'start_ms':start,'end_ms':end,'start_brt':brt(start),'end_brt':brt(end),'config':config,
              'source_hashes':{p:digest(ROOT/p) for p in paths},'config_yaml_sha256':digest(ROOT/'config/config.yaml')}
    if manifest.exists() and json.loads(manifest.read_text())!=metadata:
        raise SystemExit('Frozen inputs differ: do not mix checkpoint versions')
    manifest.write_text(json.dumps(metadata,indent=2),encoding='utf-8')
    client=BinancePublicClient(str(config.get('market_data',{}).get('rest_url') or 'https://api.binance.com'),30)
    candles={}
    for interval in ('1m','5m','15m'):
        print('Load',interval,brt(end),flush=True)
        candles[interval]=load_ge_market_data(client,str(config.get('symbol') or 'SOLUSDT'),interval,
            start-WARMUP_CANDLES*15*MINUTE_MS,end,CACHE,args.offline)
    coverage={i:{'n':len(c),'first':brt(c[0].open_time_ms),'last':brt(c[-1].boundary_ms),
                 'cache_sha256':digest(CACHE/f'SOLUSDT_{i}.jsonl')} for i,c in candles.items()}
    coverage_file=OUT/'coverage.json'
    if coverage_file.exists() and json.loads(coverage_file.read_text())!=coverage:
        raise SystemExit('Candle coverage changed: do not mix checkpoints')
    coverage_file.write_text(json.dumps(coverage,indent=2),encoding='utf-8')
    audit_forward(candles['5m'],config.get('instrumentation',{}).get('market_context',{}))
    print('Forward divergence resolved, generate signals',flush=True)
    signal_file=OUT/'signals.json'
    if signal_file.exists():
        signals=[SignalEvent(s['boundary_ms'],EntrySignal(**s['signal'])) for s in json.loads(signal_file.read_text())]
    else:
        signals=_signals(config,candles,start,end)
        signal_file.write_text(json.dumps([asdict(s) for s in signals]),encoding='utf-8')
    contexts=build_context_index(candles['5m'])
    review=Review(config,candles,signals,end)
    results={};outcomes={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        results[path]={}
        for arm in ARMS:
            checkpoint=OUT/f'{path}_{arm}.json'
            if checkpoint.exists():
                run=json.loads(checkpoint.read_text())
            else:
                print('Systemic',path,arm,flush=True)
                systemic=run_systemic(name=f'{arm}_{path}',config=config,signals=signals,candles=candles['1m'],
                    contexts=contexts,start_ms=start,end_ms=end,path=path,spread_bps=review.spread,
                    fast_enabled=arm=='FAST_DROP_EMA',hs_pause_minutes=60 if arm=='HS_PAUSE_1H' else 0)
                run=serialize(systemic,signals,review.notional)
                checkpoint.write_text(json.dumps(run),encoding='utf-8')
            results[path][arm]=run
            print(path,arm,metrics(run,'ALL'),flush=True)
        outcome_file=OUT/f'{path}_pause_signal_destinations.json'
        if outcome_file.exists():
            outcomes[path]=json.loads(outcome_file.read_text())
        else:
            outcomes[path]=blocked_destinations(results[path]['HS_PAUSE_1H'],results[path]['BE_OFF_CB'],review,path)
            outcome_file.write_text(json.dumps(outcomes[path]),encoding='utf-8')
    report(results,metadata,outcomes)
    print('DONE',OUT,flush=True)


if __name__=='__main__':
    main()
