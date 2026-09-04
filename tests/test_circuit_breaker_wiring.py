from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.app import Monitor
from src.logging_utils import JsonlLogger
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.entry_engine import EntrySignal


class _Telemetry:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def submit(self, _kind: str, payload: dict) -> None:
        self.events.append(payload)


class _Probe:
    def __init__(self, signal: EntrySignal | None = None, open_result: bool = True) -> None:
        self.signal = signal
        self.open_result = open_result
        self.ticks: list[tuple[float, str]] = []
        self.approved: list[tuple[EntrySignal, dict | None]] = []
        self.klines = 0

    def on_ws_event(self, *_args: object) -> None:
        pass

    def on_kline(self, *_args: object) -> EntrySignal | None:
        self.klines += 1
        return self.signal

    def on_signal(self, *_args: object) -> bool:
        return True

    def on_approved_real_a_signal(self, signal: EntrySignal, context: dict | None) -> bool:
        self.approved.append((signal, context))
        return True

    def on_tick(self, price: float, timestamp: str | None = None, **_kwargs: object) -> None:
        self.ticks.append((price, timestamp))

    def open_pair(self, *_args: object) -> bool:
        return self.open_result


class CircuitBreakerWiringTests(unittest.TestCase):
    def test_synthetic_detector_lifecycle_trigger_block_release_then_admit(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            telemetry = _Telemetry()
            shadow = CircuitBreakerShadow(root, _config(), JsonlLogger(root, _config()), telemetry)  # type: ignore[arg-type]
            now = datetime(2026, 9, 4, 16, tzinfo=timezone.utc)
            shadow.equity, shadow.peak_equity = 98.4, 100.0
            shadow.closed_history = [(now - timedelta(hours=2), -0.3), (now - timedelta(hours=1), -0.3)]

            shadow._evaluate_after_close(now, 100.0)
            blocked = EntrySignal("SOLUSDT", 100.0, now.isoformat(), 1_000_000, 0.2, "1m", 14)
            self.assertFalse(shadow.on_approved_real_a_signal(blocked, {"tf_5m": {}}))
            shadow._release_if_due(now + timedelta(hours=6), 100.0)
            admitted = EntrySignal("SOLUSDT", 100.0, (now + timedelta(hours=6, minutes=1)).isoformat(), 1_300_000, 0.2, "1m", 14)
            self.assertTrue(shadow.on_approved_real_a_signal(admitted, {"tf_5m": {}}))

            names = [item["event"] for item in telemetry.events if "event" in item]
            self.assertLess(names.index("CIRCUIT_BREAKER_TRIGGERED"), names.index("ENTRY_BLOCKED_CIRCUIT_BREAKER"))
            self.assertLess(names.index("ENTRY_BLOCKED_CIRCUIT_BREAKER"), names.index("CIRCUIT_BREAKER_RELEASED"))
            self.assertLess(names.index("CIRCUIT_BREAKER_RELEASED"), names.index("OPEN"))

    def test_monitor_routes_approved_signal_and_each_tick_once(self) -> None:
        signal = EntrySignal("SOLUSDT", 100.0, "2026-09-04T16:01:00+00:00", 1_000_000, 0.2, "1m", 14)
        monitor = Monitor.__new__(Monitor)
        generic = _Probe()
        circuit = _Probe()
        monitor.config = {"symbol": "SOLUSDT", "entry": {"timeframe": "1m"}}
        monitor.market_shadow = generic
        monitor.market_shadow_ge30 = None
        monitor.entry_engine = _Probe(signal)
        monitor.market_context = type("Context", (), {"latest": {"tf_5m": {"ema20": 100.0}}})()
        monitor.registry = generic
        monitor.h2_exposure_shadow = generic
        monitor.gcr_shadow = generic
        monitor.dmi15_shadow = generic
        monitor.dmi15_spread_shadow = generic
        monitor.dmi15_trajectory_shadow = generic
        monitor.dmi15_rsi70_shadow = generic
        monitor.dmi15_combined_shadow = generic
        monitor.dmi15_trajectory_context_shadow = generic
        monitor.slow_ge_context_shadow = generic
        monitor.circuit_breaker_shadow = circuit
        monitor._entry_should_pause = lambda _stream, _payload: False
        monitor._entry_operational_pause_reason = lambda: None
        monitor._stop_after_cycle_if_needed = lambda: None

        monitor._on_ws_event("solusdt@kline_1m", {"k": {"x": True}})
        monitor._on_ws_event("solusdt@aggTrade", {"p": "100.50", "T": 1_000_001})

        self.assertEqual(circuit.approved, [(signal, {"tf_5m": {"ema20": 100.0}})])
        self.assertEqual(len(circuit.ticks), 1)
        self.assertEqual(circuit.klines, 0)

    def test_monitor_does_not_admit_cb_when_real_open_did_not_complete(self) -> None:
        signal = EntrySignal("SOLUSDT", 100.0, "2026-09-04T16:01:00+00:00", 1_000_000, 0.2, "1m", 14)
        monitor = Monitor.__new__(Monitor)
        generic = _Probe()
        circuit = _Probe()
        monitor.config = {"symbol": "SOLUSDT", "entry": {"timeframe": "1m"}}
        monitor.market_shadow = generic
        monitor.market_shadow_ge30 = None
        monitor.entry_engine = _Probe(signal)
        monitor.market_context = type("Context", (), {"latest": {"tf_5m": {"ema20": 100.0}}})()
        monitor.registry = _Probe(open_result=False)
        monitor.h2_exposure_shadow = generic
        monitor.gcr_shadow = generic
        monitor.dmi15_shadow = generic
        monitor.dmi15_spread_shadow = generic
        monitor.dmi15_trajectory_shadow = generic
        monitor.dmi15_rsi70_shadow = generic
        monitor.dmi15_combined_shadow = generic
        monitor.dmi15_trajectory_context_shadow = generic
        monitor.slow_ge_context_shadow = generic
        monitor.circuit_breaker_shadow = circuit
        monitor._entry_should_pause = lambda _stream, _payload: False
        monitor._entry_operational_pause_reason = lambda: None
        monitor._stop_after_cycle_if_needed = lambda: None

        monitor._on_ws_event("solusdt@kline_1m", {"k": {"x": True}})

        self.assertEqual(circuit.approved, [])
        self.assertEqual(circuit.klines, 0)


def _config() -> dict:
    return {
        "symbol": "SOLUSDT",
        "capital": {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5},
        "trend": {"timeframe": "15m", "ema_period": 20, "ema_slope_lookback": 3},
        "trend_gate": {"mode": "ge30", "candle_interval": "5m", "lookback_candles": 3, "sync": {"enabled": False}},
        "entry": {"timeframe": "1m", "atr_period": 14, "max_entries_per_candle": 1, "admission_candle_interval": "5m", "entry_spacing_atr": 1},
        "risk": {"hard_stop": {"enabled": True, "stop_pct": 1.5}, "no_progress": {"enabled": False}, "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": 0.1}, "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 5, "lock_atr": 1.5}]}, "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5}},
        "fees": {"enabled": True, "taker_fee_pct": 0.1}, "ladder": {}, "logging": {"console": False},
        "instrumentation": {"circuit_breaker_shadow": {"enabled": True, "accept_new_entries": True, "initial_capital_usdt": 100, "max_open_positions": 5, "state_file": "data/state/cb.json", "ledger_file": "data/trades/cb.jsonl"}},
    }
