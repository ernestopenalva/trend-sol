from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tools.forward_experiment_report import (
    calculate_max_simultaneous,
    event_counts,
    filter_since,
    first_comparable_instant,
    operational_warmup_counts,
    pair_by_source,
    parse_time,
)


COHORT = datetime(2026, 9, 27, 23, 7, 29, tzinfo=timezone.utc)


class ForwardExperimentReportTests(unittest.TestCase):
    def test_default_brt_cohort_timestamp_is_exact(self) -> None:
        self.assertEqual(parse_time("27/09/2026 20:07:29"), COHORT)

    def test_filter_since_rejects_prior_cohort_and_missing_timestamps(self) -> None:
        rows = [
            {"pair_id": "old", "opened_at": "2026-09-27T23:07:28+00:00"},
            {"pair_id": "edge", "opened_at": "2026-09-27T23:07:29+00:00"},
            {"pair_id": "new", "opened_at": "2026-09-27T23:08:00+00:00"},
            {"pair_id": "missing"},
        ]
        self.assertEqual([row["pair_id"] for row in filter_since(rows, COHORT, "opened_at")], ["edge", "new"])

    def test_pairing_uses_only_exact_source_candle(self) -> None:
        control = [
            {"pair_id": "c1", "source_candle_open_time": 100},
            {"pair_id": "c2", "source_candle_open_time": 200},
        ]
        experiment = [
            {"pair_id": "e1", "source_candle_open_time": 100},
            {"pair_id": "e3", "source_candle_open_time": 300},
        ]
        pairs = pair_by_source(control, experiment)
        self.assertEqual([(left["pair_id"], right["pair_id"]) for left, right in pairs], [("e1", "c1")])

    def test_event_reader_counts_only_new_hypothesis_events_inside_cohort(self) -> None:
        events = [
            {"ts": "2026-09-27T23:07:28+00:00", "event": "HS_ELASTIC_STARTED"},
            {"ts": "2026-09-27T23:07:29+00:00", "event": "HS_ELASTIC_STARTED"},
            {"ts": "2026-09-27T23:10:00+00:00", "event": "HS_ELASTIC_ENDED_RECOVERED"},
            {"ts": "2026-09-27T23:11:00+00:00", "event": "HS_BEAR_CLUSTER_TRIGGERED"},
            {"event": "CB_EXIT_ALL_TRIGGERED"},
        ]
        self.assertEqual(
            event_counts(events, COHORT),
            {
                "HS_ELASTIC_STARTED": 1,
                "HS_ELASTIC_ENDED_RECOVERED": 1,
                "HS_BEAR_CLUSTER_TRIGGERED": 1,
            },
        )

    def test_comparable_instant_waits_for_overlapping_inherited_positions_and_cooldown(self) -> None:
        floor = datetime(2026, 9, 28, 4, 47, tzinfo=timezone.utc)
        positions = [
            (floor-timedelta(hours=1), floor+timedelta(minutes=5), "arm-a:p1"),
            (floor+timedelta(minutes=2), floor+timedelta(minutes=9), "arm-b:p2"),
        ]
        cooldowns = [(floor-timedelta(minutes=10), floor+timedelta(minutes=7), "control:CB")]
        instant, reason = first_comparable_instant(
            positions, cooldowns, floor, floor+timedelta(minutes=20)
        )
        self.assertEqual(instant, floor+timedelta(minutes=9))
        self.assertIsNone(reason)

    def test_comparable_instant_does_not_invent_close_for_open_carryover(self) -> None:
        floor = datetime(2026, 9, 28, 4, 47, tzinfo=timezone.utc)
        instant, reason = first_comparable_instant(
            [(floor-timedelta(minutes=1), None, "arm:p1")], [], floor, floor+timedelta(hours=1)
        )
        self.assertIsNone(instant)
        self.assertIn("arm:p1", reason or "")

    def test_max_simultaneous_is_recomputed_only_from_window_admissions(self) -> None:
        since = datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)
        closed = [
            {"pair_id": "old", "opened_at": "2026-09-28T04:59:00Z", "closed_at": "2026-09-28T05:30:00Z"},
            {"pair_id": "a", "opened_at": "2026-09-28T05:01:00Z", "closed_at": "2026-09-28T05:10:00Z"},
            {"pair_id": "b", "opened_at": "2026-09-28T05:02:00Z", "closed_at": "2026-09-28T05:03:00Z"},
        ]
        opened = [{"pair_id": "c", "open_ts": "2026-09-28T05:10:00Z", "status": "OPEN"}]
        self.assertEqual(calculate_max_simultaneous(closed, opened, since), 2)

    def test_operational_warmup_counts_own_arm_events_without_economics(self) -> None:
        events = [
            {"event": "HS_BEAR_CLUSTER_TRIGGERED"},
            {"event": "EXPERIMENTAL_CLOSE"},
            {"event": "EXPERIMENTAL_CLOSE"},
        ]
        records = [
            {"exit_reason": "HS_BEAR_CLUSTER_EXIT", "net_pnl_pct": -1.0},
            {"exit_reason": "HS_BEAR_CLUSTER_EXIT", "net_pnl_pct": -2.0},
        ]
        counts = operational_warmup_counts("hs_bear", events, records)
        self.assertEqual(counts["HS_BEAR_CLUSTER_TRIGGERED"], 1)
        self.assertEqual(counts["EXPERIMENTAL_CLOSE"], 2)
        self.assertEqual(counts["positions closed by cluster"], 2)
        self.assertNotIn("net", counts)


if __name__ == "__main__":
    unittest.main()
