import unittest
from tools.pl_lon_natural_reentry_study import ownership, economics, valid_split

class NaturalReentryTests(unittest.TestCase):
    def test_latest_strictly_prior_pl_owns_entry(self):
        events=[{'touch_ms':10,'source_candle':1},{'touch_ms':20,'source_candle':2}]
        self.assertEqual(ownership(events,20),[1])
        self.assertEqual(ownership(events,21),[2])
        self.assertEqual(ownership(events,10),[])

    def test_ties_are_preserved_not_arbitrarily_resolved(self):
        self.assertEqual(ownership([{'touch_ms':10,'source_candle':1},
                                    {'touch_ms':10,'source_candle':2}],11),[1,2])

    def test_realized_economics_sorted_excludes_open(self):
        ts=[{'closed_ms':2,'net_usd':-1,'exit_reason':'HARD_STOP'},
            {'closed_ms':1,'net_usd':2,'exit_reason':'PROFIT_LOCK'},
            {'closed_ms':None,'net_usd':None,'exit_reason':'OPEN'}]
        r=economics(ts)
        self.assertEqual((r['closed'],r['open'],r['net'],r['pf'],r['dd']),(2,1,1,2,1))

    def test_partial_rounding_and_both_sides_minimum(self):
        self.assertTrue(valid_split(.2,100,.5,.001,.001,5)['valid'])
        self.assertFalse(valid_split(.099,100,.5,.001,.001,5)['valid'])
        r=valid_split(.101,100,.5,.001,.001,5)
        self.assertEqual(r['sold_qty'],'0.050');self.assertTrue(r['valid'])

if __name__=='__main__':unittest.main()
