---
id: "MC-LIN-0004"
tipo: "LINHA"
versao: 1
status_documental: "INCORPORADO_FACTUAL"
nome: "Exposição e proteção da carteira"
objetivo: "Registrar sizing, slots, spacing, capacidade e circuit breakers como objetos de carteira."
status_operacional: null
estado_cientifico: null
condicao_administrativa_cientifica: "PENDENTE_DE_HOMOLOGACAO"
data_incorporacao: "2026-10-19"
origem:
  propostas:
    - "MC-PROP-20261019-TREND-SOL-CONVERSA-02/N02"
    - "MC-PROP-20261019-DMI-NORMALIZACAO-COMPLEMENTOS-01/N02"
    - "MC-PROP-20261019-RECUPERACAO-CONVERSA-01"
aprovacao_humana:
  modalidade: "AUTORIZACAO_EXPLICITA_POR_ESCOPO"
  escopo: "Incorporação documental, sem homologação científica ou alteração operacional."
  data_registro: "2026-10-19"
revisao_cientifica: "REVISAO_NAO_INDEPENDENTE"
resumo_executivo: "H2, circuit breaker, capacidade e spacing foram recuperados como pesquisa de carteira; não há vencedor homologado."
---

# Exposição e proteção da carteira

## H2 / sizing redutor

H2 foi recuperado como sizing variável por risco aberto. A comparação forward bruta REAL_A/Testnet contra H2/phantom mostrou-se sensível à convenção de saída. Em exemplo relatado, a normalização trade a trade alterou a direção da diferença agregada; isso demonstra contaminação potencial, não superioridade econômica do H2. Simulações históricas de sizing e restrições de minNotional permanecem registros de objeto específico, não validação forward.

## Circuit breaker

Regra recuperada: `COMBO_DD1P5_PNL4H0P5_MIN2` com cooldown de 6 h, baseada em DD realizado, PnL de fechamentos em janela e mínimo de fechamentos. Replays histórico/OOS e posterior implementação forward foram registrados, com correções de integração e coortes arquivadas. O replay e o forward são objetos diferentes. A documentação não homologa a hipótese nem declara o forward validado.

## Slots, spacing e capacidade

Slots próprios, spacing, admissões por candle e capacidade são mecanismos que criam path dependency. Diagnóstico H1 de clusters ascendentes/descendentes foi proposto/relatado, mas a evidência recuperada não basta para estabelecer regra nova. Qualquer contrafactual por trade deve declarar que não substitui replay de carteira.

## GCR e saídas coletivas

GCR é associado a esta linha quando representa gestão coletiva de risco/sizing; quando usado como filtro, consultar MC-LIN-0003. `CB_EXIT_ALL_SHADOW` é uma saída coletiva registrada também em MC-LIN-0002; sua evidência recuperada é incompleta e não deve ser confundida com o circuito breaker de admissão.

## Pendências

- distinguir métricas de DD realizado, MTM e risco de capital comprometido;
- recuperar artefatos e versões dos estudos H2/GCR/CB;
- testar hipóteses de spacing apenas com desenho sistêmico e pré-declarado;
- não usar resultado forward bruto de convenções distintas como critério econômico.
