---
id: "MC-LIN-0002"
tipo: "LINHA"
versao: 1
status_documental: "INCORPORADO_FACTUAL"
nome: "Gestão de posições e saídas"
objetivo: "Documentar geometria, mecanismos e variantes de saída sem promover parâmetros ou resultados a decisão operacional."
status_operacional: null
estado_cientifico: null
condicao_administrativa_cientifica: "PENDENTE_DE_HOMOLOGACAO"
data_incorporacao: "2026-10-19"
origem:
  propostas:
    - "MC-PROP-20261019-RECUPERACAO-CONVERSA-01"
    - "MC-PROP-20261019-TREND-SOL-CONVERSA-02/N03"
    - "MC-PROP-20261019-DMI-NORMALIZACAO-COMPLEMENTOS-01/N03"
    - "MC-PROP-20261019-LACUNAS-GE15-NPE-GATE3-TF-01/N01"
aprovacao_humana:
  modalidade: "AUTORIZACAO_EXPLICITA_POR_ESCOPO"
  escopo: "Incorporação documental, sem homologação científica ou alteração operacional."
  data_registro: "2026-10-19"
revisao_cientifica: "REVISAO_NAO_INDEPENDENTE"
resumo_executivo: "BE, PL, trailing e variantes de proteção foram recuperados como conhecimento histórico; nenhuma configuração foi aprovada como vencedora."
---

# Gestão de posições e saídas

## Escopo e genealogia

Esta linha reúne BREAKEVEN, PROFIT_LOCK/PL1–PL3, trailing, hard stop, NPE e variantes de proteção. FAST_DROP permanece em `MC-LIN-0001`; seus subobjetos de saída são referenciados, não duplicados.

## Fatos e resultados relatados

1. **BE/PL1 v1.4.** Auditoria estrutural relatada: entre 89 casos pós-BE cujo preço tocou PL1 efetivo, 11 teriam stop PL acima do BE e 78 manteriam o mesmo stop econômico. Em 15 PROFIT_LOCK reais relatados, todos tinham stop PL acima do BE. A explicação recuperada é o `max()` com piso econômico comum: quando `1.5*ATR <= 0.0025*entry`, PL1 efetivo pode coincidir com BE. É `RESULTADO_RELATADO` até reconciliação do artefato.
2. **Autovalidação de saída.** Um simulador de escadinha relatou 154/154 coincidências de motivo, erro absoluto médio/mediano de preço de 0.00058442/0 e de tempo 1.988/0.265 s na coorte coberta por aggTrades. Isso valida o objeto e a janela reportados, não o desempenho de carteira nem outras janelas.
3. **NPE/time_to_BE.** Estudo observacional relatado apontou mediana aproximada de 78 min para armar BE em recorte limitado. Não estima a trajetória sistêmica de um fechamento antecipado.

## Variantes indexadas

| Variante / nome exato | Destino nesta linha | Situação documental |
|---|---|---|
| `BE_OFF_SHADOW`, `BE030_SHADOW`, `BE_OFF_CB_SHADOW` | genealogia BE / proteção de saída | variantes recuperadas; sem promoção científica nesta linha |
| `BE_OFF_CB_ACT20_GAP5_SHADOW` | subvariante de ativação/folga de saída | `EVIDENCIA_INCOMPLETA`: tabela/rótulo recuperados, sem ficha integral de regra/configuração/manifesta |
| `BE_OFF_CB_ACT10_GAP13_SHADOW` | subvariante de ativação/folga de saída | `EVIDENCIA_INCOMPLETA` |
| `HS_BULL_ELASTIC_SHADOW` | hard-stop/proteção condicional | `EVIDENCIA_INCOMPLETA` |
| `HS_BEAR_CLUSTER_EXIT_SHADOW` | proteção/saída associada a cluster | `EVIDENCIA_INCOMPLETA` |
| `CB_EXIT_ALL_SHADOW` | saída coletiva; referência cruzada com MC-LIN-0004 | `EVIDENCIA_INCOMPLETA`; não confundir com CB de admissão |
| trailing por ATR e variantes de folga BE | mecanismo de saída | estudo/lab recuperado sem conclusão homologada |

## Questões abertas

- Se BE tardio ou BE desligado melhora resultado quando slots e admissão seguem trajetória própria.
- Se trailing baseado em ATR de entrada versus ATR corrente altera desempenho com base suficiente.
- Se NPE requer teste sistêmico independente.
- Se as variantes com `EVIDENCIA_INCOMPLETA` possuem regra, versões, coorte e artefatos recuperáveis.

## Limites

Não há novo pré-registro nem conclusão de que PL1, BE, NPE ou trailing deva mudar. Os resultados relatados devem ser lidos segundo `MC-MET-0003`.
