from __future__ import annotations

import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

if "requests" not in sys.modules:
    requests_stub = types.ModuleType("requests")
    requests_stub.Session = lambda: None
    sys.modules["requests"] = requests_stub

from src.logging_utils import JsonlLogger
from src.monitor.cycle_manager import CycleManager
from src.monitor.entry_engine import EntrySignal
from src.monitor.position_registry import PositionRegistry
from src.position.bot_full_engine import BotFullExitPosition
from src.state_manager import StateManager
from src.trade_ledger import TradeLedger
from tools.list_positions import _normalize_state, _print_human
from tools.trades_report import _exit_reason, _filter, _parse_args


class FakeClient:
    def __init__(self) -> None:
        self.buys = []
        self.trailing_orders = []

    def market_buy_quote(self, symbol: str, quote_qty: float, client_order_id: str):
        self.buys.append(client_order_id)
        return {
            "orderId": 1,
            "clientOrderId": client_order_id,
            "executedQty": "1",
            "cummulativeQuoteQty": "100",
            "fills": [{"price": "100", "qty": "1"}],
        }

    def validate_notional(self, symbol: str, quantity: float, price: float) -> None:
        return None

    def trailing_sell(self, *args, **kwargs):
        self.trailing_orders.append(kwargs)
        return {"orderId": 2, "clientOrderId": kwargs.get("client_order_id")}

    def open_orders(self, symbol: str):
        return []

    def all_orders(self, symbol: str, limit: int = 100):
        return []


class BotExitOnlyTests(unittest.TestCase):
    def test_bot_exit_only_does_not_create_a(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config()
            root = Path(tmp)
            logger = JsonlLogger(root, config)
            state = StateManager(root)
            client = FakeClient()
            registry = PositionRegistry(
                config,
                client,  # type: ignore[arg-type]
                logger,
                CycleManager(root, config, logger, state),
                state,
                TradeLedger(root),
            )

            registry.open_pair(EntrySignal("SOLUSDT", 100, "ts", 1, 0.2, "1m", 14))

            self.assertEqual(client.buys, ["ts-" + registry.positions[0].pair_id + "-B-buy"])
            self.assertEqual(client.trailing_orders, [])
            self.assertEqual([position.label for position in registry.positions], ["B"])

    def test_closed_bot_trade_is_written_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = TradeLedger(root)
            position = _closed_position(root)

            self.assertTrue(ledger.append_closed_bot_trade(position, _config()))
            self.assertFalse(ledger.append_closed_bot_trade(position, _config()))
            self.assertEqual(len(ledger.load()), 1)

    def test_trades_report_filters_since_and_strategy(self) -> None:
        records = [
            {"closed_at": "2026-07-08T23:00:00+00:00", "strategy_version": "old", "run_id": "r1"},
            {"closed_at": "2026-07-09T01:30:00+00:00", "strategy_version": "b_atr_v1.2", "run_id": "r2"},
        ]
        args = _parse_args_for_tests(["--since", "2026-07-08 22:00", "--strategy", "b_atr_v1.2"])

        filtered = _filter(records, args)

        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["run_id"], "r2")

    def test_trades_report_labels_be_review_stop_as_breakeven(self) -> None:
        record = {"exit_reason": "REVIEW_STOP", "final_step": "BE"}

        self.assertEqual(_exit_reason(record), "BREAKEVEN")

    def test_list_positions_omits_a_in_bot_exit_only(self) -> None:
        config = _config()
        state = [
            {"pair_id": "abcdef123456", "label": "A", "status": "OPEN", "entry_price": 100, "open_ts": "2026-07-08T22:00:00+00:00"},
            {
                "pair_id": "abcdef123456",
                "label": "B",
                "status": "OPEN",
                "entry_price": 100,
                "quantity": 1,
                "entry_atr": 0.2,
                "highest_price": 101,
                "effective_stop": 100.3,
                "stop_type": "profit_lock",
                "open_ts": "2026-07-08T22:00:00+00:00",
            },
        ]
        normalized = _normalize_state(state, 100.5, config)
        output = io.StringIO()

        with redirect_stdout(output):
            _print_human(normalized, config)

        text = output.getvalue()
        self.assertNotIn("A Binance Trail", text)
        self.assertIn("Bot Exit", text)


def _parse_args_for_tests(args):
    import sys
    from unittest.mock import patch

    with patch.object(sys, "argv", ["trades_report.py", *args]):
        return _parse_args()


def _closed_position(root: Path) -> BotFullExitPosition:
    logger = JsonlLogger(root, _config())
    position = BotFullExitPosition(
        pair_id="pair1",
        symbol="SOLUSDT",
        entry_price=100,
        quantity=1,
        entry_order={},
        open_ts="2026-07-08T22:00:00+00:00",
        config=_config()["risk"],
        client=FakeClient(),  # type: ignore[arg-type]
        logger=logger,
        entry_atr=0.2,
        atr_timeframe="1m",
        atr_period=14,
    )
    position.highest_price = 101.6
    position.effective_stop = 100.6
    position.stop_type = "profit_lock"
    position.mark_closed(100.5, "PROFIT_LOCK", "2026-07-08T22:10:00+00:00", {})
    return position


def _config():
    return {
        "symbol": "SOLUSDT",
        "position_mode": "bot_exit_only",
        "strategy_version": "b_atr_v1.2",
        "run_id": "run1",
        "capital": {"operational_balance_usdt": 1000, "trade_size_pct": 5, "max_open_pairs": 1},
        "cycle": {"pairs_per_cycle": 1, "prolabore_pct": 5, "stats_min_pairs": 50},
        "logging": {"console": False, "trade_log": "logs/trades.jsonl", "decision_log": "logs/decisions.jsonl", "system_log": "logs/system.log"},
        "console": {"mode": "human"},
        "exit_server_simple_trail": {"trailing_delta_bips": 100, "use_stop_price": False},
        "risk": {
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
        },
    }
