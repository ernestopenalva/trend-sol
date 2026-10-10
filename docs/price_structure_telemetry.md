# Contexto estrutural — telemetria, não estratégia

## Entrega local — 10/10/2026

Paridade: 3.148 snapshots completos do estudo, sem divergências. Backfill:
340 registros SOLUSDT (267 fechados, 73 abertos), identidades únicas, todos BACKFILL.
Candles estendidos até o fechamento de 10/10 10:59:59.999 BRT. Último fechamento
nas cópias de ledger: 10/10 10:15:17 BRT. Não afirmar que a cópia seja um snapshot
atômico ou que inclua acontecimentos posteriores à leitura.

12 testes novos passaram. Suíte completa: 590 casos, 588 passaram, 1 skipped,
1 erro conhecido: test_cb_hypothesis_audit.DetectorAuditTests.
test_replay_sources_match_frozen_revision compara hash dos fontes à revisão
0d3f9dc e aborta porque bot_full_engine.py atual difere dessa revisão congelada.
Não é teste de paridade econômica. Esse teste não foi removido nem alterado.
Houve também erro transitório Windows de os.replace em uma execução intermediária;
na repetição ele desapareceu. Dependências ausentes foram instaladas exclusivamente
no diretório local de teste, sem alterar venv/requirements do bot.

Arquivos desta tarefa (preservados os estudos locais preexistentes):

- src/monitor/price_structure.py — novo cálculo/buffer causal.
- src/app.py — alimentação observacional 1h.
- src/position/position_base.py — captura e persistência.
- src/position/bot_full_engine.py — restauração e eventos.
- src/position/server_simple_trail.py — restauração e eventos.
- src/trade_ledger.py — campos adicionais.
- tools/price_structure_backfill.py — novo backfill auditado.
- tools/price_structure_report.py — novo overlay/agregação.
- tools/forward_experiment_report.py — listagens.
- tools/forward_matrix_audit.py — listagem da auditoria de matriz.
- tests/test_price_structure_telemetry.py — novos testes.
- tests/test_forward_experiment_report.py — cabeçalho e valores da listagem.
- docs/price_structure_telemetry.md — procedimento e limitações.

Os arquivos novos também precisam ser incluídos na publicação realizada pelo
usuário. Nenhuma mudança de YAML, estratégia, threshold, deploy ou restart.

Versão: `PRICE_STRUCTURE_72H_1H_K3_V1`. Função única em
`src/monitor/price_structure.py`, compartilhada pelo runtime e backfill.

72 candles 1h UTC-alinhados já fechados, terminando na última hora completa
antes do timestamp do evento. Pivôs estritos k=3 inteiramente dentro da janela;
somente os dois últimos highs e lows confirmados. HH+HL=BULL, LH+LL=BEAR,
demais=MIXED, insuficiente=UNDEFINED. Empates são EQUAL. Nenhum indicador.
Timestamp de pivô é abertura da hora, não horário intrahora do extremo.

## Campos e causalidade

`trend_open`, `trend_open_at`, `trend_open_details`; equivalentes `trend_close*`.
O campo `*_at` é o instante do evento, em ISO UTC. Details guarda label, highs/lows
(open_ms, at, price, confirmed_ms), highs_class/lows_class, window_start_ms,
latest_closed_ms, version, source e status. Isso permite grupos por entrada,
saída e transição sem inferir rótulos a partir de trades.

Fonte prospectiva LIVE. Captura na construção da posição e em mark_closed,
usando open_ts e close_ts efetivos, incluindo fills Testnet. A abertura é
imutável; o fechamento é calculado separadamente. Campos seguem os checkpoints,
eventos de trade e ledger. Restauração preserva os campos existentes; posições
antigas não recebem falsa abertura LIVE no restart.

Buffer dedicado SOLUSDT: warm-up público 168 horas e stream kline_1h próprio.
Ele não é registrado em EntryEngine, não altera signals, gates, stops, sizing,
CB, thresholds, YAML econômico nem sequência de decisões dos braços.
Sem rede nem escrita adicional por tick; cálculos em memória na abertura/saída,
cacheados por hora. A persistência usa os caminhos existentes. Falha de telemetria
não pode interromper fechamento: fica UNDEFINED/TELEMETRY_ERROR.

Se faltar qualquer hora da janela exata, retorna UNDEFINED/MISSING_CANDLES:
não estende a janela com horas antigas. A chegada de uma hora fechada invalida
o cache. Se warm-up falhar ou reconnect perder uma hora, a lacuna permanece
explícita até haver 72 horas completas disponíveis ou um novo warm-up.
Somente SOLUSDT está instrumentado pelo buffer deste projeto; outros símbolos
de estudos multi-market não recebem rótulos derivados indevidamente de SOL.

## Backfill realizado localmente

Marco: 08/10/2026 23:52:00 BRT. Inclui trades abertos desde o marco e carry-overs
que fecharam depois dele; a abertura destes últimos também é reconstruída no
timestamp original. Inclui posições abertas presentes nas cópias JSON de estado.
Fonte BACKFILL. Candles históricos públicos, mesma função do runtime.

Antes de persistir o sidecar, a ferramenta verifica hash do cache do estudo e
paridade de TODOS os snapshots completos k=3 existentes em context_hourly.json:
pivôs, preços, confirmações, relações e labels. Divergência aborta o backfill.
Lacuna de candles necessária a qualquer trade também aborta, antes da escrita.

Sidecar local: `data/telemetry/price_structure_backfill.jsonl`, manifesto adjacente.
Nenhum ledger antigo, estado ou DB foi reescrito. Entrada identifica símbolo,
pair_id e timestamp de abertura; fechamento exige também timestamp exato.
Os relatórios não sobrescrevem LIVE com BACKFILL e não fabricam saída para OPEN.
Copiar o sidecar não altera trading nem recuperação. Snapshots remotos de
ledger/estado são não atômicos: o manifesto documenta exatamente as cópias usadas,
não promete abrangência além do instante de cada cópia. Outros instrumentos
multi-market são excluídos, nunca classificados usando candles SOLUSDT.

## Atualização sob controle do usuário

Publicar fontes/testes normalmente. Nenhum restart/deploy/commit/push foi feito
pelo agente. A captura LIVE começa no próximo restart realizado pelo usuário.
Para enxergar histórico reconstruído na VPS, copiar o sidecar e manifesto para
`data/telemetry/`; não substituir ledgers nem checkpoints. Relatório:

```text
python tools/forward_experiment_report.py --experiment ema_macd --list-accepted --since "08/10/2026 23:52:00"
```

Listas ACCEPTED TRADES (todos os experimentos) e auditoria de matriz têm
trend_open/trend_close. OPEN permanece OPEN na saída. Sem novas tabelas econômicas.
Detalhes completos nos campos *_details. Helper `tools.price_structure_report.aggregate`
oferece contagens por entrada, saída e transição. Relatórios históricos congelados
não são reescritos; ferramentas somente agregadas não ganharam tabelas de trades.

Para renovar backfill, trabalhar sobre novas cópias locais dos ledgers/JSONs e
escolher saída inédita (o comando recusa sobrescrever um artefato existente):

```text
python tools/price_structure_backfill.py --ledger-dir CAMINHO_DAS_COPIAS_TRADES --state-dir CAMINHO_DAS_COPIAS_STATE --candles data/analysis/price_structure_72h_20261010/market/SOLUSDT_1h.jsonl --extend-market --output data/analysis/NOVA_AUDITORIA/price_structure_backfill.jsonl
```

Depois de validar, o usuário publica o sidecar desejado no caminho lido pelo
relatório. Não há escrita do backfill em DB, nem reclassificação econômica.

## Testes

Causalidade no limite horário, candle aberto, confirmação k=3, lacuna e chegada
tardia, paridade com função original, três instantes auditados, captura independente
open/close, persistência/restauração, erro de telemetria sem bloquear saída,
paridade econômica ligada/desligada, backfill carry-over, overlay de identidade,
precedência LIVE, posição OPEN e agrupamento. Também regressão dos relatórios,
CB e shadows. No Windows, testes antigos mantêm conexões SQLite abertas ao limpar
TemporaryDirectory: runner local fecha essas conexões somente na limpeza dos
temporários. Essa adaptação não é mudança de runtime/persistência.
