from __future__ import annotations

import argparse
import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tools.profit_lock_study import _events_for_records, _real_sol_records, print_report


class ProfitLockStudyTests(unittest.TestCase):
    def test_report_uses_only_real_sol_and_never_scores_censored_shadow(self) -> None:
        records = [
            _record("observable", "PROFIT_LOCK", -0.10, "CLOSED", False, 100.30),
            _record("censored", "PROFIT_LOCK", -0.12, "ACTIVE", True, None),
            {**_record("phantom", "PROFIT_LOCK", -1.0, "CLOSED", False, 99.0), "phantom": True},
            {**_record("market", "HARD_STOP", -2.2, "CLOSED", False, 99.0), "symbol": "BMTUSDT"},
        ]
        events_list = [
            {
                "pair_id": "observable",
                "event": "PROFIT_LOCK_SHADOW_ATR_1",
                "price": 100.5,
                "pnl_atr": 5.0,
                "entry_atr": 0.1,
                "pl_shadow_raw_stop": 100.15,
                "pl_shadow_net_floor": 100.25,
                "pl_shadow_stop": 100.25,
                "pl_shadow_floor_absorbed": True,
            },
            {
                "pair_id": "observable",
                "event": "PROFIT_LOCK_SHADOW_CLOSE",
                "pl_shadow_status": "CLOSED",
                "pl_shadow_close_price": 100.30,
                "pl_shadow_active_step": "PL1",
            },
        ]
        selected = _real_sol_records(records, None, "opened_at")
        events = _events_for_records(events_list, selected)
        args = argparse.Namespace(since=None, since_field="opened_at", limit=30)

        output = io.StringIO()
        with redirect_stdout(output):
            print_report(selected, events, Path("ledger"), Path("events"), args)

        text = output.getvalue()
        self.assertIn("real trades | 2", text)
        self.assertIn("shadow observable | 1", text)
        self.assertIn("censored by real exit | 1", text)
        self.assertIn("PL1 | activations=1 | floor absorbed raw stop=1", text)
        self.assertIn("negative PROFIT_LOCK avoided | 1/1 observable negatives", text)
        self.assertNotIn("phantom |", text)


def _record(
    pair_id: str,
    reason: str,
    net: float,
    shadow_status: str,
    censored: bool,
    shadow_close: float | None,
) -> dict:
    return {
        "pair_id": pair_id,
        "symbol": "SOLUSDT",
        "position_type": "BOT_EXIT",
        "phantom": False,
        "opened_at": "2026-08-09T12:00:00+00:00",
        "closed_at": "2026-08-09T13:00:00+00:00",
        "entry_price": 100.0,
        "gross_pnl_pct": net + 0.2,
        "net_pnl_pct": net,
        "estimated_fees_pct": 0.2,
        "exit_reason": reason,
        "final_step": "PL1",
        "pl_shadow_enabled": True,
        "pl_shadow_status": shadow_status,
        "pl_shadow_close_price": shadow_close,
        "pl_shadow_active_step": "PL1",
        "pl_shadow_censored_by_real_exit": censored,
    }


if __name__ == "__main__":
    unittest.main()
