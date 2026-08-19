from __future__ import annotations

import unittest

from tools.indicator_ranking import (
    _aggregate_exit_quality,
    _fmt_exit_quality,
    _peak_excursion_pct,
)


class IndicatorRankingTests(unittest.TestCase):
    def test_float_residue_at_entry_is_not_treated_as_real_mfe(self) -> None:
        self.assertIsNone(_peak_excursion_pct(75.86, 75.86000000000001))
        quality, sample = _aggregate_exit_quality([(-0.84, None)])
        self.assertIsNone(quality)
        self.assertEqual(sample, 0)
        self.assertEqual(_fmt_exit_quality(quality, sample), "n/a (N=0)")

    def test_exit_quality_uses_ratio_of_totals_instead_of_mean_of_ratios(self) -> None:
        quality, sample = _aggregate_exit_quality([
            (0.25, 0.50),
            (-0.20, 0.10),
        ])

        self.assertEqual(sample, 2)
        self.assertAlmostEqual(quality or 0, 8.3333333333)
        self.assertEqual(_fmt_exit_quality(quality, sample), "8.3% (N=2)")

    def test_negative_aggregate_retention_remains_visible(self) -> None:
        quality, sample = _aggregate_exit_quality([
            (-0.30, 0.20),
            (0.10, 0.30),
        ])

        self.assertEqual(sample, 2)
        self.assertAlmostEqual(quality or 0, -40.0)


if __name__ == "__main__":
    unittest.main()
