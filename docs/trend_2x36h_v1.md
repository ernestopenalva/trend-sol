# TREND_72H_2X36H_V1 — implementação e aplicação

## Regra vigente

Função central `src.monitor.price_structure.classify`, compartilhada por LIVE,
BACKFILL e REPLAY. 72 candles1h fechados, exatos e UTC-alinhados: H1=primeiros36,
H2=últimos36. Máxima high e mínima low de cada metade. Empate dentro da metade:
candle mais antigo. HH+HL=BULL; LH+LL=BEAR; HH+LL=EXPA; LH+HL=COMP. Empate entre
metades:UNDEFINED/EQUAL_EXTREME. Não há pivôs,k3,indicadores ou estado MIXED.
EXPA/COMP são geometria, não expectativa de rompimento/volatilidade futura.

Distância absoluta em horas e conflito estritamente <4h são apenas telemetria.
Jamais substituem extremos nem alteram o rótulo. Hora em formação é excluída.
Gap/insuficiência produzem UNDEFINED com motivo, não uma janela stale deslocada.
UNAVAILABLE significa ausência de telemetria válida da nova versão. OPEN significa
posição sem fechamento; não é UNDEFINED. Extremo tem horário da abertura do candle,
não horário intrahora do máximo/mínimo.

Campos persistidos:trend_open/trend_close, *_at e *_details. Details guarda versão
e rule_version, source, at, janela, último candle disponível e fechamento esperado,
topo_1/topo_2/fundo_1/fundo_2 com preço/tempo, HH/LH/HL/LL, distâncias, conflitos e
undefined_reason. Highs/lows são aliases dos mesmos extremos, não classificação
paralela. Abertura é imutável; fechamento é calculado de forma independente.
trend_close nunca alimenta decisão de admissão.

As ferramentas price_structure_study/readout/forward da investigação anterior
são históricas, não a fonte operacional vigente. A comparação pontual desta
entrega lê seus rótulos arquivados, sem executar ou auditar o algoritmo descartado.

Buffer/warm-up1h existentes, sem REST, SQLite ou escrita adicional no caminho de
abertura/fechamento. Falha observacional não impede transição econômica.
Checkpoint/restart preservam campos válidos; versão antiga não é exposta como nova.

## Reconstrução e integridade

O migrador lê JSONL e payloads SQLite incrementalmente, com índice compacto de
identidades/timestamps, sem reter todos os históricos em memória. Pré-validação,
staging e verificação usam hashes canônicos equivalentes aos anteriores; hashes
de arquivos também são calculados em streaming. JSONs de estado são lidos um por
vez. Nenhuma alteração na persistência ou nas decisões do runtime.

Marco08/10/2026 23:52 BRT: admitidos desde o marco, carry-overs vivos nele,
fechados depois e posições ainda abertas. Open/close usam timestamps reais
persistidos, não source_candle. Source=BACKFILL. OPEN histórico nunca recebe
contexto futuro de fechamento, mesmo que o destino final já seja conhecido.

Migrador offline cobre ledgers JSONL, JSONs, projeções de eventos, sidecar e
payloads history/pending dos11 journals SQLite CB. Não reprocessa pending,
não muda metadata/revision/ordinal/sequence/watermark/counts, não duplica eventos.
Só seis campos estruturais são autorizados a mudar. Hash canônico de TODOS os
demais campos deve permanecer idêntico: preços, qty, taxas, PnL/equity/peak,
stops/ladder/CB, decisões e clocks. Dados ausentes/pares ambíguos abortam antes
de modificar qualquer arquivo operacional. Rótulos antigos não ficam em colunas
paralelas. Logs brutos e estudos congelados permanecem como evidência histórica.

Backup durável dos arquivos que serão alterados, staging, checksums, fsync,
SQLite integrity_check e replace atômico por arquivo. Não é transação multiarquivo:
o processo precisa estar parado. WAL/SHM existentes causam recusa; não são apagados
ou checkpointados pelo migrador. A persistência incremental do bot não muda.
Reaplicar ao mesmo estado APPLIED é no-op. Falha capturada restaura o backup.
Power loss/SIGKILL pode deixar PREPARED: não reiniciar sem rollback/reconciliação.
Rollback verifica checksum e recusa estado econômico que já avançou.

## Comandos exatos — execução somente pelo usuário

Nada foi publicado/migrado/reiniciado na VPS pelo agente. Apenas coleta read-only,
com SQLite mode=ro serializado em memória. Cópias não são snapshot global atômico.
NUNCA publicar as cópias locais de estados/DBs sobre a VPS: isso retrocederia trades.

Publicar os fontes pelo seu procedimento habitual. Na VPS:

```bash
cd ~/trend-sol
mkdir -p data/analysis/trend_2x36h_20261017
```

Enviar apenas o cache de candles, no PowerShell local:

```powershell
scp C:/dev/trend-sol/data/analysis/trend_2x36h_20261017/SOLUSDT_1h.jsonl root@207.154.197.12:/root/trend-sol/data/analysis/trend_2x36h_20261017/
```

Parar o bot pelo seu procedimento habitual (não se presume um nome de serviço).
Manter parado até a verificação final. Na VPS, atualizar candles e preparar backfill
usando dados correntes, NÃO a cópia antiga coletada para o estudo:

```bash
cd ~/trend-sol
python tools/price_structure_backfill.py --ledger-dir data/trades --state-dir data/state --candles data/analysis/trend_2x36h_20261017/SOLUSDT_1h.jsonl --extend-market --output data/analysis/trend_2x36h_apply_v1/fresh_backfill.jsonl
python tools/trend_structure_migrate.py --root /root/trend-sol --candles data/analysis/trend_2x36h_apply_v1/fresh_backfill.candles.jsonl --backup /root/trend-sol/backups/trend_2x36h_v1 --offline-confirmed
python tools/trend_structure_migrate.py --root /root/trend-sol --backup /root/trend-sol/backups/trend_2x36h_v1 --verify --offline-confirmed
```

Preparação grava apenas análise/cache, não muda a base. Saída já existente exige
outro diretório de preparação se houver novos dados. A migração lê a base ATUAL
parada e faz backup antes de trocar arquivos. Só prosseguir com APPLIED/VERIFIED.
O manifesto do backup é a evidência canônica da migração. Manifesto antigo do
sidecar descreve sua geração original, não sua reconstrução posterior.

Verificar relatório antes de reiniciar:

```bash
python tools/forward_experiment_report.py --experiment trail_activation_gap --since "08/10/2026 23:52:00"
python tools/forward_experiment_report.py --experiment ema_macd --list-accepted --trend-audit --since "08/10/2026 23:52:00"
```

Tabela econômica global preservada. TREND COVERAGE separa os4 estados, UNDEFINED,
UNAVAILABLE e OPEN por abertura/fechamento. Listas mantêm trend_open/trend_close.
--trend-audit mostra extremos e conflitos. Futuro agrupamento usa campos da nova
versão; a economia exploratória usa exclusivamente trend_open.

Reiniciar manualmente. Aguardar novo OPEN/CLOSE com source=LIVE e a nova versão;
histórico BACKFILL não comprova captura LIVE. Rollback, somente parado e antes
de qualquer novo avanço econômico:

```bash
python tools/trend_structure_migrate.py --root /root/trend-sol --backup /root/trend-sol/backups/trend_2x36h_v1 --rollback --offline-confirmed
```

Se necessário retornar ao código anterior, fazê-lo pelo procedimento do usuário.
Nunca restaurar backup de estado antigo sobre trades novos. Nesse caso o migrador
recusa rollback e uma nova reconstrução de telemetria deve ser preparada.

## Entregas e resultados locais

Diretório `data/analysis/trend_2x36h_20261017`:

- SOLUSDT_1h.jsonl: cache auditado estendido até17/10 (classificação usa só passado).
- price_structure_backfill_v1.jsonl e manifesto:1.446 registros SOLUSDT.
- delivery_test/ e backup_delivery/: ensaio local final,39 arquivos, incluindo11 DBs,
  com invariância econômica verificada. NÃO copiar esses DBs para produção.
- study_v1/RELATORIO.md: relatório científico exploratório, matriz e estabilidade.
- study_v1/stability.json: transições por dia, durações, fronteira e conflitos.
- study_v1/concordance.json: única matriz antigo×novo, particionada por braço/path.
- study_v1/economics.json: N/oportunidades, bruto/net/PF/HS, meses e concentração.
- study_v1/hourly.json e examples.json: evidências e8 exemplos SVG/PNG de09–16/10.

Replay existente:01/06→10/10 00:58 BRT (10/10 parcial). Só reclassificação de
entradas, sem nova trajetória econômica. Oportunidade=boundary_ms dos sinais
aprovados arquivados; signal.ts do runner histórico não é usado como market time.
O método antigo entra somente na comparação científica pontual. Inputs econômicos
dos8 replays permanecem com hashes idênticos; tolerância agregada1e-8.

Estabilidade01/06→17/10 01:00 BRT:122 transições; mediana observada21h,
p10=3h,p90=53,8h. Transferência de extremo pela fronteira coincide com68 transições;
expiração com46 (categorias podem se sobrepor, não são causalidade exclusiva).
Conflitos:103 horas de topos,71 de fundos, em3314 observações. Sem histerese.
Permanências inicial/final são censuradas; distribuição complete-only também entregue.

Controle: diferenças descritivas entre grupos nos dois paths; todos têm net
agregado negativo. Meses e concentração impedem transformar isso em filtro.
EXPA alterna sinais mensais; COMP só é positivo em setembro nesta amostra.
Dados usados na definição/investigação são exploratórios, NÃO validação OOS.
Nenhuma proposta de admissão/sizing, algoritmo novo ou recalibração.

## Arquivos e testes

Modificados:src/monitor/price_structure.py, tools/price_structure_backfill.py,
tools/price_structure_report.py, tools/forward_experiment_report.py,
tests/test_price_structure_telemetry.py, tests/test_forward_experiment_report.py,
docs/price_structure_telemetry.md. Novos:tools/trend_structure_migrate.py,
tools/trend_capture_readonly.py, tools/trend_2x36h_study.py,
tests/test_trend_2x36h.py e este documento.

Testes:4 estados/empates, gaps/abertos, distâncias<,=,>4h, transferência na fronteira,
causalidade, paridade LIVE×BACKFILL×REPLAY, restart, idempotência, rollback com falha
parcial e invariância econômica. O teste de hash da revisão0d3f9dc continua conhecido
e não foi enfraquecido. No Windows o runner fecha conexões SQLite antes de limpar
temporários; PermissionError transitório de os.replace é reavaliado isoladamente.
Dependências científicas/teste só em data/analysis/, sem mudar venv/requirements.
Snapshot cacheado local medido:p50≈0,017ms,p99≈0,109ms; não é benchmark do callback VPS.

Resultado final da suíte local:600 casos,598 aprovados,1 skip e1 erro conhecido:
`test_cb_hypothesis_audit.DetectorAuditTests.test_replay_sources_match_frozen_revision`.
Ele exige os hashes da revisão0d3f9dc e aborta em bot_full_engine.py, já diferente
antes desta tarefa. Não é um teste que tenha encontrado divergência econômica.
Os testes focados novos/atualizados passaram. A invariância da migração foi também
verificada em39 arquivos copiados da VPS, além dos fixtures sintéticos.
