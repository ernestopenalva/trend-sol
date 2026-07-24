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

O hard stop percentual e configurado em `risk.hard_stop`. O experimento
`b_atr_v1.3` usa `2%` e mantem o desconto de BNB desativado.

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
  --sizing-rule "HALF/3/-0.3/0.66/0.5"
```

Os campos representam nome, minimo de posicoes abertas, perda percentual para uma
posicao contar como negativa, fracao negativa exigida e fator de tamanho.
