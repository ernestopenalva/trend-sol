# ACT20_GAP5 / ACT10_GAP13 — patch e handoff

## Escopo

Novos braços independentes:

- `BE_OFF_CB_ACT20_GAP5_SHADOW`: activation20 / gap5.
- `BE_OFF_CB_ACT10_GAP13_SHADOW`: activation10 / gap13.

São clones order-free do BE_OFF_CB, executados por `PhantomExecutionClient`. Só os dois parâmetros de trailing diferem. ATR14 1m de entrada permanece congelado. Sinais compartilhados antes da admissão REAL_A; posições/slots/spacing/CB/equity/admissões/exits/persistência e ledgers próprios. REAL_A, BE_OFF_CB, PL, HS, CB, fees/spread/sizing e demais braços não foram substituídos ou recalibrados.

Seleção histórica: **01/06/2026 → 02/10/2026 22:28 BRT**. Replay é geração/seleção da hipótese; a nova coorte forward será a primeira validação OOS. Não foi criado limiar automático de aprovação por N. Medir ativações, dominância efetiva, continuações e censura antes de interpretar net.

## Pré-checagem STALE — fontes originais da VPS

Consulta somente leitura, VPS revisão `81b7284`, observação UTC 05/10/2026 05:24:23. No ledger BE_OFF_CB, depois de **02/10/2026 23:43 BRT**: 26 fechamentos, 8 STALE. Para todos esses 8, o intervalo entre snapshot persistido e exit contém `websocket_error`/`websocket_closed`/`websocket_connected` em `logs/system.log`.

| Exit UTC | Exit BRT | Fechamentos 5m ausentes na leitura |
|---|---|---:|
| 04/10 03:44:58.082 | 04/10 00:44:58.082 | 2 |
| 04/10 09:32:18.200 | 04/10 06:32:18.200 | 2 |
| 04/10 12:14:00.246 | 04/10 09:14:00.246 | 1 |
| 04/10 12:14:51.547 | 04/10 09:14:51.547 | 1 |
| 04/10 15:07:08.819 | 04/10 12:07:08.819 | 2 |
| 04/10 15:08:48.584 | 04/10 12:08:48.584 | 2 |
| 04/10 22:08:25.921 | 04/10 19:08:25.921 | 2 |
| 05/10 04:22:18.331 | 05/10 01:22:18.331 | 1 |

Exemplo: snapshot fechado UTC 04/10 03:29:59.999, exit 03:44:58.082; desconexões 03:35:29, 03:39:34 e 03:43:57, reconexão 03:44:57. Os fechamentos 03:34:59.999 e 03:39:59.999 não chegaram antes do exit. Não há snapshot elegível mais recente recebido que possa ser legitimamente usado pelo relatório.

Os STALEs compartilhados nesse episódio são **gaps reais de transporte**, não simples troca de EMA/MACD nem erro de fórmula do report. Os streams efetivos incluem SOL 1m/5m/15m. Não se elimina esse STALE falsificando contexto, usando candle aberto ou reescrevendo o ledger. Recuperação de candles ausentes/reconexão não foi adicionada: alteraria buffers e potencialmente sinais dos braços existentes, além de estar fora das duas variantes aprovadas.

Foi encontrado também um defeito arquitetural separado: `ExperimentalRiskShadow.on_closed_5m` sobrescrevia o callback e não alimentava `_exit_context_history`. Foram acrescentadas somente as chamadas de retenção do contexto anterior e do novo snapshot. A atualização/decisão econômica existente desses risk-shadows permanece igual; agora um tick anterior a um callback mais novo pode usar seu snapshot causal anterior.

Os novos braços herdam `ExitContextTelemetry.on_closed_5m`, incluindo validação de candle fechado, retenção de histórico e persistência. Recebem o snapshot aquecido no startup e todos os callbacks 5m do caminho compartilhado, sem depender de oportunidade. O exit seleciona o último snapshot **recebido e causalmente elegível**. Durante gap real sem o fechamento necessário, o report continua marcando STALE honestamente. **Não é garantia de transporte sem perdas.** Não há STALE inexplicado nesse caminho auditado; a saúde do transporte deve ser verificada antes/depois do deploy.

Ledgers históricos anteriores ao deploy desta correção não devem ser usados diretamente para análise de exit EMA/MACD nos braços afetados. Essas análises devem reconstruir causalmente o contexto a partir dos candles. Mesmo depois do deploy, saídas explicitamente STALE por gap devem ser reconstruídas para estudos de contexto; não foram reescritas nesta tarefa.

## Telemetria e recuperação

`TrailAuditPosition` chama o engine original; observa ativação e owner real (`PL1/2/3`, `TRAIL`, `HS`, outros), respeitando ratchet/empates. Não substitui o cálculo do stop. Persiste `trail_audit`: ativação, primeira dominância, peak preço/ATR e episódios de owner. Mudanças geram eventos próprios `TRAIL_ACTIVATED`, `TRAIL_FIRST_DOMINANCE`, `TRAIL_OWNER_CHANGED`; entrada gera `TRAIL_POSITION_OPENED`.

O ledger próprio recebe parâmetros, candidate, effective stop/owner, horários de ativação/dominância, episódios e segundos por owner, contexto de entrada/saída com timestamp, source/signal/arm, ATR/peak, idade, gross/fees/net e simultaneidade na entrada. `simultaneous_positions` significa simultaneidade **na admissão**, não máximo histórico; máximo da janela continua no resumo. State `latest_market_context` e histórico seguem sendo atualizados sem oportunidade. Para OPEN, o episódio corrente ainda não tem duração final: não fabricar resolução.

Restart restaura ladder/peak/ATR/stop/CB e audit do próprio state. Uma posição antiga sem audit no path de um novo braço é rejeitada, não herdada silenciosamente. Não foram alterados states reais, arquivos `.pending`, watermarks ou os protocolos de reconciliação.

Paths novos:

- `data/state/be_off_cb_act20_gap5_shadow.json`
- `data/state/be_off_cb_act10_gap13_shadow.json`
- `data/trades/trades_be_off_cb_act20_gap5_shadow.jsonl`
- `data/trades/trades_be_off_cb_act10_gap13_shadow.jsonl`
- `data/telemetry/be_off_cb_act20_gap5_shadow_events.jsonl`
- `data/telemetry/be_off_cb_act10_gap13_shadow_events.jsonl`

YAML: somente duas novas seções instrumentation, enabled/accept_new_entries true, paths próprios e pares 20/5 e 10/13. Capital/capacidade e demais settings de admissão são herdados do controle. Validação rejeita pares não aprovados ou colisão de paths declarados.

## Report

Resumo geral mantém o cabeçalho existente e inclui os dois novos braços após BE_OFF_CB, sem colunas especiais.

```text
python tools/forward_experiment_report.py --since "<MARCO BRT DD/MM/YYYY HH:MM:SS>"
python tools/forward_experiment_report.py --experiment trail_activation_gap --since "<MARCO BRT DD/MM/YYYY HH:MM:SS>"
```

O específico mostra os três braços, métricas de ativação/dominância dos candidatos, overlap exato por source, listas de trades dos dois candidatos com quatro colunas extras e lista do controle no padrão usual. Telemetria TRAIL histórica do controle não é fabricada. OPENs são censurados; medianas de durações usam milestones resolvidos. PL dominante “so far” inclui ativações abertas ainda sob PL; “never dominated” resolvido conta somente trades ativados e fechados.

O modo específico rejeita `--since` anterior ao `cohort_started_at` de qualquer candidato existente, evitando comparar com controle de um período em que os candidatos ainda não existiam. Usar o marco efetivamente persistido no startup. `--since` não reseta CB/slots do controle; posições/cooldown anteriores podem afetar admissões do controle, mesmo que seus trades sejam excluídos da janela. Essa diferença deve ser auditada antes de concluir comparabilidade econômica; não foi apagado estado do controle para artificialmente equalizar trajetórias.

Smoke local com corte 05/10/2026 00:00 BRT, **sem representar coorte iniciada**: três braços aparecem com closed/open=0, economia N/A, eventos=0 e listas vazias. Não há dados locais dos novos braços porque não houve deploy/restart.

## Testes e não regressão

9 testes novos cobrem thresholds/fórmulas, PL antes da ativação, dominância/ratchet, história causal sem oportunidades, restart, ledger, independência de capacidade e CB, mesmo source, filtro since/report geral/específico, recusa de mistura de coortes, gaps honestamente STALE e config.

Com os módulos `test_exit_context_telemetry`, `test_forward_experiment_shadows`, `test_circuit_breaker_shadow`, `test_forward_experiment_report`: **58 testes PASS**.

Suíte completa: **434 testes, 433 PASS, 1 erro preexistente**: `test_cb_hypothesis_audit.DetectorAuditTests.test_replay_sources_match_frozen_revision`. `frozen_config()` exige igualdade das dependências com `0d3f9dc` e aborta por `src/position/bot_full_engine.py`. O engine atual já tem proteções de EXIT_PENDING/reconciliação e telemetria posteriores à revisão congelada. O arquivo não foi alterado por este patch; não remover as proteções para fazer esse teste passar. Nenhum snapshot congelado foi atualizado, nenhum replay histórico foi executado nesta tarefa.

Os testes usam diretórios temporários próprios dentro do workspace, pois o sandbox Windows não permite gravações no TEMP externo. Falhas iniciais de permissão de TEMP foram resolvidas ajustando somente o local temporário do runner, não a aplicação.

## Deploy — sob controle do usuário

**Não houve git/commit/push, deploy ou restart. Nova coorte ainda não iniciada; timestamp oficial pendente.** Os braços serão instanciados enabled no próximo startup com esse YAML.

Consulta somente leitura encontrou o processo existente `python main.py`, PID `1378224`, em `/root/trend-sol`. Não foi encontrada unidade systemd running com nome contendo trend. Presença do processo não garante transporte saudável: os gaps/reconexões acima continuam sendo uma limitação operacional. Não foi executada ação sobre o processo.

Antes do seu restart único:

1. Publicar os arquivos abaixo; verificar config efetiva e ausência dos dois paths novos com posições/coorte anterior. Se existirem, não apagar/reusar cegamente.
2. Conferir saúde WebSocket; STALEs causados por gaps de transporte não foram resolvidos por este patch.
3. Preservar states/ledgers do controle e demais braços; não restaurar baseline nem limpar `.pending`.
4. Após o restart, verificar `COHORT_STARTED`/`cohort_started_at` idêntico dos novos braços, converter UTC→BRT e registrar esse instante como marco do experimento. Informar também o horário exato do restart para auditoria.
5. Confirmar os novos braços habilitados/sem posição indevidamente herdada; após oportunidade, OPEN com source comum e ledgers/eventos próprios. Sem oportunidade, ausência de OPEN/ledger de trades é normal.
6. Rodar os dois comandos com esse marco, verificar exit-context e condições herdadas do controle antes de interpretar net.

Arquivos deste patch: `config/config.yaml`, `src/app.py`, `src/config_profiles.py`, `src/monitor/trail_activation_gap_shadow.py` (novo), `src/monitor/forward_experiment_shadows.py` (retenção de histórico), `src/trade_ledger.py` (hook só para novos braços), `tools/forward_experiment_report.py`, `tests/test_trail_activation_gap_shadow.py` (novo), este documento. Arquivos de estudos anteriores preservados.
