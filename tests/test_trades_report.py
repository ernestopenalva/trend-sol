from __future__ import annotations

import unittest

from tools.trades_report import _market_stack, _store_h2_match


class TradesReportContextTests(unittest.TestCase):
    def test_ema_stack_context_uses_existing_bull_bear_mixed_definition(self) -> None:
        self.assertEqual(_market_stack({"tf_5m": {"ema20": 103, "ema50": 102, "ema100": 101}}), "BU")
        self.assertEqual(_market_stack({"tf_5m": {"ema20": 101, "ema50": 102, "ema100": 103}}), "BE")
        self.assertEqual(_market_stack({"tf_5m": {"ema20": 103, "ema50": 101, "ema100": 102}}), "MI")
        self.assertEqual(_market_stack(None), "n/a")

    def test_h2_match_uses_source_candle_and_persisted_effective_notional(self) -> None:
        matches = {}
        _store_h2_match(
            matches,
            {
                "pair_id": "h2-pair",
                "source_candle_open_time": 1_234_567_890_000,
                "position_notional_usdt": 20,
                "h2": {"effective_notional": 6.6667, "h2_step": 3, "uncovered_count": 2},
            },
            status="CLOSED",
        )

        self.assertEqual(matches[1_234_567_890_000]["h2_effective_notional_usdt"], 6.6667)
        self.assertEqual(matches[1_234_567_890_000]["h2_step"], 3)
        self.assertEqual(matches[1_234_567_890_000]["h2_uncovered_count"], 2)
        self.assertEqual(matches[1_234_567_890_000]["h2_status"], "CLOSED")
