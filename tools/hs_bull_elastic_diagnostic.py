"""Fixed HS bull diagnosis; offline replay only, never operational writes.

The broad continuation removes only HS for at most six hours. It is a diagnostic
counterfactual, NOT the current elastic rule. The systemic variant is LON only.
OHLC paths have minute precision; no intraminute timing is inferred.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.monitor.circuit_breaker_shadow import CircuitBreakerPosition
from src.monitor.entry_engine import EntrySignal
from src.monitor.forward_experiment_shadows import ElasticPosition
from src.indicators.indicators import atr
from tools import be_off_cb_fast_drop_systemic_replay as systemic
from tools.be_off_cb_defensive_closure import compare, metrics, serialize
from tools.be_off_cb_defensive_review import Review, iso
from tools.be_off_cb_exit_context_study import brt
from tools.ge_replay_study import SignalEvent, load_ge_market_data
from tools.market_bot_replay import MINUTE_MS, NullLogger, ReplayExecutionClient, _bot_exit_config, _deduplicate
from tools.market_selection_study import BinancePublicClient

WINDOWS = (15, 30, 60, 120, 360)


def quantiles(values):
    values = sorted(v for v in values if v is not None)
    def q(p):
        if not values:
            return None
        n = (len(values)-1)*p
        lo = int(n)
        return values[lo] + (values[min(lo+1, len(values)-1)]-values[lo])*(n-lo)
    return {'n': len(values), **{name: q(p) for name, p in
            (('min', 0), ('p10', .1), ('p25', .25), ('p50', .5), ('p75', .75), ('p90', .9), ('max', 1))}}


class CapturePosition(ElasticPosition):
    def _close_at_market(self, price, reason, ts, trigger_reference):
        study = self.study
        if reason == 'HARD_STOP':
            study.captures.append({'source_candle': self.source_candle_open_time,
                'opened_ms': int(datetime.fromisoformat(self.open_ts).timestamp()*1000),
                'hs_ms': study.boundary, 'causal_at_ms': study.at,
                'trigger_price': price, 'state': deepcopy(self.to_state()),
                'remaining': study.remaining[:], 'path': study.path,
                'snapshot': study.review.context(study.at)})
        return super()._close_at_market(price, reason, ts, trigger_reference)


class ReplayAdapter:
    """OHLC adapter for the existing elastic transition, not a new strategy.

    Reevaluate context only when a new CLOSED 5m snapshot arrives; context loss
    uses that snapshot's close, just as ExperimentalRiskShadow.on_closed_5m.
    Regular exits retain the existing systemic replay's modeled stop fills.
    """
    def __init__(self, review, elastic=False):
        self.review = review
        self.elastic = elastic
        self.captures = []
        self.events = []
        self.at = self.boundary = 0
        self.remaining = []
        self.path = None

    def factory(self, *args, **kwargs):
        p = CapturePosition(*args, **kwargs)
        p.study = self
        p._seen_context_close = None
        return p

    def context_transition(self, p, snapshot):
        closed = snapshot.get('latest_closed_at_ms')
        if closed == p._seen_context_close:
            return
        p._seen_context_close = closed
        if not p.hs_elastic or snapshot.get('ema_context') == 'LON':
            return
        price = float(snapshot['close'])
        original = p._elastic_hard_stop_price
        if price <= original:
            p.client.current_price = price
            p._cb_market_ts = iso(closed)
            p._close_at_market(price, 'HARD_STOP_ELASTIC_CONTEXT_LOST', iso(closed), original)
            self.events.append({'source_candle': p.source_candle_open_time,
                'event': 'HS_ELASTIC_EXIT_CONTEXT_LOST', 'at_ms': closed})
        else:
            p.hs_elastic = False
            p.hard_stop_price = original
            p._refresh_effective_stop()
            self.events.append({'source_candle': p.source_candle_open_time,
                'event': 'HS_ELASTIC_ENDED_RECOVERED', 'at_ms': closed})

    def processor(self, positions, trades, candle, path, fees, *unused):
        self.at, self.boundary, self.path = candle.open_time_ms, candle.boundary_ms, path
        snapshot = self.review.context(self.at) if self.elastic and positions else None
        points = _deduplicate((candle.open, candle.high, candle.low, candle.close)
                    if path == 'HIGH_FIRST' else (candle.open, candle.low, candle.high, candle.close))
        for rp in list(positions):
            p = rp.position
            if p.status != 'OPEN':
                continue
            if self.elastic:
                self.context_transition(p, snapshot)
                if p.status == 'CLOSED':
                    systemic._append_trade(p, rp.opened_ms, candle.open_time_ms, fees, trades)
                    continue
            previous = None
            for i, point in enumerate(points):
                self.remaining = points[i:]
                stop = p.effective_stop
                crossed = previous is not None and previous > stop and point <= stop
                tick = stop if crossed else point
                if self.elastic and not p.hs_elastic and tick <= p._elastic_hard_stop_price and snapshot['ema_context'] == 'LON':
                    p.hs_elastic = True
                    p.hs_elastic_started_at = iso(candle.boundary_ms)
                    p.hs_original_at = p.hs_elastic_started_at
                    p.hs_original_pnl_pct = p.pnl_pct(tick)
                    p.hs_original_context = deepcopy(snapshot)
                    p.hard_stop_price = None
                    p.effective_stop, p.stop_type = p.review_stop, 'review'
                    p._refresh_effective_stop()
                    self.events.append({'source_candle': p.source_candle_open_time,
                        'event': 'HS_ELASTIC_STARTED', 'at_ms': candle.boundary_ms,
                        'causal_at_ms': self.at, 'price': tick})
                    # Finish the same descending segment after suppressing HS.
                    tick = p.effective_stop if tick > p.effective_stop and point <= p.effective_stop else point
                rp.client.current_price = tick
                p.on_tick(tick, iso(candle.boundary_ms))
                previous = point
                if p.status == 'CLOSED':
                    systemic._append_trade(p, rp.opened_ms, candle.boundary_ms, fees, trades)
                    break


def replay(config, candles, signals, review, start, end, path, elastic=False):
    study = ReplayAdapter(review, elastic)
    with patch.object(systemic, 'BotFullExitPosition', study.factory), patch.object(systemic, 'process_candle_systemic', study.processor):
        run = systemic.run_systemic(name='HS_BULL_ELASTIC' if elastic else 'BE_OFF_CB',
            config=config, signals=signals, candles=candles['1m'], contexts=[],
            start_ms=start, end_ms=end, path=path, spread_bps=review.spread, fast_enabled=False)
    return serialize(run, signals, review.notional), study


def continuation(event, review, control):
    """No portfolio feedback: broad diagnostic, HS disabled, all other exits live."""
    client = ReplayExecutionClient(review.spread/2)
    p = CircuitBreakerPosition.from_state(event['state'], _bot_exit_config(review.config), client, NullLogger())
    p.hard_stop_price = None
    # _refresh_effective_stop ratchets the previous effective stop. Explicitly
    # remove the old HS floor, as the real elastic transition does, or the
    # continuation would immediately execute the HS we intended to suppress.
    p.effective_stop, p.stop_type = p.review_stop, 'review'
    p._refresh_effective_stop()
    entry = p.entry_price
    hs = event['hs_ms']
    raw = [(hs, event['trigger_price'])]
    observations = []
    previous = event['trigger_price']
    armed = {'PL': None, 'TRAIL': None}
    closed_ms = None
    horizon_end = hs + 360*MINUTE_MS
    last = hs
    gap = False
    pieces = [(hs, event['remaining'])]
    for k in range(1, 361):
        candle = review.index.get(hs+k*MINUTE_MS)
        if candle is None:
            gap = True
            break
        pieces.append((candle.boundary_ms, _deduplicate((candle.open,candle.high,candle.low,candle.close)
                      if event['path']=='HIGH_FIRST' else (candle.open,candle.low,candle.high,candle.close))))
    for at, points in pieces:
        last = at
        for point in points:
            raw.append((at, point))
            if p.status == 'OPEN':
                stop = p.effective_stop
                tick = stop if previous > stop and point <= stop else point
                client.current_price = tick
                p.on_tick(tick, iso(at))
                if p.profit_lock_stop is not None and armed['PL'] is None:
                    armed['PL'] = at
                if p.trailing_stop is not None and armed['TRAIL'] is None:
                    armed['TRAIL'] = at
                if p.status == 'CLOSED':
                    closed_ms = at
            previous = point
        observations.append((at, p.status, p.exit_reason, p.exit_price))
    windows = []
    targets = {'HS': event['trigger_price'], '-1.0%': entry*.99, '-0.5%': entry*.995, 'ENTRY': entry}
    for minutes in WINDOWS:
        cutoff = hs+minutes*MINUTE_MS
        points = [(at, price) for at, price in raw if at <= cutoff]
        lows = min(price for _, price in points)
        highs = max(price for _, price in points)
        recovery = {}
        for name, level in targets.items():
            # A return to HS requires a prior price below it, not the initial touch.
            below_seen = name != 'HS'
            first = None
            for at, price in points[1:]:
                if price < level:
                    below_seen = True
                if below_seen and price >= level:
                    first = at
                    break
            recovery[name] = (first-hs)/MINUTE_MS if first is not None else None
        ended = closed_ms is not None and closed_ms <= cutoff
        net = review.notional*((p.exit_price/entry-1)*100-review.fees)/100 if ended else None
        windows.append({'minutes': minutes, 'complete': last >= cutoff and not (gap and last < cutoff),
            'observed_minutes': min(minutes, (last-hs)/MINUTE_MS),
            'MAE_pnl_pct': (lows/entry-1)*100, 'MFE_pnl_pct': (highs/entry-1)*100,
            'additional_drop_pp': (event['trigger_price']-lows)/entry*100,
            'recovery_times_min': recovery, 'exit_reason': p.exit_reason if ended else 'CENSORED',
            'net_usd': net, 'delta_usd': net-control['net_usd'] if ended else None,
            'extra_slot_min': (closed_ms-hs)/MINUTE_MS if ended else None})
    worst_i = min(range(len(raw)), key=lambda i: raw[i][1])
    level_times = {}
    for name, level in targets.items():
        first = next((at for at, price in raw[worst_i+1:] if price >= level), None)
        level_times[name] = None if first is None else {'from_HS_min': (first-hs)/MINUTE_MS,
            'from_worst_min': (first-raw[worst_i][0])/MINUTE_MS}
    first_recovery = next((i for i, (_, price) in enumerate(raw[1:], 1) if price >= entry*.995), None)
    depth_before_recovery = min(price for _, price in raw[:first_recovery+1]) if first_recovery is not None else None
    deteriorated_after_arming = any(armed[k] is not None and at > armed[k] and price <= event['trigger_price']
                                  for at, price in raw for k in armed)
    final = windows[-1]
    delta = final['delta_usd']
    category = ('D_RECUPERA_E_DEPOIS_DETERIORA' if deteriorated_after_arming else
                'A_ELASTIC_AJUDARIA' if delta is not None and delta > 1e-8 else
                'B_ELASTIC_SO_ADIARIA_PERDA' if delta is not None and delta < -1e-8 else 'C_AMBIGUO')
    until = closed_ms if closed_ms is not None else min(last, horizon_end)
    admitted = [r for r in review.control_trades if hs < r['opened_ms'] <= until]
    capacity = int(review.config['capital']['max_open_positions'])
    conflicts = []
    for r in admitted:
        active = sum(q['opened_ms'] <= r['opened_ms'] and (q['closed_ms'] is None or q['closed_ms'] > r['opened_ms'])
                     for q in review.control_trades)
        if active+1 > capacity:
            conflicts.append(r['source_candle'])
    snapshot = event['snapshot']
    five_end = bisect.bisect_right(review.five_boundaries, event['causal_at_ms']) if hasattr(review,'five_boundaries') else 0
    five = review.candles['5m'][max(0,five_end-300):five_end] if five_end else []
    atr_at_hs = atr([c.high for c in five],[c.low for c in five],[c.close for c in five],14)[-1] if five else None
    entry_atr = event['state'].get('entry_atr')
    return {'source_candle': event['source_candle'], 'opened_ms': event['opened_ms'], 'hs_ms': hs,
        'causal_at_ms': event['causal_at_ms'], 'context': snapshot['ema_context'], 'snapshot': snapshot,
        'entry_price': entry, 'HS_price': event['trigger_price'], 'entry_ATR': entry_atr,
        'ATR_5m_at_HS': atr_at_hs, 'age_at_HS_min': (hs-event['opened_ms'])/MINUTE_MS,
        'correction_pct': (event['trigger_price']/entry-1)*100,
        'correction_entry_ATR': (entry-event['trigger_price'])/entry_atr if entry_atr else None,
        'price_relative_EMAs_pct': {str(n): (event['trigger_price']/snapshot[f'ema{n}']-1)*100 for n in (50,100,200)},
        'windows': windows, 'category': category, 'actual_exit': p.exit_reason if closed_ms else 'CENSORED',
        'actual_closed_ms': closed_ms, 'actual_net': final['net_usd'], 'delta_usd': delta,
        'raw_worst_pct': (min(price for _,price in raw)/entry-1)*100,
        'depth_before_recovering_minus_05_pct': (depth_before_recovery/entry-1)*100 if depth_before_recovery else None,
        'recovery_after_worst': level_times, 'PL_armed_ms': armed['PL'], 'TRAIL_armed_ms': armed['TRAIL'],
        'extra_slot_min': final['extra_slot_min'], 'slot_observed_censored_min': (until-hs)/MINUTE_MS if closed_ms is None else None,
        'control_admissions_during_extension': len(admitted), 'potential_capacity_conflicts': conflicts,
        'raw_path': raw}


def summary(rows):
    delta = [r['delta_usd'] for r in rows if r['delta_usd'] is not None]
    return {'n': len(rows), 'resolved_6h': len(delta), 'censored_6h': len(rows)-len(delta),
        'categories': dict(Counter(r['category'] for r in rows)),
        'actual_exits': dict(Counter(r['actual_exit'] for r in rows)),
        'helped': sum(v>1e-8 for v in delta), 'worsened': sum(v < -1e-8 for v in delta),
        'gains': sum(v for v in delta if v>0), 'extra_losses': -sum(v for v in delta if v<0),
        'resolved_delta': sum(delta), 'delta_distribution': quantiles(delta),
        'extra_slot_min': quantiles([r['extra_slot_min'] for r in rows]),
        'additional_slot_total_min': sum(r['extra_slot_min'] or 0 for r in rows),
        'potential_capacity_conflicts': sum(len(r['potential_capacity_conflicts']) for r in rows),
        'raw_worst_pct': quantiles([r['raw_worst_pct'] for r in rows]),
        'depth_before_recovery_pct': quantiles([r['depth_before_recovering_minus_05_pct'] for r in rows]),
        'context_characteristics': {
            'age_at_HS_min': quantiles([r['age_at_HS_min'] for r in rows]),
            'entry_ATR': quantiles([r['entry_ATR'] for r in rows]),
            'ATR_5m': quantiles([r['ATR_5m_at_HS'] for r in rows]),
            'correction_entry_ATR': quantiles([r['correction_entry_ATR'] for r in rows]),
            'MACD_counts':dict(Counter(r['snapshot']['macd_context'] for r in rows)),
            'EMA50_distance_pct':quantiles([r['price_relative_EMAs_pct']['50'] for r in rows]),
            'EMA100_distance_pct':quantiles([r['price_relative_EMAs_pct']['100'] for r in rows]),
            'EMA200_distance_pct':quantiles([r['price_relative_EMAs_pct']['200'] for r in rows]),
            'MACD_line':quantiles([r['snapshot']['macd_line'] for r in rows])},
        'windows': {str(w): {'n_complete': sum(r['windows'][i]['complete'] for r in rows),
            'n_censored': sum(not r['windows'][i]['complete'] for r in rows),
            'MAE': quantiles([r['windows'][i]['MAE_pnl_pct'] for r in rows if r['windows'][i]['complete']]),
            'MFE': quantiles([r['windows'][i]['MFE_pnl_pct'] for r in rows if r['windows'][i]['complete']]),
            'recoveries': {k: sum(r['windows'][i]['recovery_times_min'][k] is not None for r in rows if r['windows'][i]['complete'])
                           for k in ('HS','-1.0%','-0.5%','ENTRY')}} for i,w in enumerate(WINDOWS)}}


def fmt(v):
    return 'N/A' if v is None else str(v) if isinstance(v, (str,int)) else f'{v:.4f}'


def table(headers, rows):
    return ['| '+' | '.join(headers)+' |', '|'+'|'.join('---' for _ in headers)+'|',
            *['| '+' | '.join(fmt(v) for v in row)+' |' for row in rows], '']


def report(out, results, manifest):
    lines = ['# HS_BULL_ELASTIC — diagnóstico causal', '',
        f"Janela BRT: {manifest['start_brt']} → {manifest['end_brt']}. Offline, inputs congelados.", '',
        'População: somente HARD_STOP efetivamente executado no controle, contexto vigente LON ou BUL; separados.',
        '## Auditoria do prior 02/10', '',
        'O resumo fornecido contém uma divergência: o artefato original tem 14 casos POR cenário, 3 PROFIT_LOCK e 1 TRAILING no contrafactual, não zero recuperações. Todos eram HS no controle.',
        'Mesmos source_candles entre os cenários: não somar como 28 trades independentes. Pior perda −4,3735%, slot adicional 1.145 min por cenário, delta isolado +$0,2261 por cenário.',
        'Fonte: be_off_cb_defensive_review/20261002/diagnostic_HIGH_FIRST.json e diagnostic_LOW_FIRST.json, campo counterfactual_exits, mechanism=HS_BULL_ELASTIC. Hashes e totais em manifest.json.', '',
        '## Dois experimentos diferentes', '',
        'Diagnóstico amplo: remove somente HS após o ponto da saída do controle, mantém estado exato de ladder/PL/Trail e review stop; observa até 6h. NÃO é uma regra proposta.',
        'Regra atual: elasticidade somente LON; reavalia em novo 5m fechado; perda de LON abaixo HS fecha ao close desse snapshot; acima HS rearma stop normal. CB, slots e admissões recalculados.',
        'Fills conforme replay sistêmico existente (stop interpolado no segmento, gap ao preço disponível, custos/spread congelados). Isso não replica ticks reais nem latência forward.', '',
        '## Economia sistêmica da regra atual', '']
    for path, result in results.items():
        lines += ['### '+path, '']
        lines += table(['mês','braço','closed','net $','PF','DD $','HS','PL','TRAIL','max sim','CB crises','cooldown h'],
            [[window, arm, m['closed'], m['net'], m['pf'], m['dd'], m['HARD_STOP']+result['context_lost_counts'].get(window,0) if arm=='ELASTIC' else m['HARD_STOP'],
              m['PROFIT_LOCK'], m['TRAILING'], m['max_sim'], m['crises'],m['cooldown_h']]
             for window in (*manifest['months'],'ALL') for arm,m in result['metrics'][window].items()])
        lines += table(['mês','delta total','delta common','control-only net','elastic-only net'],
            [[w, d['delta'], d['common_delta'], d['control_only_net'],d['variant_only_net']] for w,d in result['comparisons'].items()])
        lines += ['Episódios reais de elasticidade (não soma cenários como trades únicos): '+json.dumps(result['elastic_events'],ensure_ascii=False), '',
                  'Pares envolvidos na regra atual: '+json.dumps(result['elastic_pair_summary'],ensure_ascii=False), '']
        for ctx in ('LON','BUL'):
            group = result['summaries'][ctx]
            lines += ['### '+path+' / '+ctx, '',
                f"N={group['n']}; resolvidos em 6h={group['resolved_6h']}; censurados={group['censored_6h']}; categorias={group['categories']}",
                f"Melhoram={group['helped']}; pioram={group['worsened']}; ganhos=${group['gains']:.4f}; perdas adicionais=${group['extra_losses']:.4f}; saldo resolvido=${group['resolved_delta']:.4f}.",
                'Este saldo é por trade com admissões fixas, NÃO resultado sistêmico e exclui censurados sem liquidá-los artificialmente.', '']
            lines += table(['janela min','N completo','censurado','MAE p10','MAE p50','MAE p90','MFE p10','MFE p50','MFE p90','volta HS','−1%','−0,5%','entry'],
                [[w,g['n_complete'],g['n_censored'],*[g['MAE'][k] for k in ('p10','p50','p90')],
                  *[g['MFE'][k] for k in ('p10','p50','p90')],*[g['recoveries'][k] for k in ('HS','-1.0%','-0.5%','ENTRY')]] for w,g in group['windows'].items()])
            lines += ['Distribuição completa de quantis / mensais / por resultado no summary.json; trajetórias e milestones em diagnostics_'+path+'.json.', '',
                'Slot adicional resolvido: '+json.dumps(group['extra_slot_min'])+f"; total={group['additional_slot_total_min']:.1f} min; conflitos potenciais={group['potential_capacity_conflicts']}.",
                'Conflito potencial: controle admitiu sinal com capacidade cheia contando uma posição adicional. Não prova bloqueio real; spacing/CB também podem mudar.', '']
            lines += table(['mês do HS','N','resolvidos','ganhos $','perdas $','saldo $','slot min'],
                [[w,g['n'],g['resolved_6h'],g['gains'],g['extra_losses'],g['resolved_delta'],g['additional_slot_total_min']]
                 for w,g in result['monthly_diagnostics'][ctx].items()])
            lines += table(['resultado posterior','N','MAE min','MAE p10','MAE p50','MAE p90','MAE max','idade HS p50','ATR entry p50','MACD'],
                [[kind,g['n'],*[g['raw_worst_pct'][k] for k in ('min','p10','p50','p90','max')],
                  g['context_characteristics']['age_at_HS_min']['p50'],g['context_characteristics']['entry_ATR']['p50'],
                  str(g['context_characteristics']['MACD_counts'])] for kind,g in result['outcome_diagnostics'][ctx].items()])
            examples = sorted([r for r in result['rows'] if r['context']==ctx and r['delta_usd'] is not None],key=lambda r:r['delta_usd'])
            selected = examples[:2]+examples[-2:] if len(examples)>4 else examples
            lines += table(['source BRT','HS BRT','categoria','MAE 6h %','pior antes −0,5%','saída efetiva','delta $','slot min'],
                [[brt(r['source_candle']),brt(r['hs_ms']),r['category'],r['raw_worst_pct'],r['depth_before_recovering_minus_05_pct'],r['actual_exit'],r['delta_usd'],r['extra_slot_min']] for r in selected])
    lines += ['## Leitura e limites', '',
        'A/B/C/D são descrições, não inputs de decisão. D exige defesa PL/Trail armada e retorno posterior bruto ao HS; reporta separadamente o destino econômico efetivamente executado.',
        'Retorno ao HS exige ficar abaixo e voltar; simples toque inicial não conta. PL/Trail armado NÃO equivale a saída; destino é calculado pela engine.',
        'Continuação bruta após saída da simulação é preço observado, não exposição ainda mantida. MAE/MFE bruto e custo real de slot não devem ser confundidos.',
        'Censura: falta de candle encerra observação; não atravessa gaps nem inventa recuperação. Resultados econômicos não resolvidos não entram no saldo contrafactual.',
        'HIGH_FIRST/LOW_FIRST são dois cenários para os mesmos candles, não duas amostras independentes. Tempos intraminuto não são conhecidos.',
        'Contexto congelado na abertura do minuto, usando só 5m já fechado; warm-up de 300 candles como o market_context. Nenhum contexto de saída histórico stale é usado.',
        'A comparação ao prior usa a mesma janela e configuração congeladas, mas corrige a reavaliação para ocorrer apenas em novos 5m, sem repetir decisão a cada 1m.',
        'Não encontrar vantagem do elastic atual NÃO valida o HS atual. Recuperações retrospectivas NÃO definem política pronta.',
        'Próxima pergunta discriminante: a recuperação economicamente executável ocorre antes de perder LON, ou apenas após a perda do contexto e maior ocupação de slot? Separar esses dois tempos antes de formular outra regra.', '',
        'Nenhuma alteração de trading/YAML; sem deploy, restart, commit ou push. Testes e hashes no manifest.json.']
    (out/'REPORT.md').write_text('\n'.join(lines),encoding='utf8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT/'data/studies/hs_bull_elastic/20261009')
    parser.add_argument('--reuse-systemic', type=Path, help='Reuse completed, hash-validated systemic runs; rebuild all broad diagnostics')
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Use a new output directory; frozen studies are not overwritten')
    reference = ROOT/'data/studies/be_off_cb_defensive_closure/20261002'
    frozen = json.loads((reference/'manifest.json').read_text())
    config = deepcopy(frozen['config'])
    config.setdefault('risk',{})['breakeven'] = {'mode':'off'}
    start,end = frozen['start_ms'],frozen['end_ms']
    cache = ROOT/'data/studies/be_off_cb_deterioration/klines'
    client = BinancePublicClient('https://api.binance.com',30)
    candles = {tf:load_ge_market_data(client,'SOLUSDT',tf,start-300*15*MINUTE_MS,end,cache,True) for tf in ('1m','5m')}
    signals = [SignalEvent(r['boundary_ms'],EntrySignal(**r['signal'])) for r in json.loads((reference/'signals.json').read_text())]
    review = Review(config,candles,signals,end)
    args.output.mkdir(parents=True)
    months = ('2026-06','2026-07','2026-08','2026-09','2026-10')
    manifest = {**{k:frozen[k] for k in ('start_ms','end_ms','start_brt','end_brt')},
        'config':config,'months':months,'population':['LON','BUL'],'selection':'actual HARD_STOP only',
        'source_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
            (Path(__file__),ROOT/'src/monitor/forward_experiment_shadows.py',ROOT/'src/position/bot_full_engine.py',
             ROOT/'src/monitor/market_context.py',ROOT/'tools/be_off_cb_fast_drop_systemic_replay.py',reference/'signals.json',
             cache/'SOLUSDT_1m.jsonl',cache/'SOLUSDT_5m.jsonl')},
        'OHLC_cadence':'context frozen minute open; stops at modeled segment crossings; actual elastic new 5m only',
        'tests':'python -m unittest discover -s tests -p test_hs_bull_elastic_diagnostic.py'}
    manifest['prior_original'] = {}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        prior_path = ROOT/f'data/studies/be_off_cb_defensive_review/20261002/diagnostic_{path}.json'
        prior = [r for r in json.loads(prior_path.read_text())['counterfactual_exits'] if r['mechanism']=='HS_BULL_ELASTIC']
        manifest['prior_original'][path] = {'sha256':hashlib.sha256(prior_path.read_bytes()).hexdigest(),
            'n':len(prior),'destinations':dict(Counter(r['exit_reason'] for r in prior)),
            'delta':sum(r['delta'] or 0 for r in prior),'extra_slot_min':sum(r['extra_minutes'] or 0 for r in prior),
            'worst_pct':min(r['worst_pct'] for r in prior)}
    results = {}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        print(path+' control',flush=True)
        control, adapter = replay(config,candles,signals,review,start,end,path)
        prior_control = json.loads((reference/(path+'_BE_OFF_CB.json')).read_text())
        # A frozen reference prevents silently changing control economics.
        for a,b in zip(control['trades'],prior_control['trades']):
            for key in ('source_candle','opened_ms','closed_ms','exit_reason','entry_price','exit_price','net_usd'):
                if key not in a or key not in b: continue
                if isinstance(a[key],(int,float)) and isinstance(b[key],(int,float)):
                    assert abs(a[key]-b[key])<=1e-8, (path,key,a[key],b[key])
                else: assert a[key]==b[key], (path,key,a[key],b[key])
        assert len(control['trades'])==len(prior_control['trades'])
        review.control_trades = control['trades']
        control_map = {r['source_candle']:r for r in control['trades']}
        broad = [e for e in adapter.captures if e['snapshot']['ema_context'] in ('LON','BUL')]
        print(path+f' broad HS population {len(broad)}',flush=True)
        rows = [continuation(e,review,control_map[e['source_candle']]) for e in broad]
        print(path+' systemic current elastic',flush=True)
        if args.reuse_systemic:
            previous = json.loads((args.reuse_systemic/'manifest.json').read_text())
            assert previous['config']==config
            for source,digest in previous['source_hashes'].items():
                if source != str(Path(__file__).relative_to(ROOT)):
                    assert hashlib.sha256((ROOT/source).read_bytes()).hexdigest()==digest, source
            elastic = json.loads((args.reuse_systemic/('elastic_'+path+'.json')).read_text())
            current = SimpleNamespace(events=json.loads((args.reuse_systemic/('events_'+path+'.json')).read_text()))
            manifest['reused_systemic_from']=str(args.reuse_systemic)
        else:
            elastic, current = replay(config,candles,signals,review,start,end,path,True)
        elastic_map = {r['source_candle']:r for r in elastic['trades']}
        sources = {e['source_candle'] for e in current.events if e['event']=='HS_ELASTIC_STARTED'}
        pairs = []
        for source in sorted(sources):
            a,b = control_map.get(source),elastic_map[source]
            pairs.append({'source_candle':source,'control_exit':a['exit_reason'] if a else 'NO_MATCH',
                'elastic_exit':b['exit_reason'],'delta':b['net_usd']-a['net_usd'] if a and a['net_usd'] is not None and b['net_usd'] is not None else None,
                'additional_slot_min': (b['closed_ms']-a['closed_ms'])/MINUTE_MS if a and a['closed_ms'] and b['closed_ms'] else None})
        result = {'rows':rows,'summaries':{ctx:summary([r for r in rows if r['context']==ctx]) for ctx in ('LON','BUL')},
            'monthly_diagnostics':{ctx:{w:summary([r for r in rows if r['context']==ctx and brt(r['hs_ms'])[:7]==w]) for w in months} for ctx in ('LON','BUL')},
            'outcome_diagnostics':{ctx:{kind:summary([r for r in rows if r['context']==ctx and
                  ('RECOVERED' if r['depth_before_recovering_minus_05_pct'] is not None else 'NO_OBSERVED_RECOVERY')==kind])
                for kind in ('RECOVERED','NO_OBSERVED_RECOVERY')} for ctx in ('LON','BUL')},
            'economic_groups':{ctx:{kind:summary([r for r in rows if r['context']==ctx and
                ('CENSORED' if r['delta_usd'] is None else 'IMPROVED' if r['delta_usd']>1e-8 else 'WORSENED' if r['delta_usd'] < -1e-8 else 'UNCHANGED')==kind])
                for kind in ('IMPROVED','WORSENED','UNCHANGED','CENSORED')} for ctx in ('LON','BUL')},
            'metrics':{w:{'CONTROL':metrics(control,w),'ELASTIC':metrics(elastic,w)} for w in (*months,'ALL')},
            'comparisons':{w:compare(control,elastic,w) for w in (*months,'ALL')},
            'context_lost_counts':{w:sum(r['exit_reason']=='HARD_STOP_ELASTIC_CONTEXT_LOST' and (w=='ALL' or brt(r['closed_ms'])[:7]==w) for r in elastic['trades']) for w in (*months,'ALL')},
            'elastic_events':dict(Counter(e['event'] for e in current.events)),
            'elastic_pairs':pairs,'elastic_pair_summary':{'n':len(pairs),
                'control_destinations':dict(Counter(p['control_exit'] for p in pairs)),
                'elastic_destinations':dict(Counter(p['elastic_exit'] for p in pairs)),
                'saved_PL_TRAIL':sum(p['elastic_exit'] in ('PROFIT_LOCK','TRAILING') for p in pairs),
                'improved':sum(p['delta'] is not None and p['delta']>1e-8 for p in pairs),
                'worsened':sum(p['delta'] is not None and p['delta'] < -1e-8 for p in pairs),
                'additional_slot_total_min':sum(p['additional_slot_min'] or 0 for p in pairs)}}
        for name,value in (('control',control),('elastic',elastic),('diagnostics',rows),('events',current.events)):
            (args.output/(name+'_'+path+'.json')).write_text(json.dumps(value,ensure_ascii=False),encoding='utf8')
        results[path]=result
        print(path+f" delta systemic {result['comparisons']['ALL']['delta']:+.4f}",flush=True)
    manifest['control_parity']='PASS both paths at 1e-8'
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    compact = {path:{k:v for k,v in r.items() if k!='rows'} for path,r in results.items()}
    (args.output/'summary.json').write_text(json.dumps(compact,indent=2),encoding='utf8')
    report(args.output,results,manifest)
    print(args.output/'REPORT.md',flush=True)


if __name__ == '__main__':
    main()
