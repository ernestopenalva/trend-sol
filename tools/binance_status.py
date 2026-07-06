from __future__ import annotations

import argparse

from tool_common import bootstrap, print_line


def main() -> None:
    args = _parse_args()
    config, client = bootstrap()
    symbol = str(config["symbol"])
    account = client.account()
    balances = [
        item
        for item in account.get("balances", [])
        if float(item.get("free", 0) or 0) > 0 or float(item.get("locked", 0) or 0) > 0
    ]
    if not args.all_balances:
        wanted_assets = set(args.assets)
        balances = [item for item in balances if item.get("asset") in wanted_assets]

    print_line("Balances:")
    for item in sorted(balances, key=lambda row: str(row.get("asset"))):
        print_line(f"  {item.get('asset')}: free={item.get('free')} locked={item.get('locked')}")

    print_line(f"Open orders {symbol}:")
    open_orders = client.open_orders(symbol)
    if not open_orders:
        print_line("  none")
    for order in open_orders:
        print_line(
            f"  {order.get('orderId')} {order.get('side')} {order.get('type')} "
            f"status={order.get('status')} qty={order.get('origQty')} client={order.get('clientOrderId')}"
        )

    print_line(f"Last orders {symbol}:")
    for order in client.all_orders(symbol, limit=args.limit)[-args.limit:]:
        print_line(
            f"  {order.get('orderId')} {order.get('side')} {order.get('type')} "
            f"status={order.get('status')} executed={order.get('executedQty')} client={order.get('clientOrderId')}"
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mostra status da Binance Testnet para o trend-sol.")
    parser.add_argument("--all-balances", action="store_true", help="Mostra todos os saldos nao zerados.")
    parser.add_argument("--assets", nargs="+", default=["USDT", "SOL"], help="Moedas exibidas por padrao.")
    parser.add_argument("--limit", type=int, default=20, help="Quantidade de ordens recentes.")
    return parser.parse_args()


if __name__ == "__main__":
    main()
