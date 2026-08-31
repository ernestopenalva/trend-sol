from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from src.exchange.binance_client import SymbolFilters
from src.logging_utils import JsonlLogger
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.entry_engine import EntrySignal
from src.monitor.h2_exposure_shadow import H2ExposureShadow


class H2ExposureShadowTests(unittest.TestCase):
    def test_reducer_steps_are_base_divided_by_uncovered_plus_one(self) -> None:
        with self._shadow() as shadow:
            filters = _filters(min_notional="1", step="0.0001")
            values = []
            for index, price in enumerate((100, 100, 100, 100, 100)):
                sizing = shadow._sizing_for(price, filters)
                self.assertIsNotNone(sizing)
                values.append(float(sizing["candidate_notional"]))
                shadow._open(_signal(price, index), index * 300_000, sizing)  # type: ignore[arg-type]
            self.assertEqual([item["h2_step"] for item in shadow.entry_metadata.values()], [1, 2, 3, 4, 5])
            self.assertEqual(values[0], 20.0)
            self.assertEqual(values[1], 10.0)
            self.assertAlmostEqual(values[2], 20 / 3)
            self.assertEqual(values[3], 5.0)
            self.assertEqual(values[4], 4.0)
            self.assertTrue(all(value <= 20 for value in values))
            self.assertEqual(len({round(item.position_notional_usdt or 0, 8) for item in shadow.open_positions}), 5)
            self.assertLessEqual(sum(item.position_notional_usdt or 0 for item in shadow.open_positions), 100.0)

    def test_blocks_when_minimum_operational_notional_exceeds_capital(self) -> None:
        with self._shadow(capital=4) as shadow:
            shadow.filters_provider = lambda _symbol: _filters(min_notional="5")
            self.assertFalse(shadow.on_signal(_signal(100, 0)))
            self.assertEqual(shadow.blocked_min_notional_h2, 1)

    def test_uncovered_count_drops_when_be_arms_and_step_recovers(self) -> None:
        with self._shadow() as shadow:
            shadow.filters_provider = lambda _symbol: _filters()
            self.assertTrue(shadow.on_signal(_signal(100, 0)))
            self.assertTrue(shadow.on_signal(_signal(101, 1)))
            steps = [item["h2_step"] for item in shadow.entry_metadata.values()]
            self.assertEqual(steps, [1, 2])
            shadow.on_tick(100.7, "2026-08-31T00:01:00+00:00")
            self.assertEqual(shadow.open_positions[0].current_step(), "BE")
            self.assertTrue(shadow.on_signal(_signal(102, 2)))
            self.assertEqual(shadow.entry_metadata[shadow.open_positions[-1].pair_id]["uncovered_count"], 1)
            self.assertEqual(shadow.entry_metadata[shadow.open_positions[-1].pair_id]["h2_step"], 2)
            shadow.on_tick(102.7, "2026-08-31T00:02:00+00:00")
            self.assertTrue(shadow.on_signal(_signal(103, 3)))
            self.assertEqual(shadow.entry_metadata[shadow.open_positions[-1].pair_id]["uncovered_count"], 0)
            self.assertEqual(shadow.entry_metadata[shadow.open_positions[-1].pair_id]["candidate_notional"], 20.0)

    def test_cap_and_state_survive_restart_and_do_not_share_context_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config()
            logger = _logger(root)
            shadow = H2ExposureShadow(root, config, logger, None, lambda _symbol: _filters())
            self.assertTrue(shadow.on_signal(_signal(100, 0)))
            original_notional = shadow.open_positions[0].position_notional_usdt
            restored = H2ExposureShadow(root, config, logger, None, lambda _symbol: _filters())
            self.assertEqual(len(restored.open_positions), 1)
            self.assertEqual(restored.open_positions[0].position_notional_usdt, original_notional)
            self.assertEqual(restored.entry_metadata[restored.open_positions[0].pair_id]["h2_step"], 1)
            context = RealAContextShadow(
                root, config, logger, None, settings_key="dmi15_trajectory_context_shadow",
                strategy="DMI15_TRAJECTORY_CONTEXT_SHADOW", shadow_kind="DMI15_TRAJECTORY_CONTEXT_SHADOW",
                pair_prefix="ctx", predicate=lambda _engine, _snapshot: True,
            )
            self.assertTrue(context.on_signal(_signal(100, 0)))
            self.assertEqual(len(restored.open_positions), 1)
            self.assertEqual(len(context.open_positions), 1)

    def test_variable_notional_uses_same_exit_engine_and_records_dollar_pnl_and_fees(self) -> None:
        with self._shadow() as shadow:
            shadow.filters_provider = lambda _symbol: _filters()
            self.assertTrue(shadow.on_signal(_signal(100, 0)))
            position = shadow.open_positions[0]
            self.assertIsNone(position.shadow_kind)
            shadow.on_tick(98.0, "2026-08-31T00:01:00+00:00")
            record = shadow.ledger.load()[0]
            self.assertEqual(record["position_type"], "H2_EXPOSURE_SHADOW")
            self.assertEqual(record["h2"]["sizing_version"], "v2_reducer")
            self.assertAlmostEqual(record["gross_pnl_usdt"], (98 - 100) * record["qty"])
            self.assertAlmostEqual(record["estimated_fees_usdt"], record["position_notional_usdt"] * 0.002)
            self.assertAlmostEqual(record["net_pnl_usdt"], record["gross_pnl_usdt"] - record["estimated_fees_usdt"])

    def _shadow(self, capital: float = 100):
        return _ShadowContext(capital)


class _ShadowContext:
    def __init__(self, capital: float) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.shadow = H2ExposureShadow(self.root, _config(capital), _logger(self.root), None, lambda _symbol: _filters())

    def __enter__(self) -> H2ExposureShadow:
        return self.shadow

    def __exit__(self, *_args) -> None:
        self.temp.cleanup()


def _signal(price: float, index: int) -> EntrySignal:
    return EntrySignal("SOLUSDT", price, "ts", index * 300_000, 0.2, "1m", 14)


def _filters(min_notional: str = "5", step: str = "0.001") -> SymbolFilters:
    return SymbolFilters(Decimal("0.001"), Decimal(step), Decimal(min_notional), 0, 10_000, 0, 10_000)


def _logger(root: Path) -> JsonlLogger:
    return JsonlLogger(root, {"logging": {"console": False, "trade_log": "logs/trades.jsonl", "decision_log": "logs/decisions.jsonl", "system_log": "logs/system.log"}})


def _config(capital: float = 100) -> dict:
    risk = {
        "hard_stop": {"enabled": True, "stop_pct": 1.5},
        "no_progress": {"enabled": False},
        "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1},
        "profit_lock": {"mode": "atr", "economic_floor": {"enabled": True, "net_margin_pct": 0.05}, "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]},
        "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5},
    }
    return {
        "symbol": "SOLUSDT",
        "capital": {"operational_balance_usdt": capital, "trade_size_pct": 20, "max_open_positions": 5},
        "trend": {"timeframe": "15m", "ema_period": 20, "ema_slope_lookback": 3},
        "trend_gate": {"mode": "ge30", "candle_interval": "5m", "lookback_candles": 3, "sync": {"enabled": False}},
        "entry": {"timeframe": "1m", "atr_period": 14, "max_entries_per_candle": 1, "admission_candle_interval": "5m", "entry_spacing_atr": 1},
        "risk": risk, "fees": {"enabled": True, "taker_fee_pct": 0.1}, "ladder": {"be_net_margin_pct": 0.05, "be_activation_buffer_atr": 0.5},
        "logging": {"console": False, "trade_log": "logs/trades.jsonl", "decision_log": "logs/decisions.jsonl", "system_log": "logs/system.log"},
        "instrumentation": {
            "h2_exposure_shadow": {"enabled": True, "accept_new_entries": True, "sizing_version": "v2_reducer", "capital_max_usdt": capital, "max_open_positions": 5, "state_file": "data/state/h2.json", "ledger_file": "data/trades/h2.jsonl"},
            "dmi15_trajectory_context_shadow": {"enabled": True, "accept_new_entries": True, "max_open_positions": 5, "state_file": "data/state/context.json", "ledger_file": "data/trades/context.jsonl"},
        },
    }
