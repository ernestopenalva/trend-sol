import unittest
from types import SimpleNamespace

from tools.hs_bull_elastic_diagnostic import ReplayAdapter, continuation, quantiles
from tools.market_bot_replay import NullLogger, ReplayExecutionClient
from tools.market_selection_study import MarketCandle
from tools.ge_replay_study import OpenPosition


class HsBullDiagnosticTests(unittest.TestCase):
    def make(self, context='LON', elastic=True):
        cfg = {'hard_stop': {'enabled': True, 'stop_pct': 1.5}, 'breakeven': {'mode': 'off'},
               'profit_lock': {'mode': 'atr', 'steps': []},
               'trailing': {'mode': 'atr', 'trigger_atr': 1000, 'distance_atr': 1},
               'fees': {'enabled': False}}
        snapshot = {'ema_context': context, 'close': 98, 'latest_closed_at_ms': 299999}
        review = SimpleNamespace(context=lambda at: snapshot)
        study = ReplayAdapter(review, elastic)
        client = ReplayExecutionClient(0)
        p = study.factory(pair_id='test', symbol='SOLUSDT', entry_price=100, quantity=.2,
            entry_order={}, open_ts='1970-01-01T00:00:00+00:00', config=cfg,
            client=client, logger=NullLogger(), entry_atr=1, atr_timeframe='1m', atr_period=14)
        return study, p, client, snapshot

    def test_bul_never_activates_current_rule(self):
        s,p,c,_ = self.make('BUL')
        trades=[]
        s.processor([OpenPosition(p,c,0,20)],trades,MarketCandle(300000,359999,100,100,98,98,1,1),'HIGH_FIRST',0)
        self.assertEqual(p.exit_reason,'HARD_STOP')
        self.assertFalse(s.events)
        self.assertEqual(len(s.captures),1)
        self.assertAlmostEqual(s.captures[0]['trigger_price'],98.5)
        self.assertEqual(s.captures[0]['state']['status'],'OPEN')

    def test_lon_suppresses_hs_and_processes_remaining_segment(self):
        s,p,c,_ = self.make()
        s.processor([OpenPosition(p,c,0,20)],[],MarketCandle(300000,359999,100,100,97,97,1,1),'HIGH_FIRST',0)
        self.assertEqual(p.status,'OPEN')
        self.assertTrue(p.hs_elastic)
        self.assertIsNone(p.hard_stop_price)
        self.assertEqual(s.events[0]['event'],'HS_ELASTIC_STARTED')
        self.assertEqual(c.current_price,97)

    def test_context_recheck_only_new_five_minute_close(self):
        s,p,c,snap = self.make()
        p.hs_elastic=True;p.hard_stop_price=None;p._refresh_effective_stop()
        s.context_transition(p,snap)
        snap['ema_context']='BUL'
        s.context_transition(p,snap)
        self.assertEqual(p.status,'OPEN')
        snap['latest_closed_at_ms']=599999
        s.context_transition(p,snap)
        self.assertEqual(p.exit_reason,'HARD_STOP_ELASTIC_CONTEXT_LOST')
        self.assertEqual(p.exit_price,98)

    def test_loss_of_lon_above_hs_rearms_normal_hs(self):
        s,p,c,snap=self.make('BUL')
        p.hs_elastic=True;p.hard_stop_price=None;p._refresh_effective_stop()
        snap['close']=99
        s.context_transition(p,snap)
        self.assertFalse(p.hs_elastic)
        self.assertEqual(p.hard_stop_price,98.5)
        self.assertEqual(p.status,'OPEN')

    def test_adapter_context_loss_matches_actual_shadow_transition(self):
        from src.monitor.forward_experiment_shadows import ExperimentalRiskShadow
        from unittest.mock import patch
        s,p,c,snap=self.make('BUL')
        p.hs_elastic=True;p.hard_stop_price=None;p._refresh_effective_stop()
        _,q,_,_=self.make('BUL')
        q.hs_elastic=True;q.hard_stop_price=None;q._refresh_effective_stop()
        arm=ExperimentalRiskShadow.__new__(ExperimentalRiskShadow)
        arm.enabled=True;arm.experiment='HS_BULL_ELASTIC';arm.latest_market_context={}
        arm.positions=[q];arm._remember_exit_context=lambda *a:None
        arm._context_fields=lambda:snap
        arm._save_state=lambda:None;arm._finish_elastic_clock=lambda *a:None
        arm._event=lambda *a,**k:None
        def close(position,price,stamp,reason,original):
            position.client.current_price=price
            position._cb_market_ts=stamp
            position._close_at_market(price,reason,stamp,original)
        arm._close_and_record=close
        with patch('src.monitor.forward_experiment_shadows.observe'):
            arm.on_closed_5m({'tf_5m':snap})
        s.context_transition(p,snap)
        self.assertEqual(p.exit_reason,q.exit_reason)
        self.assertEqual(p.exit_price,q.exit_price)

    def test_causal_context_has_no_current_open_five_minute(self):
        from tools.be_off_cb_defensive_review import Review
        r=Review.__new__(Review)
        bars=[MarketCandle(i*300000,(i+1)*300000-1,100,102,99,100+i,1,1) for i in range(3)]
        r.candles={'5m':bars};r.five_boundaries=[c.boundary_ms for c in bars]
        r.context_cache={};r.config={}
        snap=r.context(420000)
        self.assertEqual(snap['latest_open_at_ms'],0)
        self.assertLess(snap['latest_closed_at_ms'],420000)

    def test_same_minute_repeated_open_after_high_not_discarded(self):
        s,p,c,_=self.make('BUL',False)
        p.profit_lock_atr_steps=[{'trigger_atr':1,'lock_atr':.5}]
        p.config['profit_lock']['steps']=[{'trigger_atr':1,'lock_atr':.5}]
        s.processor([OpenPosition(p,c,0,20)],[],MarketCandle(300000,359999,100,102,100,101,1,1),'HIGH_FIRST',0)
        self.assertEqual(p.exit_reason,'PROFIT_LOCK')
        self.assertAlmostEqual(p.exit_price,100.5)

    def test_quantiles_do_not_replace_distribution_with_mean(self):
        q=quantiles([0,10])
        self.assertEqual(q['p25'],2.5)
        self.assertEqual(q['p90'],9)
        self.assertEqual(quantiles([])['n'],0)

    def test_continuation_retains_state_and_censors_missing_future(self):
        s,p,c,snap=self.make('BUL',False)
        snap.update(ema50=100,ema100=99,ema200=98)
        s.processor([OpenPosition(p,c,0,20)],[],MarketCandle(300000,359999,100,100,98,98,1,1),'HIGH_FIRST',0)
        event=s.captures[0]
        cfg={'risk':p.config,'capital':{'max_open_positions':5}}
        review=SimpleNamespace(config=cfg,spread=0,fees=0,notional=20,index={},control_trades=[])
        result=continuation(event,review,{'net_usd':-.3})
        self.assertEqual(result['category'],'C_AMBIGUO')
        self.assertEqual(result['actual_exit'],'CENSORED')
        self.assertIsNone(result['delta_usd'])
        self.assertTrue(all(not w['complete'] for w in result['windows']))
        self.assertIsNone(result['windows'][0]['recovery_times_min']['HS'])
        self.assertEqual(event['state']['status'],'OPEN')

    def test_pl_arming_is_not_pl_execution_in_continuation(self):
        s,p,c,snap=self.make('BUL',False)
        snap.update(ema50=100,ema100=99,ema200=98)
        p.profit_lock_atr_steps=[{'trigger_atr':1,'lock_atr':.5}]
        p.config['profit_lock']['steps']=[{'trigger_atr':1,'lock_atr':.5}]
        s.processor([OpenPosition(p,c,0,20)],[],MarketCandle(300000,359999,100,100,98,98,1,1),'HIGH_FIRST',0)
        cfg={'risk':p.config,'capital':{'max_open_positions':5}}
        index={420000:MarketCandle(360000,419999,98,102,98,102,1,1)}
        # LOW_FIRST: rises only after the low, so it arms but never executes PL.
        event=s.captures[0];event['path']='LOW_FIRST'
        review=SimpleNamespace(config=cfg,spread=0,fees=0,notional=20,index=index,control_trades=[])
        result=continuation(event,review,{'net_usd':-.3})
        self.assertIsNotNone(result['PL_armed_ms'])
        self.assertEqual(result['actual_exit'],'CENSORED')
        self.assertIsNone(result['actual_net'])


if __name__=='__main__':unittest.main()
