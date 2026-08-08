from __future__ import annotations

import argparse
import heapq
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.market_selection_study import (
    HOUR_MS,
    MarketCandle,
    load_candle_cache,
    load_universe_snapshot,
)
from tools.trend_gate_study import FIFTEEN_MINUTES_MS, WINDOWS


@dataclass(frozen=True)
class OpportunityConfig:
    upside_pcts: tuple[float, ...] = (0.50, 1.00, 1.50, 2.00)
    downside_pcts: tuple[float, ...] = (0.50, 1.00, 2.00)
    horizons_hours: tuple[int, ...] = (4, 8, 12, 24)
    fee_pct: float = 0.20
    min_quote_volume_24h: float = 10_000_000
    liquidity_candles: int = 96

    def validate(self) -> None:
        if not self.upside_pcts or any(value <= self.fee_pct for value in self.upside_pcts):
            raise ValueError("upside barriers must exceed the round-trip fee")
        if not self.downside_pcts or any(value <= 0 for value in self.downside_pcts):
            raise ValueError("downside barriers must be positive")
        if not self.horizons_hours or any(value <= 0 for value in self.horizons_hours):
            raise ValueError("horizons must be positive")
        if self.fee_pct < 0 or self.min_quote_volume_24h <= 0:
            raise ValueError("invalid fee or liquidity threshold")
        if self.liquidity_candles < 1:
            raise ValueError("liquidity window must be positive")


@dataclass(frozen=True)
class PathOutcome:
    outcome: str
    net_pct: float
    bars_to_resolution: int


@dataclass
class Accumulator:
    observations: int = 0
    wins: int = 0
    stops: int = 0
    timeouts: int = 0
    ambiguous_losses: int = 0
    positives: int = 0
    sum_net_pct: float = 0.0
    gross_profit_pct: float = 0.0
    gross_loss_pct: float = 0.0
    resolution_bars: int = 0
    top_three_net_pct: list[float] = field(default_factory=list)

    def add(self, outcome: PathOutcome) -> None:
        self.observations += 1
        self.sum_net_pct += outcome.net_pct
        self.resolution_bars += outcome.bars_to_resolution
        if outcome.outcome == "target":
            self.wins += 1
        elif outcome.outcome == "stop":
            self.stops += 1
        elif outcome.outcome == "ambiguous_loss":
            self.stops += 1
            self.ambiguous_losses += 1
        else:
            self.timeouts += 1
        if outcome.net_pct > 0:
            self.positives += 1
            self.gross_profit_pct += outcome.net_pct
        elif outcome.net_pct < 0:
            self.gross_loss_pct += -outcome.net_pct
        if len(self.top_three_net_pct) < 3:
            heapq.heappush(self.top_three_net_pct, outcome.net_pct)
        elif outcome.net_pct > self.top_three_net_pct[0]:
            heapq.heapreplace(self.top_three_net_pct, outcome.net_pct)

    def report(self, upside_pct: float, downside_pct: float, fee_pct: float) -> dict[str, Any]:
        if not self.observations:
            return {"observations": 0}
        resolved = self.wins + self.stops
        resolved_win_rate = self.wins / resolved if resolved else None
        theoretical_break_even = (downside_pct + fee_pct) / (upside_pct + downside_pct)
        return {
            "observations": self.observations,
            "target_first_rate": self.wins / self.observations,
            "stop_first_rate": self.stops / self.observations,
            "timeout_rate": self.timeouts / self.observations,
            "ambiguous_loss_rate": self.ambiguous_losses / self.observations,
            "resolved_win_rate": resolved_win_rate,
            "theoretical_break_even_resolved_win_rate": theoretical_break_even,
            "resolved_win_rate_excess": (
                resolved_win_rate - theoretical_break_even
                if resolved_win_rate is not None
                else None
            ),
            "mean_net_pct": self.sum_net_pct / self.observations,
            "mean_net_without_top_1_pct": self._mean_without_top(1),
            "mean_net_without_top_3_pct": self._mean_without_top(3),
            "net_positive_rate": self.positives / self.observations,
            "profit_factor": (
                self.gross_profit_pct / self.gross_loss_pct
                if self.gross_loss_pct > 0
                else None
            ),
            "mean_minutes_to_resolution": 15 * self.resolution_bars / self.observations,
        }

    def _mean_without_top(self, count: int) -> Optional[float]:
        if self.observations <= count:
            return None
        largest = sorted(self.top_three_net_pct, reverse=True)[:count]
        return (self.sum_net_pct - sum(largest)) / (self.observations - count)


def classify_path(
    entry_price: float,
    bars: Sequence[MarketCandle],
    upside_pct: float,
    downside_pct: float,
    fee_pct: float,
) -> PathOutcome:
    if not bars:
        raise ValueError("path must contain at least one candle")
    up_hits, down_hits = _first_hits(
        entry_price, bars, (upside_pct,), (downside_pct,)
    )
    return _classify_from_hits(
        entry_price,
        bars,
        upside_pct,
        downside_pct,
        fee_pct,
        up_hits[upside_pct],
        down_hits[downside_pct],
    )


def run_study(
    candles_by_symbol: dict[str, Sequence[MarketCandle]],
    config: OpportunityConfig,
) -> dict[str, Any]:
    config.validate()
    window_bounds = [
        (f"{start}__{end}", _parse_date(start), _parse_date(end))
        for start, end in WINDOWS
    ]
    accumulators: dict[tuple[str, str], Accumulator] = {}
    symbol_accumulators: dict[tuple[str, str, str], Accumulator] = {}
    eligible: dict[tuple[str, int], int] = {}
    coverage: dict[str, Any] = {}

    for symbol, source in sorted(candles_by_symbol.items()):
        bars = sorted(source, key=lambda item: item.open_time_ms)
        coverage[symbol] = {
            "candles_15m": len(bars),
            "first_15m_utc": _iso(bars[0].open_time_ms) if bars else None,
            "last_15m_utc": _iso(bars[-1].close_time_ms) if bars else None,
        }
        if len(bars) <= config.liquidity_candles:
            continue
        for decision_index in range(config.liquidity_candles - 1, len(bars) - 1):
            decision_candle = bars[decision_index]
            decision_ms = decision_candle.close_time_ms + 1
            window_name = _window_for(decision_ms, window_bounds)
            if window_name is None or decision_ms % HOUR_MS != 0:
                continue
            history_start = decision_index - config.liquidity_candles + 1
            if not _continuous(bars, history_start, decision_index + 1):
                continue
            quote_volume = sum(
                candle.quote_volume for candle in bars[history_start : decision_index + 1]
            )
            if quote_volume < config.min_quote_volume_24h:
                continue
            entry_index = decision_index + 1
            if bars[entry_index].open_time_ms != decision_ms:
                continue
            entry_price = bars[entry_index].open
            max_count = max(config.horizons_hours) * 4
            future = bars[entry_index : entry_index + max_count]
            continuous_count = _continuous_prefix_length(future)
            if not continuous_count:
                continue
            future = future[:continuous_count]
            up_hits, down_hits = _first_hits(
                entry_price, future, config.upside_pcts, config.downside_pcts
            )
            for hours in config.horizons_hours:
                count = hours * 4
                if continuous_count < count:
                    continue
                path = future[:count]
                eligible[(window_name, hours)] = eligible.get((window_name, hours), 0) + 1
                for upside_pct in config.upside_pcts:
                    for downside_pct in config.downside_pcts:
                        key = _geometry_key(upside_pct, downside_pct, hours)
                        outcome = _classify_from_hits(
                            entry_price,
                            path,
                            upside_pct,
                            downside_pct,
                            config.fee_pct,
                            up_hits[upside_pct],
                            down_hits[downside_pct],
                        )
                        accumulators.setdefault((window_name, key), Accumulator()).add(outcome)
                        symbol_accumulators.setdefault(
                            (window_name, symbol, key), Accumulator()
                        ).add(outcome)

    report: dict[str, Any] = {
        "study_version": "opportunity_geometry_v1",
        "parameters": asdict(config),
        "method_note": (
            "Hourly opportunity census; entry at next 15m open; same-candle target/stop "
            "touches count as losses; overlapping observations are not a portfolio backtest."
        ),
        "windows": {},
        "coverage": coverage,
    }
    for window_name, start_ms, end_ms in window_bounds:
        geometries: dict[str, Any] = {}
        by_symbol: dict[str, Any] = {}
        for hours in config.horizons_hours:
            for upside_pct in config.upside_pcts:
                for downside_pct in config.downside_pcts:
                    key = _geometry_key(upside_pct, downside_pct, hours)
                    acc = accumulators.get((window_name, key), Accumulator())
                    geometries[key] = {
                        "upside_pct": upside_pct,
                        "downside_pct": downside_pct,
                        "horizon_hours": hours,
                        **acc.report(upside_pct, downside_pct, config.fee_pct),
                    }
        symbols = sorted(
            symbol
            for symbol in candles_by_symbol
            if any((window_name, symbol, key) in symbol_accumulators for key in geometries)
        )
        for symbol in symbols:
            by_symbol[symbol] = {}
            for key, geometry in geometries.items():
                acc = symbol_accumulators.get((window_name, symbol, key), Accumulator())
                by_symbol[symbol][key] = acc.report(
                    geometry["upside_pct"], geometry["downside_pct"], config.fee_pct
                )
        report["windows"][window_name] = {
            "start_utc": _iso(start_ms),
            "end_exclusive_utc": _iso(end_ms),
            "eligible_observations_by_horizon": {
                str(hours): eligible.get((window_name, hours), 0)
                for hours in config.horizons_hours
            },
            "geometries": geometries,
            "best_by_mean_net_pct": _rank(geometries, "mean_net_pct"),
            "best_by_resolved_edge": _rank(geometries, "resolved_win_rate_excess"),
            "by_symbol": by_symbol,
        }
    report["cross_window_stability"] = _stability(report["windows"])
    return report


def _rank(geometries: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    rows = [
        {"geometry": key, field_name: row.get(field_name), "observations": row.get("observations")}
        for key, row in geometries.items()
        if row.get(field_name) is not None
    ]
    return sorted(rows, key=lambda row: row[field_name], reverse=True)[:10]


def _first_hits(
    entry_price: float,
    bars: Sequence[MarketCandle],
    upside_pcts: Sequence[float],
    downside_pcts: Sequence[float],
) -> tuple[dict[float, Optional[int]], dict[float, Optional[int]]]:
    up_hits: dict[float, Optional[int]] = {value: None for value in upside_pcts}
    down_hits: dict[float, Optional[int]] = {value: None for value in downside_pcts}
    upper_prices = {
        value: entry_price * (1 + value / 100) for value in upside_pcts
    }
    lower_prices = {
        value: entry_price * (1 - value / 100) for value in downside_pcts
    }
    for index, candle in enumerate(bars):
        for value, price in upper_prices.items():
            if up_hits[value] is None and candle.high >= price:
                up_hits[value] = index
        for value, price in lower_prices.items():
            if down_hits[value] is None and candle.low <= price:
                down_hits[value] = index
    return up_hits, down_hits


def _classify_from_hits(
    entry_price: float,
    bars: Sequence[MarketCandle],
    upside_pct: float,
    downside_pct: float,
    fee_pct: float,
    up_index: Optional[int],
    down_index: Optional[int],
) -> PathOutcome:
    horizon = len(bars)
    up_index = up_index if up_index is not None and up_index < horizon else None
    down_index = down_index if down_index is not None and down_index < horizon else None
    if up_index is not None and down_index is not None and up_index == down_index:
        return PathOutcome("ambiguous_loss", -downside_pct - fee_pct, down_index + 1)
    if down_index is not None and (up_index is None or down_index < up_index):
        return PathOutcome("stop", -downside_pct - fee_pct, down_index + 1)
    if up_index is not None:
        return PathOutcome("target", upside_pct - fee_pct, up_index + 1)
    close_pct = _pct(bars[-1].close, entry_price)
    return PathOutcome("timeout", close_pct - fee_pct, horizon)


def _stability(windows: dict[str, Any]) -> list[dict[str, Any]]:
    keys = sorted(
        set.intersection(
            *(set(window["geometries"]) for window in windows.values())
        )
    ) if windows else []
    rows = []
    for key in keys:
        values = [
            window["geometries"][key].get("mean_net_pct")
            for window in windows.values()
        ]
        edges = [
            window["geometries"][key].get("resolved_win_rate_excess")
            for window in windows.values()
        ]
        if any(value is None for value in values) or any(value is None for value in edges):
            continue
        rows.append({
            "geometry": key,
            "positive_windows": sum(value > 0 for value in values),
            "average_mean_net_pct": statistics.fmean(values),
            "worst_window_mean_net_pct": min(values),
            "average_resolved_win_rate_excess": statistics.fmean(edges),
            "worst_resolved_win_rate_excess": min(edges),
        })
    return sorted(
        rows,
        key=lambda row: (
            row["positive_windows"],
            row["worst_window_mean_net_pct"],
            row["average_mean_net_pct"],
        ),
        reverse=True,
    )


def load_cached_candles(cache_dir: Path, symbols: Sequence[str]) -> dict[str, list[MarketCandle]]:
    return {
        symbol: load_candle_cache(cache_dir / f"{symbol}_15m.jsonl")
        for symbol in symbols
    }


def _window_for(
    timestamp_ms: int,
    windows: Sequence[tuple[str, int, int]],
) -> Optional[str]:
    for name, start_ms, end_ms in windows:
        if start_ms <= timestamp_ms < end_ms:
            return name
    return None


def _continuous(bars: Sequence[MarketCandle], start: int, end: int) -> bool:
    if start < 0 or start >= end or end > len(bars):
        return False
    return all(
        bars[index].open_time_ms - bars[index - 1].open_time_ms == FIFTEEN_MINUTES_MS
        for index in range(start + 1, end)
    )


def _continuous_prefix_length(bars: Sequence[MarketCandle]) -> int:
    if not bars:
        return 0
    for index in range(1, len(bars)):
        if bars[index].open_time_ms - bars[index - 1].open_time_ms != FIFTEEN_MINUTES_MS:
            return index
    return len(bars)


def _geometry_key(upside_pct: float, downside_pct: float, hours: int) -> str:
    return f"up_{upside_pct:.2f}__down_{downside_pct:.2f}__hours_{hours}"


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
        description="Map upward opportunity geometry before selecting any trend gate."
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
        default=str(PROJECT_ROOT / "data" / "studies" / "opportunity_geometry"),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    config = OpportunityConfig()
    markets = load_universe_snapshot(Path(args.universe_snapshot))
    symbols = sorted({item.symbol for item in markets})
    candles = load_cached_candles(Path(args.cache_dir), symbols)
    report = run_study(candles, config)
    report["symbols_in_snapshot"] = symbols
    report["universe_note"] = (
        "Current listed-market snapshot with historical trailing-volume eligibility; "
        "delisted markets remain unavailable (survivorship bias)."
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "summary.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("TREND-SOL | geometria de oportunidades sem gate")
    for window_name, window in report["windows"].items():
        best = window["best_by_mean_net_pct"][0]
        geometry = window["geometries"][best["geometry"]]
        print(
            f"{window_name}: melhor media={best['geometry']} | "
            f"n={geometry['observations']} | net={geometry['mean_net_pct']:+.3f}% | "
            f"PF={geometry['profit_factor']:.3f} | target={geometry['target_first_rate']:.1%}"
        )
    print("Geometrias mais estaveis:")
    for row in report["cross_window_stability"][:5]:
        print(
            f"  {row['geometry']} | janelas positivas={row['positive_windows']}/3 | "
            f"pior={row['worst_window_mean_net_pct']:+.3f}% | "
            f"media={row['average_mean_net_pct']:+.3f}%"
        )
    print(f"Resumo JSON: {output_path}")


if __name__ == "__main__":
    main()
