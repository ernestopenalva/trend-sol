import unittest
from tools.be_off_cb_defensive_review import Review, active, classify_pattern, exit_economics, hs_clusters, recovery_rate, summarize
from tools.market_selection_study import MarketCandle
from tests.test_ge_replay_study import _config, _signal


class DefensiveReviewTests(unittest.TestCase):
    def test_recovery_never_uses_future_outcome(self):
        rows=[{'crossed_ms':10,'closed_ms':20,'exit_reason':'TRAILING'},
              {'crossed_ms':15,'closed_ms':30,'exit_reason':'HARD_STOP'},
              {'crossed_ms':18,'closed_ms':None,'exit_reason':'CENSORED'},
              {'crossed_ms':None,'closed_ms':10,'exit_reason':'PROFIT_LOCK'}]
        self.assertEqual(recovery_rate(rows,30)['rate'],1)
        self.assertEqual(recovery_rate(rows,31)['rate'],.5)
        self.assertEqual(recovery_rate(rows,30)['n'],1)
        self.assertLess(recovery_rate(rows,30)['latest_outcome_ms'],30)

    def test_cluster_definition_fixed_and_no_single_hs(self):
        rows=[{'closed_ms':i*60000,'exit_reason':'HARD_STOP'} for i in (0,30,70,200)]
        self.assertEqual([len(g) for g in hs_clusters(rows)],[3])

    def test_crisis_positions_exclude_same_instant_closed_or_admitted(self):
        rows=[{'opened_ms':0,'closed_ms':10},{'opened_ms':1,'closed_ms':20},
              {'opened_ms':10,'closed_ms':None},{'opened_ms':2,'closed_ms':None}]
        self.assertEqual(len(active(rows,10,True)),2)
        self.assertEqual(len(active(rows,10,False)),3)

    def test_exit_prices_apply_existing_friction_and_delta(self):
        r={'entry_price':100,'net_usd':-.34,'closed_ms':20,'exit_reason':'HARD_STOP'}
        v=exit_economics(r,99,20,5,.2)
        self.assertAlmostEqual(v['hypothetical_price'],98.97525)
        self.assertAlmostEqual(v['hypothetical_net'],20*((98.97525/100-1)*100-.2)/100)
        self.assertAlmostEqual(v['delta'],v['hypothetical_net']+.34)

    def test_censored_results_not_counted_as_losses(self):
        rows=[{'delta':.1,'control_exit':'HARD_STOP'},{'delta':-.2,'control_exit':'TRAILING'},
              {'delta':None,'control_exit':'CENSORED'}]
        s=summarize(rows)
        self.assertEqual(s['resolved'],2);self.assertEqual(s['pending'],1)
        self.assertAlmostEqual(s['delta'],-.1);self.assertEqual(s['winner_cost'],.2)

    def test_one_october_episode_cannot_be_consistent(self):
        self.assertEqual(classify_pattern([{'n':0,'delta':0}]*4+[{'n':20,'delta':10}]),'amostra insuficiente')

    def review(self):
        r=Review.__new__(Review)
        r.config=_config();r.config['risk']['hard_stop']['stop_pct']=1.5
        r.config['risk']['breakeven']={'mode':'off'}
        r.signal={60000:_signal(1,100).signal};r.notional=20;r.spread=0;r.fees=0;r.end=240000
        r.minute=[MarketCandle(i*60000,(i+1)*60000-1,100 if i==1 else 98.4,
                              100 if i==1 else 98.4,98.4 if i==1 else 97,98.4 if i==1 else 97,10,1)
                  for i in (1,2,3)]
        r.opens=[c.open_time_ms for c in r.minute];r.index={c.boundary_ms:c for c in r.minute}
        r.context=lambda at:{'ema_context':'LON' if at<180000 else 'SHO','macd_context':'BU-',
                             'latest_closed_at_ms':at-1,'close':97}
        r.context_fields=lambda at:r.context(at)
        return r

    def test_isolated_normal_exit_preserves_baseline_hs(self):
        r=self.review();out=r.standalone(60000,'HIGH_FIRST')
        self.assertEqual(out['exit_reason'],'HARD_STOP')
        self.assertEqual(out['closed_ms'],120000)
        self.assertAlmostEqual(out['net_usd'],-.3)

    def test_elastic_keeps_hs_suspended_until_causal_context_loss(self):
        r=self.review();base={'entry_price':100,'closed_ms':120000}
        out=r.standalone(60000,'HIGH_FIRST',base,elastic=True)
        self.assertEqual(out['exit_reason'],'HARD_STOP_ELASTIC_CONTEXT_LOST')
        self.assertEqual(out['closed_ms'],180000)
        self.assertEqual(out['extra_minutes'],1)
        self.assertAlmostEqual(out['extra_adverse_pct'],1.5)

    def test_open_baseline_fast_clone_cannot_subtract_unknown_close_time(self):
        r=self.review();r.minute=r.minute[:1];r.minute[0]=MarketCandle(60000,119999,100,100,100,100,10,1)
        r.opens=[60000];r.index={120000:r.minute[0]}
        out=r.standalone(60000,'HIGH_FIRST',{'entry_price':100,'closed_ms':None})
        self.assertEqual(out['exit_reason'],'CENSORED');self.assertIsNone(out['net_usd'])


if __name__=='__main__':unittest.main()
