from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.indicators.indicators import atr, ema, rsi, volume_ma
from src.logging_utils import JsonlLogger, now_iso


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool

    @classmethod
    def from_binance_kline(cls, payload: Dict[str, Any]) -> "Candle":
        kline = payload["k"]
        return cls(
            open_time=int(kline["t"]),
            open=float(kline["o"]),
            high=float(kline["h"]),
            low=float(kline["l"]),
            close=float(kline["c"]),
            volume=float(kline["v"]),
            closed=bool(kline["x"]),
        )


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    price: float
    ts: str
    source_candle_open_time: int


class EntryEngine:
    def __init__(self, symbol: str, config: Dict[str, Any], logger: JsonlLogger) -> None:
        self.symbol = symbol
        self.config = config
        self.logger = logger
        self.daily_candles: List[Candle] = []
        self.four_hour_candles: List[Candle] = []
        self.last_evaluated_4h_open_time: Optional[int] = None

    def on_kline(self, stream: str, payload: Dict[str, Any]) -> Optional[EntrySignal]:
        candle = Candle.from_binance_kline(payload)
        if not candle.closed:
            return None

        if stream.endswith("@kline_1d"):
            self._upsert(self.daily_candles, candle)
            return None
        if not stream.endswith("@kline_4h"):
            return None

        self._upsert(self.four_hour_candles, candle)
        if self.last_evaluated_4h_open_time == candle.open_time:
            return None
        self.last_evaluated_4h_open_time = candle.open_time
        return self.evaluate()

    def evaluate(self) -> Optional[EntrySignal]:
        if not self._gate_trend():
            return None
        if not self._gate_pullback():
            return None
        if not self._gate_exhaustion():
            return None
        if not self._gate_reversal():
            return None

        latest = self.four_hour_candles[-1]
        self.logger.decision(
            {
                "ts": now_iso(),
                "gate": 5,
                "passed": True,
                "near_miss": False,
                "reason": "buy_signal",
                "price": latest.close,
            }
        )
        return EntrySignal(self.symbol, latest.close, now_iso(), latest.open_time)

    def _gate_trend(self) -> bool:
        trend_cfg = self.config["trend"]
        near_cfg = self.config["entry"].get("near_miss", {})
        period = int(trend_cfg["ema_period"])
        lookback = int(trend_cfg["ema_slope_lookback"])
        closes = [candle.close for candle in self.daily_candles]
        values = ema(closes, period)
        if len(values) <= period + lookback or values[-1] is None or values[-1 - lookback] is None:
            self._log_gate(1, False, False, "insufficient_daily_candles", ema_period=period, candles=len(closes))
            return False

        current = float(values[-1])
        previous = float(values[-1 - lookback])
        passed = current > previous
        near = (
            not passed
            and abs(current - previous) / previous <= float(near_cfg.get("trend_slope_ratio", 0.002))
            if previous
            else False
        )
        self._log_gate(1, passed, near, "ema_slope", ema_current=current, ema_previous=previous)
        return passed

    def _gate_pullback(self) -> bool:
        entry_cfg = self.config["entry"]
        near_cfg = entry_cfg.get("near_miss", {})
        lookback = int(entry_cfg["lookback_candles"])
        atr_period = int(entry_cfg["atr_period"])
        multiplier = float(entry_cfg["pullback_atr_multiplier"])
        if len(self.four_hour_candles) < max(lookback, atr_period) + 1:
            self._log_gate(2, False, False, "insufficient_4h_candles", candles=len(self.four_hour_candles))
            return False

        window = self.four_hour_candles[-lookback:]
        recent_high = max(candle.high for candle in window)
        latest = self.four_hour_candles[-1]
        atr_values = atr(
            [candle.high for candle in self.four_hour_candles],
            [candle.low for candle in self.four_hour_candles],
            [candle.close for candle in self.four_hour_candles],
            atr_period,
        )
        current_atr = atr_values[-1]
        if current_atr is None:
            self._log_gate(2, False, False, "atr_unavailable")
            return False

        pullback_abs = recent_high - latest.close
        required_abs = float(current_atr) * multiplier
        passed = pullback_abs >= required_abs
        near = not passed and pullback_abs >= required_abs * float(near_cfg.get("pullback_required_ratio", 0.85))
        self._log_gate(
            2,
            passed,
            near,
            "pullback",
            recent_high=recent_high,
            close=latest.close,
            pullback_abs=pullback_abs,
            required_abs=required_abs,
            atr=current_atr,
        )
        return passed

    def _gate_exhaustion(self) -> bool:
        entry_cfg = self.config["entry"]
        near_cfg = entry_cfg.get("near_miss", {})
        period = int(entry_cfg["rsi_period"])
        threshold = float(entry_cfg["rsi_threshold"])
        lookback = int(entry_cfg["rsi_lookback_candles"])
        volume_period = int(entry_cfg["volume_ma_candles"])
        if len(self.four_hour_candles) < max(period + lookback + 1, volume_period + 1):
            self._log_gate(3, False, False, "insufficient_4h_candles", candles=len(self.four_hour_candles))
            return False

        closes = [candle.close for candle in self.four_hour_candles]
        rsi_values = rsi(closes, period)
        current = rsi_values[-1]
        previous = rsi_values[-1 - lookback]
        if current is None or previous is None:
            self._log_gate(3, False, False, "rsi_unavailable")
            return False

        volumes = [candle.volume for candle in self.four_hour_candles]
        volume_values = volume_ma(volumes[:-1], volume_period)
        prior_average = volume_values[-1]
        latest = self.four_hour_candles[-1]
        volume_ok = prior_average is not None and latest.volume < prior_average
        passed = current < threshold and current > previous and volume_ok
        near = not passed and current < threshold + float(near_cfg.get("rsi_margin", 3)) and current >= previous
        self._log_gate(
            3,
            passed,
            near,
            "exhaustion",
            rsi=current,
            rsi_previous=previous,
            rsi_threshold=threshold,
            volume=latest.volume,
            volume_average=prior_average,
            volume_ok=volume_ok,
        )
        return passed

    def _gate_reversal(self) -> bool:
        if len(self.four_hour_candles) < 2:
            self._log_gate(4, False, False, "insufficient_4h_candles")
            return False
        previous = self.four_hour_candles[-2]
        latest = self.four_hour_candles[-1]
        near_cfg = self.config["entry"].get("near_miss", {})
        candle_range = latest.high - latest.low
        close_position = (latest.close - latest.low) / candle_range if candle_range > 0 else 0.0
        passed = latest.close > previous.low and close_position >= (2 / 3)
        near = (
            not passed
            and latest.close > previous.low
            and close_position >= float(near_cfg.get("reversal_min_position", 0.58))
        )
        self._log_gate(
            4,
            passed,
            near,
            "reversal",
            previous_low=previous.low,
            close=latest.close,
            close_position_in_range=close_position,
            required=2 / 3,
        )
        return passed

    def _log_gate(self, gate: int, passed: bool, near_miss: bool, reason: str, **fields: Any) -> None:
        self.logger.decision(
            {
                "ts": now_iso(),
                "gate": gate,
                "passed": passed,
                "near_miss": near_miss,
                "reason": reason,
                **fields,
            }
        )

    @staticmethod
    def _upsert(candles: List[Candle], candle: Candle) -> None:
        if candles and candles[-1].open_time == candle.open_time:
            candles[-1] = candle
        else:
            candles.append(candle)
        del candles[:-300]
