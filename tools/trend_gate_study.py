from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.indicators.indicators import atr, ema
from tools.market_selection_study import (
    HOUR_MS,
    BinancePublicClient,
    MarketCandle,
    load_candle_cache,
    load_universe_snapshot,
    merge_candles,
    save_candle_cache,
)


MINUTE_MS = 60_000
FIFTEEN_MINUTES_MS = 15 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
WINDOWS = (
    ("2025-10-01", "2026-02-01"),
    ("2026-02-01", "2026-04-19"),
    ("2026-04-19", "2026-08-07"),
)


@dataclass(frozen=True)
class TrendGateConfig:
    ema_candles: int = 50
    ema_slope_lookback_candles: int = 3
    efficiency_lookback_candles: int = 16
    minimum_advance_atr: float = 2.0
    minimum_efficiency: float = 0.35
    atr_candles: int = 14
    min_quote_volume_24h: float = 10_000_000
    fee_pct: float = 0.20
    forward_hours: tuple[int, ...] = (1, 2, 4, 8)

    def validate(self) -> None:
        if self.ema_candles < 2 or self.ema_slope_lookback_candles < 1:
            raise ValueError("invalid EMA settings")
        if self.efficiency_lookback_candles < 2:
            raise ValueError("efficiency lookback must be at least 2")
        if self.minimum_advance_atr <= 0:
            raise ValueError("minimum advance must be positive")
        if not 0 < self.minimum_efficiency <= 1:
            raise ValueError("efficiency must be between 0 and 1")
        if self.atr_candles < 1 or self.min_quote_volume_24h <= 0:
            raise ValueError("invalid ATR or liquidity settings")


@dataclass(frozen=True)
class GateObservation:
    symbol: str
    decision_ms: int
    reference_ms: int
    reference_price: float
    gate_a_ema_slope: bool
    gate_b_efficiency: bool
    ema_current: float
    ema_previous: float
    atr_15m: float
    advance: float
    advance_atr: float
    efficiency: float
    quote_volume_24h: float
    forward: dict[str, Any]

    def to_record(self, config: TrendGateConfig) -> dict[str, Any]:
        record = asdict(self)
        record["decision_utc"] = _iso(self.decision_ms)
        record["reference_utc"] = _iso(self.reference_ms)
        record["study_version"] = "trend_gate_ema_vs_efficiency_v1"
        record["parameters"] = asdict(config)
        return record


def evaluate_efficiency_gate(
    closes: Sequence[float],
    index: int,
    atr_value: float,
    config: TrendGateConfig,
) -> tuple[bool, float, float, float]:
    start = index - config.efficiency_lookback_candles
    if start < 0 or atr_value <= 0:
        return False, 0.0, 0.0, 0.0
    path_values = closes[start : index + 1]
    advance = float(path_values[-1]) - float(path_values[0])
    path = sum(
        abs(float(path_values[item]) - float(path_values[item - 1]))
        for item in range(1, len(path_values))
    )
    efficiency = advance / path if path > 0 else 0.0
    advance_atr = advance / atr_value
    passed = (
        advance_atr >= config.minimum_advance_atr
        and efficiency >= config.minimum_efficiency
    )
    return passed, advance, advance_atr, efficiency


def analyze_symbol(
    symbol: str,
    candles_15m: Sequence[MarketCandle],
    config: TrendGateConfig,
    start_ms: int,
    end_ms: int,
) -> list[GateObservation]:
    config.validate()
    bars = sorted(candles_15m, key=lambda item: item.open_time_ms)
    if not bars:
        return []
    closes = [item.close for item in bars]
    ema_values = ema(closes, config.ema_candles)
    atr_values = atr(
        [item.high for item in bars],
        [item.low for item in bars],
        closes,
        config.atr_candles,
    )
    warmup = max(
        95,
        config.ema_candles - 1 + config.ema_slope_lookback_candles,
        config.efficiency_lookback_candles,
    )
    observations: list[GateObservation] = []
    for index in range(warmup, len(bars) - 1):
        candle = bars[index]
        boundary_ms = candle.close_time_ms + 1
        if boundary_ms < start_ms or boundary_ms >= end_ms:
            continue
        if boundary_ms % HOUR_MS != 0:
            continue
        if not _continuous(bars, index - warmup, index + 1):
            continue
        current_ema = ema_values[index]
        previous_ema = ema_values[index - config.ema_slope_lookback_candles]
        current_atr = atr_values[index]
        if current_ema is None or previous_ema is None or current_atr is None:
            continue
        quote_volume = sum(item.quote_volume for item in bars[index - 95 : index + 1])
        if quote_volume < config.min_quote_volume_24h:
            continue
        gate_b, advance, advance_atr, efficiency = evaluate_efficiency_gate(
            closes,
            index,
            float(current_atr),
            config,
        )
        next_candle = bars[index + 1]
        if next_candle.open_time_ms != boundary_ms:
            continue
        observations.append(
            GateObservation(
                symbol=symbol,
                decision_ms=boundary_ms,
                reference_ms=next_candle.open_time_ms,
                reference_price=next_candle.open,
                gate_a_ema_slope=float(current_ema) > float(previous_ema),
                gate_b_efficiency=gate_b,
                ema_current=float(current_ema),
                ema_previous=float(previous_ema),
                atr_15m=float(current_atr),
                advance=advance,
                advance_atr=advance_atr,
                efficiency=efficiency,
                quote_volume_24h=quote_volume,
                forward=_forward_metrics(index + 1, bars, config),
            )
        )
    return observations


def summarize(
    observations: Sequence[GateObservation],
    config: TrendGateConfig,
) -> dict[str, Any]:
    gates: dict[str, Callable[[GateObservation], bool]] = {
        "control": lambda _: True,
        "gate_a_ema_slope": lambda item: item.gate_a_ema_slope,
        "gate_b_efficiency": lambda item: item.gate_b_efficiency,
    }
    report: dict[str, Any] = {
        "study_version": "trend_gate_ema_vs_efficiency_v1",
        "parameters": asdict(config),
        "windows": {},
    }
    for start_text, end_text in WINDOWS:
        start_ms = _parse_date(start_text)
        end_ms = _parse_date(end_text)
        window_items = [
            item for item in observations if start_ms <= item.decision_ms < end_ms
        ]
        gate_reports = {
            name: _gate_summary(window_items, predicate, config)
            for name, predicate in gates.items()
        }
        comparisons = {}
        for hours in config.forward_hours:
            horizon = str(hours)
            control = gate_reports["control"]["horizons"][horizon]
            gate_a = gate_reports["gate_a_ema_slope"]["horizons"][horizon]
            gate_b = gate_reports["gate_b_efficiency"]["horizons"][horizon]
            comparisons[horizon] = {
                "gate_a_minus_control": _comparison(gate_a, control),
                "gate_b_minus_control": _comparison(gate_b, control),
                "gate_b_minus_gate_a": _comparison(gate_b, gate_a),
            }
        report["windows"][f"{start_text}__{end_text}"] = {
            "start_utc": _iso(start_ms),
            "end_exclusive_utc": _iso(end_ms),
            "eligible_observations": len(window_items),
            "gates": gate_reports,
            "comparisons": comparisons,
            "by_symbol_4h": _by_symbol(window_items, config, hours=4),
        }
    return report


def write_observations(
    path: Path,
    observations: Sequence[GateObservation],
    config: TrendGateConfig,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in observations:
            if not item.gate_a_ema_slope and not item.gate_b_efficiency:
                continue
            handle.write(
                json.dumps(item.to_record(config), ensure_ascii=False, separators=(",", ":"))
            )
            handle.write("\n")


def _forward_metrics(
    entry_index: int,
    bars: Sequence[MarketCandle],
    config: TrendGateConfig,
) -> dict[str, Any]:
    entry = bars[entry_index].open
    output: dict[str, Any] = {"horizons": {}}
    for hours in config.forward_hours:
        count = hours * 4
        end_index = entry_index + count
        window = bars[entry_index:end_index]
        if len(window) != count or not _continuous(window, 0, len(window)):
            output["horizons"][str(hours)] = {"complete": False}
            continue
        close_gross = _pct(window[-1].close, entry)
        mfe_gross = _pct(max(item.high for item in window), entry)
        mae = _pct(min(item.low for item in window), entry)
        output["horizons"][str(hours)] = {
            "complete": True,
            "close_gross_pct": close_gross,
            "close_net_pct": close_gross - config.fee_pct,
            "mfe_gross_pct": mfe_gross,
            "mfe_net_pct": mfe_gross - config.fee_pct,
            "mae_pct": mae,
            "adverse_1pct": mae <= -1.0,
            "adverse_2pct": mae <= -2.0,
        }
    return output


def _gate_summary(
    observations: Sequence[GateObservation],
    predicate: Callable[[GateObservation], bool],
    config: TrendGateConfig,
) -> dict[str, Any]:
    selected = [item for item in observations if predicate(item)]
    result: dict[str, Any] = {
        "observations": len(selected),
        "approval_rate": len(selected) / len(observations) if observations else None,
        "symbols": len({item.symbol for item in selected}),
        "horizons": {},
    }
    for hours in config.forward_hours:
        rows = [
            item.forward["horizons"][str(hours)]
            for item in selected
            if item.forward["horizons"].get(str(hours), {}).get("complete")
        ]
        if not rows:
            result["horizons"][str(hours)] = {"complete_observations": 0}
            continue
        net = [row["close_net_pct"] for row in rows]
        result["horizons"][str(hours)] = {
            "complete_observations": len(rows),
            "mean_close_gross_pct": statistics.fmean(
                row["close_gross_pct"] for row in rows
            ),
            "median_close_gross_pct": statistics.median(
                row["close_gross_pct"] for row in rows
            ),
            "mean_close_net_pct": statistics.fmean(net),
            "median_close_net_pct": statistics.median(net),
            "mean_close_net_without_top_1_pct": _mean_without_top(net, 1),
            "mean_close_net_without_top_3_pct": _mean_without_top(net, 3),
            "median_mfe_net_pct": statistics.median(row["mfe_net_pct"] for row in rows),
            "median_mae_pct": statistics.median(row["mae_pct"] for row in rows),
            "net_positive_rate": sum(value > 0 for value in net) / len(rows),
            "adverse_1pct_rate": sum(row["adverse_1pct"] for row in rows) / len(rows),
            "adverse_2pct_rate": sum(row["adverse_2pct"] for row in rows) / len(rows),
        }
    return result


def _comparison(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {
        "mean_close_net_uplift_pct": _difference(
            left.get("mean_close_net_pct"), right.get("mean_close_net_pct")
        ),
        "median_close_net_uplift_pct": _difference(
            left.get("median_close_net_pct"), right.get("median_close_net_pct")
        ),
        "adverse_2pct_rate_delta": _difference(
            left.get("adverse_2pct_rate"), right.get("adverse_2pct_rate")
        ),
    }


def _by_symbol(
    observations: Sequence[GateObservation],
    config: TrendGateConfig,
    hours: int,
) -> dict[str, Any]:
    grouped: dict[str, list[GateObservation]] = defaultdict(list)
    for item in observations:
        grouped[item.symbol].append(item)
    output = {}
    for symbol, items in sorted(grouped.items()):
        output[symbol] = {
            "gate_a": _gate_summary(items, lambda value: value.gate_a_ema_slope, config)[
                "horizons"
            ][str(hours)],
            "gate_b": _gate_summary(items, lambda value: value.gate_b_efficiency, config)[
                "horizons"
            ][str(hours)],
        }
    return output


def download_candles(
    client: BinancePublicClient,
    symbols: Sequence[str],
    cache_dir: Path,
    start_ms: int,
    end_ms: int,
    download_missing: bool,
) -> dict[str, list[MarketCandle]]:
    output = {}
    for number, symbol in enumerate(symbols, start=1):
        path = cache_dir / f"{symbol}_15m.jsonl"
        cached = load_candle_cache(path)
        downloaded: list[MarketCandle] = []
        if download_missing:
            if not cached:
                downloaded = client.klines(symbol, "15m", start_ms, end_ms)
            else:
                first = min(item.open_time_ms for item in cached)
                last = max(item.close_time_ms for item in cached)
                if first > start_ms:
                    downloaded.extend(
                        client.klines(symbol, "15m", start_ms, min(first - 1, end_ms))
                    )
                if last < end_ms:
                    downloaded.extend(
                        client.klines(symbol, "15m", max(last + 1, start_ms), end_ms)
                    )
        merged = merge_candles(cached, downloaded)
        if downloaded:
            save_candle_cache(path, merged)
        selected = [
            item
            for item in merged
            if start_ms <= item.open_time_ms and item.close_time_ms <= end_ms
        ]
        output[symbol] = selected
        print(
            f"[{number:>2}/{len(symbols)}] {symbol}: "
            f"{len(selected)} candles de 15m ({len(downloaded)} baixados)"
        )
    return output


def _continuous(bars: Sequence[MarketCandle], start: int, end: int) -> bool:
    if start < 0 or end > len(bars) or start >= end:
        return False
    return all(
        bars[index].open_time_ms - bars[index - 1].open_time_ms == FIFTEEN_MINUTES_MS
        for index in range(start + 1, end)
    )


def _mean_without_top(values: Sequence[float], count: int) -> Optional[float]:
    if len(values) <= count:
        return None
    ordered = sorted(values, reverse=True)[count:]
    return statistics.fmean(ordered)


def _difference(left: Optional[float], right: Optional[float]) -> Optional[float]:
    return left - right if left is not None and right is not None else None


def _pct(value: float, reference: float) -> float:
    return ((value / reference) - 1) * 100 if reference else 0.0


def _parse_date(value: str) -> int:
    return int(
        datetime.strptime(value, "%Y-%m-%d")
        .replace(tzinfo=timezone.utc)
        .timestamp()
        * 1000
    )


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare the current EMA trend gate with an ATR/efficiency gate."
    )
    parser.add_argument(
        "--universe-snapshot",
        default=str(PROJECT_ROOT / "data" / "market_selection" / "universe.jsonl"),
    )
    parser.add_argument(
        "--cache-dir",
        default=str(PROJECT_ROOT / "data" / "studies" / "trend_gate" / "klines"),
    )
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "data" / "studies" / "trend_gate"),
    )
    parser.add_argument("--market-data-url", default="https://api.binance.com")
    parser.add_argument("--http-timeout-seconds", type=int, default=15)
    parser.add_argument("--offline", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = TrendGateConfig()
    config.validate()
    markets = load_universe_snapshot(Path(args.universe_snapshot))
    symbols = sorted({item.symbol for item in markets})
    if not symbols:
        raise SystemExit("Universe snapshot is empty")
    data_start_ms = _parse_date("2025-09-24")
    data_end_ms = _parse_date("2026-08-07") - 1
    client = BinancePublicClient(args.market_data_url, args.http_timeout_seconds)
    candles = download_candles(
        client,
        symbols,
        Path(args.cache_dir),
        data_start_ms,
        data_end_ms,
        download_missing=not args.offline,
    )
    observations = []
    study_start_ms = _parse_date(WINDOWS[0][0])
    study_end_ms = _parse_date(WINDOWS[-1][1])
    for symbol, items in candles.items():
        observations.extend(
            analyze_symbol(symbol, items, config, study_start_ms, study_end_ms)
        )
    observations.sort(key=lambda item: (item.decision_ms, item.symbol))
    report = summarize(observations, config)
    report["symbols_in_snapshot"] = symbols
    report["observations"] = len(observations)
    report["coverage"] = {
        symbol: {
            "candles_15m": len(items),
            "first_15m_utc": _iso(items[0].open_time_ms) if items else None,
            "last_15m_utc": _iso(items[-1].close_time_ms) if items else None,
        }
        for symbol, items in candles.items()
    }
    report["universe_note"] = (
        "Current listed-market snapshot with historical trailing-volume eligibility; "
        "delisted markets remain unavailable (survivorship bias)."
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    observations_path = output_dir / "gate_observations.jsonl"
    summary_path = output_dir / "summary.json"
    write_observations(observations_path, observations, config)
    summary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("TREND-SOL | estudo isolado do Gate 1")
    print(f"Símbolos no snapshot: {len(symbols)} | observações elegíveis: {len(observations)}")
    for window_name, window in report["windows"].items():
        print(window_name)
        for gate_name in ("control", "gate_a_ema_slope", "gate_b_efficiency"):
            gate = window["gates"][gate_name]
            row = gate["horizons"]["4"]
            print(
                f"  {gate_name:22} n={row['complete_observations']:>5} | "
                f"approval={gate['approval_rate']:.1%} | "
                f"mean net 4h={row['mean_close_net_pct']:+.3f}% | "
                f"median net 4h={row['median_close_net_pct']:+.3f}% | "
                f"queda 2%={row['adverse_2pct_rate']:.1%}"
            )
    print(f"Resumo JSON: {summary_path}")
    print(f"Observações JSONL: {observations_path}")


if __name__ == "__main__":
    main()
