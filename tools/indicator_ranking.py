from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Callable, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.trade_ledger import TradeLedger


def main() -> None:
    parser = argparse.ArgumentParser(description="Observational indicator ranking; never changes runtime.")
    parser.add_argument("--strategy", choices=["A", "B"], default="A")
    parser.add_argument("--detail", action="store_true")
    args = parser.parse_args()
    path = PROJECT_ROOT / ("data/trades/trades_B.jsonl" if args.strategy == "A" else "data/trades/trades_gcr_shadow.jsonl")
    records = [item for item in TradeLedger(PROJECT_ROOT, path).load() if isinstance(item.get("market_context_entry"), dict)]
    rules: Dict[str, Callable[[Dict[str, Any]], int]] = {
        "EMA alignment": lambda x: _sign(_v(x, "ema20") - _v(x, "ema50")),
        "EMA slope": lambda x: _sign(_v(x, "ema20_slope_pct")),
        "DMI direction": lambda x: _sign(_v(x, "plus_di14") - _v(x, "minus_di14")),
        "ADX strength": lambda x: 1 if _v(x, "adx14") >= 25 else 0,
        "RSI": lambda x: 1 if _v(x, "rsi14") > 55 else -1 if _v(x, "rsi14") < 45 else 0,
        "relative volume": lambda x: 1 if _v(x, "relative_volume") >= 1 else 0,
        "GE15": lambda x: 1 if x.get("_ge15") == "PASS" else -1,
        "EMA slope + DMI": lambda x: _agree(_sign(_v(x, "ema20_slope_pct")), _sign(_v(x, "plus_di14")-_v(x, "minus_di14"))),
        "EMA slope + ADX": lambda x: _sign(_v(x, "ema20_slope_pct")) if _v(x, "adx14") >= 25 else 0,
        "GE15 + DMI": lambda x: _agree(1 if x.get("_ge15") == "PASS" else -1, _sign(_v(x, "plus_di14")-_v(x, "minus_di14"))),
        "GE15 + ADX": lambda x: (1 if x.get("_ge15") == "PASS" else -1) if _v(x, "adx14") >= 25 else 0,
        "EMA + DMI + ADX": lambda x: _agree(_sign(_v(x, "ema20_slope_pct")), _sign(_v(x, "plus_di14")-_v(x, "minus_di14"))) if _v(x, "adx14") >= 25 else 0,
    }
    rows = []
    for name, rule in rules.items():
        cases = []
        long_count = short_count = neutral_count = 0
        outcomes: Dict[str, int] = {}
        for record in records:
            context = record["market_context_entry"]
            values = dict(context.get("tf_5m") or {})
            values["_ge15"] = (context.get("ge15") or {}).get("status")
            direction = rule(values)
            if direction == 0:
                neutral_count += 1
                continue
            if direction > 0:
                long_count += 1
            else:
                short_count += 1
            reason = str(record.get("exit_reason") or "UNKNOWN")
            outcomes[reason] = outcomes.get(reason, 0) + 1
            gross = _f(record.get("gross_pnl_pct"))
            net = _f(record.get("net_pnl_pct"))
            peak = _f(record.get("peak_atr"))
            trough = _f(record.get("trough_atr"))
            if gross is not None:
                entry = _f(record.get("entry_price"))
                peak_price = _f(record.get("peak_price"))
                peak_pct = ((peak_price / entry - 1) * 100) if entry and peak_price is not None else None
                retained = gross / peak_pct * 100 if peak_pct and peak_pct > 0 else None
                cases.append((direction, gross, net or 0, peak, trough, retained, _f(record.get("time_to_be_seconds"))))
        accuracy = sum(1 for d, g, *_ in cases if (g > 0 and d > 0) or (g < 0 and d < 0)) / len(cases) * 100 if cases else 0
        avg_net = sum(item[2] for item in cases) / len(cases) if cases else 0
        consistency = sum(1 for _, _, n, *_ in cases if n > 0) / len(cases) * 100 if cases else 0
        peaks = [item[3] for item in cases if item[3] is not None]
        troughs = [item[4] for item in cases if item[4] is not None]
        retained = [item[5] for item in cases if item[5] is not None]
        be_times = [item[6] for item in cases if item[6] is not None]
        avg_peak = sum(peaks) / len(peaks) if peaks else 0
        avg_trough = sum(troughs) / len(troughs) if troughs else 0
        exit_quality = sum(retained) / len(retained) if retained else 0
        avg_be_minutes = sum(be_times) / len(be_times) / 60 if be_times else 0
        score = accuracy * 0.5 + consistency * 0.3 + max(-10, min(10, avg_net * 10)) * 0.2
        rows.append((score, name, len(cases), long_count, short_count, neutral_count, accuracy, avg_net, avg_peak, avg_trough, avg_be_minutes, exit_quality, consistency, outcomes))
    print("TREND-SOL | observational indicator ranking | telemetry only")
    print("indicator | N | LONG/SHORT/NEUTRAL | directional_accuracy | avg net | MFE ATR | MAE ATR | avg time_to_BE | exit_quality(% peak retained) | consistency | HS/NPE/TRAIL | score")
    for score, name, n, longs, shorts, neutrals, accuracy, avg_net, avg_peak, avg_trough, avg_be_minutes, exit_quality, consistency, outcomes in sorted(rows, reverse=True):
        print(
            f"{name} | {n} | {longs}/{shorts}/{neutrals} | {accuracy:.1f}% | {avg_net:+.3f}% | "
            f"{avg_peak:+.2f} | {avg_trough:+.2f} | {avg_be_minutes:.1f}m | {exit_quality:.1f}% | "
            f"{consistency:.1f}% | {outcomes.get('HARD_STOP', 0)}/{outcomes.get('NO_PROGRESS_EXIT', 0)}/{outcomes.get('TRAILING', 0)} | {score:.2f}"
        )
    print("score = 50% directional accuracy + 30% positive-net consistency + 20% capped avg-net component")


def _v(item: Dict[str, Any], key: str) -> float:
    return _f(item.get(key)) or 0.0


def _f(value: Any):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _agree(left: int, right: int) -> int:
    return left if left != 0 and left == right else 0


if __name__ == "__main__":
    main()
