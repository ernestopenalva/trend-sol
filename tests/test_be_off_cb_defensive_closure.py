import unittest
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_ge_replay_study import _config, _signal
from tools.be_off_cb_defensive_closure import compare, metrics, serialize
from tools.be_off_cb_fast_drop_audit import TrackingCircuitGuard
from tools.be_off_cb_fast_drop_systemic_replay import PostHsPause, run_systemic
from tools.ge_replay_study import run_universe
from tools.market_selection_study import MarketCandle

MINUTE=60000


class ClosureTests(unittest.TestCase):
    def test_pause_exactly_sixty_minutes_only_real_hs_and_extends(self):
        pause=PostHsPause(True)
        trades=[SimpleNamespace(exit_reason='FAST_DROP',closed_ms=1000)]
        pause.update(trades)
        self.assertFalse(pause.active(1000))
        trades.append(SimpleNamespace(exit_reason='HARD_STOP',closed_ms=2000))
        pause.update(trades)
        self.assertTrue(pause.active(2000))
        self.assertTrue(pause.active(3601999))
        self.assertFalse(pause.active(3602000))
        trades.append(SimpleNamespace(exit_reason='HARD_STOP',closed_ms=5000))
        pause.update(trades)
        pause.update(trades)
        self.assertEqual(pause.until,3605000)
        self.assertEqual(len(pause.intervals),2)

    def scenario(self):
        config=_config()
        config['risk']['hard_stop']['stop_pct']=1.5
        config['risk']['breakeven']={'mode':'off'}
        config['capital']['max_open_positions']=5
        config['entry']['spacing']={'enabled':False}
        signals=[_signal(1,100),_signal(2,99),_signal(3,98.5)]
        candles=[MarketCandle((i-1)*MINUTE,i*MINUTE-1,price,price,price,price,1,1)
                 for i,price in ((1,100),(2,99),(3,98.4),(4,98.4))]
        return config,signals,candles

    def run_model(self,pause=0,guard=None):
        config,signals,candles=self.scenario()
        args=dict(name='TEST',config=config,signals=signals,candles=candles,contexts=[],
                  start_ms=MINUTE,end_ms=4*MINUTE,path='HIGH_FIRST',spread_bps=0,
                  fast_enabled=False,hs_pause_minutes=pause)
        if guard is None:
            return run_systemic(**args),signals
        with patch('tools.be_off_cb_fast_drop_systemic_replay.TrackingCircuitGuard',return_value=guard):
            return run_systemic(**args),signals

    def test_pause_blocks_admission_not_existing_position(self):
        baseline,_=self.run_model()
        paused,_=self.run_model(60)
        self.assertEqual(paused.result.trades,baseline.result.trades)
        self.assertEqual(len(paused.result.entry_times),2)
        self.assertEqual(len(paused.result.open_positions),1)
        self.assertEqual(paused.result.open_positions[0].opened_ms,2*MINUTE)
        self.assertEqual(paused.admission_audit[-1]['decision'],'HS_PAUSE')
        self.assertEqual(paused.hs_pause_intervals,[(3*MINUTE,63*MINUTE)])

    def test_cb_is_evaluated_during_pause_and_has_primary_block_attribution(self):
        class Guard(TrackingCircuitGuard):
            def allows(self,at,result):
                super().allows(at,result)
                return at<3*MINUTE
        guard=Guard(100,20)
        run,_=self.run_model(60,guard)
        last=run.admission_audit[-1]
        self.assertTrue(last['hs_pause_active'])
        self.assertEqual(last['decision'],'CB')
        self.assertEqual(guard.cursor,len(run.result.trades))

    def test_disabled_variant_matches_original_control_engine(self):
        config,signals,candles=self.scenario()
        guard=TrackingCircuitGuard(config['capital']['operational_balance_usdt'],20)
        original=run_universe(name='ORIGINAL',lookback=0,config=config,signals=signals,
            execution_candles=candles,start_ms=MINUTE,end_ms=4*MINUTE,
            intrabar_path='HIGH_FIRST',round_trip_spread_bps=0,admission_guard=guard.allows)
        current,_=self.run_model()
        self.assertEqual([asdict(t) for t in original.trades],[asdict(t) for t in current.result.trades])
        self.assertEqual(original.entry_times,current.result.entry_times)
        self.assertEqual(guard.crisis_starts,current.guard.crisis_starts)

    def test_metrics_realized_dd_and_exact_source_attribution(self):
        run,signals=self.run_model(60)
        raw=serialize(run,signals,20)
        raw['trades']=[dict(source_candle=i,closed_ms=(i+1)*MINUTE,net_usd=v,exit_reason='HARD_STOP',opened_ms=0)
                       for i,v in enumerate((1,-.4,-.9,.2))]
        summary=metrics(raw,'ALL')
        self.assertAlmostEqual(summary['net'],-.1)
        self.assertAlmostEqual(summary['dd'],1.3)
        self.assertAlmostEqual(summary['pf'],1.2/1.3)
        variant={**raw,'trades':[dict(raw['trades'][0],net_usd=.8),raw['trades'][3],
                                dict(raw['trades'][1],source_candle=99,net_usd=.1)]}
        paired=compare(raw,variant,'ALL')
        self.assertEqual(paired['common'],2)
        self.assertEqual(paired['control_only'],2)
        self.assertEqual(paired['variant_only'],1)
        self.assertAlmostEqual(paired['delta'],1.2)

    def test_no_alternative_pause_duration(self):
        with self.assertRaises(ValueError):
            self.run_model(30)

    def test_fast_exit_frees_slot_and_changes_later_admission(self):
        config=_config()
        config['capital']['max_open_positions']=1
        config['risk']['hard_stop']['stop_pct']=1.5
        config['risk']['breakeven']={'mode':'off'}
        signals=[_signal(6,100),_signal(7,99.5)]
        candles=[MarketCandle((i-1)*MINUTE,i*MINUTE-1,100,100,price,price,1,1)
                 for i,price in ((2,100),(6,100),(7,99.5))]
        args=dict(config=config,signals=signals,candles=candles,contexts=[(0,'SHO','BE-')],
                  start_ms=6*MINUTE,end_ms=7*MINUTE,path='LOW_FIRST',spread_bps=0)
        control=run_systemic(name='CONTROL',fast_enabled=False,**args)
        fast=run_systemic(name='FAST',fast_enabled=True,**args)
        self.assertEqual(len(control.result.entry_times),1)
        self.assertEqual(len(fast.result.entry_times),2)
        self.assertEqual(fast.result.trades[0].exit_reason,'FAST_DROP')
        self.assertAlmostEqual(fast.guard.equity,99.86)
        self.assertEqual(fast.guard.cursor,1)
        self.assertEqual(control.admission_audit[-1]['decision'],'CAPACITY')
        self.assertEqual(fast.admission_audit[-1]['decision'],'ADMITTED')


if __name__=='__main__':
    unittest.main()
