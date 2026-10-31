---
id: "MC-LIN-0005"
tipo: "LINHA"
versao: 1
status_documental: "INCORPORADO_FACTUAL"
nome: "Contextos, regimes e confiabilidade de sinais"
objetivo: "Registrar estudos observacionais de contexto e limitações de fidelidade, sem converter contexto em gate operacional."
status_operacional: null
estado_cientifico: null
condicao_administrativa_cientifica: "PENDENTE_DE_HOMOLOGACAO"
data_incorporacao: "2026-10-19"
origem:
  propostas:
    - "MC-PROP-20261019-TREND-SOL-CONVERSA-02/N04-N05"
    - "MC-PROP-20261019-DMI-NORMALIZACAO-COMPLEMENTOS-01/N03"
    - "MC-PROP-20261019-LACUNAS-GE15-NPE-GATE3-TF-01/N01"
aprovacao_humana:
  modalidade: "AUTORIZACAO_EXPLICITA_POR_ESCOPO"
  escopo: "Incorporação documental, sem homologação científica ou alteração operacional."
  data_registro: "2026-10-19"
revisao_cientifica: "REVISAO_NAO_INDEPENDENTE"
resumo_executivo: "EMA, MACD, RSI, deterioração e fidelidade replay/forward foram preservados como estudos exploratórios ou limites metodológicos."
---

# Contextos, regimes e confiabilidade de sinais

## Estudos de contexto

Foram recuperados monitores e estudos de EMA/MACD/RSI, contexto DMI e detectores de deterioração. A medição de contexto pode ser observacional e não entra automaticamente em gates, sizing ou saídas.

O estudo de deterioração usou eventos T0 econômicos retrospectivos e repricing de tamanho para avaliar detectores como SLOW_GE, preço abaixo da EMA20, consistência/slope EMA, Local DD e expansão de ATR. Uma reemissão com PnL normalizado foi relatada; seus efeitos dependem de janela, subcoorte histórica e da limitação de contrafactual por trade. Não há detector aprovado para runtime.

## Regimes e timeframes

Janelas rotuladas como alta, lateral e baixa foram usadas em replays recuperados. Esses rótulos são retrospectivos e não demonstram que regime possa ser identificado em tempo real. A pergunta 4h/1d com hard stop percentual permanece aberta porque mistura timeframe e geometria de stop.

## Confiabilidade

Fidelidade de replay exige declarar dados, warmup, candle fechado, path intrabar, taxa, spread, sizing, posições abertas e critérios de pareamento. Autovalidações locais de saída são úteis, mas não demonstram equivalência integral de carteira. A sincronização 1m/5m de GE é uma pendência técnica/metodológica explícita.

## Referências cruzadas

- filtros contextuais DMI/GE: MC-LIN-0003;
- efeitos de carteira, CB e sizing: MC-LIN-0004;
- convenções de comparabilidade: MC-MET-0003;
- FAST_DROP e variantes EMA/MACD: MC-LIN-0001 e suas hipóteses oficiais.

## Pendências

Recuperar artefatos das análises relatadas, separar descoberta de confirmação, declarar reutilização de dados e não converter correlações/contextos em regras operacionais sem estudo prospectivo.
