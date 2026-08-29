"""Shared, closed-candle predicates used by context-only shadows and studies."""
from __future__ import annotations

from typing import Any, Mapping, Sequence


def passes_ge_structure(candles: Sequence[Any], lookback_candles: int) -> bool:
    """Return the strict high-and-low geometry of the latest closed candles."""
    if lookback_candles < 1 or len(candles) < lookback_candles + 1:
        return False
    latest, reference = candles[-1], candles[-1 - lookback_candles]
    return bool(latest.high > reference.high and latest.low > reference.low)


def passes_slow_ge45(candles_15m: Sequence[Any]) -> bool:
    """GE45: high_15m[t] > high_15m[t-3] AND low_15m[t] > low_15m[t-3]."""
    return passes_ge_structure(candles_15m, 3)


def dmi15_trajectory_values(snapshot: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """Return +DI[t], +DI[t-1], -DI[t], -DI[t-1] or None when unavailable."""
    values = (
        _number(snapshot.get("plus_di14")),
        _number(snapshot.get("plus_di14_5m_ago")),
        _number(snapshot.get("minus_di14")),
        _number(snapshot.get("minus_di14_5m_ago")),
    )
    return values if all(value is not None for value in values) else None  # type: ignore[return-value]


def passes_dmi15_trajectory(snapshot: Mapping[str, Any]) -> bool | None:
    """DMI15 plus the one-closed-5m-candle trajectory confirmation.

    ``None`` means the required closed-candle indicator context is unavailable.
    """
    values = dmi15_trajectory_values(snapshot)
    if values is None:
        return None
    plus_now, plus_previous, minus_now, minus_previous = values
    plus_15m_ago = _number(snapshot.get("plus_di14_15m_ago"))
    minus_15m_ago = _number(snapshot.get("minus_di14_15m_ago"))
    if plus_15m_ago is None or minus_15m_ago is None:
        return None
    return (
        plus_now > plus_15m_ago
        and minus_now < minus_15m_ago
        and plus_now > minus_now
        and plus_now > plus_previous
        and minus_now < minus_previous
    )


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
