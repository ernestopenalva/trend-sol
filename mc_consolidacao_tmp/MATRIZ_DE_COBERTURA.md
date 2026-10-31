---
id: "MC-PROP-20261019-PLANO-CONSOLIDACAO-01-M01"
tipo: "MATRIZ_DE_COBERTURA"
versao: 1
status_documental: "PROPOSTO_PARA_REVISAO"
data_preparacao: "2026-10-19"
autor: "Codex; inventario documental"
protocolo_referencia: {arquivo: "../../PROTOCOLO.md", versao: "0.3.1"}
aprovacao_humana: {status: PENDENTE, aprovador: null, data: null, escopo: null}
revisao_cientifica: "REVISAO_NAO_INDEPENDENTE"
estado_cientifico: null
---

# Matriz de cobertura para consolidacao

| Familia / variante | Destino de consolidacao proposto | Evidencia recuperada | Situacao documental proposta |
|---|---|---|---|
| FAST_DROP / HS rapido | manter `MC-LIN-0001` | registros oficiais + proposta de proveniencia | sem nova linha; apenas links |
| BE_OFF_CB | proposta FAST_DROP existente; linha de saidas por referencia | proposta FAST_DROP e resumos forward recuperados | nao duplicar como nova linha |
| ACT20_GAP5 | subvariante de saida de BE_OFF_CB | rotulo `BE_OFF_CB_ACT20_GAP5_SHADOW` e tabelas forward recuperados | indexar sob BE_OFF_CB; formula, YAML e manifesto ainda nao recuperados |
| ACT10_GAP13 | subvariante de saida de BE_OFF_CB | rotulo `BE_OFF_CB_ACT10_GAP13_SHADOW` e tabelas forward recuperados | indexar sob BE_OFF_CB; formula, YAML e manifesto ainda nao recuperados |
| EMA_MACD | proposta FAST_DROP existente | `H03_EMA_MACD` e relatorios forward recuperados | referenciar proposta FAST_DROP; sem conclusao oficial |
| HIST_1M | proposta FAST_DROP existente | `H04_EMA_MACD_HIST_1M` e relatorios forward recuperados | referenciar proposta FAST_DROP; sem conclusao oficial |
| MACD_BU_MINUS | proposta FAST_DROP existente | `H02_MACD_BU_MINUS` e relatorios forward recuperados | referenciar proposta FAST_DROP; sem conclusao oficial |
| GE15 e GE45/lento | linha de filtros/admissao | auditoria de fronteira e estudos relatados | questao de sincronizacao ainda aberta |
| Gate 3 / A_NO_G3 | linha de filtros/admissao | desenho solicitado; resultado nao recuperado | proposta, nao conclusao |
| DMI C | linha de filtros/admissao | forward/replay relatados | exige recuperar artefato e convencao comum |
| DMI D (spread >=6) | mesma genealogia DMI | forward normalizado relatado | continua desfavoravel no recorte relatado; verificar artefato |
| DMI E (trajetoria) | mesma genealogia DMI | forward/contexto e estudos relatados | promissor em N pequeno; inconclusivo |
| DMI F (RSI-MA <=70) | mesma genealogia DMI | forward normalizado relatado | desfavoravel no recorte relatado; RSI in-sample |
| DMI G completo / G sem RSI | mesma genealogia DMI | replay comum relatado por regimes | nao promover; alta in-sample e todos os limites devem constar |
| GCR | filtros ou exposicao, conforme mecanismo | forward normalizado relatado | conclusao economica antiga contaminada; reavaliacao limitada |
| H2 / sizing redutor | linha de exposicao | forward normalizado e simulacao relatados | execucao assimetrica altera leitura; inconclusivo |
| Circuit breaker | linha de exposicao | replay historico/OOS e forward com fixes | replay pode ser guardado; forward ainda nao validado |
| slots/capacity/spacing H1 | linha de exposicao | pergunta e diagnostico relatados | questao aberta |
| BE/PL1 / piso economico | linha de saidas | auditoria v1.4 relatada | resultado estrutural a verificar por artefato |
| trailing / ATR corrente | linha de saidas | lab e auditoria solicitados/limitados | questao aberta |
| NPE / time_to_BE | linha de saidas | estudo observacional relatado | nao prova efeito sistemico |
| HS_BULL_ELASTIC | subvariante de protecao/saida | rotulo e tabelas forward recuperados | indexar na linha de saidas; regra e artefato completo pendentes |
| HS_BEAR_CLUSTER_EXIT | subvariante de protecao/saida | rotulo e tabelas forward recuperados | indexar na linha de saidas; regra e artefato completo pendentes |
| CB_EXIT_ALL | subvariante de saida coletiva | rotulo e tabelas forward recuperados | indexar na linha de saidas, com link a exposicao; nao confundir com CB de admissao |
| EMA/MACD/RSI / deterioracao | linha de contexto | estudo amplo e reemissao normalizada relatados | exploratorio/in-sample; sem sizing runtime |
| CLOSE_5M_DOWN, Local DD, ATR expansion | linha de contexto | detector/efficiency relatados | requer subcoorte uniforme e artefatos |
| sincronizacao 1m/5m | linha de contexto/confiabilidade | achado de fronteira GE15 | correcao e coorte limpa pendentes |
| convencao Testnet vs phantom | metodo transversal | normalizacao trade a trade relatada | candidata a convencao metodologica formal |
| replay vs forward / HIGH_FIRST-LOW_FIRST | metodo transversal | validacoes e limitacoes relatadas | registrar por experimento, nao generalizar |

## Regras de classificacao na incorporacao

- **Integridade/procedencia auditavel:** arquivo bruto, hash, versao e output podem ser conferidos. Isso nao equivale a validade cientifica.
- **Validade metodologica:** requer, alem da procedencia, desenho adequado, comparador/convecao de execucao apropriados, ausencia ou tratamento declarado de vieses relevantes, aplicabilidade e limites. 
- **Comprovado por artefato:** usar somente como afirmacao de existencia/conferencia do artefato; nunca como sinonimo automatico de conclusao valida.
- **Resultado relatado:** conservar a janela e numeros, mas nao usar como decisao operacional.
- **Hipotese:** registrar mecanismo e criterio de teste; nao registrar resultado.
- **Decisao humana:** registrar somente se existir autorizacao explícita, data e escopo.
- **Nao comparavel economicamente:** preservar como historico, marcado pela assimetria de execucao, nunca como ranking de estrategia.

## Redundancias a eliminar na fase de incorporacao

1. Explicacao de Testnet versus phantom: uma vez no metodo transversal.
2. Definicao de FAST_DROP: registros oficiais continuam a fonte.
3. Formula e platô BE/PL1: uma vez na linha de saidas.
4. Definicao de slots/spacing/path dependency: uma vez na linha de exposicao.
5. Distincao entre replay e forward: uma vez no metodo, mais a ficha de cada experimento.

## Variantes com documentacao ainda insuficiente para representacao completa

As seguintes variantes possuem referencia recuperavel para o indice, mas **nao** possuem nesta curadoria uma ficha completa de regra, versao/configuracao, coorte, convencao de execucao e artefatos verificaveis: `ACT20_GAP5`, `ACT10_GAP13`, `HS_BULL_ELASTIC`, `HS_BEAR_CLUSTER_EXIT` e `CB_EXIT_ALL`. Elas devem ser indexadas como subvariantes documentadas com `EVIDENCIA_INCOMPLETA`, nao como resultados homologados.

`EMA_MACD`, `HIST_1M` e `MACD_BU_MINUS` possuem fichas em proposta FAST_DROP acessivel, mas continuam propostos e nao oficiais; sua representacao cientifica completa depende da revisao dessa proposta e dos artefatos nela referenciados.

Esta matriz e de navegacao. Nao e um catalogo oficial de variantes e nao reclassifica qualquer conclusao sem revisao humana.
