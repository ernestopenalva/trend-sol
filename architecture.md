# trend-sol - Arquitetura

Pipeline principal:

```text
WebSocket Binance real -> EntryEngine -> PositionRegistry -> Positions A/B -> CycleManager
```

## Entrada

O `EntryEngine` avalia apenas candles 4h fechados. Cada avaliacao passa pelos portoes de tendencia, pullback, exaustao, reversao e compra. Toda decisao e registrada em `logs/decisions.jsonl`.

## Execucao

O `BinanceClient` fala com a Binance Spot Testnet. As chaves ficam em `.env`.

Cada sinal abre duas compras market:

- A: `SERVER_SIMPLE_TRAIL`, com trailing server-side da Binance.
- B: `BOT_FULL_EXIT_ENGINE`, com stop, breakeven escalonado e trailing por ticks.

## Fechamento

As posicoes fecham de forma independente. Quando o par A/B inteiro fecha, o `CycleManager` grava o comparativo em `data/paired_reports.jsonl`.
