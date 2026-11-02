# MC-PROP-20261101 — implementação interrompida pela paridade

Status: **NÃO LIBERADO PARA DEPLOY**. Referência: ESPEC_BOT_20261101_v2.md.

## Gate econômico

O primeiro replay completo CHAVEADO_VIVO / LOW_FIRST produziu 1.340 trades
contra 1.343 do `scripts/switch_run.py` original da proposta, executado com
`MAPA_FINAL LOW_FIRST VIVO NOVO`. Os arquivos originais foram copiados sem
alteração para `data/analysis/keyed_20261101/reference/`. A execução adapta
somente caminhos locais; o motor econômico de referência não foi corrigido.

A primeira diferença de admissão econômica é 26/08/2026 01:49 BRT
(1787719740000 ms). Casa COMP/BULL, C07+ACT10, filtro HIST_1M.
O runtime bloqueou `ENTRY_BLOCKED_EMA_MACD_MUP_BU+`; a referência admitiu.
O primeiro registro diferente é o índice 762, contado a partir de zero.

Indício de causa: `Switch._entry_policy` altera `self.policy` conforme a casa,
mas a passagem para HIST não a redefine. `EmaMacdHist1mShadow._process_signal`
consulta `PolicyShadow._entry_policy` através de `super()`, podendo usar
MACD_BU_MINUS herdado de uma decisão anterior. O runtime redefine EMA_MACD
antes de HIST. MUP não passa no filtro EMA_MACD descrito na especificação.
Esse indício exige alinhamento; nenhuma regra ou referência foi alterada para
forçar paridade.

Prova: `data/analysis/keyed_20261101/PARITY_VIVO_LOW_FIRST.json`.
Os replays VERDE / LOW_FIRST foram interrompidos; CLUSTER_ACT10 e HIGH_FIRST
não foram executados. Não há paridade dos três braços nem autorização técnica
para ativá-los. Os três estão com `enabled: false` e `accept_new_entries: false`
no YAML.

## Trabalho local até a interrupção

- Recuperação assíncrona de candles 15m/5m por REST público quando faltam slots;
  somente candles fechados e disponibilidade a partir do recebimento. Sem
  preenchimento sintético e sem bloqueio REST no callback do WebSocket.
- Chaveados aguardam janela completa na inicialização; falhas posteriores
  continuam produzindo UNAVAILABLE e seguem as regras de fallback da v2.
- Classificadores V2 separados dos classificadores históricos V1. Paridade
  exata dos 14.500 rótulos de onda e 43.495 de marola_90 da proposta.
- Implementação inicial dos três shadows, mapa e parâmetros no YAML;
  casa da decisão persistida no input journal; escadinha e one-shot restaurados.
- Captura de onda/marola_90 na abertura e fechamento; relatório geral com os
  12 braços, frequência, USDT×hora e contagem de onda UNAVAILABLE.

Isso é trabalho em andamento, não uma entrega homologada. Faltam revisão final,
paridade completa, validação integrada do relatório e medição de memória no
ambiente alvo. A queda real da taxa UNAVAILABLE ainda depende de observação
após um futuro deploy autorizado; não foi alegada recuperação no VPS.

Testes novos cobrem empates V2 preservando V1, reposição causal sem candle aberto,
bloqueio VERDE sem rótulo, consumo FAST sem ação e persistência da escadinha.
Os testes antigos de shadows têm falhas de limpeza de SQLite aberto no Windows,
sem mudança desses testes ou da persistência para contorná-las.

## Operação

Nenhum comando Git, deploy, restart ou execução do bot foi realizado.
Somente testes locais e replays offline de paridade. Git e operação permanecem
com Ernesto. O objetivo continua sendo um único deploy e um único restart,
somente depois de resolver a divergência e passar os gates pendentes.
