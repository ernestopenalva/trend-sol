import unittest
from types import SimpleNamespace

from src.monitor.market_context import MarketContextEngine
from tools.be_off_cb_exit_context_study import (
    MINUTE_MS, MONTHS, aggregate, exit_context, markdown, pattern_status, posterior, summarize,
)
from tools.market_selection_study import MarketCandle


def candle(at, price=100, high=None, low=None):
    return MarketCandle(at, at + MINUTE_MS - 1, price,
                        high if high is not None else price,
                        low if low is not None else price, price, 10, 1)


class ExitContextStudyTests(unittest.TestCase):
    def trade(self):
        return SimpleNamespace(closed_ms=100 * MINUTE_MS, entry_price=100, exit_price=101)

    def test_context_excludes_5m_closing_inside_exit_minute(self):
        candles = [MarketCandle(i*5*MINUTE_MS, (i+1)*5*MINUTE_MS-1,
                               100+i/100, 101+i/100, 99+i/100, 100+i/100, 10, 1)
                   for i in range(302)]
        boundaries = [c.boundary_ms for c in candles]
        closed_ms = 301*5*MINUTE_MS
        ctx = exit_context(candles, boundaries, closed_ms, {})
        self.assertEqual(ctx['latest_open_at_ms'], 299*5*MINUTE_MS)
        self.assertLessEqual(ctx['latest_closed_at_ms'], closed_ms - MINUTE_MS)
        eligible = candles[:300]
        engine = MarketContextEngine.__new__(MarketContextEngine)
        engine.settings = {}
        expected = engine._timeframe_snapshot([
            SimpleNamespace(open_time=c.open_time_ms, close_time=c.close_time_ms,
                            high=c.high, low=c.low, close=c.close, volume=10, closed=True)
            for c in eligible], '5m')
        for key in ('ema_context', 'macd_context', 'macd_line', 'macd_signal', 'macd_histogram',
                    'ema50_direction', 'ema100_direction', 'ema200_direction'):
            self.assertEqual(ctx[key], expected[key])
        # Future candle values cannot change the eligible context.
        candles[300] = candle(candles[300].open_time_ms, 1000)
        self.assertEqual(exit_context(candles, boundaries, closed_ms, {})['macd_line'], ctx['macd_line'])

    def test_post_window_excludes_exit_candle_and_includes_full_horizon(self):
        t = self.trade()
        index = {t.closed_ms+k*MINUTE_MS: candle(t.closed_ms+k*MINUTE_MS, 101, 102, 100)
                 for k in range(60)}
        index[t.closed_ms-MINUTE_MS] = candle(t.closed_ms-MINUTE_MS, 100, 200, 50)
        post = posterior(t, index, 20)
        self.assertAlmostEqual(post['5']['favorable_pct'], (102/101-1)*100)
        self.assertAlmostEqual(post['5']['adverse_pct'], (100/101-1)*100)
        self.assertFalse(post['60']['original_hs_touched'])
        index[t.closed_ms+14*MINUTE_MS] = candle(t.closed_ms+14*MINUTE_MS, 100, 102, 98)
        post = posterior(t, index, 20)
        self.assertFalse(post['5']['original_hs_touched'])
        self.assertTrue(post['15']['original_hs_touched'])
        self.assertEqual(post['15']['first_original_hs_touch_min'], 15)
        self.assertNotIn('would_be_hs', post['15'])

    def test_missing_future_minutes_not_silently_partial(self):
        t = self.trade()
        index = {t.closed_ms+k*MINUTE_MS: candle(t.closed_ms+k*MINUTE_MS) for k in range(5)}
        post = posterior(t, index, 20)
        self.assertIsNotNone(post['5'])
        self.assertIsNone(post['15'])

    def test_separate_paths_reasons_and_exit_month(self):
        t = self.trade()
        post = posterior(t, {t.closed_ms+k*MINUTE_MS: candle(t.closed_ms+k*MINUTE_MS) for k in range(60)}, 20)
        rows = [dict(path=p, exit_reason=r, month=m, ema_context='SHO', macd_context='BE-',
                     histogram_state='NEGATIVE_FALLING', post=post)
                for p in ('HIGH_FIRST', 'LOW_FIRST') for r in ('PROFIT_LOCK', 'TRAILING') for m in MONTHS]
        groups = aggregate(rows)
        for g in groups:
            if g['dimension'] == 'overall':
                self.assertEqual(g['aggregate']['n'], 4)
                self.assertEqual([s['n'] for s in g['months'].values()], [1]*4)
                self.assertEqual(g['status'], 'amostra insuficiente')
        report = markdown(groups, {})
        for token in ('HIGH_FIRST', 'LOW_FIRST', 'PROFIT_LOCK', 'TRAILING', *MONTHS, 'NÃO reexecuta'):
            self.assertIn(token, report)

    def test_robustness_never_calls_one_month_consistent(self):
        self.assertEqual(pattern_status([{'n':100}, {'n':0}, {'n':0}, {'n':0}]), 'amostra insuficiente')
        months = [{'n':10, 'windows':{'60':{'favorable_pct':1, 'adverse_pct':-.5}}} for _ in MONTHS]
        self.assertEqual(pattern_status(months), 'consistente')
        months[0]['windows']['60']['adverse_pct'] = -2
        self.assertEqual(pattern_status(months), 'fraco')
        months[1]['windows']['60']['adverse_pct'] = -2
        self.assertEqual(pattern_status(months), 'contraditório')


if __name__ == '__main__':
    unittest.main()
