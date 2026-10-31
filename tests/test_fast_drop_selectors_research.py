"""Research harness checks; no operational persistence or exchange access."""
import unittest
from tools.fast_drop_selectors_study import OUT, read, START, MINUTE, iso, policy, ARMS
from tools.fast_drop_selectors_engine import memory, on_point, run, engine_checks
from src.monitor.entry_engine import EntrySignal
from tools.market_selection_study import MarketCandle
from dataclasses import asdict

class ResearchChecks(unittest.TestCase):
    def make(self):
        cfg=read(OUT/'config.json')
        obj=memory('be_off_cb',cfg,0)
        obj.latest_market_context={'tf_5m':{}}
        self.assertTrue(obj._process_signal(EntrySignal('SOLUSDT',100.,iso(START),START-MINUTE,.2,'1m',14)))
        return obj.positions[0]

    def test_missing_reference_retries_but_blocked_valid_attempt_does_not(self):
        p=self.make();attempts=[]
        on_point(p,99.4,100.,START,None,{'ema_context':'SHO'},True,attempts)
        self.assertFalse(p.fast_drop_evaluated)
        on_point(p,99.3,99.4,START+1,100.,{'ema_context':'SHO'},True,attempts)
        self.assertEqual(p.exit_reason,'FAST_DROP')
        self.assertAlmostEqual(p.exit_price,99.3)
        self.assertEqual([x['decision'] for x in attempts],['NOT_EVALUABLE','TRIGGER'])

    def test_equal_stop_precedes_fast(self):
        p=self.make();p.hard_stop_price=99.5;p._refresh_effective_stop();attempts=[]
        on_point(p,99.4,100.,START,100.,{'ema_context':'SHO'},True,attempts)
        self.assertEqual(p.exit_reason,'HARD_STOP')
        self.assertEqual(attempts[0]['reasons'],['NORMAL_STOP_PRECEDES'])

    def test_hard_stop_gap_costs(self):
        p=self.make();p.client.bps=10
        on_point(p,98.,None,START,100.,{'ema_context':'LON'},False,[])
        self.assertEqual(p.exit_reason,'HARD_STOP')
        self.assertAlmostEqual(p.exit_price,98.*.999)

    def test_selector_methods(self):
        candles=[MarketCandle(START-2*MINUTE,START-MINUTE-1,99.,101.,98.,100.,1.,1),MarketCandle(START-MINUTE,START-1,100.,102.,99.,101.,1.,1)]
        c={'ema_context':'LON','macd_context':'BU+','macd_histogram':.2,'macd_histogram_previous':.1,'latest_closed_at_ms':START-1}
        self.assertTrue(all(policy(a,c,START,candles) for a in ARMS))
        c['macd_context']='BU-'
        self.assertTrue(policy('be_off_cb',c,START,candles))
        self.assertFalse(any(policy(a,c,START,candles) for a in ARMS[1:]))
        c['macd_context']='BU+';c['latest_closed_at_ms']=START+1
        self.assertFalse(policy('ema_macd_hist_1m',c,START,candles))

    def test_dedicated_engine_checks(self):
        engine_checks()

if __name__=='__main__':unittest.main()
