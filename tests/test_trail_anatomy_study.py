import unittest
from types import SimpleNamespace
from tools.trail_anatomy_study import classify_window, giveback, magnitude, follow_points, restore_without_trail
from tools.market_bot_replay import ReplayExecutionClient, NullLogger
from src.position.bot_full_engine import BotFullExitPosition
from tools.trail_anatomy_readout import combo_matches


class TrailAnatomyTests(unittest.TestCase):
    def window(self,prices):return classify_window([(60000*i,p) for i,p in enumerate(prices)],100,102,99,97,1,0)

    def test_classes_use_existing_floor_and_recovery(self):
        for prices,expected in [([100,98],'PROTECTION'),([100,102],'EARLY'),
                                ([98,102],'MIXED'),([100,99],'NEUTRAL')]:
            self.assertEqual(self.window(prices)['class'],expected)
        self.assertFalse(self.window([102])['new_high'])

    def test_same_minute_order_and_zero(self):
        w=classify_window([(0,98),(0,103)],100,102,99,97,1,0)
        self.assertEqual(w['mixed_order'],'FLOOR_THEN_RECOVERY')
        self.assertTrue(w['floor_at_zero']);self.assertTrue(w['recovery_at_zero'])
        r=classify_window([(0,103),(0,98)],100,102,99,97,1,0)
        self.assertEqual(r['mixed_order'],'RECOVERY_THEN_FLOOR')

    def test_excluding_remainder_changes_mixed_to_early_without_fake_zero(self):
        inclusive=classify_window([(0,98),(60000,102)],100,102,99,97,1,0)
        later=classify_window([(60000,102)],100,102,99,97,1,0)
        self.assertEqual(inclusive['class'],'MIXED')
        self.assertEqual(later['class'],'EARLY')
        self.assertTrue(inclusive['floor_at_zero'])
        self.assertFalse(later['floor_at_zero']);self.assertFalse(later['recovery_at_zero'])

    def test_giveback_fraction_and_denominator(self):
        g=giveback(100,104,110,1,105)
        self.assertEqual(g['giveback_atr'],6)
        self.assertEqual(g['fraction_peak_returned_pct'],60)
        self.assertEqual(g['giveback_pct_points'],6)
        self.assertEqual(g['effective_gap_atr'],5)

    def test_natural_bins(self):
        pl={'steps':[{'trigger_atr':5},{'trigger_atr':8},{'trigger_atr':12}]}
        self.assertEqual(magnitude(11,pl),'10–12 ATR')
        self.assertEqual(magnitude(30,pl),'>=12 ATR')

    def test_two_descriptive_combinations_require_lon_and_momentum(self):
        e={'snapshot':{'ema_context':'LON','macd_context':'BU+',
                       'histogram_state':'NEGATIVE_RISING'}}
        self.assertTrue(combo_matches(e,'LON + BU+'))
        self.assertFalse(combo_matches(e,'LON + POSITIVE_EXPANDING'))
        e['snapshot']['ema_context']='BUL'
        e['snapshot']['histogram_state']='POSITIVE_EXPANDING'
        self.assertFalse(combo_matches(e,'LON + BU+'))
        self.assertFalse(combo_matches(e,'LON + POSITIVE_EXPANDING'))

    def test_censor_and_small_recoil_not_important_floor_breach(self):
        w=self.window([99.5]);self.assertEqual(w['class'],'NEUTRAL')
        self.assertTrue(w['below_exit_at_zero'])
        w=classify_window([(0,98)],100,102,99,97,1,0,False)
        self.assertEqual(w['class'],'CENSORED')

    def test_crossed_stop_uses_stop_not_segment_endpoint(self):
        class Fake:
            effective_stop=99;status='OPEN'
            seen=[]
            def on_tick(self,p,ts):
                self.seen.append(p)
                if p<=99:self.status='CLOSED'
        p=Fake();self.assertTrue(follow_points(p,SimpleNamespace(),[101,95],60000))
        self.assertEqual(p.seen,[101,99])

    def test_no_trail_restore_preserves_pl_hs_and_original_state(self):
        client=ReplayExecutionClient(0)
        config={'hard_stop':{'enabled':True,'stop_pct':1.5},
            'profit_lock':{'mode':'atr','steps':[{'trigger_atr':5,'lock_atr':1.5}]},
            'trailing':{'mode':'atr','activation_atr':10,'gap_atr':5},
            'breakeven':{'mode':'off'}}
        p=BotFullExitPosition(pair_id='t',symbol='SOLUSDT',entry_price=100,quantity=.2,
            entry_order={},open_ts='2026-06-01T00:00:00+00:00',config=config,
            client=client,logger=NullLogger(),entry_atr=1)
        client.current_price=112;p.on_tick(112,'2026-06-01T00:01:00+00:00')
        state=p.to_state();snapshot=repr(state)
        alt=restore_without_trail({'state':state},config,client)
        self.assertEqual(repr(state),snapshot)
        self.assertIsNone(alt.trailing_stop);self.assertFalse(alt.trailing_active)
        self.assertEqual(alt.hard_stop_price,p.hard_stop_price)
        self.assertEqual(alt.profit_lock_stop,p.profit_lock_stop)
        self.assertEqual(alt.applied_steps,p.applied_steps)
        self.assertFalse(alt._should_activate_trailing(100,100))
        self.assertEqual(alt.effective_stop,alt.profit_lock_stop)
        client.current_price=106;alt.on_tick(106,'2026-06-01T00:02:00+00:00')
        self.assertEqual(alt.status,'OPEN')
        client.current_price=101.5;alt.on_tick(101.5,'2026-06-01T00:03:00+00:00')
        self.assertEqual(alt.exit_reason,'PROFIT_LOCK')
        self.assertEqual(p.status,'OPEN')

    def test_economic_floor_exists_before_any_pl_is_armed(self):
        config={'hard_stop':{'enabled':True,'stop_pct':1.5},
            'profit_lock':{'mode':'atr','steps':[{'trigger_atr':5,'lock_atr':1.5}],
                'economic_floor':{'enabled':True,'net_margin_pct':.05}},
            'fees':{'enabled':True,'taker_fee_pct':.1},
            'trailing':{'mode':'atr','activation_atr':10,'gap_atr':5},
            'breakeven':{'mode':'off'}}
        c=ReplayExecutionClient(0);p=BotFullExitPosition(pair_id='tiny',symbol='SOLUSDT',
            entry_price=100,quantity=.2,entry_order={},open_ts='2026-06-01T00:00:00+00:00',
            config=config,client=c,logger=NullLogger(),entry_atr=.01)
        c.current_price=100.1;p.on_tick(100.1,'2026-06-01T00:01:00+00:00')
        self.assertTrue(p.trailing_active)
        self.assertIsNone(p.profit_lock_economic_floor)
        self.assertAlmostEqual(p._active_profit_lock_economic_floor(),100.25)


if __name__=='__main__':unittest.main()
