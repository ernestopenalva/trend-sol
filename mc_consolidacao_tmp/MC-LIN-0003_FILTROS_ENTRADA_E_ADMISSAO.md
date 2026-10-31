---
id: "MC-LIN-0003"
tipo: "LINHA"
versao: 1
status_documental: "INCORPORADO_FACTUAL"
nome: "Filtros de entrada e admissão"
objetivo: "Organizar regras de entrada, gates e variantes sem concluir que uma seleção é economicamente superior."
status_operacional: null
estado_cientifico: null
condicao_administrativa_cientifica: "PENDENTE_DE_HOMOLOGACAO"
data_incorporacao: "2026-10-19"
origem:
  propostas:
    - "MC-PROP-20261019-RECUPERACAO-CONVERSA-01"
    - "MC-PROP-20261019-DMI-NORMALIZACAO-COMPLEMENTOS-01/N01"
    - "MC-PROP-20261019-LACUNAS-GE15-NPE-GATE3-TF-01/N01"
aprovacao_humana:
  modalidade: "AUTORIZACAO_EXPLICITA_POR_ESCOPO"
  escopo: "Incorporação documental, sem homologação científica ou alteração operacional."
  data_registro: "2026-10-19"
revisao_cientifica: "REVISAO_NAO_INDEPENDENTE"
resumo_executivo: "As genealogias GE, Gate 3, DMI C–G e GCR foram registradas; a maioria dos resultados permanece relatada ou metodologicamente limitada."
---

# Filtros de entrada e admissão

## GE e Gate 3

- **GE15/GE45 lento:** há achado recuperado de risco de sincronização entre avaliações 1m e candles 5m fechados. A aritmética da regra não foi invalidada; falta recuperar patch, testes concorrentes e coorte limpa posterior.
- **Gate 3 / A_NO_G3:** existe desenho solicitado para replay sistemico que desliga apenas Gate 3. Nenhum resultado verificável foi recuperado; permanece `QUESTAO_ABERTA`.

## Genealogia DMI

| Variante | Regra recuperada |
|---|---|
| C — `DMI15_SHADOW_C` | `+DI[t] > +DI[t-3]`, `-DI[t] < -DI[t-3]`, `+DI[t] > -DI[t]`, com candles 5m fechados/Wilder-14 |
| D — `DMI15_SPREAD6_SHADOW_D` | C + spread DMI >= 6 |
| E — `DMI15_TRAJECTORY_SHADOW_E` | C + trajetória curta t/t-1 |
| F — `DMI15_RSI70_SHADOW_F` | C + RSI-MA <= 70 |
| G — `DMI15_COMBINED_SHADOW_G` | C + spread + trajetória + RSI-MA <=70 |

Resultados forward normalizados relatados indicaram D e F desfavoráveis nos recortes analisados e G como caso a reabrir, não vencedor comprovado. Replay relatado de G completo versus G sem RSI usou convenção comum, mas a alta foi in-sample e os regimes relatados não autorizam forward novo. A reconstituição de artefatos é pendente.

## GCR e contexto de entrada

GCR foi recuperado em usos de admissão/risco coletivo. Uma reanálise normalizada relatada do recorte 22–27/08 ainda mostrou resultado inferior, mas a conclusão anterior baseada no forward bruto Testnet/phantom era contaminada. Não há conclusão científica aprovada.

`DMI15_TRAJECTORY_CONTEXT_SHADOW` e `SLOW_GE_CONTEXT_SHADOW` pertencem a esta linha enquanto atuam como filtros de contexto, com referências cruzadas a MC-LIN-0005. Seus forwards normalizados relatados são exploratórios e sujeitos a N pequeno/trajectórias independentes.

## Variantes FAST_DROP ligadas a admissão

`BE_OFF_CB_EMA_MACD_SHADOW`, `EMA_MACD_HIST_1M_SHADOW` e `BE_OFF_CB_MACD_BU_MINUS_SHADOW` são localizadas por referência à genealogia oficial FAST_DROP (`MC-LIN-0001`, `MC-HIP-0002..0004`), não por registro duplicado. Sua evidência e validade permanecem as dos registros FAST_DROP correspondentes.

## Pendências

Recuperar manifests e regras exatas dos replays; validar sincronização GE; definir pré-registro para A_NO_G3; evitar que comparações forward de convenção de execução distinta sustentem ranking econômico.
