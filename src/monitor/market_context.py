from __future__ import annotations

from typing import Any, Dict, Optional

from src.indicators.indicators import dmi_adx, ema, rsi
from src.logging_utils import now_iso
from src.monitor.entry_engine import Candle, EntryEngine


class MarketContextEngine:
    """Telemetry-only market context built exclusively from closed candles."""

    def __init__(self, entry_engine: EntryEngine, config: Dict[str, Any]) -> None:
        self.entry_engine = entry_engine
        settings = config.get("instrumentation", {}).get("market_context", {})
        self.settings = settings if isinstance(settings, dict) else {}
        self.enabled = bool(self.settings.get("enabled", False))
        self.latest: Optional[Dict[str, Any]] = None

    def refresh(self) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return None
        timeframes: Dict[str, Any] = {}
        for timeframe in ("5m", "15m"):
            timeframes[f"tf_{timeframe}"] = self._timeframe_snapshot(
                self.entry_engine._candles_for(timeframe),
                timeframe,
            )
        gate = self.entry_engine.config.get("trend_gate", {})
        ge_interval = str(gate.get("candle_interval", "5m"))
        lookback = int(gate.get("lookback_candles", 3))
        ge_candles = self.entry_engine._candles_for(ge_interval)
        ge15: Dict[str, Any] = {
            "interval": ge_interval,
            "lookback_candles": lookback,
            "status": "UNAVAILABLE",
        }
        if len(ge_candles) >= lookback + 1:
            latest = ge_candles[-1]
            reference = ge_candles[-1 - lookback]
            high_up = latest.high > reference.high
            low_up = latest.low > reference.low
            ge15.update(
                {
                    "status": "PASS" if high_up and low_up else "BLOCK",
                    "high_direction": "UP" if high_up else "DOWN_OR_EQUAL",
                    "low_direction": "UP" if low_up else "DOWN_OR_EQUAL",
                    "latest_closed_at_ms": latest.close_time,
                }
            )
        self.latest = {
            "captured_at": now_iso(),
            **timeframes,
            "ge15": ge15,
            "telemetry_only": True,
        }
        return self.latest

    def _timeframe_snapshot(self, candles: list[Candle], timeframe: str) -> Dict[str, Any]:
        closed = [item for item in candles if item.closed]
        closes = [item.close for item in closed]
        highs = [item.high for item in closed]
        lows = [item.low for item in closed]
        volumes = [item.volume for item in closed]
        ema20 = ema(closes, int(self.settings.get("ema_fast_period", 20)))
        ema50 = ema(closes, int(self.settings.get("ema_slow_period", 50)))
        ema100 = ema(closes, int(self.settings.get("ema_long_period", 100)))
        rsi14 = rsi(closes, int(self.settings.get("rsi_period", 14)))
        rsi_ma = _rsi_based_ma(
            rsi14,
            str(self.settings.get("rsi_based_ma_type", "SMA")),
            int(self.settings.get("rsi_based_ma_period", 14)),
        ) if timeframe == "5m" else []
        plus_di, minus_di, adx = dmi_adx(
            highs, lows, closes, int(self.settings.get("adx_period", 14))
        )
        slope_lookback = max(1, int(self.settings.get("slope_lookback_candles", 3)))
        rvol_period = max(1, int(self.settings.get("relative_volume_period", 20)))
        fifteen_minute_lookback = {"5m": 3, "15m": 1}[timeframe]
        baseline = volumes[-1 - rvol_period : -1] if len(volumes) > rvol_period else []
        baseline_avg = sum(baseline) / len(baseline) if baseline else None
        latest_volume = volumes[-1] if volumes else None
        ema_values = {
            "ema20": _last(ema20),
            "ema20_t_minus_3": _lookback(ema20, 3),
            "ema50": _last(ema50),
            "ema50_t_minus_3": _lookback(ema50, 3),
            "ema100": _last(ema100),
            "ema100_t_minus_3": _lookback(ema100, 3),
        }
        for label, values in (("ema20", ema20), ("ema50", ema50), ("ema100", ema100)):
            current, previous = _last(values), _lookback(values, 3)
            ema_values[f"{label}_delta_pct"] = _delta_pct(current, previous)
            ema_values[f"{label}_rising"] = (current > previous) if current is not None and previous is not None else None
        rising_values = [ema_values[f"{label}_rising"] for label in ("ema20", "ema50", "ema100")]
        if any(value is None for value in rising_values):
            score, trend_label = None, "UNAVAILABLE"
        else:
            rising_count = sum(value is True for value in rising_values)
            score, trend_label = {0: (0.0, "FALLING"), 1: (3.3, "MOSTLY_FALLING"), 2: (6.7, "MOSTLY_RISING"), 3: (10.0, "RISING")}[rising_count]
        return {
            "latest_open_at_ms": closed[-1].open_time if closed else None,
            "latest_closed_at_ms": closed[-1].close_time if closed else None,
            "close": closes[-1] if closes else None,
            **ema_values,
            "ema_trend_score": score,
            "ema_trend_label": trend_label,
            "ema20_slope_pct": _slope_pct(ema20, slope_lookback),
            "ema50_slope_pct": _slope_pct(ema50, slope_lookback),
            "adx14": _last(adx),
            "plus_di14": _last(plus_di),
            "minus_di14": _last(minus_di),
            "plus_di14_5m_ago": _lookback(plus_di, 1) if timeframe == "5m" else None,
            "minus_di14_5m_ago": _lookback(minus_di, 1) if timeframe == "5m" else None,
            "rsi14": _last(rsi14),
            "rsi14_sma14": _last(rsi_ma),
            "plus_di14_15m_ago": _lookback(plus_di, fifteen_minute_lookback),
            "minus_di14_15m_ago": _lookback(minus_di, fifteen_minute_lookback),
            "rsi14_15m_ago": _lookback(rsi14, fifteen_minute_lookback),
            "relative_volume": (
                latest_volume / baseline_avg
                if latest_volume is not None and baseline_avg not in (None, 0)
                else None
            ),
            "relative_volume_baseline": "mean of previous closed candles",
            "relative_volume_period": rvol_period,
            "slope_lookback_candles": slope_lookback,
            "candles": len(closed),
        }


def _last(values: list[Optional[float]]) -> Optional[float]:
    return float(values[-1]) if values and values[-1] is not None else None


def _lookback(values: list[Optional[float]], candles: int) -> Optional[float]:
    if len(values) <= candles or values[-1 - candles] is None:
        return None
    return float(values[-1 - candles])


def _slope_pct(values: list[Optional[float]], lookback: int) -> Optional[float]:
    if len(values) <= lookback:
        return None
    current = values[-1]
    previous = values[-1 - lookback]
    if current is None or previous in (None, 0):
        return None
    return (float(current) / float(previous) - 1) * 100


def _delta_pct(current: Optional[float], previous: Optional[float]) -> Optional[float]:
    if current is None or previous in (None, 0):
        return None
    return (float(current) / float(previous) - 1) * 100


def _rsi_based_ma(
    values: list[Optional[float]],
    ma_type: str,
    period: int,
) -> list[Optional[float]]:
    if ma_type.upper() != "SMA":
        raise ValueError(f"Unsupported RSI-based MA type: {ma_type}")
    if period <= 0:
        raise ValueError("RSI-based MA period must be positive")
    output: list[Optional[float]] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        if any(value is None for value in window):
            continue
        output[index] = sum(float(value) for value in window if value is not None) / period
    return output
