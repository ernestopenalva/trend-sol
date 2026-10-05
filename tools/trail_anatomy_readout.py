"""Read-only analysis of saved TRAIL diagnostics; no additional replay."""
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.trail_anatomy_study import OUT, PATHS, HORIZONS, MONTHS, summarize
from tools.be_off_cb_defensive_closure import table, fmt, digest


def combo_matches(e,label):
    if e['snapshot']['ema_context']!='LON':return False
    if label=='LON + BU+':return e['snapshot']['macd_context']=='BU+'
    if label=='LON + POSITIVE_EXPANDING':return e['snapshot']['histogram_state']=='POSITIVE_EXPANDING'
    raise ValueError('Only the two prespecified descriptive combinations are supported')


def main():
    events=[json.loads(line) for line in (OUT/'events.jsonl').read_text().splitlines()]
    combinations={p:{label:{m:{str(h):summarize([e for e in events if e['path']==p and
        combo_matches(e,label) and (m=='ALL' or e['month']==m)],h,'post_minutes')
        for h in HORIZONS} for m in MONTHS} for label in
        ('LON + BU+','LON + POSITIVE_EXPANDING')} for p in PATHS}
    (OUT/'context_combinations.json').write_text(json.dumps(combinations,indent=2),encoding='utf-8')
    lines=['# Duas combinações descritivas de contexto', '',
        'Escolhidas após separação marginal BU+ versus BU− / histograma positivo expandindo versus contraindo em HIGH e LOW.',
        'Não houve busca de combinações, grid ou nova regra. Apenas LON+BU+ e LON+hist positivo expandindo.',
        'Minutos posteriores ao candle da saída. Sem TRAIL: apenas pares fechados; censurados não são perdas.', '']
    for p,cs in combinations.items():
        lines += [f'## {p}', '']
        lines += table(['combinação','mês','min','N','A','B','C','D','pico recuperado','novo pico','HS trajetória',
            'sem TRAIL resolvidos','cens','delta $'],
            [[label,m,h,r['n'],*[r['classes'][c] for c in ('PROTECTION','EARLY','MIXED','NEUTRAL')],
              r['recovered_peak'],r['new_high'],r['hs_path'],r['no_trail']['resolved'],r['no_trail']['censored'],
              fmt(r['no_trail']['delta_total'])] for label,months in cs.items() for m,hs in months.items() for h,r in hs.items()])
    (OUT/'context_combinations.md').write_text('\n'.join(lines),encoding='utf-8')
    lines=['# TRAIL — auditoria individual e saída sem TRAIL', '',
        'Preços reais do baseline incluem spread; porcentagens giveback são pontos percentuais sobre entry.',
        'Sem TRAIL é isolado, não sistêmico. OPEN: net/delta ainda indisponíveis. Trajetórias por horizonte no events.jsonl.', '']
    for p in PATHS:
        lines += [f'## {p}', '']
        lines += table(['source','entry BRT','exit BRT','entry','ATR','exit','peak','TRAIL stop','net $','idade min',
            'PL armados','exit EMA','exit MACD','histograma','peak bruto %','exit bruto %','giveback pp',
            'giveback ATR','fração devolvida %','sem TRAIL reason','sem TRAIL exit BRT','sem TRAIL preço',
            'tempo extra min','sem TRAIL net $','delta $'],
            [[e['source_candle'],e['opened_brt'],e['exit_brt'],fmt(e['entry']),fmt(e['entry_atr']),fmt(e['exit_price']),
              fmt(e['peak_before_exit']),fmt(e['trailing_stop']),fmt(e['net']),fmt(e['age_min']),','.join(e['pl_armed']),
              e['snapshot']['ema_context'],e['snapshot']['macd_context'],e['snapshot']['histogram_state'],
              *[fmt(e['giveback'][k]) for k in ('peak_gross_pct','exit_gross_pct','giveback_pct_points',
                'giveback_atr','fraction_peak_returned_pct')],e['no_trail']['reason'],e['no_trail']['exit_brt'],
              fmt(e['no_trail']['price']),fmt(e['no_trail']['additional_min']),fmt(e['no_trail']['net']),
              fmt(e['no_trail']['delta'])] for e in events if e['path']==p])
    (OUT/'events.md').write_text('\n'.join(lines),encoding='utf-8')
    (OUT/'readout_manifest.json').write_text(json.dumps({'input_events_hash':digest(OUT/'events.jsonl'),
        'input_summary_hash':digest(OUT/'summary.json'),'readout_tool_hash':digest(Path(__file__)),
        'selection':'Two existing simple combinations following marginal evidence; no optimization.',
        'scope':'Saved results only; no replay, runtime, config or live state access'},indent=2),encoding='utf-8')
    print('Readout DONE',OUT)


if __name__=='__main__':main()
