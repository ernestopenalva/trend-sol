"""Presentation and causal artifact checks for the defensive diagnosis."""
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from tools.be_off_cb_defensive_review import OUT, MONTHS, brt, mean, median, output_summary, summarize


def table(headers, rows):
    return ['| '+' | '.join(headers)+' |','|'+'|'.join('---' for _ in headers)+'|',
            *['| '+' | '.join(str(v) for v in r)+' |' for r in rows],'']


def f(v):return 'N/A' if v is None else f'{v:.4f}'


def percent(v):return 'N/A' if v is None else f'{v*100:.1f}%'


def ms(value):return int(datetime.fromisoformat(value).timestamp()*1000)


def coverage_for_cluster(d,c):
    members=set(c['trades']);covered=set();counts={}
    for mechanism in ('FAST_DROP_EMA','HS_BEAR_SHO','HS_BULL_ELASTIC','CB_EXIT_ALL'):
        ids={r['opened_ms'] for r in d['counterfactual_exits'] if r['mechanism']==mechanism and
             r['opened_ms'] in members and r['at_ms']<=c['end_ms']}
        counts[mechanism]=len(ids);covered.update(ids)
    return {'eligible_targets_by_mechanism':counts,'covered_union':len(covered),
            'hs_without_eligible_exit_predicate':len(members-covered),
            'no_eligible_exit_predicate':not covered,
            'multiple_hs_without_eligible_exit_predicate':len(members-covered)>=2}


def forward_case(directory):
    ledger=[json.loads(x) for x in (directory/'trades_be_off_cb_shadow.jsonl').read_text().splitlines()]
    state=json.loads((directory/'be_off_cb_shadow.json').read_text())
    hs=[r for r in ledger if r.get('exit_reason')=='HARD_STOP' and brt(ms(r['closed_at'])).startswith('2026-10-01')]
    crises=[e for e in state['audit_events'] if e.get('event')=='CIRCUIT_BREAKER_TRIGGERED' and
            brt(ms(e['ts'])).startswith('2026-10-01')]
    details=[]
    for e in crises:
        at=ms(e['ts']);positions=[r for r in ledger if ms(r['opened_at'])<at<ms(r['closed_at'])]
        details.append({'event':e,'positions':[{'opened_at':r['opened_at'],'entry':r['entry_price'],
                      'original_hs':r['entry_price']*.985,
                      'hs_gap_entry_pct':(e['price']/r['entry_price']-.985)*100} for r in positions]})
    return {'hs':hs,'crises':details,'snapshot_updated_at':state['updated_at']}


def generate(results, case):
    lines=['# Revisão defensiva — auditoria detalhada', '',
           'Diagnóstico por posição/oportunidade sobre trajetória BE_OFF_CB fixa. Valores de mecanismos não são somáveis entre si.',
           'Não há replay sistêmico de thresholds/cooldown alternativos. O resultado hipotético de um sinal negado ignora slots/spacing/conflitos com outras admissões.',
           'HIGH/LOW não são amostras independentes. Outubro parcial e futuros não disponíveis ficam censurados.',
           'Classificação de suporte é exploratória, não validação fora da amostra. 01/10 é somente ilustração.', '']
    cb_summary=[];context_summary=[];recovery_summary=[];risk_summary=[];quadrant_summary=[]
    for path,d in results.items():
        lines += [f'## {path}: HS e clusters', '']
        for month in (*MONTHS,'ALL'):
            rr=[r for r in d['trades'] if month=='ALL' or brt(r['opened_ms'])[:7]==month]
            crossed=[r for r in rr if r.get('crossed_ms') is not None and r.get('closed_ms') is not None]
            recovery_summary.append([path,month,len(crossed),percent(mean([r['exit_reason']!='HARD_STOP' for r in crossed]))])
            for name in ('ALL','BUL+BU-','OTHER'):
                chosen=[r for r in rr if name=='ALL' or ((r['entry_context']['ema_context']=='BUL' and r['entry_context']['macd_context']=='BU-')==(name=='BUL+BU-'))]
                closed=[r for r in chosen if r.get('closed_ms') is not None]
                quadrant_summary.append([path,month,name,len(chosen),len(closed),sum(r['exit_reason']=='HARD_STOP' for r in closed),percent(mean([r['exit_reason']=='HARD_STOP' for r in closed]))])
            cc=[c for c in d['crises'] if month=='ALL' or c['month']==month]
            positions=[p for c in cc for p in c['positions']]
            negative=[p for p in positions if p['pnl_pct']<0]
            blocked=[b for b in d['blocked_signals'] if month=='ALL' or b['month']==month]
            cb_summary.append([path,month,len(cc),len(positions),len(negative),
                f(min([p['hs_gap_entry_pct'] for p in negative],default=None)),
                f(median([p['hs_gap_entry_pct'] for p in negative])),sum(p['hs_gap_entry_pct']<.3 for p in negative),
                len(blocked),sum(b['closed_ms'] is not None for b in blocked),
                f(sum(b['net_usd'] for b in blocked if b['net_usd'] is not None)),
                sum(c['recovery_context_ms'] is not None for c in cc),f(median([c['recovery_minutes'] for c in cc if c['recovery_minutes'] is not None]))])
            for bucket in ('0-1h','1-2h','2-4h','4-6h'):
                bb=[b for b in blocked if b['bucket']==bucket];resolved=[b for b in bb if b['net_usd'] is not None]
                after=[b for b in resolved if any(c['at_ms']==b['crisis_ms'] and c['recovery_context_ms'] is not None and b['at_ms']>=c['recovery_context_ms'] for c in cc)]
                context_summary.append([path,month,bucket,len(bb),len(resolved),sum(b['exit_reason']=='HARD_STOP' for b in resolved),
                    f(sum(b['net_usd'] for b in resolved)),f(mean([b['net_usd'] for b in resolved])),len(after),f(sum(b['net_usd'] for b in after))])
            for label in ('below50','atleast50','insufficient'):
                risk=[r for r in d['risk_episodes'] if (month=='ALL' or r['month']==month) and r['hs_next60'] is not None]
                chosen=[r for r in risk if ('insufficient' if r['recovery20']['n']<10 else 'below50' if r['recovery20']['rate']<.5 else 'atleast50')==label]
                risk_summary.append([path,month,label,len(chosen),sum(r['hs_next60']>=2 for r in chosen),percent(mean([r['hs_next60']>=2 for r in chosen])),percent(mean([r['recovery20']['rate'] for r in chosen if r['recovery20']['rate'] is not None]))])
        rows=[]
        for c in d['clusters']:
            c.update(coverage_for_cluster(d,c))
            s=c['snapshots'][-1];ctx=s['context']
            during=Counter(e['context']['ema_context']+'/'+e['context']['macd_context'] for e in d['hs_events'] if c['start_ms']<=e['at_ms']<=c['end_ms'])
            rows.append([c['start_brt'],c['end_brt'],c['hs'],f(c['net']),s['open'],s['negative'],
                ctx['ema_context']+'/'+ctx['macd_context'],str(dict(during)),f(c['velocity5_pct_per_min']),
                f(c['dd_before']),f(c['closed_pnl4h_before']),c['cb_active_before'],len(c['cb_triggers_during']),
                c['fast_targets'],c['bear_SHO_triggers'],c['bull_LON_targets'],c['entry_BUL_BU_MINUS'],
                s['recovery20']['n'],percent(s['recovery20']['rate']),c['hs_without_eligible_exit_predicate'],c['no_eligible_exit_predicate']])
        lines+=table(['start BRT','end BRT','HS','net $','open before','negative before','context before','contexts HS',
                      'v5 %/min','DD before $','PnL4h before $','CB active','CB triggers','FAST HS targets','SHO triggers','LON HS targets',
                      'BUL BU- entries','N recovery20','recovery20','HS without eligible exit','no eligible exit'],rows)
        lines+=['"Missed" refere-se aos predicados na trajetória controle, não a uma engine combinando todas as hipóteses. CB admission-only não protege automaticamente posições existentes.',
                'Snapshots −30/−15/−5/0m, perda média/máxima, próximos ao HS, direção EMA/histograma, concentração de entradas e rate7d estão em clusters.jsonl.', '']
        # Per-crisis complete price-window coverage and actual economic alternatives.
        lines += [f'## {path}: cada crise', '']
        rows=[]
        for c in d['crises']:
            b=[r for r in d['blocked_signals'] if r['crisis_ms']==c['at_ms']]
            values=[]
            for h in ('1','2','4','6'):
                w=c['future'][h]
                values.append(f'{f(w["end_return_pct"])}/{f(w["adverse_pct"])}' if w['complete'] else 'CENSORED')
            rows.append([brt(c['at_ms']),c['open'],f(c['net_last4h']),c['closes_last4h'],f(c['realized_dd']),len(b),
                         f(sum(r['net_usd'] for r in b if r['net_usd'] is not None)),*values,f(c['recovery_minutes'])])
        lines+=table(['trigger BRT','open','prior PnL4h $','closes4h','DD $','blocked raw signals','standalone net $',
                      '1h end/adverse %','2h end/adverse %','4h end/adverse %','6h end/adverse %','bull/rising recovery min'],rows)
    lines+=['## CB: distância ao HS e cobertura por mês','',
            'Distância ao HS em % da entrada; somente posições negativas que sobrevivem aos exits do minuto do trigger.',
            'N posições/crise pode repetir a mesma posição entre crises. Resultados denied são isolados, não economia efetiva sistêmica.','']
    lines+=table(['path','month','crises','positions','negative','min gap %','median gap %','gap <0.3%','denied signals','resolved','denied standalone net $','crises recovered <=6h','median recovery min'],cb_summary)
    lines+=['## Cooldown: custo potencial em todas as crises','',
            'Amostra inclui todas as crises, sem selecionar somente recuperação. Retorno de contexto usa 5m fechado, EMA LON/BUL + MACD rising BU+/BE+. Não libera CB nem implementa cooldown novo.','']
    lines+=table(['path','month','bucket','N denied','resolved','HS','standalone net $','net/standalone $','N after context recovery','net after recovery $'],context_summary)
    lines+=['## Recuperação de trades que cruzaram −0,50%','', 'Mês da entrada; somente destino realizado disponível, sem usar censurados. Esta tabela retrospectiva não é input da rate pré-cluster.','']
    lines+=table(['path','month','N crossed/closed','avoided HS'],recovery_summary)
    lines+=['## Taxa anterior versus episódios de risco','',
            'Episódio: 2+ posições <=−0,50%, observações separadas por >=60m. Rate dos últimos20 crossers FECHADOS estritamente antes; N<10 é insuficiente. Rótulo 2+HS em próxima1h é somente diagnóstico futuro, nunca input. Não otimiza janela/cutoff.','']
    lines+=table(['path','month','prior rate bucket','N episodes','next1h 2+ HS','cluster rate','mean prior recovery'],risk_summary)
    lines+=['## BUL + BU− na admissão','', 'Correlações, não prova de gate causal; mês da entrada. OTHER é complemento, não seleção de quadrantes ótimos.','']
    lines+=table(['path','month','entry group','N admitted','resolved','HS','HS rate'],quadrant_summary)
    lines+=['## Forward 01/10 — ilustração separada','',
            'Fonte: snapshot VPS em forward_input. Usar somente a coorte pós-restart 30/09 22:03:09 BRT; não agregar dados do intervalo inválido de 29/09. Fills reais/shadow sem confundir com OHLC e spread sintético.','']
    rows=[]
    for r in case['hs']:
        en=r['market_context_entry']['tf_5m'];ex=r['market_context_exit']['tf_5m']
        rows.append([brt(ms(r['opened_at'])),brt(ms(r['closed_at'])),r['entry_price'],r['exit_price'],
                     en['ema_context']+'/'+en['macd_context'],ex['ema_context']+'/'+ex['macd_context'],
                     f(r['position_notional_usdt']*r['net_pnl_pct']/100),ex['ema50_direction'],ex['ema100_direction'],ex['ema200_direction']])
    lines+=table(['entry BRT','HS BRT','entry','exit','entry context','exit context','net $','EMA50','EMA100','EMA200'],rows)
    for c in case['crises']:
        e=c['event'];lines += [f'CB: {brt(ms(e["ts"]))}; price={e["price"]}; cooldown até {brt(ms(e["cooldown_until"]))}; realized DD=${e["realized_peak"]-e["realized_equity"]:.4f}.','']
        lines+=table(['remaining entry BRT','entry','original HS','gap entry %'],[[brt(ms(p['opened_at'])),p['entry'],f(p['original_hs']),f(p['hs_gap_entry_pct'])] for p in c['positions']])
    stats={'cb':cb_summary,'cooldown':context_summary,'recovery':recovery_summary,'risk':risk_summary,'entry_quadrant':quadrant_summary}
    return '\n'.join(lines),stats


def main():
    results={p:json.loads((OUT/f'diagnostic_{p}.json').read_text()) for p in ('HIGH_FIRST','LOW_FIRST')}
    end=json.loads((OUT/'manifest.json').read_text())['end_ms']
    for path,d in results.items():
        for r in d['counterfactual_exits']:
            assert r['at_ms']<=end
        for c in d['clusters']:
            for s in c['snapshots']:
                for key in ('recovery20','recovery7d'):
                    t=s[key]['latest_outcome_ms'];assert t is None or t<s['at_ms']
                assert s['context']['latest_closed_at_ms']<s['at_ms']
        for r in d['risk_episodes']:
            t=r['recovery20']['latest_outcome_ms'];assert t is None or t<r['at_ms']
    case=forward_case(OUT/'forward_input')
    report,stats=generate(results,case)
    (OUT/'defensive_audit.md').write_text(report,encoding='utf-8')
    (OUT/'audit_summary.json').write_text(json.dumps(stats,indent=2),encoding='utf-8')
    (OUT/'forward_oct01.json').write_text(json.dumps(case,indent=2),encoding='utf-8')
    (OUT/'coverage_clusters.jsonl').write_text(''.join(json.dumps(c)+'\n' for d in results.values() for c in d['clusters']),encoding='utf-8')
    print(f'Validated artifacts: {OUT}/defensive_audit.md',flush=True)


if __name__=='__main__':main()
