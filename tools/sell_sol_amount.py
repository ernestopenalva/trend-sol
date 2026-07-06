from __future__ import annotations

import argparse

from tool_common import bootstrap, confirm_or_exit, print_line


def main() -> None:
    args = _parse_args()
    config, client = bootstrap()
    symbol = str(config["symbol"])
    quantity = float(args.quantity)
    if quantity <= 0:
        raise SystemExit("Quantidade deve ser positiva.")

    print_line(f"Preparando SELL market em {symbol}: quantity={quantity}")
    if args.dry_run:
        print_line("Dry-run: nenhuma venda foi enviada.")
        return

    confirm_or_exit(f"Enviar SELL market de {quantity} SOL em {symbol}?", args.yes)
    result = client.market_sell(symbol, quantity, client_order_id=args.client_order_id)
    print_line(
        f"Venda enviada order_id={result.get('orderId')} status={result.get('status')} "
        f"executed={result.get('executedQty')} quote={result.get('cummulativeQuoteQty')}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Vende uma quantidade explicita de SOL via market sell.")
    parser.add_argument("quantity", help="Quantidade de SOL a vender.")
    parser.add_argument("--client-order-id", default="ts-manual-sell", help="Client order id da venda manual.")
    parser.add_argument("--dry-run", action="store_true", help="Mostra a acao sem vender.")
    parser.add_argument("--yes", action="store_true", help="Confirma sem perguntar.")
    return parser.parse_args()


if __name__ == "__main__":
    main()
