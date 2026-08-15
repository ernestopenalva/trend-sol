from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.exchange.binance_market_data import BinanceMarketDataClient
from src.trade_ledger import TradeLedger
from tools.atr_exit_study import parse_cli_datetime, percentile


MINUTE_MS = 60_000


@dataclass(frozen=True)
class Candle:
    open_ms: int
    close_ms: int
    high: float


@dataclass(frozen=True)
class Trade:
    record: Dict[str, Any]
    opened: datetime
    closed: datetime
    entry: float
    activation_price: Optional[float]

    @property
    def pair_id(self) -> str:
        return str(self.record.get("pair_id") or "")

    @property
    def reason(self) -> str:
        reason = str(self.record.get("exit_reason") or "UNKNOWN")
        if reason == "REVIEW_STOP" and str(self.record.get("final_step") or "") == "BE":
            return "BREAKEVEN"
        return reason

    @property
    def age_seconds(self) -> float:
        recorded = number(self.record.get("age_seconds"))
        return recorded if recorded is not None else (self.closed - self.opened).total_seconds()


@dataclass(frozen=True)
class BeResult:
    trade: Trade
    armed_at: Optional[datetime]
    status: str
    source: str

    @property
    def time_seconds(self) -> Optional[float]:
        if self.armed_at is None:
            return None
        return max(0.0, (self.armed_at - self.trade.opened).total_seconds())


@dataclass(frozen=True)
class NoProgressResult:
    trade: Trade
    future_mfe_pct: Optional[float]
    fidelity: str


@dataclass(frozen=True)
class NoProgressEvaluation:
    results: list[NoProgressResult]
    eligible: int
    armed_by_checkpoint: int
    unavailable: int


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config))
    ledger_path = Path(args.ledger) if args.ledger else PROJECT_ROOT / "data/trades/trades_B.jsonl"
    event_paths = [Path(value) for value in args.events] if args.events else [PROJECT_ROOT / "logs/trades.jsonl"]
    records = TradeLedger(PROJECT_ROOT, ledger_path).load()
    selected, exclusions = select_real_trades(records, args, str(config.get("symbol") or "SOLUSDT"))
    trades, invalid = build_trades(selected)
    exclusions.update(invalid)
    event_times = load_be_events(event_paths, trades)

    candles: list[Candle] = []
    fetch_error: Optional[str] = None
    if trades:
        try:
            candles = fetch_candles(
                BinanceMarketDataClient(
                    str(config.get("market_data", {}).get("rest_url", "https://api.binance.com")),
                    int(args.http_timeout_seconds),
                ),
                str(config.get("symbol") or "SOLUSDT"),
                min(trade.opened for trade in trades),
                max(trade.closed for trade in trades),
            )
        except Exception as exc:  # never turn missing market data into a timing estimate
            fetch_error = f"{type(exc).__name__}: {exc}"

    results = reconstruct_be_results(trades, event_times, candles, fetch_error)
    no_progress = evaluate_no_progress(
        trades,
        event_times,
        candles,
        float(args.no_progress_hours),
        fetch_error,
    )
    print_report(results, no_progress, exclusions, ledger_path, event_paths, config, args, fetch_error)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estudo read-only de time_to_BE real/reconstruido e NO_PROGRESS; nunca envia ordens."
    )
    parser.add_argument("--since", help="Inicio em DD/MM HH:MM ou ISO 8601.")
    parser.add_argument("--until", help="Fim inclusivo em DD/MM HH:MM ou ISO 8601.")
    parser.add_argument("--since-field", choices=["opened_at", "closed_at"], default="opened_at")
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument("--rolling", type=int, default=20, help="Quantidade dos trades fechados mais recentes.")
    parser.add_argument("--no-progress-hours", type=float, default=2.0)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--ledger")
    parser.add_argument("--events", action="append", help="JSONL de eventos; pode ser repetido.")
    parser.add_argument("--http-timeout-seconds", type=int, default=10)
    args = parser.parse_args()
    if args.rolling <= 0:
        parser.error("--rolling must be positive")
    if args.no_progress_hours <= 0:
        parser.error("--no-progress-hours must be positive")
    return args


def select_real_trades(
    records: Sequence[Dict[str, Any]], args: argparse.Namespace, symbol: str
) -> tuple[list[Dict[str, Any]], Counter[str]]:
    since = parse_cli_datetime(args.since)
    until = parse_cli_datetime(args.until)
    selected: list[Dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for record in records:
        if bool(record.get("phantom")) or str(record.get("position_type") or "") == "PHANTOM":
            excluded["phantom"] += 1
            continue
        position_type = str(record.get("position_type") or "")
        if position_type not in ("", "BOT_EXIT") or record.get("shadow_kind"):
            excluded["not_real_bot_b"] += 1
            continue
        if str(record.get("symbol") or symbol) != symbol:
            excluded["other_symbol"] += 1
            continue
        if args.profile != "all" and str(record.get("profile") or "") != args.profile:
            excluded["profile"] += 1
            continue
        boundary = parse_ts(record.get(args.since_field))
        if boundary is None:
            excluded[f"missing_{args.since_field}"] += 1
            continue
        if since is not None and boundary < since:
            excluded["before_since"] += 1
            continue
        if until is not None and boundary > until:
            excluded["after_until"] += 1
            continue
        selected.append(record)
    return sorted(selected, key=lambda item: str(item.get("closed_at") or "")), excluded


def build_trades(records: Sequence[Dict[str, Any]]) -> tuple[list[Trade], Counter[str]]:
    output: list[Trade] = []
    invalid: Counter[str] = Counter()
    for record in records:
        opened = parse_ts(record.get("opened_at"))
        closed = parse_ts(record.get("closed_at"))
        entry = number(record.get("entry_price"))
        if opened is None or closed is None or closed < opened:
            invalid["invalid_timestamps"] += 1
            continue
        if entry is None or entry <= 0:
            invalid["invalid_entry"] += 1
            continue
        output.append(
            Trade(
                record=record,
                opened=opened,
                closed=closed,
                entry=entry,
                activation_price=number(record.get("be_activation_price")),
            )
        )
    return output, invalid


def load_be_events(paths: Sequence[Path], trades: Sequence[Trade]) -> dict[str, datetime]:
    bounds = {trade.pair_id: (trade.opened, trade.closed) for trade in trades if trade.pair_id}
    output: dict[str, datetime] = {}
    for path in paths:
        for event in read_jsonl(path):
            if not str(event.get("event") or "").startswith("BREAKEVEN_"):
                continue
            pair_id = str(event.get("pair_id") or "")
            ts = parse_ts(event.get("ts"))
            if pair_id not in bounds or ts is None:
                continue
            opened, closed = bounds[pair_id]
            if ts < opened or ts > closed:
                continue
            previous = output.get(pair_id)
            if previous is None or ts < previous:
                output[pair_id] = ts
    return output


def reconstruct_be_results(
    trades: Sequence[Trade],
    event_times: Dict[str, datetime],
    candles: Sequence[Candle],
    fetch_error: Optional[str] = None,
) -> list[BeResult]:
    indexed = {candle.open_ms: candle for candle in candles}
    output: list[BeResult] = []
    for trade in trades:
        event_time = event_times.get(trade.pair_id)
        if event_time is not None:
            output.append(BeResult(trade, event_time, "ARMED", "EVENT_EXACT"))
            continue
        if trade.activation_price is None:
            output.append(BeResult(trade, None, "UNAVAILABLE", "MISSING_ACTIVATION_PRICE"))
            continue
        if fetch_error:
            output.append(BeResult(trade, None, "UNAVAILABLE", "BINANCE_FETCH_ERROR"))
            continue
        crossing, complete = first_crossing(
            trade.opened, trade.closed, trade.activation_price, indexed
        )
        if not complete:
            output.append(BeResult(trade, None, "UNAVAILABLE", "INCOMPLETE_1M_PATH"))
        elif crossing is not None:
            output.append(BeResult(trade, crossing, "ARMED", "BINANCE_1M"))
        else:
            peak = number(trade.record.get("peak_price"))
            if peak is not None and peak + 1e-12 >= trade.activation_price:
                output.append(BeResult(trade, None, "UNAVAILABLE", "PARTIAL_MINUTE_CROSSING"))
            else:
                output.append(BeResult(trade, None, "NOT_ARMED", "BINANCE_1M"))
    return output


def evaluate_no_progress(
    trades: Sequence[Trade],
    event_times: Dict[str, datetime],
    candles: Sequence[Candle],
    hours: float,
    fetch_error: Optional[str] = None,
) -> NoProgressEvaluation:
    indexed = {candle.open_ms: candle for candle in candles}
    output: list[NoProgressResult] = []
    eligible = 0
    armed_by_checkpoint = 0
    unavailable = 0
    for trade in trades:
        checkpoint = trade.opened + timedelta(hours=hours)
        if trade.closed < checkpoint:
            continue
        eligible += 1
        event_time = event_times.get(trade.pair_id)
        if event_time is not None:
            if event_time <= checkpoint:
                armed_by_checkpoint += 1
                continue
            affected = True
            fidelity = "EVENT_EXACT"
        elif trade.activation_price is None or fetch_error:
            unavailable += 1
            continue
        else:
            crossing, complete = first_crossing(
                trade.opened, checkpoint, trade.activation_price, indexed
            )
            if not complete:
                unavailable += 1
                continue
            if crossing is not None:
                armed_by_checkpoint += 1
                continue
            if opening_minute_ambiguous(trade.opened, trade.activation_price, indexed):
                unavailable += 1
                continue
            affected = True
            fidelity = "BINANCE_1M"
        if not affected:
            continue
        future_mfe, complete = future_mfe_pct(trade, checkpoint, indexed)
        output.append(
            NoProgressResult(
                trade=trade,
                future_mfe_pct=future_mfe if complete else None,
                fidelity=fidelity if complete else f"{fidelity}+INCOMPLETE_FUTURE_PATH",
            )
        )
    return NoProgressEvaluation(output, eligible, armed_by_checkpoint, unavailable)


def opening_minute_ambiguous(
    opened: datetime, trigger: float, candles_by_open: Dict[int, Candle]
) -> bool:
    opened_ms = int(opened.timestamp() * 1000)
    if opened_ms % MINUTE_MS == 0:
        return False
    candle = candles_by_open.get(floor_minute_ms(opened_ms))
    return candle is None or candle.high + 1e-12 >= trigger


def first_crossing(
    opened: datetime,
    end: datetime,
    trigger: float,
    candles_by_open: Dict[int, Candle],
) -> tuple[Optional[datetime], bool]:
    expected = expected_full_candle_opens(opened, end)
    if any(open_ms not in candles_by_open for open_ms in expected):
        return None, False
    for open_ms in expected:
        candle = candles_by_open[open_ms]
        if candle.high + 1e-12 >= trigger:
            return datetime.fromtimestamp(candle.open_ms / 1000, tz=timezone.utc), True
    return None, True


def future_mfe_pct(
    trade: Trade, checkpoint: datetime, candles_by_open: Dict[int, Candle]
) -> tuple[Optional[float], bool]:
    expected = expected_full_candle_opens(checkpoint, trade.closed)
    if any(open_ms not in candles_by_open for open_ms in expected):
        return None, False
    exit_price = number(trade.record.get("exit_price"))
    candidates = [trade.entry, *(candles_by_open[open_ms].high for open_ms in expected)]
    if exit_price is not None:
        candidates.append(exit_price)
    peak = max(candidates)
    return (peak / trade.entry - 1) * 100, True


def expected_full_candle_opens(start: datetime, end: datetime) -> list[int]:
    first = ceil_minute_ms(int(start.timestamp() * 1000))
    end_ms = int(end.timestamp() * 1000)
    last = ((end_ms - (MINUTE_MS - 1)) // MINUTE_MS) * MINUTE_MS
    return list(range(first, last + 1, MINUTE_MS)) if last >= first else []


def fetch_candles(
    client: BinanceMarketDataClient,
    symbol: str,
    opened: datetime,
    closed: datetime,
) -> list[Candle]:
    cursor = floor_minute_ms(int(opened.timestamp() * 1000))
    end_ms = int(closed.timestamp() * 1000)
    indexed: dict[int, Candle] = {}
    while cursor <= end_ms:
        data = client.get(
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": "1m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected kline response for {symbol}/1m")
        page = [candle_from_binance(item) for item in data if isinstance(item, list)]
        if not page:
            break
        for candle in page:
            indexed[candle.open_ms] = candle
        next_cursor = max(candle.open_ms for candle in page) + MINUTE_MS
        if next_cursor <= cursor:
            raise RuntimeError("Binance kline pagination did not advance")
        cursor = next_cursor
        if len(page) < 1000:
            break
    return [indexed[key] for key in sorted(indexed)]


def candle_from_binance(value: Sequence[Any]) -> Candle:
    if len(value) < 7:
        raise ValueError("Binance kline must have at least 7 fields")
    return Candle(open_ms=int(value[0]), close_ms=int(value[6]), high=float(value[2]))


def print_report(
    results: Sequence[BeResult],
    no_progress: NoProgressEvaluation,
    exclusions: Counter[str],
    ledger_path: Path,
    event_paths: Sequence[Path],
    config: Dict[str, Any],
    args: argparse.Namespace,
    fetch_error: Optional[str],
) -> None:
    armed = [result for result in results if result.status == "ARMED"]
    unavailable = [result for result in results if result.status == "UNAVAILABLE"]
    print("TREND-SOL | historical time_to_BE study")
    print(
        f"Filter: real Bot B | phantoms/shadows=no | profile={args.profile} | "
        f"since={args.since or 'all'} | until={args.until or 'all'} | field={args.since_field}"
    )
    print(f"Ledger: {ledger_path}")
    print("Events: " + ", ".join(str(path) for path in event_paths))
    print("Timing hierarchy: BREAKEVEN event exact > Binance public 1m reconstruction")
    if fetch_error:
        print(f"Binance reconstruction unavailable: {fetch_error}")
    print()
    print("1. UNIVERSE")
    print(f"valid closed real trades | {len(results)}")
    print(f"BE armed | {len(armed)} ({pct(len(armed), len(results))})")
    print(f"not armed | {sum(result.status == 'NOT_ARMED' for result in results)}")
    print(f"unavailable | {len(unavailable)}")
    print(f"covered period | {covered_period(results)}")
    if exclusions:
        print("excluded | " + " | ".join(f"{key}={value}" for key, value in sorted(exclusions.items())))
    if unavailable:
        print("unavailable reasons | " + " | ".join(
            f"{key}={value}" for key, value in sorted(Counter(result.source for result in unavailable).items())
        ))

    print()
    print("2. TIME_TO_BE | all armed trades")
    print_timing_stats(armed)
    print("by source | " + " | ".join(
        f"{key}={value}" for key, value in sorted(Counter(result.source for result in armed).items())
    ) if armed else "by source | none")

    print()
    print("3. TIME_TO_BE BY FINAL OUTCOME")
    print("reason | trades | armed | armed% | min | median | mean | p75 | p85 | p90 | max")
    for reason in sorted({result.trade.reason for result in results}):
        group = [result for result in results if result.trade.reason == reason]
        group_armed = [result for result in group if result.status == "ARMED"]
        print_timing_row(reason, group, group_armed)

    recent = sorted(results, key=lambda result: result.trade.closed)[-int(args.rolling):]
    print()
    print(f"4. LAST {args.rolling} CLOSED TRADES")
    print(
        f"trades={len(recent)} | armed={sum(result.status == 'ARMED' for result in recent)} | "
        f"not_armed={sum(result.status == 'NOT_ARMED' for result in recent)} | "
        f"unavailable={sum(result.status == 'UNAVAILABLE' for result in recent)}"
    )
    print("opened | closed | final_reason | BE | time_to_BE | source")
    for result in recent:
        print(
            f"{short_time(result.trade.opened)} | {short_time(result.trade.closed)} | "
            f"{result.trade.reason} | {result.status} | {duration(result.time_seconds)} | {result.source}"
        )
    recent_armed = [result for result in recent if result.status == "ARMED"]
    print("rolling armed stats | ", end="")
    print_timing_stats(recent_armed, inline=True)

    hours = float(args.no_progress_hours)
    reasons = Counter(item.trade.reason for item in no_progress.results)
    mfe_values = [item.future_mfe_pct for item in no_progress.results if item.future_mfe_pct is not None]
    print()
    print(f"5. NO_PROGRESS_EXIT = {hours:g}h | observational, no orders simulated")
    print(f"trades still open at checkpoint | {no_progress.eligible}")
    print(f"BE armed by checkpoint | {no_progress.armed_by_checkpoint}")
    print(f"affected trades | {len(no_progress.results)}")
    print(f"classification unavailable | {no_progress.unavailable}")
    print("final outcomes | " + (" | ".join(f"{key}={value}" for key, value in sorted(reasons.items())) or "none"))
    print(
        "future MFE from entry after checkpoint | "
        + stats_text([float(value) for value in mfe_values], "%")
    )
    print(f"future MFE unavailable | {len(no_progress.results) - len(mfe_values)}")
    if args.detail:
        print("opened | checkpoint | final_reason | future_MFE | final_net | fidelity")
        for item in no_progress.results:
            print(
                f"{short_time(item.trade.opened)} | "
                f"{short_time(item.trade.opened + timedelta(hours=hours))} | "
                f"{item.trade.reason} | {signed_pct(item.future_mfe_pct)} | "
                f"{signed_pct(number(item.trade.record.get('net_pnl_pct')))} | {item.fidelity}"
            )
    print("Interpretation: affected means the trade was still open and BE had not armed at the checkpoint.")
    print("Future MFE is the best post-checkpoint price relative to entry; it is not simulated exit PnL.")
    print("The partial 1m candle containing entry/checkpoint is excluded to avoid pre-boundary look-ahead.")

    print()
    print("6. CURRENT ENTRY CANDLE RESTRICTION")
    entry = config.get("entry") if isinstance(config.get("entry"), dict) else {}
    gate = config.get("trend_gate") if isinstance(config.get("trend_gate"), dict) else {}
    entry_timeframe = str(entry.get("timeframe") or "unknown")
    print(f"entry timeframe | {entry_timeframe} (resolved {args.profile} config)")
    print(f"max entries per source candle | {entry.get('max_entries_per_candle', 1)}")
    print(f"source candle | the closed {entry_timeframe} entry candle that produced EntrySignal")
    print(
        f"GE structural candle | {gate.get('candle_interval') or 'n/a'}; "
        "it does not define the per-candle entry counter"
    )


def print_timing_stats(results: Sequence[BeResult], inline: bool = False) -> None:
    values = [result.time_seconds / 60 for result in results if result.time_seconds is not None]
    text = stats_text(values, "m")
    print(text if inline else f"armed N={len(values)} | {text}")


def print_timing_row(reason: str, group: Sequence[BeResult], armed: Sequence[BeResult]) -> None:
    values = [result.time_seconds / 60 for result in armed if result.time_seconds is not None]
    metrics = timing_metrics(values)
    print(
        f"{reason} | {len(group)} | {len(values)} | {pct(len(values), len(group))} | "
        + " | ".join(duration_minutes(metrics[key]) for key in ("min", "median", "mean", "p75", "p85", "p90", "max"))
    )


def stats_text(values: Sequence[float], suffix: str) -> str:
    metrics = timing_metrics(values)
    return " | ".join(
        f"{key}={format_metric(metrics[key], suffix)}"
        for key in ("min", "median", "mean", "p75", "p85", "p90", "max")
    )


def timing_metrics(values: Sequence[float]) -> dict[str, Optional[float]]:
    return {
        "min": min(values) if values else None,
        "median": percentile(values, 50),
        "mean": statistics.fmean(values) if values else None,
        "p75": percentile(values, 75),
        "p85": percentile(values, 85),
        "p90": percentile(values, 90),
        "max": max(values) if values else None,
    }


def read_jsonl(path: Path) -> list[Dict[str, Any]]:
    if not path.exists():
        return []
    output = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                output.append(value)
    return output


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise SystemExit(f"Invalid config: {path}")
    return effective_config(value)


def parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def ceil_minute_ms(value: int) -> int:
    return ((value + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS


def floor_minute_ms(value: int) -> int:
    return value - value % MINUTE_MS


def short_time(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


def duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "n/a"
    minutes = max(0, int(round(seconds / 60)))
    hours, minute = divmod(minutes, 60)
    return f"{hours}h{minute:02d}m" if hours else f"{minute}m"


def duration_minutes(minutes: Optional[float]) -> str:
    return duration(None if minutes is None else minutes * 60)


def format_metric(value: Optional[float], suffix: str) -> str:
    return "n/a" if value is None else f"{value:.2f}{suffix}"


def signed_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


def pct(count: int, total: int) -> str:
    return f"{count / total * 100:.1f}%" if total else "n/a"


def covered_period(results: Sequence[BeResult]) -> str:
    if not results:
        return "n/a"
    return f"{short_time(min(item.trade.opened for item in results))} to {short_time(max(item.trade.closed for item in results))}"


if __name__ == "__main__":
    main()
