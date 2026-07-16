from __future__ import annotations

import io
import json
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
from src.exchange.binance_client import BinanceClientError
from src.monitor.cycle_manager import CycleManager
from src.monitor.entry_engine import EntrySignal
from src.monitor.position_registry import PositionRegistry
from src.position.bot_full_engine import BotFullExitPosition
from src.state_manager import StateManager
from src.trade_ledger import TradeLedger
from tools.list_positions import _effective_stop_text, _normalize_state, _print_human
from tools.trades_report import (
    _estimated_fees_pct,
    _estimated_fees_pct_for_records,
    _exit_reason,
    _filter,
    _net_pnl,
    _parse_args,
    _print_detail_sections,
    _print_inline_counts,
    _print_summary,
    _print_trades,
    _slots_full_pct,
)


class FakeClient:
    def __init__(self) -> None:
        self.buys = []
        self.buy_quotes = []
        self.trailing_orders = []
        self.sells = []

    def market_buy_quote(self, symbol: str, quote_qty: float, client_order_id: str):
        self.buys.append(client_order_id)
        self.buy_quotes.append(quote_qty)
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

    def market_sell(self, symbol: str, quantity: float, client_order_id: str):
        self.sells.append((symbol, quantity, client_order_id))
        return {
            "orderId": 3,
            "clientOrderId": client_order_id,
            "executedQty": str(quantity),
            "cummulativeQuoteQty": str(quantity * 97),
            "fills": [{"price": "97", "qty": str(quantity)}],
        }

    def open_orders(self, symbol: str):
        return []

    def all_orders(self, symbol: str, limit: int = 100):
        return []


class FakeTelemetryWriter:
    def __init__(self) -> None:
        self.events = []

    def submit(self, stream: str, event: dict) -> bool:
        self.events.append((stream, event))
        return True


class FailingSellClient(FakeClient):
    def __init__(self, fail_on_attempt: int) -> None:
        super().__init__()
        self.fail_on_attempt = fail_on_attempt
        self.sell_attempts = 0

    def market_sell(self, symbol: str, quantity: float, client_order_id: str):
        self.sell_attempts += 1
        if self.sell_attempts == self.fail_on_attempt:
            raise BinanceClientError("ambiguous market sell failure")
        return super().market_sell(symbol, quantity, client_order_id)


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

    def test_multi_trade_uses_fixed_configured_size_and_sequential_position_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config()
            config["capital"] = {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5}
            config["entry"] = {"entry_spacing_atr": 0, "max_entries_per_candle": 1}
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
            registry.open_pair(EntrySignal("SOLUSDT", 101, "ts", 2, 0.2, "1m", 14))

            self.assertEqual(client.buy_quotes, [20.0, 20.0])
            self.assertEqual([position.position_id for position in registry.positions], [1, 2])
            self.assertEqual([position.position_notional_usdt for position in registry.positions], [20.0, 20.0])

    def test_hard_stop_closes_five_positions_sequentially_and_clears_state(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config()
            config["capital"] = {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5}
            config["entry"] = {"entry_spacing_atr": 0, "max_entries_per_candle": 1}
            config["risk"]["hard_stop"] = {"enabled": True, "stop_pct": 3.0}
            root = Path(tmp)
            logger = JsonlLogger(root, config)
            state = StateManager(root)
            client = FakeClient()
            ledger = TradeLedger(root)
            registry = PositionRegistry(
                config,
                client,  # type: ignore[arg-type]
                logger,
                CycleManager(root, config, logger, state),
                state,
                ledger,
            )
            for candle in range(1, 6):
                registry.open_pair(EntrySignal("SOLUSDT", 100, "ts", candle, 0.2, "1m", 14))

            registry.on_tick(96.9, market_ts="2026-07-13T22:00:00+00:00")

            self.assertEqual(len(client.sells), 5)
            self.assertEqual(registry.positions, [])
            self.assertEqual(state.load_open_positions(), [])
            records = ledger.load()
            self.assertEqual(len(records), 5)
            self.assertEqual({record["exit_reason"] for record in records}, {"HARD_STOP"})
            self.assertEqual({record["hard_stop_pct"] for record in records}, {3.0})
            self.assertEqual({record["hard_stop_applied_on_restore"] for record in records}, {False})

    def test_hard_stop_sell_error_pauses_entries_and_continues_other_positions(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config()
            config["capital"] = {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 3}
            config["entry"] = {"entry_spacing_atr": 0, "max_entries_per_candle": 1}
            config["risk"]["hard_stop"] = {"enabled": True, "stop_pct": 3.0}
            root = Path(tmp)
            logger = JsonlLogger(root, config)
            state = StateManager(root)
            client = FailingSellClient(fail_on_attempt=2)
            ledger = TradeLedger(root)
            registry = PositionRegistry(
                config,
                client,  # type: ignore[arg-type]
                logger,
                CycleManager(root, config, logger, state),
                state,
                ledger,
            )
            for candle in range(1, 4):
                registry.open_pair(EntrySignal("SOLUSDT", 100, "ts", candle, 0.2, "1m", 14))

            registry.on_tick(96.9, market_ts="2026-07-13T22:00:00+00:00")

            self.assertEqual(client.sell_attempts, 3)
            self.assertEqual(len(client.sells), 2)
            self.assertTrue(registry.review_required)
            self.assertEqual(len(registry.positions), 1)
            self.assertEqual(registry.positions[0].status, "NEEDS_REVIEW")
            self.assertEqual(len(ledger.load()), 2)

    def test_admission_blocks_second_entry_in_same_candle(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config()
            config["capital"] = {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5}
            config["entry"] = {"entry_spacing_atr": 0, "max_entries_per_candle": 1}
            root = Path(tmp)
            logger = JsonlLogger(root, config)
            state = StateManager(root)
            registry = PositionRegistry(
                config,
                FakeClient(),  # type: ignore[arg-type]
                logger,
                CycleManager(root, config, logger, state),
                state,
                TradeLedger(root),
            )

            registry.open_pair(EntrySignal("SOLUSDT", 100, "ts", 1, 0.2, "1m", 14))
            registry.open_pair(EntrySignal("SOLUSDT", 101, "ts", 1, 0.2, "1m", 14))

            self.assertEqual(len(registry.positions), 1)
            self.assertEqual(_last_decision_reason(root), "BLOCKED_CANDLE_LIMIT")

    def test_admission_spacing_uses_new_signal_atr(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config()
            config["capital"] = {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5}
            config["entry"] = {"entry_spacing_atr": 1.0, "max_entries_per_candle": 1}
            root = Path(tmp)
            logger = JsonlLogger(root, config)
            state = StateManager(root)
            registry = PositionRegistry(
                config,
                FakeClient(),  # type: ignore[arg-type]
                logger,
                CycleManager(root, config, logger, state),
                state,
                TradeLedger(root),
            )

            registry.open_pair(EntrySignal("SOLUSDT", 100, "ts", 1, 0.2, "1m", 14))
            registry.open_pair(EntrySignal("SOLUSDT", 100.90, "ts", 2, 1.0, "1m", 14))

            self.assertEqual(len(registry.positions), 1)
            self.assertEqual(_last_decision_reason(root), "BLOCKED_SPACING")
            decision = _last_decision(root)
            self.assertEqual(decision["blocked_against_position_id"], 1)
            self.assertAlmostEqual(decision["existing_entry_price"], 100.0)
            self.assertAlmostEqual(decision["new_signal_atr"], 1.0)
            self.assertAlmostEqual(decision["required_distance"], 1.0)
            self.assertAlmostEqual(decision["actual_distance"], 0.9)

    def test_admission_blocks_when_max_positions_full(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config()
            config["capital"] = {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 1}
            config["entry"] = {"entry_spacing_atr": 0, "max_entries_per_candle": 1}
            root = Path(tmp)
            logger = JsonlLogger(root, config)
            state = StateManager(root)
            telemetry = FakeTelemetryWriter()
            registry = PositionRegistry(
                config,
                FakeClient(),  # type: ignore[arg-type]
                logger,
                CycleManager(root, config, logger, state),
                state,
                TradeLedger(root),
                telemetry,  # type: ignore[arg-type]
            )

            registry.open_pair(EntrySignal("SOLUSDT", 100, "ts", 1, 0.2, "1m", 14))
            registry.open_pair(EntrySignal("SOLUSDT", 101, "ts", 2, 0.2, "1m", 14))

            self.assertEqual(len(registry.positions), 1)
            self.assertEqual(_last_decision_reason(root), "BLOCKED_MAX_POSITIONS")
            rejected = [event for stream, event in telemetry.events if stream == "rejected_signal"]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["reason"], "BLOCKED_MAX_POSITIONS")
            self.assertEqual(rejected[0]["open_positions"], 1)
            self.assertAlmostEqual(rejected[0]["worst_open_pnl_pct"], 1.0)
            self.assertFalse(rejected[0]["phantom_created"])

    def test_hourly_snapshot_is_emitted_on_first_tick_of_new_hour(self) -> None:
        with TemporaryDirectory() as tmp:
            config = _config()
            config["capital"] = {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5}
            config["entry"] = {"entry_spacing_atr": 0, "max_entries_per_candle": 1}
            root = Path(tmp)
            logger = JsonlLogger(root, config)
            state = StateManager(root)
            telemetry = FakeTelemetryWriter()
            registry = PositionRegistry(
                config,
                FakeClient(),  # type: ignore[arg-type]
                logger,
                CycleManager(root, config, logger, state),
                state,
                TradeLedger(root),
                telemetry,  # type: ignore[arg-type]
            )
            registry.open_pair(EntrySignal("SOLUSDT", 100, "ts", 1, 0.2, "1m", 14))

            registry.on_tick(100.0, market_ts="2026-07-12T10:59:59+00:00")
            registry.on_tick(100.1, market_ts="2026-07-12T11:00:00+00:00")
            registry.on_tick(100.2, market_ts="2026-07-12T11:30:00+00:00")
            registry.on_tick(99.8, market_ts="2026-07-12T11:31:00+00:00")

            snapshots = [event for stream, event in telemetry.events if stream == "position_snapshot"]
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(snapshots[0]["snapshot_hour"], "2026-07-12T11:00:00+00:00")
            self.assertEqual(snapshots[0]["position_id"], 1)
            self.assertAlmostEqual(snapshots[0]["entry_atr"], 0.2)
            self.assertEqual(snapshots[0]["current_step"], "NONE")
            troughs = [event for stream, event in telemetry.events if stream == "trough_event"]
            self.assertEqual(len(troughs), 1)
            self.assertEqual(troughs[0]["ts"], "2026-07-12T11:31:00+00:00")
            self.assertAlmostEqual(troughs[0]["trough_atr"], -1.0)

    def test_closed_bot_trade_is_written_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = TradeLedger(root)
            position = _closed_position(root)

            self.assertTrue(ledger.append_closed_bot_trade(position, _config()))
            self.assertFalse(ledger.append_closed_bot_trade(position, _config()))
            self.assertEqual(len(ledger.load()), 1)
            self.assertAlmostEqual(ledger.load()[0]["trough_price"], 99.0)
            self.assertEqual(ledger.load()[0]["time_to_trough_seconds"], 300)

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

    def test_trades_report_estimates_round_trip_taker_fees(self) -> None:
        config = {"fees": {"enabled": True, "taker_fee_pct": 0.10, "use_bnb_discount": False}}

        self.assertAlmostEqual(_estimated_fees_pct(4, config), 0.80)

    def test_trades_report_calculates_slots_full_time(self) -> None:
        records = [
            {"opened_at": "2026-07-08T00:00:00+00:00", "closed_at": "2026-07-08T00:10:00+00:00"},
            {"opened_at": "2026-07-08T00:05:00+00:00", "closed_at": "2026-07-08T00:15:00+00:00"},
        ]
        config = {"capital": {"max_open_positions": 2}}

        self.assertAlmostEqual(_slots_full_pct(records, config), 100 / 3)

    def test_trades_report_prefers_record_level_fees_and_net(self) -> None:
        config = {"fees": {"enabled": True, "taker_fee_pct": 0.10, "use_bnb_discount": False}}
        records = [
            {"gross_pnl_pct": 0.50, "estimated_fees_pct": 0.20, "net_pnl_pct": 0.30},
            {"realized_pnl_pct": 0.40},
        ]

        self.assertAlmostEqual(_estimated_fees_pct_for_records(records, config), 0.40)
        self.assertAlmostEqual(_net_pnl(records[0], config), 0.30)
        self.assertAlmostEqual(_net_pnl(records[1], config), 0.20)

    def test_trades_report_mae_is_null_safe_and_shows_sample_size(self) -> None:
        records = [
            {
                "trough_pct": -1.5,
                "trough_atr": -4,
                "time_to_trough_seconds": 3600,
                "trough_tracking_complete": True,
            },
            {"trough_tracking_complete": False},
            {},
        ]
        output = io.StringIO()

        with redirect_stdout(output):
            _print_detail_sections(records)

        text = output.getvalue()
        self.assertIn("MAE / trough (N=1/3)", text)
        self.assertIn("worst trough: -1.50%", text)

    def test_trades_report_groups_summary_families_inline(self) -> None:
        records = [
            {
                "opened_at": "2026-07-11T07:00:00+00:00",
                "closed_at": "2026-07-11T08:00:00+00:00",
                "age_seconds": 3600,
                "position_notional_usdt": 20,
                "gross_pnl_pct": 0.5,
                "estimated_fees_pct": 0.2,
                "net_pnl_pct": 0.3,
            }
        ]
        config = {
            "fees": {"enabled": True, "taker_fee_pct": 0.1, "use_bnb_discount": False},
            "capital": {"max_open_positions": 5},
        }
        output = io.StringIO()

        with redirect_stdout(output):
            _print_summary(records, config)
            _print_inline_counts("Exit reasons", {"BREAKEVEN": 1, "HARD_STOP": 2})

        text = output.getvalue()
        self.assertIn("trades: 1 | avg age=1h00m | slots full time=0.0%", text)
        self.assertIn("fees: estimated sum=-0.20% (-0.0400 USDT) | taker=0.100%/side", text)
        self.assertIn("gross: total=+0.50% | avg/trade=+0.50%", text)
        self.assertIn("net: total=+0.30% | avg/trade=+0.30%", text)
        self.assertIn("Exit reasons: BREAKEVEN=1 | HARD_STOP=2", text)

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
                "trough_price": 99,
                "trough_tracking_complete": True,
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
        self.assertNotIn("SOL/USDT | opened", text)
        self.assertIn("entry: 08/07 19:00 (age ", text)
        self.assertIn(") | 100.0000 -> now 100.5000", text)
        self.assertIn("peak:  101.0000  (+1.00% / +5.00 ATR)", text)
        self.assertIn("trough: 99.0000  (-1.00% / -5.00 ATR)", text)
        self.assertIn("next:  PL2 at 101.6000 (+1.60% / +8.00 ATR)", text)
        self.assertIn("trail: inactive, activates at 102.0000 (+2.00% / +10.00 ATR)", text)
        self.assertEqual(
            _effective_stop_text({"effective_stop": 97, "stop_type": "hard_stop", "hard_stop_pct": 3}),
            "hard stop 97.0000 (-3.00%)",
        )

    def test_trades_report_lists_trough_and_marks_partial_tracking(self) -> None:
        records = [
            {
                "opened_at": "2026-07-11T07:00:00+00:00",
                "closed_at": "2026-07-11T08:00:00+00:00",
                "age_seconds": 3600,
                "entry_price": 100,
                "peak_price": 101,
                "trough_price": 98.5,
                "trough_tracking_complete": False,
                "exit_price": 100.5,
                "position_notional_usdt": 20,
                "gross_pnl_pct": 0.5,
                "exit_reason": "BREAKEVEN",
            }
        ]
        output = io.StringIO()

        with redirect_stdout(output):
            _print_trades(records, {"fees": {"enabled": False}})

        text = output.getvalue()
        self.assertIn("peak               trough", text)
        self.assertIn("101.0000 (+1.00%)", text)
        self.assertIn("98.5000* (-1.50%)", text)
        self.assertIn("giveback", text)
        self.assertIn("+0.5000 (+0.50%)", text)
        self.assertIn("* observed trough; tracking started after the trade opened", text)


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
    position.on_tick(99.0, market_ts="2026-07-08T22:05:00+00:00")
    position.highest_price = 101.6
    position.effective_stop = 100.6
    position.stop_type = "profit_lock"
    position.be_atr_stop = 100.02
    position.be_net_floor = 100.25
    position.be_stop = 100.25
    position.be_activation_price = 100.35
    position.be_activation_buffer_atr = 0.5
    position.be_floor_source = "NET_FLOOR"
    position.be_floor_absorbed_atr_stop = True
    position.exit_trigger_price = 100.4
    position.exit_trigger_price_source = "aggTrade"
    position.exit_slippage_pct = -0.1
    position.mark_closed(100.5, "PROFIT_LOCK", "2026-07-08T22:10:00+00:00", {})
    return position


def _last_decision_reason(root: Path) -> str:
    return str(_last_decision(root)["reason"])


def _last_decision(root: Path) -> dict:
    path = root / "logs" / "decisions.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1])


def _config():
    return {
        "symbol": "SOLUSDT",
        "active_profile": "intraday",
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
