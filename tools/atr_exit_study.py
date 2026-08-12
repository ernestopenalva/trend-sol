from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.exchange.binance_market_data import BinanceMarketDataClient
from src.indicators.indicators import atr
from src.trade_ledger import TradeLedger


DEFAULT_TIMEFRAMES = "1m,3m,5m,15m"
RUNTIME_CANDLE_WINDOW = 300
TRIGGERS = (3.0, 5.0, 8.0, 10.0, 12.0)
LOCKS = (1.5, 3.0, 6.0)


@dataclass(frozen=True)
class StudyCandle:
    open_time_ms: int
    close_time_ms: int
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class TradeAtr:
    timeframe: str
    atr_abs: Optional[float]
    atr_pct: Optional[float]
    candles_used: int
    reason: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.atr_pct is not None


@dataclass(frozen=True)
class TradeObservation:
    record: Dict[str, Any]
    opened_at: datetime
    entry_price: float
    peak_pct: float
    age_seconds: Optional[float]
    atrs: Dict[str, TradeAtr]


def main() -> None:
    args = _parse_args()
    raw_config = _load_config(Path(args.config))
    runtime_config = effective_config(raw_config)
    records = TradeLedger(
        PROJECT_ROOT,
        Path(args.ledger) if args.ledger else None,
    ).load()
    selected, base_exclusions = select_real_bot_b_trades(records, args)
    timeframes = parse_timeframes(args.atr_timeframes)
    period = positive_int(args.atr_period, "--atr-period")
    net_margin_pct = nonnegative_float(args.net_margin_pct, "--net-margin-pct")
    fee_pct = round_trip_fee_pct(runtime_config)

    candle_sets: Dict[str, list[StudyCandle]] = {}
    fetch_errors: Dict[str, str] = {}
    if selected:
        opened_values = [_parse_ts(item.get("opened_at")) for item in selected]
        opened_values = [item for item in opened_values if item is not None]
        if opened_values:
            client = BinanceMarketDataClient(
                str(runtime_config.get("market_data", {}).get("rest_url", "https://api.binance.com")),
                int(args.http_timeout_seconds),
            )
            symbol = str(runtime_config.get("symbol") or "SOLUSDT")
            for timeframe in timeframes:
                try:
                    candle_sets[timeframe] = fetch_candles_for_entries(
                        client,
                        symbol,
                        timeframe,
                        min(opened_values),
                        max(opened_values),
                    )
                except Exception as exc:  # a failed timeframe must not invent ATR values
                    candle_sets[timeframe] = []
                    fetch_errors[timeframe] = f"{type(exc).__name__}: {exc}"

    observations = build_observations(
        selected,
        timeframes,
        period,
        candle_sets,
        fetch_errors,
    )
    invalid_geometry = len(selected) - len(observations)
    if invalid_geometry:
        base_exclusions["invalid_trade_geometry"] += invalid_geometry
    _print_report(
        observations,
        timeframes,
        period,
        fee_pct,
        net_margin_pct,
        runtime_config,
        args,
        base_exclusions,
        fetch_errors,
        candle_sets,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estudo offline da geometria da escadinha de saida sob diferentes "
            "timeframes de ATR; nao envia ordens nem altera estado."
        )
    )
    parser.add_argument("--since", help="Inicio em DD/MM HH:MM ou ISO 8601.")
    parser.add_argument("--until", help="Fim inclusivo em DD/MM HH:MM ou ISO 8601.")
    parser.add_argument("--since-field", choices=["opened_at", "closed_at"], default="opened_at")
    parser.add_argument("--profile", choices=["intraday", "production", "all"], default="intraday")
    parser.add_argument("--strategy", help="Filtra por strategy_version.")
    parser.add_argument("--atr-timeframes", default=DEFAULT_TIMEFRAMES)
    parser.add_argument("--atr-period", type=int, default=14)
    parser.add_argument("--net-margin-pct", type=float, default=0.05)
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    parser.add_argument("--ledger")
    parser.add_argument("--http-timeout-seconds", type=int, default=10)
    return parser.parse_args()


def select_real_bot_b_trades(
    records: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> tuple[list[Dict[str, Any]], Counter[str]]:
    since = parse_cli_datetime(args.since)
    until = parse_cli_datetime(args.until)
    output: list[Dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for record in records:
        if bool(record.get("phantom")) or str(record.get("position_type") or "") == "PHANTOM":
            excluded["phantom"] += 1
            continue
        position_type = str(record.get("position_type") or "")
        if position_type not in ("", "BOT_EXIT") or record.get("shadow_kind"):
            excluded["not_real_bot_b"] += 1
            continue
        if args.profile != "all" and str(record.get("profile") or "") != args.profile:
            excluded["profile"] += 1
            continue
        if args.strategy and str(record.get("strategy_version") or "") != args.strategy:
            excluded["strategy"] += 1
            continue
        boundary = _parse_ts(record.get(args.since_field))
        if boundary is None:
            excluded[f"missing_{args.since_field}"] += 1
            continue
        if since is not None and boundary < since:
            excluded["before_since"] += 1
            continue
        if until is not None and boundary > until:
            excluded["after_until"] += 1
            continue
        output.append(record)
    return sorted(output, key=lambda item: str(item.get("opened_at") or "")), excluded


def fetch_candles_for_entries(
    client: BinanceMarketDataClient,
    symbol: str,
    timeframe: str,
    first_entry: datetime,
    last_entry: datetime,
) -> list[StudyCandle]:
    interval_ms = interval_milliseconds(timeframe)
    start_ms = int(first_entry.timestamp() * 1000) - RUNTIME_CANDLE_WINDOW * interval_ms
    end_ms = int(last_entry.timestamp() * 1000)
    cursor = floor_ms(start_ms, interval_ms)
    indexed: Dict[int, StudyCandle] = {}
    while cursor <= end_ms:
        data = client.get(
            "/api/v3/klines",
            {
                "symbol": symbol,
                "interval": timeframe,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        if not isinstance(data, list):
            raise RuntimeError(f"unexpected kline response for {symbol}/{timeframe}")
        page = [candle_from_binance(item) for item in data if isinstance(item, list)]
        if not page:
            break
        for candle in page:
            indexed[candle.open_time_ms] = candle
        next_cursor = max(item.open_time_ms for item in page) + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError(f"kline pagination did not advance for {symbol}/{timeframe}")
        cursor = next_cursor
        if len(page) < 1000:
            break
    return [indexed[key] for key in sorted(indexed)]


def candle_from_binance(value: Sequence[Any]) -> StudyCandle:
    if len(value) < 7:
        raise ValueError("Binance kline must have at least 7 fields")
    return StudyCandle(
        open_time_ms=int(value[0]),
        close_time_ms=int(value[6]),
        high=float(value[2]),
        low=float(value[3]),
        close=float(value[4]),
    )


def calculate_entry_atr(
    candles: Sequence[StudyCandle],
    opened_at: datetime,
    entry_price: float,
    timeframe: str,
    period: int,
) -> TradeAtr:
    if not math.isfinite(entry_price) or entry_price <= 0:
        return TradeAtr(timeframe, None, None, 0, "invalid_entry_price")
    opened_ms = int(opened_at.timestamp() * 1000)
    closed = [item for item in candles if item.close_time_ms <= opened_ms]
    selected = closed[-RUNTIME_CANDLE_WINDOW:]
    if len(selected) < period:
        return TradeAtr(timeframe, None, None, len(selected), "insufficient_history")
    interval_ms = interval_milliseconds(timeframe)
    if any(
        selected[index].open_time_ms - selected[index - 1].open_time_ms != interval_ms
        for index in range(1, len(selected))
    ):
        return TradeAtr(timeframe, None, None, len(selected), "candle_gap")
    values = atr(
        [item.high for item in selected],
        [item.low for item in selected],
        [item.close for item in selected],
        period,
    )
    atr_abs = values[-1] if values else None
    if atr_abs is None or not math.isfinite(float(atr_abs)) or float(atr_abs) <= 0:
        return TradeAtr(timeframe, None, None, len(selected), "invalid_atr")
    number = float(atr_abs)
    return TradeAtr(timeframe, number, number / entry_price * 100, len(selected))


def build_observations(
    records: Sequence[Dict[str, Any]],
    timeframes: Sequence[str],
    period: int,
    candle_sets: Dict[str, list[StudyCandle]],
    fetch_errors: Dict[str, str],
) -> list[TradeObservation]:
    output = []
    for record in records:
        opened_at = _parse_ts(record.get("opened_at"))
        entry = _optional_float(record.get("entry_price"))
        peak = _optional_float(record.get("peak_price"))
        if opened_at is None or entry is None or entry <= 0 or peak is None or peak <= 0:
            continue
        atrs = {}
        for timeframe in timeframes:
            if timeframe in fetch_errors:
                atrs[timeframe] = TradeAtr(timeframe, None, None, 0, "fetch_error")
            else:
                atrs[timeframe] = calculate_entry_atr(
                    candle_sets.get(timeframe, []),
                    opened_at,
                    entry,
                    timeframe,
                    period,
                )
        output.append(
            TradeObservation(
                record=record,
                opened_at=opened_at,
                entry_price=entry,
                peak_pct=(peak / entry - 1) * 100,
                age_seconds=_optional_float(record.get("age_seconds")),
                atrs=atrs,
            )
        )
    return output


def aggregate_timeframe(
    observations: Sequence[TradeObservation],
    timeframe: str,
    fees_pct: float,
    economic_floor_pct: float,
) -> Dict[str, Any]:
    valid = [item for item in observations if item.atrs[timeframe].available]
    atr_pcts = [float(item.atrs[timeframe].atr_pct) for item in valid]
    reached = {
        trigger: sum(item.peak_pct + 1e-12 >= trigger * float(item.atrs[timeframe].atr_pct) for item in valid)
        for trigger in TRIGGERS
    }
    locks_below_fees = {
        lock: sum(lock * float(item.atrs[timeframe].atr_pct) < fees_pct for item in valid)
        for lock in LOCKS
    }
    locks_below_floor = {
        lock: sum(lock * float(item.atrs[timeframe].atr_pct) < economic_floor_pct for item in valid)
        for lock in LOCKS
    }
    return {
        "valid": len(valid),
        "excluded": len(observations) - len(valid),
        "atr_pcts": atr_pcts,
        "median": percentile(atr_pcts, 50),
        "mean": statistics.fmean(atr_pcts) if atr_pcts else None,
        "p25": percentile(atr_pcts, 25),
        "p75": percentile(atr_pcts, 75),
        "reached": reached,
        "below_fees": locks_below_fees,
        "below_floor": locks_below_floor,
    }


def _print_report(
    observations: Sequence[TradeObservation],
    timeframes: Sequence[str],
    period: int,
    fee_pct: float,
    net_margin_pct: float,
    config: Dict[str, Any],
    args: argparse.Namespace,
    base_exclusions: Counter[str],
    fetch_errors: Dict[str, str],
    candle_sets: Dict[str, list[StudyCandle]],
) -> None:
    floor_pct = fee_pct + net_margin_pct
    aggregates = {
        timeframe: aggregate_timeframe(observations, timeframe, fee_pct, floor_pct)
        for timeframe in timeframes
    }
    print("TREND-SOL | ATR exit timeframe study")
    print(
        f"Filter: real Bot B only | phantoms=no | profile={args.profile} | "
        f"strategy={args.strategy or 'all'} | since={args.since or 'all'} | "
        f"until={args.until or 'all'} | since_field={args.since_field}"
    )
    print(
        f"Runtime context: active_profile={config.get('active_profile')} | "
        f"entry timeframe={config.get('entry', {}).get('timeframe')} | "
        f"entry ATR period={config.get('entry', {}).get('atr_period')} | "
        f"runtime candle window={RUNTIME_CANDLE_WINDOW}"
    )
    print(
        f"Economics: round-trip fees={fee_pct:.3f}% | net margin={net_margin_pct:.3f}% | "
        f"economic floor={floor_pct:.3f}%"
    )
    print("Candles: Binance public market-data API | cache=in-memory per timeframe during this run")
    print("Approximation: only candles closed at or before opened_at; last 300 reproduce the runtime window.")
    print()
    print("1. DATA QUALITY")
    print(f"real trades analyzed | {len(observations)}")
    if observations:
        print(
            f"covered period | {_short_time(min(item.opened_at for item in observations))} to "
            f"{_short_time(max(item.opened_at for item in observations))}"
        )
    else:
        print("covered period | n/a")
    invalid_base = sum(base_exclusions.values())
    print(f"ledger records excluded before study | {invalid_base}")
    for reason, count in sorted(base_exclusions.items()):
        print(f"  {reason} | {count}")
    print("ATR TF | valid | excluded | exclusion reasons | fetched candles | gaps")
    for timeframe in timeframes:
        reasons = Counter(
            item.atrs[timeframe].reason
            for item in observations
            if not item.atrs[timeframe].available
        )
        reason_text = ", ".join(f"{key}={value}" for key, value in sorted(reasons.items())) or "none"
        candles = len(candle_sets.get(timeframe, []))
        gap_count = reasons.get("candle_gap", 0)
        print(
            f"{timeframe} | {aggregates[timeframe]['valid']} | {aggregates[timeframe]['excluded']} | "
            f"{reason_text} | {candles} | {gap_count}"
        )
        if timeframe in fetch_errors:
            print(f"  fetch error | {fetch_errors[timeframe]}")

    print()
    print("2. RULER SIZE | percentages use median entry ATR")
    print("ATR TF | trades | median ATR% | mean ATR% | p25 ATR% | p75 ATR% | 3ATR% | 5ATR% | 8ATR% | 10ATR% | 12ATR%")
    for timeframe in timeframes:
        item = aggregates[timeframe]
        median = item["median"]
        print(
            f"{timeframe} | {item['valid']} | {_pct(median)} | {_pct(item['mean'])} | "
            f"{_pct(item['p25'])} | {_pct(item['p75'])} | "
            + " | ".join(_pct(None if median is None else median * value) for value in TRIGGERS)
        )

    print()
    print("3. HISTORICAL PEAK REACHED | reach only; this is not an alternative-exit simulation")
    print("ATR TF | reached 3ATR | reached 5ATR | reached 8ATR | reached 10ATR | reached 12ATR")
    for timeframe in timeframes:
        item = aggregates[timeframe]
        print(
            f"{timeframe} | "
            + " | ".join(_count_pct(item["reached"][value], item["valid"]) for value in TRIGGERS)
        )

    print()
    print("4. ECONOMIC SIZE OF LOCKS")
    print("ATR TF | PL1 lock < fees | PL1 lock < floor | PL2 lock < fees | PL2 lock < floor | PL3 lock < fees | PL3 lock < floor")
    for timeframe in timeframes:
        item = aggregates[timeframe]
        cells = []
        for lock in LOCKS:
            cells.extend(
                [
                    _count_pct(item["below_fees"][lock], item["valid"]),
                    _count_pct(item["below_floor"][lock], item["valid"]),
                ]
            )
        print(f"{timeframe} | " + " | ".join(cells))

    print()
    print("5. TRIGGER-TO-LOCK GEOMETRY | median percentages")
    print("ATR TF | PL1 trigger% | PL1 lock% | PL1 gap% | PL2 trigger% | PL2 lock% | PL2 gap% | PL3 trigger% | PL3 lock% | PL3 gap%")
    for timeframe in timeframes:
        median = aggregates[timeframe]["median"]
        multipliers = (5, 1.5, 3.5, 8, 3, 5, 12, 6, 6)
        print(
            f"{timeframe} | "
            + " | ".join(_pct(None if median is None else median * value) for value in multipliers)
        )

    print()
    _print_age_analysis(observations, timeframes, period)
    if args.detail:
        print()
        _print_detail(observations, timeframes, fee_pct, floor_pct)
    print()
    print("Scope: geometry and historical peak reach only; no alternative exit, PnL, slot or path was simulated.")
    print("The report presents measurements and does not select a winning timeframe.")


def _print_age_analysis(
    observations: Sequence[TradeObservation],
    timeframes: Sequence[str],
    period: int,
) -> None:
    ages = [item.age_seconds / 60 for item in observations if item.age_seconds is not None and item.age_seconds >= 0]
    print("6. TRADE AGE VS ATR HORIZON | descriptive only")
    print(
        f"trade ages | N={len(ages)} | mean={_minutes(statistics.fmean(ages) if ages else None)} | "
        f"median={_minutes(percentile(ages, 50))} | p25={_minutes(percentile(ages, 25))} | "
        f"p75={_minutes(percentile(ages, 75))}"
    )
    print("ATR TF | ATR horizon approx | median trade age | ratio")
    median_age = percentile(ages, 50)
    for timeframe in timeframes:
        horizon = period * interval_milliseconds(timeframe) / 60_000
        ratio = median_age / horizon if median_age is not None and horizon > 0 else None
        print(f"{timeframe} | {_minutes(horizon)} | {_minutes(median_age)} | {_number(ratio)}")


def _print_detail(
    observations: Sequence[TradeObservation],
    timeframes: Sequence[str],
    fees_pct: float,
    floor_pct: float,
) -> None:
    print("7. DETAIL BY TRADE/TIMEFRAME")
    print(
        "opened | entry | peak% | age | ATR TF | ATR% | 3ATR% | 5ATR% | PL1 lock% | "
        "8ATR% | PL2 lock% | 10ATR% | 12ATR% | PL3 lock% | PL1<fees | PL1<floor | status"
    )
    for item in observations:
        for timeframe in timeframes:
            value = item.atrs[timeframe]
            atr_pct = value.atr_pct
            scaled = lambda multiple: None if atr_pct is None else atr_pct * multiple
            below_fees = atr_pct is not None and scaled(1.5) < fees_pct
            below_floor = atr_pct is not None and scaled(1.5) < floor_pct
            print(
                f"{_short_time(item.opened_at)} | {item.entry_price:.4f} | {item.peak_pct:+.3f}% | "
                f"{_minutes(None if item.age_seconds is None else item.age_seconds / 60)} | {timeframe} | "
                f"{_pct(atr_pct)} | {_pct(scaled(3))} | {_pct(scaled(5))} | {_pct(scaled(1.5))} | "
                f"{_pct(scaled(8))} | {_pct(scaled(3))} | {_pct(scaled(10))} | "
                f"{_pct(scaled(12))} | {_pct(scaled(6))} | {_bool(below_fees, atr_pct)} | "
                f"{_bool(below_floor, atr_pct)} | {value.reason or 'available'}"
            )


def round_trip_fee_pct(config: Dict[str, Any]) -> float:
    fees = config.get("fees") if isinstance(config.get("fees"), dict) else {}
    if not fees or not bool(fees.get("enabled", False)):
        return 0.0
    taker = _optional_float(fees.get("taker_fee_pct")) or 0.0
    if bool(fees.get("use_bnb_discount", False)):
        taker *= 0.75
    return taker * 2


def parse_timeframes(value: str) -> list[str]:
    output = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not output:
        raise SystemExit("--atr-timeframes must contain at least one timeframe")
    for timeframe in output:
        interval_milliseconds(timeframe)
    return output


def interval_milliseconds(value: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if len(value) < 2 or value[-1] not in units:
        raise ValueError(f"unsupported timeframe: {value}")
    try:
        amount = int(value[:-1])
    except ValueError:
        raise ValueError(f"unsupported timeframe: {value}") from None
    if amount <= 0:
        raise ValueError(f"unsupported timeframe: {value}")
    return amount * units[value[-1]]


def percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def parse_cli_datetime(value: Optional[str], reference: Optional[datetime] = None) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    current = reference or datetime.now(BRASILIA_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BRASILIA_TZ)
    try:
        if len(text) == 11 and text[2] == "/" and text[5] == " " and text[8] == ":":
            parsed = datetime.strptime(
                f"{text}/{current.astimezone(BRASILIA_TZ).year}",
                "%d/%m %H:%M/%Y",
            ).replace(tzinfo=BRASILIA_TZ)
        elif len(text) == 10:
            parsed = datetime.combine(date.fromisoformat(text), time.min)
        else:
            parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        raise SystemExit(
            f"Invalid date/time: {value}. Use DD/MM HH:MM or ISO 8601."
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BRASILIA_TZ)
    return parsed.astimezone(timezone.utc)


def _load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SystemExit(f"Invalid config: {path}")
    return data


def _parse_ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SystemExit(f"{label} must be a positive integer")
    return value


def nonnegative_float(value: Any, label: str) -> float:
    number = _optional_float(value)
    if number is None or number < 0:
        raise SystemExit(f"{label} must be greater than or equal to zero")
    return number


def floor_ms(value: int, interval_ms: int) -> int:
    return value - value % interval_ms


def _pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.3f}%"


def _count_pct(count: int, total: int) -> str:
    return f"{count} ({count / total * 100:.1f}%)" if total else "0 (n/a)"


def _minutes(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.1f}m"


def _number(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _bool(value: bool, available: Optional[float]) -> str:
    return "n/a" if available is None else ("yes" if value else "no")


def _short_time(value: datetime) -> str:
    return value.astimezone(BRASILIA_TZ).strftime("%d/%m %H:%M")


if __name__ == "__main__":
    main()
