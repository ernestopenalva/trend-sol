"""Isolated post-exit diagnosis, not an alternative strategy replay.

Baseline admissions/exits reuse the existing BE_OFF_CB full-engine replay.
No live configuration, state or runtime is modified.
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import statistics
import sys
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.monitor.market_context import MarketContextEngine
from tools.be_off_cb_deterioration_study import COMBO_RULE, _signals
from tools.cohort_study import _load_config
from tools.ge_replay_study import WARMUP_CANDLES, load_ge_market_data, run_universe
from tools.market_bot_replay import MINUTE_MS
from tools.market_selection_study import BinancePublicClient
from tools.real_a_circuit_breaker_replay import CircuitGuard

HORIZONS = (5, 15, 30, 60)
MONTHS = ('2026-06', '2026-07', '2026-08', '2026-09')
CONTEXTS = {
    'ema_context': ('LON', 'BUL', 'MUP', 'MDO', 'BEA', 'SHO', 'MIX', 'UNAVAILABLE'),
    'macd_context': ('BU+', 'BU-', 'BE+', 'BE-', 'UNAVAILABLE'),
    'histogram_state': ('POSITIVE_EXPANDING', 'POSITIVE_CONTRACTING', 'NEGATIVE_RISING', 'NEGATIVE_FALLING', 'FLAT', 'UNAVAILABLE'),
}


def brt(ms):
    return datetime.fromtimestamp(ms / 1000, BRASILIA_TZ).isoformat()


def exit_context(candles, boundaries, closed_ms, settings):
    # Replay ticks share a minute-end timestamp, but intraminute OHLC points
    # occurred earlier. Freeze context at the START of that exit minute.
    cutoff = closed_ms - MINUTE_MS
    end = bisect.bisect_right(boundaries, cutoff)
    eligible = candles[max(0, end - 300):end]
    telemetry = MarketContextEngine.__new__(MarketContextEngine)
    telemetry.settings = settings
    snapshot = telemetry._timeframe_snapshot([
        SimpleNamespace(open_time=c.open_time_ms, close_time=c.close_time_ms,
                        open=c.open, high=c.high, low=c.low, close=c.close,
                        volume=c.quote_volume, closed=True) for c in eligible
    ], '5m')
    snapshot['causal_cutoff_ms'] = cutoff
    hist, prev = snapshot['macd_histogram'], snapshot['macd_histogram_previous']
    if hist is None or prev is None:
        state = 'UNAVAILABLE'
    elif hist == prev or hist == 0:
        state = 'FLAT'
    elif hist > 0:
        state = 'POSITIVE_EXPANDING' if hist > prev else 'POSITIVE_CONTRACTING'
    else:
        state = 'NEGATIVE_RISING' if hist > prev else 'NEGATIVE_FALLING'
    snapshot['histogram_state'] = state
    snapshot['histogram_direction'] = ('UNAVAILABLE' if hist is None or prev is None else
                                      'UP' if hist > prev else 'DOWN' if hist < prev else 'FLAT')
    return snapshot


def posterior(trade, minute_index, notional):
    """Cumulative future windows; exclude exit candle, require every minute."""
    output = {}
    for horizon in HORIZONS:
        candles = [minute_index.get(trade.closed_ms + k * MINUTE_MS) for k in range(horizon)]
        if any(c is None for c in candles):
            output[str(horizon)] = None
            continue
        favorable = max(0.0, (max(c.high for c in candles) / trade.exit_price - 1) * 100)
        adverse = min(0.0, (min(c.low for c in candles) / trade.exit_price - 1) * 100)
        hs_times = [k + 1 for k, c in enumerate(candles) if c.low <= trade.entry_price * .985]
        lower_closes = [k + 1 for k, c in enumerate(candles) if c.close < trade.exit_price]
        # Fixed entry quantity, mark-to-price potential only; not an extra net.
        output[str(horizon)] = {
            'favorable_pct': favorable, 'adverse_pct': adverse,
            'potential_additional_usd': notional * trade.exit_price / trade.entry_price * favorable / 100,
            'potential_giveback_usd': notional * trade.exit_price / trade.entry_price * -adverse / 100,
            'original_hs_touched': bool(hs_times),
            'first_original_hs_touch_min': min(hs_times) if hs_times else None,
            'first_close_below_exit_min': min(lower_closes) if lower_closes else None,
        }
    return output


def summarize(rows):
    result = {'n': len(rows), 'windows': {}}
    for h in HORIZONS:
        values = [r['post'][str(h)] for r in rows if r['post'][str(h)] is not None]
        result['windows'][str(h)] = {
            'n': len(values),
            **{key: statistics.median([v[key] for v in values]) if values else None
               for key in ('favorable_pct', 'adverse_pct', 'potential_additional_usd', 'potential_giveback_usd')},
            'original_hs_touch_rate': sum(v['original_hs_touched'] for v in values) / len(values) if values else None,
            'first_lower_close_n': sum(v['first_close_below_exit_min'] is not None for v in values),
            'first_lower_close_median_min': statistics.median([v['first_close_below_exit_min'] for v in values if v['first_close_below_exit_min'] is not None]) if any(v['first_close_below_exit_min'] is not None for v in values) else None,
        }
    return result


def pattern_status(months):
    # Descriptive balance of separate extrema, NOT realizable PnL or an oracle.
    if any(s['n'] < 10 for s in months):
        return 'amostra insuficiente'
    balances = [s['windows']['60']['favorable_pct'] + s['windows']['60']['adverse_pct'] for s in months]
    if all(v > 0 for v in balances) or all(v < 0 for v in balances):
        return 'consistente'
    if sum(v > 0 for v in balances) == 2:
        return 'contraditório'
    return 'fraco'


def aggregate(rows):
    groups = []
    for path in ('HIGH_FIRST', 'LOW_FIRST'):
        for reason in ('PROFIT_LOCK', 'TRAILING'):
            base = [r for r in rows if r['path'] == path and r['exit_reason'] == reason]
            for dimension, labels in {'overall': ('ALL',), **CONTEXTS}.items():
                for label in labels:
                    subset = base if dimension == 'overall' else [r for r in base if r[dimension] == label]
                    summaries = {m: summarize([r for r in subset if r['month'] == m]) for m in MONTHS}
                    groups.append({'path': path, 'reason': reason, 'dimension': dimension, 'context': label,
                                   'months': summaries, 'aggregate': summarize(subset),
                                   'status': pattern_status(list(summaries.values()))})
    return groups


def fmt(value):
    return 'N/A' if value is None else f'{value:.3f}'


def markdown(groups, manifest):
    lines = ['# BE_OFF_CB — diagnóstico de PROFIT_LOCK e TRAILING', '',
             'Junho–setembro de 2026, por mês da saída BRT; baseline contínuo, sem resets mensais.',
             'HIGH_FIRST e LOW_FIRST são sensibilidades da mesma amostra, não trades independentes.',
             '', '## Metodologia e limites', '',
             '- Somente contexto causal: último 5m fechado antes/início do minuto da saída; buffer 300, funções oficiais de market_context.',
             '- Replay OHLC 1m não conhece o segundo da saída. Contexto conservador no início do minuto; movimentos posteriores começam no minuto seguinte.',
             '- MACD 12/26/9, EMA com seed SMA conforme projeto; histogram = line − signal; direção contra 5m anterior.',
             '- Janelas cumulativas 5/15/30/60m; F = máximo favorável %, A = pior adverso % em relação ao fill de saída; medianas.',
             '- Extremos favorável/adverso não são simultâneos nem lucro realizável. Potenciais $ mantêm quantidade de entrada, sem novos custos.',
             '- HS touch é toque posterior no nível entry × 0,985, NÃO previsão de destino HARD_STOP.',
             '- Baseline usa CB/slots/admissões existentes. Diagnóstico posterior NÃO reexecuta alternativa, CB, slots, cooldown ou equity.',
             '- Status: insuficiente se qualquer mês N<10; consistente se saldo descritivo mediana F60 + mediana A60 tem mesmo sinal nos quatro meses; 2/2 contraditório; restante fraco. Não é teste estatístico nem prova de elasticidade.',
             '- Nenhum grid, nova regra ou cruzamento automático EMA×MACD; subgrupos de histograma são descritivos separados.',
             '- Precedente informado: elasticidade fixa do trailing lab foi inferior/DD maior; não foi retestada, nem extrapolada como rejeição de condicionamento por contexto.',
             '', 'Manifesto/configuração congelada: `manifest.json` (hashes, contagens, custos e parâmetros).', '']
    for g in groups:
        if g['aggregate']['n'] == 0:
            continue
        lines += [f"## {g['path']} · {g['reason']} · {g['dimension']}={g['context']}", '',
                  f"Padrão descritivo: **{g['status']}**.", '',
                  '| período | N | F5 / A5 % | F15 / A15 % | F30 / A30 % | F60 / A60 % | HS touch15/30/60 % | potencial adicional / devolução $ 60m | deterioração: N / mediana min |',
                  '|---|---:|---|---|---|---|---|---|---|']
        for period, s in [*g['months'].items(), ('ALL', g['aggregate'])]:
            w = s['windows']
            cells = [f"{fmt(w[str(h)]['favorable_pct'])} / {fmt(w[str(h)]['adverse_pct'])}" for h in HORIZONS]
            touches = ' / '.join(fmt(w[str(h)]['original_hs_touch_rate'] * 100) if w[str(h)]['original_hs_touch_rate'] is not None else 'N/A' for h in (15,30,60))
            lines.append(f"| {period} | {s['n']} | " + ' | '.join(cells) + f" | {touches} | {fmt(w['60']['potential_additional_usd'])} / {fmt(w['60']['potential_giveback_usd'])} | {w['60']['first_lower_close_n']} / {fmt(w['60']['first_lower_close_median_min'])} |")
        lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--output-dir', default='data/studies/be_off_cb_exit_context/20261002')
    args = parser.parse_args()
    output = Path(args.output_dir)
    if output.exists() and any(output.iterdir()):
        raise SystemExit('Output directory not empty: preserve previous study; choose another directory.')
    config = deepcopy(effective_config(_load_config(ROOT / 'config/config.yaml')))
    config.setdefault('risk', {})['breakeven'] = {'mode': 'off'}
    start = int(datetime.fromisoformat('2026-06-01T00:00:00-03:00').timestamp() * 1000)
    end = int(datetime.fromisoformat('2026-10-01T00:00:00-03:00').timestamp() * 1000) - 1
    data_start = start - WARMUP_CANDLES * 15 * MINUTE_MS
    client = BinancePublicClient(str(config.get('market_data', {}).get('rest_url') or 'https://api.binance.com'), 30)
    cache = ROOT / 'data/studies/be_off_cb_deterioration/klines'
    candles = {}
    for interval in ('1m', '5m', '15m'):
        print(f'Loading public {interval} candles, including 60m posterior coverage', flush=True)
        candles[interval] = load_ge_market_data(client, str(config.get('symbol') or 'SOLUSDT'), interval,
                                              data_start, end + 60 * MINUTE_MS, cache, args.offline)
    print('Generating existing baseline signals', flush=True)
    signals = _signals(config, candles, start, end)
    capital = float(config['capital']['operational_balance_usdt'])
    notional = capital * float(config['capital']['trade_size_pct']) / 100
    spread = float(config.get('instrumentation', {}).get('market_bot_replay', {}).get('round_trip_spread_bps', 5))
    boundaries = [c.boundary_ms for c in candles['5m']]
    minute_index = {c.open_time_ms: c for c in candles['1m']}
    settings = config.get('instrumentation', {}).get('market_context', {})
    contexts, rows, baseline = {}, [], {}
    for path in ('HIGH_FIRST', 'LOW_FIRST'):
        print(f'Baseline BE_OFF_CB {path}; no alternative exit rules', flush=True)
        guard = CircuitGuard(COMBO_RULE, 6., capital, notional)
        replay = run_universe(name=f'BE_OFF_CB_{path}', lookback=0, config=config, signals=signals,
                              execution_candles=candles['1m'], start_ms=start, end_ms=end,
                              intrabar_path=path, round_trip_spread_bps=spread, admission_guard=guard.allows)
        baseline[path] = {'closed': len(replay.trades), 'open_at_end': len(replay.open_positions),
                          'exits': dict(Counter(t.exit_reason for t in replay.trades)),
                          'cb_crises': guard.crises, 'cb_blocked': replay.blocked_circuit}
        for t in replay.trades:
            if t.exit_reason not in ('PROFIT_LOCK', 'TRAILING'):
                continue
            if t.closed_ms not in contexts:
                contexts[t.closed_ms] = exit_context(candles['5m'], boundaries, t.closed_ms, settings)
            ctx = contexts[t.closed_ms]
            rows.append({**asdict(t), 'path': path, 'month': brt(t.closed_ms)[:7],
                         'opened_at_brt': brt(t.opened_ms), 'closed_at_brt': brt(t.closed_ms),
                         'ema_context': ctx['ema_context'], 'macd_context': ctx['macd_context'],
                         'histogram_state': ctx['histogram_state'], 'snapshot': ctx,
                         'net_usd': notional * t.net_pct / 100, 'post': posterior(t, minute_index, notional)})
        print(f'{path}: {baseline[path]}', flush=True)
    groups = aggregate(rows)
    manifest = {'created_at_brt': datetime.now(BRASILIA_TZ).isoformat(), 'config': config,
                'window': [brt(start), brt(end)], 'continuous_baseline': True,
                'month_basis': 'closed_at BRT', 'notional': notional, 'spread_bps': spread,
                'baseline': baseline, 'rows': len(rows), 'missing_windows': sum(v is None for r in rows for v in r['post'].values()),
                'candles': {k: {'n': len(v), 'first': brt(v[0].open_time_ms), 'last': brt(v[-1].boundary_ms)} for k,v in candles.items()},
                'source_hashes': {str(p): hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in (
                    Path('tools/ge_replay_study.py'), Path('src/position/bot_full_engine.py'),
                    Path('src/monitor/market_context.py'), Path('tools/real_a_circuit_breaker_replay.py'),
                    Path('tools/be_off_cb_exit_context_study.py'))},
                'cache_hashes': {k: hashlib.sha256((cache/f'SOLUSDT_{k}.jsonl').read_bytes()).hexdigest() for k in candles}}
    output.mkdir(parents=True, exist_ok=True)
    for name, obj in (('manifest.json', manifest), ('summary.json', groups)):
        (output/name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'trades.jsonl').write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows), encoding='utf-8')
    (output/'report.md').write_text(markdown(groups, manifest), encoding='utf-8')
    print(f'Artifacts: {output.resolve()} | rows={len(rows)} | missing windows={manifest["missing_windows"]}', flush=True)


if __name__ == '__main__':
    main()
