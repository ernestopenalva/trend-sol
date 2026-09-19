"""Safely settle explicitly named BOT_EXIT positions stranded in NEEDS_REVIEW.

The tool is intentionally separate from the runtime and never rewrites the
ordinary trade ledger.  A recovery journal records the actual Testnet fill and
the original state.  It is dry-run by default.  In execute mode the journaled
client order id makes an interrupted recovery safe to resume without sending a
second market sell.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.exchange.binance_client import BinanceClientError
from src.state_manager import StateManager
from tools.tool_common import bootstrap, confirm_or_exit, print_line


def main() -> None:
    args = _args()
    root = Path(args.project_root).resolve()
    pair_ids = tuple(args.pair_id)
    if len(pair_ids) != len(set(pair_ids)):
        raise SystemExit("Each --pair-id must be supplied at most once.")
    if not pair_ids:
        raise SystemExit("Supply every stranded position with --pair-id.")
    if args.execute and args.offline:
        raise SystemExit("--execute cannot be combined with --offline.")

    state_manager = StateManager(root)
    state_path = Path(args.state_file).resolve() if args.state_file else state_manager.open_positions_file
    if args.execute and state_path != state_manager.open_positions_file.resolve():
        raise SystemExit("--state-file is permitted only with --offline; execute mode always uses live data/state/open_positions.json.")
    positions = _load_positions(state_path)
    targets = _targets(positions, pair_ids)
    recovery_path = root / "data" / "recovery" / f"{args.recovery_id}.json"
    journal = _load_json(recovery_path)
    plan = _plan(args.recovery_id, targets, journal)
    _print_plan(plan, recovery_path)

    if args.offline:
        return

    config, client = bootstrap()
    symbol = str(config["symbol"])
    _verify_original_close_orders_absent(client.get_order, symbol, targets)
    account = client.account()
    free_sol = _free_balance(account, "SOL")
    if free_sol + 1e-12 < plan["quantity"]:
        raise SystemExit(
            f"Insufficient free SOL for recovery: free={free_sol:.8f}, "
            f"required={plan['quantity']:.8f}."
        )
    print_line(f"Testnet check: free SOL={free_sol:.8f}; recovery quantity={plan['quantity']:.8f}")

    existing = _find_order(client.get_order, symbol, args.recovery_id)
    if existing is not None:
        _require_filled(existing, args.recovery_id)
        print_line(f"Existing recovery order found: {existing.get('orderId')} FILLED.")
        _commit(root, state_manager, positions, targets, plan, recovery_path, existing)
        print_line("Recovery reconciliation committed; no sell was sent in this invocation.")
        return

    if not args.execute:
        print_line("Dry-run complete: no order, journal, archive, ledger, or state was changed.")
        return

    confirm_or_exit(
        f"Enviar uma unica SELL market Testnet de {plan['quantity']:.8f} SOL "
        f"com clientOrderId={args.recovery_id}?",
        args.yes,
    )
    _write_json_atomic(recovery_path, {
        "schema": 1,
        "recovery_id": args.recovery_id,
        "phase": "intent_recorded",
        "created_at": _now(),
        "symbol": symbol,
        "targets": targets,
        "quantity": plan["quantity"],
        "ordinary_ledger_changed": False,
    })
    try:
        order = client.market_sell(symbol, plan["quantity"], args.recovery_id)
    except BinanceClientError as error:
        # The journal preserves the idempotency key.  A later dry-run will
        # query it rather than guessing whether the exchange received the POST.
        _update_journal(recovery_path, phase="sell_response_ambiguous", error=str(error))
        raise SystemExit(
            "SELL returned an exchange error. State was left untouched; rerun "
            "without --execute to query the same recovery clientOrderId."
        ) from error
    _require_filled(order, args.recovery_id)
    _commit(root, state_manager, positions, targets, plan, recovery_path, order)
    print_line(f"Recovery complete: order={order.get('orderId')} executed={order.get('executedQty')}.")


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recover explicitly named BOT_EXIT NEEDS_REVIEW positions.")
    parser.add_argument("--pair-id", action="append", required=True, help="Exact NEEDS_REVIEW pair id; repeat for every target.")
    parser.add_argument("--recovery-id", required=True, help="Unique Testnet clientOrderId for this one recovery incident.")
    parser.add_argument("--project-root", default=str(PROJECT_ROOT))
    parser.add_argument("--state-file", help="Alternate frozen open_positions.json; allowed only with --offline.")
    parser.add_argument("--offline", action="store_true", help="Validate only the local state; make no network call.")
    parser.add_argument("--execute", action="store_true", help="Permit the one Testnet market sell after confirmation.")
    parser.add_argument("--yes", action="store_true", help="Confirm execute mode non-interactively.")
    return parser.parse_args()


def _targets(positions: list[dict[str, Any]], pair_ids: Iterable[str]) -> list[dict[str, Any]]:
    requested = set(pair_ids)
    targets = [item for item in positions if str(item.get("pair_id")) in requested]
    found = {str(item.get("pair_id")) for item in targets}
    missing = sorted(requested - found)
    if missing:
        raise SystemExit("Requested pair ids missing from state: " + ", ".join(missing))
    unexpected = [item for item in targets if item.get("label") != "B" or item.get("status") != "NEEDS_REVIEW" or item.get("exit_order")]
    if unexpected:
        raise SystemExit("Every target must be BOT_EXIT label B, NEEDS_REVIEW, and have no exit_order.")
    quantities = [_number(item.get("reserved_qty"), "reserved_qty") for item in targets]
    if any(value <= 0 for value in quantities):
        raise SystemExit("Every target must have a positive reserved_qty.")
    return sorted(targets, key=lambda item: str(item["pair_id"]))


def _plan(recovery_id: str, targets: list[dict[str, Any]], journal: dict[str, Any] | None) -> dict[str, Any]:
    if journal and journal.get("recovery_id") != recovery_id:
        raise SystemExit("Existing recovery journal has a different recovery id.")
    if journal and journal.get("targets") != targets:
        raise SystemExit("Existing recovery journal does not match the current target-state snapshot.")
    quantity = sum(_number(item.get("reserved_qty"), "reserved_qty") for item in targets)
    return {"recovery_id": recovery_id, "quantity": quantity, "targets": targets, "journal_phase": journal.get("phase") if journal else None}


def _verify_original_close_orders_absent(get_order: Callable[..., dict[str, Any]], symbol: str, targets: list[dict[str, Any]]) -> None:
    present = []
    for target in targets:
        client_id = f"ts-{target['pair_id']}-B-close"
        order = _find_order(get_order, symbol, client_id)
        if order is not None:
            present.append(client_id)
    if present:
        raise SystemExit("Original close order now exists; stop for manual reconciliation: " + ", ".join(present))


def _find_order(get_order: Callable[..., dict[str, Any]], symbol: str, client_order_id: str) -> dict[str, Any] | None:
    try:
        return get_order(symbol, client_order_id=client_order_id)
    except BinanceClientError as error:
        if "-2013" in str(error):
            return None
        raise


def _require_filled(order: dict[str, Any], client_order_id: str) -> None:
    if str(order.get("clientOrderId")) != client_order_id or str(order.get("status")) != "FILLED":
        raise SystemExit(f"Recovery order {client_order_id} is not a confirmed FILLED order.")


def _commit(
    root: Path,
    state_manager: StateManager,
    positions: list[dict[str, Any]],
    targets: list[dict[str, Any]],
    plan: dict[str, Any],
    recovery_path: Path,
    order: dict[str, Any],
) -> None:
    archive = root / "data" / "archive" / "needs_review_recovery" / f"{plan['recovery_id']}_positions.json"
    if not archive.exists():
        _write_json_atomic(archive, {"archived_at": _now(), "targets": targets})
    remaining = [item for item in positions if str(item.get("pair_id")) not in {str(target["pair_id"]) for target in targets}]
    _update_journal(
        recovery_path,
        phase="sell_observed",
        observed_at=_now(),
        recovery_order=order,
        archive=str(archive.relative_to(root)),
        ordinary_ledger_changed=False,
    )
    state_manager.save_open_positions(remaining)
    _update_journal(recovery_path, phase="state_reconciled", reconciled_at=_now(), remaining_positions=len(remaining))


def _free_balance(account: dict[str, Any], asset: str) -> float:
    for item in account.get("balances", []):
        if str(item.get("asset")) == asset:
            return _number(item.get("free"), f"free {asset}")
    return 0.0


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"Recovery journal is not an object: {path}")
    return value


def _load_positions(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SystemExit(f"State is not a position list: {path}")
    return value


def _update_journal(path: Path, **fields: Any) -> None:
    value = _load_json(path) or {}
    value.update(fields)
    _write_json_atomic(path, value)


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def _number(value: Any, field: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise SystemExit(f"Invalid {field}: {value!r}") from error


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _print_plan(plan: dict[str, Any], recovery_path: Path) -> None:
    print_line("NEEDS_REVIEW recovery plan")
    print_line(f"  recovery id: {plan['recovery_id']}")
    print_line(f"  targets: {', '.join(str(item['pair_id']) for item in plan['targets'])}")
    print_line(f"  exact reserved quantity: {plan['quantity']:.8f} SOL")
    print_line(f"  journal: {recovery_path}")
    print_line(f"  existing journal phase: {plan['journal_phase'] or 'none'}")


if __name__ == "__main__":
    main()
