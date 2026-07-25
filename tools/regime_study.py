from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.indicators.indicators import ema
from tools.cohort_study import (
    _dedupe_records,
    _fmt_ts,
    _is_real_bot_position,
    _load_config,
    _net_pnl,
    _net_usdt,
    _normalize_record,
    _opened_at,
    _operational_balance,
    _read_jsonl,
    _record_sort_key,
    _score_eligible,
)


UP = "UP"
DOWN = "DOWN"
MIXED = "MIXED"
UNKNOWN = "UNKNOWN"
REGIMES = (UP, MIXED, DOWN, UNKNOWN)


@dataclass(frozen=True)
class Kline:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class RegimePoint:
    candle: Kline
    ema: Optional[float]
    ema_previous: Optional[float]
    ema_slope_pct: Optional[float]
    price_vs_ema_pct: Optional[float]
    regime: str


@dataclass(frozen=True)
class RegimeObservation:
    record: Dict[str, Any]
    point: Optional[RegimePoint]

    @property
    def regime(self) -> str:
        return self.point.regime if self.point is not None else UNKNOWN

    @property
    def scored(self) -> bool:
        return _score_eligible(self.record)


@dataclass(frozen=True)
class RegimePolicy:
    name: str
    affected_regimes: frozenset[str]
    size_factor: float


@dataclass(frozen=True)
class PolicyDecision:
    policy: RegimePolicy
    observation: RegimeObservation
    factor: float

    @property
    def changed(self) -> bool:
        return self.factor < 1 - 1e-12

    @property
    def scored(self) -> bool:
        return self.observation.scored


def main() -> None:
    args = _parse_args()
    config = _load_config(Path(args.config))
    study = _study_config(config)
    records = _load_records([Path(value) for value in args.ledger], args.profile)
    if not records:
        raise SystemExit("No real Bot B entries found in the supplied ledgers.")

    timeframe = str(args.timeframe or study.get("timeframe", "1h"))
    interval_ms = _interval_ms(timeframe)
    ema_period = int(args.ema_period or study.get("ema_period", 50))
    slope_lookback = int(args.slope_lookback or study.get("ema_slope_lookback", 3))
    slope_deadband_pct = float(
        args.slope_deadband_pct
        if args.slope_deadband_pct is not None
        else study.get("slope_deadband_pct", 0)
    )
    warmup_candles = int(args.warmup_candles or study.get("warmup_candles", 300))
    episode_gap_hours = float(
        args.episode_gap_hours
        if args.episode_gap_hours is not None
        else study.get("episode_gap_hours", 6)
    )
    _validate_parameters(ema_period, slope_lookback, slope_deadband_pct, warmup_candles)

    opened = [value for item in records if (value := _opened_at(item)) is not None]
    if not opened:
        raise SystemExit("No valid opened_at timestamps found in the supplied ledgers.")
    required_start_ms = _floor_ms(
        int(min(opened).timestamp() * 1000) - warmup_candles * interval_ms,
        interval_ms,
    )
    required_end_ms = int(max(opened).timestamp() * 1000) - 1

    cache_path = Path(args.klines_cache)
    klines = load_kline_cache(cache_path)
    missing = missing_kline_ranges(klines, required_start_ms, required_end_ms, interval_ms)
    if missing and args.offline:
        raise SystemExit(
            f"Kline cache is missing {len(missing)} range(s); rerun without --offline to download them."
        )
    if missing:
        base_url = str(
            args.market_data_url
            or (config.get("market_data") or {}).get("rest_url")
            or "https://api.binance.com"
        )
        symbol = str(args.symbol or config.get("symbol") or "SOLUSDT").upper()
        downloaded = [
            candle
            for start_ms, end_ms in missing
            for candle in fetch_binance_klines(
                base_url,
                symbol,
                timeframe,
                start_ms,
                end_ms,
                int(args.http_timeout_seconds),
            )
        ]
        klines = merge_klines(klines, downloaded)
        save_kline_cache(cache_path, klines)
        remaining = missing_kline_ranges(
            klines,
            required_start_ms,
            required_end_ms,
            interval_ms,
        )
        if remaining:
            raise SystemExit(
                f"Binance kline download left {len(remaining)} uncovered range(s)."
            )

    relevant_klines = [
        item
        for item in klines
        if required_start_ms <= item.open_time_ms and item.close_time_ms <= required_end_ms
    ]
    points = build_regime_points(
        relevant_klines,
        ema_period,
        slope_lookback,
        slope_deadband_pct,
    )
    observations = label_records(records, points)
    policies = _policies_from_config(study)
    decisions = apply_policies(observations, policies)
    _print_report(
        observations,
        decisions,
        policies,
        timeframe,
        ema_period,
        slope_lookback,
        slope_deadband_pct,
        warmup_candles,
        _operational_balance(config),
        episode_gap_hours,
        cache_path,
        args.detail,
    )


def build_regime_points(
    klines: Sequence[Kline],
    ema_period: int,
    slope_lookback: int,
    slope_deadband_pct: float = 0.0,
) -> list[RegimePoint]:
    ordered = sorted(merge_klines([], klines), key=lambda item: item.open_time_ms)
    values = ema([item.close for item in ordered], ema_period)
    output: list[RegimePoint] = []
    for index, candle in enumerate(ordered):
        current = values[index]
        previous = values[index - slope_lookback] if index >= slope_lookback else None
        slope = (
            (float(current) - float(previous)) / float(previous) * 100
            if current is not None and previous not in {None, 0}
            else None
        )
        relation = (
            (candle.close - float(current)) / float(current) * 100
            if current not in {None, 0}
            else None
        )
        regime = UNKNOWN
        if slope is not None and relation is not None:
            if relation > 0 and slope > slope_deadband_pct:
                regime = UP
            elif relation < 0 and slope < -slope_deadband_pct:
                regime = DOWN
            else:
                regime = MIXED
        output.append(
            RegimePoint(
                candle=candle,
                ema=float(current) if current is not None else None,
                ema_previous=float(previous) if previous is not None else None,
                ema_slope_pct=slope,
                price_vs_ema_pct=relation,
                regime=regime,
            )
        )
    return output


def label_records(
    records: Sequence[Dict[str, Any]],
    points: Sequence[RegimePoint],
) -> list[RegimeObservation]:
    ordered_points = sorted(points, key=lambda item: item.candle.close_time_ms)
    output: list[RegimeObservation] = []
    point_index = -1
    for record in sorted(records, key=_record_sort_key):
        opened = _opened_at(record)
        if opened is None:
            output.append(RegimeObservation(record, None))
            continue
        opened_ms = int(opened.timestamp() * 1000)
        while (
            point_index + 1 < len(ordered_points)
            and ordered_points[point_index + 1].candle.close_time_ms < opened_ms
        ):
            point_index += 1
        point = ordered_points[point_index] if point_index >= 0 else None
        output.append(RegimeObservation(record, point))
    return output


def apply_policies(
    observations: Sequence[RegimeObservation],
    policies: Sequence[RegimePolicy],
) -> list[PolicyDecision]:
    output = []
    for policy in policies:
        _validate_policy(policy)
        for observation in observations:
            factor = policy.size_factor if observation.regime in policy.affected_regimes else 1.0
            output.append(PolicyDecision(policy, observation, factor))
    return output


def fetch_binance_klines(
    base_url: str,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    timeout_seconds: int = 8,
) -> list[Kline]:
    if end_ms < start_ms:
        return []
    interval_ms = _interval_ms(interval)
    cursor = _floor_ms(start_ms, interval_ms)
    output: list[Kline] = []
    session = requests.Session()
    while cursor <= end_ms:
        response = session.get(
            f"{base_url.rstrip('/')}/api/v3/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
            timeout=timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Binance market data error {response.status_code}: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected Binance kline response: {payload}")
        page = [_kline_from_binance(item) for item in payload if isinstance(item, list)]
        if not page:
            break
        output.extend(page)
        next_cursor = max(item.open_time_ms for item in page) + interval_ms
        if next_cursor <= cursor:
            raise RuntimeError("Binance kline pagination did not advance.")
        cursor = next_cursor
        if len(page) < 1000:
            break
    return [
        item
        for item in merge_klines([], output)
        if item.open_time_ms >= start_ms and item.close_time_ms <= end_ms
    ]


def load_kline_cache(path: Path) -> list[Kline]:
    output = []
    for item in _read_jsonl(path):
        try:
            output.append(
                Kline(
                    open_time_ms=int(item["open_time_ms"]),
                    close_time_ms=int(item["close_time_ms"]),
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    volume=float(item.get("volume", 0)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return merge_klines([], output)


def save_kline_cache(path: Path, klines: Sequence[Kline]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for item in sorted(merge_klines([], klines), key=lambda value: value.open_time_ms):
            handle.write(
                json.dumps(
                    {
                        "open_time_ms": item.open_time_ms,
                        "close_time_ms": item.close_time_ms,
                        "open": item.open,
                        "high": item.high,
                        "low": item.low,
                        "close": item.close,
                        "volume": item.volume,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
    tmp.replace(path)


def missing_kline_ranges(
    klines: Sequence[Kline],
    required_start_ms: int,
    required_end_ms: int,
    interval_ms: int,
) -> list[tuple[int, int]]:
    first_open = _floor_ms(required_start_ms, interval_ms)
    last_open = _floor_ms(required_end_ms - interval_ms + 1, interval_ms)
    if last_open < first_open:
        return []
    existing = {
        item.open_time_ms
        for item in klines
        if first_open <= item.open_time_ms <= last_open
    }
    missing_opens = [
        value
        for value in range(first_open, last_open + 1, interval_ms)
        if value not in existing
    ]
    if not missing_opens:
        return []
    output: list[tuple[int, int]] = []
    range_start = missing_opens[0]
    previous = missing_opens[0]
    for value in missing_opens[1:]:
        if value != previous + interval_ms:
            output.append((range_start, min(required_end_ms, previous + interval_ms - 1)))
            range_start = value
        previous = value
    output.append((range_start, min(required_end_ms, previous + interval_ms - 1)))
    return output


def merge_klines(
    existing: Sequence[Kline],
    incoming: Sequence[Kline],
) -> list[Kline]:
    indexed = {item.open_time_ms: item for item in existing}
    indexed.update({item.open_time_ms: item for item in incoming})
    return [indexed[key] for key in sorted(indexed)]


def _print_report(
    observations: Sequence[RegimeObservation],
    decisions: Sequence[PolicyDecision],
    policies: Sequence[RegimePolicy],
    timeframe: str,
    ema_period: int,
    slope_lookback: int,
    slope_deadband_pct: float,
    warmup_candles: int,
    operational_balance_usdt: float,
    episode_gap_hours: float,
    cache_path: Path,
    detail: bool,
) -> None:
    scored = [item for item in observations if item.scored]
    baseline_usdt = sum(_net_usdt(item.record) or 0.0 for item in scored)
    print("TREND-SOL | higher-timeframe regime study")
    print(
        f"Sample: entries={len(observations)} | scored={len(scored)} | "
        f"context/unscored={len(observations) - len(scored)} | "
        f"baseline={baseline_usdt:+.4f} USDT"
    )
    print(
        f"Regime: {timeframe} EMA{ema_period} | slope lookback={slope_lookback} closed candles | "
        f"deadband={slope_deadband_pct:.4f}% | warmup={warmup_candles}"
    )
    print("No lookahead: each entry uses the last higher-timeframe candle closed before it.")
    print(f"Kline cache: {cache_path}")
    print()
    print("Observed outcome by regime:")
    print(
        f"{'regime':8} {'trades':>6} {'wins':>5} {'loss':>5} {'HS':>4} "
        f"{'net':>10} {'avg net':>10} {'PF':>8}"
    )
    for regime in REGIMES:
        selected = [item for item in scored if item.regime == regime]
        values = [_net_usdt(item.record) or 0.0 for item in selected]
        print(
            f"{regime:8} {len(selected):6d} "
            f"{sum(value > 0 for value in values):5d} "
            f"{sum(value < 0 for value in values):5d} "
            f"{sum(str(item.record.get('exit_reason') or '') == 'HARD_STOP' for item in selected):4d} "
            f"{sum(values):+10.4f} "
            f"{(sum(values) / len(values) if values else 0.0):+10.4f} "
            f"{_fmt_profit_factor(values):>8}"
        )
    print()
    print("Policy replay (first-order; historical occupancy is unchanged):")
    print(
        f"{'policy':13} {'changed':>7} {'factor':>7} {'chgW':>5} {'chgL':>5} "
        f"{'HS':>4} {'saveL':>9} {'cutW':>9} {'delta':>9} {'hyp_net':>9} "
        f"{'bal_pp':>8} {'episodes':>8} {'score +/-/=':>11}"
    )
    for policy in policies:
        changed = [
            item
            for item in decisions
            if item.policy == policy and item.changed and item.scored
        ]
        saved_losses, cut_winners, delta = _decision_economics(changed)
        episodes = _decision_episodes(changed, episode_gap_hours)
        scores = _episode_score(
            [sum(_decision_delta(item) for item in group) for group in episodes]
        )
        print(
            f"{policy.name:13} {len(changed):7d} {policy.size_factor:7.2f} "
            f"{sum((_net_usdt(item.observation.record) or 0.0) > 0 for item in changed):5d} "
            f"{sum((_net_usdt(item.observation.record) or 0.0) < 0 for item in changed):5d} "
            f"{sum(str(item.observation.record.get('exit_reason') or '') == 'HARD_STOP' for item in changed):4d} "
            f"{saved_losses:9.4f} {cut_winners:9.4f} {delta:+9.4f} "
            f"{baseline_usdt + delta:+9.4f} "
            f"{(delta / operational_balance_usdt * 100 if operational_balance_usdt else 0.0):+8.3f} "
            f"{len(episodes):8d} {scores[0]:3d}/{scores[1]}/{scores[2]:<3d}"
        )
    print()
    print("chgW/chgL=net winners/losses resized or blocked | HS=affected HARD_STOPs")
    print("saveL/cutW/delta/hyp_net are USDT; positive delta improves the historical sample.")
    print(
        "This replay rescales actual outcomes and does not invent signals that changed slot occupancy "
        "could have created."
    )
    if not detail:
        return
    print()
    print("Entry classification detail:")
    for item in observations:
        point = item.point
        print(
            f"  {_fmt_ts(item.record.get('opened_at'))} pair={item.record.get('pair_id')} "
            f"regime={item.regime} candle={_fmt_candle(point)} "
            f"close={_fmt_optional(point.candle.close if point else None)} "
            f"ema={_fmt_optional(point.ema if point else None)} "
            f"slope={_fmt_pct(point.ema_slope_pct if point else None)} "
            f"vs_ema={_fmt_pct(point.price_vs_ema_pct if point else None)} "
            f"outcome={item.record.get('exit_reason') or 'OPEN'} "
            f"net={_fmt_pct(_net_pnl(item.record))} scored={'yes' if item.scored else 'no'}"
        )


def _decision_economics(
    decisions: Sequence[PolicyDecision],
) -> tuple[float, float, float]:
    saved_losses = 0.0
    cut_winners = 0.0
    for item in decisions:
        actual = _net_usdt(item.observation.record) or 0.0
        if actual < 0:
            saved_losses += -actual * (1 - item.factor)
        elif actual > 0:
            cut_winners += actual * (1 - item.factor)
    return saved_losses, cut_winners, saved_losses - cut_winners


def _decision_delta(item: PolicyDecision) -> float:
    actual = _net_usdt(item.observation.record) or 0.0
    return actual * item.factor - actual


def _decision_episodes(
    decisions: Sequence[PolicyDecision],
    gap_hours: float,
) -> list[list[PolicyDecision]]:
    ordered = sorted(
        decisions,
        key=lambda item: _opened_at(item.observation.record)
        or datetime.min.replace(tzinfo=timezone.utc),
    )
    output: list[list[PolicyDecision]] = []
    for item in ordered:
        opened = _opened_at(item.observation.record)
        if opened is None:
            continue
        previous = (
            _opened_at(output[-1][-1].observation.record) if output else None
        )
        if previous is None or opened - previous > timedelta(hours=gap_hours):
            output.append([item])
        else:
            output[-1].append(item)
    return output


def _episode_score(values: Sequence[float]) -> tuple[int, int, int]:
    improved = sum(value > 1e-9 for value in values)
    worse = sum(value < -1e-9 for value in values)
    return improved, worse, len(values) - improved - worse


def _load_records(paths: Sequence[Path], profile: str) -> list[Dict[str, Any]]:
    records = [
        _normalize_record(item)
        for path in paths
        for item in _read_jsonl(path)
    ]
    return sorted(
        _dedupe_records(
            item
            for item in records
            if _is_real_bot_position(item)
            and (
                profile == "all"
                or not item.get("profile")
                or str(item.get("profile")) == profile
            )
        ),
        key=_record_sort_key,
    )


def _study_config(config: Dict[str, Any]) -> Dict[str, Any]:
    instrumentation = (
        config.get("instrumentation")
        if isinstance(config.get("instrumentation"), dict)
        else {}
    )
    value = instrumentation.get("regime_study")
    return value if isinstance(value, dict) else {}


def _policies_from_config(study: Dict[str, Any]) -> list[RegimePolicy]:
    values = study.get("policies") or [
        {"name": "BLOCK_DOWN", "affected_regimes": [DOWN], "size_factor": 0},
        {"name": "HALF_DOWN", "affected_regimes": [DOWN], "size_factor": 0.5},
        {
            "name": "REQUIRE_UP",
            "affected_regimes": [DOWN, MIXED],
            "size_factor": 0,
        },
    ]
    output = []
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            continue
        regimes = frozenset(
            str(value).upper() for value in item.get("affected_regimes") or []
        )
        policy = RegimePolicy(
            name=str(item.get("name") or f"POLICY_{index + 1}").upper(),
            affected_regimes=regimes,
            size_factor=float(item.get("size_factor", 0)),
        )
        _validate_policy(policy)
        output.append(policy)
    return output


def _validate_policy(policy: RegimePolicy) -> None:
    if not policy.affected_regimes:
        raise ValueError(f"{policy.name}: affected_regimes cannot be empty")
    invalid = policy.affected_regimes.difference({UP, DOWN, MIXED})
    if invalid:
        raise ValueError(f"{policy.name}: invalid regimes: {sorted(invalid)}")
    if not 0 <= policy.size_factor <= 1:
        raise ValueError(f"{policy.name}: size_factor must be between 0 and 1")


def _validate_parameters(
    ema_period: int,
    slope_lookback: int,
    slope_deadband_pct: float,
    warmup_candles: int,
) -> None:
    if ema_period <= 0:
        raise ValueError("ema_period must be positive")
    if slope_lookback <= 0:
        raise ValueError("ema_slope_lookback must be positive")
    if slope_deadband_pct < 0:
        raise ValueError("slope_deadband_pct cannot be negative")
    if warmup_candles < ema_period + slope_lookback:
        raise ValueError("warmup_candles must cover ema_period plus slope lookback")


def _kline_from_binance(value: Sequence[Any]) -> Kline:
    if len(value) < 7:
        raise ValueError("Binance kline must have at least 7 fields")
    return Kline(
        open_time_ms=int(value[0]),
        close_time_ms=int(value[6]),
        open=float(value[1]),
        high=float(value[2]),
        low=float(value[3]),
        close=float(value[4]),
        volume=float(value[5]),
    )


def _interval_ms(value: str) -> int:
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000}
    if len(value) < 2 or value[-1] not in units:
        raise ValueError(f"Unsupported timeframe: {value}")
    try:
        count = int(value[:-1])
    except ValueError:
        raise ValueError(f"Unsupported timeframe: {value}") from None
    if count <= 0:
        raise ValueError(f"Unsupported timeframe: {value}")
    return count * units[value[-1]]


def _floor_ms(value: int, interval_ms: int) -> int:
    return value - value % interval_ms


def _fmt_profit_factor(values: Sequence[float]) -> str:
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    if losses <= 1e-12:
        return "n/a" if gains <= 1e-12 else "inf"
    return f"{gains / losses:.2f}"


def _fmt_candle(point: Optional[RegimePoint]) -> str:
    if point is None:
        return "n/a"
    return datetime.fromtimestamp(
        point.candle.open_time_ms / 1000,
        tz=timezone.utc,
    ).strftime("%d/%m %H:%M")


def _fmt_optional(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _fmt_pct(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Estudo offline de regime em timeframe superior; nunca envia ordens "
            "nem altera estado, slots ou saldo."
        )
    )
    parser.add_argument(
        "--ledger",
        action="append",
        required=True,
        help="Ledger trades_B.jsonl; pode ser repetido.",
    )
    parser.add_argument(
        "--profile",
        choices=["intraday", "production", "all"],
        default="intraday",
    )
    parser.add_argument("--timeframe")
    parser.add_argument("--ema-period", type=int)
    parser.add_argument("--slope-lookback", type=int)
    parser.add_argument("--slope-deadband-pct", type=float)
    parser.add_argument("--warmup-candles", type=int)
    parser.add_argument("--episode-gap-hours", type=float)
    parser.add_argument("--symbol")
    parser.add_argument("--market-data-url")
    parser.add_argument("--http-timeout-seconds", type=int, default=8)
    parser.add_argument(
        "--klines-cache",
        default=str(PROJECT_ROOT / "data/market/solusdt_1h.jsonl"),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Proibe downloads e exige que o cache cubra toda a amostra.",
    )
    parser.add_argument("--detail", action="store_true")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config/config.yaml"))
    return parser.parse_args()


if __name__ == "__main__":
    main()
