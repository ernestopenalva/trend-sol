import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.indicators.indicators import ema
from src.logging_utils import JsonlLogger
from src.monitor.entry_engine import Candle, EntrySignal
from src.monitor.forward_experiment_shadows import EmaMacdHist1mShadow, PolicyShadow
from src.monitor.market_context import MarketContextEngine
from tests.test_circuit_breaker_shadow import _config


STAMP = int(datetime.fromisoformat('2026-10-01T00:10:00+00:00').timestamp()*1000)


class Hist1mTests(unittest.TestCase):
    def make(self, root):
        config = _config()
        config['instrumentation']['ema_macd_hist_1m_shadow'] = {
            'enabled': True, 'accept_new_entries': True, 'initial_capital_usdt': 100,
            'max_open_positions': 5, 'state_file': 'hist.json',
            'ledger_file': 'hist.jsonl', 'events_file': 'hist_events.jsonl'}
        return EmaMacdHist1mShadow(root, config, JsonlLogger(root, config), None,
                                   cohort_started_at='2026-10-01T00:00:00+00:00'), config

    def candle(self, index, close=100., opening=99., closed=True):
        return {'x': closed, 't': STAMP + index*60000, 'T': STAMP+(index+1)*60000-1,
                'o': opening, 'h': max(opening, close), 'l': min(opening, close), 'c': close}

    def snapshot(self, hist=.2, previous=.1, context='BUL'):
        return {'tf_5m': {'ema_context': context, 'macd_context': 'BU+',
            'latest_open_at_ms': STAMP-300000, 'latest_closed_at_ms': STAMP-1,
            'macd_line': .4, 'macd_line_previous': .3, 'macd_signal': .2,
            'macd_signal_previous': .2, 'macd_histogram': hist, 'macd_histogram_previous': previous}}

    def signal(self):
        return EntrySignal('SOLUSDT', 100., '2026-10-01T00:10:00+00:00', STAMP-60000, .2, '1m', 14)

    def test_layers_and_precedence(self):
        for hist, prior, previous_close, current_close, opening, context, expected in (
            (.2,.1,99.,100.,99.,'BUL',True),
            (.1,.2,99.,100.,99.,'BUL',False),
            (-.1,-.2,99.,100.,99.,'BUL',False),
            (.2,.1,100.,100.,99.,'BUL',False),
            (.2,.1,101.,100.,99.,'BUL',False),
            (.2,.1,99.,100.,101.,'BUL',False),
            (.2,.1,99.,100.,99.,'SHO',False)):
            with self.subTest(case=(hist,prior,previous_close,current_close,opening,context)), TemporaryDirectory() as tmp:
                shadow, _ = self.make(Path(tmp))
                shadow.on_closed_1m(self.candle(-2, previous_close))
                shadow.on_closed_1m(self.candle(-1, current_close, opening))
                result = shadow.on_approved_real_a_signal(self.signal(), self.snapshot(hist, prior, context))
                self.assertEqual(bool(result), expected)
                audit = shadow.audit_events[-1]
                self.assertEqual(audit['event'], 'ADMISSION_FILTERS')
                self.assertEqual(audit['final_decision'], 'admitted' if expected else 'blocked')
                self.assertEqual(audit['one_minute_current']['close_time'], STAMP-1)
                if context == 'SHO':
                    self.assertTrue(audit['filter_reason'].startswith('ENTRY_BLOCKED_EMA_MACD'))

    def test_open_and_future_1m_are_not_used(self):
        with TemporaryDirectory() as tmp:
            shadow, _ = self.make(Path(tmp))
            shadow.on_closed_1m(self.candle(-2, 99.))
            shadow.on_closed_1m(self.candle(-1, 100.))
            shadow.on_closed_1m(self.candle(0, 1., closed=False))
            shadow.on_closed_1m(self.candle(1, 1.))
            self.assertTrue(shadow.on_approved_real_a_signal(self.signal(), self.snapshot()))
            self.assertEqual(shadow.audit_events[-1]['one_minute_current']['close'], 100.)

    def test_future_5m_snapshot_fails_closed(self):
        with TemporaryDirectory() as tmp:
            shadow, _ = self.make(Path(tmp))
            shadow.on_closed_1m(self.candle(-2, 99.)); shadow.on_closed_1m(self.candle(-1, 100.))
            snapshot = self.snapshot()
            snapshot['tf_5m']['latest_closed_at_ms'] = STAMP+299999
            self.assertFalse(shadow.on_approved_real_a_signal(self.signal(), snapshot))
            self.assertEqual(shadow.audit_events[-1]['filter_reason'], 'ENTRY_BLOCKED_HISTOGRAM')

    def test_shared_snapshot_excludes_open_5m_and_aligns_signal_histogram(self):
        candles = [Candle(i*300000, (i+1)*300000-1, 100., 102., 99., 100.+i*.1, 1., True) for i in range(210)]
        engine = MarketContextEngine(SimpleNamespace(config={}), {})
        expected = engine._timeframe_snapshot(candles, '5m')
        result = engine._timeframe_snapshot(candles+[Candle(210*300000,211*300000-1,1.,1.,1.,1.,1.,False)], '5m')
        self.assertEqual(expected, result)
        closes = [c.close for c in candles]
        line = [f-s for f,s in zip(ema(closes,12)[25:],ema(closes,26)[25:])]
        signals = ema(line,9)
        self.assertAlmostEqual(result['macd_signal'], signals[-1])
        self.assertAlmostEqual(result['macd_histogram'], line[-1]-signals[-1])
        self.assertAlmostEqual(result['macd_histogram_previous'], line[-2]-signals[-2])

    def test_independence_and_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp); shadow, config = self.make(root)
            shadow.on_closed_1m(self.candle(-2, 99.)); shadow.on_closed_1m(self.candle(-1, 100.))
            self.assertTrue(shadow.on_approved_real_a_signal(self.signal(), self.snapshot()))
            config['instrumentation']['base_ema'] = {'enabled':True, 'accept_new_entries':True,
                'state_file':'base.json', 'ledger_file':'base.jsonl', 'events_file':'base_events.jsonl'}
            base = PolicyShadow(root, config, JsonlLogger(root,config), None, settings_key='base_ema',
                strategy='BE_OFF_CB_EMA_MACD_SHADOW', pair_prefix='base', policy='EMA_MACD',
                cohort_started_at='2026-10-01T00:00:00+00:00')
            self.assertTrue(base.on_approved_real_a_signal(self.signal(), self.snapshot()))
            restored, _ = self.make(root)
            self.assertEqual(restored.tactical_candles, shadow.tactical_candles)
            for field in ('pair_id', 'entry_price', 'quantity', 'open_ts', 'effective_stop',
                          'market_context_entry', 'shadow_kind', 'phantom'):
                self.assertEqual(getattr(restored.open_positions[0], field), getattr(shadow.open_positions[0], field))
            self.assertEqual(restored.clock.to_state(), shadow.clock.to_state())
            self.assertEqual(restored.audit_events, shadow.audit_events)
            restored.on_tick(98., '2026-10-01T00:11:01+00:00')
            self.assertEqual(len(restored.open_positions), 0)
            self.assertEqual(len(base.open_positions), 1)
            restored.on_tick(98., '2026-10-01T00:12:01+00:00')
            self.assertLess(restored.equity, base.equity)


if __name__ == '__main__':
    unittest.main()
