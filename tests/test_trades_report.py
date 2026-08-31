from __future__ import annotations

import unittest

from tools.trades_report import _market_stack


class TradesReportContextTests(unittest.TestCase):
    def test_ema_stack_context_uses_existing_bull_bear_mixed_definition(self) -> None:
        self.assertEqual(_market_stack({"tf_5m": {"ema20": 103, "ema50": 102, "ema100": 101}}), "BU")
        self.assertEqual(_market_stack({"tf_5m": {"ema20": 101, "ema50": 102, "ema100": 103}}), "BE")
        self.assertEqual(_market_stack({"tf_5m": {"ema20": 103, "ema50": 101, "ema100": 102}}), "MI")
        self.assertEqual(_market_stack(None), "n/a")
