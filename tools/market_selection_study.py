from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.cohort_study import _load_config, _read_jsonl


HOUR_MS = 3_600_000


@dataclass(frozen=True)
class MarketCandle:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float
    trades: int

    @property
    def boundary_ms(self) -> int:
        return self.close_time_ms + 1


@dataclass(frozen=True)
class CurrentMarket:
    symbol: str
    base_asset: str
    change_24h_pct: float
    change_7d_pct: Optional[float]
    quote_volume_24h: float
    spread_bps: Optional[float]


@dataclass(frozen=True)
class CandidateSnapshot:
    symbol: str
    decision_ms: int
    price: float
    change_24h_pct: float
    change_7d_pct: float
    quote_volume_24h: float


@dataclass(frozen=True)
class ForwardOutcome:
    return_pct: float
    mfe_pct: float
    mae_pct: float


@dataclass(frozen=True)
class BasketOutcome:
    policy: str
    decision_ms: int
    horizon_hours: int
    symbols: tuple[str, ...]
    return_pct: float
    mfe_pct: float
    mae_pct: float


class BinancePublicClient:
    def __init__(self, base_url: str, timeout_seconds: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Binance market data error {response.status_code}: {response.text}"
            )
        return response.json()

    def exchange_info(self) -> Dict[str, Any]:
        data = self.get(
            "/api/v3/exchangeInfo",
            {"permissions": "SPOT", "symbolStatus": "TRADING"},
        )
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected exchangeInfo response.")
        return data

    def tickers_24h(self) -> list[Dict[str, Any]]:
        data = self.get("/api/v3/ticker/24hr", {"symbolStatus": "TRADING"})
        if not isinstance(data, list):
            raise RuntimeError("Unexpected 24hr ticker response.")
        return [item for item in data if isinstance(item, dict)]

    def rolling_tickers(self, symbols: Sequence[str], window: str) -> list[Dict[str, Any]]:
        output = []
        for start in range(0, len(symbols), 100):
            batch = list(symbols[start : start + 100])
            data = self.get(
                "/api/v3/ticker",
                {
                    "symbols": json.dumps(batch, separators=(",", ":")),
                    "windowSize": window,
                    "type": "FULL",
                    "symbolStatus": "TRADING",
                },
            )
            if isinstance(data, dict):
                output.append(data)
            elif isinstance(data, list):
                output.extend(item for item in data if isinstance(item, dict))
            else:
                raise RuntimeError("Unexpected rolling ticker response.")
        return output

    def book_tickers(self) -> list[Dict[str, Any]]:
        data = self.get("/api/v3/ticker/bookTicker", {"symbolStatus": "TRADING"})
        if not isinstance(data, list):
            raise RuntimeError("Unexpected bookTicker response.")
        return [item for item in data if isinstance(item, dict)]

    def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
    ) -> list[MarketCandle]:
        interval_ms = _interval_ms(interval)
        cursor = _floor_ms(start_ms, interval_ms)
        output = []
        while cursor <= end_ms:
            data = self.get(
                "/api/v3/klines",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            if not isinstance(data, list):
                raise RuntimeError(f"Unexpected kline response for {symbol}.")
            page = [_candle_from_binance(item) for item in data if isinstance(item, list)]
            if not page:
                break
            output.extend(page)
            next_cursor = max(item.open_time_ms for item in page) + interval_ms
            if next_cursor <= cursor:
                raise RuntimeError(f"Kline pagination did not advance for {symbol}.")
            cursor = next_cursor
            if len(page) < 1000:
                break
        return [
            item
            for item in merge_candles([], output)
            if item.open_time_ms >= start_ms and item.close_time_ms <= end_ms
        ]


def main() -> None:
    args = _parse_args()
    config = _load_config(Path(args.config))
    study = _study_config(config)
    base_url = str(
        args.market_data_url
        or (config.get("market_data") or {}).get("rest_url")
        or "https://api.binance.com"
    )
    timeframe = str(args.timeframe or study.get("timeframe", "1h"))
    lookback_days = int(args.lookback_days or study.get("lookback_days", 90))
    decision_interval_hours = int(
        args.decision_interval_hours or study.get("decision_interval_hours", 4)
    )
    max_symbols = int(args.max_symbols or study.get("max_universe_symbols", 50))
    min_quote_volume = float(
        args.min_quote_volume_usdt
        if args.min_quote_volume_usdt is not None
        else study.get("min_quote_volume_usdt", 10_000_000)
    )
    top_counts = (
        [int(value) for value in args.top_count]
        if args.top_count
        else [int(value) for value in study.get("top_counts", [1, 3, 5])]
    )
    horizons = (
        [int(value) for value in args.horizon_hours]
        if args.horizon_hours
        else [int(value) for value in study.get("forward_horizons_hours", [4, 12, 24])]
    )
    excluded_assets = {
        str(value).upper()
        for value in study.get(
            "excluded_base_assets",
            [
                "USDC",
                "FDUSD",
                "TUSD",
                "USDP",
                "DAI",
                "EUR",
                "WBTC",
                "RLUSD",
                "USD1",
                "U",
                "USDE",
                "USDS",
                "PYUSD",
                "BUSD",
                "XAUT",
            ],
        )
    }
    require_full_history = bool(study.get("require_full_history", True))
    _validate_settings(
        timeframe,
        lookback_days,
        decision_interval_hours,
        max_symbols,
        min_quote_volume,
        top_counts,
        horizons,
    )

    client = BinancePublicClient(base_url, int(args.http_timeout_seconds))
    snapshot_path = Path(args.universe_snapshot)
    if args.offline:
        current_markets = load_universe_snapshot(snapshot_path)
        if not current_markets:
            raise SystemExit("Offline mode requires a non-empty universe snapshot.")
    else:
        current_markets = fetch_current_markets(
            client,
            excluded_assets,
            min_quote_volume,
            max_symbols,
        )
        save_universe_snapshot(snapshot_path, current_markets)
    if not current_markets:
        raise SystemExit("No eligible USDT Spot markets found.")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    interval_ms = _interval_ms(timeframe)
    end_ms = _floor_ms(now_ms, interval_ms) - 1
    warmup_ms = 7 * 24 * HOUR_MS
    start_ms = end_ms - lookback_days * 24 * HOUR_MS - warmup_ms + 1
    cache_dir = Path(args.cache_dir)
    candles_by_symbol = {}
    excluded_short_history = []
    for market in current_markets:
        path = cache_dir / f"{market.symbol}_{timeframe}.jsonl"
        cached = load_candle_cache(path)
        missing = missing_candle_ranges(cached, start_ms, end_ms, interval_ms)
        if missing and args.offline:
            if require_full_history:
                excluded_short_history.append(market.symbol)
                continue
            raise SystemExit(
                f"Offline cache for {market.symbol} is missing {len(missing)} range(s)."
            )
        downloaded = [
            candle
            for missing_start, missing_end in missing
            for candle in client.klines(
                market.symbol,
                timeframe,
                missing_start,
                missing_end,
            )
        ]
        candles = merge_candles(cached, downloaded)
        if downloaded:
            save_candle_cache(path, candles)
        remaining = missing_candle_ranges(candles, start_ms, end_ms, interval_ms)
        if require_full_history and remaining:
            excluded_short_history.append(market.symbol)
            continue
        candles_by_symbol[market.symbol] = candles
    current_markets = [
        item for item in current_markets if item.symbol in candles_by_symbol
    ]

    snapshots = build_candidate_snapshots(
        candles_by_symbol,
        decision_interval_hours,
        min_quote_volume,
        require_positive_24h=True,
        require_positive_7d=True,
    )
    baskets = run_selection_replay(
        snapshots,
        candles_by_symbol,
        top_counts,
        horizons,
        min_quote_volume,
    )
    _print_report(
        current_markets,
        snapshots,
        baskets,
        timeframe,
        lookback_days,
        decision_interval_hours,
        min_quote_volume,
        max_symbols,
        top_counts,
        horizons,
        cache_dir,
        excluded_short_history,
    )


def fetch_current_markets(
    client: BinancePublicClient,
    excluded_base_assets: set[str],
    min_quote_volume_usdt: float,
    max_symbols: int,
) -> list[CurrentMarket]:
    info = client.exchange_info()
    symbols = {
        str(item.get("symbol")): str(item.get("baseAsset"))
        for item in info.get("symbols", [])
        if isinstance(item, dict)
        and item.get("quoteAsset") == "USDT"
        and item.get("status") == "TRADING"
        and _eligible_base_asset(str(item.get("baseAsset") or ""), excluded_base_assets)
    }
    tickers = {
        str(item.get("symbol")): item
        for item in client.tickers_24h()
        if str(item.get("symbol")) in symbols
    }
    liquid_symbols = sorted(
        (
            symbol
            for symbol in symbols
            if _number(tickers.get(symbol, {}).get("quoteVolume")) >= min_quote_volume_usdt
        ),
        key=lambda symbol: _number(tickers[symbol].get("quoteVolume")),
        reverse=True,
    )[:max_symbols]
    rolling = {
        str(item.get("symbol")): item
        for item in client.rolling_tickers(liquid_symbols, "7d")
    }
    books = {
        str(item.get("symbol")): item
        for item in client.book_tickers()
        if str(item.get("symbol")) in liquid_symbols
    }
    return sorted(
        [
            CurrentMarket(
                symbol=symbol,
                base_asset=symbols[symbol],
                change_24h_pct=_number(tickers[symbol].get("priceChangePercent")),
                change_7d_pct=_optional_number(
                    rolling.get(symbol, {}).get("priceChangePercent")
                ),
                quote_volume_24h=_number(tickers[symbol].get("quoteVolume")),
                spread_bps=_spread_bps(books.get(symbol, {})),
            )
            for symbol in liquid_symbols
        ],
        key=lambda item: item.quote_volume_24h,
        reverse=True,
    )


def build_candidate_snapshots(
    candles_by_symbol: Dict[str, Sequence[MarketCandle]],
    decision_interval_hours: int,
    min_quote_volume_usdt: float,
    require_positive_24h: bool,
    require_positive_7d: bool,
) -> list[CandidateSnapshot]:
    output = []
    for symbol, candles in candles_by_symbol.items():
        indexed = {item.boundary_ms: item for item in candles}
        for decision_ms, candle in sorted(indexed.items()):
            if _utc_hour(decision_ms) % decision_interval_hours:
                continue
            day_ago = indexed.get(decision_ms - 24 * HOUR_MS)
            week_ago = indexed.get(decision_ms - 7 * 24 * HOUR_MS)
            if day_ago is None or week_ago is None or day_ago.close <= 0 or week_ago.close <= 0:
                continue
            trailing = [
                indexed.get(decision_ms - offset * HOUR_MS)
                for offset in range(24)
            ]
            if any(item is None for item in trailing):
                continue
            quote_volume = sum(item.quote_volume for item in trailing if item is not None)
            change_24h = (candle.close / day_ago.close - 1) * 100
            change_7d = (candle.close / week_ago.close - 1) * 100
            if quote_volume < min_quote_volume_usdt:
                continue
            if require_positive_24h and change_24h <= 0:
                continue
            if require_positive_7d and change_7d <= 0:
                continue
            output.append(
                CandidateSnapshot(
                    symbol=symbol,
                    decision_ms=decision_ms,
                    price=candle.close,
                    change_24h_pct=change_24h,
                    change_7d_pct=change_7d,
                    quote_volume_24h=quote_volume,
                )
            )
    return sorted(output, key=lambda item: (item.decision_ms, -item.change_24h_pct, item.symbol))


def run_selection_replay(
    positive_snapshots: Sequence[CandidateSnapshot],
    candles_by_symbol: Dict[str, Sequence[MarketCandle]],
    top_counts: Sequence[int],
    horizons: Sequence[int],
    min_quote_volume_usdt: float,
) -> list[BasketOutcome]:
    positive_by_time = _group_snapshots(positive_snapshots)
    liquid_snapshots = build_candidate_snapshots(
        candles_by_symbol,
        decision_interval_hours=1,
        min_quote_volume_usdt=min_quote_volume_usdt,
        require_positive_24h=False,
        require_positive_7d=False,
    )
    liquid_by_time = _group_snapshots(liquid_snapshots)
    candle_indexes = {
        symbol: {item.boundary_ms: item for item in candles}
        for symbol, candles in candles_by_symbol.items()
    }
    decisions = sorted(positive_by_time)
    output = []
    for decision_ms in decisions:
        positive = sorted(
            positive_by_time[decision_ms],
            key=lambda item: (-item.change_24h_pct, -item.quote_volume_24h, item.symbol),
        )
        liquid = liquid_by_time.get(decision_ms, [])
        policies = {
            **{f"TOP_{count}": positive[:count] for count in top_counts},
            "POSITIVE_ALL": positive,
            "LIQUID_ALL": liquid,
            "SOL": [item for item in liquid if item.symbol == "SOLUSDT"],
        }
        for horizon in horizons:
            for policy, selected in policies.items():
                outcomes = [
                    value
                    for item in selected
                    if (
                        value := forward_outcome(
                            candle_indexes.get(item.symbol, {}),
                            decision_ms,
                            horizon,
                            item.price,
                        )
                    )
                    is not None
                ]
                if not outcomes:
                    continue
                output.append(
                    BasketOutcome(
                        policy=policy,
                        decision_ms=decision_ms,
                        horizon_hours=horizon,
                        symbols=tuple(item.symbol for item in selected),
                        return_pct=statistics.fmean(item.return_pct for item in outcomes),
                        mfe_pct=statistics.fmean(item.mfe_pct for item in outcomes),
                        mae_pct=statistics.fmean(item.mae_pct for item in outcomes),
                    )
                )
    return output


def forward_outcome(
    candles: Dict[int, MarketCandle],
    decision_ms: int,
    horizon_hours: int,
    entry_price: float,
) -> Optional[ForwardOutcome]:
    future = [
        candles.get(decision_ms + offset * HOUR_MS)
        for offset in range(1, horizon_hours + 1)
    ]
    if entry_price <= 0 or any(item is None for item in future):
        return None
    values = [item for item in future if item is not None]
    return ForwardOutcome(
        return_pct=(values[-1].close / entry_price - 1) * 100,
        mfe_pct=(max(item.high for item in values) / entry_price - 1) * 100,
        mae_pct=(min(item.low for item in values) / entry_price - 1) * 100,
    )


def load_candle_cache(path: Path) -> list[MarketCandle]:
    output = []
    for item in _read_jsonl(path):
        try:
            output.append(
                MarketCandle(
                    open_time_ms=int(item["open_time_ms"]),
                    close_time_ms=int(item["close_time_ms"]),
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    quote_volume=float(item["quote_volume"]),
                    trades=int(item["trades"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return merge_candles([], output)


def save_candle_cache(path: Path, candles: Sequence[MarketCandle]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for item in merge_candles([], candles):
            handle.write(
                json.dumps(
                    {
                        "open_time_ms": item.open_time_ms,
                        "close_time_ms": item.close_time_ms,
                        "open": item.open,
                        "high": item.high,
                        "low": item.low,
                        "close": item.close,
                        "quote_volume": item.quote_volume,
                        "trades": item.trades,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    tmp.replace(path)


def merge_candles(
    existing: Sequence[MarketCandle],
    incoming: Sequence[MarketCandle],
) -> list[MarketCandle]:
    indexed = {item.open_time_ms: item for item in existing}
    indexed.update({item.open_time_ms: item for item in incoming})
    return [indexed[key] for key in sorted(indexed)]


def missing_candle_ranges(
    candles: Sequence[MarketCandle],
    required_start_ms: int,
    required_end_ms: int,
    interval_ms: int,
) -> list[tuple[int, int]]:
    first_open = _floor_ms(required_start_ms, interval_ms)
    last_open = _floor_ms(required_end_ms - interval_ms + 1, interval_ms)
    if last_open < first_open:
        return []
    existing = {item.open_time_ms for item in candles}
    missing = [
        value
        for value in range(first_open, last_open + 1, interval_ms)
        if value not in existing
    ]
    if not missing:
        return []
    output = []
    range_start = missing[0]
    previous = missing[0]
    for value in missing[1:]:
        if value != previous + interval_ms:
            output.append((range_start, previous + interval_ms - 1))
            range_start = value
        previous = value
    output.append((range_start, min(required_end_ms, previous + interval_ms - 1)))
    return output


def load_universe_snapshot(path: Path) -> list[CurrentMarket]:
    output = []
    for item in _read_jsonl(path):
        try:
            output.append(
                CurrentMarket(
                    symbol=str(item["symbol"]),
                    base_asset=str(item["base_asset"]),
                    change_24h_pct=float(item["change_24h_pct"]),
                    change_7d_pct=_optional_number(item.get("change_7d_pct")),
                    quote_volume_24h=float(item["quote_volume_24h"]),
                    spread_bps=_optional_number(item.get("spread_bps")),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return output


def save_universe_snapshot(path: Path, markets: Sequence[CurrentMarket]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in markets:
            handle.write(
                json.dumps(
                    {
                        "symbol": item.symbol,
                        "base_asset": item.base_asset,
                        "change_24h_pct": item.change_24h_pct,
                        "change_7d_pct": item.change_7d_pct,
                        "quote_volume_24h": item.quote_volume_24h,
                        "spread_bps": item.spread_bps,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )


def _print_report(
    current_markets: Sequence[CurrentMarket],
    snapshots: Sequence[CandidateSnapshot],
    baskets: Sequence[BasketOutcome],
    timeframe: str,
    lookback_days: int,
    decision_interval_hours: int,
    min_quote_volume_usdt: float,
    max_symbols: int,
    top_counts: Sequence[int],
    horizons: Sequence[int],
    cache_dir: Path,
    excluded_short_history: Sequence[str],
) -> None:
    current_positive = sorted(
        (
            item
            for item in current_markets
            if item.change_24h_pct > 0
            and item.change_7d_pct is not None
            and item.change_7d_pct > 0
        ),
        key=lambda item: (-item.change_24h_pct, -item.quote_volume_24h),
    )
    print("TREND-SOL | Binance Top Gainers market-selection study")
    print(
        f"Universe: current liquid USDT Spot={len(current_markets)} | max={max_symbols} | "
        f"minimum 24h quote volume={min_quote_volume_usdt:,.0f} USDT"
    )
    if excluded_short_history:
        print(
            "Excluded without full replay history: "
            + ", ".join(sorted(excluded_short_history))
        )
    print(
        f"Replay: {lookback_days}d | candles={timeframe} | decision every "
        f"{decision_interval_hours}h | causal 24h and 7d returns"
    )
    print(
        "Fidelity: historical ranking uses only closed candles; the current-symbol universe "
        "has survivorship bias."
    )
    print("Historical spread is unavailable here; current real-market spread is shown below.")
    print(f"Cache: {cache_dir}")
    print()
    print("Current Binance gainers passing 24h>0, 7d>0 and liquidity:")
    print(
        f"{'symbol':12} {'24h':>9} {'7d':>9} {'quote vol':>15} {'spread':>9}"
    )
    for item in current_positive[:10]:
        print(
            f"{item.symbol:12} {item.change_24h_pct:+8.2f}% "
            f"{item.change_7d_pct:+8.2f}% {item.quote_volume_24h:15,.0f} "
            f"{_fmt_bps(item.spread_bps):>9}"
        )
    if not current_positive:
        print("  none")
    print()
    decision_count = len({item.decision_ms for item in snapshots})
    print(
        f"Historical positive pool: decisions={decision_count} | "
        f"candidate observations={len(snapshots)}"
    )
    print(
        f"{'horizon':>7} {'policy':13} {'baskets':>7} {'winrate':>8} "
        f"{'avg ret':>9} {'trim avg':>9} {'median':>9} {'avg MFE':>9} {'avg MAE':>9}"
    )
    policy_order = [
        *[f"TOP_{count}" for count in top_counts],
        "POSITIVE_ALL",
        "LIQUID_ALL",
        "SOL",
    ]
    for horizon in horizons:
        for policy in policy_order:
            selected = [
                item
                for item in baskets
                if item.horizon_hours == horizon and item.policy == policy
            ]
            if not selected:
                continue
            returns = [item.return_pct for item in selected]
            print(
                f"{horizon:5d}h {policy:13} {len(selected):7d} "
                f"{sum(value > 0 for value in returns) / len(returns):8.1%} "
                f"{statistics.fmean(returns):+8.3f}% "
                f"{_trimmed_mean(returns, 0.05):+8.3f}% "
                f"{statistics.median(returns):+8.3f}% "
                f"{statistics.fmean(item.mfe_pct for item in selected):+8.3f}% "
                f"{statistics.fmean(item.mae_pct for item in selected):+8.3f}%"
            )
    print()
    top_one = [
        item
        for item in baskets
        if item.policy == "TOP_1"
        and item.horizon_hours == min(horizons)
        and item.symbols
    ]
    concentration: Dict[str, int] = {}
    for item in top_one:
        concentration[item.symbols[0]] = concentration.get(item.symbols[0], 0) + 1
    print("TOP_1 selection concentration:")
    for symbol, count in sorted(
        concentration.items(),
        key=lambda item: (-item[1], item[0]),
    )[:10]:
        print(f"  {symbol}: {count} baskets ({count / len(top_one):.1%})")
    if not top_one:
        print("  none")
    print()
    print("Each row scores an equal-weight basket at each decision time, not individual coins.")
    print("trim avg removes the highest and lowest 5% of basket returns.")
    print("Forward windows overlap when the horizon is longer than the 4h decision interval.")
    print("This validates market direction selection, not the bot entry gates or realized PnL.")


def _group_snapshots(
    snapshots: Sequence[CandidateSnapshot],
) -> Dict[int, list[CandidateSnapshot]]:
    output: Dict[int, list[CandidateSnapshot]] = {}
    for item in snapshots:
        output.setdefault(item.decision_ms, []).append(item)
    return output


def _eligible_base_asset(value: str, excluded: set[str]) -> bool:
    upper = value.upper()
    return bool(
        upper
        and upper not in excluded
        and not upper.endswith(("UP", "DOWN", "BULL", "BEAR"))
    )


def _spread_bps(value: Dict[str, Any]) -> Optional[float]:
    bid = _optional_number(value.get("bidPrice"))
    ask = _optional_number(value.get("askPrice"))
    if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
        return None
    midpoint = (bid + ask) / 2
    return (ask - bid) / midpoint * 10_000 if midpoint > 0 else None


def _candle_from_binance(value: Sequence[Any]) -> MarketCandle:
    if len(value) < 9:
        raise ValueError("Binance kline must have at least 9 fields.")
    return MarketCandle(
        open_time_ms=int(value[0]),
        close_time_ms=int(value[6]),
        open=float(value[1]),
        high=float(value[2]),
        low=float(value[3]),
        close=float(value[4]),
        quote_volume=float(value[7]),
        trades=int(value[8]),
    )


def _study_config(config: Dict[str, Any]) -> Dict[str, Any]:
    instrumentation = (
        config.get("instrumentation")
        if isinstance(config.get("instrumentation"), dict)
        else {}
    )
    value = instrumentation.get("market_selection_study")
    return value if isinstance(value, dict) else {}


def _validate_settings(
    timeframe: str,
    lookback_days: int,
    decision_interval_hours: int,
    max_symbols: int,
    min_quote_volume: float,
    top_counts: Sequence[int],
    horizons: Sequence[int],
) -> None:
    if _interval_ms(timeframe) != HOUR_MS:
        raise ValueError("Initial market-selection study requires timeframe=1h.")
    if lookback_days < 14:
        raise ValueError("lookback_days must be at least 14.")
    if decision_interval_hours <= 0 or 24 % decision_interval_hours:
        raise ValueError("decision_interval_hours must divide 24.")
    if max_symbols < 1 or min_quote_volume < 0:
        raise ValueError("max_symbols must be positive and volume cannot be negative.")
    if not top_counts or any(value < 1 for value in top_counts):
        raise ValueError("top_counts must contain positive values.")
    if not horizons or any(value < 1 for value in horizons):
        raise ValueError("forward horizons must contain positive hours.")


def _interval_ms(value: str) -> int:
    if value == "1m":
        return 60_000
    if value == "15m":
        return 15 * 60_000
    if value == "1h":
        return HOUR_MS
    if value == "4h":
        return 4 * HOUR_MS
    if value == "1d":
        return 24 * HOUR_MS
    raise ValueError(f"Unsupported timeframe: {value}")


def _floor_ms(value: int, interval_ms: int) -> int:
    return value - value % interval_ms


def _utc_hour(boundary_ms: int) -> int:
    return datetime.fromtimestamp(boundary_ms / 1000, tz=timezone.utc).hour


def _number(value: Any) -> float:
    parsed = _optional_number(value)
    return parsed if parsed is not None else 0.0


def _optional_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt_bps(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}bp"


def _trimmed_mean(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    trim = int(len(ordered) * fraction)
    selected = ordered[trim : len(ordered) - trim] if trim else ordered
    return statistics.fmean(selected)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estudo offline de selecao dos Top Gainers da Binance; nunca envia ordens "
            "nem altera estado, saldo ou slots."
        )
    )
    parser.add_argument("--lookback-days", type=int)
    parser.add_argument("--timeframe")
    parser.add_argument("--decision-interval-hours", type=int)
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--min-quote-volume-usdt", type=float)
    parser.add_argument("--top-count", action="append", type=int)
    parser.add_argument("--horizon-hours", action="append", type=int)
    parser.add_argument("--market-data-url")
    parser.add_argument("--http-timeout-seconds", type=int, default=10)
    parser.add_argument(
        "--cache-dir",
        default=str(PROJECT_ROOT / "data/market_selection/klines"),
    )
    parser.add_argument(
        "--universe-snapshot",
        default=str(PROJECT_ROOT / "data/market_selection/universe.jsonl"),
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    return parser.parse_args()


if __name__ == "__main__":
    main()
