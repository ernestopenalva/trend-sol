import unittest
from copy import deepcopy
from types import SimpleNamespace
from tools.trail_atr_lifecycle_study import ATRPosition, ClosedATR, capture, economics, realized_dd, table, fmt
from tools.market_bot_replay import ReplayExecutionClient, NullLogger


class ATRLifecycleTests(unittest.TestCase):
    def position(self,variant='ATR_DYNAMIC'):
        config={'hard_stop':{'enabled':True,'stop_pct':1.5},
            'profit_lock':{'mode':'atr','steps':[{'trigger_atr':5,'lock_atr':1.5}]},
            'trailing':{'mode':'atr','activation_atr':10,'gap_atr':5},'breakeven':{'mode':'off'}}
        client=ReplayExecutionClient(0)
        p=ATRPosition(pair_id='t',symbol='SOLUSDT',entry_price=100,quantity=.2,
            entry_order={},open_ts='2026-06-01T00:00:00+00:00',config=config,
            client=client,logger=NullLogger(),entry_atr=1)
        series=SimpleNamespace(snapshot=lambda at:{'atr':2 if at==0 else 1,'close_ms':at-1,'open_ms':at-300000})
        review=SimpleNamespace(context_fields=lambda at:{'at':at})
        p.initialize_study(variant,series,review,0)
        return p,client

    def tick(self,p,client,price):
        client.current_price=price;p.on_tick(price,'2026-06-01T00:01:00+00:00')

    def test_closed_5m_selection_excludes_current_open(self):
        candles=[SimpleNamespace(open_time_ms=i*300000,close_time_ms=(i+1)*300000-1,
            boundary_ms=(i+1)*300000,high=102+i,low=100+i,close=101+i) for i in range(20)]
        s=ClosedATR(candles,14)
        self.assertEqual(s.snapshot(15*300000+1000)['open_ms'],14*300000)
        self.assertEqual(s.snapshot(15*300000)['close_ms'],15*300000-1)
        self.assertEqual(s.snapshot(15*300000-1)['close_ms'],14*300000-1)

    def test_peak_updates_only_on_new_peak(self):
        p,c=self.position('ATR_PEAK');self.tick(p,c,112)
        before=(p.trailing_stop,deepcopy(p.peak_snapshot))
        p.set_clock(300000);self.tick(p,c,111)
        self.assertEqual((p.trailing_stop,p.peak_snapshot),before)
        self.tick(p,c,113)
        self.assertEqual(p.peak_snapshot['atr'],1)
        self.assertEqual(p.trailing_stop,108)

    def test_dynamic_tightens_without_new_peak(self):
        p,c=self.position();self.tick(p,c,112)
        self.assertEqual(p.trailing_stop,102)
        p.set_clock(300000)
        self.assertEqual(p.trailing_stop,107)
        self.assertEqual(p.highest_price,112)
        self.assertEqual(p.counters['tighten_without_peak_atr_decrease'],1)
        self.assertEqual(p.additional_stop_rise,5)

    def test_ratchet_blocks_atr_increase(self):
        p,c=self.position();self.tick(p,c,112);p.set_clock(300000)
        stop=p.trailing_stop;p.set_clock(0)
        self.assertEqual(p.trailing_stop,stop)
        self.assertEqual(p.counters['widen_blocked_by_ratchet'],1)

    def test_original_activation_uses_frozen_entry_not_alternative_atr(self):
        p,c=self.position();self.tick(p,c,109)
        self.assertFalse(p.trailing_active)
        self.tick(p,c,110)
        self.assertTrue(p.trailing_active)
        self.assertEqual(p.activation['atr'],1)
        self.assertNotIn('ATR_DYNAMIC',p.diagnostic_activation)

    def test_original_pl_and_hs_unchanged(self):
        for v in ('ATR_ENTRY','ATR_PEAK','ATR_DYNAMIC'):
            p,c=self.position(v);self.assertEqual(p.hard_stop_price,98.5)
            self.tick(p,c,105);self.assertEqual(p.profit_lock_stop,101.5)
            self.assertFalse(p.trailing_active);self.tick(p,c,101.5)
            self.assertEqual(p.exit_reason,'PROFIT_LOCK')

    def test_entry_control_gap_and_stop_priority(self):
        p,c=self.position('ATR_ENTRY');self.tick(p,c,112)
        self.assertEqual(p.trailing_stop,107)
        self.tick(p,c,107);self.assertEqual(p.exit_reason,'TRAILING')

    def test_gap_executes_at_real_open_not_stop(self):
        p,c=self.position();self.tick(p,c,112);p.set_clock(300000)
        self.tick(p,c,103)
        self.assertEqual(p.exit_price,103)

    def test_capture_own_and_control_denominators(self):
        self.assertEqual(capture(100,105,110),.5)
        self.assertEqual(capture(100,105,120),.25)
        self.assertIsNone(capture(100,105,100))
        self.assertIsInstance(table(['PF'],[[fmt('inf')]]),str)
        self.assertIn('| inf |',table(['PF'],[[fmt('inf')]]))

    def test_censored_excluded_from_net_delta_winner_labels(self):
        e={'arms':{'ATR_ENTRY':{'net':1},'ATR_DYNAMIC':{'net':None,'counters':{},
            'stop_rise_atr_decrease_no_peak':0,'effective_stop_rise_atr_updates':0}}}
        r=economics([e],'ATR_DYNAMIC')
        self.assertEqual((r['closed'],r['censored']),(0,1))
        self.assertEqual((r['net'],r['delta'],r['winners_to_losers']),(0,0,0))
        self.assertIsNone(r['net_trade'])

    def test_realized_dd_ordered_close_times(self):
        self.assertEqual(realized_dd([{'closed_ms':2,'net':-3},{'closed_ms':1,'net':2},
            {'closed_ms':3,'net':1},{'closed_ms':None,'net':None}]),3)


if __name__=='__main__':unittest.main()
