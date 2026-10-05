import unittest
from types import SimpleNamespace
from tools.pl_lon_protection_study import diagnose, summarize, distribution, ArmingLogger, event_row


class PLProtectionTest(unittest.TestCase):
    def run_path(self,prices,complete=True):
        w=diagnose([(60000*i,p) for i,p in enumerate(prices)],100,102,99,97,2,0,complete)
        w['lon_lost']=False
        return w

    def test_four_classes_and_strict_boundaries(self):
        for prices,expected in [([100,98],'PROTECTION'),([100,103],'EARLY'),
                                ([100,98,103],'MIXED'),([100,99,102],'NEUTRAL')]:
            self.assertEqual(self.run_path(prices)['class'],expected)
        self.assertTrue(self.run_path([102])['peak_recovered'])
        self.assertFalse(self.run_path([102])['new_high'])

    def test_mixed_order_same_minute_uses_path_index(self):
        for prices,expected in [([98,103],'FLOOR_THEN_PEAK'),([103,98],'PEAK_THEN_FLOOR')]:
            w=diagnose([(60000,p) for p in prices],100,102,99,97,2,0)
            self.assertEqual(w['mixed_order'],expected)
            self.assertEqual(w['mixed_gap_min'],0)

    def test_excursions_atr_hs_and_times(self):
        w=self.run_path([100,96,104,101])
        self.assertAlmostEqual(w['adverse_pct'],4)
        self.assertAlmostEqual(w['favorable_pct'],4)
        self.assertEqual(w['adverse_atr'],2)
        self.assertEqual(w['below_floor_abs'],3)
        self.assertEqual(w['hs_min'],1)
        self.assertEqual(w['max_adverse_min'],1)
        self.assertEqual(w['max_favorable_min'],2)
        self.assertEqual(w['final_price'],101)

    def test_censored_excluded_from_classification(self):
        events=[{'windows':{'15':self.run_path([98,103])}},
                {'windows':{'15':self.run_path([96],False)}}]
        r=summarize(events,15)
        self.assertEqual((r['n'],r['valid'],r['censored']),(2,1,1))
        self.assertEqual(r['classes']['MIXED'],1)
        self.assertEqual(r['hs_counterfactual'],0)

    def test_missing_economic_floor_rejected(self):
        with self.assertRaises(ValueError):diagnose([],100,102,None,97,2,0)

    def test_distribution_no_sum_and_small_sample(self):
        self.assertEqual(distribution([1,3])['mean'],2)
        self.assertEqual(distribution([1,3])['median'],2)
        self.assertIsNone(distribution([])['mean'])

    def test_arming_log_only_actual_events(self):
        class Fake:
            boundary=60000
            armings=[]
        f=Fake();logger=ArmingLogger(f)
        logger.trade({'event':'PROFIT_LOCK_SHADOW_ATR_1'})
        logger.trade({'event':'PROFIT_LOCK_ATR_2'})
        self.assertEqual(len(f.armings),1)
        self.assertEqual(f.armings[0]['step'],'PL2')

    def test_event_excludes_pre_exit_extrema_and_marks_short_horizon(self):
        e={'touch_ms':60000,'causal_at_ms':0,'remaining':[99.5,100],
           'entry_atr':1,'entry_price':98,'pl_step':'PL1','pl_stop':100,
           'existing_net_floor':99,'touch_price':100,'snapshot':{},'source_candle':0,
           'state':{'highest_price':102,'hard_stop_price':96},
           'benchmark':{'lon_lost_ms':None}}
        # The exit candle high/low are NOT used wholesale after its touch.
        minutes={60000:SimpleNamespace(open=98,high=110,low=90,close=100)}
        r=event_row(e,'HIGH_FIRST',{'exit_price':99.975},minutes,60000)
        for w in r['windows'].values():
            self.assertEqual(w['best_price'],100)
            self.assertEqual(w['worst_price'],99.5)
            self.assertFalse(w['complete'])


if __name__=='__main__':unittest.main()
