import unittest
from unittest.mock import patch
from types import SimpleNamespace
from tools.trail_atr_same_scale_study import ScalePosition, distribution, paired_metrics, activation_stats
from tools.trail_atr_lifecycle_study import ClosedATR
from tools.market_bot_replay import ReplayExecutionClient,NullLogger


class SameScaleTests(unittest.TestCase):
    def position(self,tf,v):
        c=ReplayExecutionClient(0)
        cfg={'hard_stop':{'enabled':True,'stop_pct':1.5},
            'profit_lock':{'mode':'atr','steps':[{'trigger_atr':5,'lock_atr':1.5}]},
            'trailing':{'mode':'atr','activation_atr':10,'gap_atr':5},'breakeven':{'mode':'off'}}
        p=ScalePosition(pair_id='test',symbol='SOLUSDT',entry_price=100,quantity=.2,
            entry_order={},open_ts='2026-06-01T00:00:00+00:00',config=cfg,
            client=c,logger=NullLogger(),entry_atr=1)
        s=SimpleNamespace(timeframe=tf,snapshot=lambda at:{'atr':2 if at==0 else 1,'close_ms':at-1})
        p.initialize_study(v,s,SimpleNamespace(context_fields=lambda at:{}),0)
        return p,c

    def tick(self,p,c,price):
        c.current_price=price;p.on_tick(price,'2026-06-01T00:01:00+00:00')

    def test_entry5_gap_changes_only_gap_not_activation_pl_hs(self):
        p,c=self.position('5m','ATR_ENTRY');self.tick(p,c,110)
        self.assertTrue(p.trailing_active);self.assertEqual(p.activation['atr'],1)
        self.assertEqual(p.trailing_stop,100);self.assertEqual(p.entry_atr,1)
        self.assertEqual(p.hard_stop_price,98.5);self.assertEqual(p.profit_lock_stop,101.5)
        self.assertNotIn('ENTRY_SCALE',p.diagnostic_activation)

    def test_entry1_uses_authoritative_stored_atr(self):
        p,c=self.position('1m','ATR_ENTRY');self.tick(p,c,110)
        self.assertEqual(p.trailing_stop,105)
        self.assertIn('ENTRY_SCALE',p.diagnostic_activation)

    def test_entry5_stays_frozen_after_new_closed_candle(self):
        p,c=self.position('5m','ATR_ENTRY');self.tick(p,c,112)
        p.set_clock(60000);self.tick(p,c,113)
        self.assertEqual(p.trailing_stop,103)

    def test_peak1_does_not_update_without_new_peak(self):
        p,c=self.position('1m','ATR_PEAK');self.tick(p,c,112)
        p.set_clock(60000);self.tick(p,c,111)
        self.assertEqual(p.trailing_stop,102)
        self.tick(p,c,113);self.assertEqual(p.trailing_stop,108)

    def test_dynamic1_updates_without_new_peak_and_ratchets(self):
        p,c=self.position('1m','ATR_DYNAMIC');self.tick(p,c,112)
        p.set_clock(60000);self.assertEqual(p.trailing_stop,107)
        p.set_clock(0);self.assertEqual(p.trailing_stop,107)

    def test_causal_1m_selection(self):
        cs=[SimpleNamespace(open_time_ms=i*60000,close_time_ms=(i+1)*60000-1,
            boundary_ms=(i+1)*60000,high=102,low=100,close=101) for i in range(20)]
        s=ClosedATR(cs,14)
        self.assertEqual(s.snapshot(15*60000+100)['close_ms'],15*60000-1)
        self.assertEqual(s.snapshot(15*60000-1)['close_ms'],14*60000-1)

    def test_distribution_has_requested_quantiles(self):
        d=distribution(list(range(101)))
        self.assertEqual([d[k] for k in ('p10','p25','p50','p75','p90')],[10,25,50,75,90])

    def test_censored_pair_not_given_zero_realized(self):
        e={'replays':{'5m':{'ATR_ENTRY':{'net':1},'ATR_PEAK':{'net':None}}}}
        r=paired_metrics([e],'5m','ATR_PEAK')
        self.assertEqual(r['paired_closed'],0);self.assertEqual(r['variant_censored'],1)
        self.assertIsNone(r['net_trade']);self.assertIsNone(e['replays']['5m']['ATR_PEAK']['net'])

    def test_missing_activation_is_not_a_fake_delay(self):
        e={'opened_ms':0,'activation_diagnostic':{'5m':{'ATR_DYNAMIC':{'modeled_at_ms':60000,'price':105}}}}
        r=activation_stats([e],'5m','ATR_DYNAMIC')
        self.assertEqual(r['base_not_reached'],1);self.assertEqual(r['both_reached'],0)
        self.assertIsNone(r['delta_min']['p50'])

    def test_5m_delta_uses_5m_entry_control_not_runtime(self):
        def row(net,price):return {'net':net,'closed_ms':60000,'exit_price':price,'peak':110}
        e={'entry_price':100,'replays':{'1m':{'ATR_ENTRY':row(10,105)},
            '5m':{'ATR_ENTRY':row(2,102),'ATR_PEAK':row(3,103)}}}
        before=e['replays']['5m']['ATR_PEAK'].copy()
        with patch('tools.trail_atr_same_scale_study.economics',
                   side_effect=lambda es,v:{'delta':sum(x['arms'][v]['delta'] for x in es)}):
            r=paired_metrics([e],'5m','ATR_PEAK')
        self.assertEqual(r['delta'],1)
        self.assertEqual(e['replays']['5m']['ATR_PEAK'],before)


if __name__=='__main__':unittest.main()
