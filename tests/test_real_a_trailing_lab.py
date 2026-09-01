from datetime import datetime, timedelta, timezone
import unittest

from tools.real_a_exit_simulator import Seed, Tick
from tools.real_a_trailing_lab import Candle1m, CurrentAtrSeries, _position


class RealATrailingLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 8, 16, tzinfo=timezone.utc)
        candles = [Candle1m(self.start + timedelta(minutes=i), self.start + timedelta(minutes=i + 1) - timedelta(milliseconds=1), 101, 99, 100) for i in range(30)]
        self.series = CurrentAtrSeries(candles, 14)
        self.seed = Seed("p", "SOLUSDT", self.start + timedelta(minutes=20), 100, 1, "TRAILING", 0, self.start + timedelta(minutes=21), False, None, None)
        self.config = {"hard_stop": {"enabled": True, "stop_pct": 1.5}, "breakeven": {"mode": "atr", "trigger_atr": 3, "offset_atr": .1}, "profit_lock": {"mode": "atr", "economic_floor": {"enabled": False}, "steps": []}, "trailing": {"mode": "atr", "activation_atr": 10, "gap_atr": 5}, "fees": {"enabled": False}, "ladder": {"be_activation_buffer_atr": .5}}

    def test_fractional_stop_uses_highest_gain(self) -> None:
        position = _position(self.seed, self.config, "FRACTIONAL_30", self.series)
        position._lab_seed = self.seed
        position.highest_price = 120
        self.assertEqual(position._current_trailing_stop(), 114)

    def test_current_atr_uses_last_closed_candle_not_future(self) -> None:
        position = _position(self.seed, self.config, "CURRENT_ATR_5", self.series)
        position._lab_seed = self.seed
        position.highest_price = 120
        position.on_lab_tick(Tick(self.start + timedelta(minutes=21), 110))
        self.assertIsNotNone(position.last_current_atr)
        self.assertEqual(position._current_trailing_stop(), 110)

