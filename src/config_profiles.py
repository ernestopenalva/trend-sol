from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


def effective_config(raw_config: Dict[str, Any]) -> Dict[str, Any]:
    config = deepcopy(raw_config)
    active_profile = str(config.get("active_profile", "production"))
    profiles = config.get("profiles") or {}
    profile = profiles.get(active_profile)
    if not isinstance(profile, dict):
        raise ValueError(f"active_profile not found in config profiles: {active_profile}")

    config["active_profile"] = active_profile
    for section in ("trend", "entry"):
        merged = deepcopy(config.get(section, {}))
        merged.update(deepcopy(profile.get(section, {})))
        config[section] = merged

    symbol = str(config["symbol"]).lower()
    trend_timeframe = str(config["trend"]["timeframe"])
    entry_timeframe = str(config["entry"]["timeframe"])
    config.setdefault("market_data", {})
    config["market_data"]["kline_streams"] = [
        f"{symbol}@kline_{entry_timeframe}",
        f"{symbol}@kline_{trend_timeframe}",
    ]
    _validate_hard_stop(config)
    return config


def _validate_hard_stop(config: Dict[str, Any]) -> None:
    risk = config.get("risk") if isinstance(config.get("risk"), dict) else {}
    hard_stop = risk.get("hard_stop") if isinstance(risk.get("hard_stop"), dict) else {}
    if not bool(hard_stop.get("enabled", False)):
        return
    value = hard_stop.get("stop_pct")
    try:
        stop_pct = float(value)
    except (TypeError, ValueError):
        raise ValueError("risk.hard_stop.stop_pct must be greater than 0 and less than 100") from None
    if isinstance(value, bool) or stop_pct <= 0 or stop_pct >= 100:
        raise ValueError("risk.hard_stop.stop_pct must be greater than 0 and less than 100")
