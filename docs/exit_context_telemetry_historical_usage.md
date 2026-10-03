# Exit context: sanidade final antes do deploy

Ledgers históricos anteriores ao deploy desta correção não devem ser usados diretamente para análise de exit EMA/MACD nos braços afetados. Essas análises devem reconstruir causalmente o contexto a partir dos candles.

Braços com o problema de atualização por oportunidade: BE_OFF_CB_SHADOW,
REAL_A_CB_SHADOW, BE030_SHADOW e BE_OFF_SHADOW. O marco é o deploy efetivo
da correção, a ser registrado pelo operador; esta auditoria não realizou deploy.
Não foram reescritos ledgers, reexecutados estudos ou modificados estados.

## REAL_A titular

Não apresenta a mesma ausência de despacho contínuo. `src/app.py` chama
`registry.record_market_context(snapshot)` no startup e em cada evento de
kline 5m fechado, independentemente de haver sinal/admissão. REAL_A não precisa
receber o callback `on_closed_5m` dos shadows: esse método do registry é seu
caminho equivalente. `MarketContextEngine.refresh` usa candles marcados como
fechados e atualiza `market_context.latest`; o registry guarda uma cópia profunda
em `_latest_market_context`.

`PositionRegistry.open_pair` copia o snapshot em `market_context_entry`.
No fechamento REAL_A, `PositionRegistry.on_tick` copia `_latest_market_context`
em `market_context_exit`. `BotFullExitPosition` salva/restaura ambos no estado;
`TradeLedger` grava ambos no ledger REAL_A, historicamente `trades_B.jsonl`.

Campos relevantes dentro de `market_context_entry` e `market_context_exit`:
`captured_at`, `tf_5m`, `tf_15m`, `ge15`, `telemetry_only`. Dentro de `tf_5m`:
`ema_context`, `ema50`, `ema100`, `ema200`, os respectivos campos
`*_previous` e `*_direction`; `macd_context`, `macd_line`,
`macd_line_previous`, `macd_signal`, `macd_signal_previous`, `macd_histogram`,
`macd_histogram_previous`, `macd_direction`, `macd_position`;
`latest_open_at_ms`, `latest_closed_at_ms`, `previous_open_at_ms`,
`previous_closed_at_ms`. Há outros indicadores observacionais no snapshot.

Limite: o registry não valida sozinho a recência ou causalidade do snapshot
ao copiar para a saída. Falha de refresh, interrupção dos candles ou entrega
fora de ordem exige auditoria temporal própria. A atualização contínua não é
uma certificação indiscriminada de todos os registros históricos do REAL_A.
Não se justifica incluí-lo no patch de despacho dos quatro shadows.

## Consumidores históricos

Classificação restrita à dependência de exit context persistido; não certifica
todos os detalhes de simulação de cada ferramenta.

| Ferramenta / caminho | Classificação | Uso e limite |
| --- | --- | --- |
| `tools/forward_experiment_report.py`, `_recorded_exit_context` | Afetado; apresentação agora protegida | Lê o snapshot persistido, não reconstrói. O patch marca STALE/UNAVAILABLE; esses labels não são contextos técnicos. Métricas econômicas não dependem desses labels. |
| `tools/be_off_cb_defensive_review_report.py`, `forward_case` | Afetado | Caso ilustrativo forward lê exit persistido; os artefatos antigos podem reproduzir BUL/BU- stale. Não reutilizar essa versão como verdade técnica. |
| `tools/be_off_cb_defensive_closure.py`, `audit_forward` | Seguro para a versão reconstruída | Mantém versão persistida como evidência separada e reconstrói dos candles; usar `reconstructed_exit`, não `recorded_exit`. |
| `tools/be_off_cb_defensive_review.py` e `tools/be_off_cb_exit_context_study.py` | Seguro | Contextos históricos reconstruídos dos candles, sem tomar exit persistido como verdade. |
| `tools/ema_context_historical_backtest.py` e `tools/ema_stack_block_backtest.py` | Seguro quanto a este bug | Calculam contextos a partir dos candles; não dependem do snapshot de saída do ledger. |
| `tools/export_real_a_context_list.py` | Seguro condicionado à proveniência | Copia labels do relatório de entrada, não reconstrói EMA. O produtor `ema_stack_block_backtest.py` reconstrói dos candles; um arquivo arbitrário/stale não ganha validade ao ser exportado. |
| `tools/market_context_report.py` | Não aplicável aos quatro braços pela seleção atual; leitor direto | CLI atual lê REAL_A e GCR/DMI, não os quatro ledgers afetados. Exibe entry/exit persistidos sem validação de recência; se usado com dados afetados, exige reconstrução. |
| `tools/trades_report.py`, `_market_stack` | Leitor direto; afetado se receber dados afetados | Apresentação normal/pairs usa snapshot persistido, não candles. Não considerar essa apresentação uma reconstrução causal. |
| `tools/exit_context_telemetry_audit.py` | Não aplicável como análise de contexto | Usa timestamps persistidos justamente para detectar stale/ausência; não atribui novo contexto nem corrige ledger. |
| `tools/be_ladder_shadow_report.py`, `tools/context_shadow_forward_report.py` | Não aplicável | Comparações econômicas/pairing não usam exit EMA/MACD persistidos. |
| `tools/real_a_exit_simulator.py`, `tools/real_a_exit_ladder_triage.py`, `tools/real_a_exit_ladder_followup.py` | Não aplicável | Trajetórias/preços/resultados, não exit EMA/MACD persistidos. |
| `tools/be_off_cb_fast_drop_systemic_replay.py` | Seguro quanto a este bug | Contextos de avaliação vêm dos candles do replay, não de exit context histórico persistido. |
| `tools/ema_context_monitor.py` | Não aplicável | Observação dos candles fechados atuais, não análise do exit persistido histórico. |

Os artefatos históricos permanecem intactos. Para qualquer nova análise técnica
de saída dos braços afetados, registrar fonte/hash dos candles, instante exato
do exit e snapshot fechado elegível, mantendo a reconstrução separada do ledger.
