from __future__ import annotations

import unittest

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
        "trend_gate": {"mode": "ge30", "candle_interval": "5m", "lookback_candles": 3},
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


class EntryEngineGe15Tests(unittest.TestCase):
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
