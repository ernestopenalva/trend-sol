from __future__ import annotations

import argparse
import unittest
from datetime import datetime, timedelta, timezone

from tools.hard_stop_study import (
    Candle,
    Trade,
    build_trades,
    peak_at_checkpoint,
    select_records,
    survivor_rows,
)


class HardStopStudyTests(unittest.TestCase):
    def test_select_records_excludes_phantoms_shadows_and_other_profiles(self) -> None:
        args = argparse.Namespace(
            since=None,
            until=None,
            since_field="opened_at",
            profile="intraday",
        )
        base = record("real")
        records = [
            base,
            {**record("phantom"), "phantom": True, "position_type": "PHANTOM"},
            {**record("shadow"), "position_type": "MARKET_SHADOW", "shadow_kind": "TOP3"},
            {**record("production"), "profile": "production"},
        ]

        selected, excluded = select_records(records, args, "SOLUSDT")

        self.assertEqual([item["pair_id"] for item in selected], ["real"])
        self.assertEqual(excluded["phantom"], 1)
        self.assertEqual(excluded["not_real_bot_b"], 1)
        self.assertEqual(excluded["profile"], 1)

    def test_build_trades_uses_ledger_age_and_peak(self) -> None:
        trades, invalid = build_trades([record("one")])

        self.assertFalse(invalid)
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].age_seconds, 7200)
        self.assertAlmostEqual(trades[0].peak_pct, 0.20)
        self.assertEqual(trades[0].reason, "HARD_STOP")

    def test_peak_at_checkpoint_uses_only_full_post_entry_candles(self) -> None:
        opened = datetime(2026, 8, 12, 12, 0, 30, tzinfo=timezone.utc)
        trade = trade_object("hs", opened, 2, "HARD_STOP")
        first_full = int(datetime(2026, 8, 12, 12, 1, tzinfo=timezone.utc).timestamp() * 1000)
        candles = {
            first_full - 60_000: Candle(first_full - 60_000, first_full - 1, 110.0),
            **{
                first_full + minute * 60_000: Candle(
                    first_full + minute * 60_000,
                    first_full + (minute + 1) * 60_000 - 1,
                    100.20,
                )
                for minute in range(59)
            },
        }

        peak = peak_at_checkpoint(trade, 1, candles)

        self.assertAlmostEqual(peak or 0, 0.20)

    def test_peak_at_checkpoint_refuses_incomplete_history(self) -> None:
        opened = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        trade = trade_object("hs", opened, 2, "HARD_STOP")

        self.assertIsNone(peak_at_checkpoint(trade, 1, {}))

    def test_survivor_rows_use_peak_at_checkpoint_not_final_peak(self) -> None:
        opened = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)
        recovered = trade_object("winner", opened, 3, "TRAILING", peak_price=101.0)
        stopped = trade_object("loser", opened, 3, "HARD_STOP", peak_price=100.2)
        first = int(opened.timestamp() * 1000)
        candles = []
        for minute in range(180):
            high = 100.20 if minute < 120 else 101.0
            candles.append(Candle(first + minute * 60_000, first + (minute + 1) * 60_000 - 1, high))

        rows = {hours: cohort for hours, cohort, unavailable in survivor_rows(
            [recovered, stopped], candles, 0.25
        ) if unavailable == 0}

        self.assertEqual({item.reason for item in rows[1]}, {"HARD_STOP", "TRAILING"})
        self.assertEqual({item.reason for item in rows[2]}, {"HARD_STOP", "TRAILING"})
        self.assertEqual(rows[3], [])


def record(pair_id: str) -> dict:
    return {
        "pair_id": pair_id,
        "symbol": "SOLUSDT",
        "position_type": "BOT_EXIT",
        "profile": "intraday",
        "opened_at": "2026-08-12T12:00:00+00:00",
        "closed_at": "2026-08-12T14:00:00+00:00",
        "entry_price": 100.0,
        "peak_price": 100.2,
        "age_seconds": 7200,
        "exit_reason": "HARD_STOP",
    }


def trade_object(
    pair_id: str,
    opened: datetime,
    age_hours: int,
    reason: str,
    peak_price: float = 100.2,
) -> Trade:
    closed = opened + timedelta(hours=age_hours)
    return Trade(
        record={"pair_id": pair_id, "exit_reason": reason},
        opened=opened,
        closed=closed,
        entry=100.0,
        peak_price=peak_price,
        age_seconds=age_hours * 3600,
    )


if __name__ == "__main__":
    unittest.main()
