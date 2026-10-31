---
id: "MC-PROP-20261019-PLANO-CONSOLIDACAO-01"
tipo: "PLANO_DE_CONSOLIDACAO"
versao: 1
status_documental: "PROPOSTO_PARA_REVISAO"
data_preparacao: "2026-10-19"
autor: "Codex; consolidacao documental a partir das propostas e fontes acessiveis no Drive"
protocolo_referencia: {arquivo: "../../PROTOCOLO.md", versao: "0.3.1"}
aprovacao_humana: {status: PENDENTE, aprovador: null, data: null, escopo: null}
revisao_cientifica: "REVISAO_NAO_INDEPENDENTE"
estado_cientifico: null
resumo_proposto: "Plano de incorporacao humana para reduzir duplicacao e preservar a proveniencia de resultados, sem homologar conclusoes." 
---

# Plano de consolidacao da Memoria Cientifica

## Escopo e limites

Este documento e um plano, nao uma incorporacao. Ele se baseia exclusivamente em documentos acessiveis no Drive e no historico recuperado das propostas. Nao cria resultado novo, nao altera estados cientificos, nao substitui artefatos brutos e nao autoriza alteracao de runtime.

Fontes a reconciliar:

1. `MC-PROP-20261019-RECUPERACAO-CONVERSA-01`;
2. `MC-PROP-20261019-PROV-FASTDROP-01A14FBE`;
3. `MC-PROP-20261019-TREND-SOL-CONVERSA-02`;
4. `MC-PROP-20261019-DMI-NORMALIZACAO-COMPLEMENTOS-01`;
5. `MC-PROP-20261019-LACUNAS-GE15-NPE-GATE3-TF-01`.

Regra de triagem: resultado com apenas transcript/console relatado permanece `RESULTADO_RELATADO`; resultado com codigo, YAML, dados, output e hash verificaveis ganha **integridade e procedencia auditaveis**, mas nao recebe automaticamente validade metodologica. Para propor `VALIDO`, e necessario ainda que o desenho responda a pergunta declarada, use comparador e convencao de execucao apropriados, trate vieses relevantes, declare limites e seja aplicavel ao objeto concluido. Comparacao forward entre fill Testnet e preco phantom nunca deve sustentar comparacao economica direta sem normalizacao trade a trade.

## A. Registros oficiais existentes a complementar

| Registro existente | Complemento possivel apos revisao humana | Limite |
|---|---|---|
| `MC-LIN-0001_FAST_DROP_SELETORES` | acrescentar somente vinculo de proveniencia recuperada e fronteira com gestao de posicao | nao reabrir a conclusao FAST_DROP sem novo experimento proprio |
| `MC-EXP-0001_FAST_DROP_AUDITORIA_A` | acrescentar referencia cruzada para a recuperacao de artefatos e convencoes de execucao | nao misturar resultados de BE/PL, DMI ou CB ao experimento |
| `MC-EXP-0002_FAST_DROP_IDEALIZADO_B` | registrar explicitamente que e `REGRA_IDEALIZADA`, se ainda nao estiver suficientemente claro | nao promover para runtime por este plano |
| `MC-HIP-0001..0004` e `MC-DEC-0001..0004` | nenhuma mudanca de substancia proposta; apenas links de navegacao, se aprovados | evitar duplicar conclusoes oficiais |

Nao ha evidência neste plano para alterar as conclusoes cientificas oficiais FAST_DROP. A incorporacao deve ser aditiva e referencial, nao uma reescrita.

## B. Novos registros realmente necessarios

Propoem-se cinco objetos oficiais distintos, somente se a revisao humana aprovar a separacao e a auditoria de evidencia:

1. **Metodo — Convencoes de execucao, procedencia e comparabilidade.** Objeto transversal: Testnet-fill, phantom, `exit_trigger_price`, `FORWARD_OBSERVADO`, `REPLAY_SISTEMICO`, integridade/procedencia de artefatos e limites de pareamento. Fonte principal: N01 de normalizacao e N01 de confiabilidade. Evita repetir a mesma ressalva em cada linha.
2. **Linha — Gestao de posicao e saidas.** BE, PL1–PL3, trailing, NPE, hard stop e estrutura de stop. Deve conter apenas fatos de geometria, autovalidacao e estudos de saida; FAST_DROP continua em sua linha propria.
3. **Linha — Filtros de entrada e admissao.** GE15/GE45, Gate 3, DMI C–G, spread, trajetoria, RSI-MA e GCR quando atuar como filtro/admissao. Cada variante deve ser uma hipotese/experimento ligado, nao uma linha nova.
4. **Linha — Exposicao e protecao de carteira.** H2, GCR quando atuar como sizing coletivo, circuit breaker, spacing e capacidade/slots. Deve separar simulacao de portfólio e forward observacional.
5. **Linha — Contexto, regime e confiabilidade de sinal.** EMA/MACD/RSI, deterioracao, contexto de mercado, sincronizacao 1m/5m e fidelidade replay/forward. A classificacao de estado de mercado nao pode ser promovida a mecanismo operacional sem estudo prospectivo.

Cada linha so deve receber conclusoes depois de um experimento com objeto formal, artefato verificavel e validacao de aplicabilidade. Onde isso faltar, o registro deve conter `QUESTAO_ABERTA` ou `PROPOSTA`.

## C. Conhecimento repetido e referencia cruzada proposta

| Tema repetido | Fonte primaria proposta | Onde apenas referenciar |
|---|---|---|
| Assimetria Testnet vs phantom | Metodo de comparabilidade | H2, GCR, DMI C–G, context shadows, deterioracao |
| FAST_DROP / HS rapido | registros oficiais FAST_DROP | linha de saidas e linha de exposicao |
| BE/PL1 e piso economico | linha de saidas | NPE, trailing, estudos de truncagem |
| `SLOW_GE` e contexto EMA | linha de contexto | CB e sizing apenas por link |
| Slots, spacing e path dependency | linha de exposicao | DMI/GCR/H2/CB por link |
| Convencao intrabar e fidelidade replay | metodo de comparabilidade | todos os experimentos de replay |
| Integridade/procedencia versus validade metodologica | metodo de comparabilidade | toda proposta que use artefato, hash ou output |

Nao copiar tabelas entre registros. Um registro secundario deve manter: identificador da evidencia primaria, janela, convencao de execucao e uma frase de limite.

## D. Cobertura de pesquisa e variantes

A matriz anexa enumera a cobertura. A regra de organizacao e:

- GE15, GE45/lento e Gate 3: entrada/admissao;
- DMI C–G, spread, trajetoria e RSI-MA: variantes de entrada dentro da mesma genealogia DMI;
- `EMA_MACD`, `HIST_1M` e `MACD_BU_MINUS`: variantes FAST_DROP ja recuperadas em proposta propria; devem ser indexadas por referencia a essa genealogia, sem documento paralelo;
- `ACT20_GAP5` e `ACT10_GAP13`: variantes de ativacao/folga de saida associadas ao ramo `BE_OFF_CB`; devem entrar como subvariantes da linha de saidas;
- `HS_BULL_ELASTIC`, `HS_BEAR_CLUSTER_EXIT` e `CB_EXIT_ALL`: variantes de protecao/saida; devem entrar como subvariantes da linha de saidas, com vinculo secundario a exposicao quando afetarem fechamento coletivo;
- GCR: classificar por mecanismo usado no experimento (admissao coletiva ou sizing/exposicao), sem duplicar conclusao;
- H2, CB, slots e spacing: exposicao/carteira;
- BE, PL, trailing, NPE e hard stop: saidas;
- EMA, MACD, RSI, deterioracao e sincronizacao 1m/5m: contexto/confiabilidade.

O replay G completo versus G sem RSI deve constar como `RESULTADO_RELATADO` de replay comum, com alta marcada in-sample e lateral/baixa como fora da amostra em relacao a hipotese recente. Ele nao autoriza shadow novo sem recuperacao do artefato e decisao humana.

## E. Pendencias cientificas prioritarias

1. Recuperar manifests, hashes, codigo/YAML e outputs dos estudos historicos antes de promover qualquer resultado relatado.
2. Reconciliar economicamente os forwards legados por convencao comum: GCR; DMI C–G; context shadows; H2. A normalizacao de saida reduz um viés, mas nao remove path dependency de entradas independentes.
3. Verificar a sincronizacao GE15 1m/5m com teste concorrente e coorte limpa posterior.
4. Formalizar, antes de rodar, o replay sistemico de `A_NO_G3`; a solicitacao recuperada nao contem resultado verificavel.
5. Separar NPE observacional de contrafactual sistemico; `time_to_BE` historico nao responde sozinho o custo em slots e admissoes.
6. Tratar 4h/1d + hard stop percentual como pergunta conjunta de timeframe e geometria de stop; falta experimento com unidade comparavel.
7. Para deterioracao/EMA, revalidar T0 e efficiency ratio com PnL normalizado e subcoorte historicamente uniforme; nao promover sizing apenas por resultado amplo in-sample.
8. Spacing e trailing continuam hipoteses abertas ate que haja amostra, definicao predeclarada e modelo comum de execucao.

## F. Indice e painel, somente apos incorporacao aprovada

Depois de aprovar registros e IDs, atualizar:

1. **Indice da MC:** adicionar as cinco entradas propostas, status e links cruzados aos registros FAST_DROP; nao indexar propostas como oficiais.
2. **Painel HTML:** preservar integralmente o desenho gerencial aprovado. Nao acrescentar colunas tecnicas de hash, convencao de execucao, estado cientifico ou rastreabilidade. O painel so recebe, mediante aprovacao humana explicita, resumos gerenciais de decisoes ja incorporadas; detalhes tecnicos permanecem nos registros cientificos e no indice documental.
3. **Mapa de evidencias:** uma linha por experimento, distinguindo `FORWARD_OBSERVADO`, `RUNTIME_RECONSTRUIDO`, `CONTRAFACTUAL_POR_TRADE` e `REPLAY_SISTEMICO`.
4. **Cronologia:** registrar a descoberta da assimetria Testnet/phantom como marco metodologico, sem reclassificar retroativamente cada resultado antes da auditoria correspondente.

## G. Diario de pesquisa futura

Nao criar nem implementar agora. Sua funcao futura e registrar a evolucao do raciocinio cientifico nas conversas humanas com ChatGPT e Claude: perguntas, alternativas consideradas, argumentos, discordancias, motivos das decisoes e caminhos nao adotados. Ele nao deve duplicar fichas tecnicas de experimento, manifests, dados, hashes ou conclusoes formais. Quando houver experimento, o diario apenas aponta para o registro cientifico correspondente e preserva o contexto deliberativo que aquele registro deliberadamente nao contem.

## Decisoes humanas requeridas

1. Aprovar ou ajustar os cinco objetos propostos e seus limites de escopo.
2. Definir se a normalizacao `exit_trigger_price` vira convencao obrigatoria para pesquisa comparativa forward.
3. Escolher ordem de recuperacao dos artefatos: comparabilidade legada primeiro ou lacunas GE/NPE/Gate 3 primeiro.
4. Definir quem pode promover um `RESULTADO_RELATADO` a `VALIDO` apos verificacao de artefatos.
5. Definir se o painel deve exibir resultados historicos de comparacao enviesada como "nao comparaveis" em vez de ocultá-los.

## Encerramento

Nenhum registro oficial, protocolo, indice, painel, bot, runtime, experimento ou deploy foi alterado por este plano. A proposta aguarda revisao e autorizacao humana.
