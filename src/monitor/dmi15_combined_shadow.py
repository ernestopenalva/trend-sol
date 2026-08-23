from __future__ import annotations

from typing import Any, Dict

from src.monitor.dmi15_shadow import Dmi15ShadowRegistry, _number


class Dmi15CombinedShadowRegistry(Dmi15ShadowRegistry):
    """Independent DMI15 shadow: spread, trajectory, then RSI-MA gates in that order."""

    STRATEGY = "DMI15_COMBINED_SHADOW_G"
    SHADOW_KIND = "DMI15_COMBINED_SHADOW"
    SETTINGS_KEY = "dmi15_combined_shadow"
    LEDGER_APPEND_METHOD = "append_closed_dmi15_combined_shadow_trade"
    TELEMETRY_STREAM = "dmi15_combined_shadow_event"
    PAIR_PREFIX = "dmi15c"
    TRACK_REQUIRED_INDICATOR_UNAVAILABLE = True

    def _passes_additional_entry_gate(
        self, bucket: int, dmi_spread: float, snapshot: Dict[str, Any]
    ) -> bool:
        minimum_spread = float(self.settings.get("min_di_spread", 6.0))
        if dmi_spread < minimum_spread:
            self.blocked_spread += 1
            self._event("ENTRY_BLOCKED_DMI_SPREAD", bucket, dmi_spread=dmi_spread, min_di_spread=minimum_spread)
            return False
        plus_now = _number(snapshot.get("plus_di14"))
        plus_previous = _number(snapshot.get("plus_di14_5m_ago"))
        minus_now = _number(snapshot.get("minus_di14"))
        minus_previous = _number(snapshot.get("minus_di14_5m_ago"))
        if None in (plus_now, plus_previous, minus_now, minus_previous):
            self._record_required_indicator_unavailable(
                bucket, ("plus_di14", "plus_di14_5m_ago", "minus_di14", "minus_di14_5m_ago")
            )
            return False
        if not (
            plus_now > plus_previous and minus_now < minus_previous
        ):
            self.blocked_trajectory += 1
            self._event("ENTRY_BLOCKED_DMI_TRAJECTORY", bucket, plus_di_now=plus_now, plus_di_5m_ago=plus_previous, minus_di_now=minus_now, minus_di_5m_ago=minus_previous)
            return False
        rsi_ma = _number(snapshot.get("rsi14_sma14"))
        maximum_rsi_ma = float(self.settings.get("max_rsi_ma", 70.0))
        if rsi_ma is None:
            self._record_required_indicator_unavailable(bucket, ("rsi14_sma14",))
            return False
        if rsi_ma > maximum_rsi_ma:
            self.blocked_rsi_ma += 1
            self._event("ENTRY_BLOCKED_RSI_MA", bucket, rsi_ma=rsi_ma, max_rsi_ma=maximum_rsi_ma)
            return False
        return True
