---
id: "MC-MET-0003"
tipo: "METODOLOGIA"
versao: 1
status_documental: "INCORPORADO_FACTUAL"
data_incorporacao: "2026-10-19"
origem:
  propostas:
    - "MC-PROP-20261019-TREND-SOL-CONVERSA-02/N01"
    - "MC-PROP-20261019-DMI-NORMALIZACAO-COMPLEMENTOS-01/N02"
    - "MC-PROP-20261019-PLANO-CONSOLIDACAO-01"
aprovacao_humana:
  modalidade: "AUTORIZACAO_EXPLICITA_POR_ESCOPO"
  escopo: "Incorporação documental de conhecimento histórico; não homologa ciência nem autoriza mudança operacional."
  data_registro: "2026-10-19"
condicao_administrativa_cientifica: "PENDENTE_DE_HOMOLOGACAO"
revisao_cientifica: "REVISAO_NAO_INDEPENDENTE"
resumo_executivo: "Convenções e limites de comparabilidade foram incorporados como método; nenhum resultado econômico foi homologado."
---

# Execução, comparabilidade e validade metodológica

## Objeto

Registrar o vocabulário transversal necessário para interpretar estudos históricos do Trend-Sol sem equiparar objetos distintos. Este registro não valida nenhum replay, shadow, resultado forward ou mudança de estratégia.

## Objetos de evidência

- `FORWARD_OBSERVADO`: eventos e ledger produzidos em execução operacional. No período recuperado, REAL_A podia observar mercado real e registrar fills da Binance Spot Testnet.
- `REPLAY_SISTEMICO`: simulação cronológica com regras, posições, slots e admissões do modelo. Seu valor depende da fidelidade declarada; não é automaticamente forward.
- `RUNTIME_RECONSTRUIDO`: tentativa de reproduzir o caminho operacional a partir de dados persistidos.
- `CONTRAFACTUAL_POR_TRADE`: alteração de uma dimensão de trades já ocorridos; não reproduz, sozinho, efeito de slots, capacidade ou admissão posterior.
- `REGRA_IDEALIZADA`: objeto hipotético distinto do runtime.

## Assimetria Testnet versus phantom

Foi documentado que comparações forward podiam opor REAL_A com `exit_price` de fill Testnet a shadows registrados por preço phantom/gatilho do mercado real. Essa diferença de convenção pode contaminar comparação econômica direta.

Para reanálise de estudos legados, a convenção recuperada é normalizar REAL_A trade a trade com `exit_trigger_price` no lugar do fill de saída, preservando entrada, quantidade, taxas e a trajetória registrada; shadows mantêm o preço phantom já gravado. Não se usa slippage médio.

### Limites

Essa normalização reduz uma assimetria de **saída**, mas não torna carteiras equivalentes: entradas, admissão, spacing, capacidade, slots, posições abertas e trajetórias independentes podem continuar diferentes. Portanto, ela não autoriza concluir superioridade sistêmica, nem reprocessar automaticamente estudos antigos.

## Integridade/procedência não é validade

Código, YAML, output, hash e dados permitem auditar a existência e a proveniência de um artefato. Eles não bastam isoladamente para afirmar validade metodológica. A validade exige também desenho apropriado, comparador e convenção de execução adequados, tratamento ou declaração de vieses, escopo/população claros, métricas aplicáveis e limites explícitos.

## Convenções de replay

Preço intrabar, ordem de eventos, custos, spread, sizing, dados ausentes e paths como `HIGH_FIRST`/`LOW_FIRST` devem ser declarados por experimento. Cenários de path e custo não criam observações estatisticamente independentes. A comparação entre braços é mais interpretável quando ambos usam a mesma convenção de execução.

## Aplicação às linhas

Este registro é referência obrigatória para Gestão de Posições e Saídas, Filtros de Entrada e Admissão, Exposição e Proteção da Carteira e Contextos/Regimes. Não duplica seus resultados; apenas define o limite de interpretação.

## Pendências

Não foi recuperado nesta incorporação um manifesto completo para todos os estudos legados. Resultados preservados nas linhas permanecem `RESULTADO_RELATADO`, salvo onde o respectivo experimento oficial já tiver outra classificação documentada.
