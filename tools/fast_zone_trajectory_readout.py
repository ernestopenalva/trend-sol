"""Complete metric-by-metric readout; no ranking, fitting or threshold selection."""
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.fast_zone_trajectory_study import OUT,FEATURES,WINDOWS
from tools.be_off_cb_exit_context_study import brt
from tools.fast_drop_post_trigger_study import distribution


def fmt(x):return 'N/A' if x is None else f'{x:.4f}'
def quantile_cell(d):return f"{d['N']}: "+'/'.join(fmt(d[k]) for k in ('p10','p50','p90'))


def main():
    h=json.loads((OUT/'historical_summary.json').read_text())
    recent=json.loads((OUT/'recent_analysis.json').read_text()) if (OUT/'recent_analysis.json').exists() else {}
    raw=json.loads((OUT/'recent_raw.json').read_text()) if (OUT/'recent_raw.json').exists() else {}
    main=[INTRO,'\n## Risk sets e fechamento precoce\n',
        '| caminho | janela | destino | aberto no corte | fechou antes | fechou no corte |',
        '|---|---:|---|---:|---:|---:|']
    for path,ww in h.items():
        for w,s in ww.items():
            for label,status in s['status_counts'].items():main.append(f"| {path} | {w} | {label} | {status.get('OPEN_AT_CUT',0)} | {status.get('CLOSED_BEFORE_CUT',0)} | {status.get('CLOSED_AT_CUT',0)} |")
    main += ['\n## Exemplos de dimensões econômicas e geométricas, sem ranking\n',
        'A tabela detalhada contém TODAS as métricas, na ordem congelada. Aqui ilustramos PnL no corte, ocupação da zona e inclinação de fundos: não são uma combinação/modelo selecionado por retorno.\n',
        '| caminho | janela | métrica | HS N:p10/p50/p90 | PL N:p10/p50/p90 | TRAIL N:p10/p50/p90 | Cliff HS–PL | Cliff HS–TRAIL |',
        '|---|---:|---|---|---|---|---:|---:|']
    for path,ww in h.items():
        for w,s in ww.items():
            for name in ('end_pnl','minutes_below050','bottom_slope_pct_per_min'):
                m=s['metrics'][name];g=m['distribution']
                main.append(f"| {path} | {w} | {name} | "+' | '.join(quantile_cell(g[label]) for label in ('HARD_STOP','PROFIT_LOCK','TRAILING'))+f" | {fmt(m['comparisons']['PROFIT_LOCK']['overall']['cliff_delta'])} | {fmt(m['comparisons']['TRAILING']['overall']['cliff_delta'])} |")
    main.append(INTERPRETATION)
    main += ['\n## Exceções representativas, conforme seleção congelada\n',
        'Exemplo: HS próximos à mediana TRAIL e TRAIL próximos à mediana HS do PnL conhecido aos 60m. Seleção por distância à mediana do outro grupo, não extremos escolhidos manualmente. EXCECOES.md faz isso para cada métrica/janela/comparação.\n',
        '| caminho | destino | source BRT | zona BRT | PnL aos 60m |',
        '|---|---|---|---|---:|']
    for path in h:
        exceptions=h[path]['60']['metrics']['end_pnl']['comparisons']['TRAILING']['exceptions_near_other_median']
        for label,rows in exceptions.items():
            for r in rows:main.append(f"| {path} | {label} | {brt(r['source'])} | {brt(r['zone_ms'])} | {fmt(r['value'])} |")
    if raw:
        main += ['\n## Episódio recente — checagem adicional, não calibração\n',
            f"Snapshot do estado: {brt(__import__('tools.be_off_cb_defensive_closure',fromlist=['ms']).ms(raw['state_updated_at']))}. Candles públicos Binance SOLUSDT 1m: {len(raw['public_1m'])}; erros de consulta: {raw['errors']}. Seleção por opened_at >=06/10 12:00 BRT. Valores de OPEN/unresolved não são convertidos em destino conhecido.",
            '\nFonte recente: ledger observado + reconstrução de candles públicos, NÃO ticks efetivamente recebidos/processados pela VPS. Entrada intraminuto com low abaixo da zona seria marcada como âncora incerta e excluída. Candles da entrada/saída parcial não são usados como se inteiros pertencessem à posição.\n',
            '| caminho | source | destino | qualidade |', '|---|---|---|---|']
        for path,rr in recent.items():
            for r in rr:main.append(f"| {path} | {brt(r['source'])} | {r['label']} | {r['quality']} |")
        main += ['\n### HARD_STOPs recentes: posição do PnL no corte nas distribuições históricas\n',
            '| caminho | source | zona | janela | status | PnL corte | pior PnL | percentil HS | percentil PL | percentil TRAIL |',
            '|---|---|---|---:|---|---:|---:|---:|---:|---:|']
        for path,rr in recent.items():
            for r in rr:
                if r['label']!='HARD_STOP':continue
                for w in r['windows']:
                    positions=w['historical_positions'].get('end_pnl',{})
                    percentile=lambda label:fmt(positions.get(label,{}).get('empirical_percentile'))
                    main.append(f"| {path} | {brt(r['source'])} | {brt(r['zone_ms'])} | {w['minutes']} | {w['status']} | {fmt(w['features']['end_pnl'])} | {fmt(w['features']['worst_pnl'])} | {percentile('HARD_STOP')} | {percentile('PROFIT_LOCK')} | {percentile('TRAILING')} |")
        main.append(RECENT_INTERPRETATION)
    main.append(LIMITS)
    (OUT/'RELATORIO.md').write_text('\n'.join(main),encoding='utf8')
    metrics=['# Todas as métricas — sem ranking\n\nDistribuições entre OPEN_AT_CUT; N de valores não ausentes. Célula N:p10/mediana/p90. Quantis p05/p25/p75/p95/min/max e valores completos no historical_summary.json. Diferenças em p.p., minutos, contagens ou p.p./min conforme métrica.\n']
    periods=['# Estabilidade por mês de entrada na zona\n\nN mostrado para cada comparação. N<10 recebe LOW_N, apenas guardrail descritivo, não teste estatístico de aprovação. Outubro parcial não sustenta generalização.\n']
    exceptions=['# Exceções para todas as métricas\n\nTrês casos de cada destino mais próximos da mediana do outro grupo, conforme protocolo; destino é rótulo retrospectivo.\n']
    for path,ww in h.items():
        for w,s in ww.items():
            metrics += [f'\n## {path} — {w}m\n',
                '| métrica | HS N:p10/p50/p90 | PL N:p10/p50/p90 | TRAIL N:p10/p50/p90 | HS−PL med | Cliff | KS | % HS dentro PL central90 / inverso | HS−TRAIL med | Cliff | KS | % HS dentro TRAIL central90 / inverso |',
                '|---|---|---|---|---:|---:|---:|---|---:|---:|---:|---|']
            periods += [f'\n## {path} — {w}m\n','| métrica | comparação | mês | N HS/outro | diferença mediana | Cliff | KS | suporte |','|---|---|---|---:|---:|---:|---:|---|']
            exceptions += [f'\n## {path} — {w}m\n','| métrica | comparação | destino do caso | source BRT | valor causal |','|---|---|---|---|---:|']
            for name in FEATURES:
                m=s['metrics'][name];g=m['distribution'];cells=[name]+[quantile_cell(g[label]) for label in ('HARD_STOP','PROFIT_LOCK','TRAILING')]
                for label in ('PROFIT_LOCK','TRAILING'):
                    overall=m['comparisons'][label]['overall']
                    cells += [fmt(overall.get(k)) for k in ('median_difference','cliff_delta','KS_D')]
                    cells.append(fmt(overall.get('HS_inside_other_central90_pct'))+' / '+fmt(overall.get('other_inside_HS_central90_pct')))
                    for month,e in m['comparisons'][label]['monthly'].items():
                        support='LOW_N' if min(e['N_HS'],e['N_other'])<10 else 'descritivo'
                        periods.append(f"| {name} | HS vs {label} | {month} | {e['N_HS']}/{e['N_other']} | {fmt(e.get('median_difference'))} | {fmt(e.get('cliff_delta'))} | {fmt(e.get('KS_D'))} | {support} |")
                    for origin,rr in m['comparisons'][label]['exceptions_near_other_median'].items():
                        for r in rr:exceptions.append(f"| {name} | HS vs {label} | {origin} | {brt(r['source'])} | {fmt(r['value'])} |")
                metrics.append('| '+' | '.join(cells)+' |')
    (OUT/'METRICAS.md').write_text('\n'.join(metrics),encoding='utf8')
    (OUT/'PERIODOS.md').write_text('\n'.join(periods),encoding='utf8')
    (OUT/'EXCECOES.md').write_text('\n'.join(exceptions),encoding='utf8')
    recent_lines=['# Todas as métricas recentes, mesmo protocolo\n\nPercentis só para OPEN_AT_CUT. Fechamentos precoces mantêm apenas prefixo observado, sem comparação fictícia de janela completa.\n',
        '| caminho | source | destino | janela | status | métrica | valor | pctl HS | pctl PL | pctl TRAIL |',
        '|---|---|---|---:|---|---|---:|---:|---:|---:|']
    for path,rr in recent.items():
        for r in rr:
            for w in r['windows']:
                for name in FEATURES:
                    pos=w['historical_positions'].get(name,{})
                    percentiles=[fmt(pos.get(label,{}).get('empirical_percentile')) for label in ('HARD_STOP','PROFIT_LOCK','TRAILING')]
                    recent_lines.append(f"| {path} | {brt(r['source'])} | {r['label']} | {w['minutes']} | {w['status']} | {name} | {fmt(w['features'][name])} | "+' | '.join(percentiles)+' |')
    (OUT/'RECENTES.md').write_text('\n'.join(recent_lines),encoding='utf8')
    # Present vectors already specified/extracted in the frozen protocol, not new predictors.
    event_lines=['# Amplitudes e velocidades individuais\n\nEventos de um trade são correlacionados. N_eventos NÃO é N independente. Apenas OPEN_AT_CUT; N_trade indica trades com vetor não vazio.\n',
        '| caminho | janela | vetor | destino | N trades | N eventos | p10 | p25 | p50 | p75 | p90 |',
        '|---|---:|---|---|---:|---:|---:|---:|---:|---:|---:|']
    event_summary={}
    for path in h:
        history_rows=json.loads((OUT/f'{path}_historical.json').read_text())
        event_summary[path]={}
        for minutes in WINDOWS:
            event_summary[path][str(minutes)]={}
            for key in ('recovery_amplitudes','fall_speeds','recovery_speeds'):
                event_summary[path][str(minutes)][key]={}
                for label in ('HARD_STOP','PROFIT_LOCK','TRAILING'):
                    selected=[next(w for w in r['windows'] if w['minutes']==minutes) for r in history_rows if r['label']==label]
                    vectors=[w['features']['details'][key] for w in selected if w['status']=='OPEN_AT_CUT']
                    d=distribution([v for vector in vectors for v in vector]);d['N_trades']=sum(bool(v) for v in vectors)
                    event_summary[path][str(minutes)][key][label]=d
                    event_lines.append(f"| {path} | {minutes} | {key} | {label} | {d['N_trades']} | {d['N']} | "+' | '.join(fmt(d[k]) for k in ('p10','p25','p50','p75','p90'))+' |')
    (OUT/'EVENTOS.md').write_text('\n'.join(event_lines),encoding='utf8')
    (OUT/'event_distributions.json').write_text(json.dumps(event_summary,indent=2),encoding='utf8')
    early=['# Prefixos encerrados antes/no corte\n\nNão são janelas completas. Não entram nas distribuições de OPEN_AT_CUT. Demais métricas do prefixo estão nos JSONs por trade.\n',
        '| caminho | janela pretendida | destino | source BRT | zona BRT | status | minutos observados | pior PnL prefixo | melhor PnL prefixo | PnL último close observado |',
        '|---|---:|---|---|---|---|---:|---:|---:|---:|']
    for path in h:
        history_rows=json.loads((OUT/f'{path}_historical.json').read_text())
        for r in history_rows:
            for w in r['windows']:
                if w['status'] not in ('CLOSED_BEFORE_CUT','CLOSED_AT_CUT'):continue
                f=w['features']
                early.append(f"| {path} | {w['minutes']} | {r['label']} | {brt(r['source'])} | {brt(r['zone_ms'])} | {w['status']} | {fmt(w['observed_minutes'])} | {fmt(f['worst_pnl'])} | {fmt(f['best_pnl'])} | {fmt(f['end_pnl'])} |")
    (OUT/'ENCERRADOS_ANTES.md').write_text('\n'.join(early),encoding='utf8')
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    (OUT/'readout_manifest.json').write_text(json.dumps({'protocol_sha256':digest(OUT/'protocol.json'),
        'historical_summary_sha256':digest(OUT/'historical_summary.json'),
        'recent_raw_sha256':digest(OUT/'recent_raw.json') if raw else None,
        'analysis_tool_sha256':digest(ROOT/'tools/fast_zone_trajectory_study.py'),
        'readout_tool_sha256':digest(Path(__file__))},indent=2),encoding='utf8')
    print('Reports written; all metrics retained, no ranking or model fitting.')


INTRO='''# Trajetória causal após entrada na zona FAST_DROP

## Resposta curta

Há **informação descritiva parcial e reproduzível**, sobretudo ao observar o quanto a recuperação se sustenta aos 30–60m. Não há separação suficiente para classificar destinos sem erros. O simples número de fundos, uma recuperação isolada ou a maior recuperação perdida têm sobreposição ampla; diferenças entre grupos não são uma regra validada.

Histórico: 01/06/2026 00:00 até 02/10/2026 22:28 BRT. Controle BE_OFF_CB, todos os trades fechados que cruzam a perda de -0,50%, NÃO somente os disparos FAST aprovados por speed/EMA. 876 HIGH / 877 LOW: HS433/434, PL209/194, TRAIL234/249. HIGH/LOW não duplicam N.

Protocolo foi gravado antes dos resultados e da consulta recente. Sem grid, busca de corte, combinação de features ou ajuste por OOS. Rótulos futuros ficam fora do extrator de features.

## Definições e causalidade

- Âncora: label de fim do minuto do primeiro cruzamento efetivamente visitado pela posição no replay. Exclui-se o restante desse candle; usam-se somente próximos candles fechados até o corte. Evita fazer a recuperação no restante do candle aparecer como segundos precisos reais.
- Extremos/atingimento de níveis: OHLC ordenado, parando em stop/saída efetivamente executado. Não se usa low posterior à saída do controle.
- Fundos e pernas: série de fechamentos 1m mais preço da âncora. Fundos locais confirmados por inversão de queda→alta já ocorrida dentro da janela. Último fundo não confirmado não é completado com preço futuro. O cruzamento inicial fornece direção inicial descendente.
- Recuperações: amplitude em pontos percentuais sobre entry. Melhor recuperação após PRIMEIRO fundo confirmado, não recuperação após mínimo final do trade. Velocidade por pernas monotônicas, p.p./min; pernas em andamento usam apenas trecho já observado.
- Fundos descendentes/estáveis/ascendentes: sinal da inclinação entre primeiro e último fundo confirmado; requer >=2 fundos. Estável significa igualdade na tolerância numérica 1e-12, não threshold econômico calibrado.
- Tempo abaixo: quantidade de minutos fechados abaixo do nível (proxy de exposição por fechamento, NÃO duração intraminuto exata). Fundos sucessivos têm distância ASSINADA em p.p. e intervalo em minutos; amplitudes/velocidades individuais ficam nos JSONs.
- PnL no corte é preço do último fechamento conhecido no prefixo, NÃO PnL final do trade. Fees/fills finais não são features.
- Recuperou FAST significa voltar ao nível FIXO de -0,50%, inclusive se o primeiro quote disponível cruzou com gap abaixo dele. Não se desloca esse nível para o PnL do gap. Teste específico verifica essa leitura; nenhuma métrica foi escolhida/ajustada por resultado recente.
- Distribuições principais usam somente OPEN_AT_CUT. Fechou antes/no corte: status e prefixo observado separados; jamais preencher até 15/30/60. Dados faltantes não são zero; N efetivo/missing ficam explícitos.
- Comparações: diferença das medianas, Cliff delta (rank; negativo indica HS geralmente menor), KS D (diferença de ECDF, sem selecionar o ponto de corte), e fração dentro do intervalo central90 do outro grupo, nas duas direções. Esta última NÃO é overlap de densidade estimado nem precisão de classificador.
'''

INTERPRETATION='''
## Leitura dos dados históricos

**15m:** diferenças modestas. Exemplo HIGH, PnL no corte mediano HS -0,534%, PL -0,418%, TRAIL -0,395%; intervalos p10–p90 se sobrepõem muito. Inclinação dos fundos ainda pode ter direção contrária à associação agregada em junho quando HS é comparado a TRAIL.

**30m:** a associação de recuperação sustentada fica mais visível, mas imperfeita. PnL mediano HIGH HS -0,541%, PL -0,352%, TRAIL -0,325%; Cliff HS vs TRAIL -0,372, LOW -0,368. Tempo abaixo e inclinação dos fundos apontam na mesma direção geral, sem serem combinados num score.

**60m:** no HIGH, mediana do proxy abaixo de -0,50% = 33min HS, 15,5min PL, 8min TRAIL. PnL no corte p10/mediana/p90: HS -1,097/-0,608/-0,095%; PL -0,772/-0,280/+0,265%; TRAIL -0,665/-0,243/+0,509%. Isso mantém grande região compartilhada. Cliff do PnL HS–TRAIL -0,538 HIGH / -0,523 LOW; inclinação dos fundos -0,480/-0,461. São associações de tamanho relevante, não destinos determinísticos.

**Métricas que não sustentam narrativa simples:** contagem de fundos tem medianas iguais entre destinos e diferenças de rank pequenas. A maior recuperação perdida novamente tem diferenças pequenas, ampla sobreposição e sinal mensal variável; no HS vs PL, alterna sinal em julho/agosto/setembro conforme janela. Velocidades e amplitudes individuais são reportadas integralmente, sem selecionar automaticamente a maior diferença.

**Estabilidade:** aos 30/60m, PnL no corte, tempo abaixo e inclinação tendem a repetir direção em junho–setembro, com intensidade variável. Aos 60m, Cliff PnL HS–PL varia de -0,138 em agosto a -0,566 em setembro no HIGH; HS–TRAIL -0,418 a -0,676. Não há efeito de intensidade constante entre regimes. Outubro tem apenas 2 HS vivos aos 60m HIGH e poucos PL/TRAIL: LOW_N, sem generalização. PERIODOS.md mostra TODAS as comparações e Ns.

Há seleção de sobreviventes: dos 433 HS HIGH, 115 já encerraram antes/no corte de 60m; no LOW, idem 115 de 434. Assim, a associação aos 60m é condicional ao trade continuar aberto — não identifica retrospectivamente todos os HS do universo e não pode ser usada para alegar que evitaria os HS anteriores.

Conclusão histórica: a sustentação observada carrega mais informação descritiva que o simples fato de ter um alívio, mas não se demonstrou poder preditivo out-of-sample, calibração de probabilidade, ganho econômico ou utilidade de uma regra. Nenhum modelo foi treinado e nenhuma feature foi aprovada para trading.
'''

RECENT_INTERPRETATION='''
## Leitura do episódio recente

O snapshot tem 16 trades desde a entrada de 06/10 12:00: 7 HS, 2 PL, 2 TRAIL e 5 OPEN. Onze entram na zona na reconstrução pública; cinco não entram no trecho disponível. Não são 16 destinos resolvidos nem 22 novos testes HIGH+LOW independentes.

Os sete futuros HS estão abertos nos cortes de 15/30m; aos 60m, dois já fecharam e ficam separados, restando cinco para comparação de janela completa. Aos 15m, todos os sete PnLs no corte ficam entre percentis ~63–84 da população HS HIGH: inicialmente recuperaram melhor que a mediana dos futuros HS, apesar do destino posterior. Não há separação inequívoca precoce.

Exceções à leitura determinística: source 06/10 15:18 BRT volta ao zero aos 60m e ainda termina em HS; é percentil 94 dos HS históricos e ~73 de PL/TRAIL HIGH. Source 06/10 13:25 BRT está em -0,364% aos 60m, melhor que a mediana histórica HS e dentro das populações vencedoras, mas também termina em HS. Esses casos são compatíveis com a sobreposição histórica, não falsificam uma associação probabilística nem validam uma regra.

Outros HS pioram progressivamente. Source 07/10 09:02 BRT chega a -0,955% aos 60m, percentil ~19 dos HS e ~3 de PL/TRAIL. Sources 06/10 21:43 e 21:57 pioram rápido aos 30m e já fecharam antes dos 60m; NÃO comparar seus prefixos parciais como janelas de 60m completas.

As métricas recentes HIGH/LOW coincidem porque aqui não se reexecutam exits: usam-se exits efetivamente registrados, extremos dos candles completos e mesma série de closes; a ordem intrabar não altera esses resumos. Isso não equivale a comprovação da trajetória de ticks da VPS.

Comportamento não representado pelo replay idealizado: esses dois HS recentes encerraram com perdas de cerca de -2,49%/-2,54% no preço efetivo, enquanto os HS históricos executavam -1,524625% após spread modelado. A trajetória pública antes do exit também contém perdas mais profundas. É diferença de execução/observabilidade entre fontes, não uma feature nova nem prova de mecanismo causal do atraso. Não usamos tal diferença para recalibrar os descritores.

Checagem adicional: compatível com recuperações iniciais enganosas e grande sobreposição; não é validação suficiente com N=7 HS e apenas 2/2 vencedores. O histórico já foi explorado em estudos anteriores, portanto não é um treino virgem; recente é uma checagem temporal adicional pequena, sem tuning.
'''

LIMITS='''
## Conclusão e limites

Resposta: **informação parcial, reproduzível no OHLC, mais consistente em sustentação aos 30–60m; forte sobreposição e limitações impedem classificá-la como decisão pronta.** Não concluo que FAST_DROP esteja correto porque não foi proposta alternativa; também não converto diferença retrospectiva em regra.

Não há comparação de PnL de políticas, threshold ótimo, feature vencedora selecionada, score ou shadow. Correlação entre features e entre trades/dias permanece; não se oferecem p-values de centenas de testes como aprovação. Censura por exit é informativa; risco no corte muda de população conforme horizonte. Proxy de tempo e estrutura 1m não substituem ticks. Uma diferença de distribuição não garante incremento preditivo sobre o simples PnL corrente; esse incremento não foi testado por modelo nesta rodada.

## Arquivos, testes e reprodução

Protocolo: protocol.json, gravado antes dos resultados/OOS. Histórico: HIGH_FIRST_historical.json e LOW_FIRST_historical.json (por trade, janela, features e detalhes), historical_summary.json (distribuições completas/efeitos/exceções/monthly), historical_manifest.json. Recente: recent_raw.json (ledger/candles públicos), recent_analysis.json (mesmas métricas e percentis somente de janelas completas), readout_manifest.json (hashes).

Leituras separadas: METRICAS.md (todas), EVENTOS.md (amplitudes/velocidades individuais agregadas, com N_eventos/N_trades e correlação explicitada), PERIODOS.md (todos os meses/N), EXCECOES.md (todos os descritores), ENCERRADOS_ANTES.md (prefixos históricos incompletos separados), RECENTES.md (cada trade/métrica/janela/percentis), este relatório curto. Fechamentos precoces permanecem identificados nos JSONs/RECENTES.md; não foram descartados silenciosamente ou completados.

```
python tools/fast_zone_trajectory_study.py
python tools/fast_zone_trajectory_study.py --recent-only --fetch-recent
python tools/fast_zone_trajectory_readout.py
python -m unittest tests.test_fast_zone_trajectory_study
```

O comando --fetch-recent só consulta estado/candles remotamente e salva cópias locais; sem ele, usa o snapshot local congelado. Novas fontes: tools/fast_zone_trajectory_study.py, tools/fast_zone_trajectory_readout.py, tests/test_fast_zone_trajectory_study.py. Nenhum runtime/YAML/estado/shadow alterado; nenhum deploy/restart/commit/push.

Verificação: sete testes específicos passaram, cobrindo corte causal sem futuro/tempo total, fechamento precoce sem preenchimento, fundo não confirmado, amplitudes/pernas/fundos, missing/ties/efeitos, recuperação fixa -0,50% em gap e âncora incerta em entrada intraminuto recente. A suíte completa do runtime não foi executada. Prefixos históricos encerrados têm checagem de paridade de horário/preço/motivo; nenhuma classificação depende do mínimo final.
'''

if __name__=='__main__':main()
