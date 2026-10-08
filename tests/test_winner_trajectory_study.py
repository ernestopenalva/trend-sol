import unittest
from types import SimpleNamespace
from tools.winner_trajectory_study import Pullbacks, terminal, future_label, stage, dist, prediction
from tools.winner_trajectory_readout import economics, intervention


class WinnerTrajectoryTests(unittest.TestCase):
    def test_cashable_peak_accounts_for_exit_spread_and_fees(self):
        self.assertFalse(terminal(100,100.21,100.1,1,-1,.2,5)['eligible'])
        self.assertTrue(terminal(100,100.30,99,1,-1,.2,5)['eligible'])
        self.assertLess(terminal(100,101,99,1,-1,.2)['captured_fraction'],0)

    def test_same_retracement_different_progress_is_not_equivalent(self):
        outcomes=[]
        for advance in (6,25):
            p=Pullbacks(100,1,0);p.observe(100+advance,1,{'ema_context':'LON'})
            p.observe(100+advance-4,2,{'ema_context':'BUL'})
            p.finish(False,3);outcomes.append(p.episodes[0])
        self.assertEqual(outcomes[0]['depth_atr'],outcomes[1]['depth_atr'])
        self.assertAlmostEqual(outcomes[0]['depth_fraction'],4/6)
        self.assertAlmostEqual(outcomes[1]['depth_fraction'],4/25)
        self.assertEqual(outcomes[0]['start_context']['ema_context'],'LON')
        self.assertEqual(outcomes[0]['landmark']['context']['ema_context'],'BUL')

    def test_episodes_disjoint_and_landmark_does_not_use_future_trough(self):
        p=Pullbacks(100,1,0);p.observe(106,1,{})
        p.observe(104.5,2,{});p.observe(102,3,{})
        self.assertEqual(p.episode['landmark']['depth_atr'],1.5)
        p.observe(107,4,{})
        self.assertTrue(p.episodes[0]['recovered_before_exit'])
        self.assertEqual(p.episodes[0]['depth_atr'],4)
        p.observe(104,5,{});p.finish(False,6)
        self.assertEqual(len(p.episodes),2)
        self.assertFalse(p.episodes[1]['recovered_before_exit'])

    def test_future_excludes_current_bar_and_requires_complete_coverage(self):
        bars=[SimpleNamespace(open_time_ms=i*60000,high=120 if i==0 else 105,low=99,close=103) for i in range(3)]
        review=SimpleNamespace(opens=[b.open_time_ms for b in bars],minute=bars)
        label=future_label(review,{'at_ms':60000,'peak':110,'price':100},2)
        self.assertTrue(label['complete']);self.assertFalse(label['recovered'])
        self.assertFalse(future_label(review,{'at_ms':60000,'peak':110,'price':100},3)['complete'])
        bars[2].open_time_ms+=60000
        self.assertFalse(future_label(review,{'at_ms':60000,'peak':110,'price':100},2)['complete'])

    def test_small_model_sample_reports_insufficient(self):
        self.assertEqual(prediction([])['status'],'INSUFFICIENT')
        self.assertEqual(stage(6),'5–10');self.assertEqual(stage(25),'20+')
        self.assertIsNone(dist([])['median'])

    def test_realized_equity_orders_closes_not_entries(self):
        rows=[{'source':'a','closed_ms':2,'net':-3}, {'source':'b','closed_ms':1,'net':2}]
        result=economics(rows)
        self.assertEqual(result['net'],-1)
        self.assertAlmostEqual(result['PF'],2/3)
        self.assertEqual(result['realized_DD'],3)

    def test_intervention_uses_first_known_landmark_and_costs(self):
        row={'source':'a','closed_ms':9,'net':1,'entry':100,'entry_atr':1,
             'reason':'TRAILING','mfe_atr':9,'month':'2026-06',
             'episodes':[{'landmark':{'at_ms':2,'price':102,'peak':104,'advance_atr':4,'depth_atr':2}},
                         {'landmark':{'at_ms':4,'price':108,'peak':109,'advance_atr':9,'depth_atr':1}}]}
        result=intervention([row],.2,5,20)
        self.assertEqual(result['control_same_admissions']['N'],1)
        pair=result['rows'][0]
        self.assertEqual(pair['cf_ms'],2)
        self.assertAlmostEqual(pair['cf_fill'],102*(1-.00025))
        self.assertAlmostEqual(pair['cf_net'],20*((pair['cf_fill']/100-1)-.002))
        self.assertAlmostEqual(pair['delta'],pair['cf_net']-1)


if __name__=='__main__':unittest.main()
