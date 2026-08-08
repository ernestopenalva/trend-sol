from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.market_selection_study import MarketCandle, load_candle_cache


HOUR_MS = 3_600_000
DEFAULT_HORIZONS = (1, 2, 4, 8, 12, 24, 48)
DEFAULT_TARGET_PCTS = (0.25, 0.50, 1.00, 2.00)


@dataclass(frozen=True)
class DonchianConfig:
    channel_hours: int = 20
    fee_pct: float = 0.20
    forward_hours: tuple[int, ...] = DEFAULT_HORIZONS
    target_pcts: tuple[float, ...] = DEFAULT_TARGET_PCTS

    def validate(self) -> None:
        if self.channel_hours < 2:
            raise ValueError("channel_hours must be at least 2")
        if self.fee_pct < 0:
            raise ValueError("fee_pct cannot be negative")
        if not self.forward_hours or any(value < 1 for value in self.forward_hours):
            raise ValueError("forward_hours must be positive")


@dataclass
class DonchianObservation:
    symbol: str
    kind: str
    decision_ms: int
    channel_high: float
    decision_close: float
    breakout_margin_pct: float
    entry_ms: int
    entry_price: float
    forward: dict[str, Any]

    def to_record(self, config: DonchianConfig) -> dict[str, Any]:
        record = asdict(self)
        record["decision_utc"] = _iso(self.decision_ms)
        record["entry_utc"] = _iso(self.entry_ms)
        record["study_version"] = "donchian_20h_geometry_v1"
        record["parameters"] = asdict(config)
        return record


def analyze_symbol(
    symbol: str,
    candles_1h: Sequence[MarketCandle],
    config: DonchianConfig,
) -> tuple[list[DonchianObservation], list[DonchianObservation]]:
    config.validate()
    bars = sorted(candles_1h, key=lambda item: item.open_time_ms)
    signals: list[DonchianObservation] = []
    controls: list[DonchianObservation] = []
    previous_was_breakout = False

    for index in range(config.channel_hours, len(bars) - 1):
        decision = bars[index]
        channel = bars[index - config.channel_hours : index]
        channel_high = max(item.high for item in channel)
        is_breakout = decision.close > channel_high
        is_episode_start = is_breakout and not previous_was_breakout
        entry_index = index + 1
        observation = DonchianObservation(
            symbol=symbol,
            kind="BREAKOUT" if is_episode_start else "CONTROL",
            decision_ms=decision.close_time_ms,
            channel_high=channel_high,
            decision_close=decision.close,
            breakout_margin_pct=_pct(decision.close, channel_high),
            entry_ms=bars[entry_index].open_time_ms,
            entry_price=bars[entry_index].open,
            forward=_forward_metrics(entry_index, bars, config),
        )
        if is_episode_start:
            signals.append(observation)
        else:
            controls.append(observation)
        previous_was_breakout = is_breakout
    return signals, controls


def summarize(
    signals: Sequence[DonchianObservation],
    controls: Sequence[DonchianObservation],
    config: DonchianConfig,
) -> dict[str, Any]:
    by_symbol: dict[str, list[DonchianObservation]] = defaultdict(list)
    for item in signals:
        by_symbol[item.symbol].append(item)
    signal_summary = _observation_summary(signals, config)
    control_summary = _observation_summary(controls, config)
    comparison: dict[str, Any] = {}
    for hours in config.forward_hours:
        horizon = str(hours)
        signal_row = signal_summary["horizons"][horizon]
        control_row = control_summary["horizons"][horizon]
        comparison[horizon] = {
            "signal_events": signal_row["complete_events"],
            "control_events": control_row["complete_events"],
            "median_close_net_uplift_pct": _difference(
                signal_row.get("median_close_net_pct"),
                control_row.get("median_close_net_pct"),
            ),
            "mean_close_net_uplift_pct": _difference(
                signal_row.get("mean_close_net_pct"),
                control_row.get("mean_close_net_pct"),
            ),
            "median_mfe_net_uplift_pct": _difference(
                signal_row.get("median_mfe_net_pct"),
                control_row.get("median_mfe_net_pct"),
            ),
        }
    return {
        "study_version": "donchian_20h_geometry_v1",
        "parameters": asdict(config),
        "signals": len(signals),
        "controls": len(controls),
        "symbols_with_signals": len(by_symbol),
        "signal_summary": signal_summary,
        "control_summary": control_summary,
        "signal_minus_control": comparison,
        "by_symbol": {
            symbol: _observation_summary(items, config)
            for symbol, items in sorted(by_symbol.items())
        },
    }


def write_observations(
    path: Path,
    observations: Sequence[DonchianObservation],
    config: DonchianConfig,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in observations:
            handle.write(
                json.dumps(item.to_record(config), ensure_ascii=False, separators=(",", ":"))
            )
            handle.write("\n")


def select_audit_observations(
    observations: Sequence[DonchianObservation],
    sample_size: int,
) -> list[DonchianObservation]:
    if sample_size <= 0 or not observations:
        return []
    ordered = sorted(observations, key=lambda item: (item.decision_ms, item.symbol))
    count = min(sample_size, len(ordered))
    if count == 1:
        return [ordered[len(ordered) // 2]]
    indexes = [
        round(index * (len(ordered) - 1) / (count - 1))
        for index in range(count)
    ]
    return [ordered[index] for index in indexes]


def _forward_metrics(
    entry_index: int,
    bars: Sequence[MarketCandle],
    config: DonchianConfig,
) -> dict[str, Any]:
    entry = bars[entry_index].open
    horizons: dict[str, Any] = {}
    for hours in config.forward_hours:
        end_index = entry_index + hours
        if end_index > len(bars):
            horizons[str(hours)] = {"complete": False}
            continue
        window = bars[entry_index:end_index]
        close_gross = _pct(window[-1].close, entry)
        mfe_gross = _pct(max(item.high for item in window), entry)
        mae = _pct(min(item.low for item in window), entry)
        horizons[str(hours)] = {
            "complete": True,
            "close_gross_pct": close_gross,
            "close_net_pct": close_gross - config.fee_pct,
            "mfe_gross_pct": mfe_gross,
            "mfe_net_pct": mfe_gross - config.fee_pct,
            "mae_pct": mae,
            "targets_hit": {
                _target_key(target): mfe_gross >= target
                for target in config.target_pcts
            },
        }
    return {"entry_price": entry, "fee_pct": config.fee_pct, "horizons": horizons}


def _observation_summary(
    observations: Sequence[DonchianObservation],
    config: DonchianConfig,
) -> dict[str, Any]:
    result: dict[str, Any] = {"events": len(observations), "horizons": {}}
    for hours in config.forward_hours:
        rows = [
            item.forward["horizons"][str(hours)]
            for item in observations
            if item.forward["horizons"].get(str(hours), {}).get("complete")
        ]
        if not rows:
            result["horizons"][str(hours)] = {"complete_events": 0}
            continue
        closes = [row["close_net_pct"] for row in rows]
        result["horizons"][str(hours)] = {
            "complete_events": len(rows),
            "median_close_net_pct": statistics.median(closes),
            "mean_close_net_pct": sum(closes) / len(closes),
            "mean_close_net_without_best_pct": _mean_without_best(closes),
            "net_positive_close_rate": sum(value > 0 for value in closes) / len(closes),
            "median_mfe_net_pct": statistics.median(
                row["mfe_net_pct"] for row in rows
            ),
            "median_mae_pct": statistics.median(row["mae_pct"] for row in rows),
            "target_hit_rates": {
                _target_key(target): sum(
                    row["targets_hit"][_target_key(target)] for row in rows
                )
                / len(rows)
                for target in config.target_pcts
            },
        }
    return result


def _discover_symbols(universe_cache_dir: Path, hourly_cache_dir: Path) -> list[str]:
    symbols = []
    for path in universe_cache_dir.glob("*_15m.jsonl"):
        symbol = path.name.removesuffix("_15m.jsonl")
        if (universe_cache_dir / f"{symbol}_1m.jsonl").exists() and (
            hourly_cache_dir / f"{symbol}_1h.jsonl"
        ).exists():
            symbols.append(symbol)
    return sorted(symbols)


def _difference(left: Optional[float], right: Optional[float]) -> Optional[float]:
    return left - right if left is not None and right is not None else None


def _mean_without_best(values: Sequence[float]) -> Optional[float]:
    if len(values) <= 1:
        return None
    return (sum(values) - max(values)) / (len(values) - 1)


def _pct(value: float, reference: float) -> float:
    return ((value / reference) - 1) * 100 if reference else 0.0


def _target_key(value: float) -> str:
    return f"{value:.2f}"


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure 20-hour Donchian breakout geometry on cached 1h candles."
    )
    parser.add_argument(
        "--hourly-cache-dir",
        default=str(PROJECT_ROOT / "data" / "market_selection" / "klines"),
    )
    parser.add_argument(
        "--universe-cache-dir",
        default=str(PROJECT_ROOT / "data" / "market_bot_replay" / "klines"),
        help="15m+1m cache used only to freeze the existing 22-symbol universe.",
    )
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument("--channel-hours", type=int, default=20)
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "data" / "studies" / "donchian_breakout_geometry.jsonl"),
    )
    parser.add_argument("--summary-json")
    parser.add_argument("--audit-output")
    parser.add_argument("--audit-size", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    hourly_cache_dir = Path(args.hourly_cache_dir)
    universe_cache_dir = Path(args.universe_cache_dir)
    config = DonchianConfig(channel_hours=args.channel_hours)
    config.validate()
    symbols = sorted(
        set(
            args.symbols
            or _discover_symbols(universe_cache_dir, hourly_cache_dir)
        )
    )
    if not symbols:
        raise SystemExit("No frozen-universe symbols with hourly candles found")

    signals: list[DonchianObservation] = []
    controls: list[DonchianObservation] = []
    coverage: dict[str, Any] = {}
    for symbol in symbols:
        bars = load_candle_cache(hourly_cache_dir / f"{symbol}_1h.jsonl")
        symbol_signals, symbol_controls = analyze_symbol(symbol, bars, config)
        signals.extend(symbol_signals)
        controls.extend(symbol_controls)
        coverage[symbol] = {
            "candles_1h": len(bars),
            "first_1h_utc": _iso(bars[0].open_time_ms) if bars else None,
            "last_1h_utc": _iso(bars[-1].close_time_ms) if bars else None,
            "signals": len(symbol_signals),
        }

    output = Path(args.output)
    write_observations(output, signals, config)
    report = summarize(signals, controls, config)
    report["coverage"] = coverage
    report["output"] = str(output)

    if args.audit_output:
        audit_path = Path(args.audit_output)
        audited = select_audit_observations(signals, args.audit_size)
        write_observations(audit_path, audited, config)
        report["audit_output"] = str(audit_path)
        report["audit_events"] = len(audited)
    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print("TREND-SOL | Donchian 20h breakout geometry v1")
    print(
        f"Symbols: {len(symbols)} | signals: {len(signals)} | "
        f"unconditional controls: {len(controls)}"
    )
    print(f"Signals JSONL: {output}")
    for hours in config.forward_hours:
        signal = report["signal_summary"]["horizons"][str(hours)]
        uplift = report["signal_minus_control"][str(hours)]
        if not signal.get("complete_events"):
            continue
        print(
            f"{hours:>2}h | n={signal['complete_events']} | "
            f"median net={signal['median_close_net_pct']:+.3f}% | "
            f"mean net={signal['mean_close_net_pct']:+.3f}% | "
            f"median MFE net={signal['median_mfe_net_pct']:+.3f}% | "
            f"median MAE={signal['median_mae_pct']:+.3f}% | "
            f"median uplift vs control={uplift['median_close_net_uplift_pct']:+.3f} pp"
        )


if __name__ == "__main__":
    main()
