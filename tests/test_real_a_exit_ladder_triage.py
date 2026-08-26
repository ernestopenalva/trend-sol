from datetime import datetime, timezone
import unittest

from tools.real_a_exit_ladder_triage import Outcome, _arm_configs, _summary
from tools.real_a_exit_simulator import Seed


class RealAExitLadderTriageTests(unittest.TestCase):
    def test_arm_b_only_changes_be_activation_buffer_and_c_turns_be_off(self) -> None:
        config = {
            "risk": {
                "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1},
                "profit_lock": {"mode": "atr", "steps": []}, "trailing": {"mode": "atr"},
            }, "fees": {}, "ladder": {"be_activation_buffer_atr": 0.5},
        }
        arms = _arm_configs(config)
        self.assertEqual(arms["A"]["ladder"]["be_activation_buffer_atr"], 0.5)
        self.assertEqual(arms["B"]["ladder"]["be_activation_buffer_atr"], 2.9)
        self.assertEqual(arms["C"]["breakeven"]["mode"], "off")

    def test_open_positions_are_excluded_from_pnl_summary(self) -> None:
        seed = Seed("p", "SOLUSDT", datetime(2026, 8, 19, tzinfo=timezone.utc), 100, 1, "BREAKEVEN", 100, datetime(2026, 8, 19, 1, tzinfo=timezone.utc), False, None, None)
        closed = Outcome(seed, "A", "CLOSED", "TRAILING", gross_pct=2, net_pct=1.8, age_seconds=60)
        open_item = Outcome(seed, "A")
        summary = _summary([closed, open_item])
        self.assertEqual(summary["closed"], 1)
        self.assertEqual(summary["open"], 1)
        self.assertEqual(summary["gross"], 2)
        self.assertEqual(summary["net"], 1.8)
