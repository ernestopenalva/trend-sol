from __future__ import annotations

import unittest
from unittest.mock import Mock

from src.monitor.entry_engine import Candle, EntryEngine


class FakeLogger:
    def __init__(self) -> None:
        self.decisions = []
        self.systems = []

    def decision(self, event):
        self.decisions.append(event)

    def system(self, event, **fields):
        self.systems.append((event, fields))


def candle(index: int, high: float, low: float, close: float) -> Candle:
    width = 5 * 60 * 1000
    return Candle(
        open_time=index * width,
        close_time=(index + 1) * width - 1,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=100,
        closed=True,
    )


def config() -> dict:
    return {
        "active_profile": "intraday",
        "trend": {"timeframe": "15m", "ema_period": 50, "ema_slope_lookback": 3},
        "trend_gate": {
            "mode": "ge30",
            "candle_interval": "5m",
            "lookback_candles": 3,
            "sync": {
                "enabled": True,
                "timeout_seconds": 15,
                "expire_on_next_entry_candle": True,
                "timeout_action": "SKIP",
            },
        },
        "ema_observations": {
            "enabled": True,
            "slope_window_minutes": 30,
            "variants": [
                {"period": 50, "interval": "15m"},
                {"period": 20, "interval": "15m"},
                {"period": 50, "interval": "5m"},
                {"period": 20, "interval": "5m"},
            ],
        },
        "entry": {
            "timeframe": "1m",
            "lookback_candles": 30,
            "atr_period": 14,
            "pullback_atr_multiplier": 0.8,
            "rsi_period": 14,
            "rsi_threshold": 55,
            "rsi_lookback_candles": 1,
            "volume_ma_candles": 5,
            "require_volume_drying": False,
        },
    }


def kline_payload(open_time: int, interval_ms: int) -> dict:
    return {
        "k": {
            "t": open_time,
            "T": open_time + interval_ms - 1,
            "o": "10",
            "h": "11",
            "l": "9",
            "c": "10",
            "v": "100",
            "x": True,
        }
    }


class EntryEngineGe15Tests(unittest.TestCase):
    def test_boundary_evaluates_immediately_when_5m_arrives_first(self) -> None:
        logger = FakeLogger()
        engine = EntryEngine("SOLUSDT", config(), logger)  # type: ignore[arg-type]
        engine.evaluate = Mock(return_value="signal")  # type: ignore[method-assign]

        self.assertIsNone(engine.on_kline("solusdt@kline_5m", kline_payload(0, 300_000)))
        result = engine.on_kline("solusdt@kline_1m", kline_payload(240_000, 60_000))

        self.assertEqual(result, "signal")
        engine.evaluate.assert_called_once_with()  # type: ignore[attr-defined]
        self.assertIsNone(engine.pending_ge_evaluation)
        self.assertEqual(engine.ge_sync_counters["fresh"], 1)
        self.assertEqual(logger.decisions[-1]["event"], "GE_CANDLE_FRESH")

    def test_boundary_waits_for_exact_5m_when_1m_arrives_first(self) -> None:
        logger = FakeLogger()
        clock = Mock(return_value=100.0)
        engine = EntryEngine(
            "SOLUSDT", config(), logger, monotonic_clock=clock  # type: ignore[arg-type]
        )
        engine.auxiliary_candles["5m"] = [candle(-1, 10, 5, 7)]
        engine.evaluate = Mock(return_value="signal")  # type: ignore[method-assign]

        self.assertIsNone(
            engine.on_kline("solusdt@kline_1m", kline_payload(240_000, 60_000))
        )
        self.assertIsNotNone(engine.pending_ge_evaluation)
        self.assertEqual(logger.decisions[-1]["event"], "GE_CANDLE_WAITING")
        clock.return_value = 103.0
        result = engine.on_kline("solusdt@kline_5m", kline_payload(0, 300_000))

        self.assertEqual(result, "signal")
        engine.evaluate.assert_called_once_with()  # type: ignore[attr-defined]
        self.assertEqual(engine.ge_sync_counters["released"], 1)
        self.assertEqual(logger.decisions[-1]["event"], "GE_CANDLE_READY")
        self.assertEqual(logger.decisions[-1]["waited_seconds"], 3.0)
        self.assertIsNone(
            engine.on_kline("solusdt@kline_5m", kline_payload(0, 300_000))
        )
        engine.evaluate.assert_called_once_with()  # type: ignore[attr-defined]

    def test_late_5m_discards_signal_instead_of_using_stale_candle(self) -> None:
        logger = FakeLogger()
        clock = Mock(return_value=100.0)
        engine = EntryEngine(
            "SOLUSDT", config(), logger, monotonic_clock=clock  # type: ignore[arg-type]
        )
        engine.auxiliary_candles["5m"] = [candle(-1, 10, 5, 7)]
        engine.evaluate = Mock(return_value="signal")  # type: ignore[method-assign]
        engine.on_kline("solusdt@kline_1m", kline_payload(240_000, 60_000))

        clock.return_value = 116.0
        result = engine.on_kline("solusdt@kline_5m", kline_payload(0, 300_000))

        self.assertIsNone(result)
        engine.evaluate.assert_not_called()  # type: ignore[attr-defined]
        self.assertEqual(engine.ge_sync_counters["timeout"], 1)
        self.assertEqual(logger.decisions[-1]["event"], "GE_CANDLE_TIMEOUT")
        self.assertEqual(logger.decisions[-1]["action"], "SKIP")

    def test_next_1m_expires_pending_when_it_arrives_before_long_timeout(self) -> None:
        logger = FakeLogger()
        settings = config()
        settings["trend_gate"]["sync"]["timeout_seconds"] = 120
        clock = Mock(return_value=100.0)
        engine = EntryEngine(
            "SOLUSDT", settings, logger, monotonic_clock=clock  # type: ignore[arg-type]
        )
        engine.auxiliary_candles["5m"] = [candle(-1, 10, 5, 7)]
        engine.evaluate = Mock(return_value=None)  # type: ignore[method-assign]
        engine.on_kline("solusdt@kline_1m", kline_payload(240_000, 60_000))

        clock.return_value = 160.0
        engine.on_kline("solusdt@kline_1m", kline_payload(300_000, 60_000))

        self.assertEqual(engine.ge_sync_counters["expired_next_entry_candle"], 1)
        self.assertTrue(
            any(event.get("event") == "GE_CANDLE_EXPIRED_NEXT_1M" for event in logger.decisions)
        )

    def test_ge15_compares_last_closed_candle_with_exactly_three_intervals_back(self) -> None:
        logger = FakeLogger()
        engine = EntryEngine("SOLUSDT", config(), logger)  # type: ignore[arg-type]
        bars = [candle(index, 10, 5, 7) for index in range(4)]
        bars[1] = candle(1, 99, 1, 7)  # Prova que nao usa o item anterior errado.
        bars[-1] = candle(3, 11, 6, 8)
        engine.auxiliary_candles["5m"] = bars

        self.assertTrue(engine._gate_trend())

        event = logger.decisions[-1]
        self.assertEqual(event["high_now"], 11)
        self.assertEqual(event["high_lookback"], 10)
        self.assertEqual(event["low_now"], 6)
        self.assertEqual(event["low_lookback"], 5)
        self.assertEqual(event["lookback_candles"], 3)
        self.assertEqual(event["lookback_minutes"], 15)
        self.assertEqual(event["ge_label"], "GE15")
        self.assertEqual(event["GE15"], "PASS")

    def test_ge15_requires_strictly_higher_high_and_low(self) -> None:
        logger = FakeLogger()
        engine = EntryEngine("SOLUSDT", config(), logger)  # type: ignore[arg-type]
        bars = [candle(index, 10, 5, 7) for index in range(4)]
        bars[-1] = candle(3, 11, 5, 8)
        engine.auxiliary_candles["5m"] = bars

        self.assertFalse(engine._gate_trend())

        event = logger.decisions[-1]
        self.assertEqual(event["high_direction"], "UP")
        self.assertEqual(event["low_direction"], "DOWN_OR_EQUAL")
        self.assertEqual(event["GE15"], "BLOCK")

        logger = FakeLogger()
        engine = EntryEngine("SOLUSDT", config(), logger)  # type: ignore[arg-type]
        bars[-1] = candle(3, 10, 6, 8)
        engine.auxiliary_candles["5m"] = bars
        self.assertFalse(engine._gate_trend())
        self.assertEqual(logger.decisions[-1]["GE15"], "BLOCK")

    def test_four_ema_variants_are_observation_only_with_30_minute_references(self) -> None:
        logger = FakeLogger()
        engine = EntryEngine("SOLUSDT", config(), logger)  # type: ignore[arg-type]
        engine.trend_candles = [
            candle(index, 101 + index, 99 + index, 100 + index)
            for index in range(60)
        ]
        engine.auxiliary_candles["5m"] = [
            candle(index, 101 + index, 99 + index, 100 + index)
            for index in range(60)
        ]

        observations = engine._ema_observations()

        self.assertEqual(
            set(observations or {}),
            {"ema50_15m", "ema20_15m", "ema50_5m", "ema20_5m"},
        )
        self.assertEqual(observations["ema50_15m"]["reference_candles"], 2)  # type: ignore[index]
        self.assertEqual(observations["ema50_5m"]["reference_candles"], 6)  # type: ignore[index]
        self.assertGreater(observations["ema20_5m"]["slope_pct"], 0)  # type: ignore[index]
        self.assertEqual(engine.gate1_mode, "ge30")

        engine.evaluate()
        telemetry_events = [
            event for event in logger.decisions if event.get("reason") == "ema_observations"
        ]
        self.assertEqual(len(telemetry_events), 1)
        self.assertEqual(set(telemetry_events[0]["ema_observations"]), set(observations or {}))


if __name__ == "__main__":
    unittest.main()
