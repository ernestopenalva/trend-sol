from __future__ import annotations

from statistics import median
from typing import Any, Dict, Iterable


def resolved_no_progress_tolerance(
    closed_trades: Iterable[Dict[str, Any]],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve the tolerance from the strict recent closed-trade window."""
    default_seconds = max(0.0, float(settings.get("default_hours", 2)) * 3600)
    window = max(1, int(settings.get("rolling_window", 20)))
    minimum = max(1, int(settings.get("min_be_samples", 4)))
    buffer_pct = max(0.0, float(settings.get("tolerance_buffer_pct", 25)))
    recent = list(closed_trades)[-window:]
    samples = []
    for trade in recent:
        value = trade.get("time_to_be_seconds")
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if trade.get("be_armed_at") and seconds >= 0:
            samples.append(seconds)
    if len(samples) < minimum:
        return {
            "seconds": default_seconds,
            "source": "DEFAULT",
            "sample_size": len(samples),
            "window_trade_count": len(recent),
            "median_seconds": None,
        }
    median_seconds = float(median(samples))
    return {
        "seconds": median_seconds * (1 + buffer_pct / 100),
        "source": "ROLLING_MEDIAN",
        "sample_size": len(samples),
        "window_trade_count": len(recent),
        "median_seconds": median_seconds,
    }
