import unittest
from tools.friction_pl_trail_study import accounting, PLOnly, rank_effect, stats, BotFullExitPosition
from tools.market_bot_replay import NullLogger, ReplayExecutionClient


class FrictionPLTests(unittest.TestCase):
    def test_exact_cost_identity(self):
        t={'entry_price':100.025,'exit_price':100.97475,'gross_pct':(100.97475/100.025-1)*100}
        t['net_pct']=t['gross_pct']-.2;t['net_usd']=20*t['net_pct']/100
        a=accounting(t,20,5)
        self.assertAlmostEqual(a['gross']-a['spread']-a['fees'],t['net_usd'],places=12)
        self.assertAlmostEqual(a['gross'],20/100.025,places=12)

    def test_fees_remark_only(self):
        t={'entry_price':100,'exit_price':101,'gross_pct':1,'net_pct':.8,'net_usd':.16}
        a=accounting(t,20,5);b=accounting(t,20,5,.1)
        self.assertAlmostEqual(b['net']-a['net'],.02)
        self.assertEqual(a['spread'],b['spread'])

    def test_pl_engine_does_not_exit_on_unarmed_level(self):
        cfg={'hard_stop':{'enabled':True,'stop_pct':1.5},'review_stop_pct':30,
             'breakeven':{'mode':'off'},'profit_lock':{'mode':'atr','economic_floor':{'enabled':False},
                 'steps':[{'trigger_atr':5,'lock_atr':1.5}]},
             'trailing':{'mode':'atr','activation_atr':10,'gap_atr':5},'no_progress':{'enabled':False}}
        client=ReplayExecutionClient(0)
        p=PLOnly(pair_id='cf-test',symbol='SOLUSDT',entry_price=100,quantity=.2,entry_order={},
                 open_ts='2026-06-01T00:00:00Z',config=cfg,client=client,logger=NullLogger(),entry_atr=1)
        client.current_price=102;p.on_tick(102,'2026-06-01T00:01:00Z')
        client.current_price=101.5;p.on_tick(101.5,'2026-06-01T00:02:00Z')
        self.assertEqual(p.status,'OPEN')
        client.current_price=110;p.on_tick(110,'2026-06-01T00:03:00Z')
        self.assertFalse(p.trailing_active)
        client.current_price=101.5;p.on_tick(101.5,'2026-06-01T00:04:00Z')
        self.assertEqual(p.exit_reason,'PROFIT_LOCK')

    def test_descriptive_rank_and_quantiles(self):
        self.assertEqual(rank_effect([1,2],[1,2])['cliff_delta'],0)
        self.assertEqual(stats([0,1,2,3])['p25'],.75)

    def test_last_pl_is_a_fixed_ceiling_not_forecast(self):
        cfg={'hard_stop':{'enabled':True,'stop_pct':1.5},'review_stop_pct':30,
             'breakeven':{'mode':'off'},'profit_lock':{'mode':'atr','economic_floor':{'enabled':False},
                 'steps':[{'trigger_atr':5,'lock_atr':1.5},{'trigger_atr':8,'lock_atr':3},{'trigger_atr':12,'lock_atr':6}]},
             'trailing':{'mode':'atr','activation_atr':10,'gap_atr':5},'no_progress':{'enabled':False}}
        ps=[]
        for klass in [BotFullExitPosition,PLOnly]:
            client=ReplayExecutionClient(0)
            p=klass(pair_id='ceiling-'+klass.__name__,symbol='SOLUSDT',entry_price=100,quantity=.2,entry_order={},
                    open_ts='2026-06-01T00:00:00Z',config=cfg,client=client,logger=NullLogger(),entry_atr=1)
            ps.append((p,client))
            client.current_price=112;p.on_tick(112,'2026-06-01T00:01:00Z')
            self.assertEqual(p.profit_lock_step,'PL3')
            self.assertEqual(p.profit_lock_stop,106)
        ps[0][1].current_price=107;ps[0][0].on_tick(107,'2026-06-01T00:02:00Z')
        self.assertEqual(ps[0][0].exit_price,107)
        ps[1][1].current_price=120;ps[1][0].on_tick(120,'2026-06-01T00:03:00Z')
        self.assertEqual(ps[1][0].profit_lock_stop,106)
        ps[1][1].current_price=106;ps[1][0].on_tick(106,'2026-06-01T00:04:00Z')
        self.assertAlmostEqual(ps[1][0].exit_price,106)
