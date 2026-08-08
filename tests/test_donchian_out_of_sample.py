from __future__ import annotations

import unittest

from tools.donchian_out_of_sample import _parse_date_end, _parse_date_start


class DonchianOutOfSampleTests(unittest.TestCase):
    def test_date_window_is_utc_and_end_is_inclusive(self) -> None:
        start = _parse_date_start("2025-10-01")
        end = _parse_date_end("2025-10-01")

        self.assertEqual(end - start + 1, 24 * 3_600_000)


if __name__ == "__main__":
    unittest.main()
