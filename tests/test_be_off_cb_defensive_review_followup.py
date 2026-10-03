import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tools.be_off_cb_defensive_review_followup import after_hs, later_fast
from tools.market_selection_study import MarketCandle


class FollowupTests(unittest.TestCase):
    def test_after_hs_uses_strictly_prior_close_and_one_hour_only(self):
        rows = [dict(opened_ms=0, closed_ms=100, exit_reason='HARD_STOP', net_usd=-1),
                dict(opened_ms=100, closed_ms=200, exit_reason='TRAILING', net_usd=1),
                dict(opened_ms=101, closed_ms=300, exit_reason='PROFIT_LOCK', net_usd=.2),
                dict(opened_ms=3600101, closed_ms=4000000, exit_reason='TRAILING', net_usd=.3)]
        result = after_hs(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['opened_ms'], 101)
        self.assertEqual(result[0]['delta'], -.2)

    def scenario(self):
        candle = MarketCandle(600000, 659999, 99.4, 99.4, 99.3, 99.3, 10, 1)
        reference = MarketCandle(300000, 359999, 100.1, 100.1, 100.1, 100.1, 10, 1)
        review = SimpleNamespace(minute=[candle], opens=[600000], index={360000: reference},
                                 end=1000000, notional=20, spread=0, fees=0,
                                 context=lambda at: {'ema_context': 'SHO'},
                                 context_fields=lambda at: {'snapshot_at': at})
        trade = dict(entry_price=100, opened_ms=0, closed_ms=720000, net_usd=-.3,
                     exit_reason='HARD_STOP', fast_audit={'crossed_ms': 600000}, fast_eligible=False)
        return trade, review

    def test_recheck_uses_observed_price_and_causal_snapshot(self):
        trade, review = self.scenario()
        with patch('tools.be_off_cb_defensive_review_followup.fast_drop_allowed', return_value=True):
            result = later_fast(trade, review)
        self.assertEqual(result['quote'], 99.3)
        self.assertEqual(result['target'], 99.5)
        self.assertEqual(result['context']['snapshot_at'], 600000)
        self.assertAlmostEqual(result['delta'], .16)

    def test_no_duplicate_original_fast_and_no_post_exit_price(self):
        trade, review = self.scenario()
        trade['fast_eligible'] = True
        self.assertIsNone(later_fast(trade, review))
        trade['fast_eligible'] = False
        trade['closed_ms'] = 660000
        self.assertIsNone(later_fast(trade, review))

    def test_unavailable_or_unclosed_reference_rejected(self):
        trade, review = self.scenario()
        review.index = {}
        self.assertIsNone(later_fast(trade, review))
        review.index = {360000: SimpleNamespace(close_time_ms=600000, close=100.1)}
        self.assertIsNone(later_fast(trade, review))


if __name__ == '__main__':
    unittest.main()
