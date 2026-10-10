import unittest
from tools.pl_pretrail_geometry_audit import violations
from tools.market_bot_replay import NullLogger, ReplayExecutionClient
from src.position.bot_full_engine import BotFullExitPosition


class GeometryTests(unittest.TestCase):
    def test_economic_floor_can_dominate_armed_trail(self):
        entry=77.80944750000002;atr=.030818774575268458
        cfg={'hard_stop':{'enabled':True,'stop_pct':1.5},'review_stop_pct':30,
            'breakeven':{'mode':'off'},'profit_lock':{'mode':'atr','economic_floor':{'enabled':True,'net_margin_pct':.05},
                'steps':[{'trigger_atr':5,'lock_atr':1.5},{'trigger_atr':8,'lock_atr':3},{'trigger_atr':12,'lock_atr':6}]},
            'trailing':{'mode':'atr','activation_atr':10,'gap_atr':5},'no_progress':{'enabled':False},
            'fees':{'enabled':True,'taker_fee_pct':.1,'use_bnb_discount':False}}
        client=ReplayExecutionClient(2.5)
        p=BotFullExitPosition(pair_id='geometry',symbol='SOLUSDT',entry_price=entry,quantity=20/entry,
            entry_order={},open_ts='2026-07-11T00:00:00Z',config=cfg,client=client,logger=NullLogger(),entry_atr=atr)
        client.current_price=78.12;p.on_tick(78.12,'2026-07-11T00:01:00Z')
        self.assertTrue(p.trailing_active)
        self.assertEqual(p.profit_lock_step,'PL1')
        self.assertEqual(p.stop_type,'profit_lock')
        self.assertGreater(p.profit_lock_stop,p.trailing_stop)
        self.assertAlmostEqual(p.profit_lock_stop,78.00397111875002)
        self.assertEqual(p.status,'OPEN')

    def test_unarmed_and_missing_pl_are_not_violations(self):
        data={'run':{'trades':[{'source_candle':1,'closed_ms':None,'exit_reason':'OPEN'}]},
              'details':[{'source_candle':1,'opened_ms':0,'entry_price':100,'entry_atr':1,'floor_events':[
                  {'trail_active':False},{'trail_active':True,'PL_stop':None}]}]}
        n,bad=violations(data)
        self.assertEqual(n,1);self.assertEqual(bad,[])
