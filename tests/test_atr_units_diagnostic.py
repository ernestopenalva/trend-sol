import unittest
from tools.atr_units_diagnostic import quartile,distribution,summary


class UnitsTests(unittest.TestCase):
    def test_fixed_quartile_boundaries(self):
        self.assertEqual(quartile(.05,[.05,.1,.2]),1)
        self.assertEqual(quartile(.15,[.05,.1,.2]),3)

    def test_existing_units_converted(self):
        entry=100;atr=.05;atr_pct=100*atr/entry
        self.assertAlmostEqual(.25/atr_pct,5)
        self.assertAlmostEqual(1.5/atr_pct,30)
        self.assertEqual(max(1.5,.25/atr_pct),max(3,.25/atr_pct))

    def test_quantiles_not_means(self):
        self.assertEqual(distribution([1,2,3,100])['median'],2.5)

    def test_group_summary_preserves_net(self):
        rows=[{'closed_ms':1,'net_usd':.1,'gross_before_costs':.15,'exit_group':'PL1','mfe_atr':7,
               'economic_floor_atr':4,'armed_steps':[1,2]},
              {'closed_ms':2,'net_usd':-.2,'gross_before_costs':-.15,'exit_group':'HS','mfe_atr':2,
               'economic_floor_atr':2,'armed_steps':[]}]
        r=summary(rows)
        self.assertAlmostEqual(r['net'],-.1)
        self.assertEqual(r['both_armed_coincident'],1)
        self.assertEqual(r['PF'],.5)
