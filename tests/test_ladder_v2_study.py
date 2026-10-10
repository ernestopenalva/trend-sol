import unittest
from tools.ladder_v2_study import Instrumentation
from tools.market_bot_replay import NullLogger, ReplayExecutionClient

class LadderV2Tests(unittest.TestCase):
    def position(self,v2=True,atr=1):
        inst=Instrumentation(0,9999999999999,20,v2=v2);client=ReplayExecutionClient(0)
        cfg=dict(hard_stop=dict(enabled=True,stop_pct=1.5),breakeven=dict(mode='off'),
            trailing=dict(mode='atr',activation_atr=10,gap_atr=5),
            profit_lock=dict(mode='atr',steps=[dict(trigger_atr=5,lock_atr=1.5),dict(trigger_atr=8,lock_atr=3),dict(trigger_atr=12,lock_atr=6)],
                economic_floor=dict(enabled=True,net_margin_pct=.05)),fees=dict(enabled=True,taker_fee_pct=.1))
        p=inst.factory(pair_id='test',symbol='SOLUSDT',entry_price=100,quantity=.2,entry_order={},
            open_ts='2026-06-01T00:00:00+00:00',config=cfg,client=client,logger=NullLogger(),entry_atr=atr)
        return p,client
    def tick(self,p,c,price,k):
        c.current_price=price;p.on_tick(price,f'2026-06-01T00:{k:02d}:00+00:00')
    def test_trail_enabled_with_pl1_and_peak_since_entry(self):
        p,c=self.position();self.tick(p,c,105,1)
        self.assertTrue(p.trailing_active);self.assertEqual(p.highest_price,105)
        self.assertAlmostEqual(p.trailing_stop,92);self.assertEqual(p.stop_type,'profit_lock')
        self.tick(p,c,116,2);self.assertEqual(p.stop_type,'trailing');self.assertEqual(p.effective_stop,103)
        self.tick(p,c,115,3);self.assertEqual(p.highest_price,116);self.assertEqual(p.effective_stop,103)
    def test_economic_floor_delays_trigger(self):
        p,c=self.position(atr=.05);self.tick(p,c,100.4,1)
        self.assertFalse(p.trailing_active)
        self.tick(p,c,100.425,2);self.assertTrue(p.trailing_active)
        self.assertAlmostEqual(p.profit_lock_stop,100.25)
    def test_no_pl2_pl3(self):
        p,c=self.position();self.tick(p,c,112,1)
        self.assertEqual(p.applied_steps,{'atr:1'});self.assertEqual(p.profit_lock_stop,101.5)
    def test_parity_mode_preserves_original_configuration(self):
        p,c=self.position(False);self.tick(p,c,108,1)
        self.assertFalse(p.trailing_active);self.assertEqual(p.profit_lock_stop,103)
        self.tick(p,c,112,2);self.assertTrue(p.trailing_active);self.assertEqual(p.effective_stop,107)

if __name__=='__main__':unittest.main()
