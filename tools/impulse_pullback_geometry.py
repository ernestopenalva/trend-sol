from __future__ import annotations

import argparse
import bisect
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.indicators.indicators import atr, ema
from tools.market_selection_study import MarketCandle, load_candle_cache


MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DEFAULT_FORWARD_MINUTES = (15, 30, 60, 120, 240)
DEFAULT_TARGET_PCTS = (0.25, 0.50, 1.00, 2.00)


@dataclass(frozen=True)
class GeometryConfig:
    atr_period: int = 14
    impulse_lookback_candles: int = 8
    min_impulse_atr: float = 4.0
    pullback_min_fraction: float = 0.20
    pullback_max_fraction: float = 0.60
    confirmation_lookback_candles: int = 6
    confirmation_margin_atr: float = 0.10
    expiry_minutes: int = 120
    fee_pct: float = 0.20
    regime_ema_period: int = 50
    regime_slope_lookback: int = 3
    forward_minutes: tuple[int, ...] = DEFAULT_FORWARD_MINUTES
    target_pcts: tuple[float, ...] = DEFAULT_TARGET_PCTS

    def validate(self) -> None:
        if self.atr_period < 1 or self.impulse_lookback_candles < 2:
            raise ValueError("ATR period and impulse lookback must be positive")
        if self.min_impulse_atr <= 0:
            raise ValueError("min impulse ATR must be positive")
        if not 0 < self.pullback_min_fraction < self.pullback_max_fraction < 1:
            raise ValueError("pullback fractions must satisfy 0 < min < max < 1")
        if self.confirmation_lookback_candles < 1 or self.confirmation_margin_atr < 0:
            raise ValueError("invalid confirmation settings")
        if self.expiry_minutes < 1 or self.fee_pct < 0:
            raise ValueError("invalid expiry or fee")


@dataclass(frozen=True)
class RegimePoint:
    close_time_ms: int
    regime: str
    ema_value: Optional[float]
    ema_previous: Optional[float]


@dataclass
class GeometryEvent:
    symbol: str
    status: str
    reason: str
    impulse_detected_ms: int
    impulse_origin_ms: int
    impulse_origin_price: float
    impulse_peak_ms: int
    impulse_peak_price: float
    impulse_atr_15m: float
    impulse_size_atr: float
    impulse_entry_price: float
    pullback_entered_ms: Optional[int] = None
    pullback_entry_price: Optional[float] = None
    max_pullback_fraction: float = 0.0
    confirmation_ms: Optional[int] = None
    confirmation_price: Optional[float] = None
    confirmation_threshold: Optional[float] = None
    confirmation_atr_1m: Optional[float] = None
    regime_1h: str = "UNAVAILABLE"
    resolved_ms: Optional[int] = None
    forward: Optional[dict[str, Any]] = None
    phase_forward: Optional[dict[str, dict[str, Any]]] = None

    def to_record(self, config: GeometryConfig) -> dict[str, Any]:
        record = asdict(self)
        for field in (
            "impulse_detected_ms",
            "impulse_origin_ms",
            "impulse_peak_ms",
            "pullback_entered_ms",
            "confirmation_ms",
            "resolved_ms",
        ):
            value = record.get(field)
            record[field.removesuffix("_ms") + "_utc"] = _iso(value) if value is not None else None
        record["study_version"] = "impulse_pullback_geometry_v1"
        record["parameters"] = {
            "atr_period": config.atr_period,
            "impulse_lookback_candles": config.impulse_lookback_candles,
            "min_impulse_atr": config.min_impulse_atr,
            "pullback_min_fraction": config.pullback_min_fraction,
            "pullback_max_fraction": config.pullback_max_fraction,
            "confirmation_lookback_candles": config.confirmation_lookback_candles,
            "confirmation_margin_atr": config.confirmation_margin_atr,
            "expiry_minutes": config.expiry_minutes,
            "fee_pct": config.fee_pct,
        }
        return record


def analyze_symbol(
    symbol: str,
    candles_15m: Sequence[MarketCandle],
    candles_1m: Sequence[MarketCandle],
    candles_1h: Sequence[MarketCandle],
    config: GeometryConfig,
) -> list[GeometryEvent]:
    config.validate()
    bars_15m = _ordered(candles_15m)
    bars_1m = _ordered(candles_1m)
    if not bars_15m or not bars_1m:
        return []

    atr_15m = atr(
        [item.high for item in bars_15m],
        [item.low for item in bars_15m],
        [item.close for item in bars_15m],
        config.atr_period,
    )
    atr_1m_values = atr(
        [item.high for item in bars_1m],
        [item.low for item in bars_1m],
        [item.close for item in bars_1m],
        config.atr_period,
    )
    minute_opens = [item.open_time_ms for item in bars_1m]
    regimes = build_regime_timeline(candles_1h, config)
    regime_times = [item.close_time_ms for item in regimes]

    events: list[GeometryEvent] = []
    blocked_until_ms = -1
    start_index = max(config.atr_period, config.impulse_lookback_candles - 1)
    for index in range(start_index, len(bars_15m)):
        candle = bars_15m[index]
        if candle.close_time_ms <= blocked_until_ms:
            continue
        current_atr = atr_15m[index]
        if current_atr is None or current_atr <= 0:
            continue

        previous = bars_15m[index - config.impulse_lookback_candles + 1 : index]
        if not previous or candle.high <= max(item.high for item in previous):
            continue
        window = bars_15m[index - config.impulse_lookback_candles + 1 : index + 1]
        origin = min(window, key=lambda item: (item.low, item.open_time_ms))
        closed_move = candle.close - origin.low
        if closed_move < config.min_impulse_atr * float(current_atr):
            continue

        event = GeometryEvent(
            symbol=symbol,
            status="WAITING_FOR_PULLBACK",
            reason="impulse_detected",
            impulse_detected_ms=candle.close_time_ms,
            impulse_origin_ms=origin.open_time_ms,
            impulse_origin_price=origin.low,
            impulse_peak_ms=candle.close_time_ms,
            impulse_peak_price=candle.high,
            impulse_atr_15m=float(current_atr),
            impulse_size_atr=(candle.high - origin.low) / float(current_atr),
            impulse_entry_price=candle.close,
            phase_forward={},
        )
        first_minute = bisect.bisect_right(minute_opens, candle.close_time_ms)
        _resolve_event(
            event,
            bars_1m,
            atr_1m_values,
            first_minute,
            regimes,
            regime_times,
            config,
        )
        events.append(event)
        blocked_until_ms = event.resolved_ms or (
            candle.close_time_ms + config.expiry_minutes * MINUTE_MS
        )
    return events


def build_regime_timeline(
    candles_1h: Sequence[MarketCandle],
    config: GeometryConfig,
) -> list[RegimePoint]:
    bars = _ordered(candles_1h)
    values = ema([item.close for item in bars], config.regime_ema_period)
    points: list[RegimePoint] = []
    for index, candle in enumerate(bars):
        previous_index = index - config.regime_slope_lookback
        current = values[index]
        previous = values[previous_index] if previous_index >= 0 else None
        if current is None or previous is None:
            regime = "UNAVAILABLE"
        elif current > previous:
            regime = "UP"
        elif current < previous:
            regime = "DOWN"
        else:
            regime = "FLAT"
        points.append(
            RegimePoint(
                close_time_ms=candle.close_time_ms,
                regime=regime,
                ema_value=float(current) if current is not None else None,
                ema_previous=float(previous) if previous is not None else None,
            )
        )
    return points


def summarize(events: Sequence[GeometryEvent], config: GeometryConfig) -> dict[str, Any]:
    statuses = Counter(item.status for item in events)
    confirmed = [item for item in events if item.status == "CONFIRMED" and item.forward]
    regimes: dict[str, list[GeometryEvent]] = defaultdict(list)
    for item in confirmed:
        regimes[item.regime_1h].append(item)

    return {
        "study_version": "impulse_pullback_geometry_v1",
        "events": len(events),
        "statuses": dict(sorted(statuses.items())),
        "confirmed": len(confirmed),
        "symbols_with_events": len({item.symbol for item in events}),
        "symbols_with_confirmations": len({item.symbol for item in confirmed}),
        "median_impulse_atr": _median([item.impulse_size_atr for item in events]),
        "median_confirmed_pullback_pct": _median(
            [item.max_pullback_fraction * 100 for item in confirmed]
        ),
        "all_confirmed": _forward_summary(confirmed, config),
        "stage_comparison": _stage_comparison(events, config),
        "matched_confirmed_comparison": _matched_confirmed_comparison(events, config),
        "by_regime_1h": {
            name: _forward_summary(items, config) for name, items in sorted(regimes.items())
        },
        "by_symbol": {
            symbol: _forward_summary(
                [item for item in confirmed if item.symbol == symbol],
                config,
            )
            for symbol in sorted({item.symbol for item in confirmed})
        },
    }


def write_events(path: Path, events: Sequence[GeometryEvent], config: GeometryConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event.to_record(config), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def write_audit_sample(
    path: Path,
    events: Sequence[GeometryEvent],
    minute_candles: dict[str, Sequence[MarketCandle]],
    config: GeometryConfig,
    sample_size: int = 20,
) -> list[GeometryEvent]:
    selected = select_audit_events(events, sample_size)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for event in selected:
            end_ms = (event.resolved_ms or event.impulse_detected_ms) + 60 * MINUTE_MS
            path_start_ms = event.impulse_detected_ms - 15 * MINUTE_MS
            path_candles = [
                item
                for item in minute_candles.get(event.symbol, ())
                if path_start_ms <= item.open_time_ms <= end_ms
            ]
            record = event.to_record(config)
            record["audit_price_path_1m"] = [
                {
                    "open_time_ms": item.open_time_ms,
                    "open_time_utc": _iso(item.open_time_ms),
                    "open": item.open,
                    "high": item.high,
                    "low": item.low,
                    "close": item.close,
                }
                for item in path_candles
            ]
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
    return selected


def select_audit_events(
    events: Sequence[GeometryEvent],
    sample_size: int,
) -> list[GeometryEvent]:
    if sample_size <= 0 or not events:
        return []
    ordered = sorted(events, key=lambda item: (item.impulse_detected_ms, item.symbol))
    chosen: list[GeometryEvent] = []
    for status in ("CONFIRMED", "INVALIDATED", "EXPIRED"):
        match = next((item for item in ordered if item.status == status), None)
        if match is not None:
            chosen.append(match)
    remaining = [item for item in ordered if item not in chosen]
    slots = min(sample_size - len(chosen), len(remaining))
    if slots > 0:
        if slots == 1:
            indexes = [len(remaining) // 2]
        else:
            indexes = [
                round(index * (len(remaining) - 1) / (slots - 1))
                for index in range(slots)
            ]
        chosen.extend(remaining[index] for index in indexes)
    return sorted(chosen[:sample_size], key=lambda item: (item.impulse_detected_ms, item.symbol))


def _resolve_event(
    event: GeometryEvent,
    bars_1m: Sequence[MarketCandle],
    atr_1m_values: Sequence[Optional[float]],
    start_index: int,
    regimes: Sequence[RegimePoint],
    regime_times: Sequence[int],
    config: GeometryConfig,
) -> None:
    expiry_ms = event.impulse_detected_ms + config.expiry_minutes * MINUTE_MS
    band_entry_index: Optional[int] = None
    if start_index > 0:
        impulse_minute_index = start_index - 1
        event.phase_forward["impulse_close"] = _forward_metrics(
            impulse_minute_index,
            bars_1m,
            config,
            entry_price=event.impulse_entry_price,
        )
    for index in range(start_index, len(bars_1m)):
        candle = bars_1m[index]
        if candle.open_time_ms > expiry_ms:
            break

        if band_entry_index is None and candle.high > event.impulse_peak_price:
            event.impulse_peak_price = candle.high
            event.impulse_peak_ms = candle.close_time_ms
            event.impulse_size_atr = (
                event.impulse_peak_price - event.impulse_origin_price
            ) / event.impulse_atr_15m

        amplitude = event.impulse_peak_price - event.impulse_origin_price
        if amplitude <= 0:
            continue
        pullback_fraction = max(
            0.0,
            (event.impulse_peak_price - candle.close) / amplitude,
        )
        event.max_pullback_fraction = max(event.max_pullback_fraction, pullback_fraction)

        if candle.close <= event.impulse_origin_price:
            _finish(event, "INVALIDATED", "lost_impulse_origin", candle.close_time_ms)
            return
        if pullback_fraction > config.pullback_max_fraction:
            _finish(event, "INVALIDATED", "pullback_too_deep", candle.close_time_ms)
            return
        if (
            band_entry_index is None
            and config.pullback_min_fraction
            <= pullback_fraction
            <= config.pullback_max_fraction
        ):
            band_entry_index = index
            event.pullback_entered_ms = candle.close_time_ms
            event.pullback_entry_price = candle.close
            event.status = "WAITING_FOR_CONFIRMATION"
            event.reason = "pullback_band_entered"
            event.phase_forward["pullback_band"] = _forward_metrics(index, bars_1m, config)
            continue
        if band_entry_index is None or index <= band_entry_index:
            continue

        current_atr = atr_1m_values[index]
        prior_start = index - config.confirmation_lookback_candles
        if current_atr is None or current_atr <= 0 or prior_start < 0:
            continue
        previous = bars_1m[prior_start:index]
        threshold = max(item.high for item in previous) + (
            config.confirmation_margin_atr * float(current_atr)
        )
        if candle.close <= threshold:
            continue

        event.status = "CONFIRMED"
        event.reason = "local_high_breakout"
        event.confirmation_ms = candle.close_time_ms
        event.confirmation_price = candle.close
        event.confirmation_threshold = threshold
        event.confirmation_atr_1m = float(current_atr)
        event.regime_1h = _regime_at(candle.close_time_ms, regimes, regime_times)
        event.resolved_ms = candle.close_time_ms
        event.forward = _forward_metrics(index, bars_1m, config)
        event.phase_forward["confirmation"] = event.forward
        return

    _finish(event, "EXPIRED", "confirmation_timeout", expiry_ms)


def _forward_metrics(
    confirmation_index: int,
    bars: Sequence[MarketCandle],
    config: GeometryConfig,
    entry_price: Optional[float] = None,
) -> dict[str, Any]:
    entry = float(entry_price if entry_price is not None else bars[confirmation_index].close)
    output: dict[str, Any] = {
        "entry_price": entry,
        "fee_pct": config.fee_pct,
        "horizons": {},
    }
    for minutes in config.forward_minutes:
        end_ms = bars[confirmation_index].close_time_ms + minutes * MINUTE_MS
        future = [
            item
            for item in bars[confirmation_index + 1 :]
            if item.close_time_ms <= end_ms
        ]
        if not future or future[-1].close_time_ms < end_ms:
            output["horizons"][str(minutes)] = {"complete": False}
            continue
        max_high = max(item.high for item in future)
        min_low = min(item.low for item in future)
        close = future[-1].close
        mfe_pct = _pct(max_high, entry)
        mae_pct = _pct(min_low, entry)
        close_gross_pct = _pct(close, entry)
        output["horizons"][str(minutes)] = {
            "complete": True,
            "close_gross_pct": close_gross_pct,
            "close_net_pct": close_gross_pct - config.fee_pct,
            "mfe_gross_pct": mfe_pct,
            "mfe_net_pct": mfe_pct - config.fee_pct,
            "mae_pct": mae_pct,
            "targets_hit": {
                _target_key(target): mfe_pct >= target for target in config.target_pcts
            },
        }
    return output


def _forward_summary(
    events: Sequence[GeometryEvent],
    config: GeometryConfig,
) -> dict[str, Any]:
    return _forward_records_summary(
        [item.forward for item in events if item.forward],
        config,
    )


def _forward_records_summary(
    forwards: Sequence[dict[str, Any]],
    config: GeometryConfig,
) -> dict[str, Any]:
    result: dict[str, Any] = {"events": len(forwards), "horizons": {}}
    for minutes in config.forward_minutes:
        rows = [
            item["horizons"][str(minutes)]
            for item in forwards
            if item["horizons"].get(str(minutes), {}).get("complete")
        ]
        if not rows:
            result["horizons"][str(minutes)] = {"complete_events": 0}
            continue
        result["horizons"][str(minutes)] = {
            "complete_events": len(rows),
            "median_close_net_pct": _median([row["close_net_pct"] for row in rows]),
            "mean_close_net_pct": sum(row["close_net_pct"] for row in rows) / len(rows),
            "mean_close_net_without_best_pct": _mean_without_best(
                [row["close_net_pct"] for row in rows]
            ),
            "sum_close_net_pct": sum(row["close_net_pct"] for row in rows),
            "sum_close_net_without_best_pct": sum(row["close_net_pct"] for row in rows)
            - max(row["close_net_pct"] for row in rows),
            "median_mfe_net_pct": _median([row["mfe_net_pct"] for row in rows]),
            "median_mae_pct": _median([row["mae_pct"] for row in rows]),
            "net_positive_close_rate": sum(row["close_net_pct"] > 0 for row in rows)
            / len(rows),
            "target_hit_rates": {
                _target_key(target): sum(
                    row["targets_hit"][_target_key(target)] for row in rows
                )
                / len(rows)
                for target in config.target_pcts
            },
        }
    return result


def _stage_comparison(
    events: Sequence[GeometryEvent],
    config: GeometryConfig,
) -> dict[str, Any]:
    phases = ("impulse_close", "pullback_band", "confirmation")
    return {
        phase: _forward_records_summary(
            [
                item.phase_forward[phase]
                for item in events
                if item.phase_forward and phase in item.phase_forward
            ],
            config,
        )
        for phase in phases
    }


def _matched_confirmed_comparison(
    events: Sequence[GeometryEvent],
    config: GeometryConfig,
) -> dict[str, Any]:
    phases = ("impulse_close", "pullback_band", "confirmation")
    matched = [
        item
        for item in events
        if item.status == "CONFIRMED"
        and item.phase_forward
        and all(phase in item.phase_forward for phase in phases)
    ]
    result: dict[str, Any] = {
        "events": len(matched),
        "phases": {
            phase: _forward_records_summary(
                [item.phase_forward[phase] for item in matched],
                config,
            )
            for phase in phases
        },
        "paired_uplift": {},
    }
    for minutes in config.forward_minutes:
        horizon = str(minutes)
        complete = [
            item
            for item in matched
            if all(
                item.phase_forward[phase]["horizons"].get(horizon, {}).get("complete")
                for phase in phases
            )
        ]
        deltas_confirmation_pullback = [
            item.phase_forward["confirmation"]["horizons"][horizon]["close_net_pct"]
            - item.phase_forward["pullback_band"]["horizons"][horizon]["close_net_pct"]
            for item in complete
        ]
        deltas_confirmation_impulse = [
            item.phase_forward["confirmation"]["horizons"][horizon]["close_net_pct"]
            - item.phase_forward["impulse_close"]["horizons"][horizon]["close_net_pct"]
            for item in complete
        ]
        result["paired_uplift"][horizon] = {
            "complete_events": len(complete),
            "median_confirmation_minus_pullback_net_pct": _median(
                deltas_confirmation_pullback
            ),
            "mean_confirmation_minus_pullback_net_pct": (
                sum(deltas_confirmation_pullback) / len(deltas_confirmation_pullback)
                if deltas_confirmation_pullback
                else None
            ),
            "confirmation_beats_pullback_rate": (
                sum(value > 0 for value in deltas_confirmation_pullback)
                / len(deltas_confirmation_pullback)
                if deltas_confirmation_pullback
                else None
            ),
            "median_confirmation_minus_impulse_net_pct": _median(
                deltas_confirmation_impulse
            ),
        }
    return result


def _finish(event: GeometryEvent, status: str, reason: str, resolved_ms: int) -> None:
    event.status = status
    event.reason = reason
    event.resolved_ms = resolved_ms


def _regime_at(
    timestamp_ms: int,
    regimes: Sequence[RegimePoint],
    regime_times: Sequence[int],
) -> str:
    index = bisect.bisect_right(regime_times, timestamp_ms) - 1
    return regimes[index].regime if index >= 0 else "UNAVAILABLE"


def _ordered(candles: Sequence[MarketCandle]) -> list[MarketCandle]:
    return sorted(candles, key=lambda item: item.open_time_ms)


def _pct(value: float, reference: float) -> float:
    return ((value / reference) - 1) * 100 if reference else 0.0


def _median(values: Sequence[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def _mean_without_best(values: Sequence[float]) -> Optional[float]:
    if len(values) <= 1:
        return None
    return (sum(values) - max(values)) / (len(values) - 1)


def _target_key(value: float) -> str:
    return f"{value:.2f}"


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _discover_symbols(cache_dir: Path) -> list[str]:
    symbols = []
    for path in cache_dir.glob("*_15m.jsonl"):
        symbol = path.name.removesuffix("_15m.jsonl")
        if (cache_dir / f"{symbol}_1m.jsonl").exists():
            symbols.append(symbol)
    return sorted(symbols)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Study impulse -> controlled pullback -> local breakout geometry."
    )
    parser.add_argument(
        "--cache-dir",
        default=str(PROJECT_ROOT / "data" / "market_bot_replay" / "klines"),
    )
    parser.add_argument(
        "--regime-cache-dir",
        default=str(PROJECT_ROOT / "data" / "market_selection" / "klines"),
    )
    parser.add_argument("--symbols", nargs="*", help="Defaults to every cached 15m+1m symbol.")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "data" / "studies" / "impulse_pullback_geometry.jsonl"),
    )
    parser.add_argument("--summary-json", help="Optional path for the aggregate JSON summary.")
    parser.add_argument(
        "--audit-output",
        help="Optional JSONL sample with 1m price paths for manual audit.",
    )
    parser.add_argument("--audit-size", type=int, default=20)
    parser.add_argument("--min-impulse-atr", type=float, default=4.0)
    parser.add_argument("--pullback-min-fraction", type=float, default=0.20)
    parser.add_argument("--pullback-max-fraction", type=float, default=0.60)
    parser.add_argument("--confirmation-margin-atr", type=float, default=0.10)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    cache_dir = Path(args.cache_dir)
    regime_cache_dir = Path(args.regime_cache_dir)
    config = GeometryConfig(
        min_impulse_atr=args.min_impulse_atr,
        pullback_min_fraction=args.pullback_min_fraction,
        pullback_max_fraction=args.pullback_max_fraction,
        confirmation_margin_atr=args.confirmation_margin_atr,
    )
    config.validate()
    symbols = sorted(set(args.symbols or _discover_symbols(cache_dir)))
    if not symbols:
        raise SystemExit(f"No cached 15m+1m symbols found in {cache_dir}")

    all_events: list[GeometryEvent] = []
    minute_candles: dict[str, Sequence[MarketCandle]] = {}
    coverage: dict[str, Any] = {}
    for symbol in symbols:
        bars_15m = load_candle_cache(cache_dir / f"{symbol}_15m.jsonl")
        bars_1m = load_candle_cache(cache_dir / f"{symbol}_1m.jsonl")
        minute_candles[symbol] = bars_1m
        bars_1h = load_candle_cache(regime_cache_dir / f"{symbol}_1h.jsonl")
        events = analyze_symbol(symbol, bars_15m, bars_1m, bars_1h, config)
        all_events.extend(events)
        coverage[symbol] = {
            "candles_15m": len(bars_15m),
            "candles_1m": len(bars_1m),
            "candles_1h": len(bars_1h),
            "first_1m_utc": _iso(bars_1m[0].open_time_ms) if bars_1m else None,
            "last_1m_utc": _iso(bars_1m[-1].close_time_ms) if bars_1m else None,
            "events": len(events),
            "confirmed": sum(item.status == "CONFIRMED" for item in events),
        }

    output = Path(args.output)
    write_events(output, all_events, config)
    report = summarize(all_events, config)
    report["coverage"] = coverage
    report["parameters"] = asdict(config)
    report["output"] = str(output)
    if args.audit_output:
        audit_path = Path(args.audit_output)
        audited = write_audit_sample(
            audit_path,
            all_events,
            minute_candles,
            config,
            args.audit_size,
        )
        report["audit_output"] = str(audit_path)
        report["audit_events"] = len(audited)
    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print("TREND-SOL | impulse-pullback geometry v1")
    print(f"Symbols: {len(symbols)} | events: {report['events']} | confirmed: {report['confirmed']}")
    print(f"Statuses: {report['statuses']}")
    print(f"Events JSONL: {output}")
    for minutes in config.forward_minutes:
        row = report["all_confirmed"]["horizons"][str(minutes)]
        if not row.get("complete_events"):
            print(f"{minutes:>3}m | no complete confirmed events")
            continue
        hits = row["target_hit_rates"]
        print(
            f"{minutes:>3}m | n={row['complete_events']} | "
            f"median close net={row['median_close_net_pct']:+.3f}% | "
            f"median MFE net={row['median_mfe_net_pct']:+.3f}% | "
            f"median MAE={row['median_mae_pct']:+.3f}% | "
            f"hit +0.50%={hits['0.50']:.1%} | hit +1.00%={hits['1.00']:.1%}"
        )
    print("Matched A/B/C | confirmation uplift over pullback-band entry")
    for minutes in config.forward_minutes:
        row = report["matched_confirmed_comparison"]["paired_uplift"][str(minutes)]
        if not row["complete_events"]:
            continue
        print(
            f"{minutes:>3}m | n={row['complete_events']} | "
            f"median delta={row['median_confirmation_minus_pullback_net_pct']:+.3f} pp | "
            f"confirmation wins={row['confirmation_beats_pullback_rate']:.1%}"
        )


if __name__ == "__main__":
    main()
