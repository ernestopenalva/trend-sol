from __future__ import annotations

from typing import Any, Dict

from src.monitor.dmi15_shadow import Dmi15ShadowRegistry, _number


class Dmi15Rsi70ShadowRegistry(Dmi15ShadowRegistry):
    """Independent DMI15 shadow blocked only above the closed 5m RSI-MA maximum."""

    STRATEGY = "DMI15_RSI70_SHADOW_F"
    SHADOW_KIND = "DMI15_RSI70_SHADOW"
    SETTINGS_KEY = "dmi15_rsi70_shadow"
    LEDGER_APPEND_METHOD = "append_closed_dmi15_rsi70_shadow_trade"
    TELEMETRY_STREAM = "dmi15_rsi70_shadow_event"
    PAIR_PREFIX = "dmi15r70"
    TRACK_REQUIRED_INDICATOR_UNAVAILABLE = True

    def _passes_additional_entry_gate(
        self, bucket: int, dmi_spread: float, snapshot: Dict[str, Any]
    ) -> bool:
        del dmi_spread
        rsi_ma = _number(snapshot.get("rsi14_sma14"))
        maximum = float(self.settings.get("max_rsi_ma", 70.0))
        if rsi_ma is None:
            self._record_required_indicator_unavailable(bucket, ("rsi14_sma14",))
            return False
        if rsi_ma <= maximum:
            return True
        self.blocked_rsi_ma += 1
        self._event("ENTRY_BLOCKED_RSI_MA", bucket, rsi_ma=rsi_ma, max_rsi_ma=maximum)
        return False
