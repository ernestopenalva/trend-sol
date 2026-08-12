from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.monitor.entry_engine import EntrySignal
from tools.ge_replay_study import (
    MATCH_TOLERANCE_MS,
    ReplayEntry,
    SignalEvent,
    entry_overlap,
    find_equivalent_flat_start,
    ge_variant_config,
    generate_ge_signals,
    load_ge_market_data,
    parse_lookbacks,
    run_universe,
)
from tools.market_selection_study import MarketCandle


MINUTE = 60_000


class GeReplayStudyTests(unittest.TestCase):
    def test_ge_variants_differ_only_by_lookback(self) -> None:
        base = _config()
        ge30 = ge_variant_config(base, 6)
        ge15 = ge_variant_config(base, 3)

        ge30["trend_gate"].pop("lookback_candles")
        ge15["trend_gate"].pop("lookback_candles")
        self.assertEqual(ge30, ge15)
        self.assertEqual(parse_lookbacks("30,15"), (6, 3))

    def test_ge30_ge15_and_off_by_one_use_real_entry_engine(self) -> None:
        entry = [_minute_candle(index, 100) for index in range(40)]
        gate = [
            _candle(index * 5, 5, high, low, 100)
            for index, (high, low) in enumerate(
                [(90, 80), (110, 100), (120, 110), (100, 90), (105, 95), (115, 105), (95, 85)]
            )
        ]
        trend = [_candle(0, 15, 100, 90, 100), _candle(15, 15, 100, 90, 100)]
        start = gate[-1].boundary_ms
        end = start

        _, trace30 = generate_ge_signals(ge_variant_config(_config(), 6), entry, gate, trend, start, end, 6)
        _, trace15 = generate_ge_signals(ge_variant_config(_config(), 3), entry, gate, trend, start, end, 3)

        self.assertTrue(trace30[start].passed)   # 95/85 > candle exactly 6 back: 90/80
        self.assertFalse(trace15[start].passed)  # 95/85 is not > candle exactly 3 back: 100/90

    def test_universes_have_independent_slots_and_future_exclusive_entry(self) -> None:
        config = _config()
        config["capital"]["max_open_positions"] = 1
        first = _signal(1, 100)
        second = _signal(2, 101)
        candles = [_minute_candle(index, 100) for index in range(5)]

        universe_a = run_universe(
            name="A", lookback=6, config=config, signals=[first, second],
            execution_candles=candles, start_ms=MINUTE, end_ms=4 * MINUTE,
            intrabar_path="HIGH_FIRST", round_trip_spread_bps=0,
        )
        universe_b = run_universe(
            name="B", lookback=3, config=config, signals=[second],
            execution_candles=candles, start_ms=MINUTE, end_ms=4 * MINUTE,
            intrabar_path="HIGH_FIRST", round_trip_spread_bps=0,
        )

        self.assertEqual([item[0] for item in universe_a.entry_times], [MINUTE])
        self.assertEqual([item[0] for item in universe_b.entry_times], [2 * MINUTE])
        self.assertEqual(universe_a.blocked_slots, 1)
        self.assertEqual(universe_b.blocked_slots, 0)

    def test_future_candles_do_not_change_prior_ge_decision(self) -> None:
        entry = [_minute_candle(index, 100) for index in range(45)]
        gate = [
            _candle(index * 5, 5, 90 + index, 80 + index, 100)
            for index in range(8)
        ]
        trend = [_candle(index * 15, 15, 100, 90, 100) for index in range(3)]
        decision_boundary = 35 * MINUTE

        _, baseline = generate_ge_signals(
            ge_variant_config(_config(), 6), entry, gate, trend,
            decision_boundary, decision_boundary, 6,
        )
        future_gate = [*gate, _candle(40, 5, 10_000, 1, 500)]
        _, with_future = generate_ge_signals(
            ge_variant_config(_config(), 6), entry, future_gate, trend,
            decision_boundary, decision_boundary, 6,
        )

        self.assertEqual(baseline[decision_boundary], with_future[decision_boundary])

    def test_matching_is_deterministic_one_to_one(self) -> None:
        first = [ReplayEntry(MINUTE, 100, None, None, None, None, "OPEN")]
        second = [ReplayEntry(MINUTE + 30_000, 100, None, None, None, None, "OPEN")]
        common, only_first, only_second = entry_overlap(first, second, MATCH_TOLERANCE_MS)
        self.assertEqual(len(common), 1)
        self.assertFalse(only_first)
        self.assertFalse(only_second)

    def test_initial_state_shifts_to_first_flat_minute(self) -> None:
        records = [{"opened_at": _iso(0), "closed_at": _iso(3 * MINUTE)}]
        start, fidelity = find_equivalent_flat_start(MINUTE, 10 * MINUTE, records, [])
        self.assertEqual(start, 3 * MINUTE)
        self.assertEqual(fidelity, "SHIFTED_TO_FIRST_OBSERVED_EMPTY_MINUTE")

    def test_core_replay_does_not_write_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            before = set(Path(directory).iterdir())
            run_universe(
                name="READ_ONLY", lookback=6, config=_config(), signals=[],
                execution_candles=[], start_ms=0, end_ms=MINUTE,
                intrabar_path="HIGH_FIRST", round_trip_spread_bps=0,
            )
            self.assertEqual(before, set(Path(directory).iterdir()))

    def test_historical_loader_supports_5m_and_reuses_cache(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, path: str, params: dict):
                self.calls += 1
                self.assertions = (path, params["symbol"], params["interval"])
                duration = 5 * MINUTE
                return [
                    [
                        open_ms, "100", "101", "99", "100", "10",
                        open_ms + duration - 1, "1000", 10,
                    ]
                    for open_ms in range(
                        int(params["startTime"]),
                        int(params["endTime"]) + 1,
                        duration,
                    )
                ]

        with tempfile.TemporaryDirectory() as directory:
            client = FakeClient()
            candles = load_ge_market_data(
                client, "SOLUSDT", "5m", 0, 15 * MINUTE - 1,
                Path(directory), offline=False,  # type: ignore[arg-type]
            )
            self.assertEqual(len(candles), 3)
            self.assertEqual(client.calls, 1)
            self.assertEqual(client.assertions, ("/api/v3/klines", "SOLUSDT", "5m"))
            cached = load_ge_market_data(
                client, "SOLUSDT", "5m", 0, 15 * MINUTE - 1,
                Path(directory), offline=True,  # type: ignore[arg-type]
            )
            self.assertEqual(len(cached), 3)
            self.assertEqual(client.calls, 1)


def _config() -> dict:
    return {
        "symbol": "SOLUSDT",
        "active_profile": "intraday",
        "trend": {"timeframe": "15m", "ema_period": 2, "ema_slope_lookback": 1},
        "trend_gate": {"mode": "ge30", "candle_interval": "5m", "lookback_candles": 6},
        "entry": {
            "timeframe": "1m", "lookback_candles": 2, "atr_period": 2,
            "pullback_atr_multiplier": 100, "rsi_period": 2, "rsi_threshold": 55,
            "rsi_lookback_candles": 1, "volume_ma_candles": 2,
            "require_volume_drying": False, "require_reversal_candle_closed": True,
            "max_entries_per_candle": 1, "entry_spacing_atr": 0,
        },
        "capital": {"operational_balance_usdt": 100, "trade_size_pct": 20, "max_open_positions": 5},
        "fees": {"enabled": True, "taker_fee_pct": 0.1, "use_bnb_discount": False},
        "risk": {
            "hard_stop": {"enabled": True, "stop_pct": 2}, "review_stop_pct": 30,
            "breakeven": {"mode": "atr", "trigger_atr": 100, "offset_atr": 0.1},
            "profit_lock": {"mode": "atr", "steps": [{"trigger_atr": 100, "lock_atr": 1}]},
            "trailing": {"mode": "atr", "activation_atr": 100, "gap_atr": 5},
        },
        "ladder": {},
    }


def _signal(minute: int, price: float) -> SignalEvent:
    return SignalEvent(
        minute * MINUTE,
        EntrySignal("SOLUSDT", price, _iso(minute * MINUTE), (minute - 1) * MINUTE, 1, "1m", 2),
    )


def _minute_candle(minute: int, price: float) -> MarketCandle:
    return _candle(minute, 1, price + 0.1, price - 0.1, price)


def _candle(start_minute: int, duration_minutes: int, high: float, low: float, close: float) -> MarketCandle:
    return MarketCandle(
        open_time_ms=start_minute * MINUTE,
        close_time_ms=(start_minute + duration_minutes) * MINUTE - 1,
        open=close, high=high, low=low, close=close, quote_volume=1000, trades=10,
    )


def _iso(value_ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value_ms / 1000, tz=timezone.utc).isoformat()


if __name__ == "__main__":
    unittest.main()
