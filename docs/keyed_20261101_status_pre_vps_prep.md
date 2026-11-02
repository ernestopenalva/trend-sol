# MC-PROP-20261101 — paridade após a errata

As seis comparações econômicas entre o código novo e os scripts oficiais são
exatas, trade a trade. O gate documental continua fechado: a errata declara
1.020 trades para EMA_MACD+CLUSTER+ACT10/HIGH, mas tanto o bot quanto o
`ladder_run2.py` oficial produzem 1.021, todos fechados, sem posição aberta.
Nenhuma regra nem número esperado foi alterado para fechar esse gate.

| Braço | LOW bot / script | HIGH bot / script | Esperado na errata LOW / HIGH |
|---|---:|---:|---:|
| CHAVEADO_VIVO | 1340 / 1340 | 1341 / 1341 | 1340 / 1341 |
| CHAVEADO_VIVO_VERDE | 274 / 274 | 274 / 274 | 274 / 274 |
| EMA_MACD+CLUSTER+ACT10 | 1020 / 1020 | 1021 / 1021 | 1020 / 1020 |

## Evidências

Pasta: `data/analysis/keyed_20261101_errata/`.

- `FECHAMENTO_PARIDADE.json`: resultado final das seis comparações, 5.270 pares
  de registros. `all_six_trade_parities_exact=true`, `gate_passed=false`.
- `PARITY_{braço}_{path}.json`: provas individuais. Todos os 19 campos de cada
  trade são idênticos, incluindo fonte, instantes, preços, quantidade,
  notional, taxas, net, motivo, estado FAST e marcação das posições abertas.
  Apenas `pair_id` é excluído, pois identifica carteiras de nomes distintos.
- Admissões (inclusive razões), estatísticas, curva minuto a minuto,
  patrimônio diário e contribuições diárias também são exatamente iguais.
- `candidate_runs/`, `reference_full/`, `reference_runs/`: retornos completos do
  bot adaptado e da referência, além das saídas originais dos scripts.
- `reference/`: cópias sem alteração da errata, especificação, scripts e rótulos.
- `REFERENCE_CORRECTION.diff`: a única mudança no switch oficial é fixar a
  política em cada decisão de entrada. `ladder_run2.py` não mudou.
- `INPUT_VERIFICATION.json`, `INPUT_CODE_HASHES.json`, `provenance/`: hashes dos
  candles, entradas, configuração, código do bot e dependências históricas.
  Os códigos e referências permaneceram inalterados durante a rodada.
- `LABEL_PARITY.json`: 14.500 rótulos de onda e 43.495 de marola_90 exatos.

O motor de referência usa as dependências históricas congeladas; o candidato
usa os módulos atuais de `src/`, com origens e hashes registrados. Somente
caminhos locais foram adaptados nos scripts oficiais. O retorno completo foi
capturado sem mudar as regras econômicas da referência.

O replay candidato usa os métodos atuais de admissão, escadinha e redutores,
com posições e ledger em memória e amostragem OHLC de primeiros cruzamentos.
Os rótulos fornecidos são os calculados pelo classificador atual e verificados
contra os rótulos oficiais. Isso demonstra paridade econômica offline nessa
janela; não é validação integral do feed, journal ou execução em tempo real.

O processo da suíte encerrou ao detectar a discrepância HIGH contra o número
declarado. O replay LOW já estava concluído, mas sua comparação concorrente foi
cancelada. Essa comparação foi concluída depois, sem novo replay, e passou.
`PARIDADE_COMPLETA.json` e `suite.log` preservam a sequência original;
`FECHAMENTO_PARIDADE.json` reúne as seis provas finais e a pendência documental.

## Estado operacional

Os três braços novos permanecem desabilitados no YAML. Não houve alteração de
regra econômica do bot nesta retomada, nem Git, deploy, restart ou execução
do bot. Só ocorreram replays locais e comparação dos artefatos.

A discrepância 1020/1021 precisa de alinhamento antes de considerar o gate
fechado. Também continuam pendentes revisão final da integração, validação
integrada do relatório e conferência de memória no ambiente alvo. A correção
local dos candles ainda não foi observada em produção; a taxa UNAVAILABLE
após deploy não foi medida.

A tentativa anterior, com a referência defeituosa, permanece preservada em
`data/analysis/keyed_20261101/`. Seu registro documental está em
`docs/keyed_20261101_status_pre_errata.md`. O erro de política herdada do HIST
foi confirmado pela errata, e as novas provas substituem aquele bloqueio.
