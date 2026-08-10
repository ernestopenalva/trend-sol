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
    _validate_trend_observations(config)
    timeframes = [entry_timeframe, trend_timeframe]
    trend_gate = config.get("trend_gate")
    if isinstance(trend_gate, dict) and str(trend_gate.get("mode", "")).lower() == "ge30":
        timeframes.append(str(trend_gate["candle_interval"]))
    observations = config.get("ema_observations")
    if isinstance(observations, dict) and bool(observations.get("enabled", False)):
        for variant in observations.get("variants", []):
            if isinstance(variant, dict) and variant.get("interval"):
                timeframes.append(str(variant["interval"]))
    config.setdefault("market_data", {})
    config["market_data"]["kline_streams"] = [
        f"{symbol}@kline_{timeframe}"
        for timeframe in dict.fromkeys(timeframes)
    ]
    _validate_hard_stop(config)
    _validate_profit_lock_shadow(config)
    _validate_profit_lock_economic_floor(config)
    _validate_phantoms(config)
    _validate_multi_market_shadow(config)
    return config


def _validate_trend_observations(config: Dict[str, Any]) -> None:
    gate = config.get("trend_gate")
    if isinstance(gate, dict) and str(gate.get("mode", "")).lower() == "ge30":
        if not str(gate.get("candle_interval", "")):
            raise ValueError("trend_gate.candle_interval is required for GE30")
        lookback = gate.get("lookback_candles")
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback <= 0:
            raise ValueError("trend_gate.lookback_candles must be a positive integer")
    observations = config.get("ema_observations")
    if not isinstance(observations, dict) or not bool(observations.get("enabled", False)):
        return
    window = observations.get("slope_window_minutes")
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("ema_observations.slope_window_minutes must be a positive integer")
    variants = observations.get("variants")
    if not isinstance(variants, list) or not variants:
        raise ValueError("ema_observations.variants must be a non-empty list")
    for variant in variants:
        if not isinstance(variant, dict):
            raise ValueError("ema_observations variants must be mappings")
        period = variant.get("period")
        if isinstance(period, bool) or not isinstance(period, int) or period <= 0:
            raise ValueError("ema_observations variant period must be a positive integer")
        if not str(variant.get("interval", "")):
            raise ValueError("ema_observations variant interval is required")


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


def _validate_profit_lock_shadow(config: Dict[str, Any]) -> None:
    risk = config.get("risk") if isinstance(config.get("risk"), dict) else {}
    profit_lock = risk.get("profit_lock") if isinstance(risk.get("profit_lock"), dict) else {}
    shadow = profit_lock.get("net_floor_shadow") if isinstance(profit_lock.get("net_floor_shadow"), dict) else {}
    if not bool(shadow.get("enabled", False)):
        return
    for field in ("net_margin_pct", "activation_buffer_atr"):
        value = shadow.get(field)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"risk.profit_lock.net_floor_shadow.{field} must be greater than or equal to 0") from None
        if isinstance(value, bool) or number < 0:
            raise ValueError(f"risk.profit_lock.net_floor_shadow.{field} must be greater than or equal to 0")


def _validate_profit_lock_economic_floor(config: Dict[str, Any]) -> None:
    risk = config.get("risk") if isinstance(config.get("risk"), dict) else {}
    profit_lock = risk.get("profit_lock") if isinstance(risk.get("profit_lock"), dict) else {}
    floor = profit_lock.get("economic_floor") if isinstance(profit_lock.get("economic_floor"), dict) else {}
    if not bool(floor.get("enabled", False)):
        return
    value = floor.get("net_margin_pct")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(
            "risk.profit_lock.economic_floor.net_margin_pct must be greater than or equal to 0"
        ) from None
    if isinstance(value, bool) or number < 0:
        raise ValueError(
            "risk.profit_lock.economic_floor.net_margin_pct must be greater than or equal to 0"
        )


def _validate_phantoms(config: Dict[str, Any]) -> None:
    instrumentation = config.get("instrumentation")
    if not isinstance(instrumentation, dict) or not bool(instrumentation.get("enabled", False)):
        return
    phantoms = instrumentation.get("phantoms")
    if not isinstance(phantoms, dict) or not bool(phantoms.get("enabled", False)):
        return
    max_open = phantoms.get("max_open_positions")
    max_age = phantoms.get("max_age_hours")
    if isinstance(max_open, bool) or not isinstance(max_open, int) or max_open <= 0:
        raise ValueError("instrumentation.phantoms.max_open_positions must be a positive integer")
    try:
        max_age_value = float(max_age)
    except (TypeError, ValueError):
        raise ValueError("instrumentation.phantoms.max_age_hours must be greater than 0") from None
    if isinstance(max_age, bool) or max_age_value <= 0:
        raise ValueError("instrumentation.phantoms.max_age_hours must be greater than 0")


def _validate_multi_market_shadow(config: Dict[str, Any]) -> None:
    instrumentation = config.get("instrumentation")
    if not isinstance(instrumentation, dict) or not bool(instrumentation.get("enabled", False)):
        return
    shadow = instrumentation.get("multi_market_shadow")
    if not isinstance(shadow, dict) or not bool(shadow.get("enabled", False)):
        return
    for field in (
        "top_count",
        "reevaluate_hours",
        "max_universe_symbols",
        "max_open_positions_per_symbol",
        "max_entries_per_selection_epoch",
    ):
        value = shadow.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(
                f"instrumentation.multi_market_shadow.{field} must be a positive integer"
            )
    for field in ("min_quote_volume_usdt", "max_spread_bps"):
        value = shadow.get(field)
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"instrumentation.multi_market_shadow.{field} must be greater than 0"
            ) from None
        if isinstance(value, bool) or number <= 0:
            raise ValueError(
                f"instrumentation.multi_market_shadow.{field} must be greater than 0"
            )
    if int(shadow["max_open_positions_per_symbol"]) > int(
        shadow["max_entries_per_selection_epoch"]
    ):
        raise ValueError(
            "instrumentation.multi_market_shadow.max_entries_per_selection_epoch "
            "must be greater than or equal to max_open_positions_per_symbol"
        )
    ge30 = shadow.get("ge30_variant")
    if isinstance(ge30, dict) and bool(ge30.get("enabled", False)):
        for field in ("state_file", "ledger_file"):
            value = str(ge30.get(field, ""))
            if not value:
                raise ValueError(
                    f"instrumentation.multi_market_shadow.ge30_variant.{field} is required"
                )
            if value == str(shadow.get(field, "")):
                raise ValueError(
                    f"instrumentation.multi_market_shadow.ge30_variant.{field} "
                    "must be independent from legacy"
                )
