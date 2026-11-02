# MC-PROP-20261101 — preparação concluída; execução pelo operador

A errata corrigida da MC informa HIGH=1.021 para EMA_MACD+CLUSTER+ACT10.
Ernesto aprovou a paridade nos seis casos: VIVO 1340/1341, VERDE 274/274,
EMA_MACD+CLUSTER+ACT10 1020/1021. O bloqueio documental anterior está resolvido.
As provas antigas, inclusive a interrupção por digitação, foram preservadas.

## Entrega preparada

- Pacote: `data/analysis/keyed_20261101_release/trend-sol-keyed-20261101.zip`.
- Roteiro: `docs/keyed_vps_validation_20261101.md`.
- Aprovação/derivação: `data/analysis/keyed_20261101_approved/`.
- Ferramentas: `keyed_release_check.py`, `keyed_vps_check.py` e
  `keyed_fallback_config.py`, em `tools/`.
- YAML local preparado com os três braços novos habilitados para o próximo
  startup do operador. Nenhum processo em execução foi alterado.
- DMI15/HS_BULL continuam habilitados e drenando, com novas entradas bloqueadas.
  O fallback completo só é preparado após medição reprovada e aquecida.

## Validação local

83 testes passaram, incluindo startup integrado sem rede/ordens, recuperação
causal, persistência/restauração, relatório com 12 braços e verificações do VPS.
Uma verificação final de 10 testes operacionais também passou.

A revisão corrigiu somente metadados de origem MAROLA e coerência de versão
na restauração. `METADATA_ONLY_AST_PROOF.json` demonstra que classificação,
buffer e provider permaneceram idênticos; mapa, filtros, stops, escadinha e
redutores aprovados não foram alterados. Os hashes anteriores foram preservados.

## Pendência operacional explícita

Não houve acesso ao VPS, Git, deploy, restart ou execução do bot pelo agente.
Não há RSS/PSS, taxa LIVE de UNAVAILABLE ou recuperação real do VPS medidos
nesta entrega. O teste público isolado e as coletas reais estão prontos para
execução pelo operador no roteiro; não confundir fixtures locais com produção.

Critério informado por Ernesto: após pelo menos 30 minutos aquecido, medir
antes/depois, exigir pelo menos 192 MiB disponíveis e swap sem crescimento
contínuo/paginação constante. Se falhar, desabilitar completamente somente
DMI15_TRAJECTORY_CONTEXT e HS_BULL_ELASTIC, preservando checkpoints/ledgers/
históricos, aquecer e medir novamente. Se ainda falhar, reportar antes de
qualquer outra mudança. Esse fallback não foi aplicado.

O caminho normal prevê um deploy e um restart pelo operador. Se a necessidade
do fallback só aparecer após a ativação, o mecanismo atual exige outro
stop/start, explicitado no roteiro; nenhuma ferramenta o executa automaticamente.
