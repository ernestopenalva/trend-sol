from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.forward_experiment_report import event_counts, filter_since, pair_by_source, parse_time


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


if __name__ == "__main__":
    unittest.main()
