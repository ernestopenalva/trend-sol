from __future__ import annotations

import argparse
import unittest

from tools.ge_entry_audit import (
    audit_entries,
    classify_freshness,
    expected_latest_5m_close,
    select_real_trades,
    select_sync_events,
)


class GeEntryAuditTests(unittest.TestCase):
    def test_expected_latest_candle_handles_exact_five_minute_boundary(self) -> None:
        # Source 1m candle 18:59 closes exactly at 19:00; the 18:55 5m candle is expected.
        source_open = 18 * 3_600_000 + 59 * 60_000
        self.assertEqual(expected_latest_5m_close(source_open), 19 * 3_600_000 - 1)
        # Source 1m candle 19:00 closes at 19:01; latest complete 5m is still 18:55.
        source_open = 19 * 3_600_000
        self.assertEqual(expected_latest_5m_close(source_open), 19 * 3_600_000 - 1)

    def test_freshness_distinguishes_stale_and_future(self) -> None:
        expected = 1_799_999
        self.assertEqual(classify_freshness(expected, expected, 300_000), ("FRESH", 0))
        self.assertEqual(classify_freshness(expected, expected - 300_000, 300_000), ("STALE_1x5m", 1))
        self.assertEqual(classify_freshness(expected, expected + 300_000, 300_000)[0], "FUTURE_CANDLE")

    def test_audit_recomputes_ge_and_matches_same_evaluation_chain(self) -> None:
        source_open = 19 * 3_600_000
        latest_close = 19 * 3_600_000 - 1
        opened = "2026-08-15T22:00:03+00:00"
        records = [
            {
                "pair_id": "p1",
                "position_type": "BOT_EXIT",
                "phantom": False,
                "profile": "intraday",
                "opened_at": opened,
                "source_candle_open_time": source_open,
                "market_context_entry": {
                    "tf_5m": {"latest_closed_at_ms": latest_close},
                    "ge15": {"status": "PASS", "latest_closed_at_ms": latest_close},
                },
            }
        ]
        decisions = [
            {
                "ts": "2026-08-15T22:00:00+00:00",
                "gate": 1,
                "reason": "ge_structure",
                "passed": True,
                "candle_interval": "5m",
                "lookback_candles": 3,
                "high_now": 101,
                "high_lookback": 100,
                "low_now": 99,
                "low_lookback": 98,
            },
            {"ts": "2026-08-15T22:00:00+00:00", "gate": 2, "reason": "pullback", "passed": True},
            {"ts": "2026-08-15T22:00:00+00:00", "gate": 5, "reason": "buy_signal", "passed": True},
        ]
        audit = audit_entries(records, decisions)[0]
        self.assertTrue(audit["decision_matched"])
        self.assertEqual(audit["arithmetic"], "CONSISTENT")
        self.assertEqual(audit["freshness"], "FRESH")
        self.assertTrue(audit["recomputed_passed"])

    def test_real_trade_selection_excludes_phantoms_and_profile_mismatch(self) -> None:
        args = argparse.Namespace(
            since=None,
            until=None,
            since_field="opened_at",
            profile="intraday",
        )
        records = [
            {"position_type": "BOT_EXIT", "phantom": False, "profile": "intraday", "opened_at": "2026-08-15T00:00:00+00:00"},
            {"position_type": "BOT_EXIT", "phantom": True, "profile": "intraday", "opened_at": "2026-08-15T00:00:00+00:00"},
            {"position_type": "BOT_EXIT", "phantom": False, "profile": "production", "opened_at": "2026-08-15T00:00:00+00:00"},
        ]
        self.assertEqual(len(select_real_trades(records, args)), 1)

    def test_sync_event_selection_uses_report_time_window(self) -> None:
        args = argparse.Namespace(
            since="15/08 18:58",
            until="15/08 19:10",
            since_field="opened_at",
            profile="intraday",
        )
        decisions = [
            {"ts": "2026-08-15T21:57:59+00:00", "event": "GE_CANDLE_FRESH"},
            {"ts": "2026-08-15T22:00:00+00:00", "event": "GE_CANDLE_WAITING"},
            {"ts": "2026-08-15T22:00:03+00:00", "event": "GE_CANDLE_READY"},
            {"ts": "2026-08-15T22:00:03+00:00", "reason": "ge_structure"},
        ]
        selected = select_sync_events(decisions, args)
        self.assertEqual([item["event"] for item in selected], ["GE_CANDLE_WAITING", "GE_CANDLE_READY"])


if __name__ == "__main__":
    unittest.main()
