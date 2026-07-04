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

## Logs

- `logs/decisions.jsonl`: avaliacoes dos portoes de entrada.
- `logs/trades.jsonl`: eventos das posicoes A e B.
- `logs/system.log`: eventos operacionais, erros e reconexoes.
- `data/paired_reports.jsonl`: relatorio pareado por par fechado.
