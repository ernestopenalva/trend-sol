# Observabilidade operacional do feed e inteligências HS

Não muda thresholds, regras, prioridades de saída, one-shot, CB financeiro,
admissões, sizing, YAML ou estado persistido. Somente diagnóstico e robustez
de conexão. Reconectar mais cedo pode mudar a disponibilidade de inputs, mas
as decisões econômicas para os mesmos inputs continuam iguais.

## Feed

`WSManager.feed_health` fornece `last_exchange_ts`, `last_receive_at` e
`current_lag_ms`; nenhuma estratégia consulta esses campos.

`receive_at` é o instante de entrada no callback `on_message`, antes de parsing
e processamento. NÃO é timestamp de chegada ao kernel/socket. Para aggTrade,
exchange_ts usa T (como o bot); para kline usa E (emissão), não o fechamento
futuro k.T. Lag negativo é mantido para revelar possível skew de relógio.
Ack/inputs sem timestamp contam em missing_exchange_ts; não inventar exchange_ts.

O relógio UTC mede receive/exchange; monotônico/perf_counter mede duração do
callback e desconexão. Callback_ms inclui parsing, bookkeeping e on_event;
exclui escrita dos eventos anômalos ao final. Exceções do callback continuam
propagando exatamente como antes, com duração/erro contabilizados.

Uma thread diagnóstica emite `websocket_input_metrics` em logs/system.log a
cada10s, inclusive quando nenhum input chega ou um callback está bloqueado.
Quantis exatos p50/p90/p99 por janela, em ms: lag_ms e callback_ms; inputs,
callbacks_completed, erros/missing e saúde por stream. Input e conclusão podem
pertencer a janelas diferentes quando o callback cruza o corte. Nenhum write
por input normal, nenhuma fila financeira nova. Amostras são apenas em memória,
descartadas após o resumo; ordenação/IO do resumo ficam fora do callback.

Eventos individuais novos: websocket_input_lag somente se lag>5000ms;
websocket_error (flag ping_pong_timeout), websocket_closed, websocket_connected,
websocket_reconnect_first_input e agendamento de reconexão. Eventos operacionais
anteriores são preservados. Desconexão/reconexão incluem lag anterior, datas em
ms, duração monotônica; lag depois é NULL até o primeiro input válido, quando
websocket_reconnect_first_input registra o valor real. Não usar o último lag
antigo como se fosse medição da nova conexão.

telemetry_write_count/ms/errors quantifica tentativas e tempo gasto pelo writer
destes eventos no período anterior; a escrita do resumo entra na próxima janela.
JsonlLogger pode absorver erro de IO internamente: write_errors conta exceções
que chegam a este observador, NÃO garante sucesso de gravação de cada tentativa.

Backoff1/2/4/8/10s em falhas consecutivas sem input válido; um input de mercado
com timestamp reinicia a sequência. Handshake sozinho não reinicia a sequência.
Watchdog de conexão encerrada não atua sobre conexões posteriores. Ping interval
e timeout existentes NÃO foram mudados.

Lag ao entrar + duração do callback distinguem preço já antigo à entrada de
tempo gasto neste consumidor. Ainda não separam buffering TCP/kernel de atraso
da rede/exchange: não há captura de pacotes. Não usar saúde para trading aqui.

## Inteligências

Eventos `HS_INTELLIGENCE_EVALUATION` uniformes com arm/intelligence/pair/source,
market_ts, logged_at UTC em ms, conditions, values, decision/reasons e freshness.
Destino padrão: data/telemetry/hs_intelligence_events.jsonl, via TelemetryWriter
assíncrono existente. Se desabilitado/fila cheia, fallback ao decisions.jsonl.
Nenhum evento novo entra no journal/checkpoint financeiro do CB. Erro do
observador não desativa braço nem altera flags financeiras.

Deduplicação somente em memória, até4096 identidades, por inteligência/pair e
decisão/motivo/corte relevante. Reinício pode repetir diagnóstico, nunca evento
financeiro. Sem write por tick acima das zonas relevantes, nem write redundante
por repetição da mesma reprovação. Contadores internos _hs_observation_emitted,
_hs_observation_errors e _hs_observation_ms medem emissões/erro/custo.

- FAST_DROP: reference missing uma vez por referência1m requerida, retry_allowed
  true e one_shot_consumed false; primeira avaliação válida registra speed/EMA,
  prioridade do stop normal e one_shot_consumed true, inclusive reprovação por
  múltiplos motivos. No stop posterior, NOT_REEVALUATED explicita one-shot já
  consumido. Fórmula, thresholds e ordem originais permanecem. Contexto stale
  continua sendo usado quando a lógica anterior o usaria; agora isso é visível,
  NÃO existe novo gate de freshness.
- HS_BULL_ELASTIC: avaliação ao alcançar HS, início ou EMA_NOT_LON; cada snapshot
  fechado durante elasticidade registra HOLD, TRIGGER/contexto perdido ou rearmar
  HS; recuperação de entry/economic BE e dados ausentes são eventos de estado.
- HS_BEAR_CLUSTER: avalia somente após HS real do próprio braço; registra SHO
  ou reprovação, vizinhos negativos com PnL e quantidade de posições restantes.
- CB_EXIT_ALL: observa os minutos reais do detector quando há closes/eventos ou
  mudança de predicados, não cada tick/minuto sem mudança. Registra DD, PnL4h,
  closes4h e cooldown. Na ativação registra vítimas/PNL e origem tick/signal;
  sem posições abertas fica explícito. Condições observadas não alimentam CB.

Freshness: FRESH=latest_closed_at_ms exatamente igual ao último5m que deveria
estar fechado; STALE=mais antigo; UNAVAILABLE=dado/as-of ausente; NOT_CAUSAL=
mais recente que o corte. No evento on_closed_5m, as-of é close_ms+1 (primeiro
instante em que o candle terminou); market_ts conserva o close_ms da reavaliação
original. Não muda o timestamp econômico nem seleção usada pelo shadow.

TRIGGER é decisão/intenção; sucesso financeiro é confirmado separadamente no
ledger/EXPERIMENTAL_CLOSE. Não confundir tentativa com fill/fechamento confirmado.
Retenção histórica anterior a este patch continua limitada; novos logs não
reconstroem inputs antigos que nunca foram gravados.

## Validação local

62 testes pertinentes passaram (14 novos), cobrindo feed, flags/retry/freshness,
elasticidade/cluster/CB, restart, equivalência replay-forward, ladder/REAL_A e
wiring. Comparação sintética HEAD/current de estados financeiros passou em
FAST_TRIGGER, FAST_BLOCKED, HS_BULL_ELASTIC, HS_BEAR_CLUSTER_EXIT e CB_EXIT_ALL
(com crise e liquidação reais na fixture). Journal/audit financeiro permaneceu
igual, não só net final.

Suíte completa:508 testes;507 passaram. Único erro conhecido:
test_cb_hypothesis_audit.DetectorAuditTests.test_replay_sources_match_frozen_revision,
guard de replay contra0d3f9dc: src/position/bot_full_engine.py já difere daquela
revisão; esse arquivo NÃO foi alterado neste patch. Guard não foi enfraquecido.

Benchmark: tools/benchmark_feed_hs_observability.py; resultado em
data/analysis/feed_hs_observability_20261008/benchmark.json. Mediana de5 rodadas,
100000 inputs normais:0 writes; ~17µs incremental/input neste ambiente local.
Resumo com1000 amostras ~732bytes e ~3ms com IO local;6 resumos/minuto (~4,4KB/min,
dependendo dos streams). Quantis de100000 amostras ~18,5ms fora do callback.
Não é benchmark da VPS nem garantia de latência operacional após deploy.

Não foi feito deploy, restart, commit ou push. Fontes de estudos anteriores e
arquivos não relacionados foram preservados.
