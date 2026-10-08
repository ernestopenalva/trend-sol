import unittest
from types import SimpleNamespace
from tools.profit_retention_study import retention,replay,POLICIES


class RetentionTests(unittest.TestCase):
    def test_bounded_predeclared_points(self):
        self.assertLessEqual(len(POLICIES),3)
        self.assertTrue(all(len(p)<=4 for p in POLICIES.values()))

    def test_frontier_units_and_unarmed_region(self):
        self.assertIsNone(retention('affine',.5,.2,.2,.1))
        self.assertAlmostEqual(retention('affine',.5,1,.2,.1),.3)
        self.assertIsNone(retention('concave_budget',2,.2,.2,.1))
        self.assertAlmostEqual(retention('concave_budget',1,1,.2,.25),.5)

    def test_new_peak_does_not_protect_prior_low(self):
        bars=[SimpleNamespace(open_time_ms=0,boundary_ms=60000,open=100,high=102,low=99,close=101)]
        r={'entry':100,'entry_atr':1,'opened_ms':0,'path':'LOW_FIRST','source':'a',
           'month':'2026-06','reason':'TRAILING','net':1,'closed_ms':60000,'mfe_atr':2}
        result=replay(r,bars,[0],60000,'affine',.5,.2,5,20)
        self.assertEqual(result['reason'],'OPEN')
        self.assertIsNone(result['closed_ms'])
        self.assertIsNone(result['net'])
        r['path']='HIGH_FIRST'
        result=replay(r,bars,[0],60000,'affine',.5,.2,5,20)
        self.assertEqual(result['reason'],'RETENTION')
        self.assertGreater(result['net'],0)

    def test_hs_before_positive_peak_is_not_reclassified(self):
        bars=[SimpleNamespace(open_time_ms=0,boundary_ms=60000,open=100,high=100,low=98,close=99)]
        r={'entry':100,'entry_atr':1,'opened_ms':0,'path':'LOW_FIRST','source':'a',
           'month':'2026-06','reason':'HARD_STOP','net':-1,'closed_ms':60000,'mfe_atr':0}
        result=replay(r,bars,[0],60000,'affine',1,.2,5,20)
        self.assertEqual(result['reason'],'HARD_STOP')
        self.assertAlmostEqual(result['exit'],98.5*(1-.00025))

    def test_future_control_labels_do_not_change_decision(self):
        bars=[SimpleNamespace(open_time_ms=0,boundary_ms=60000,open=100,high=103,low=99,close=102)]
        r={'entry':100,'entry_atr':1,'opened_ms':0,'path':'HIGH_FIRST','source':'a',
           'month':'2026-06','reason':'HARD_STOP','net':-1,'closed_ms':60000,'mfe_atr':0}
        a=replay(r,bars,[0],60000,'concave_budget',1,.2,5,20)
        r.update(reason='TRAILING',net=100,closed_ms=999999,mfe_atr=99)
        b=replay(r,bars,[0],60000,'concave_budget',1,.2,5,20)
        for key in ('reason','closed_ms','net','peak','exit','armed_ms'):
            self.assertEqual(a[key],b[key])


if __name__=='__main__':unittest.main()
