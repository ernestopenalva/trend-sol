import unittest
from tools.hs_overshoot_operational_audit import economics

class OvershootTests(unittest.TestCase):
    def test_phantom_uses_actual_quantity_and_fixed_cost(self):
        r={'entry_price':120.53,'exit_price':117.53,'hard_stop_price':118.72205,'qty':20/120.53,'estimated_fees_pct':.2,'net_pnl_pct':-2.6890068862524}
        e=economics(r)
        self.assertAlmostEqual(e['additional_usd'],.19780137725048)
        self.assertAlmostEqual(e['overshoot_pp'],.9890068862524)
        self.assertAlmostEqual(e['expected_net_pct'],-1.7)

    def test_real_fill_vs_trigger_not_conflated(self):
        e=economics({'entry_price':120.54,'exit_price':117.29,'hard_stop_price':118.7319,'qty':.165})
        self.assertAlmostEqual(e['additional_usd'],.2379135)

    def test_early_cb_close_is_not_additional_loss(self):
        e=economics({'entry_price':120.53,'exit_price':119.14,'hard_stop_price':118.72205,'qty':20/120.53})
        self.assertEqual(e['additional_usd'],0)
        self.assertLess(e['overshoot_pp'],0)

    def test_missing_stop_is_not_assumed(self):
        self.assertIsNone(economics({'entry_price':120,'exit_price':117,'qty':.1}))

if __name__=='__main__':unittest.main()
