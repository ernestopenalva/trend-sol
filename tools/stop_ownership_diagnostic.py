"""Reclassification of frozen snapshots only. No engine or replay execution."""
import json, hashlib, sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.atr_units_diagnostic import distribution, quartile
from tools.be_off_cb_exit_context_study import brt
ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / 'data/analysis/feed_trail_revalidation_20261009'
OUT = ROOT / 'data/analysis/stop_ownership_20261009'
STEPS = ((5, 1.5), (8, 3), (12, 6))

def classify(d, trade, cuts):
    entry, atr = d['entry_price'], d['entry_atr']
    floor = .0025 * entry / atr
    stop = entry * .985
    seen = set(); arms = []; changes = []; transitions = []
    timeline = [dict(at_ms=d['opened_ms'], tick_index=None, owner='HARD_STOP',
        stop_atr=(stop-entry)/atr, delta_atr=0., regression=False)]
    previous = {'owner':'HARD_STOP'}
    for e in d['floor_events']:
        # PL plans execute in ascending step order, before this tick's TRAIL update.
        working = stop
        for key in e['PL_armed']:
            if key in seen: continue
            seen.add(key); step = int(key.split(':')[1]); trigger, lock = STEPS[step-1]
            candidate = entry + max(lock, floor) * atr
            raised = candidate > working + 1e-8
            arms.append(dict(step=step, at_ms=e['at_ms'], tick_index=e.get('tick_index'),
                before=working, candidate=candidate, changed=raised,
                trigger_atr=max(lock,floor)+trigger-lock))
            working = max(working, candidate)
            if raised: changes.append(dict(at_ms=e['at_ms'], tick_index=e.get('tick_index'),
                floor_atr=(working-entry)/atr, step=step))
        if previous is None or previous['owner'] != e['owner']:
            timeline.append(dict(at_ms=e['at_ms'], tick_index=e.get('tick_index'), owner=e['owner'],
                stop_atr=(e['effective_stop']-entry)/atr,
                delta_atr=(e['effective_stop']-stop)/atr,
                regression=e['effective_stop'] < stop-1e-8))
            if e['owner']=='TRAIL':
                transitions.append(dict(at_ms=e['at_ms'], previous_owner=previous['owner'] if previous else 'HARD_STOP',
                    snapshot_jump_atr=(e['effective_stop']-stop)/atr,
                    takeover_jump_atr=(e['effective_stop']-working)/atr,
                    peak_atr=(e['peak']-entry)/atr))
        stop=e['effective_stop']; previous=e
    first=transitions[0] if transitions else None
    boundary_index=next((i for i,e in enumerate(d['floor_events']) if first and e['owner']=='TRAIL'),len(d['floor_events']))
    before=d['floor_events'][:boundary_index]
    eligible=[c for c in changes if any(e['at_ms']==c['at_ms'] and e.get('tick_index')==c['tick_index'] for e in before)]
    zone=None
    if eligible:
        start=eligible[-1]
        samples=[e for e in before if (e['at_ms'],e.get('tick_index',0)) >= (start['at_ms'],start.get('tick_index',0))]
        zone=dict(start=start, end_ms=first['at_ms'] if first else d['closed_ms'],
            reached_trail=bool(first), censored_by_exit=not bool(first),
            observed_max_giveback_atr=max((e['peak']-e['effective_stop'])/atr for e in samples),
            start_peak_atr=(samples[0]['peak']-entry)/atr,
            end_peak_atr=first['peak_atr'] if first else (d['terminal_peak']-entry)/atr)
    return dict(source_candle=d['source_candle'],quartile=quartile(100*atr/entry,cuts),
        net=trade['net_usd'],exit_reason=trade['exit_reason'],arms=arms,timeline=timeline,
        switches=max(0,len(timeline)-1),regressions=sum(t['regression'] for t in timeline),
        effective_stop_decreases=sum(b['effective_stop']<a['effective_stop']-1e-8 for a,b in zip(d['floor_events'],d['floor_events'][1:])),
        transitions=transitions,zone=zone,peak_atr=(d['terminal_peak']-entry)/atr,
        terminal_giveback_atr=(d['terminal_peak']-d['terminal_stop'])/atr)

def economics(rows):
    return dict(N=len(rows),net=sum(r['net'] for r in rows),
        net_trade=sum(r['net'] for r in rows)/len(rows) if rows else None,
        exits=dict(Counter(r['exit_reason'] for r in rows)),
        terminal_giveback_atr=distribution([r['terminal_giveback_atr'] for r in rows]))

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    cuts=json.loads((ROOT/'data/analysis/atr_units_20261009/summary.json').read_text())['quartile_cuts_atr_pct']
    summaries={}; hashes={}; allrows={}
    for path in ['HIGH_FIRST','LOW_FIRST']:
        file=INPUT/f'{path}_ACT10_GAP5.json'; hashes[path]=hashlib.sha256(file.read_bytes()).hexdigest()
        data=json.loads(file.read_text()); assert data['prior_full_parity']
        trades={t['source_candle']:t for t in data['run']['trades']}
        rows=[classify(d,trades[d['source_candle']],cuts) for d in data['details']]; allrows[path]=rows
        summaries[path]=dict(
            arms={q:{s:dict(N=len(xs),changed=sum(a['changed'] for a in xs),unchanged=sum(not a['changed'] for a in xs))
                for s in [1,2,3] for xs in [[a for r in rows if r['quartile']==q for a in r['arms'] if a['step']==s]]} for q in range(1,5)},
            sequences=dict(Counter(' -> '.join(t['owner'] for t in r['timeline']) for r in rows)),
            switches=distribution([r['switches'] for r in rows]),regressions=sum(r['regressions'] for r in rows),
            stop_decreases=sum(r['effective_stop_decreases'] for r in rows),
            peak_8_10={q:economics([r for r in rows if r['quartile']==q and 8<=r['peak_atr']<10]) for q in range(1,5)},
            peak_above_10=economics([r for r in rows if r['peak_atr']>=10]),
            peak_8_10_all=economics([r for r in rows if 8<=r['peak_atr']<10]),
            zones={q:dict(N=len(xs),reached_trail=sum(r['zone']['reached_trail'] for r in xs),
                exit_censored=sum(r['zone']['censored_by_exit'] for r in xs),
                giveback=distribution([r['zone']['observed_max_giveback_atr'] for r in xs]),
                start_peak=distribution([r['zone']['start_peak_atr'] for r in xs]),
                end_peak=distribution([r['zone']['end_peak_atr'] for r in xs]))
                for q in range(1,5) for xs in [[r for r in rows if r['quartile']==q and r['zone']]]},
            takeover={q:dict(N=len(xs),jump=distribution([t['takeover_jump_atr'] for t in xs]),
                snapshot_jump=distribution([t['snapshot_jump_atr'] for t in xs]))
                for q in range(1,5) for xs in [[t for r in rows if r['quartile']==q for t in r['transitions']]]})
    for name,value in [('summary',summaries),('trades',allrows),('manifest',dict(input_sha256=hashes,cuts=cuts,replay='NONE'))]:
        (OUT/f'{name}.json').write_text(json.dumps(value,indent=2),encoding='utf-8')
    def table(headers,rows):
        def fmt(x): return 'N/A' if x is None else f'{x:.4f}' if isinstance(x,float) else str(x)
        return '\n'.join(['| '+' | '.join(headers)+' |','|'+'|'.join(['---']*len(headers))+'|']+['| '+' | '.join(map(fmt,r))+' |' for r in rows])
    lines=['# ACT10/GAP5 — redundância, propriedade e intervalo pré-dominância',
        '01/06 → 02/10/2026 22:28 BRT. HIGH_FIRST/LOW_FIRST separados. Reclassificação de snapshots congelados; nenhum replay novo. Geometria ajustada previamente validada a 1e-8. Quartis fixos do estudo anterior.',
        '## Definições e limites',
        'Arme sem efeito: o piso do novo plano não aumenta o stop vigente na ordem real do engine (PL1/2/3 antes do update TRAIL). Armes simultâneos são decompostos nessa ordem por álgebra, sem simular preços. Timeline é o owner ao final de cada tick persistido; não revela owners intermediários dentro do mesmo tick. PL_step pode mudar o rótulo sem apertar o piso. Regressão significa diminuição numérica de proteção, não a ordenação nominal dos rótulos.',
        'Zona individual: após a última elevação efetiva causada por um PL em um tick anterior à primeira dominância TRAIL. Quando TRAIL nunca domina, usa a última elevação PL observada e termina censurada na saída. PL e TRAIL no mesmo tick não definem intervalo temporal mensurável e não entram nessa população. Seleção retrospectiva descritiva, não feature causal de decisão. Sem PL antes de TRAIL não há zona PL definida. Devolução aqui é pico conhecido menos stop vigente; não queda efetivamente executada nem MTM. Snapshots preservam novos picos, portanto permitem máximos de devolução, mas não tempo abaixo de níveis.',
        '## 1 — Armes por degrau e quartil',
        table(['path','Q','PL','armes','apertou','sem efeito'],[[p,q,s,x['N'],x['changed'],x['unchanged']] for p,v in summaries.items() for q,qs in v['arms'].items() for s,x in qs.items()]),
        'Classificação observada: PL1 sempre eleva o stop no instante de sua instrução. PL2 é redundante em preço em Q1, majoritariamente redundante em Q2 e efetivo em Q3/Q4. PL3 é misto em todos os quartis. Apertar durante a instrução PL não significa dominar ao final do tick: TRAIL é atualizado depois e pode sobrepor o mesmo incremento. Não confundir contribuição intermediária com proteção final exclusiva.',
        '## 2 — Sequências de owner',
        table(['path','sequência','N','%'],[[p,k,n,100*n/len(allrows[p])] for p,v in summaries.items() for k,n in sorted(v['sequences'].items(),key=lambda kv:-kv[1])]),
        table(['path','trocas mediana','p90','máximo','regressões nas trocas','decreases em todos snapshots'],[[p,v['switches']['median'],v['switches']['p90'],v['switches']['max'],v['regressions'],v['stop_decreases']] for p,v in summaries.items()]),
        '## 3 — Intervalo individual anterior à dominância',
        table(['path','Q','N zona','TRAIL assumiu','censurada na saída','pico inicial med','pico final med','devolução med','p90'],[[p,q,x['N'],x['reached_trail'],x['exit_censored'],x['start_peak']['median'],x['end_peak']['median'],x['giveback']['median'],x['giveback']['p90']] for p,v in summaries.items() for q,x in v['zones'].items()]),
        '### Faixa 8–10 ATR: comparação descritiva, não fronteira uniforme',
        table(['path','Q','N','net','net/trade','devolução terminal med','p90','saídas'],[[p,q,x['N'],x['net'],x['net_trade'],x['terminal_giveback_atr']['median'],x['terminal_giveback_atr']['p90'],x['exits']] for p,v in summaries.items() for q,x in v['peak_8_10'].items()]),
        table(['path','pico','N','net','net/trade','devolução terminal med','p90','saídas'],[[p,k,x['N'],x['net'],x['net_trade'],x['terminal_giveback_atr']['median'],x['terminal_giveback_atr']['p90'],x['exits']] for p,v in summaries.items() for k,x in [('8–10',v['peak_8_10_all']),('>=10',v['peak_above_10'])]]),
        'Comparar populações selecionadas pelo pico final não identifica efeito causal da exposição: atingir >=10 seleciona desenvolvimento maior do trade. A diferença econômica observada não demonstra que o intervalo sem nova elevação PL causou o resultado.',
        '## 4 — Salto quando TRAIL assume',
        table(['path','Q','N','salto min','p25','med','p75','p90','max','snapshot delta med'],[[p,q,x['N'],*[x['jump'][k] for k in ['min','p25','median','p75','p90','max']],x['snapshot_jump']['median']] for p,v in summaries.items() for q,x in v['takeover'].items()]),
        'Salto takeover = stop TRAIL menos piso imediatamente anterior após os PLs do mesmo tick. Snapshot delta também inclui eventual PL no mesmo tick. Em arming direto a 10ATR há salto geométrico; se TRAIL já estava armado abaixo do PL, a assunção ocorre acima do ponto de igualdade, e seu incremento depende do passo de preço observado. Valores discretos não provam descontinuidade contínua nesse segundo caso. Registros individuais em trades.json e TIMELINES.md. Nenhuma proposta de arquitetura ou parâmetros.']
    (OUT/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    (OUT/'TIMELINES.md').write_text('# Owners observados por trade\n\n'+table(['path','source BRT','owner BRT','tick','owner','stop ATR','delta ATR'],[[p,brt(r['source_candle']),brt(t['at_ms']),t['tick_index'],t['owner'],t['stop_atr'],t['delta_atr']] for p,rows in allrows.items() for r in rows for t in r['timeline']]),encoding='utf-8')
    print(json.dumps({p:{k:v[k] for k in ['sequences','regressions','stop_decreases','peak_8_10_all','peak_above_10']} for p,v in summaries.items()}))

if __name__=='__main__': main()
