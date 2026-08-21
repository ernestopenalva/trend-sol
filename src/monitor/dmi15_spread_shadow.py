from __future__ import annotations

from src.monitor.dmi15_shadow import Dmi15ShadowRegistry


class Dmi15SpreadShadowRegistry(Dmi15ShadowRegistry):
    """Independent DMI15 shadow that additionally requires +DI minus -DI >= YAML minimum."""

    STRATEGY = "DMI15_SPREAD6_SHADOW_D"
    SHADOW_KIND = "DMI15_SPREAD6_SHADOW"
    SETTINGS_KEY = "dmi15_spread_shadow"
    LEDGER_APPEND_METHOD = "append_closed_dmi15_spread_shadow_trade"
    TELEMETRY_STREAM = "dmi15_spread_shadow_event"
    PAIR_PREFIX = "dmi15s6"

    def _passes_additional_entry_gate(self, bucket: int, dmi_spread: float) -> bool:
        minimum = float(self.settings.get("min_di_spread", 6.0))
        if dmi_spread >= minimum:
            return True
        self.blocked_spread += 1
        self._event(
            "ENTRY_BLOCKED_DMI_SPREAD",
            bucket,
            dmi_spread=dmi_spread,
            min_di_spread=minimum,
        )
        return False
