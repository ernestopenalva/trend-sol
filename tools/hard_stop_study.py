from __future__ import annotations

import argparse
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


CHECKPOINT_HOURS = (1, 2, 3, 4, 6)
PEAK_THRESHOLDS = (0.20, 0.25, 0.30)
INTERVAL_MS = 60_000


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
    peak_price: float
    age_seconds: float

    @property
    def peak_pct(self) -> float:
        return (self.peak_price / self.entry - 1) * 100

    @property
    def reason(self) -> str:
        reason = str(self.record.get("exit_reason") or "UNKNOWN")
        if reason == "REVIEW_STOP" and str(self.record.get("final_step") or "") == "BE":
            return "BREAKEVEN"
        return reason


def main() -> None:
    args = parse_args()
    config = load_config(Path(args.config))
    records = TradeLedger(PROJECT_ROOT, Path(args.ledger) if args.ledger else None).load()
    selected, exclusions = select_records(records, args, str(config.get("symbol") or "SOLUSDT"))
    trades, invalid = build_trades(selected)
    exclusions.update(invalid)
    hard_stops = [trade for trade in trades if trade.reason == "HARD_STOP"]

    candles: list[Candle] = []
    fetch_error: Optional[str] = None
    if trades:
        try:
            candles = fetch_checkpoint_candles(
                BinanceMarketDataClient(
                    str(config.get("market_data", {}).get("rest_url", "https://api.binance.com")),
                    int(args.http_timeout_seconds),
                ),
                str(config.get("symbol") or "SOLUSDT"),
                trades,
            )
        except Exception as exc:  # survivor analysis must fail closed, never invent a path
            fetch_error = f"{type(exc).__name__}: {exc}"

    print_report(trades, hard_stops, candles, config, exclusions, args, fetch_error)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estudo read-only de idade, peak e no-progress dos HARD_STOPs reais do Bot B."
    )
    parser.add_argument("--since", help="Inicio em DD/MM HH:MM ou ISO 8601.")
    parser.add_argument("--until", help="Fim inclusivo em DD/MM HH:MM ou ISO 8601.")
    parser.add_argument("--since-field", choices=["opened_at", "closed_at"], default="opened_at")
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument("--detail", action="store_true", help="Mostra os trades de cada coorte sobrevivente.")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--ledger")
    parser.add_argument("--http-timeout-seconds", type=int, default=10)
    return parser.parse_args()


def select_records(
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
    return sorted(selected, key=lambda item: str(item.get("opened_at") or "")), excluded


def build_trades(records: Sequence[Dict[str, Any]]) -> tuple[list[Trade], Counter[str]]:
    output: list[Trade] = []
    invalid: Counter[str] = Counter()
    for record in records:
        opened = parse_ts(record.get("opened_at"))
        closed = parse_ts(record.get("closed_at"))
        entry = number(record.get("entry_price"))
        peak = number(record.get("peak_price"))
        if opened is None or closed is None or closed < opened:
            invalid["invalid_timestamps"] += 1
            continue
        if entry is None or entry <= 0 or peak is None or peak <= 0:
            invalid["missing_geometry"] += 1
            continue
        age = number(record.get("age_seconds"))
        if age is None or age < 0:
            age = (closed - opened).total_seconds()
        output.append(Trade(record, opened, closed, entry, peak, age))
    return output, invalid


def fetch_checkpoint_candles(
    client: BinanceMarketDataClient, symbol: str, trades: Sequence[Trade]
) -> list[Candle]:
    start_ms = min(ceil_minute_ms(int(trade.opened.timestamp() * 1000)) for trade in trades)
    checkpoint_ends = [
        int((trade.opened + timedelta(hours=hours)).timestamp() * 1000)
        for trade in trades
        for hours in CHECKPOINT_HOURS
        if trade.closed >= trade.opened + timedelta(hours=hours)
    ]
    if not checkpoint_ends:
        return []
    end_ms = max(checkpoint_ends)
    cursor = start_ms
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
        next_cursor = max(item.open_ms for item in page) + INTERVAL_MS
        if next_cursor <= cursor:
            raise RuntimeError("kline pagination did not advance")
        cursor = next_cursor
        if len(page) < 1000:
            break
    return [indexed[key] for key in sorted(indexed)]


def candle_from_binance(value: Sequence[Any]) -> Candle:
    if len(value) < 7:
        raise ValueError("Binance kline must have at least 7 fields")
    return Candle(open_ms=int(value[0]), close_ms=int(value[6]), high=float(value[2]))


def peak_at_checkpoint(
    trade: Trade, hours: int, candles_by_open: Dict[int, Candle]
) -> Optional[float]:
    checkpoint = trade.opened + timedelta(hours=hours)
    if trade.closed < checkpoint:
        return None
    first_open = ceil_minute_ms(int(trade.opened.timestamp() * 1000))
    checkpoint_ms = int(checkpoint.timestamp() * 1000)
    last_open = ((checkpoint_ms - (INTERVAL_MS - 1)) // INTERVAL_MS) * INTERVAL_MS
    expected = list(range(first_open, last_open + 1, INTERVAL_MS)) if last_open >= first_open else []
    if not expected or any(open_ms not in candles_by_open for open_ms in expected):
        return None
    peak = max([trade.entry, *(candles_by_open[open_ms].high for open_ms in expected)])
    return (peak / trade.entry - 1) * 100


def survivor_rows(
    trades: Sequence[Trade], candles: Sequence[Candle], threshold: float
) -> list[tuple[int, list[Trade], int]]:
    indexed = {candle.open_ms: candle for candle in candles}
    output = []
    for hours in CHECKPOINT_HOURS:
        eligible: list[Trade] = []
        unavailable = 0
        for trade in trades:
            if trade.closed < trade.opened + timedelta(hours=hours):
                continue
            peak = peak_at_checkpoint(trade, hours, indexed)
            if peak is None:
                unavailable += 1
            elif peak + 1e-12 < threshold:
                eligible.append(trade)
        output.append((hours, eligible, unavailable))
    return output


def print_report(
    trades: Sequence[Trade],
    hard_stops: Sequence[Trade],
    candles: Sequence[Candle],
    config: Dict[str, Any],
    exclusions: Counter[str],
    args: argparse.Namespace,
    fetch_error: Optional[str],
) -> None:
    print("TREND-SOL | HARD_STOP age, peak and no-progress study")
    print(
        f"Filter: real Bot B | phantoms/shadows=no | profile={args.profile} | "
        f"since={args.since or 'all'} | until={args.until or 'all'} | field={args.since_field}"
    )
    print(f"Ledger: {Path(args.ledger) if args.ledger else PROJECT_ROOT / 'data/trades/trades_B.jsonl'}")
    print()
    print("1. UNIVERSE")
    print(f"valid real trades | {len(trades)}")
    print(f"HARD_STOP trades total | {len(hard_stops)}")
    print(f"covered period | {covered_period(trades)}")
    print_hard_stop_versions(hard_stops)
    print(f"fees reference | {fee_reference(trades, config)}")
    if exclusions:
        print("excluded | " + " | ".join(f"{key}={value}" for key, value in sorted(exclusions.items())))
    print()
    print("2. HARD_STOP TRADES | chronological")
    print("opened | closed | age | entry | peak | peak_pct | trough_pct | exit | gross | net")
    for trade in hard_stops:
        record = trade.record
        print(
            f"{short_time(trade.opened)} | {short_time(trade.closed)} | {duration(trade.age_seconds)} | "
            f"{trade.entry:.4f} | {trade.peak_price:.4f} | {trade.peak_pct:+.3f}% | "
            f"{signed_pct(number(record.get('trough_pct')))} | {price(record.get('exit_price'))} | "
            f"{signed_pct(gross(record))} | {signed_pct(net(record, config))}"
        )
    if not hard_stops:
        print("none")

    ages_hours = [trade.age_seconds / 3600 for trade in hard_stops]
    peaks = [trade.peak_pct for trade in hard_stops]
    print()
    print("3. HARD_STOP AGE")
    print_stats(ages_hours, "h")
    age_buckets = (
        ("<1h", lambda value: value < 1),
        ("1h-2h", lambda value: 1 <= value < 2),
        ("2h-4h", lambda value: 2 <= value < 4),
        ("4h-6h", lambda value: 4 <= value < 6),
        ("6h-12h", lambda value: 6 <= value <= 12),
        (">12h", lambda value: value > 12),
    )
    for label, predicate in age_buckets:
        count = sum(predicate(value) for value in ages_hours)
        print(f"{label} | {count_pct(count, len(ages_hours))}")

    print()
    print("4. HARD_STOP PEAK")
    print_stats(peaks, "%")
    print("Cumulative buckets (strict peak < threshold):")
    for threshold in (0.10, 0.20, 0.25, 0.30, 0.50):
        count = sum(value + 1e-12 < threshold for value in peaks)
        print(f"peak < +{threshold:.2f}% | {count_pct(count, len(peaks))}")

    print()
    print("5. FINAL AGE + FINAL PEAK | outcome comparison")
    print("condition | trades | HS | non-HS | HS rate")
    for threshold in PEAK_THRESHOLDS:
        for hours in CHECKPOINT_HOURS:
            population = [
                trade for trade in trades
                if trade.age_seconds >= hours * 3600 and trade.peak_pct + 1e-12 < threshold
            ]
            hs = sum(trade.reason == "HARD_STOP" for trade in population)
            print(
                f">={hours}h & final_peak<+{threshold:.2f}% | {len(population)} | {hs} | "
                f"{len(population) - hs} | {rate(hs, len(population))}"
            )

    print()
    print("6. SURVIVOR NO-PROGRESS AT CHECKPOINT | peak < +0.25% at that instant")
    print("checkpoint | cohort | HS | non-HS | HS rate | BREAKEVEN | PROFIT_LOCK | PL_ECON_EXIT | TRAILING | others | unavailable")
    if fetch_error:
        print(f"UNAVAILABLE: Binance historical 1m fetch failed: {fetch_error}")
    else:
        for hours, cohort, unavailable in survivor_rows(trades, candles, 0.25):
            reasons = Counter(trade.reason for trade in cohort)
            known = sum(reasons.get(reason, 0) for reason in (
                "HARD_STOP", "BREAKEVEN", "PROFIT_LOCK", "PROFIT_LOCK_ECONOMIC_EXIT", "TRAILING"
            ))
            hard_stop_count = reasons["HARD_STOP"]
            print(
                f"{hours}h | {len(cohort)} | {hard_stop_count} | {len(cohort) - hard_stop_count} | "
                f"{rate(hard_stop_count, len(cohort))} | {reasons['BREAKEVEN']} | "
                f"{reasons['PROFIT_LOCK']} | {reasons['PROFIT_LOCK_ECONOMIC_EXIT']} | "
                f"{reasons['TRAILING']} | {len(cohort) - known} | {unavailable}"
            )
            if args.detail and cohort:
                print("  " + " | ".join(
                    f"{str(trade.record.get('pair_id') or '')[:12]}:{short_time(trade.opened)}->{trade.reason}"
                    for trade in cohort
                ))
    print("Trajectory source: Binance public 1m klines; only fully post-entry candles are used.")
    print("Fidelity: the partial 1m candle containing the entry is excluded to avoid pre-entry look-ahead.")
    print("Final peak sections use the peak_price persisted in the real trade ledger.")


def print_hard_stop_versions(hard_stops: Sequence[Trade]) -> None:
    grouped: dict[str, list[Trade]] = {}
    for trade in hard_stops:
        value = number(trade.record.get("hard_stop_pct"))
        grouped.setdefault("unknown" if value is None else f"{value:g}%", []).append(trade)
    if not grouped:
        print("recorded hard stop | none")
        return
    for label, items in grouped.items():
        print(
            f"recorded hard stop {label} | trades={len(items)} | "
            f"{short_time(min(item.opened for item in items))} to {short_time(max(item.opened for item in items))}"
        )


def print_stats(values: Sequence[float], suffix: str) -> None:
    metrics = {
        "mean": statistics.fmean(values) if values else None,
        "median": percentile(values, 50),
        "p25": percentile(values, 25),
        "p75": percentile(values, 75),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }
    print(" | ".join(f"{key}={stat(value, suffix)}" for key, value in metrics.items()))


def fee_reference(trades: Sequence[Trade], config: Dict[str, Any]) -> str:
    values = sorted({round(value, 6) for trade in trades if (value := number(trade.record.get("estimated_fees_pct"))) is not None})
    configured = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    taker = number(configured.get("taker_fee_pct")) or 0.0
    factor = 0.75 if bool(configured.get("use_bnb_discount", False)) else 1.0
    fallback = 2 * taker * factor
    recorded = ",".join(f"{value:.3f}%" for value in values) if values else "none"
    return f"recorded={recorded} | current configured round-trip={fallback:.3f}%"


def gross(record: Dict[str, Any]) -> Optional[float]:
    return number(record.get("gross_pnl_pct", record.get("realized_pnl_pct")))


def net(record: Dict[str, Any], config: Dict[str, Any]) -> Optional[float]:
    value = number(record.get("net_pnl_pct"))
    if value is not None:
        return value
    value = gross(record)
    if value is None:
        return None
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    taker = number(fees.get("taker_fee_pct")) or 0.0
    return value - 2 * taker * (0.75 if bool(fees.get("use_bnb_discount", False)) else 1.0)


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid config: {path}")
    return effective_config(data)


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
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def ceil_minute_ms(value: int) -> int:
    return ((value + INTERVAL_MS - 1) // INTERVAL_MS) * INTERVAL_MS


def covered_period(trades: Sequence[Trade]) -> str:
    if not trades:
        return "n/a"
    return f"{short_time(min(item.opened for item in trades))} to {short_time(max(item.closed for item in trades))}"


def short_time(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


def duration(seconds: float) -> str:
    minutes = max(0, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m"


def price(value: Any) -> str:
    parsed = number(value)
    return "n/a" if parsed is None else f"{parsed:.4f}"


def signed_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


def stat(value: Optional[float], suffix: str) -> str:
    return "n/a" if value is None else f"{value:.3f}{suffix}"


def count_pct(count: int, total: int) -> str:
    return f"{count} ({count / total * 100:.1f}%)" if total else "0 (n/a)"


def rate(count: int, total: int) -> str:
    return f"{count / total * 100:.1f}%" if total else "n/a"


if __name__ == "__main__":
    main()
