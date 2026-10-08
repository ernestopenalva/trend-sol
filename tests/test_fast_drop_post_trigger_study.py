import unittest
from tools.fast_drop_post_trigger_study import recovery_stats,distribution


class PostTriggerTests(unittest.TestCase):
    def curve(self,prices,times=None):
        return [{'price':p,'at_ms':(times[i] if times else i*60000),'ordinal':i} for i,p in enumerate(prices)]

    def test_earlier_recovery_is_not_recovery_after_terminal_worst(self):
        s=recovery_stats(self.curve([100,99,101,98]),100)
        self.assertTrue(s['recovered_any_before_exit'])
        self.assertTrue(s['recovered_before_final_worst'])
        self.assertFalse(s['recovered_after_worst'])
        self.assertTrue(s['censored_for_recovery_after_worst'])
        self.assertFalse(s['censored_for_any_recovery'])
        self.assertIsNone(s['worst_to_recovery_min'])
        self.assertEqual(s['followup_after_worst_min'],0)
        self.assertEqual(s['deepest_quote_before_observed_return'],99)

    def test_recovery_on_exit_is_not_before_exit(self):
        s=recovery_stats(self.curve([100,99,100]),100)
        self.assertFalse(s['recovered_after_worst'])
        self.assertTrue(s['censored_for_any_recovery'])

    def test_intraminute_order_not_elapsed_precision(self):
        s=recovery_stats(self.curve([100,99,100.1,100.2],[60000,60000,60000,120000]),100)
        self.assertTrue(s['recovered_after_worst'])
        self.assertEqual(s['worst_to_recovery_min'],0)
        self.assertTrue(s['first_recovery_same_minute'])
        self.assertFalse(s['any_recovery_later_minute'])

    def test_first_global_minimum_tie_and_quantiles(self):
        s=recovery_stats(self.curve([100,99,100.1,99,100.1,100.2]),100)
        self.assertEqual(s['worst_index'],1)
        self.assertEqual(s['recovery_after_worst_index'],2)
        d=distribution([4,1,3,2])
        self.assertEqual(d['all_sorted'],[1,2,3,4])
        self.assertEqual(d['p25'],1.75)
        self.assertIsNone(distribution([])['p50'])


if __name__=='__main__':unittest.main()
