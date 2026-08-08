from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.market_selection_study import MarketCandle, load_candle_cache


@dataclass(frozen=True)
class PortfolioConfig:
    entry_channel_hours: int = 20
    exit_channel_hours: int = 10
    hard_stop_pct: float = 2.0
    fee_per_side_pct: float = 0.10
    notional_usdt: float = 20.0
    max_positions: int = 5
    operational_balance_usdt: float = 100.0
    admission_policy: str = "strongest"
    min_quote_volume_24h: Optional[float] = None

    def validate(self) -> None:
        if self.entry_channel_hours < 2 or self.exit_channel_hours < 1:
            raise ValueError("invalid Donchian channel")
        if self.hard_stop_pct <= 0 or self.hard_stop_pct >= 100:
            raise ValueError("hard stop must be between 0 and 100")
        if (
            self.fee_per_side_pct < 0
            or self.notional_usdt <= 0
            or self.operational_balance_usdt <= 0
        ):
            raise ValueError("invalid fees, notional, or operational balance")
        if self.max_positions < 1:
            raise ValueError("max_positions must be positive")
        if self.admission_policy not in {
            "strongest",
            "alphabetical",
            "reverse_alphabetical",
        }:
            raise ValueError(f"unsupported admission policy: {self.admission_policy}")
        if self.min_quote_volume_24h is not None and self.min_quote_volume_24h <= 0:
            raise ValueError("min_quote_volume_24h must be positive when enabled")


@dataclass(frozen=True)
class EntryCandidate:
    symbol: str
    decision_ms: int
    execute_ms: int
    decision_close: float
    channel_high: float
    breakout_margin_pct: float


@dataclass
class OpenPosition:
    symbol: str
    entry_ms: int
    entry_price: float
    quantity: float
    notional_usdt: float
    hard_stop_price: float
    signal_decision_ms: int
    breakout_margin_pct: float
    pending_exit_ms: Optional[int] = None


@dataclass(frozen=True)
class PortfolioTrade:
    symbol: str
    opened_ms: int
    closed_ms: int
    entry_price: float
    exit_price: float
    quantity: float
    notional_usdt: float
    gross_pct: float
    net_pct: float
    gross_usdt: float
    fees_usdt: float
    net_usdt: float
    exit_reason: str
    holding_hours: float
    breakout_margin_pct: float

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        record["opened_utc"] = _iso(self.opened_ms)
        record["closed_utc"] = _iso(self.closed_ms)
        record["study_version"] = "donchian_20h_10h_portfolio_v1"
        return record


@dataclass
class PortfolioReplayResult:
    trades: list[PortfolioTrade] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    raw_signals: int = 0
    executed_entries: int = 0
    blocked_slots: int = 0
    blocked_symbol: int = 0
    maximum_concurrent_positions: int = 0


def replay_portfolio(
    candles_by_symbol: dict[str, Sequence[MarketCandle]],
    config: PortfolioConfig,
    replay_start_ms: Optional[int] = None,
    replay_end_ms: Optional[int] = None,
) -> PortfolioReplayResult:
    config.validate()
    bars = {
        symbol: sorted(items, key=lambda item: item.open_time_ms)
        for symbol, items in candles_by_symbol.items()
        if items
    }
    indexes = {
        symbol: {item.open_time_ms: index for index, item in enumerate(items)}
        for symbol, items in bars.items()
    }
    all_times = sorted(
        {item.open_time_ms for items in bars.values() for item in items}
    )
    pending_entries: dict[int, list[EntryCandidate]] = defaultdict(list)
    pending_exits: dict[int, list[str]] = defaultdict(list)
    positions: dict[str, OpenPosition] = {}
    result = PortfolioReplayResult()

    for timestamp_ms in all_times:
        if replay_end_ms is not None and timestamp_ms > replay_end_ms:
            break
        current = {
            symbol: (indexes[symbol][timestamp_ms], items[indexes[symbol][timestamp_ms]])
            for symbol, items in bars.items()
            if timestamp_ms in indexes[symbol]
        }

        for symbol in sorted(pending_exits.pop(timestamp_ms, [])):
            position = positions.get(symbol)
            current_bar = current.get(symbol)
            if position is None or current_bar is None:
                continue
            _, candle = current_bar
            result.trades.append(
                _close_position(
                    position,
                    candle.open,
                    timestamp_ms,
                    "DONCHIAN_EXIT",
                    config,
                )
            )
            del positions[symbol]
            result.decisions.append(
                _decision("position_closed", timestamp_ms, symbol, reason="DONCHIAN_EXIT")
            )

        candidates = _ordered_candidates(
            pending_entries.pop(timestamp_ms, []),
            config.admission_policy,
        )
        for candidate in candidates:
            if candidate.symbol in positions:
                result.blocked_symbol += 1
                result.decisions.append(
                    _decision(
                        "entry_blocked",
                        timestamp_ms,
                        candidate.symbol,
                        reason="symbol_already_open",
                    )
                )
                continue
            current_bar = current.get(candidate.symbol)
            if current_bar is None:
                continue
            if len(positions) >= config.max_positions:
                result.blocked_slots += 1
                result.decisions.append(
                    _decision(
                        "entry_blocked",
                        timestamp_ms,
                        candidate.symbol,
                        reason="portfolio_full",
                        breakout_margin_pct=candidate.breakout_margin_pct,
                    )
                )
                continue
            _, candle = current_bar
            quantity = config.notional_usdt / candle.open
            positions[candidate.symbol] = OpenPosition(
                symbol=candidate.symbol,
                entry_ms=timestamp_ms,
                entry_price=candle.open,
                quantity=quantity,
                notional_usdt=config.notional_usdt,
                hard_stop_price=candle.open * (1 - config.hard_stop_pct / 100),
                signal_decision_ms=candidate.decision_ms,
                breakout_margin_pct=candidate.breakout_margin_pct,
            )
            result.executed_entries += 1
            result.maximum_concurrent_positions = max(
                result.maximum_concurrent_positions,
                len(positions),
            )
            result.decisions.append(
                _decision(
                    "position_opened",
                    timestamp_ms,
                    candidate.symbol,
                    entry_price=candle.open,
                    breakout_margin_pct=candidate.breakout_margin_pct,
                )
            )

        for symbol in sorted(list(positions)):
            current_bar = current.get(symbol)
            if current_bar is None:
                continue
            _, candle = current_bar
            position = positions[symbol]
            stop_exit = None
            if candle.open <= position.hard_stop_price:
                stop_exit = candle.open
            elif candle.low <= position.hard_stop_price:
                stop_exit = position.hard_stop_price
            if stop_exit is None:
                continue
            result.trades.append(
                _close_position(
                    position,
                    stop_exit,
                    candle.close_time_ms,
                    "HARD_STOP",
                    config,
                )
            )
            del positions[symbol]
            result.decisions.append(
                _decision(
                    "position_closed",
                    candle.close_time_ms,
                    symbol,
                    reason="HARD_STOP",
                    exit_price=stop_exit,
                )
            )

        for symbol, (index, candle) in sorted(current.items()):
            symbol_bars = bars[symbol]
            position = positions.get(symbol)
            if position is not None and position.pending_exit_ms is None:
                if index >= config.exit_channel_hours:
                    prior_exit_channel = symbol_bars[
                        index - config.exit_channel_hours : index
                    ]
                    channel_low = min(item.low for item in prior_exit_channel)
                    if candle.close < channel_low and index + 1 < len(symbol_bars):
                        execute_ms = symbol_bars[index + 1].open_time_ms
                        position.pending_exit_ms = execute_ms
                        pending_exits[execute_ms].append(symbol)
                        result.decisions.append(
                            _decision(
                                "exit_scheduled",
                                candle.close_time_ms,
                                symbol,
                                execute_ms=execute_ms,
                                decision_close=candle.close,
                                channel_low=channel_low,
                            )
                        )
                continue
            if position is not None or index < config.entry_channel_hours:
                continue
            if index + 1 >= len(symbol_bars):
                continue
            if replay_start_ms is not None and candle.close_time_ms < replay_start_ms:
                continue
            if replay_end_ms is not None and symbol_bars[index + 1].open_time_ms > replay_end_ms:
                continue
            if config.min_quote_volume_24h is not None:
                if index < 23:
                    continue
                trailing_quote_volume = sum(
                    item.quote_volume for item in symbol_bars[index - 23 : index + 1]
                )
                if trailing_quote_volume < config.min_quote_volume_24h:
                    continue
            prior_entry_channel = symbol_bars[
                index - config.entry_channel_hours : index
            ]
            channel_high = max(item.high for item in prior_entry_channel)
            if candle.close <= channel_high:
                continue
            next_open_ms = symbol_bars[index + 1].open_time_ms
            candidate = EntryCandidate(
                symbol=symbol,
                decision_ms=candle.close_time_ms,
                execute_ms=next_open_ms,
                decision_close=candle.close,
                channel_high=channel_high,
                breakout_margin_pct=_pct(candle.close, channel_high),
            )
            pending_entries[next_open_ms].append(candidate)
            result.raw_signals += 1
            result.decisions.append(
                _decision(
                    "entry_scheduled",
                    candle.close_time_ms,
                    symbol,
                    execute_ms=next_open_ms,
                    decision_close=candle.close,
                    channel_high=channel_high,
                    breakout_margin_pct=candidate.breakout_margin_pct,
                )
            )

    for symbol, position in sorted(positions.items()):
        eligible_bars = [
            item
            for item in bars[symbol]
            if replay_end_ms is None or item.open_time_ms <= replay_end_ms
        ]
        if not eligible_bars:
            continue
        last = eligible_bars[-1]
        result.trades.append(
            _close_position(
                position,
                last.close,
                last.close_time_ms,
                "END_OF_DATA",
                config,
            )
        )
        result.decisions.append(
            _decision(
                "position_closed",
                last.close_time_ms,
                symbol,
                reason="END_OF_DATA",
            )
        )
    result.trades.sort(key=lambda item: (item.closed_ms, item.symbol))
    return result


def summarize(result: PortfolioReplayResult, config: PortfolioConfig) -> dict[str, Any]:
    trades = result.trades
    net_values = [item.net_usdt for item in trades]
    winners = [value for value in net_values if value > 0]
    losers = [value for value in net_values if value < 0]
    by_symbol: dict[str, list[PortfolioTrade]] = defaultdict(list)
    for item in trades:
        by_symbol[item.symbol].append(item)
    total_net = sum(net_values)
    best = max(net_values) if net_values else 0.0
    return {
        "study_version": "donchian_20h_10h_portfolio_v1",
        "parameters": asdict(config),
        "raw_signals": result.raw_signals,
        "executed_entries": result.executed_entries,
        "blocked_slots": result.blocked_slots,
        "blocked_symbol": result.blocked_symbol,
        "maximum_concurrent_positions": result.maximum_concurrent_positions,
        "closed_trades": len(trades),
        "total_net_usdt": total_net,
        "total_net_pct_on_operational_balance": (
            total_net / config.operational_balance_usdt * 100
        ),
        "total_net_usdt_without_best_trade": total_net - best,
        "average_net_pct": (
            sum(item.net_pct for item in trades) / len(trades) if trades else None
        ),
        "median_net_pct": (
            statistics.median(item.net_pct for item in trades) if trades else None
        ),
        "win_rate": len(winners) / len(trades) if trades else None,
        "profit_factor": (
            sum(winners) / abs(sum(losers))
            if losers
            else math.inf if winners else None
        ),
        "realized_max_drawdown_usdt": _realized_max_drawdown(trades),
        "exit_reasons": dict(sorted(Counter(item.exit_reason for item in trades).items())),
        "by_symbol": {
            symbol: _trade_summary(items)
            for symbol, items in sorted(by_symbol.items())
        },
    }


def write_trades(path: Path, trades: Sequence[PortfolioTrade]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in trades:
            handle.write(json.dumps(item.to_record(), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def write_decisions(path: Path, decisions: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in decisions:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def _close_position(
    position: OpenPosition,
    exit_price: float,
    closed_ms: int,
    reason: str,
    config: PortfolioConfig,
) -> PortfolioTrade:
    exit_notional = position.quantity * exit_price
    gross_usdt = exit_notional - position.notional_usdt
    entry_fee = position.notional_usdt * config.fee_per_side_pct / 100
    exit_fee = exit_notional * config.fee_per_side_pct / 100
    fees = entry_fee + exit_fee
    net_usdt = gross_usdt - fees
    return PortfolioTrade(
        symbol=position.symbol,
        opened_ms=position.entry_ms,
        closed_ms=closed_ms,
        entry_price=position.entry_price,
        exit_price=exit_price,
        quantity=position.quantity,
        notional_usdt=position.notional_usdt,
        gross_pct=_pct(exit_price, position.entry_price),
        net_pct=net_usdt / position.notional_usdt * 100,
        gross_usdt=gross_usdt,
        fees_usdt=fees,
        net_usdt=net_usdt,
        exit_reason=reason,
        holding_hours=(closed_ms - position.entry_ms) / 3_600_000,
        breakout_margin_pct=position.breakout_margin_pct,
    )


def _trade_summary(trades: Sequence[PortfolioTrade]) -> dict[str, Any]:
    net = [item.net_usdt for item in trades]
    return {
        "trades": len(trades),
        "net_usdt": sum(net),
        "net_usdt_without_best": sum(net) - max(net) if net else 0.0,
        "win_rate": sum(value > 0 for value in net) / len(net) if net else None,
        "median_net_pct": statistics.median(item.net_pct for item in trades)
        if trades
        else None,
        "exit_reasons": dict(sorted(Counter(item.exit_reason for item in trades).items())),
    }


def _realized_max_drawdown(trades: Sequence[PortfolioTrade]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for item in sorted(trades, key=lambda value: (value.closed_ms, value.symbol)):
        cumulative += item.net_usdt
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _decision(event: str, timestamp_ms: int, symbol: str, **fields: Any) -> dict[str, Any]:
    return {
        "event": event,
        "timestamp_ms": timestamp_ms,
        "timestamp_utc": _iso(timestamp_ms),
        "symbol": symbol,
        **fields,
    }


def _discover_symbols(universe_cache_dir: Path, hourly_cache_dir: Path) -> list[str]:
    output = []
    for path in universe_cache_dir.glob("*_15m.jsonl"):
        symbol = path.name.removesuffix("_15m.jsonl")
        if (universe_cache_dir / f"{symbol}_1m.jsonl").exists() and (
            hourly_cache_dir / f"{symbol}_1h.jsonl"
        ).exists():
            output.append(symbol)
    return sorted(output)


def _ordered_candidates(
    candidates: Sequence[EntryCandidate],
    policy: str,
) -> list[EntryCandidate]:
    if policy == "strongest":
        return sorted(
            candidates,
            key=lambda item: (-item.breakout_margin_pct, item.symbol),
        )
    if policy == "alphabetical":
        return sorted(candidates, key=lambda item: item.symbol)
    if policy == "reverse_alphabetical":
        return sorted(candidates, key=lambda item: item.symbol, reverse=True)
    raise ValueError(f"unsupported admission policy: {policy}")


def _pct(value: float, reference: float) -> float:
    return ((value / reference) - 1) * 100 if reference else 0.0


def _iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serial 20h/10h Donchian portfolio replay with a 2% hard stop."
    )
    parser.add_argument(
        "--hourly-cache-dir",
        default=str(PROJECT_ROOT / "data" / "market_selection" / "klines"),
    )
    parser.add_argument(
        "--universe-cache-dir",
        default=str(PROJECT_ROOT / "data" / "market_bot_replay" / "klines"),
    )
    parser.add_argument("--symbols", nargs="*")
    parser.add_argument(
        "--admission-policy",
        choices=("strongest", "alphabetical", "reverse_alphabetical"),
        default="strongest",
    )
    parser.add_argument(
        "--trades-output",
        default=str(PROJECT_ROOT / "data" / "studies" / "donchian_portfolio_trades.jsonl"),
    )
    parser.add_argument(
        "--decisions-output",
        default=str(PROJECT_ROOT / "data" / "studies" / "donchian_portfolio_decisions.jsonl"),
    )
    parser.add_argument("--summary-json")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    hourly_cache_dir = Path(args.hourly_cache_dir)
    universe_cache_dir = Path(args.universe_cache_dir)
    symbols = sorted(
        set(
            args.symbols
            or _discover_symbols(universe_cache_dir, hourly_cache_dir)
        )
    )
    if not symbols:
        raise SystemExit("No frozen-universe symbols with hourly candles found")
    candles = {
        symbol: load_candle_cache(hourly_cache_dir / f"{symbol}_1h.jsonl")
        for symbol in symbols
    }
    config = PortfolioConfig(admission_policy=args.admission_policy)
    result = replay_portfolio(candles, config)
    report = summarize(result, config)
    report["symbols"] = symbols
    report["coverage"] = {
        symbol: {
            "candles_1h": len(items),
            "first_1h_utc": _iso(items[0].open_time_ms) if items else None,
            "last_1h_utc": _iso(items[-1].close_time_ms) if items else None,
        }
        for symbol, items in candles.items()
    }
    trades_path = Path(args.trades_output)
    decisions_path = Path(args.decisions_output)
    write_trades(trades_path, result.trades)
    write_decisions(decisions_path, result.decisions)
    report["trades_output"] = str(trades_path)
    report["decisions_output"] = str(decisions_path)
    if args.summary_json:
        summary_path = Path(args.summary_json)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    print("TREND-SOL | serial Donchian 20h/10h portfolio replay")
    print(
        f"Symbols={len(symbols)} | signals={result.raw_signals} | "
        f"entries={result.executed_entries} | blocked_slots={result.blocked_slots}"
    )
    print(
        f"Trades={report['closed_trades']} | net={report['total_net_usdt']:+.4f} USDT | "
        f"without best={report['total_net_usdt_without_best_trade']:+.4f} USDT | "
        f"PF={report['profit_factor']:.3f} | win={report['win_rate']:.1%} | "
        f"realized max DD={report['realized_max_drawdown_usdt']:.4f} USDT"
    )
    print(f"Exits={report['exit_reasons']}")
    print(f"Trades JSONL: {trades_path}")
    print(f"Decisions JSONL: {decisions_path}")


if __name__ == "__main__":
    main()
