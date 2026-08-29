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


EMA_PERIODS = (20, 50, 100)
LOOKBACK = 3
HISTORY_POINTS = 12
# 100 candles seed EMA100 and 15 valid EMA values cover the 12 displayed t/t-3 comparisons.
REQUIRED_CLOSED_CANDLES = 100 + HISTORY_POINTS + LOOKBACK
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
BAR_FILLED = _terminal_symbol("█", "#")
BAR_EMPTY = _terminal_symbol("░", ".")
ARROW = _terminal_symbol("→", "->")


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
    print("TREND-SOL | EMA context monitor | OBSERVATIONAL ONLY")
    if len(candles) < REQUIRED_CLOSED_CANDLES:
        print(
            f"Insufficient closed 5m candles: {len(candles)}/{REQUIRED_CLOSED_CANDLES}. "
            "No EMA100 history is shown."
        )
        return
    closes = [item.close for item in candles]
    values = {period: ema(closes, period) for period in EMA_PERIODS}
    available = _available_indices(values, len(candles))
    if len(available) < HISTORY_POINTS:
        print("Insufficient post-warmup EMA values for 12 safe t vs t-3 comparisons.")
        return
    indices = available[-HISTORY_POINTS:]
    current = indices[-1]
    latest = candles[current]
    print(f"Now BRT: {_brt(datetime.now(timezone.utc))} | symbol: {symbol} | last closed 5m: {_brt_ms(latest.close_time_ms)} | last closed price: {latest.close:.4f}")
    print("EMA | value t | value t-3 | delta | delta % | direction")
    directions: dict[int, str] = {}
    for period in EMA_PERIODS:
        now_value, old_value = values[period][current], values[period][current - LOOKBACK]
        assert now_value is not None and old_value is not None
        delta = float(now_value) - float(old_value)
        directions[period] = _direction(delta)
        print(f"EMA{period} | {float(now_value):.4f} | {float(old_value):.4f} | {delta:+.4f} | {_pct(delta, float(old_value)):+.3f}% | {directions[period]}")
    score, label = _score(directions.values())
    print(f"\nEMA SCORE: {score:.1f} / 10\nLABEL: {label}\nRISING EMAs: {sum(value == UP for value in directions.values())}/3\nTREND READ: {label}")
    print("\nLAST 12 CLOSED 5m CANDLES")
    print("BRT | EMA20 | EMA50 | EMA100 | score | label | bar")
    scores: list[str] = []
    for index in indices:
        row_directions = {
            period: _direction(float(values[period][index]) - float(values[period][index - LOOKBACK]))
            for period in EMA_PERIODS
        }
        row_score, row_label = _score(row_directions.values())
        scores.append(f"{row_score:.1f}")
        print(f"{_brt_ms(candles[index].close_time_ms)} | {row_directions[20]} | {row_directions[50]} | {row_directions[100]} | {row_score:>4.1f} | {row_label:<14} | {_bar(row_score)}")
    print("\nSCORE TRAJECTORY:")
    print(f" {ARROW} ".join(scores))
    print("\nSource: Binance public REST /api/v3/klines, SOLUSDT 5m; only close_time <= now is used.")
    print(f"EMA method: src.indicators.indicators.ema; SMA seed of first period candles, then standard alpha=2/(period+1). Warmup: {len(candles)} closed candles; EMA100 has {len(candles) - 100} post-seed values.")
    print("EMA score is observational only; it does not enter gates, entries, sizing, exits, or shadows.")


def _available_indices(values: dict[int, list[float | None]], candle_count: int) -> list[int]:
    return [
        index for index in range(LOOKBACK, candle_count)
        if all(values[period][index] is not None and values[period][index - LOOKBACK] is not None for period in EMA_PERIODS)
    ]


def _direction(delta: float) -> str:
    return UP if delta > 0 else DOWN if delta < 0 else FLAT


def _score(directions: Iterable[str]) -> tuple[float, str]:
    rising = sum(item == UP for item in directions)
    return {0: (0.0, "FALLING"), 1: (3.3, "MOSTLY_FALLING"), 2: (6.7, "MOSTLY_RISING"), 3: (10.0, "RISING")}[rising]


def _pct(delta: float, baseline: float) -> float:
    return delta / baseline * 100 if baseline else 0.0


def _bar(score: float) -> str:
    filled = round(score)
    return BAR_FILLED * filled + BAR_EMPTY * (10 - filled)


def _brt_ms(value: int) -> str:
    return _brt(datetime.fromtimestamp(value / 1000, tz=timezone.utc))


def _brt(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


if __name__ == "__main__":
    main()
