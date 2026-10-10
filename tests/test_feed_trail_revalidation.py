import unittest
from tools.feed_trail_revalidation import quantile, dist, stamp, net_value, temporal_blocks


class DiagnosticTests(unittest.TestCase):
    def test_quantiles_interpolated(self):
        self.assertEqual(quantile([1,3],.5),2)
        self.assertEqual(quantile([1,3],1),3)

    def test_empty_not_zero(self):
        self.assertIsNone(quantile([],.5))
        self.assertEqual(dist([])['n'],0)

    def test_brt_boundary(self):
        self.assertEqual(stamp('2026-10-08T23:52:00-03:00'),stamp('2026-10-09T02:52:00Z'))

    def test_control_net_percentage(self):
        self.assertAlmostEqual(net_value({'net_pnl_pct':-1.7,'position_notional_usdt':20}),-.34)
        self.assertEqual(net_value({'net_pnl':.1}),.1)
        with self.assertRaises(ValueError):net_value({})

    def test_overlapping_temporal_blocks(self):
        blocks=temporal_blocks([{'at':v} for v in [0,10,20,30,40]],20)
        self.assertEqual([[w['at'] for w in b] for b in blocks],[[0,10],[10,20],[20,30]])
