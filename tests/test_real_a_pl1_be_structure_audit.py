from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.real_a_pl1_be_structure_audit import LadderGeometry, Row, _row


class Pl1BeStructureTests(unittest.TestCase):
    def test_classifies_equal_and_above_stops(self) -> None:
        opened = datetime(2026, 8, 1, tzinfo=timezone.utc)
        equal = Row("equal", "A", opened, 100, 1, None, LadderGeometry(100.25, 103, "PL1", 100.25, 103.75))
        above = Row("above", "A", opened, 100, 1, None, LadderGeometry(100.25, 103, "PL1", 101.5, 105))
        self.assertEqual(equal.classification, "PL_STOP_EQUALS_BE")
        self.assertEqual(above.classification, "PL_STOP_ABOVE_BE")

    def test_historical_v14_formula_has_floor_plateau_for_small_atr(self) -> None:
        config = {
            "risk": {
                "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]},
                "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1},
                "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5},
            },
            "fees": {"enabled": True, "taker_fee_pct": 0.10, "use_bnb_discount": False},
            "ladder": {"be_net_margin_pct": 0.05, "be_activation_buffer_atr": 0.5},
        }
        record = {"pair_id": "pair", "opened_at": "2026-08-01T00:00:00+00:00", "entry_price": 100, "entry_atr": 0.1, "gross_pnl_pct": 0.2}
        row = _row(record, config, "test")
        self.assertAlmostEqual(row.geometry.be_stop, 100.25)
        self.assertAlmostEqual(row.geometry.pl_effective_stop, 100.25)
        self.assertEqual(row.classification, "PL_STOP_EQUALS_BE")
