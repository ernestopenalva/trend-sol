from __future__ import annotations

from typing import Iterable, List, Optional


def ema(values: Iterable[float], period: int) -> List[Optional[float]]:
    series = [float(value) for value in values]
    if period <= 0:
        raise ValueError("period must be positive")
    if not series:
        return []

    result: List[Optional[float]] = [None] * len(series)
    if len(series) < period:
        return result

    seed = sum(series[:period]) / period
    result[period - 1] = seed
    multiplier = 2 / (period + 1)

    previous = seed
    for index in range(period, len(series)):
        previous = (series[index] - previous) * multiplier + previous
        result[index] = previous
    return result


def atr(highs: Iterable[float], lows: Iterable[float], closes: Iterable[float], period: int) -> List[Optional[float]]:
    high_values = [float(value) for value in highs]
    low_values = [float(value) for value in lows]
    close_values = [float(value) for value in closes]
    _assert_same_length(high_values, low_values, close_values)
    if period <= 0:
        raise ValueError("period must be positive")
    if not high_values:
        return []

    true_ranges: List[float] = []
    for index, high in enumerate(high_values):
        low = low_values[index]
        if index == 0:
            true_ranges.append(high - low)
            continue
        previous_close = close_values[index - 1]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))

    result: List[Optional[float]] = [None] * len(true_ranges)
    if len(true_ranges) < period:
        return result

    current = sum(true_ranges[:period]) / period
    result[period - 1] = current
    for index in range(period, len(true_ranges)):
        current = ((current * (period - 1)) + true_ranges[index]) / period
        result[index] = current
    return result


def rsi(values: Iterable[float], period: int) -> List[Optional[float]]:
    closes = [float(value) for value in values]
    if period <= 0:
        raise ValueError("period must be positive")
    result: List[Optional[float]] = [None] * len(closes)
    if len(closes) <= period:
        return result

    gains: List[float] = []
    losses: List[float] = []
    for index in range(1, period + 1):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period
    result[period] = _rsi_from_averages(average_gain, average_loss)

    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
        result[index] = _rsi_from_averages(average_gain, average_loss)
    return result


def volume_ma(volumes: Iterable[float], period: int) -> List[Optional[float]]:
    series = [float(value) for value in volumes]
    if period <= 0:
        raise ValueError("period must be positive")
    result: List[Optional[float]] = [None] * len(series)
    for index in range(period - 1, len(series)):
        result[index] = sum(series[index - period + 1 : index + 1]) / period
    return result


def _rsi_from_averages(average_gain: float, average_loss: float) -> float:
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def _assert_same_length(*series: List[float]) -> None:
    lengths = {len(item) for item in series}
    if len(lengths) > 1:
        raise ValueError("series must have the same length")
