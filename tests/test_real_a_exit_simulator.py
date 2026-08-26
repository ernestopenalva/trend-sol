from datetime import datetime, timedelta, timezone
import unittest

from tools.download_real_a_aggtrades import merge_windows
from tools.real_a_exit_simulator import Seed, Tick, run_simulation


class RealAExitSimulatorTests(unittest.TestCase):
    def test_reuses_engine_for_breakeven_trigger(self) -> None:
        seed = Seed(
            pair_id="pair", symbol="SOLUSDT", opened_at=datetime(2026, 8, 19, tzinfo=timezone.utc),
            entry_price=100.0, entry_atr=1.0, ledger_reason="BREAKEVEN", ledger_trigger_price=100.0,
            ledger_closed_at=datetime(2026, 8, 19, 0, 0, 3, tzinfo=timezone.utc),
            no_progress_enabled=False, no_progress_tolerance_seconds=None, no_progress_tolerance_source=None,
        )
        config = {
            "hard_stop": {"enabled": True, "stop_pct": 1.5},
            "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1},
            "profit_lock": {"mode": "atr", "economic_floor": {"enabled": False}, "steps": []},
            "trailing": {"mode": "atr", "activation_atr": 99, "gap_atr": 5},
            "fees": {"enabled": False}, "ladder": {"be_activation_buffer_atr": 0.5},
        }
        ticks = [
            Tick(datetime(2026, 8, 19, 0, 0, 1, tzinfo=timezone.utc), 103.5),
            Tick(datetime(2026, 8, 19, 0, 0, 2, tzinfo=timezone.utc), 100.0),
        ]

        result = run_simulation([seed], ticks, config)[0]

        self.assertTrue(result["reason_match"])
        self.assertEqual(result["simulated_trigger_price"], 100.0)
        self.assertEqual(result["trigger_at"], ticks[1].timestamp)

    def test_merges_overlapping_download_windows(self) -> None:
        start = datetime(2026, 8, 19, tzinfo=timezone.utc)
        windows = merge_windows([
            (start, start + timedelta(minutes=2)),
            (start + timedelta(minutes=1), start + timedelta(minutes=3)),
        ])
        self.assertEqual(windows, [(start, start + timedelta(minutes=3))])
