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
