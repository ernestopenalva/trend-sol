"""Read-only exit-ladder validation for REAL_A seeds and recorded aggTrades.

This tool intentionally does not model entries, gates, slots, admission, fills, or
balances.  It reuses BotFullExitPosition and feeds it the recorded aggTrade stream.
The ledger has no persisted trigger timestamp; validation compares the simulated
aggTrade timestamp with the ledger's closed_at (the closest persisted surrogate).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.position.bot_full_engine import BotFullExitPosition


@dataclass(frozen=True)
class Tick:
    timestamp: datetime
    price: float


@dataclass(frozen=True)
class Seed:
    pair_id: str
    symbol: str
    opened_at: datetime
    entry_price: float
    entry_atr: float
    ledger_reason: str
    ledger_trigger_price: float | None
    ledger_closed_at: datetime
    no_progress_enabled: bool
    no_progress_tolerance_seconds: float | None
    no_progress_tolerance_source: str | None


class _NoopLogger:
    """Prevents a read-only run from emitting trade or system JSONL records."""

    def trade(self, event: dict[str, Any]) -> None:
        return None

    def system(self, event: str, **fields: Any) -> None:
        return None


class _NoopClient:
    """The engine needs a client only when it closes; an empty order uses tick price."""

    def market_sell(self, symbol: str, quantity: float, client_order_id: str) -> dict[str, Any]:
        return {}


def main() -> None:
    args = _parse_args()
    raw_config = _read_yaml(args.config)
    config = effective_config(raw_config)
    seeds = load_real_a_seeds(args.ledger, _parse_timestamp(args.since))
    ticks = load_aggtrades(args.aggtrades)
    _validate_coverage(seeds, ticks, args.max_gap_seconds)

    baseline = run_simulation(seeds, ticks, _exit_config(config))
    _print_validation(baseline)
    matched = sum(item["reason_match"] for item in baseline)
    agreement = matched / len(baseline) if baseline else 0.0
    if agreement < 0.95:
        print("\nCOUNTERFACTUAL NOT RUN: reason agreement below 95%.")
        raise SystemExit(2)

    if args.variant is None:
        print("\nAUTOVALIDATION PASSED. No counterfactual requested.")
        return

    variant_raw = _read_yaml(args.variant)
    _validate_variant(variant_raw)
    variant_config = effective_config(_deep_merge(raw_config, variant_raw))
    variant = run_simulation(seeds, ticks, _exit_config(variant_config))
    print("\nSINGLE VARIANT (read-only; no parameter sweep)")
    _print_variant(baseline, variant)


def load_real_a_seeds(ledger_path: Path, since: datetime) -> list[Seed]:
    records = _read_jsonl(ledger_path)
    seeds: list[Seed] = []
    for item in records:
        if item.get("phantom") or item.get("shadow_kind") or item.get("position_type") != "BOT_EXIT":
            continue
        closed_at = _parse_timestamp(item.get("closed_at"))
        if closed_at is None or closed_at < since:
            continue
        entry_atr = _as_float(item.get("entry_atr"))
        entry_price = _as_float(item.get("entry_price"))
        opened_at = _parse_timestamp(item.get("opened_at"))
        if entry_atr is None or entry_atr <= 0 or entry_price is None or entry_price <= 0 or opened_at is None:
            continue
        seeds.append(
            Seed(
                pair_id=str(item["pair_id"]), symbol=str(item.get("symbol") or "SOLUSDT"),
                opened_at=opened_at, entry_price=entry_price, entry_atr=entry_atr,
                ledger_reason=str(item.get("exit_reason") or ""),
                ledger_trigger_price=_as_float(item.get("exit_trigger_price")),
                ledger_closed_at=closed_at,
                no_progress_enabled=bool(item.get("no_progress_enabled", False)),
                no_progress_tolerance_seconds=_as_float(item.get("no_progress_tolerance_seconds")),
                no_progress_tolerance_source=item.get("no_progress_tolerance_source"),
            )
        )
    if not seeds:
        raise ValueError("No REAL_A closed BOT_EXIT seeds found for the requested window.")
    return sorted(seeds, key=lambda item: item.opened_at)


def load_aggtrades(path: Path) -> list[Tick]:
    ticks: list[Tick] = []
    for item in _read_jsonl(path):
        payload = item.get("data") if isinstance(item.get("data"), dict) else item
        price = _as_float(payload.get("p") or payload.get("price"))
        timestamp = _parse_timestamp(payload.get("T") or payload.get("timestamp") or payload.get("ts"))
        if price is not None and price > 0 and timestamp is not None:
            ticks.append(Tick(timestamp, price))
    if not ticks:
        raise ValueError("No aggTrade ticks found. Expected JSONL with p/price and T/timestamp/ts.")
    return sorted(ticks, key=lambda item: item.timestamp)


def run_simulation(seeds: Iterable[Seed], ticks: list[Tick], exit_config: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for seed in seeds:
        position = BotFullExitPosition(
            pair_id=seed.pair_id, symbol=seed.symbol, entry_price=seed.entry_price, quantity=1.0,
            entry_order={}, open_ts=seed.opened_at.isoformat(), config=deepcopy(exit_config),
            client=_NoopClient(), logger=_NoopLogger(), entry_atr=seed.entry_atr,
            atr_timeframe="1m", atr_period=14, no_progress_enabled=seed.no_progress_enabled,
            no_progress_tolerance_seconds=seed.no_progress_tolerance_seconds,
            no_progress_tolerance_source=seed.no_progress_tolerance_source,
        )
        result: dict[str, Any] | None = None
        trigger_at: datetime | None = None
        for tick in ticks:
            if tick.timestamp < seed.opened_at:
                continue
            result = position.on_tick(tick.price, market_ts=tick.timestamp.isoformat())
            if result is not None:
                trigger_at = tick.timestamp
                break
        simulated_reason = str((result or {}).get("exit_reason") or "UNRESOLVED")
        trigger_price = _as_float((result or {}).get("trigger_price"))
        price_error = None if trigger_price is None or seed.ledger_trigger_price is None else abs(trigger_price - seed.ledger_trigger_price)
        time_error = None if trigger_at is None else abs((trigger_at - seed.ledger_closed_at).total_seconds())
        output.append({
            "pair_id": seed.pair_id, "ledger_reason": seed.ledger_reason, "simulated_reason": simulated_reason,
            "reason_match": simulated_reason == seed.ledger_reason, "ledger_trigger_price": seed.ledger_trigger_price,
            "simulated_trigger_price": trigger_price, "trigger_at": trigger_at,
            "ledger_closed_at": seed.ledger_closed_at, "price_error": price_error, "time_error_seconds": time_error,
        })
    return output


def _print_validation(rows: list[dict[str, Any]]) -> None:
    matches = sum(row["reason_match"] for row in rows)
    price_errors = [row["price_error"] for row in rows if row["price_error"] is not None]
    time_errors = [row["time_error_seconds"] for row in rows if row["time_error_seconds"] is not None]
    print("REAL_A exit ladder autovalidation")
    print(f"seeds: {len(rows)}")
    print(f"reason agreement: {matches}/{len(rows)} ({matches / len(rows) * 100:.2f}%)")
    print(f"trigger price abs error mean/median: {_mean(price_errors):.8f} / {_median(price_errors):.8f}")
    print(f"trigger time vs ledger closed_at abs seconds mean/median: {_mean(time_errors):.3f} / {_median(time_errors):.3f}")
    divergent = [row for row in rows if not row["reason_match"]]
    if divergent:
        print("divergences:")
        for row in divergent:
            print(
                f"  {row['pair_id']} ledger={row['ledger_reason']} sim={row['simulated_reason']} "
                f"ledger_trigger={row['ledger_trigger_price']} sim_trigger={row['simulated_trigger_price']} "
                f"sim_at={_format_ts(row['trigger_at'])} ledger_closed={_format_ts(row['ledger_closed_at'])}"
            )


def _print_variant(baseline: list[dict[str, Any]], variant: list[dict[str, Any]]) -> None:
    for base, changed in zip(baseline, variant):
        if (base["simulated_reason"], base["simulated_trigger_price"], base["trigger_at"]) != (
            changed["simulated_reason"], changed["simulated_trigger_price"], changed["trigger_at"]
        ):
            print(
                f"{base['pair_id']} | baseline={base['simulated_reason']} @ {base['simulated_trigger_price']} "
                f"{_format_ts(base['trigger_at'])} | variant={changed['simulated_reason']} @ "
                f"{changed['simulated_trigger_price']} {_format_ts(changed['trigger_at'])}"
            )


def _exit_config(config: dict[str, Any]) -> dict[str, Any]:
    output = deepcopy(config.get("risk") or config["exit_bot_full_engine"])
    output["fees"] = deepcopy(config.get("fees") or {})
    output["ladder"] = deepcopy(config.get("ladder") or {})
    return output


def _validate_coverage(seeds: list[Seed], ticks: list[Tick], max_gap_seconds: float) -> None:
    if ticks[0].timestamp > min(seed.opened_at for seed in seeds):
        raise ValueError("aggTrade file begins after the first seed; coverage before entries is required.")
    for previous, current in zip(ticks, ticks[1:]):
        gap = (current.timestamp - previous.timestamp).total_seconds()
        if gap > max_gap_seconds:
            raise ValueError(f"aggTrade coverage gap of {gap:.3f}s exceeds --max-gap-seconds={max_gap_seconds}.")


def _validate_variant(value: dict[str, Any]) -> None:
    allowed = {"risk", "fees", "ladder"}
    unexpected = set(value) - allowed
    if unexpected:
        raise ValueError(f"Variant may change only risk/fees/ladder, not {sorted(unexpected)}.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only REAL_A exit-ladder simulator over recorded aggTrade JSONL.")
    parser.add_argument("--ledger", type=Path, default=PROJECT_ROOT / "data" / "trades" / "trades_B.jsonl")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "config.yaml")
    parser.add_argument("--aggtrades", type=Path, required=True, help="Raw aggTrade JSONL: p/price and T/timestamp/ts.")
    parser.add_argument("--since", default="2026-08-19T01:05:00-03:00")
    parser.add_argument("--max-gap-seconds", type=float, default=30.0)
    parser.add_argument("--variant", type=Path, help="Optional single YAML overlay; only runs after >=95%% baseline reason agreement.")
    return parser.parse_args()


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    output = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                output.append(value)
    return output


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        milliseconds = float(value)
        return datetime.fromtimestamp(milliseconds / (1000 if milliseconds > 10_000_000_000 else 1), timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _as_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    output = deepcopy(base)
    for key, value in overlay.items():
        output[key] = _deep_merge(output[key], value) if isinstance(value, dict) and isinstance(output.get(key), dict) else deepcopy(value)
    return output


def _mean(values: list[float]) -> float:
    return statistics.mean(values) if values else float("nan")


def _median(values: list[float]) -> float:
    return statistics.median(values) if values else float("nan")


def _format_ts(value: datetime | None) -> str:
    return value.isoformat() if value else "n/a"


if __name__ == "__main__":
    main()
