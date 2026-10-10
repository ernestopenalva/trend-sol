"""Prerequisite-only audit; stop before counterfactual on geometry exceptions."""
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.be_off_cb_exit_context_study import brt

INPUT=ROOT/'data/analysis/feed_trail_revalidation_20261009'
OUT=ROOT/'data/analysis/pl_pretrail_geometry_20261009'
TOL=1e-9


def violations(data):
    trades={t['source_candle']:t for t in data['run']['trades']}
    result=[];active=0
    for d in data['details']:
        for r in d['floor_events']:
            if not r['trail_active']:continue
            active+=1
            if r['PL_stop'] is None or r['trail_stop']>r['PL_stop']+TOL:continue
            t=trades[d['source_candle']]
            floor=d['entry_price']*1.0025
            result.append({'source_candle':d['source_candle'],'opened_ms':d['opened_ms'],
                'closed_ms':t['closed_ms'],'exit_reason':t['exit_reason'],
                'entry':d['entry_price'],'entry_atr':d['entry_atr'],
                'economic_floor':floor,'economic_floor_atr':(floor-d['entry_price'])/d['entry_atr'],
                'pl_floor_atr':(r['PL_stop']-d['entry_price'])/d['entry_atr'],
                'trail_floor_atr':(r['trail_stop']-d['entry_price'])/d['entry_atr'],
                'relation':'LOWER' if r['trail_stop']<r['PL_stop']-TOL else 'TIE',
                'pl_matches_economic_floor':abs(r['PL_stop']-floor)<1e-8,**r})
    return active,result


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    m=json.loads((ROOT/'data/studies/trail_activation_gap_systemic/20261005/manifest.json').read_text())
    assert m['arms']['ACT10_GAP5']==[10,5]
    assert m['config']['risk']['profit_lock']['economic_floor']=={'enabled':True,'net_margin_pct':.05}
    assert m['config']['fees']['taker_fee_pct']==.1 and not m['config']['fees']['use_bnb_discount']
    output={};lines=['# PL antes do trailing — verificação geométrica e interrupção',
        'Janela01/06/2026 → 02/10/2026 22:28 BRT. ACT10/GAP5, paths separados. Apenas etapa1 executada. Nenhum contrafactual de continuação, mudança de trading/YAML/estado ou operação de deploy/Git.',
        'Activation10ATR e gap5ATR CONFIRMADOS. Premissa “em todo estado armado, piso TRAIL é superior ao PL” é FALSA no replay congelado.',
        'Causa: piso econômico PL = entry×(1+0,20% taxas+0,05% margem). PL efetivo é max(raw PL, piso econômico); em ATR pequeno esse piso excede+5ATR. Seu trigger também é deslocado pelo engine: PL1 dispara no piso efetivo+(5−1,5)ATR. Assim PL1 pode estar armado quando TRAIL alcança10ATR e seu candidato peak−5ATR ainda é menor que o piso PL.',
        'Exceções abaixo são snapshots de mudanças persistidos, não contagem de ticks independentes. Estados com PL não armado não têm piso PL para comparar. A reprodução direta no engine confirmou uma exceção, sem depender apenas dos labels dos artefatos.']
    for path in ['HIGH_FIRST','LOW_FIRST']:
        p=INPUT/f'{path}_ACT10_GAP5.json';data=json.loads(p.read_text());assert data['prior_full_parity']
        active,bad=violations(data);groups={}
        for r in bad:groups.setdefault(r['source_candle'],[]).append(r)
        summary={'active_recorded_states':active,'violation_recorded_states':len(bad),'sources':len(groups),
                 'relations':dict(Counter(r['relation'] for r in bad)),
                 'pl_steps':dict(Counter(r['PL_step'] for r in bad)),
                 'all_economic_floor':all(r['pl_matches_economic_floor'] for r in bad),
                 'terminal_reasons_among_sources':dict(Counter(rows[0]['exit_reason'] for rows in groups.values()))}
        output[path]={'summary':summary,'exceptions':bad,'input_sha256':hashlib.sha256(p.read_bytes()).hexdigest()}
        lines += [f'## {path}',json.dumps(summary,ensure_ascii=False)]
        table_lines=['| source BRT | entry BRT | primeiro estado BRT | PL | peak ATR | PL floor | TRAIL floor | floor econômico ATR | snapshots | destino original |',
                     '|---|---|---|---|---|---|---|---|---|---|']
        for source,rows in sorted(groups.items()):
            r=rows[0]
            table_lines.append('| '+' | '.join([brt(source),brt(r['opened_ms']),brt(r['at_ms']),r['PL_step'],
                f"{r['peak_atr']:.8f}",f"{r['PL_stop']:.10f}",f"{r['trail_stop']:.10f}",
                f"{r['economic_floor_atr']:.8f}",str(len(rows)),r['exit_reason']])+' |')
        lines.append('\n'.join(table_lines))
    lines += ['## Por que interromper e esclarecer o desenho',
        'Armar trailing e tornar trailing dominante são marcos diferentes. Logo PROFIT_LOCK não implica pico<10ATR nem trailing nunca armado. A população precisaria manter separadas as posições realmente pré10ATR das que já armaram trailing mas ainda estavam sob PL.',
        'Preservar a ladder e suprimir apenas o fechamento PL exige explicitar qual proteção executa quando PL continua sendo o maior piso: ignorar o reason PL mantendo effective_stop/ratchet PL pode continuar a interpolar preços no piso suprimido e mascarar HS/TRAIL. Não implementar esse mecanismo por inferência nesta tarefa.',
        'Relação com estudo anterior: ACT20/GAP5 e ACT10/GAP13 tiveram conversões TRAILING→PROFIT_LOCK economicamente negativas. Isso permanece registrado; as exceções ACT10/GAP5 não alteram aqueles números. Corrige-se somente a generalização de que ACT10 separaria universalmente PL pré10 e TRAIL pós10.',
        'Não foi escolhida nova activation, gap ou regra. Nenhuma interpretação econômica da continuação foi feita; etapas2–7 interrompidas conforme o guardrail explícito.']
    (OUT/'exceptions.json').write_text(json.dumps(output,indent=2),encoding='utf-8')
    (OUT/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    print(json.dumps({p:d['summary'] for p,d in output.items()}),flush=True)


if __name__=='__main__':main()
