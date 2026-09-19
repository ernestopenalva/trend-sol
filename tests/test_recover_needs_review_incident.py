from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.exchange.binance_client import BinanceClientError
from src.state_manager import StateManager
from tools import recover_needs_review_incident as recovery


def _position(pair_id: str, quantity: float = 0.191) -> dict:
    return {
        "pair_id": pair_id,
        "label": "B",
        "engine": "BOT_FULL_EXIT_ENGINE",
        "status": "NEEDS_REVIEW",
        "reserved_qty": quantity,
        "exit_order": None,
    }


class NeedsReviewRecoveryTests(unittest.TestCase):
    def test_plan_uses_exact_reserved_quantities_and_does_not_mutate_state(self) -> None:
        positions = [_position("first", 0.191), _position("second", 0.192), {"pair_id": "keep", "status": "OPEN"}]
        targets = recovery._targets(positions, ("second", "first"))
        plan = recovery._plan("ts-recovery-test", targets, None)

        self.assertEqual([item["pair_id"] for item in targets], ["first", "second"])
        self.assertAlmostEqual(plan["quantity"], 0.383)
        self.assertEqual(positions[0]["status"], "NEEDS_REVIEW")

    def test_original_close_order_found_blocks_recovery(self) -> None:
        target = _position("first")

        def get_order(_symbol: str, *, client_order_id: str):
            if client_order_id == "ts-first-B-close":
                return {"clientOrderId": client_order_id, "status": "FILLED"}
            raise AssertionError("unexpected id")

        with self.assertRaisesRegex(SystemExit, "Original close order now exists"):
            recovery._verify_original_close_orders_absent(get_order, "SOLUSDT", [target])

    def test_missing_original_close_order_is_allowed(self) -> None:
        def get_order(_symbol: str, *, client_order_id: str):
            raise BinanceClientError('Binance error 400: {"code":-2013,"msg":"Order does not exist."}')

        recovery._verify_original_close_orders_absent(get_order, "SOLUSDT", [_position("first")])

    def test_commit_archives_targets_and_removes_only_them_from_active_state(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = StateManager(root)
            first, second, keep = _position("first"), _position("second"), {"pair_id": "keep", "label": "B", "status": "OPEN", "reserved_qty": 0.2}
            positions = [first, second, keep]
            state.save_open_positions(positions)
            targets = recovery._targets(positions, ("first", "second"))
            plan = recovery._plan("ts-recovery-test", targets, None)
            journal = root / "data" / "recovery" / "ts-recovery-test.json"
            order = {"clientOrderId": "ts-recovery-test", "status": "FILLED", "orderId": 123, "executedQty": "0.383"}

            recovery._commit(root, state, positions, targets, plan, journal, order)

            self.assertEqual(state.load_open_positions(), [keep])
            saved = json.loads(journal.read_text(encoding="utf-8"))
            self.assertEqual(saved["phase"], "state_reconciled")
            self.assertEqual(saved["recovery_order"]["orderId"], 123)
            archive = root / saved["archive"]
            archived = json.loads(archive.read_text(encoding="utf-8"))
            self.assertEqual([item["pair_id"] for item in archived["targets"]], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
