"""Independent full-engine REAL_A circuit-breaker replay; read-only."""
from __future__ import annotations

import argparse
import math
import statistics
import sys
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger
from tools.cohort_study import _load_config
from tools.ge_replay_study import MATCH_TOLERANCE_MS, WARMUP_CANDLES, ReplayResult, generate_ge_signals, load_ge_market_data, real_bot_b_records, run_universe, validate_replay
from tools.market_bot_replay import MINUTE_MS, _round_trip_fees_pct
from tools.market_selection_study import BinancePublicClient


@dataclass(frozen=True)
class Rule:
    name: str
    kind: str
    threshold: float
    hours: float = 0.0
    min_closes: int = 0


class CircuitGuard:
    """A strict admission-only governor; positions/exits never pass through it."""
    def __init__(self, rule: Rule, cooldown_h: float, capital: float, notional: float) -> None:
        self.rule, self.cooldown_ms, self.capital, self.notional = rule, int(cooldown_h * 3600_000), capital, notional
        self.cursor = 0; self.equity = self.peak = capital; self.history: list[tuple[int, float]] = []
        self.was_true = False; self.pause_until = -1; self.crises = 0; self.paused_minutes = 0

    def allows(self, boundary: int, result: ReplayResult) -> bool:
        for trade in result.trades[self.cursor:]:
            dollars = self.notional * trade.net_pct / 100
            self.equity += dollars; self.peak = max(self.peak, self.equity); self.history.append((trade.closed_ms, dollars))
        self.cursor = len(result.trades)
        state = self._state(boundary)
        if state and not self.was_true:
            self.pause_until = max(self.pause_until, boundary + self.cooldown_ms); self.crises += 1
        self.was_true = state
        paused = boundary < self.pause_until
        if paused: self.paused_minutes += 1
        return not paused

    def _state(self, boundary: int) -> bool:
        drawdown_pct = (self.peak - self.equity) / self.capital * 100
        if self.rule.kind == "DD": return drawdown_pct >= self.rule.threshold
        window = [(t, p) for t, p in self.history if boundary - int(self.rule.hours * 3600_000) < t <= boundary]
        window_pct = sum(p for _, p in window) / self.capital * 100
        if self.rule.kind == "PNL": return window_pct <= -self.rule.threshold
        return drawdown_pct >= self.rule.threshold and window_pct <= -0.5 and len(window) >= self.rule.min_closes


def main() -> None:
    args = _args(); start, end = _ts(args.since), _ts(args.until)
    if not start or not end or end <= start: raise SystemExit("--since/--until must be valid offset/BRT timestamps")
    raw = _load_config(Path(args.config)); config = effective_config(raw); _validate(config)
    config = deepcopy(config); config["capital"]["operational_balance_usdt"] = args.capital
    spread = float(args.round_trip_spread_bps if args.round_trip_spread_bps is not None else config.get("instrumentation", {}).get("market_bot_replay", {}).get("round_trip_spread_bps", 5.0))
    start_ms, end_ms = int(start.timestamp()*1000), int(end.timestamp()*1000)-1
    data_start = start_ms - WARMUP_CANDLES * 15 * MINUTE_MS
    client = BinancePublicClient(str(args.market_data_url or config.get("market_data",{}).get("rest_url") or "https://api.binance.com"), args.http_timeout_seconds)
    cache = Path(args.cache_dir); symbol = str(config.get("symbol") or "SOLUSDT")
    candles = {i: load_ge_market_data(client, symbol, i, data_start, end_ms, cache, bool(args.offline)) for i in ("1m", "5m", "15m")}
    signals = _signals(config, candles, start_ms, end_ms)
    notional = args.capital * float(config["capital"]["trade_size_pct"]) / 100
    print("TREND-SOL | REAL_A circuit breaker | FULL-ENGINE REPLAY | READ-ONLY")
    print(f"Window: {_fmt(start)} -> {_fmt(end)} (end exclusive) | signals={len(signals)} | OHLC path={args.intrabar_path.upper()} | capital=${args.capital:.2f}")
    print("Predeclared candidates: DD_1P5; PNL_2H_0P5; COMBO_DD1P5_PNL4H0P5_MIN2. Cooldowns: 1h/2h/4h/6h.")
    base = run_universe(name="CONTROL", lookback=0, config=config, signals=signals, execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=args.intrabar_path.upper(), round_trip_spread_bps=spread)
    _forward_validation(args, base, end)
    results: list[tuple[str, ReplayResult, CircuitGuard | None]] = [("CONTROL", base, None)]
    for rule in _rules():
        for cooldown in (1, 2, 4, 6):
            guard = CircuitGuard(rule, cooldown, args.capital, notional)
            name = f"{rule.name}_{cooldown}H"
            replay = run_universe(name=name, lookback=0, config=config, signals=signals, execution_candles=candles["1m"], start_ms=start_ms, end_ms=end_ms, intrabar_path=args.intrabar_path.upper(), round_trip_spread_bps=spread, admission_guard=guard.allows)
            results.append((name, replay, guard))
    print("\narm | trades | open end | net $ | net/trade | wallet $ | max DD $/% | PF | crises | pause h | blocked circuit | blocked cap | max sim")
    for name, result, guard in results:
        row = _metrics(result, args.capital, notional)
        crisis = "-" if guard is None else str(guard.crises); pause = "-" if guard is None else f"{guard.paused_minutes/60:.2f}" 
        print(f"{name} | {row['trades']} | {len(result.open_positions)} | {row['net']:+.3f} | {row['net_trade']:+.3f}% | {row['wallet']:.3f} | {row['dd']:.3f}/{row['dd_pct']:.3f}% | {_pf(row['pf'])} | {crisis} | {pause} | {result.blocked_circuit} | {result.blocked_slots} | {result.max_simultaneous_positions}")
        if guard is not None: _path(name, base, result, notional)
    print("\nWEEKLY CONTRIBUTIONS (not independent replays)")
    for name, result, _ in results:
        print(name + " | " + " | ".join(f"{week}: ${sum(t.net_pct*notional/100 for t in values):+.3f}" for week, values in sorted(_weeks(result).items())))
    print("\nLIMITS: this is an in-sample comparative OHLC replay. Each arm has independent positions, slots, spacing and admission; exits use the unchanged runtime engine. No runtime/configuration/shadow state was modified.")


def _rules() -> tuple[Rule, ...]:
    return (Rule("DD_1P5", "DD", 1.5), Rule("PNL_2H_0P5", "PNL", 0.5, 2), Rule("COMBO_DD1P5_PNL4H0P5_MIN2", "COMBO", 1.5, 4, 2))


def _forward_validation(args: argparse.Namespace, control: ReplayResult, end: datetime) -> None:
    since = _ts(args.validation_since) or datetime(2026, 8, 19, 1, 5, tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
    until = _ts(args.validation_until) if args.validation_until else end
    if until is None or until <= since:
        raise SystemExit("Invalid forward-validation window.")
    records = TradeLedger(PROJECT_ROOT, Path(args.ledger)).load()
    observed = []
    for item in real_bot_b_records(records, "intraday"):
        opened = _ts(str(item.get("opened_at") or ""))
        if opened is None or not (since <= opened < until): continue
        if str(item.get("strategy_version") or "") != "b_atr_v1.4" or item.get("no_progress_enabled") is not False: continue
        observed.append(item)
    replayed = [x for x in control.entries() if int(since.timestamp()*1000) <= x.opened_ms < int(until.timestamp()*1000)]
    check = validate_replay(replayed, observed, MATCH_TOLERANCE_MS)
    print(f"FORWARD VALIDATION CONTROL | {_fmt(since)} -> {_fmt(until)} | observed={check.observed} | replay={check.replayed} | matched={check.matched}/{check.observed} ({check.match_rate:.1%}) | timing MAE={check.entry_time_abs_error_seconds if check.entry_time_abs_error_seconds is not None else 'n/a'}s | reason matches={check.reason_matches}/{check.matched} | fidelity={check.level}")
    if check.observed >= 5 and check.match_rate < 0.60:
        raise SystemExit("STOP: control entry fidelity is below 60%; do not interpret circuit-breaker arms.")


def _signals(config: dict[str, Any], candles: dict[str, list[Any]], start: int, end: int) -> list[Any]:
    warm = start - WARMUP_CANDLES * 15 * MINUTE_MS
    scoped = {key: [x for x in value if warm <= x.boundary_ms <= end] for key, value in candles.items()}
    signals, _ = generate_ge_signals(config, scoped["1m"], scoped["5m"], scoped["15m"], start, end, 0)
    return signals


def _metrics(result: ReplayResult, capital: float, notional: float) -> dict[str, float | int]:
    equity = peak = capital; dd = 0.; values=[]
    for trade in sorted(result.trades, key=lambda x: x.closed_ms):
        value=trade.net_pct; values.append(value); equity += notional*value/100; peak=max(peak,equity); dd=max(dd,peak-equity)
    gains=sum(x for x in values if x>0); losses=-sum(x for x in values if x<0)
    return {"trades":len(values),"net":equity-capital,"net_trade":sum(values)/len(values) if values else 0,"wallet":equity,"dd":dd,"dd_pct":dd/capital*100,"pf":gains/losses if losses else math.inf}


def _path(name: str, control: ReplayResult, arm: ReplayResult, notional: float) -> None:
    a={t.opened_ms:t for t in control.trades}; b={t.opened_ms:t for t in arm.trades}; ao={x.opened_ms for x in control.entries()}; bo={x.opened_ms for x in arm.entries()}
    print(f"  path {name}: common={len(ao&bo)} | control-only={len(ao-bo)} (${sum(a[x].net_pct*notional/100 for x in ao-bo if x in a):+.3f}) | arm-only={len(bo-ao)} (${sum(b[x].net_pct*notional/100 for x in bo-ao if x in b):+.3f})")


def _weeks(result: ReplayResult) -> dict[str,list[Any]]:
    out: dict[str,list[Any]]=defaultdict(list)
    for item in result.trades:
        dt=datetime.fromtimestamp(item.opened_ms/1000,timezone.utc); out[f"{dt.isocalendar().year}-W{dt.isocalendar().week:02d}"].append(item)
    return out


def _validate(config: dict[str,Any]) -> None:
    if str(config.get("entry",{}).get("timeframe"))!="1m" or str(config.get("trend",{}).get("timeframe"))!="15m": raise SystemExit("Requires current intraday REAL_A 1m/15m architecture.")
    gate=config.get("trend_gate",{}); 
    if str(gate.get("mode",""))!="ge30" or str(gate.get("candle_interval"))!="5m": raise SystemExit("Requires current closed-5m GE15 REAL_A gate.")


def _args()->argparse.Namespace:
    p=argparse.ArgumentParser(description="Read-only full-engine circuit-breaker replay.");p.add_argument("--since",required=True);p.add_argument("--until",required=True);p.add_argument("--config",default=str(PROJECT_ROOT/"config/config.yaml"));p.add_argument("--ledger",default=str(PROJECT_ROOT/"data/trades/trades_B.jsonl"));p.add_argument("--validation-since",default="2026-08-19T01:05:00-03:00");p.add_argument("--validation-until");p.add_argument("--capital",type=float,default=100.);p.add_argument("--cache-dir",default=str(PROJECT_ROOT/"data/studies/real_a_circuit_breaker/klines"));p.add_argument("--offline",action="store_true");p.add_argument("--market-data-url");p.add_argument("--http-timeout-seconds",type=int,default=15);p.add_argument("--round-trip-spread-bps",type=float);p.add_argument("--intrabar-path",choices=("high_first","low_first"),default="high_first");return p.parse_args()


def _ts(value:str)->datetime|None:
    for fmt in ("%d/%m/%Y %H:%M","%Y-%m-%d %H:%M"):
        try:return datetime.strptime(value,fmt).replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
        except ValueError:pass
    try:
        d=datetime.fromisoformat(value.replace("Z","+00:00"));return d.replace(tzinfo=d.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError:return None


def _fmt(value:datetime)->str:return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M BRT")
def _pf(value:float)->str:return "inf" if math.isinf(value) else f"{value:.3f}"
if __name__=="__main__":main()
