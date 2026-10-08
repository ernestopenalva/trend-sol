"""Read-only analytical presentation of the eight predeclared retention points."""
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.profit_retention_study import OUT,PREVIOUS
from tools.winner_trajectory_study import dist
from tools.winner_trajectory_readout import economics


def attribution(rows,baseline):
    by={r['source']:r for r in baseline}
    winners=sorted([r for r in baseline if r['net']>0],key=lambda r:r['net'],reverse=True)
    top={r['source'] for r in winners[:max(1,len(winners)//10)]}
    groups={'original_top_decile_winners':[], 'original_cashable_never_protected':[],
            'original_cashable_brief_1m':[], 'original_never_cashable':[]}
    earlier=0
    for r in rows:
        b=by[r['source']]
        protection=next((s['at_ms'] for s in b['stops'] if s['owner']!='HARD_STOP'),None)
        if r['armed_ms'] is not None and b['winner_at'] is not None and (protection is None or r['armed_ms']<protection):earlier+=1
        if r['closed_ms'] is None:continue
        pair={'delta':r['net']-b['net'],'net':r['net'],'original_net':b['net']}
        if r['source'] in top:groups['original_top_decile_winners'].append(pair)
        if b['eligible'] and protection is None:groups['original_cashable_never_protected'].append(pair)
        if b['winner_at'] and b['closed_ms']-b['winner_at']['at_ms']<=60000:groups['original_cashable_brief_1m'].append(pair)
        if not b['eligible']:groups['original_never_cashable'].append(pair)
    return {'armed_before_original_protection':earlier,
        'age_min':dist([(r['closed_ms']-r['opened_ms'])/60000 for r in rows if r['closed_ms'] is not None]),
        'groups':{k:{'N':len(s),'delta':sum(r['delta'] for r in s),
                     'original_net':sum(r['original_net'] for r in s),
                     'candidate_net':sum(r['net'] for r in s)} for k,s in groups.items()}}


def number(x):return 'N/A' if x is None else f'{x:.3f}'


def main():
    summary=json.loads((OUT/'summary.json').read_text());extra={}
    text=[HEADER]
    text.append('## Controle integral de referência\n\n| caminho | closed | net $ | PF | DD realizado $ |\n|---|---:|---:|---:|---:|')
    for path in summary:
        baseline=json.loads((PREVIOUS/f'{path}_trajectories.json').read_text());e=economics(baseline)
        text.append(f"| {path} | {len(baseline)} | {e['net']:.3f} | {number(e['PF'])} | {e['realized_DD']:.3f} |")
    text.append('## Resultado agregado por ponto\n\nNet/PF/DD são dos fechados da política; delta usa somente pares resolvidos. OPEN é censurado. Não são curvas sistêmicas executáveis.\n')
    text.append('| Caminho | Família/ponto | closed/open | delta pareado $ | net $ | PF | DD $ | giveback ATR med | captura MFE med % |\n|---|---|---:|---:|---:|---:|---:|---:|---:|')
    for path,points in summary.items():
        baseline=json.loads((PREVIOUS/f'{path}_trajectories.json').read_text());extra[path]={}
        for key,s in points.items():
            rows=json.loads((OUT/f'{path}_{key}_trades.json').read_text())
            extra[path][key]=attribution(rows,baseline)
            e=s['economics_closed'];capture=s['captured_fraction']['median']
            text.append(f"| {path} | {key} | {s['closed']}/{s['open']} | {s['paired_delta']:.3f} | {e['net']:.3f} | {number(e['PF'])} | {e['realized_DD']:.3f} | {number(s['giveback_atr']['median'])} | {number(None if capture is None else capture*100)} |")
    text.append('\n## Delta mensal ($), por admissões fixas\n\n| Caminho | ponto | junho | julho | agosto | setembro | outubro parcial |\n|---|---|---:|---:|---:|---:|---:|')
    for path,points in summary.items():
        for key,s in points.items():
            values=[number(s['monthly'].get('2026-'+m,{}).get('delta')) for m in ('06','07','08','09','10')]
            text.append('| '+path+' | '+key+' | '+' | '.join(values)+' |')
    text.append('\n## Atribuição por destino original e grandes winners\n\nAs classes futuras são rótulos de diagnóstico, nunca inputs da política. Grupos podem se sobrepor.\n\n| Caminho | ponto | HS delta $ | PL delta $ | TRAIL delta $ | top decil winners delta $ | zona positiva nunca protegida delta $ | proteção antecipada N |\n|---|---|---:|---:|---:|---:|---:|---:|')
    for path,points in summary.items():
        for key,s in points.items():
            g=extra[path][key];reasons=s['by_original_exit']
            values=[number(reasons.get(k,{}).get('delta')) for k in ('HARD_STOP','PROFIT_LOCK','TRAILING')]
            values += [number(g['groups'][k]['delta']) for k in ('original_top_decile_winners','original_cashable_never_protected')]
            text.append('| '+path+' | '+key+' | '+' | '.join(values)+f" | {g['armed_before_original_protection']} |")
    text.append('\n## Concentração dos deltas\n\n| Caminho | ponto | delta sem top 3 benefícios $ | delta sem top 3 custos $ | pares resolvidos | alterados |\n|---|---|---:|---:|---:|---:|')
    for path,points in summary.items():
        for key,s in points.items():
            text.append(f"| {path} | {key} | {s['delta_without_top3_benefits']:.3f} | {s['delta_without_top3_costs']:.3f} | {s['paired_N']} | {s['changed']} |")
    text.append(FOOTER)
    (OUT/'attribution.json').write_text(json.dumps(extra,indent=2),encoding='utf8')
    (OUT/'RELATORIO.md').write_text('\n'.join(text),encoding='utf8')


HEADER='''# Exploração limitada de retenção de lucro — protocolo e resultados

Controle: BE_OFF_CB. Histórico 01/06/2026 00:00–02/10/2026 22:28 BRT, outubro parcial. Fonte congelada e custos idênticos ao diagnóstico anterior: notional $20, spread round-trip 5bps, taxas round-trip 0,20%. Todas as 1.542/1.543 admissões do controle entram, não apenas winners ou futuros HS. HIGH/LOW não duplicam N.

## Limite explícito da exploração

No máximo 3 famílias mecanicamente distintas e 4 parametrizações representativas por família. Foram usadas **2 famílias × 4 pontos**, gravados em `predeclared_plan.json` antes de obter resultados. Os pontos revelam forma econômica, não buscam máximo retorno. Não se acrescentam pontos após ver o melhor. Quatro pontos insuficientes significam sensibilidade elevada/especificação insuficiente, não autorização para ampliar a varredura. Um ponto isolado superior não prova robustez.

## Não depender de previsão de continuação

Nenhuma família usa EMA/MACD, velocidade, regime ou classificação de destino futuro. Progresso é apenas uma escala econômica da fronteira: não se presume que separe futuros HS de PL/TRAIL. O benefício de ambas depende da distribuição realizada de retrações e custos, mas **não de acertar previamente a direção futura**. O diagnóstico anterior não sustenta tal poder preditivo; geometria não é previsão.

## Problema e zona sem proteção

O controle teve 227/228 trades com pico liquidavelmente positivo que acabaram em HS sem PL armado. Também preservou grandes winners que uma liquidação genericamente apertada sacrificaria. A zona positiva antes da proteção abrange todos os destinos. Aqui medimos proteção antecipada, positivos nunca protegidos no controle, grandes winners e trades brevemente positivos; estes últimos são um grupo descritivo de duração até saída <=1 candle 1m, não gatilho de decisão.

## Duas famílias, sem arquitetura favorecida

Em pontos percentuais: G = lucro líquido liquidável no pico conhecido, após taxas/spread; C = taxa round-trip conhecida; A = ATR de entrada / entry ×100; R = lucro líquido mínimo preservado. O stop correspondente converte R de volta a preço, incluindo taxas/spread de venda. O entry já incorpora spread de compra. R só arma quando estritamente positivo, nunca afrouxa; o HS original -1,5% permanece. PL e Trail são substituídos em toda a posição, não combinados silenciosamente com a fronteira.

**Afim com orçamento de custos:** R=qG−C, q={0,25;0,50;0,75;1,00}. Pontos equidistantes cobrem baixa a alta retenção. A subtração C é orçamento adicional deliberado de tolerância, não cobrança duplicada de fees no net. Lucros pequenos ainda não armam; nos grandes, giveback líquido permitido cresce como (1−q)G+C. Relação ATR: nenhuma na regra, apenas diagnóstico. Benefício potencial: retenção proporcional simples; risco: interromper winners modestos e tornar a política praticamente Trail percentual. q=1 equivale a orçamento absoluto de giveback líquido de um custo round-trip. Não é arquitetura inédita.

**Orçamento côncavo de giveback:** R=G−b√(GA), b={0,5;1;2;4}. Pontos multiplicativos representam quatro escalas amplas, não busca fina. Arma quando G>b²A; lucros pequenos ficam livres, grandes retêm fração crescente, enquanto tolerância absoluta continua aumentando. Custos entram em G e na conversão do stop; ATR é fixo de entrada, nunca recalculado retrospectivamente. Benefício potencial: conciliar folga absoluta com retenção proporcional maior nos grandes winners. Risco: alta sensibilidade à escala ATR e proteção tardia com b alto; não pressupõe que um winner grande continuará.

Ambas podem implicitamente reintroduzir um piso semelhante a BE econômico ao começar a proteger lucro. Isso é explicitado, não escondido. Nenhuma depende de contexto. Se o custo dos winners superar a proteção, a família falha, mesmo que evite muitos HS.

## Execução causal e interpretação

O stop anterior é avaliado antes de atualizar pico/fronteira; candle HIGH/LOW é percorrido na ordem indicada, cruzamento descendente interpolado no stop, gap no open executado no preço disponível com spread. Pico futuro nunca protege low passado. Custos iguais para controle/candidatas. Candidatas podem sobreviver à saída do controle e continuar até fim do cache; OPEN não recebe net fictício. MFE/captura são do percurso de cada política, não do pico futuro do controle.

Admissões são fixas: não se recalculam CB, equity de admissão, capacity ou spacing. Sobreposições podem exceder capacidade real; a curva realizada é contrafactual pareada, não sistema negociável. Resultado positivo exige replay sistêmico posterior. O controle foi reconstruído com paridade integral no estudo anterior. Testes focados cobrem causalidade, custos e HS; as fórmulas alternativas são analíticas, não runtime.
'''

FOOTER='''
## Forma da resposta e decisão

**Afim: sensível, sem platô demonstrado.** q=0,50 melhora o agregado +$18,49/+19,83 e DD nas duas trajetórias, mas junho/julho não melhoram e os pontos vizinhos não repetem o benefício. Sem os três maiores benefícios, o delta continua +$10,81/+10,84: não é só top 3, mas também não é estabilidade paramétrica. Aumentar q amplia economia nos HS e simultaneamente aumenta o custo dos TRAIL. No q=0,50 os futuros PL contribuem +$11,06/+12,83; os grandes winners (decil superior original) perdem $16,47/$14,53. A melhora não significa retenção melhor em toda a população. O atraso mediano até proteção é 48m, contra 37m até PL no diagnóstico do controle (populações/marcos diferentes; não comparação pareada de tempos).

**Côncava: altamente sensível e dependente do regime.** b=0,5 captura muito MFE mediano e evita perdas em futuros HS, mas sacrifica $70,76/$81,33 nos futuros TRAIL; o agregado muda de sinal HIGH/LOW. b=1 piora ambos; b=2 melhora modestamente ambos, sem melhora em junho e praticamente neutro julho. b=4 tem grande delta pareado +$71,27/+57,47, mas piora junho/julho, preserva 2 OPEN censurados e aumenta DD para ~$73,44/$73,48 contra $66,35/$52,59 do controle. Seus ganhos permanecem sem top 3, mas isso não elimina concentração temporal ou dependência de slots/CB. Não é solução da zona inicial: proteção antecipada N=0, atraso mediano até armar 338,5m e 566/567 trades positivos nunca armados. É sobretudo mudança da liberdade de holding em regimes posteriores.

Não apareceu uma região ampla de melhora simultânea de retenção, custo dos winners, meses e execução. Quatro pontos foram suficientes para revelar instabilidade, mas não para especificar uma fronteira robusta. Conforme o limite solicitado, **não haverá ampliação automática da varredura**. Não seleciono um ponto como candidato robusto nem avanço automaticamente qualquer família para replay sistêmico. Classificação das duas: **precisam de mais evidência / especificação econômica**, não prontas para shadow. O resultado também não prova superioridade conceitual definitiva de PL+Trail.

Se uma etapa posterior for autorizada, o objetivo teria de ser pré-especificado e validado sistêmicamente, não procurar pontos próximos do melhor net desta tabela. Slot/CB/admissões poderiam inverter justamente os ganhos de holdings mais longos. Não há recomendação operacional nesta entrega.

## Limitações e falsificação

As tabelas não escolhem automaticamente vencedor. O arquivo `summary.json` inclui mediana/p10/p90 de PnL e deltas, giveback e captura, regiões de progresso, duração sem proteção e positivos nunca armados; `attribution.json` contém grupos brevemente positivos, não positivos e top decil de winners original. Top decil é rótulo retrospectivo para atribuição, nunca entrada de regra. A dependência dos três maiores benefícios/custos é reportada nos dois sentidos.

Falsificação da afim: não apresentar região ampla de melhora, custo nos winners superar economia nos HS, depender de um q exato ou mudar de sinal por mês/caminho. Falsificação da côncava: deslocamento pequeno da escala causar mudança grande/instável, deixar zona positiva sem proteção ou capturar apenas poucos losses evitados à custa de grandes winners. Não se altera o protocolo para salvar uma família que falhe.

Sensibilidade de execução é delimitada por HIGH/LOW e pelo tratamento causal de cruzamento/gap. Isso não cobre distribuição de ticks, spreads variáveis ou latência real; não foram adicionados parâmetros de custo após resultados. Histórico já amplamente explorado não é OOS virgem. Não há intervalos de confiança que tratem trades correlacionados como independentes.

## Reproduzir

```
python tools/profit_retention_study.py
python tools/profit_retention_readout.py
python -m unittest tests.test_profit_retention_study tests.test_winner_trajectory_study
```

Criados apenas `tools/profit_retention_study.py`, `tools/profit_retention_readout.py`, `tests/test_profit_retention_study.py` e artefatos analíticos desta pasta. Os arquivos do estudo anterior são preservados. Saídas: plano pré-declarado, 16 arquivos de trades, summary, attribution e relatório. Nenhum shadow/runtime/YAML/estado alterado; nenhum deploy, restart, commit ou push.

Verificação: 12 testes focados passaram (5 novos e 7 do diagnóstico anterior); `git diff --check` sem erros. A suíte completa do runtime não foi executada. O ajuste posterior da ferramenta apenas preserva o plano existente em reruns: não muda pontos, fórmulas ou resultados. O hash da ferramenta que executou esta rodada está no plano original, preservado.
'''

if __name__=='__main__':main()
