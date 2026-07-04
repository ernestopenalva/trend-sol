from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.position.bot_full_engine import BotFullExitPosition


class FakeClient:
    def __init__(self) -> None:
        self.sells = []

    def market_sell(self, symbol: str, quantity: float, client_order_id: str):
        self.sells.append((symbol, quantity, client_order_id))
        return {
            "orderId": 123,
            "clientOrderId": client_order_id,
            "executedQty": str(quantity),
            "cummulativeQuoteQty": str(quantity * 106),
            "fills": [{"price": "106", "qty": str(quantity), "commission": "0.01"}],
        }


class BotFullEngineTests(unittest.TestCase):
    def test_breakeven_and_trailing_close(self) -> None:
        with TemporaryDirectory() as tmp:
            logger = JsonlLogger(
                Path(tmp),
                {
                    "logging": {
                        "trade_log": "logs/trades.jsonl",
                        "decision_log": "logs/decisions.jsonl",
                        "system_log": "logs/system.log",
                    }
                },
            )
            client = FakeClient()
            position = BotFullExitPosition(
                pair_id="pair",
                symbol="SOLUSDT",
                entry_price=100.0,
                quantity=1.0,
                entry_order={},
                open_ts="2026-07-04T00:00:00+00:00",
                config={
                    "stop_loss_pct": 30,
                    "breakeven": [
                        {"trigger_pct": 5, "stop_to_pct": 1},
                        {"trigger_pct": 6, "stop_to_pct": 3},
                        {"trigger_pct": 10, "stop_to_pct": 5},
                    ],
                    "trailing": {"activation_pct": 10, "gap_pct": 4},
                },
                client=client,  # type: ignore[arg-type]
                logger=logger,
            )

            self.assertIsNone(position.on_tick(110.0))
            self.assertTrue(position.trailing_active)
            self.assertAlmostEqual(position.stop_price, 105.0)

            event = position.on_tick(105.0)
            self.assertIsNotNone(event)
            self.assertEqual(position.status, "CLOSED")
            self.assertEqual(position.reserved_qty, 0.0)
            self.assertEqual(client.sells[0][1], 1.0)


if __name__ == "__main__":
    unittest.main()
