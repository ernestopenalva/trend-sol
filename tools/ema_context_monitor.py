"""Read-only live view of the closed-5m EMA context used by the new cohort."""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.exchange.binance_market_data import BinanceMarketDataClient, BinanceMarketDataError
from src.indicators.indicators import ema
from src.monitor.market_context import _direction, classify_ema_context, classify_macd_context


EMA_PERIODS = (50, 100, 200)
ALL_PERIODS = (12, 26, *EMA_PERIODS)
HISTORY_POINTS = 12
# EMA200 first becomes available at index 199; direction needs the immediately prior value.
REQUIRED_CLOSED_CANDLES = 200 + HISTORY_POINTS
FETCH_LIMIT = 300


def _terminal_symbol(unicode_text: str, fallback: str) -> str:
    try:
        unicode_text.encode(sys.stdout.encoding or "utf-8")
        return unicode_text
    except UnicodeEncodeError:
        return fallback


UP = _terminal_symbol("↑", "^")
DOWN = _terminal_symbol("↓", "v")
FLAT = "="


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    close: float


def main() -> None:
    config = _load_config()
    symbol = str(config.get("symbol", "SOLUSDT"))
    market = config.get("market_data", {}) if isinstance(config.get("market_data"), dict) else {}
    execution = config.get("execution", {}) if isinstance(config.get("execution"), dict) else {}
    client = BinanceMarketDataClient(
        str(market.get("rest_url", "https://api.binance.com")),
        timeout_seconds=int(execution.get("http_timeout_seconds", 8)),
    )
    try:
        candles = _closed_candles(client.klines(symbol, "5m", FETCH_LIMIT))
    except (BinanceMarketDataError, OSError, RuntimeError) as exc:
        print(f"EMA context unavailable: {exc}")
        return
    except Exception as exc:  # requests may fail before the market-data client can normalize it
        print(f"EMA context unavailable: {type(exc).__name__}: {exc}")
        return
    _print_monitor(symbol, candles)


def _load_config() -> dict[str, Any]:
    path = PROJECT_ROOT / "config" / "config.yaml"
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"EMA context unavailable: unable to read config/config.yaml ({exc})")
        return {}


def _closed_candles(rows: Iterable[Iterable[Any]], now_ms: int | None = None) -> list[Candle]:
    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    candles: list[Candle] = []
    for row in rows:
        values = list(row)
        if len(values) < 7:
            continue
        try:
            candle = Candle(int(values[0]), int(values[6]), float(values[4]))
        except (TypeError, ValueError):
            continue
        if candle.close_time_ms <= now_ms:
            candles.append(candle)
    return candles


def _print_monitor(symbol: str, candles: list[Candle]) -> None:
    print("TREND-SOL | EMA + MACD context monitor | OBSERVATIONAL ONLY")
    if len(candles) < REQUIRED_CLOSED_CANDLES:
        print(
            f"Insufficient closed 5m candles: {len(candles)}/{REQUIRED_CLOSED_CANDLES}. "
            "No EMA200 context history is shown."
        )
        return
    closes = [item.close for item in candles]
    values = {period: ema(closes, period) for period in ALL_PERIODS}
    available = _available_indices(values, len(candles))
    if len(available) < HISTORY_POINTS:
        print("Insufficient post-warmup values for 12 closed-candle classifications.")
        return
    indices = available[-HISTORY_POINTS:]
    current = indices[-1]
    latest = candles[current]
    previous_candle = candles[current - 1]
    snapshot = _context_at(values, current)
    print(f"NOW BRT: {_brt_seconds(datetime.now(timezone.utc))}")
    print(f"symbol: {symbol}")
    print(f"latest closed 5m open time: {_brt_precise_ms(latest.open_time_ms)}")
    print(f"latest closed 5m close time: {_brt_precise_ms(latest.close_time_ms)}")
    print(f"previous closed 5m open time: {_brt_precise_ms(previous_candle.open_time_ms)}")
    print(f"latest closed price: {latest.close:.4f}")
    print("snapshot alignment: EMA and MACD use the same closed 5m current/previous candles")

    print("\nEMA | current | previous | delta | direction")
    for period in EMA_PERIODS:
        now_value = snapshot[f"ema{period}"]
        old_value = snapshot[f"ema{period}_previous"]
        delta = float(now_value) - float(old_value)
        print(f"EMA{period} | {float(now_value):.6f} | {float(old_value):.6f} | {delta:+.6f} | {snapshot[f'ema{period}_direction']}")

    print(f"\nEMA CONTEXT: {snapshot['ema_context']}")
    print("reason:")
    for period in EMA_PERIODS:
        print(f"EMA{period} {snapshot[f'ema{period}_direction']}")
    print(f"ordering: {_ordering(snapshot)}")
    print(_ema_reason(snapshot))

    macd_delta = float(snapshot["macd_line"]) - float(snapshot["macd_line_previous"])
    print("\nMACD")
    print(f"MACD current: {float(snapshot['macd_line']):+.8f}")
    print(f"MACD previous: {float(snapshot['macd_line_previous']):+.8f}")
    print(f"delta: {macd_delta:+.8f}")
    print(f"position: {snapshot['macd_position']}")
    print(f"direction: {snapshot['macd_direction']}")
    print(f"MACD CONTEXT: {snapshot['macd_context']}")

    print("\nLAST 12 CLOSED 5m CLASSIFICATIONS")
    print("BRT | EMA_CONTEXT | MACD_CONTEXT | EMA50 | EMA100 | EMA200 | MACD")
    for index in indices:
        row = _context_at(values, index)
        arrows = [_direction_symbol(str(row[f"ema{period}_direction"])) for period in EMA_PERIODS]
        print(f"{_brt_ms(candles[index].open_time_ms)} | {row['ema_context']} | {row['macd_context']} | "
              f"{arrows[0]} | {arrows[1]} | {arrows[2]} | {float(row['macd_line']):+.4f}")
    print("\nSource: Binance public REST /api/v3/klines, SOLUSDT 5m; only close_time <= now is used.")
    print("EMA/MACD method: src.indicators.indicators.ema + src.monitor.market_context classifications; MACD line=EMA12-EMA26, no signal/histogram.")
    print("This monitor is observational only; it does not enter gates, entries, sizing, exits, or shadows.")


def _available_indices(values: dict[int, list[float | None]], candle_count: int) -> list[int]:
    return [
        index for index in range(1, candle_count)
        if all(values[period][index] is not None and values[period][index - 1] is not None for period in ALL_PERIODS)
    ]


def _context_at(values: dict[int, list[float | None]], index: int) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for period in EMA_PERIODS:
        current, previous = values[period][index], values[period][index - 1]
        output[f"ema{period}"] = current
        output[f"ema{period}_previous"] = previous
        output[f"ema{period}_direction"] = _direction(current, previous)
    output["ema_context"] = classify_ema_context(
        output["ema50"], output["ema100"], output["ema200"],
        output["ema50_direction"], output["ema100_direction"], output["ema200_direction"],
    )
    current = (float(values[12][index]) - float(values[26][index]))
    previous = (float(values[12][index - 1]) - float(values[26][index - 1]))
    output["macd_line"] = current
    output["macd_line_previous"] = previous
    output["macd_direction"] = _direction(current, previous)
    output["macd_position"] = "ABOVE_ZERO" if current > 0 else "BELOW_ZERO" if current < 0 else "ZERO"
    output["macd_context"] = classify_macd_context(current, previous)
    return output


def _ordering(snapshot: dict[str, Any]) -> str:
    return " > ".join(
        name for name, _ in sorted(
            ((f"EMA{period}", float(snapshot[f"ema{period}"])) for period in EMA_PERIODS),
            key=lambda item: item[1], reverse=True,
        )
    )


def _ema_reason(snapshot: dict[str, Any]) -> str:
    context = str(snapshot["ema_context"])
    explanations = {
        "LON": "all three EMAs are UP with EMA50 > EMA100 > EMA200",
        "SHO": "all three EMAs are DOWN with EMA50 < EMA100 < EMA200",
        "BUL": "EMA50 and EMA100 are UP; LON was excluded by ordering/direction",
        "MUP": "EMA50 is UP and EMA100 is DOWN",
        "MDO": "EMA50 is DOWN and EMA100 is UP",
        "BEA": "EMA50 is DOWN after excluding SHO and MDO",
        "MIX": "none of the higher-precedence contexts matched",
    }
    return f"therefore {context}: {explanations.get(context, 'indicator data unavailable')}"


def _direction_symbol(direction: str) -> str:
    return UP if direction == "UP" else DOWN if direction == "DOWN" else FLAT


def _brt_ms(value: int) -> str:
    return _brt(datetime.fromtimestamp(value / 1000, tz=timezone.utc))


def _brt_precise_ms(value: int) -> str:
    converted = datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone(BRASILIA_TZ)
    return converted.strftime("%d/%m %H:%M:%S.") + f"{value % 1000:03d}"


def _brt_seconds(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M:%S")


def _brt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


if __name__ == "__main__":
    main()
