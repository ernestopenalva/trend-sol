from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.indicators.indicators import atr, ema, rsi, volume_ma
from src.logging_utils import JsonlLogger, now_iso


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
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
            close_time=int(kline["T"]),
            open=float(kline["o"]),
            high=float(kline["h"]),
            low=float(kline["l"]),
            close=float(kline["c"]),
            volume=float(kline["v"]),
            closed=bool(kline["x"]),
        )

    @classmethod
    def from_rest_kline(cls, kline: List[Any], now_ms: int) -> "Candle":
        close_time = int(kline[6])
        return cls(
            open_time=int(kline[0]),
            close_time=close_time,
            open=float(kline[1]),
            high=float(kline[2]),
            low=float(kline[3]),
            close=float(kline[4]),
            volume=float(kline[5]),
            closed=close_time <= now_ms,
        )


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    price: float
    ts: str
    source_candle_open_time: int
    entry_atr: Optional[float]
    atr_timeframe: str
    atr_period: int


class EntryEngine:
    def __init__(
        self,
        symbol: str,
        config: Dict[str, Any],
        logger: JsonlLogger,
        gate1_mode: Optional[str] = None,
    ) -> None:
        self.symbol = symbol
        self.config = config
        self.logger = logger
        self.trend_timeframe = str(config["trend"]["timeframe"])
        self.entry_timeframe = str(config["entry"]["timeframe"])
        self.gate1_mode = str(
            gate1_mode or config.get("trend_gate", {}).get("mode", "legacy_ema")
        ).lower()
        self.trend_candles: List[Candle] = []
        self.entry_candles: List[Candle] = []
        self.auxiliary_candles: Dict[str, List[Candle]] = {}
        self.last_evaluated_entry_open_time: Optional[int] = None
        self.last_diagnostic: Dict[str, Any] = self._empty_diagnostic()

    def required_timeframes(self) -> List[str]:
        timeframes = [self.trend_timeframe, self.entry_timeframe]
        trend_gate = self.config.get("trend_gate", {})
        if self.gate1_mode == "ge30":
            timeframes.append(str(trend_gate["candle_interval"]))
        observations = self.config.get("ema_observations", {})
        if bool(observations.get("enabled", False)):
            for variant in observations.get("variants", []):
                if isinstance(variant, dict) and variant.get("interval"):
                    timeframes.append(str(variant["interval"]))
        return list(dict.fromkeys(timeframes))

    def load_history(self, timeframe: str, klines: List[List[Any]], now_ms: int) -> None:
        candles = [Candle.from_rest_kline(kline, now_ms) for kline in klines]
        closed_candles = [candle for candle in candles if candle.closed]
        if timeframe == self.trend_timeframe:
            self.trend_candles = closed_candles[-300:]
        if timeframe == self.entry_timeframe:
            self.entry_candles = closed_candles[-300:]
        if timeframe not in (self.trend_timeframe, self.entry_timeframe):
            if timeframe not in self.required_timeframes():
                raise ValueError(f"unsupported timeframe for history: {timeframe}")
            self.auxiliary_candles[timeframe] = closed_candles[-300:]
        elif timeframe not in self.required_timeframes():
            raise ValueError(f"unsupported timeframe for history: {timeframe}")
        self.logger.system("historical_candles_loaded", timeframe=timeframe, candles=len(closed_candles))

    def on_kline(self, stream: str, payload: Dict[str, Any]) -> Optional[EntrySignal]:
        candle = Candle.from_binance_kline(payload)
        if not candle.closed:
            return None

        timeframe = stream.rsplit("@kline_", 1)[-1] if "@kline_" in stream else ""
        if timeframe not in self.required_timeframes():
            return None
        self._upsert(self._candles_for(timeframe), candle)
        if timeframe != self.entry_timeframe:
            return None
        if self.last_evaluated_entry_open_time == candle.open_time:
            return None
        self.last_evaluated_entry_open_time = candle.open_time
        return self.evaluate()

    def evaluate(self) -> Optional[EntrySignal]:
        self.last_diagnostic = self._empty_diagnostic()
        ema_observations = self._ema_observations()
        if ema_observations is not None:
            self.last_diagnostic["ema_observations"] = ema_observations
            self.logger.decision(
                {
                    "ts": now_iso(),
                    "gate": 0,
                    "passed": None,
                    "near_miss": False,
                    "reason": "ema_observations",
                    "ema_observations": ema_observations,
                }
            )
        if not self._gate_trend():
            return None
        if not self._gate_pullback():
            return None
        if not self._gate_exhaustion():
            return None
        if not self._gate_reversal():
            return None

        latest = self.entry_candles[-1]
        entry_atr = self._current_entry_atr()
        atr_period = int(self.config["entry"]["atr_period"])
        self.last_diagnostic["last_reason"] = "buy_signal"
        self.logger.decision(
            {
                "ts": now_iso(),
                "gate": 5,
                "passed": True,
                "near_miss": False,
                "reason": "buy_signal",
                "price": latest.close,
                "entry_atr": entry_atr,
                "atr_timeframe": self.entry_timeframe,
                "atr_period": atr_period,
                "ema_observations": ema_observations,
            }
        )
        return EntrySignal(
            self.symbol,
            latest.close,
            now_iso(),
            latest.open_time,
            entry_atr,
            self.entry_timeframe,
            atr_period,
        )

    def set_paused(self, reason: str) -> None:
        self.last_diagnostic = self._empty_diagnostic()
        self.last_diagnostic["final_decision"] = reason
        self.last_diagnostic["last_reason"] = reason

    def _gate_trend(self) -> bool:
        if self.gate1_mode == "ge30":
            return self._gate_ge30()
        return self._gate_legacy_ema()

    def _gate_ge30(self) -> bool:
        gate_cfg = self.config["trend_gate"]
        interval = str(gate_cfg["candle_interval"])
        lookback = int(gate_cfg["lookback_candles"])
        candles = self._candles_for(interval)
        if lookback < 1 or len(candles) < lookback + 1:
            self._log_gate(
                1,
                False,
                False,
                "insufficient_ge30_candles",
                candle_interval=interval,
                lookback_candles=lookback,
                candles=len(candles),
                **{"GE30": "BLOCK"},
            )
            return False
        latest = candles[-1]
        reference = candles[-1 - lookback]
        high_passed = latest.high > reference.high
        low_passed = latest.low > reference.low
        passed = high_passed and low_passed
        self._log_gate(
            1,
            passed,
            False,
            "ge30",
            candle_interval=interval,
            lookback_candles=lookback,
            high_now=latest.high,
            high_lookback=reference.high,
            low_now=latest.low,
            low_lookback=reference.low,
            high_direction="UP" if high_passed else "DOWN_OR_EQUAL",
            low_direction="UP" if low_passed else "DOWN_OR_EQUAL",
            **{"GE30": "PASS" if passed else "BLOCK"},
        )
        return passed

    def _gate_legacy_ema(self) -> bool:
        trend_cfg = self.config["trend"]
        near_cfg = self.config["entry"].get("near_miss", {})
        period = int(trend_cfg["ema_period"])
        lookback = int(trend_cfg["ema_slope_lookback"])
        closes = [candle.close for candle in self.trend_candles]
        values = ema(closes, period)
        if len(values) <= period + lookback or values[-1] is None or values[-1 - lookback] is None:
            self._log_gate(
                1,
                False,
                False,
                "insufficient_trend_candles",
                timeframe=self.trend_timeframe,
                ema_period=period,
                candles=len(closes),
            )
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

    def _ema_observations(self) -> Optional[Dict[str, Any]]:
        settings = self.config.get("ema_observations", {})
        if not bool(settings.get("enabled", False)):
            return None
        window_minutes = int(settings["slope_window_minutes"])
        output: Dict[str, Any] = {}
        for variant in settings.get("variants", []):
            if not isinstance(variant, dict):
                continue
            period = int(variant["period"])
            interval = str(variant["interval"])
            interval_minutes = _interval_minutes(interval)
            reference_candles = max(1, round(window_minutes / interval_minutes)) if interval_minutes else 0
            key = f"ema{period}_{interval}"
            candles = self._candles_for(interval) if interval else []
            values = ema([candle.close for candle in candles], period) if period > 0 else []
            current = values[-1] if values else None
            previous = (
                values[-1 - reference_candles]
                if reference_candles > 0 and len(values) > reference_candles
                else None
            )
            slope_pct = (
                (float(current) / float(previous) - 1) * 100
                if current is not None and previous not in (None, 0)
                else None
            )
            output[key] = {
                "period": period,
                "interval": interval,
                "slope_window_minutes": window_minutes,
                "reference_candles": reference_candles,
                "current": float(current) if current is not None else None,
                "previous": float(previous) if previous is not None else None,
                "slope_pct": slope_pct,
                "direction": (
                    "UP" if slope_pct is not None and slope_pct > 0 else
                    "DOWN_OR_EQUAL" if slope_pct is not None else
                    "UNAVAILABLE"
                ),
                "candles": len(candles),
            }
        return output

    def _gate_pullback(self) -> bool:
        entry_cfg = self.config["entry"]
        near_cfg = entry_cfg.get("near_miss", {})
        lookback = int(entry_cfg["lookback_candles"])
        atr_period = int(entry_cfg["atr_period"])
        multiplier = float(entry_cfg["pullback_atr_multiplier"])
        if len(self.entry_candles) < max(lookback, atr_period) + 1:
            self._log_gate(2, False, False, "insufficient_entry_candles", candles=len(self.entry_candles))
            return False

        window = self.entry_candles[-lookback:]
        recent_high = max(candle.high for candle in window)
        latest = self.entry_candles[-1]
        atr_values = atr(
            [candle.high for candle in self.entry_candles],
            [candle.low for candle in self.entry_candles],
            [candle.close for candle in self.entry_candles],
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
        if len(self.entry_candles) < max(period + lookback + 1, volume_period + 1):
            self._log_gate(3, False, False, "insufficient_entry_candles", candles=len(self.entry_candles))
            return False

        closes = [candle.close for candle in self.entry_candles]
        rsi_values = rsi(closes, period)
        current = rsi_values[-1]
        previous = rsi_values[-1 - lookback]
        if current is None or previous is None:
            self._log_gate(3, False, False, "rsi_unavailable")
            return False

        volumes = [candle.volume for candle in self.entry_candles]
        volume_values = volume_ma(volumes[:-1], volume_period)
        prior_average = volume_values[-1]
        latest = self.entry_candles[-1]
        require_volume = bool(entry_cfg.get("require_volume_drying", True))
        volume_ok = (prior_average is not None and latest.volume < prior_average) if require_volume else True
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
            require_volume_drying=require_volume,
            volume_ok=volume_ok,
        )
        return passed

    def _gate_reversal(self) -> bool:
        if len(self.entry_candles) < 2:
            self._log_gate(4, False, False, "insufficient_entry_candles")
            return False
        previous = self.entry_candles[-2]
        latest = self.entry_candles[-1]
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
        gate_name = {1: "trend", 2: "pullback", 3: "exhaustion", 4: "recovery", 5: "buy"}.get(gate)
        if gate_name:
            self.last_diagnostic["gates"][gate_name] = {
                "passed": passed,
                "near_miss": near_miss,
                "reason": reason,
                **fields,
            }
            if not passed:
                self.last_diagnostic["last_reason"] = reason
                self.last_diagnostic["last_rejected_gate"] = gate_name
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

    def _current_entry_atr(self) -> Optional[float]:
        period = int(self.config["entry"]["atr_period"])
        values = atr(
            [candle.high for candle in self.entry_candles],
            [candle.low for candle in self.entry_candles],
            [candle.close for candle in self.entry_candles],
            period,
        )
        if not values or values[-1] is None:
            return None
        return float(values[-1])

    def _empty_diagnostic(self) -> Dict[str, Any]:
        return {
            "profile": self.config.get("active_profile", "production"),
            "trend_timeframe": self.trend_timeframe,
            "entry_timeframe": self.entry_timeframe,
            "gate1_mode": self.gate1_mode,
            "last_reason": "waiting_for_entry_candle",
            "last_rejected_gate": None,
            "gates": {
                "trend": {"passed": None, "reason": "not_evaluated"},
                "pullback": {"passed": None, "reason": "not_evaluated"},
                "exhaustion": {"passed": None, "reason": "not_evaluated"},
                "recovery": {"passed": None, "reason": "not_evaluated"},
            },
        }

    def _candles_for(self, timeframe: str) -> List[Candle]:
        if timeframe == self.entry_timeframe:
            return self.entry_candles
        if timeframe == self.trend_timeframe:
            return self.trend_candles
        return self.auxiliary_candles.setdefault(timeframe, [])

    @staticmethod
    def _upsert(candles: List[Candle], candle: Candle) -> None:
        if candles and candles[-1].open_time == candle.open_time:
            candles[-1] = candle
        else:
            candles.append(candle)
        del candles[:-300]


def _interval_minutes(interval: str) -> int:
    if interval.endswith("m"):
        try:
            return int(interval[:-1])
        except ValueError:
            return 0
    if interval.endswith("h"):
        try:
            return int(interval[:-1]) * 60
        except ValueError:
            return 0
    return 0
