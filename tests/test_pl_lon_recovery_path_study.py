import unittest
from tools.pl_lon_recovery_path_study import first_passage, diagnose, summarize

class RecoveryPathTests(unittest.TestCase):
    def test_low_after_recovery_does_not_inflate_required_slack(self):
        r=first_passage([(60000,99),(60000,103),(60000,95)],102,100,2,0)
        self.assertTrue(r['recovered']);self.assertEqual(r['required_atr'],.5)
        self.assertEqual(r['minutes'],1)

    def test_before_recovery_low_changes_with_intrabar_path(self):
        high=first_passage([(60000,103),(60000,95)],102,100,2,0)
        low=first_passage([(60000,95),(60000,103)],102,100,2,0)
        self.assertEqual(high['required_atr'],0);self.assertEqual(low['required_atr'],2.5)

    def test_no_recovery_has_no_hindsight_slack_estimate(self):
        r=first_passage([(60000,99)],102,100,2,0)
        self.assertFalse(r['recovered']);self.assertIsNone(r['required_atr'])

    def test_touch_equal_is_not_a_rebound(self):
        self.assertFalse(first_passage([(0,100)],100,100,1,0,True)['recovered'])

    def test_floor_before_peak_not_after_peak(self):
        e={'touch_price':100,'entry_atr':1,'touch_ms':0,
           'state':{'highest_price':102,'hard_stop_price':98.5},'existing_net_floor':99.5}
        a=diagnose(e,[(60000,103),(60000,99)],60000,True)
        b=diagnose(e,[(60000,99),(60000,103)],60000,True)
        self.assertTrue(a['peak_restored_without_floor_breach'])
        self.assertFalse(b['peak_restored_without_floor_breach'])

    def test_censored_window_excluded_from_rates(self):
        e={'touch_price':100,'entry_atr':1,'touch_ms':0,
           'state':{'highest_price':102,'hard_stop_price':98.5},'existing_net_floor':99.5}
        r=diagnose(e,[(60000,103)],60000,False)
        s=summarize([{'windows':{'60':r}}],'60')
        self.assertEqual(s['censored'],1);self.assertIsNone(s['peak_restored_pct'])

    def test_floor_already_touched_is_counted_at_zero(self):
        e={'touch_price':100,'entry_atr':1,'touch_ms':0,
           'state':{'highest_price':102,'hard_stop_price':98.5},'existing_net_floor':100}
        r=diagnose(e,[(60000,103)],60000,True)
        self.assertEqual(r['economic_floor_touched_min'],0)
        self.assertFalse(r['peak_restored_without_floor_breach'])

if __name__=='__main__':unittest.main()
