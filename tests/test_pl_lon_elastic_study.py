import unittest
from types import SimpleNamespace
from tools.pl_lon_elastic_study import Study, elastic_floor, quantile
from tools.market_selection_study import MarketCandle
from tools.market_bot_replay import NullLogger, ReplayExecutionClient


class PlLonElasticTests(unittest.TestCase):
    def position(self,variant='C_025_ATR',context='LON'):
        review=SimpleNamespace(context_fields=lambda at:{'ema_context':context},
                               context=lambda at:{'ema_context':context})
        study=Study(review,variant);study.at=60000;study.boundary=120000
        client=ReplayExecutionClient(0)
        p=study.factory(pair_id='study',symbol='SOLUSDT',entry_price=100,quantity=.2,
              entry_order={},open_ts='1970-01-01T00:00:00+00:00',
              config={'hard_stop':{'enabled':True,'stop_pct':1.5},'breakeven':{'mode':'off'},
                      'profit_lock':{'mode':'atr','steps':[],
                          'economic_floor':{'enabled':True,'net_margin_pct':.05}},
                      'trailing':{'mode':'atr','trigger_atr':1000,'distance_atr':1},
                      'fees':{'enabled':True,'taker_fee_pct':.1}},
              client=client,logger=NullLogger(),entry_atr=1,atr_timeframe='1m',atr_period=14)
        p.profit_lock_stop=101;p.effective_stop=101;p.stop_type='profit_lock'
        return p,study,client

    def test_fixed_floors_and_original_pl_degenerate(self):
        p,_,_=self.position()
        self.assertAlmostEqual(elastic_floor(p,'C_025_ATR'),100.75)
        self.assertAlmostEqual(elastic_floor(p,'C_050_ATR'),100.5)
        self.assertAlmostEqual(elastic_floor(p,'A_NET_FLOOR'),100.25)
        self.assertTrue(101<=p.profit_lock_stop)

    def test_non_lon_keeps_pl(self):
        p,s,c=self.position(context='BUL');c.current_price=101
        p._close_at_market(101,'PROFIT_LOCK','1970-01-01T00:02:00Z',101)
        self.assertEqual(p.exit_reason,'PROFIT_LOCK');self.assertIsNone(p.elastic_started)

    def test_lon_touch_survives_but_same_segment_floor_closes(self):
        p,s,c=self.position()
        from tools.ge_replay_study import OpenPosition
        candle=MarketCandle(60000,119999,102,102,100,100,1,1)
        trades=[];s.processor([OpenPosition(p,c,0,20)],trades,candle,'HIGH_FIRST',.2)
        self.assertEqual(p.exit_reason,'PL_ELASTIC_FLOOR')
        self.assertAlmostEqual(p.exit_price,100.75)
        self.assertEqual(len(s.events),1)
        self.assertEqual(s.events[0]['causal_at_ms'],60000)

    def test_context_loss_closes_at_first_available_open(self):
        p,s,c=self.position();c.current_price=101
        p._close_at_market(101,'PROFIT_LOCK','1970-01-01T00:02:00Z',101)
        self.assertEqual(p.status,'OPEN')
        s.review.context=lambda at:{'ema_context':'BEA'}
        from tools.ge_replay_study import OpenPosition
        trades=[];s.processor([OpenPosition(p,c,0,20)],trades,
                             MarketCandle(120000,179999,101.3,110,90,95,1,1),'HIGH_FIRST',.2)
        self.assertEqual(p.exit_reason,'PL_ELASTIC_CONTEXT_LOST')
        self.assertEqual(p.exit_price,101.3);self.assertEqual(trades[0].closed_ms,120000)

    def test_trailing_remains_authoritative(self):
        p,s,c=self.position();c.current_price=101
        p._close_at_market(101,'PROFIT_LOCK','1970-01-01T00:02:00Z',101)
        p.trailing_stop=101.5;p._refresh_effective_stop()
        self.assertEqual(p.stop_type,'trailing');self.assertEqual(p.effective_stop,101.5)

    def test_repeated_open_low_is_not_removed_after_the_high(self):
        p,s,c=self.position(variant=None)
        p.profit_lock_stop=None;p.effective_stop=p.hard_stop_price
        p.profit_lock_atr_steps=[{'trigger_atr':1,'lock_atr':.5}]
        from tools.ge_replay_study import OpenPosition
        trades=[]
        s.processor([OpenPosition(p,c,0,20)],trades,
                    MarketCandle(60000,119999,100,102,100,101,1,1),'HIGH_FIRST',.2)
        self.assertEqual(p.exit_reason,'PROFIT_LOCK')
        self.assertEqual(p.exit_price,100.5)

    def test_percentile(self):
        self.assertEqual(quantile([0,10],.75),7.5)
        self.assertIsNone(quantile([],.95))

    def test_context_reconstruction_excludes_current_open_five_minute(self):
        from tools.be_off_cb_defensive_review import Review
        r=Review.__new__(Review)
        bars=[MarketCandle(i*300000,(i+1)*300000-1,100,102,99,101+i,1,1) for i in range(3)]
        r.candles={'5m':bars};r.five_boundaries=[c.boundary_ms for c in bars]
        r.context_cache={};r.config={}
        ctx=r.context(420000)
        self.assertEqual(ctx['latest_open_at_ms'],0)
        self.assertEqual(ctx['latest_closed_at_ms'],299999)
        self.assertLess(ctx['latest_closed_at_ms'],420000)

if __name__=='__main__':unittest.main()
