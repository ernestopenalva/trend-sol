# trend-sol

Bot de trend-following em SOL/USDT para paper trading na Binance Spot Testnet.

## Rodar local

1. Crie um `.env` a partir do `.env.example`.
2. Instale as dependencias em um ambiente virtual.
3. Execute:

```bash
python main.py
```

O monitor usa dados reais da Binance via WebSocket e executa ordens na Spot Testnet.

## Risco e PL shadow

O hard stop percentual e configurado em `risk.hard_stop`; o perfil atual usa
`1,5%` e mantem o desconto de BNB desativado.

`risk.profit_lock.net_floor_shadow` calcula, para todos os degraus PL, um piso que
cobre as duas taxas taker mais a margem liquida configurada. A ativacao tambem exige
o buffer em ATR. Os estados `PENDING`, `ACTIVE` e `CLOSED` sao apenas observacionais:
o shadow nunca altera o stop efetivo, envia ordem, ocupa slot ou reserva quantidade.
Se o trade real fechar primeiro, o ledger marca o contrafactual como censurado.

## Logs

- `logs/decisions.jsonl`: avaliacoes dos portoes de entrada.
- `logs/trades.jsonl`: eventos das posicoes A e B.
- `logs/system.log`: eventos operacionais, erros e reconexoes.
- `data/paired_reports.jsonl`: relatorio pareado por par fechado.

## Estudo offline de stops

Sinais aprovados pelo pipeline e bloqueados por falta de slot podem gerar posicoes
fantasma quando `instrumentation.phantoms.enabled` estiver ativo. Fantasmas usam os
mesmos ticks e a mesma escadinha, mas nao enviam ordens, nao ocupam slots e nao
participam de saldo ou quantidade reservada. O hard stop fica desativado neles para
preservar o contrafactual. Registros fantasma entram no ledger com `phantom=true` e
sao excluidos de todos os agregados normais do relatorio.

```bash
python tools/trades_report.py --phantoms
python tools/stop_study.py
python tools/stop_study.py --detail --episode-gap-hours 6
python tools/stop_study.py --cluster-guards "2/60/60,2/60/120"
```

No formato do cluster guard, os tres numeros representam quantidade de HARD_STOPs,
janela retrospectiva em minutos e pausa em minutos. O estudo informa sua hierarquia
de fidelidade: trajetorias de eventos e snapshots sao preferidas; resumos de trough
sao aproximacoes. O replay historico nao reconstrui os sinais adicionais que uma
mudanca de ocupacao dos slots poderia liberar.

## Estudo offline de pressao da coorte

`cohort_study.py` testa se a quarta e a quinta entradas deveriam ser bloqueadas
quando as posicoes anteriores ja indicam um pullback deteriorado. As regras ficam
em `instrumentation.cohort_guard_study` e sao apenas parametros do replay: o estudo
nao altera o motor, slots, ordens, estado ou saldo.

```bash
python tools/cohort_study.py --ledger data/trades/trades_B.jsonl
python tools/cohort_study.py --ledger data/trades/trades_B.jsonl --detail
python tools/cohort_study.py --ledger archive/trades_B.jsonl \
  --ledger data/trades/trades_B.jsonl --mode both
```

O modo `static` preserva a ocupacao historica. O modo `sequential` remove do
contexto as entradas reais bloqueadas anteriormente, mas nao inventa sinais que
poderiam ter surgido com slots livres. Fantasmas sao sempre excluidos.

O mesmo comando tambem simula sizing degressivo configurado em
`instrumentation.cohort_sizing_study`. Nesse contrafactual, todas as entradas
continuam admitidas e ocupam slots normalmente, mas o notional das entradas
selecionadas e multiplicado por `size_factor`. O resultado e ponderado em USDT e
como percentual de `capital.operational_balance_usdt`; nenhum tamanho real e
alterado.

Regras de sizing podem ser fornecidas sem editar o YAML:

```bash
python tools/cohort_study.py --ledger data/trades/trades_B.jsonl \
  --sizing-rule "HALF_4H/3/-0.3/0.66/0.5/240"
```

Os campos representam nome, minimo de posicoes abertas, perda percentual para uma
posicao contar como negativa, fracao negativa exigida, fator de tamanho e, quando
informada, idade minima da posicao mais antiga em minutos.

## Estudo offline de HARD_STOPs seriais

`serial_stop_study.py` compara duas formas de limitar exposicao correlacionada:
orçamento coletivo de risco ate os HARD_STOPs e limite de entradas abertas dentro
de uma faixa percentual de preco.

```bash
python tools/serial_stop_study.py --ledger data/trades/trades_B.jsonl
python tools/serial_stop_study.py \
  --ledger data/archive/ciclo-anterior/trades/trades_B.jsonl \
  --ledger data/trades/trades_B.jsonl --detail
```

O replay de risco e conservador: uma posicao reserva todo o risco ate seu fechamento
historico, sem liberar orçamento quando BE, PL ou trailing protegem o trade. Entradas
alteradas nao geram sinais historicos novos.

## Estudo offline de regime em 1h

`regime_study.py` etiqueta cada entrada pelo ultimo candle de `1h` que ja estava
fechado naquele instante. A classificacao configurada em
`instrumentation.regime_study` usa preco versus EMA50 e a inclinacao da EMA em tres
candles: `UP` exige preco acima e EMA subindo, `DOWN` exige preco abaixo e EMA
caindo, e os demais casos ficam como `MIXED`.

```bash
python tools/regime_study.py --ledger data/trades/trades_B.jsonl
python tools/regime_study.py \
  --ledger data/archive/ciclo-anterior/trades/trades_B.jsonl \
  --ledger data/trades/trades_B.jsonl --detail
```

Na primeira execucao, os candles publicos reais sao baixados da Binance e gravados
em `data/market/solusdt_1h.jsonl`. Depois, `--offline` impede novos downloads e
exige cobertura completa do cache. O estudo compara o resultado observado por
regime e tres contrafactuais parametrizados: bloquear `DOWN`, operar `DOWN` com
metade do tamanho e exigir `UP`. Nenhum deles altera o bot vivo.

## Estudo offline dos Top Gainers da Binance

`market_selection_study.py` parte dos pares Spot `USDT` que a propria Binance
marca como `TRADING`, aplica volume minimo e reconstroi historicamente quais moedas
estavam positivas em 24h e 7d. A cada quatro horas, compara cestas `TOP_1`,
`TOP_3`, `TOP_5`, todas as positivas, todo o universo liquido e SOL.

```bash
python tools/market_selection_study.py
python tools/market_selection_study.py --offline
```

Na primeira execucao, a ferramenta grava o universo atual e os candles publicos
em `data/market_selection/`. O replay usa somente candles fechados, mas o universo
de simbolos atuais introduz vies de sobrevivencia. O spread mostrado e o bid/ask
real no instante da execucao; o arquivo historico oficial nao contem snapshots do
book. O estudo valida direcao de mercado, nao os gates nem o PnL realizado do bot.

## Replay completo do bot nos mercados selecionados

`market_bot_replay.py` usa as classes reais `EntryEngine` e
`BotFullExitPosition` para executar os quatro gates, spacing, limite de slots,
hard stop, BE, profit locks e trailing sobre candles historicos de `1m` e `15m`.
Ele compara SOL com cinco slots contra Top 5 com uma ou duas posicoes por moeda.

```bash
python tools/market_bot_replay.py
python tools/market_bot_replay.py --offline
```

Como candles de um minuto nao revelam se a maxima veio antes da minima, o
relatorio mostra `LOW_FIRST` e `HIGH_FIRST`. O custo de spread/slippage e uma
hipotese parametrizada em `instrumentation.market_bot_replay`; taxas usam
`fees.*`. O mesmo relatorio agrupa aprovacoes repetidas e simula rearme depois
de 1, 3, 5 ou 15 candles completos sem os quatro gates passarem. Isso permite
distinguir um novo setup da repeticao do anterior. A ferramenta nunca envia
ordens nem altera o estado do bot.

## Shadow prospectivo Top 3 multimercado

`instrumentation.multi_market_shadow` seleciona os tres mercados Spot/USDT
liquidos com melhor variacao positiva em 24h e 7d, respeitando o spread maximo,
e executa neles o pipeline e a saida reais do Trend-Sol apenas em memoria. Cada
mercado tem cinco slots virtuais independentes. Nenhuma ordem, saldo ou slot do
bot vivo e alterado.

A selecao e refeita a cada quatro horas. Um `HARD_STOP` coloca somente aquele
mercado em quarentena ate o proximo candle fechado de uma hora e antecipa uma
nova selecao. Posicoes abertas continuam acompanhadas mesmo quando seu mercado
sai do Top 3; novas entradas deixam de ser admitidas. O limite de cinco entradas
por mercado em cada janela de selecao evita repetir indefinidamente o mesmo
setup.

```bash
python tools/shadow_market_report.py
python tools/shadow_market_report.py --limit 50
```

O estado fica em `data/state/multi_market_shadow.json`, os fechamentos em
`data/trades/trades_shadow_top3.jsonl` e a auditoria completa em
`data/telemetry/market_shadow_events.jsonl`.

## REAL_A, abandono e GCR_SHADOW_B

Nos relatorios novos, `REAL_A` e apenas um alias conceitual para o runtime real
historicamente gravado como Bot `B`; o ledger antigo nao foi renomeado. Novas
entradas reais sao limitadas a uma por candle de 5m. A tolerancia de
`NO_PROGRESS_EXIT` e congelada na entrada: usa 2h enquanto houver menos de quatro
`time_to_BE` validos nos ultimos 20 fechamentos e, depois, mediana vezes 1,25.
Posicoes restauradas sem os novos campos ficam isentas.

`GCR_SHADOW_B` tem estado, slots e ledger sinteticos proprios. Sua unica diferenca
conceitual de `REAL_A` e bloquear uma entrada enquanto sua posicao anterior ainda
nao armou BE. Ele nunca envia ordens nem reserva saldo.

O market context e somente telemetria. Em 5m e 15m registra, sempre com candles
fechados, EMA20/50, slopes percentuais, ADX14, +DI14, -DI14, RSI14 e RVOL contra a
media dos 20 candles fechados anteriores, alem do GE15 estrutural.

```bash
python tools/trades_report.py --since "15/08 21:30" --since-field opened_at --detail
python tools/market_context_report.py --strategy A --since "15/08 21:30" --profile intraday
python tools/market_context_report.py --strategy B --since "15/08 21:30" --profile intraday
python tools/logic_comparison_report.py --since "15/08 21:30" --profile intraday
python tools/logic_comparison_report.py --strategy B --since "15/08 21:30" --profile intraday
python tools/indicator_ranking.py --strategy A
python tools/indicator_ranking.py --strategy B
```

Para auditar apenas a precisao mecanica do GE15 nas entradas reais, incluindo os
dois candles efetivamente usados, reproducao de maxima/minima e frescor do candle
5m em fronteiras de fechamento:

```bash
python tools/ge_entry_audit.py \
  --since "15/08 18:58" \
  --until "16/08 06:30" \
  --profile intraday \
  --detail
```

A ferramenta e estritamente read-only e nao consulta a Binance. Ela separa
inconsistencia aritmetica de uso de candle 5m atrasado por ordem de chegada dos
streams e resume os eventos de sincronizacao do runtime (`GE_CANDLE_FRESH`,
`WAITING`, `READY`, `TIMEOUT`, `EXPIRED_NEXT_1M` e `FUTURE`).

Na fronteira exata de 5 minutos, o runtime espera por ate 15 segundos pelo candle
5m correto. Se ele nao chegar nesse prazo (ou antes do proximo candle de entrada),
o sinal e descartado; o candle estrutural anterior nunca e usado como fallback e
nao ha consulta REST para completar a avaliacao.
