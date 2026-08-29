from __future__ import annotations

from typing import Any, Dict

from src.monitor.context_predicates import dmi15_trajectory_values
from src.monitor.dmi15_shadow import Dmi15ShadowRegistry


class Dmi15TrajectoryShadowRegistry(Dmi15ShadowRegistry):
    """Independent DMI15 shadow requiring one-candle DI trajectory confirmation."""

    STRATEGY = "DMI15_TRAJECTORY_SHADOW_E"
    SHADOW_KIND = "DMI15_TRAJECTORY_SHADOW"
    SETTINGS_KEY = "dmi15_trajectory_shadow"
    LEDGER_APPEND_METHOD = "append_closed_dmi15_trajectory_shadow_trade"
    TELEMETRY_STREAM = "dmi15_trajectory_shadow_event"
    PAIR_PREFIX = "dmi15t"
    TRACK_REQUIRED_INDICATOR_UNAVAILABLE = True

    def _passes_additional_entry_gate(
        self, bucket: int, dmi_spread: float, snapshot: Dict[str, Any]
    ) -> bool:
        del dmi_spread
        values = dmi15_trajectory_values(snapshot)
        if values is None:
            self._record_required_indicator_unavailable(
                bucket, ("plus_di14", "plus_di14_5m_ago", "minus_di14", "minus_di14_5m_ago")
            )
            return False
        plus_now, plus_previous, minus_now, minus_previous = values
        if (
            plus_now > plus_previous and minus_now < minus_previous
        ):
            return True
        self.blocked_trajectory += 1
        self._event(
            "ENTRY_BLOCKED_DMI_TRAJECTORY",
            bucket,
            plus_di_now=plus_now,
            plus_di_5m_ago=plus_previous,
            minus_di_now=minus_now,
            minus_di_5m_ago=minus_previous,
        )
        return False
