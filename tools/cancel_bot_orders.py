from __future__ import annotations

import argparse

from tool_common import bootstrap, confirm_or_exit, print_line


def main() -> None:
    args = _parse_args()
    config, client = bootstrap()
    symbol = str(config["symbol"])
    orders = [
        order
        for order in client.open_orders(symbol)
        if str(order.get("clientOrderId", "")).startswith(args.prefix)
    ]

    if not orders:
        print_line(f"Nenhuma ordem aberta do bot encontrada em {symbol}.")
        return

    print_line(f"Ordens abertas do bot em {symbol}: {len(orders)}")
    for order in orders:
        print_line(
            f"  {order.get('orderId')} {order.get('side')} {order.get('type')} "
            f"qty={order.get('origQty')} client={order.get('clientOrderId')}"
        )

    if args.dry_run:
        print_line("Dry-run: nenhuma ordem foi cancelada.")
        return

    confirm_or_exit(f"Cancelar {len(orders)} ordens abertas do bot em {symbol}?", args.yes)
    for order in orders:
        result = client.cancel_order(symbol, order_id=str(order.get("orderId")))
        print_line(
            f"Cancelada {result.get('orderId')} {result.get('side')} "
            f"{result.get('type')} client={result.get('clientOrderId')}"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cancela ordens abertas criadas pelo trend-sol.")
    parser.add_argument("--prefix", default="ts-", help="Prefixo dos clientOrderId do bot.")
    parser.add_argument("--dry-run", action="store_true", help="Lista ordens sem cancelar.")
    parser.add_argument("--yes", action="store_true", help="Confirma sem perguntar.")
    return parser.parse_args()


if __name__ == "__main__":
    main()
