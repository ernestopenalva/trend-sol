"""Analysis helpers for forward_experiment_report --matrix-audit; no standalone CLI.

Observed selections are NOT an alternative systemic replay. Economic outcomes
of opportunities that never opened are never imputed.
"""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median
from types import SimpleNamespace

from src.monitor.forward_experiment_shadows import PolicyShadow

EMAS = ('LON', 'BUL', 'BEA', 'SHO', 'MUP', 'MDO', 'MIX')
MACDS = ('BU+', 'BU-', 'BE+', 'BE-')
LOW_N_GUARDRAIL = 10  # Descriptive safeguard, NOT statistical approval.
# Known outage of the forward CB arms. Crossing outcomes are not evidence.
FREEZE_START = datetime.fromisoformat('2026-09-29T16:25:12+00:00')
FREEZE_END = datetime.fromisoformat('2026-10-01T01:03:09+00:00')
COMPARABLE_START = datetime.fromisoformat('2026-09-28T14:52:14+00:00')


def parse_pair(text):
    compact = text.replace(' ', '').replace('−', '-')
    for ema in EMAS:
        for macd in MACDS:
            if compact == ema+'+'+macd: return ema, macd
    raise ValueError('pair must be an official EMA+MACD combination, e.g. SHO+BE+')


def matrix_allowed(pair):
    # Reuse the real policy. No second copy of the admission matrix.
    return PolicyShadow._entry_policy(SimpleNamespace(policy='EMA_MACD'),
        {'ema_context': pair[0], 'macd_context': pair[1]})[0]


def event_pair(row):
    return str(row.get('ema_context') or 'UNKNOWN'), str(row.get('macd_context') or 'UNKNOWN')


def unique_sources(report, rows, *, opportunities=False):
    result = {}
    for row in rows:
        source = report._source(row)
        if source is None: raise ValueError('matrix audit: missing source_candle; cannot pair')
        if source in result:
            previous = result[source]
            if opportunities and event_pair(previous) != event_pair(row):
                raise ValueError(f'conflicting opportunity contexts for source {source}')
            if not opportunities and previous != row:
                raise ValueError(f'conflicting trade rows for source {source}')
            continue
        result[source] = row
    return result


def trade_index(report, closed, opened, events):
    finals = unique_sources(report, closed)
    current = unique_sources(report, opened)
    if set(finals) & set(current):
        raise ValueError('source recorded both closed and open; reconcile snapshot before matrix audit')
    admissions = unique_sources(report, [r for r in events if r.get('event') == 'OPEN'], opportunities=True)
    result = {}
    for source in set(finals) | set(current) | set(admissions):
        row = dict(finals.get(source) or current.get(source) or {})
        event = admissions.get(source, {})
        context = (row.get('market_context_entry') or {}).get('tf_5m') or event
        row['_pair'] = event_pair(context)
        row['_source'] = source
        row['_opened'] = report.parse_time(row.get('opened_at') or row.get('open_ts') or event.get('ts'))
        row['_closed'] = report.parse_time(row.get('closed_at') or row.get('close_ts')) if source in finals else None
        row['_resolved'] = source in finals
        row['_status'] = 'CLOSED' if source in finals else 'OPEN' if source in current else 'UNRESOLVED'
        row['_net'] = report._net_dollars(row) if source in finals else None
        row['_entry'] = report._number(row.get('entry_price', event.get('price')))
        row['_invalid'] = (row['_opened'] is not None and row['_opened'] < FREEZE_END and
            (row['_closed'] is None or row['_closed'] > FREEZE_START))
        result[source] = row
    return result


def metrics(report, trades):
    trades = list(trades)
    closed = [r for r in trades if r['_resolved']]
    resolved = sorted([r for r in closed if r['_net'] is not None and not r['_invalid']],
                      key=lambda r:(r['_closed'] or datetime.min.replace(tzinfo=timezone.utc), r['_source']))
    values = [r['_net'] for r in resolved]
    positive = sorted((v for v in values if v > 0), reverse=True)
    negative = sorted(v for v in values if v < 0)
    gross_win, gross_loss = sum(positive), -sum(negative)
    total = sum(values) if values else None
    counts = Counter(r.get('exit_reason') for r in resolved)
    ages = [(r['_closed']-r['_opened']).total_seconds()/60 for r in resolved if r['_closed'] and r['_opened']]
    days = defaultdict(lambda: {'closed': 0, 'net': 0., 'HS': 0, 'PL': 0, 'TRAIL': 0})
    for r in resolved:
        day = r['_opened'].astimezone(report.BRASILIA_TZ).strftime('%d/%m/%Y')
        days[day]['closed'] += 1; days[day]['net'] += r['_net']
        key = {'HARD_STOP':'HS', 'PROFIT_LOCK':'PL', 'TRAILING':'TRAIL'}.get(r.get('exit_reason'))
        if key: days[day][key] += 1
    stamps = [r['_opened'] for r in trades if r['_opened']]
    return {'closed': len(closed), 'resolved': len(resolved),
        'open': sum(r['_status']=='OPEN' for r in trades),
        'unresolved': sum(not r['_resolved'] for r in trades),
        'invalid': sum(r['_invalid'] for r in closed),
        'missing_net': sum(r['_net'] is None for r in closed), 'net': total,
        'net_trade': total/len(values) if values else None,
        'PF': gross_win/gross_loss if gross_loss else float('inf') if gross_win else None,
        'DD': report.realized_max_drawdown(resolved), 'median_age': median(ages) if ages else None,
        'median_net': median(values) if values else None, 'best': max(values) if values else None,
        'worst': min(values) if values else None, 'top3_winners': sum(positive[:3]),
        'top3_losers': sum(negative[:3]), 'top3_winner_share': sum(positive[:3])/gross_win if gross_win else None,
        'net_without_top1': total-sum(positive[:1]) if values else None,
        'net_without_top3': total-sum(positive[:3]) if values else None,
        'net_without_worst': total-(negative[0] if negative else 0) if values else None,
        'HS': counts['HARD_STOP'], 'PL': counts['PROFIT_LOCK'], 'TRAIL': counts['TRAILING'],
        'days': dict(days), 'period': (min(stamps), max(stamps)) if stamps else None,
        'resolved_rows': resolved, 'rows': trades}


def classify(status, m):
    """Transparent descriptive triage, never a trading/approval rule."""
    low = m['resolved'] < LOW_N_GUARDRAIL
    if low:
        return 'INSUFFICIENT_SAMPLE', 'LOW_N / EXPLORATORY', 'N resolved < 10: não há suporte suficiente para mudança da matriz.'
    positive_days = sum(d['net'] > 0 for d in m['days'].values())
    negative_days = sum(d['net'] < 0 for d in m['days'].values())
    robust_positive = m['net'] > 0 and m['PF'] > 1 and m['net_without_top3'] > 0 and positive_days >= 2
    persistent_negative = m['net'] < 0 and m['PF'] < 1 and m['median_net'] <= 0 and m['net_without_worst'] < 0 and negative_days >= 2
    if m['unresolved'] >= m['resolved']:
        return 'REVIEW_ACCEPTED' if status=='ACCEPTED' else 'INSUFFICIENT_SAMPLE', 'EXPLORATORY / CENSORED', 'Muitos resultados pendentes; distribuição e estabilidade ainda não sustentam mudança.'
    if status == 'ACCEPTED':
        if robust_positive:
            return 'KEEP_ACCEPTED', 'DESCRIPTIVE', 'Net/PF positivos em mais de um dia e net ainda positivo sem top 3 winners.'
        if persistent_negative:
            return 'CANDIDATE_BLOCK', 'EXPLORATORY', 'Net/PF e mediana negativos, perda em vários dias e não explicada só pelo pior loser; validar fora da amostra.'
        return 'REVIEW_ACCEPTED', 'EXPLORATORY', 'Distribuição mista ou resultado concentrado; revisar sem atribuir a HS% sozinho uma decisão de entrada.'
    if persistent_negative:
        return 'KEEP_BLOCKED', 'DESCRIPTIVE', 'Controle teve perda em vários dias, PF < 1 e resultado negativo mesmo sem o pior loser.'
    if m['net'] > 0 and m['PF'] > 1:
        return 'CANDIDATE_UNBLOCK', 'EXPLORATORY' if robust_positive else 'EXPLORATORY / CONCENTRATED', 'Winners observados no controle merecem investigação; '+('saldo resiste à remoção dos top 3.' if robust_positive else 'saldo não demonstra robustez à concentração/tempo.')
    return 'KEEP_BLOCKED', 'EXPLORATORY', 'Saldo observado não favorece desbloqueio, mas concentração/N/regimes não provam robustez estrutural.'


def build_audit(report, since):
    exp = report.EXPERIMENTS['ema_macd']
    estate, cstate = report._state(exp), report._state(report.CONTROL)
    eevents, cevents = report._events(exp, since), report._events(report.CONTROL, since)
    opp = unique_sources(report, [r for r in eevents if r.get('event')=='SIGNAL_OPPORTUNITY'], opportunities=True)
    experiment = trade_index(report, report._records(exp, since), report._open_positions(estate, since), eevents)
    control = trade_index(report, report._records(report.CONTROL, since), report._open_positions(cstate, since), cevents)
    blocks = {report._source(r) for r in eevents if str(r.get('event','')).startswith('ENTRY_BLOCKED_EMA_MACD_')}
    edecisions, cdecisions = defaultdict(list), defaultdict(list)
    for rows, dest in ((eevents, edecisions), (cevents, cdecisions)):
        for r in rows:
            if str(r.get('event','')).startswith('ENTRY_BLOCKED'): dest[report._source(r)].append(r['event'])
    pairs = {}
    all_pairs = set((e,m) for e in EMAS for m in MACDS) | {event_pair(r) for r in opp.values()} | {r['_pair'] for r in experiment.values()}
    for pair in sorted(all_pairs):
        sources = {s for s,r in opp.items() if event_pair(r)==pair}
        es = {s for s,r in experiment.items() if r['_pair']==pair}
        cs = {s for s in sources if s in control}
        blocked_control = sources & blocks & set(control)
        status = 'ACCEPTED' if matrix_allowed(pair) else 'BLOCKED'
        economic = [experiment[s] for s in es] if status=='ACCEPTED' else [control[s] for s in blocked_control]
        m = metrics(report, economic)
        comparable = metrics(report, [r for r in economic if r['_opened'] and r['_opened'] >= COMPARABLE_START])
        warmup = metrics(report, [r for r in economic if r['_opened'] and r['_opened'] < COMPARABLE_START])
        classification, confidence, reason = classify(status, comparable)
        mismatches = sum(control[s]['_pair'] != pair for s in cs)
        if mismatches:
            classification, confidence, reason = 'INSUFFICIENT_SAMPLE', 'CONTEXT_MISMATCH', 'Contextos de admissão divergem entre braços: auditar antes de concluir.'
        nonopened = sources-set(control)
        reasons = Counter()
        for s in nonopened:
            observed = cdecisions.get(s, [])
            reasons.update(set(observed) or {'NO_CONTROL_DECISION_RECORDED'})
        pairs[pair] = {'pair': pair, 'status': status, 'opportunities': len(sources),
            'matrix_eligible': len(sources) if status=='ACCEPTED' else 0,
            'matrix_pass_observed': sum(s in experiment or any(x!='ENTRY_BLOCKED_CIRCUIT_BREAKER' and not x.startswith('ENTRY_BLOCKED_EMA_MACD_') and x!='ENTRY_BLOCKED_CONTEXT_UNAVAILABLE' for x in edecisions.get(s,[])) for s in sources) if status=='ACCEPTED' else 0,
            'blocked_by_matrix': len(sources & blocks), 'opened_experiment': len(es), 'opened_control': len(cs),
            'common': len(es & set(control)), 'control_only': len(cs-set(experiment)),
            'experiment_only': len(es-set(control)), 'context_mismatches': mismatches,
            'not_opened_control': dict(reasons), 'control_rows': [control[s] for s in sorted(cs)],
            'opportunity_days': dict(Counter(report.parse_time(opp[s]['ts']).astimezone(report.BRASILIA_TZ).strftime('%d/%m/%Y') for s in sources)),
            'metrics': m, 'classification_metrics': comparable, 'warmup_metrics': warmup,
            'classification': classification, 'confidence': confidence, 'reason': reason}
    blocked_sources = blocks & set(control)
    valid_nets = [control[s]['_net'] for s in blocked_sources if control[s]['_resolved'] and control[s]['_net'] is not None]
    totals = {'opportunities': len(opp), 'accepted_opportunities': len({report._source(r) for r in eevents if r.get('event')=='OPEN'}),
        'blocked_opportunities': len({report._source(r) for r in eevents if str(r.get('event','')).startswith('ENTRY_BLOCKED')}),
        'control_closed': sum(r['_resolved'] for r in control.values()), 'control_open': sum(not r['_resolved'] for r in control.values()),
        'experiment_closed': sum(r['_resolved'] for r in experiment.values()), 'experiment_open': sum(not r['_resolved'] for r in experiment.values()),
        'common': len(set(control)&set(experiment)), 'control_only': len(set(control)-set(experiment)), 'experiment_only': len(set(experiment)-set(control)),
        'matrix_blocked_opened_control': len(blocked_sources), 'matrix_blocked_resolved_raw': len(valid_nets),
        'matrix_blocked_net_raw': sum(valid_nets), 'matrix_blocked_invalid': sum(control[s]['_invalid'] and control[s]['_resolved'] for s in blocked_sources)}
    return {'since': since, 'pairs': pairs, 'totals': totals, 'control': control, 'experiment': experiment,
            'opportunities': opp, 'events': eevents,
            'state_updated_at': {'control': cstate.get('updated_at'), 'experiment': estate.get('updated_at')}}


def money(report, value): return report._fmt_net(value)
def number(value): return 'N/A' if value is None else 'inf' if value==float('inf') else f'{value:.3f}'
def share(count, total): return f'{100*count/total:.1f}%' if total else 'N/A'


def print_metrics(report, item):
    m=item['metrics']; n=m['resolved']
    print(f"N closed | {m['closed']} | N resolved/evaluable | {n} | N open | {m['open']} | N unresolved incl. open | {m['unresolved']} | invalid outcomes | {m['invalid']}")
    print(f"net | {money(report,m['net'])} | net/trade (= avg net) | {money(report,m['net_trade'])} | PF | {number(m['PF'])} | realized subset DD $ | {money(report,m['DD'])}")
    print(' | '.join(f"{k} | {m[k]} | {k}% | {share(m[k],n)}" for k in ('HS','PL','TRAIL')))
    print(f"median age min | {number(m['median_age'])} | median net | {money(report,m['median_net'])} | best | {money(report,m['best'])} | worst | {money(report,m['worst'])}")
    if m['resolved_rows']:
        best=max(m['resolved_rows'],key=lambda r:r['_net'])
        worst=min(m['resolved_rows'],key=lambda r:r['_net'])
        print('best source_candle | '+report._fmt_ms(best['_source'])+' | worst source_candle | '+report._fmt_ms(worst['_source']))
    print(f"top 3 winners net | {money(report,m['top3_winners'])} | share of gross positive net | {share(m['top3_winners'],sum(r['_net'] for r in m['resolved_rows'] if r['_net']>0))}")
    print(f"top 3 losers net | {money(report,m['top3_losers'])} | net without top 1 winner | {money(report,m['net_without_top1'])} | net without top 3 winners | {money(report,m['net_without_top3'])}")
    period=m['period']
    print('entry period | '+(' to '.join(report._fmt(x) for x in period) if period else 'N/A'))
    print(f"{item['classification']} | {item['confidence']} | {item['reason']}")
    support=item['classification_metrics']; warmup=item['warmup_metrics']
    print(f"classification support (no warm-up) | N resolved={support['resolved']} | N open/unresolved={support['unresolved']} | net={money(report,support['net'])}")
    print('classification support entry period | '+(' to '.join(report._fmt(x) for x in support['period']) if support['period'] else 'N/A'))
    print(f"warm-up economics (observational only, excluded from classification) | closed={warmup['closed']} | net={money(report,warmup['net'])}")
    print(f"admission overlap | common={item['common']} | control-only={item['control_only']} | experiment-only={item['experiment_only']}")
    if n>=10:
        print('day BRT | resolved | net | HS | PL | TRAIL')
        for day,d in sorted(m['days'].items(),key=lambda x:datetime.strptime(x[0],'%d/%m/%Y')):
            print(f"{day} | {d['closed']} | {money(report,d['net'])} | {d['HS']} | {d['PL']} | {d['TRAIL']}")


def print_trades(report, rows, *, exit_context=False):
    print('source_candle | entry BRT | entry price | exit BRT | exit price | entry EMA | entry MACD | exit reason | net | age min'+(' | exit EMA | exit MACD | exit snapshot status' if exit_context else '')+' | forward validity | trend_open | trend_close')
    for r in rows:
        closed=r['_resolved']; ctx=report._recorded_exit_context(r) if closed else {}
        validity='INVALID_FREEZE' if r['_invalid'] else 'OBSERVED'
        age=(r['_closed']-r['_opened']).total_seconds()/60 if closed and r['_closed'] and r['_opened'] else None
        cells=[report._fmt_ms(r['_source']), report._fmt(r['_opened']), report._fmt_price(r['_entry']),
            report._fmt(r['_closed']) if closed else r['_status'], report._fmt_price(report._number(r.get('exit_price'))) if closed else r['_status'],
            *r['_pair'], str(r.get('exit_reason') or 'N/A') if closed else r['_status'],
            money(report,r['_net']) if closed else r['_status'], number(age)]
        if exit_context:
            ema,macd=ctx.get('ema_context','UNAVAILABLE'),ctx.get('macd_context','UNAVAILABLE')
            cells += [ema,macd,'STALE' if 'STALE' in (ema,macd) else 'UNAVAILABLE' if 'UNAVAILABLE' in (ema,macd) else 'RECORDED_CAUSAL' if closed else r['_status']]
        print(' | '.join(cells)+' | '+validity+' | '+r.get('trend_open','UNAVAILABLE')+' | '+(r.get('trend_close','UNAVAILABLE') if closed else r['_status']))
    if not rows: print('N/A')


def print_matrix_audit(report, since, pair_filter=None, top_n=10):
    audit=build_audit(report,since)
    selected=parse_pair(pair_filter) if pair_filter else None
    items=[v for k,v in audit['pairs'].items() if selected is None or k==selected]
    stamps=[report.parse_time(r['ts']) for r in audit['events'] if r.get('ts')]
    print('EMA + MACD MATRIX AUDIT | DIAGNOSTIC ONLY | since '+report._fmt(since))
    print('last recorded event | '+report._fmt(max(stamps) if stamps else None))
    for arm,stamp in audit['state_updated_at'].items():print('snapshot '+arm+' | '+report._fmt(report.parse_time(stamp)))
    print('Matrix status comes from the current real PolicyShadow; observed pairs come from entry/opportunity telemetry.')
    print('Warm-up before 28/09/2026 11:52:14 BRT is observational/non-comparable. No systemic delta is inferred.')
    print('Known CB freeze 29/09 13:25:12 to 30/09 22:03:09 BRT: crossing outcomes remain in raw reconciliation, excluded from evaluable economics/classification.')
    print('Exit STALE does not invalidate entry pair/net; never use it to infer technical deterioration. Subset DD is realized, not portfolio/MTM DD.')
    print('LOW_N / EXPLORATORY if N resolved < 10; 10 is descriptive, not statistical approval. All changes require out-of-window systemic replay, distinct regimes and independent forward validation.')
    print('\nRECONCILIATION WITH EXISTING REPORT (full requested window, before optional pair filter)')
    for k,v in audit['totals'].items():print(f'{k} | {money(report,v) if k.endswith("net_raw") else v}')
    print('\nMATRIX PAIR SUMMARY')
    print('pair | matrix_status | opportunities | trades_evaluable | N open/unresolved | N resolved support excl. warmup | net | net/trade | PF | HS | HS% | PL | PL% | TRAIL | TRAIL% | best | worst | top3_winner_share | classification | confidence')
    for item in items:
        m=item['metrics'];n=m['resolved']
        cells=['+'.join(item['pair']),item['status'],str(item['opportunities']),str(n),str(m['unresolved']),str(item['classification_metrics']['resolved']),money(report,m['net']),money(report,m['net_trade']),number(m['PF'])]
        for k in ('HS','PL','TRAIL'):cells += [str(m[k]),share(m[k],n)]
        cells += [money(report,m['best']),money(report,m['worst']),share(m['top3_winner_share'],1) if m['top3_winner_share'] is not None else 'N/A',item['classification'],item['confidence']]
        print(' | '.join(cells))
    for status,section in (('ACCEPTED','ACCEPTED PAIRS AUDIT'),('BLOCKED','BLOCKED PAIRS AUDIT')):
        print('\n'+section)
        for item in items:
            if item['status']!=status or not item['opportunities'] and not item['opened_experiment']:continue
            print('\n'+'+'.join(item['pair']))
            print(f"opportunities | {item['opportunities']} | matrix eligible | {item['matrix_eligible']} | matrix pass observed | {item['matrix_pass_observed']} | blocked by matrix | {item['blocked_by_matrix']} | actually opened EMA_MACD | {item['opened_experiment']} | actually opened control | {item['opened_control']}")
            print_metrics(report,item)
    print('\nACCEPTED HARD STOPS BY MATRIX PAIR')
    hard=[r for item in items if item['status']=='ACCEPTED' for r in item['metrics']['rows'] if r['_resolved'] and r.get('exit_reason')=='HARD_STOP']
    print_trades(report,sorted(hard,key=lambda r:(r['_pair'],r['_opened'])),exit_context=True)
    print('\nBLOCKED IMPORTANT WINNERS | criterion: top '+str(top_n)+' positive net trades actually matrix-blocked and opened by control; not a minimum-effect threshold')
    winners=[r for item in items if item['status']=='BLOCKED' for r in item['metrics']['resolved_rows'] if r['_net']>0]
    print_trades(report,sorted(winners,key=lambda r:r['_net'],reverse=True)[:top_n])
    print('\nREFINEMENT CANDIDATES | retrospective, not deployable changes')
    for item in items:
        if item['classification'] in ('CANDIDATE_BLOCK','CANDIDATE_UNBLOCK','REVIEW_ACCEPTED'):
            print('\n'+'+'.join(item['pair'])); print_metrics(report,item)
    if selected is None or selected==('SHO','BE+'):
        item=audit['pairs'][('SHO','BE+')]
        print('\nSPECIAL FOCUS SHO+BE+')
        print(f"opportunities | {item['opportunities']} | actually opened control | {item['opened_control']} | blocked by matrix | {item['blocked_by_matrix']}")
        print_metrics(report,item)
        print('control not-opened reasons | '+str(item['not_opened_control']))
        print('opportunity distribution day BRT | '+str(item['opportunity_days']))
        print('ALL SHO+BE+ CONTROL TRADES (including open and invalid, explicitly labelled)')
        print_trades(report,sorted(item['control_rows'],key=lambda r:r['_opened']))
        print('TOP WINNERS / LOSERS SHO+BE+ | evaluable only')
        rows=item['metrics']['resolved_rows']
        print_trades(report,sorted([r for r in rows if r['_net']>0],key=lambda r:r['_net'],reverse=True)[:top_n])
        print_trades(report,sorted([r for r in rows if r['_net']<0],key=lambda r:r['_net'])[:top_n])
    print('\nPATH DEPENDENCY: blocked net cannot be added mechanically to EMA_MACD. Changing matrix changes slots, spacing, CB and future admissions. No matrix/config/runtime/state changes.')
    return audit
