"""Two prespecified diagnostic concepts; no alternative portfolio execution."""
import bisect
import json
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from src.monitor.entry_engine import EntrySignal
from src.monitor.fast_drop_semantics import closed_before, fast_drop_allowed, fast_drop_values
from tools.be_off_cb_defensive_review import OUT, MONTHS, Review, brt, exit_economics, summarize
from tools.ge_replay_study import SignalEvent
from tools.market_selection_study import load_candle_cache


def after_hs(records):
    times=sorted(r['closed_ms'] for r in records if r['exit_reason']=='HARD_STOP')
    output=[]
    for r in records:
        i=bisect.bisect_left(times,r['opened_ms'])-1
        if i>=0 and r['opened_ms']-times[i]<=3600000:
            output.append({'opened_ms':r['opened_ms'],'at_ms':r['opened_ms'],
                'month':brt(r['opened_ms'])[:7],'prior_hs_ms':times[i],
                'control_exit':r['exit_reason'],'control_net':r['net_usd'],
                'hypothetical_net':0.,'delta':-r['net_usd'] if r['net_usd'] is not None else None})
    return output


def later_fast(r,review):
    audit=r.get('fast_audit')
    if not audit or r.get('fast_eligible'):return None
    first=audit['crossed_ms']; end=r.get('closed_ms') or review.end
    lo=bisect.bisect_left(review.opens,first)
    for c in review.minute[lo:]:
        # Entire minute must precede the original exit. No theoretical fill at
        # the -0.5% level after price is already below it: use observed 1m close.
        if c.boundary_ms>=end:break
        target,unused=fast_drop_values(r['entry_price'],None)
        if not(r['entry_price']*.985<c.close<=target):continue
        reference=review.index.get(c.boundary_ms-5*60000)
        if not reference or not closed_before(reference.close_time_ms,c.open_time_ms):continue
        target,velocity=fast_drop_values(r['entry_price'],reference.close)
        context=review.context(c.open_time_ms)
        if not fast_drop_allowed(velocity,context['ema_context']):continue
        return {'opened_ms':r['opened_ms'],'at_ms':c.boundary_ms,'month':brt(c.boundary_ms)[:7],
                'first_evaluated_ms':first,'reference_boundary_ms':reference.boundary_ms,
                'reference_price':reference.close,'target':target,'quote':c.close,'velocity':velocity,
                'context':review.context_fields(c.open_time_ms),
                **exit_economics(r,c.close,review.notional,review.spread,review.fees)}
    return None


def main():
    metadata=json.loads((OUT/'manifest.json').read_text())
    candles={i:[c for c in load_candle_cache(ROOT/f'data/studies/be_off_cb_deterioration/klines/SOLUSDT_{i}.jsonl')
                if c.boundary_ms<=metadata['end_ms']] for i in ('1m','5m','15m')}
    signals=[SignalEvent(r['boundary_ms'],EntrySignal(**r['signal'])) for r in json.loads((OUT/'signals.json').read_text())]
    review=Review(metadata['config'],candles,signals,metadata['end_ms'])
    results={};summary=[]; lines=['# Dois conceitos para diagnóstico, não implementação', '',
        'Sem grid ou alteração dos thresholds. Hora pós-HS é a janela de cluster já declarada.',
        'AFTER_HS_1H: apagar admissões do controle dentro de 1h de um HS anterior; delta = −net original. NÃO simula slots, próximas admissões ou CB alterado.',
        'FAST_RECHECK_CLOSED_1M: após avaliação inicial reprovada, verificar novamente as MESMAS condições em fechamentos 1m posteriores, antes do exit original.',
        'Mantém perda0,50%, velocidade−0,10%/min, referência nominal5m, fórmula target/reference e EMA SHO/BEA; mesmo snapshot causal conservador no início do minuto.',
        'Execução hipotética da reavaliação usa close1m e spread/fees, nunca atribui fill retroativo ao target−0,50%. Primeiro novo match por posição, sem dupla contagem.',
        'Não replica ticks/latência forward e não mede efeitos sistêmicos. Outubro parcial. Nenhuma regra nova foi aplicada ao bot.', '']
    for path in ('HIGH_FIRST','LOW_FIRST'):
        d=json.loads((OUT/f'diagnostic_{path}.json').read_text())
        concepts={'AFTER_HS_1H':after_hs(d['trades']),
                  'FAST_RECHECK_CLOSED_1M':[v for r in d['trades'] if (v:=later_fast(r,review)) is not None]}
        results[path]=concepts
        for name,rows in concepts.items():
            monthly={m:summarize([r for r in rows if r['month']==m]) for m in MONTHS}
            s={'path':path,'concept':name,'months':monthly,'aggregate':summarize(rows)};summary.append(s)
            lines += [f'## {path} · {name}', '', '| month | N | HS | PL | TRAIL | HS savings $ | winner cost $ | delta $ |', '|---|---:|---:|---:|---:|---:|---:|---:|']
            for m,v in [*monthly.items(),('ALL',s['aggregate'])]:
                c=v['control_exits'];lines.append(f'| {m} | {v["n"]} | {c.get("HARD_STOP",0)} | {c.get("PROFIT_LOCK",0)} | {c.get("TRAILING",0)} | {v["hs_savings"]:.4f} | {v["winner_cost"]:.4f} | {v["delta"]:.4f} |')
            lines.append('')
    (OUT/'followup_summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    (OUT/'followup_trades.json').write_text(json.dumps(results),encoding='utf-8')
    (OUT/'followup_diagnostic.md').write_text('\n'.join(lines),encoding='utf-8')
    print([(s['path'],s['concept'],[(m,v['n'],round(v['delta'],4)) for m,v in s['months'].items()],s['aggregate']) for s in summary])


if __name__=='__main__':main()
