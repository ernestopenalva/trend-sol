import unittest
from tools.fast_zone_trajectory_study import features,windows,effect,recent_analysis


class ZoneTrajectoryTests(unittest.TestCase):
    def test_cut_ignores_future_and_total_future_lifetime(self):
        quotes=[(60000,99.4),(900000,99.8),(960000,90)]
        closes=[(i*60000,99.4) for i in range(1,61)]
        a=windows(0,99.5,100,quotes,closes,99999999,99999999)
        b=windows(0,99.5,100,quotes[:-1],closes,999999999,999999999)
        self.assertEqual(a[0]['features'],b[0]['features'])
        self.assertEqual(a[0]['features']['worst_pnl'],(99.4/100-1)*100)
        self.assertNotEqual(a[1]['features']['worst_pnl'],b[1]['features']['worst_pnl'])

    def test_closed_early_never_completed_to_cut(self):
        rows=windows(0,99.5,100,[(60000,99.4),(120000,98.5),(900000,110)],
            [(60000,99.4),(120000,98.5),(900000,110)],120000,99999999)
        self.assertEqual(rows[0]['status'],'CLOSED_BEFORE_CUT')
        self.assertEqual(rows[0]['observed_minutes'],2)
        self.assertLess(rows[0]['features']['best_pnl'],0)
        self.assertEqual(rows[0]['features']['minutes_below050'],2)

    def test_bottom_not_confirmed_with_future_outside_prefix(self):
        closes=[(0,99.5),(60000,99.4),(120000,99.2)]
        a=features(closes,closes,100,99.5)
        self.assertEqual(a['local_bottom_count'],0)
        self.assertIsNone(a['best_recovery_after_first_bottom'])
        b=features(closes+[(180000,99.4)],closes+[(180000,99.4)],100,99.5)
        self.assertEqual(b['local_bottom_count'],1)
        self.assertGreater(b['best_recovery_after_first_bottom'],0)

    def test_rebound_loss_and_successive_bottoms(self):
        s=[(i*60000,p) for i,p in enumerate([99.5,99.2,99.4,99.1,99.3,99.0])]
        f=features(s,s,100,99.5)
        self.assertEqual(f['local_bottom_count'],2)
        self.assertEqual(f['recovery_count'],2)
        self.assertEqual(f['new_bottom_after_recovery_count'],1)
        self.assertEqual(f['bottoms_descending'],1)
        self.assertAlmostEqual(f['largest_recovery_lost_again'],.2)

    def test_ties_missing_metrics_and_effect(self):
        s=[(0,99.5),(60000,99.5)]
        f=features(s,s,100,99.5)
        self.assertIsNone(f['recovery_speed_median'])
        self.assertIsNone(f['bottom_slope_pct_per_min'])
        self.assertEqual(effect([1,2],[1,2])['cliff_delta'],0)
        self.assertEqual(effect([3,4],[1,2])['KS_D'],1)
        self.assertIsNone(effect([],[])['cliff_delta'])

    def test_gap_crossing_does_not_redefine_FAST_recovery_level(self):
        s=[(0,99.3),(60000,99.4)]
        self.assertEqual(features(s,s,100,99.3)['recovered_FAST'],0)
        s.append((120000,99.5))
        self.assertEqual(features(s,s,100,99.3)['recovered_FAST'],1)

    def test_recent_partial_entry_bar_cannot_prove_first_zone_anchor(self):
        raw={'end_ms':120000,'public_1m':[[0,100,101,99,100,0,59999]],
            'rows':[{'opened_at':'1970-01-01T00:00:30+00:00','entry_price':100,
                     'source_candle_open_time':1,'exit_reason':'HARD_STOP'}]}
        r=recent_analysis(raw,'HIGH_FIRST')[0]
        self.assertEqual(r['quality'],'ZONE_ANCHOR_UNCERTAIN_PARTIAL_ENTRY')
        self.assertEqual(r['windows'],[])


if __name__=='__main__':unittest.main()
