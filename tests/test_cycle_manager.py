from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.monitor.cycle_manager import CycleManager
from src.state_manager import StateManager


class FakePosition:
    def __init__(self, label: str) -> None:
        self.pair_id = "pair-1"
        self.label = label
        self.entry_price = 100.0
        self.exit_price = 101.0
        self.exit_reason = "TEST_EXIT"
        self.open_ts = "2026-07-05T00:00:00+00:00"
        self.close_ts = "2026-07-05T00:01:00+00:00"

    def pnl_pct(self, price: float) -> float:
        return (price - self.entry_price) / self.entry_price * 100


class CycleManagerTests(unittest.TestCase):
    def test_single_cycle_completes_after_one_closed_pair(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {
                "run_control": {"mode": "single_cycle"},
                "capital": {"operational_balance_usdt": 1000, "trade_size_pct": 5},
                "cycle": {"pairs_per_cycle": 1, "trades_per_cycle": 1, "prolabore_pct": 5, "stats_min_pairs": 50},
                "logging": {"console": False, "system_log": "logs/system.log"},
                "console": {"mode": "human"},
            }
            manager = CycleManager(root, config, JsonlLogger(root, config), StateManager(root))

            manager.on_pair_closed([FakePosition("A"), FakePosition("B")])

            self.assertTrue(manager.single_cycle_complete)
            self.assertEqual(manager.completed_cycles, 1)
            self.assertEqual(manager.closed_pairs_in_current_cycle, 0)


if __name__ == "__main__":
    unittest.main()
