from __future__ import annotations

from tool_common import PROJECT_ROOT, bootstrap, print_line

from src.state_manager import StateManager


def main() -> None:
    config, client = bootstrap()
    symbol = str(config["symbol"])
    state = StateManager(PROJECT_ROOT).load_open_positions()
    open_orders = client.open_orders(symbol)
    all_orders = client.all_orders(symbol, limit=100)
    open_ids = {str(order.get("clientOrderId")) for order in open_orders}
    all_ids = {str(order.get("clientOrderId")) for order in all_orders}

    divergences = []
    if not state:
        stale_orders = sorted(client_id for client_id in open_ids if client_id.startswith("ts-"))
        if stale_orders:
            divergences.append(
                f"Binance tem {len(stale_orders)} ordens do bot, mas nao existe state local."
            )

    for item in state:
        if item.get("label") != "A" or item.get("status") != "OPEN":
            continue
        trailing = item.get("trailing_order") or {}
        client_order_id = str(trailing.get("clientOrderId") or "")
        if client_order_id and client_order_id not in open_ids and client_order_id not in all_ids:
            divergences.append(
                f"{item.get('pair_id')} A trailing order not found on Binance: {client_order_id}"
            )

    print_line(f"Local positions: {len(state)}")
    print_line(f"Binance open orders: {len(open_orders)}")
    if not divergences:
        print_line("Reconcile: OK")
        return
    print_line("Reconcile: DIVERGENCES")
    for item in divergences:
        print_line(f"  - {item}")


if __name__ == "__main__":
    main()
