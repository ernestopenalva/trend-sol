"""Presentation of FAST_DROP v2 replay; no execution of a trading policy."""
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.fast_drop_v2_study import OUT,MODES,Observer
from tools.winner_trajectory_study import dist
from tools.be_off_cb_exit_context_study import brt


def normalized_counts(path,mode):
    """Remove endpoints not visited because interpolation closed above loss.

    Only observational counters change. Trades, admissions and equity stay intact.
    """
    data=json.loads((OUT/f'{path}_{mode}_trades.json').read_text())
    observer=Observer(mode,path,None)
    observer.all_sources={r['source_candle']:r['at_ms'] for r in data['admissions'] if r['decision']=='ADMITTED'}
    excluded=0
    with (OUT/f'{path}_{mode}_evaluations.jsonl').open() as stream:
        for line in stream:
            event=json.loads(line)
            if event['point_index']>0 and event['normal_stop_first_on_segment'] and not event['predicates']['normal_priority']:
                excluded+=1;continue
            s=observer.stats[event['source']];s['zone']=True
            if not event['skipped']:
                s['attempts']+=1
                valid=event['valid_reference_evaluation'];s['valid']+=int(valid)
                s['missing_reference']+=int(event['reference'] is None)
                if valid:
                    s['blocked_speed']+=int(not event['predicates']['speed'])
                    s['blocked_EMA']+=int(not event['predicates']['EMA'])
                    s['blocked_fresh']+=int(mode=='REEVALUATE_FRESH' and not event['predicates']['fresh'])
                    s['stale_used']+=int(event['context_close'] is not None and not event['predicates']['fresh'] and mode!='REEVALUATE_FRESH')
            if event['fired']:
                s['trigger']=True;s['after_first']=s['valid']>1
    return {'counts':observer.counts(),'monthly':{m:observer.counts(m) for m in ('2026-06','2026-07','2026-08','2026-09','2026-10')},
        'excluded_unvisited_endpoints':excluded}

def fmt(value):return 'N/A' if value is None else f'{value:.3f}'

def main():
    summary=json.loads((OUT/'summary.json').read_text());lines=[HEADER]
    cache=OUT/'normalized_counts.json'
    fingerprint=hashlib.sha256((OUT/'summary.json').read_bytes()).hexdigest()
    stored=json.loads(cache.read_text()) if cache.exists() else {}
    if stored.get('summary_sha256')==fingerprint:normal=stored['counts']
    else:
        normal={p:{m:normalized_counts(p,m) for m in MODES} for p in summary}
        cache.write_text(json.dumps({'summary_sha256':fingerprint,'counts':normal},indent=2),encoding='utf8')
    for path,versions in summary.items():
        for mode,v in versions.items():
            v['counts']=normal[path][mode]['counts']
            for month,d in v['monthly'].items():d['counts_by_entry_month']=normal[path][mode]['monthly'][month]
            if 'attribution_FAST_vs_control' in v:
                a=v['attribution_FAST_vs_control']
                a['winners_sacrificed']=sum(r['control_reason'] in ('PROFIT_LOCK','TRAILING') and r['control_net']>0 and r['delta']<0 for r in a['rows'])
    # Keep original run counters intact and make corrected presentation explicit.
    (OUT/'summary_normalized.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
    lines += ['## Frequência e avaliações\n',
        '| path | versão | admitidos | zona perda | avaliados | avaliações med/p90/max | FAST | % zona | % admitidos | após 1ª válida | speed bloqueou | EMA bloqueou | freshness bloqueou | sem referência |',
        '|---|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    recommendations={}
    for path,versions in summary.items():
        for mode,v in versions.items():
            c=v['counts'];d=c['valid_evaluations_per_evaluated_trade']
            text='N/A' if mode=='CONTROL' else f"{fmt(d['median'])}/{fmt(d['p90'])}/{c['max_evaluations']}"
            lines.append(f"| {path} | {mode} | {v['admitted']} | {c['zone_trades']} | {c['evaluated_trades']} | {text} | {c['FAST']} | {fmt(c['FAST_pct_zone'])} | {fmt(c['FAST']/v['admitted']*100)} | {c['after_first_valid']} | {c['blocked_speed']} | {c['blocked_EMA']} | {c['blocked_fresh']} | {c['missing_reference']} |")
        one=versions['ONE_SHOT']['counts']['FAST'];re=versions['REEVALUATE']['counts']['FAST']
        ratio=re/one if one else None
        recommendations[path]={'reevaluation_vs_one_shot_trigger_ratio':ratio,
            'order_of_magnitude':ratio is not None and ratio>=10,
            'fresh_identical_economics':versions['REEVALUATE']['metrics']==versions['REEVALUATE_FRESH']['metrics']}
    lines += ['\n## Economia sistêmica agregada\n',
        '| path | versão | closed/open | net $ | delta net $ | PF | DD $ | HS | PL | TRAIL | FAST | crises CB | cooldown h | max simultaneous |',
        '|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for path,versions in summary.items():
        control=versions['CONTROL']['metrics']['net']
        for mode,v in versions.items():
            m=v['metrics']
            lines.append(f"| {path} | {mode} | {m['closed']}/{v['open']} | {fmt(m['net'])} | {fmt(m['net']-control)} | {fmt(m['pf'])} | {fmt(m['dd'])} | {m['HS']} | {m['PL']} | {m['TRAIL']} | {m['FAST']} | {m['crises']} | {fmt(m['cooldown'])} | {m['max_sim']} |")
    lines += ['\n## FAST pareados com o controle — componente direto, não delta sistêmico\n',
        '| path | versão | HS antecipados | economia HS $ | winners sacrificados | PL N / delta $ | TRAIL N / delta $ | sem controle fechado |',
        '|---|---|---:|---:|---:|---|---|---:|']
    for path,versions in summary.items():
        for mode in MODES[1:]:
            a=versions[mode]['attribution_FAST_vs_control'];g=a['by_control_exit']
            # Explicitly require a profitable control for the term "winner".
            winners=sum(r['control_reason'] in ('PROFIT_LOCK','TRAILING') and r['control_net']>0 and r['delta']<0 for r in a['rows'])
            pl=g.get('PROFIT_LOCK',{'N':0,'delta':0});trail=g.get('TRAILING',{'N':0,'delta':0})
            lines.append(f"| {path} | {mode} | {a['HS_anticipated']} | {fmt(a['HS_saving'])} | {winners} | {pl['N']} / {fmt(pl['delta'])} | {trail['N']} / {fmt(trail['delta'])} | {g.get('NO_CLOSED_MATCH',{}).get('N',0)} |")
    lines += ['\n## Estabilidade mensal\n\nContagens de avaliação/FAST por mês de entrada; economia por mês de fechamento. Outubro parcial até 02/10 22:28 BRT.\n',
        '| path | mês | versão | zona / avaliados / FAST | net $ | delta net $ | PF | DD $ |',
        '|---|---|---|---|---:|---:|---:|---:|']
    for path,versions in summary.items():
        for month in versions['CONTROL']['monthly']:
            control=versions['CONTROL']['monthly'][month]['metrics_by_close_month']['net']
            for mode,v in versions.items():
                c=v['monthly'][month]['counts_by_entry_month'];m=v['monthly'][month]['metrics_by_close_month']
                lines.append(f"| {path} | {month} | {mode} | {c['zone_trades']}/{c['evaluated_trades']}/{c['FAST']} | {fmt(m['net'])} | {fmt(m['net']-control)} | {fmt(m['pf'])} | {fmt(m['dd'])} |")
    lines += ['\n## Ampliação da política e freshness\n']
    for path,r in recommendations.items():
        lines.append(f"- {path}: multiplicador de FAST reavaliação/one-shot = {fmt(r['reevaluation_vs_one_shot_trigger_ratio'])}; >=10× = {r['order_of_magnitude']}; métricas reavaliação/fresh iguais = {r['fresh_identical_economics']}.")
    lines += ['\n## Checagem da semântica one-shot contra o replay anterior\n']
    for path in summary:
        old=json.loads((ROOT/f'data/studies/be_off_cb_defensive_closure/20261002/{path}_FAST_DROP_EMA.json').read_text())
        by={r['source_candle']:r for r in old['trades']}
        new=json.loads((OUT/f'{path}_ONE_SHOT_trades.json').read_text())['closed']
        changes=[r for r in new if r['source'] in by and abs(r['net_pct']-by[r['source']].get('net_pct',0))>1e-8]
        same_timing_reason=all(r['source'] in by and r['closed_ms']==by[r['source']]['closed_ms'] and r['reason']==by[r['source']]['exit_reason'] for r in new)
        delta=sum((r['net_pct']-by[r['source']]['net_pct'])*.2 for r in new if r['source'] in by)
        lines.append(f'- {path}: mesmos motivos/horários dos fechados = {same_timing_reason}; {len(changes)} preços diferem, delta de execução ${delta:+.6f} (notional $20). Diferença é preço disponível em gap, não threshold ou semântica de consumo da avaliação.')
    lines += ['\n## Ajuste estritamente observacional das contagens\n']
    for path in summary:
        lines.append(f"- {path}: endpoints além de stop normal interpolado removidos das contagens: "+str({m:normal[path][m]['excluded_unvisited_endpoints'] for m in MODES})+'. Arquivos brutos, fechamentos, admissões e resultados econômicos preservados.')
    lines.append(FOOTER)
    (OUT/'RELATORIO.md').write_text('\n'.join(lines),encoding='utf8')
    (OUT/'readout.json').write_text(json.dumps(recommendations,indent=2),encoding='utf8')
    print(json.dumps(recommendations,indent=2))

HEADER='''# FAST_DROP v2 — one-shot versus reavaliação

Controle BE_OFF_CB. Replay sistêmico 01/06/2026 00:00 até 02/10/2026 22:28 BRT, mesma configuração congelada do diagnóstico anterior. Não usa 06–07/10 para calibração ou seleção. Sem thresholds alternativos: loss<=−0,50%, velocidade nominal 5m<=−0,10%/min, EMA em {SHO,BEA}. MACD não participa.

CONTROL não toma decisões FAST; instrumenta entrada na zona para comparação. ONE_SHOT consome a primeira avaliação com referência válida antes dos testes speed/EMA. REEVALUATE verifica novamente em cada ponto modelado enquanto loss estiver na zona, sem modificar thresholds. REEVALUATE_FRESH exige adicionalmente o close 5m exatamente igual a floor(instante/5m)×5m−1ms; contexto antigo/elegível, mas não o mais recente esperado, não permite disparo. Referência ausente não consome tentativa one-shot.

## Causalidade, frequência e execução

Até quatro pontos OHLC distintos por minuto, HIGH_FIRST ou LOW_FIRST. Contexto congelado na abertura do minuto; timestamps de fechamento usados como labels de saída, não como autorização para ler dados futuros. Referência 1m é o fechamento com boundary=boundary do minuto−5min; fórmula usa preço teórico entry×0,995 e divisor nominal 5. Reavaliação não altera essa fórmula. O pico futuro não participa.

Preços de saída: primeiro cruzamento descendente pode interpolar −0,50%; avaliação posterior na zona usa preço do ponto disponível, NÃO o nível passado. Normal stop mais alto precede FAST; um stop já cruzado antes do endpoint de reavaliação é respeitado. Gaps não garantem execução no threshold. Esta convenção causal é comum às três versões, não recalibração. Pode diferir da rotina histórica que vendia no target em gaps; não se apresentam resultados antigos como se fossem paridade da execução nova. O controle tem paridade integral com os arquivos congelados.

Todas as tentativas na zona são escritas em *_evaluations.jsonl, incluindo referência ausente, predicados falsos e skip após one-shot. Skip não conta como avaliação efetiva. "Efetivamente avaliado" significa referência válida e tentativa habilitada; pode ter EMA reprovada/indisponível. Mediana/p90/máximo contam avaliações válidas entre trades efetivamente avaliados, não entre todas as admissões. Bloqueios speed/EMA podem coincidir; não somar como causas mutuamente exclusivas.

Slots, spacing, capacidade, equity realizada e CB são recalculados por braço. Atribuição de futuros HS/winners usa SOMENTE source comum com controle fechado; sinais novos por slots/CB não recebem destino inferido. Economia desses FAST pareados não equivale ao delta total sistêmico. Net/PF/DD seguem fechamentos; DD realizado, não MTM; abertas são censuradas.
'''

FOOTER='''
## Diagnóstico e recomendação

O one-shot é uma limitação **mecânica**, mas esta comparação não o identifica como a limitação econômica principal. Nas duas trajetórias, 30 FAST ocorreram somente depois da primeira avaliação válida. Disparos aumentaram 35→66 e 36→67, cerca de 1,9×, não uma ordem de magnitude. O volume de avaliações válidas, por outro lado, sobe de uma por trade para mediana ~180 e p90 ~884–900; são centenas de vezes mais avaliações no agregado. Uma futura implementação em ticks não teria necessariamente a mesma frequência de decisões deste OHLC.

Reavaliar antecipou mais HS pareados: 26→38 HIGH e 27→39 LOW. Economia nos futuros HS aumentou de $5,20/$5,40 para $7,56/$7,76. Porém, winners sacrificados passaram de 8 para 22; custo nos futuros PL/TRAIL subiu de ~$1,50 para ~$5,23/$5,24. Assim, a proteção extra não compensou o custo extra dos winners. Quantidades por destino são populações pareadas diferentes, não uma contabilidade causal que congele slots.

No sistema completo, reavaliação piorou o net em $1,1483 HIGH e $1,3378 LOW versus one-shot; PF e DD também ficaram piores. Continua melhor que BE_OFF_CB no agregado, mas devolve vantagem em setembro: delta versus controle -$0,6178/-$0,5556, enquanto one-shot ficou aproximadamente neutro/levemente positivo nesse mês. Junho/julho também são menos favoráveis na reavaliação que no one-shot; agosto melhora; outubro é parcial e pequeno. Não há superioridade robusta da v2 sobre a versão atual.

**Recomendação:** não selecionar reavaliação como substituta do one-shot nem criar shadow v2 com base nesta evidência. O one-shot continua a referência experimental atual; isso não prova que seja ótimo ou resolve a auditoria forward sem registros. A variante freshness merece apenas investigação operacional em dados com disponibilidade/ordem reais: este replay não demonstra benefício econômico incremental dela, porque não houve buffer/contexto ausente ou STALE no histórico idealizado. Não se escolhem nova cadência ou novos thresholds nesta rodada. Nenhuma versão v2 foi aprovada para avanço automático.

As duas versões de reavaliação têm trades, admissões e métricas iguais neste cache. Não interpretar essa igualdade como prova de que contexto STALE seria seguro. Não foi necessário recorrer à janela 06–07/10, nem foram selecionados parâmetros usando-a.

## Limites

Histórico de candles completos pressupõe disponibilidade no fechamento, não simula atrasos de WebSocket/buffer da VPS. Freshness igual entre versões nesse histórico não prova que a proteção seja dispensável no forward. Testes sintéticos verificam gaps/staleness, sem injetar falhas artificiais para selecionar política economicamente. A avaliação OHLC é até quatro pontos/minuto, não cada tick real; reavaliação é particularmente sensível à cadência. HIGH/LOW não são duplicação de N. Este histórico já foi explorado: não é validação forward independente.

Se o multiplicador de trades FAST alcançar >=10×, isso é explicitamente uma **política defensiva diferente**, não apenas correção semântica do FAST_DROP atual. Mesmo sem atingir 10×, quantidade de reavaliações e mudança de admissões precisam ser consideradas.

## Reprodução e arquivos

```
python tools/fast_drop_v2_study.py
python tools/fast_drop_v2_readout.py
python -m unittest tests.test_fast_drop_v2_study
```

Ferramentas novas apenas analíticas: tools/fast_drop_v2_study.py e tools/fast_drop_v2_readout.py; testes/test_fast_drop_v2_study.py. Saídas: manifest, summary, readout, oito arquivos de avaliações completas, oito arquivos de trades/admissões/contadores e este relatório. Nenhum shadow forward/runtime/YAML/estado alterado. Nenhum deploy, restart, commit ou push.

summary.json/JSONL conservam instrumentação bruta; normalized_counts.json e summary_normalized.json removem endpoints além do stop normal já executado das contagens de avaliação, sem mudar qualquer fechamento/admissão/economia. Essa remoção observacional não acrescenta predicado à política. Comparações principais usam summary_normalized.json. Verificação: 11 testes passaram, incluindo sete novos mais os módulos test_fast_drop_temporal_equivalence e test_be_off_cb_fast_drop_systemic_replay; controle com paridade integral nos dois caminhos. A suíte completa do runtime não foi executada.
'''

if __name__=='__main__':main()
