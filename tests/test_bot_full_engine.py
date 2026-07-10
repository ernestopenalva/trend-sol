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
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_breakeven_and_trailing_close(self) -> None:
        with TemporaryDirectory() as tmp:
            logger = JsonlLogger(
                Path(tmp),
                {
                    "logging": {
                        "console": False,
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
            self.assertAlmostEqual(position.profit_lock_stop, 105.0)
            self.assertAlmostEqual(position.effective_stop, 105.6)

            event = position.on_tick(105.0)
            self.assertIsNotNone(event)
            self.assertEqual(position.status, "CLOSED")
            self.assertEqual(position.reserved_qty, 0.0)
            self.assertEqual(client.sells[0][1], 1.0)

    def test_atr_profit_lock_activates_at_trigger(self) -> None:
        position = self._atr_position()

        self.assertIsNone(position.on_tick(100.99))
        self.assertIsNone(position.profit_lock_stop)

        self.assertIsNone(position.on_tick(101.00))
        self.assertAlmostEqual(position.profit_lock_stop, 100.30)
        self.assertEqual(position.stop_type, "profit_lock")
        self.assertAlmostEqual(position.effective_stop, 100.30)

    def test_atr_trailing_activates_at_trigger(self) -> None:
        position = self._atr_position()

        self.assertIsNone(position.on_tick(101.99))
        self.assertFalse(position.trailing_active)

        self.assertIsNone(position.on_tick(102.00))

        self.assertTrue(position.trailing_active)
        self.assertAlmostEqual(position.trailing_stop, 101.00)
        self.assertEqual(position.stop_type, "trailing")
        self.assertAlmostEqual(position.effective_stop, 101.00)

    def test_effective_stop_chooses_highest_stop(self) -> None:
        position = self._atr_position()

        self.assertIsNone(position.on_tick(102.40))

        self.assertAlmostEqual(position.profit_lock_stop, 101.20)
        self.assertAlmostEqual(position.trailing_stop, 101.40)
        self.assertEqual(position.stop_type, "trailing")
        self.assertAlmostEqual(position.effective_stop, 101.40)

    def test_effective_stop_never_moves_down(self) -> None:
        position = self._atr_position()
        position.effective_stop = 101.50
        position.stop_price = 101.50
        position.stop_type = "trailing"

        self.assertIsNone(position.on_tick(102.00))

        self.assertAlmostEqual(position.trailing_stop, 101.00)
        self.assertAlmostEqual(position.effective_stop, 101.50)
        self.assertEqual(position.stop_type, "trailing")

    def test_breakeven_atr_activates_before_profit_lock(self) -> None:
        position = self._atr_position()

        self.assertIsNone(position.on_tick(100.60))

        self.assertAlmostEqual(position.breakeven_stop, 100.02)
        self.assertIsNone(position.profit_lock_stop)
        self.assertEqual(position.stop_type, "breakeven")
        self.assertAlmostEqual(position.effective_stop, 100.02)

    def test_breakeven_atr_uses_net_fee_floor_when_enabled(self) -> None:
        position = self._atr_position(
            config_overrides={
                "fees": {"enabled": True, "taker_fee_pct": 0.10, "use_bnb_discount": False},
                "ladder": {"be_net_margin_pct": 0.05},
            }
        )

        self.assertIsNone(position.on_tick(100.60))

        self.assertAlmostEqual(position.breakeven_stop, 100.25)
        self.assertEqual(position.stop_type, "breakeven")

    def test_breakeven_net_fee_floor_applies_bnb_discount(self) -> None:
        position = self._atr_position(
            config_overrides={
                "fees": {"enabled": True, "taker_fee_pct": 0.10, "use_bnb_discount": True},
                "ladder": {"be_net_margin_pct": 0.05},
            }
        )

        self.assertIsNone(position.on_tick(100.60))

        self.assertAlmostEqual(position.breakeven_stop, 100.20)

    def test_breakeven_waits_until_price_covers_net_floor(self) -> None:
        position = self._atr_position(
            entry_atr=0.05,
            config_overrides={
                "fees": {"enabled": True, "taker_fee_pct": 0.10, "use_bnb_discount": False},
                "ladder": {"be_net_margin_pct": 0.05},
            },
        )

        self.assertIsNone(position.on_tick(100.15))
        self.assertIsNone(position.breakeven_stop)

        self.assertIsNone(position.on_tick(100.251))
        self.assertAlmostEqual(position.breakeven_stop, 100.25)

    def test_breakeven_atr_close_uses_breakeven_reason(self) -> None:
        client = FakeClient()
        position = self._atr_position(client=client)

        self.assertIsNone(position.on_tick(100.60))
        event = position.on_tick(100.01)

        self.assertIsNotNone(event)
        self.assertEqual(position.status, "CLOSED")
        self.assertEqual(position.exit_reason, "BREAKEVEN")
        self.assertEqual(event["exit_reason"], "BREAKEVEN")
        self.assertEqual(client.sells[0][1], 1.0)

    def test_missing_entry_atr_marks_position_needs_review(self) -> None:
        position = self._atr_position(entry_atr=None)

        self.assertIsNone(position.on_tick(101.00))

        self.assertEqual(position.status, "NEEDS_REVIEW")
        self.assertEqual(position.reserved_qty, 1.0)

    def _atr_position(self, entry_atr=0.20, client=None, config_overrides=None) -> BotFullExitPosition:
        config = {
            "review_stop_pct": 30,
            "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1},
            "profit_lock": {
                "mode": "atr",
                "steps": [
                    {"trigger_atr": 5, "lock_atr": 1.5},
                    {"trigger_atr": 8, "lock_atr": 3},
                    {"trigger_atr": 12, "lock_atr": 6},
                ],
            },
            "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5},
        }
        if config_overrides:
            config.update(config_overrides)
        return BotFullExitPosition(
            pair_id="pair",
            symbol="SOLUSDT",
            entry_price=100.0,
            quantity=1.0,
            entry_order={},
            open_ts="2026-07-04T00:00:00+00:00",
            config=config,
            client=client or FakeClient(),  # type: ignore[arg-type]
            logger=JsonlLogger(
                Path(self.tmp.name),
                {
                    "logging": {
                        "console": False,
                        "trade_log": "logs/trades.jsonl",
                        "decision_log": "logs/decisions.jsonl",
                        "system_log": "logs/system.log",
                    }
                },
            ),
            entry_atr=entry_atr,
            atr_timeframe="1m",
            atr_period=14,
        )


if __name__ == "__main__":
    unittest.main()
