from __future__ import annotations

import unittest
from datetime import datetime, timezone

from tools.shadow_market_report import _filter_since


class ShadowMarketReportTests(unittest.TestCase):
    def test_since_filters_selected_timestamp_field(self) -> None:
        records = [
            {
                "pair_id": "old-open",
                "opened_at": "2026-08-08T20:59:00+00:00",
                "closed_at": "2026-08-08T22:00:00+00:00",
            },
            {
                "pair_id": "new-open",
                "opened_at": "2026-08-08T21:01:00+00:00",
                "closed_at": "2026-08-08T22:30:00+00:00",
            },
        ]
        since = datetime(2026, 8, 8, 21, 0, tzinfo=timezone.utc)

        filtered = _filter_since(records, since, "opened_at")

        self.assertEqual([item["pair_id"] for item in filtered], ["new-open"])


if __name__ == "__main__":
    unittest.main()
