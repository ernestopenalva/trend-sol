from __future__ import annotations

import csv
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tools.real_a_be_pl1_pre_be_audit import AuditCase, _load_cases, _sequence
from tools.real_a_be_pl1_order_study import BeSeed


class PreBeAuditTests(unittest.TestCase):
    def test_selects_only_current_ladder_pl1_more_than_five_seconds_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            details, ledger = root / "details.csv", root / "ledger.jsonl"
            headers = ("pair_id", "strategy_version", "ledger_hard_stop_pct", "outcome", "resolved_brt", "closed_brt")
            rows = (
                ("include", "b_atr_v1.4", "1.50000000", "PL1_FIRST", "01/08/2026 10:00:00 BRT", "01/08/2026 10:00:10 BRT"),
                ("near", "b_atr_v1.4", "1.50000000", "PL1_FIRST", "01/08/2026 10:00:06 BRT", "01/08/2026 10:00:10 BRT"),
                ("old", "b_atr_v1.3", "2.00000000", "PL1_FIRST", "01/08/2026 10:00:00 BRT", "01/08/2026 10:00:10 BRT"),
            )
            with details.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle); writer.writerow(headers); writer.writerows(rows)
            record = {
                "pair_id": "include", "opened_at": "2026-08-01T12:00:00+00:00", "closed_at": "2026-08-01T13:00:10+00:00",
                "entry_price": 100, "entry_atr": 1, "peak_price": 106, "trough_price": 99,
                "hard_stop_price": 98.5, "hard_stop_pct": 1.5, "strategy_version": "b_atr_v1.4", "profile": "intraday",
            }
            ledger.write_text(json.dumps(record) + "\n", encoding="utf-8")
            cases = _load_cases(details, ledger)
            self.assertEqual([case.seed.pair_id for case in cases], ["include"])

    def test_sequence_orders_ticks_and_real_close(self) -> None:
        opened = datetime(2026, 8, 1, tzinfo=timezone.utc)
        seed = BeSeed("pair", opened, opened + timedelta(minutes=5), 100, 1, 106, 99, "b_atr_v1.4", "intraday", 98.5, 1.5, None, None)
        case = AuditCase(seed, {}, 105, 103, 101, opened + timedelta(seconds=2), opened + timedelta(seconds=1), opened + timedelta(seconds=3))
        self.assertEqual(_sequence(case), "BE_ARM -> PL1_TOUCH -> BE_STOP_TOUCH -> CLOSED_BE")
