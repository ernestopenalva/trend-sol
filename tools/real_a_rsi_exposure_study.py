"""Read-only RSI / RSI-MA exposure study over historical REAL_A ledger trades."""
from __future__ import annotations

import argparse
import bisect
import csv
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.indicators.indicators import rsi
from src.trade_ledger import TradeLedger
from tools.cohort_study import _load_config
from tools.dmi15_rsi_ma_study import sma_optional
from tools.ge_replay_study import load_ge_market_data
from tools.market_context_report import _parse_ts, _parse_user_dt
from tools.market_selection_study import BinancePublicClient, MarketCandle

RSI_PERIOD = SMA_PERIOD = 14
WARMUP_MS = 30 * 24 * 60 * 60 * 1000
KNOWN_NEVER_PROTECTED = {"HARD_STOP", "REVIEW_STOP", "NO_PROGRESS_EXIT"}
BUCKETS = (("<60", -math.inf, 60), ("60-65", 60, 65), ("65-70", 65, 70), ("70-75", 70, 75), ("75-80", 75, 80), (">=80", 80, math.inf))


@dataclass
class Trade:
    row: dict[str, Any]
    opened: datetime
    closed: datetime
    entry: float
    exit: float
    gross: float
    net: float
    rsi: float | None = None
    rsi_ma: float | None = None
    open_count: int | None = None
    unprotected_count: int | None = None


def main() -> None:
    args = _args(); since = _parse_user_dt(args.since)
    if since is None: raise SystemExit("--since is required")
    until = _parse_user_dt(args.until) if args.until else None
    trades = _load_trades(Path(args.ledger), since, until)
    if not trades: raise SystemExit("No closed REAL_A trades matched the window.")
    raw = _load_config(Path(args.config)); symbol = str(args.symbol or raw.get("symbol") or "SOLUSDT")
    first = int(min(item.opened for item in trades).timestamp() * 1000)
    last = int(max(item.opened for item in trades).timestamp() * 1000)
    base_url = str(args.market_data_url or raw.get("market_data", {}).get("rest_url") or "https://api.binance.com")
    candles = load_ge_market_data(BinancePublicClient(base_url, int(args.http_timeout_seconds)), symbol, "5m", first - WARMUP_MS, last + 5 * 60 * 1000, Path(args.cache_dir), bool(args.offline))
    _attach_indicators(trades, candles); _attach_exposure(trades)
    _print_header(trades, candles, since, until)
    _print_continuous(trades); _print_buckets(trades); _print_repricing(trades, args.capital); _print_weeks(trades); _print_blocks(trades, since); _print_exposure(trades)
    _write_csv(args.output, trades); print(f"\ndetailed CSV: {args.output}")


def _load_trades(path: Path, since: datetime, until: datetime | None) -> list[Trade]:
    result=[]
    for row in TradeLedger(PROJECT_ROOT, path).load():
        if row.get("phantom") or row.get("shadow_kind") or str(row.get("position_type")) != "BOT_EXIT": continue
        opened, closed = _parse_ts(row.get("opened_at")), _parse_ts(row.get("closed_at"))
        try: entry, exit_price = float(row["entry_price"]), float(row["exit_price"])
        except (KeyError, TypeError, ValueError): continue
        if not opened or not closed or opened < since or (until is not None and opened >= until): continue
        gross = _number(row.get("gross_pnl_pct"), (exit_price-entry)/entry*100); net = _number(row.get("net_pnl_pct"), gross-_number(row.get("estimated_fees_pct"), .2))
        result.append(Trade(row, opened, closed, entry, exit_price, gross, net))
    return sorted(result, key=lambda x: (x.opened, x.closed, str(x.row.get("pair_id"))))


def _attach_indicators(trades: list[Trade], candles: list[MarketCandle]) -> None:
    closes=[item.close for item in candles]; rsi_values=rsi(closes, RSI_PERIOD); ma_values=sma_optional(rsi_values, SMA_PERIOD); close_times=[item.close_time_ms for item in candles]
    for trade in trades:
        # Last 5m candle *closed* at entry; never the current/in-progress bar.
        index=bisect.bisect_right(close_times, int(trade.opened.timestamp()*1000))-1
        if index >= 0:
            trade.rsi = rsi_values[index]; trade.rsi_ma = ma_values[index]


def _attach_exposure(trades: list[Trade]) -> None:
    for target in trades:
        open_before=[item for item in trades if item.opened < target.opened and item.closed > target.opened]
        target.open_count=len(open_before)
        unknown=False; unprotected=0
        for item in open_before:
            armed=_parse_ts(item.row.get("be_armed_at")); reason=str(item.row.get("exit_reason") or "")
            if armed is not None:
                unprotected += armed > target.opened
            elif reason in KNOWN_NEVER_PROTECTED:
                unprotected += 1
            else:
                unknown=True
        target.unprotected_count=None if unknown else unprotected


def _print_header(trades: list[Trade], candles: list[MarketCandle], since: datetime, until: datetime | None) -> None:
    available=sum(item.rsi is not None and item.rsi_ma is not None for item in trades)
    print("TREND-SOL | REAL_A RSI / RSI-MA entry study | READ-ONLY")
    print(f"opened_at: {_fmt(since)} -> {_fmt(until) if until else _fmt(max(x.opened for x in trades))}")
    print(f"trades={len(trades)} | indicators resolved={available} | unavailable={len(trades)-available}")
    print(f"source: continuous 5m cache | RSI Wilder-14(close), RSI-MA=SMA-14(RSI) | cache candles={len(candles)}")
    print("Entry mapping: last 5m candle whose close_time <= opened_at; unavailable is never converted into a numeric value or block.")


def _print_continuous(trades: list[Trade]) -> None:
    print("\nCONTINUOUS DESCRIPTIVE (correlation is not causality)")
    for field, label in (("rsi", "RSI14"), ("rsi_ma", "RSI-MA14")):
        rows=[item for item in trades if getattr(item, field) is not None]; xs=[float(getattr(item, field)) for item in rows]
        print(f"{label} | N={len(xs)} | corr gross={_corr(xs,[x.gross for x in rows]):+.3f} | corr net={_corr(xs,[x.net for x in rows]):+.3f}")
        quantiles=_quantile_groups(rows, field)
        print("  quantile | N | mean net | median net")
        for name, group in quantiles: print(f"  {name} | {len(group)} | {_mean([x.net for x in group]):+.3f}% | {_median([x.net for x in group]):+.3f}%")


def _print_buckets(trades: list[Trade]) -> None:
    for field,label in (("rsi","RSI14"),("rsi_ma","RSI-MA14")):
        print(f"\nFIXED BANDS — {label}")
        print("band | N | gross total/trade | net total/trade | PF | win | HS/BE/PL/TRAIL | mean/median/p25/p75 net | best/worst | $ PnL @20")
        for name, low, high in BUCKETS:
            rows=[x for x in trades if getattr(x,field) is not None and low <= float(getattr(x,field)) < high]
            m=_metrics(rows,20); r=m['reasons']
            print(f"{name} | {m['n']} | {m['gross']:+.2f}%/{m['gross_avg']:+.3f}% | {m['net']:+.2f}%/{m['net_avg']:+.3f}% | {_pf(m['pf'])} | {m['win']:.1f}% | {r['HARD_STOP']}/{r['BREAKEVEN']}/{r['PROFIT_LOCK']}/{r['TRAILING']} | {m['mean']:+.3f}/{m['median']:+.3f}/{m['p25']:+.3f}/{m['p75']:+.3f}% | {m['best']:+.2f}/{m['worst']:+.2f}% | ${m['dollars']:+.3f}")


def _print_repricing(trades: list[Trade], capital: float) -> None:
    print("\nEXPOSURE REPRICING — historical ledger only; does not replay admissions, slots, or later entries")
    for field,label in (("rsi","RSI14"),("rsi_ma","RSI-MA14")):
        print(f"\n{label} | threshold | arm | net $ | return $100 | realized DD $/% | avg committed $ | affected N/PnL | unaffected PnL")
        for threshold in (65,70,75,80):
            affected=[x for x in trades if getattr(x,field) is not None and float(getattr(x,field)) > threshold]
            for arm, factor in (("BASE",1.0),("SKIP",0.0),("HALF_SIZE",.5),("ONE_THIRD_SIZE",1/3)):
                value=_portfolio(trades, capital, affected, factor)
                affected_pnl=sum(20*factor*x.net/100 for x in affected); unaffected=sum(20*x.net/100 for x in trades if x not in affected)
                print(f"{label} | >{threshold} | {arm} | ${value['net']:+.3f} | {value['return']:+.3f}% | ${value['dd']:.3f}/{value['dd_pct']:.3f}% | ${value['avg_commit']:.2f} | {len(affected)}/${affected_pnl:+.3f} | ${unaffected:+.3f}")


def _print_weeks(trades: list[Trade]) -> None:
    print("\nISO WEEKS — net/trade by RSI-MA fixed band (descriptive; no threshold selection)")
    weeks=defaultdict(list)
    for item in trades: weeks[_week(item.opened)].append(item)
    for week, rows in sorted(weeks.items()):
        bits=[]
        for name,lo,hi in BUCKETS:
            selected=[x.net for x in rows if x.rsi_ma is not None and lo <= float(x.rsi_ma) < hi]
            if selected: bits.append(f"{name}:N={len(selected)},net/t={_mean(selected):+.3f}%")
        print(f"{week} | " + " | ".join(bits))


def _print_blocks(trades: list[Trade], since: datetime) -> None:
    """Fixed blocks are declared from the requested start, never selected by outcome."""
    blocks=defaultdict(list)
    for item in trades:
        index=max(0, int((item.opened-since).total_seconds() // (7*24*3600))); blocks[index].append(item)
    print("\nFIXED 7-DAY BLOCKS FROM --since — RSI-MA net/trade")
    for index, rows in sorted(blocks.items()):
        start=since+timedelta(days=7*index); end=start+timedelta(days=7)
        values=[x.net for x in rows if x.rsi_ma is not None]
        print(f"{_fmt(start)} -> {_fmt(end)} | N={len(values)} | net/trade={_mean(values):+.3f}%")


def _print_exposure(trades: list[Trade]) -> None:
    print("\nRSI-MA × UNPROTECTED POSITIONS (unavailable exposure is excluded, never assumed zero)")
    table=defaultdict(list)
    for item in trades:
        if item.rsi_ma is None or item.unprotected_count is None: continue
        band=next(name for name,lo,hi in BUCKETS if lo <= float(item.rsi_ma) < hi); count="3+" if item.unprotected_count>=3 else str(item.unprotected_count); table[(band,count)].append(item)
    for band,_,_ in BUCKETS:
        bits=[]
        for count in ("0","1","2","3+"):
            rows=table[(band,count)]
            if rows: bits.append(f"N{count}: N={len(rows)}, net/t={_mean([x.net for x in rows]):+.3f}%, HS={sum(x.row.get('exit_reason')=='HARD_STOP' for x in rows)/len(rows)*100:.1f}%")
        print(f"{band} | " + (" | ".join(bits) or "no resolved observations"))


def _metrics(rows: list[Trade], notional: float) -> dict[str,Any]:
    net=[x.net for x in rows]; gross=[x.gross for x in rows]; gains=sum(x for x in gross if x>0); losses=-sum(x for x in gross if x<0)
    return {'n':len(rows),'gross':sum(gross),'net':sum(net),'gross_avg':_mean(gross),'net_avg':_mean(net),'pf':gains/losses if losses else math.inf,'win':sum(x>0 for x in net)/len(net)*100 if net else 0,'reasons':Counter(str(x.row.get('exit_reason') or '') for x in rows),'mean':_mean(net),'median':_median(net),'p25':_percentile(net,.25),'p75':_percentile(net,.75),'best':max(net,default=0),'worst':min(net,default=0),'dollars':sum(notional*x/100 for x in net)}


def _portfolio(trades:list[Trade],capital:float,affected:list[Trade],factor:float)->dict[str,float]:
    ids={id(x) for x in affected}; balance=peak=capital; dd=0.; points=sorted({min(x.opened for x in trades),max(x.closed for x in trades),*(x.opened for x in trades),*(x.closed for x in trades)}); area=0.
    for item in sorted(trades,key=lambda x:x.closed): balance+=20*(factor if id(item) in ids else 1)*item.net/100; peak=max(peak,balance); dd=max(dd,peak-balance)
    for a,b in zip(points,points[1:]): area+=sum(20*(factor if id(x) in ids else 1) for x in trades if x.opened<=a<x.closed)*(b-a).total_seconds()
    duration=max((points[-1]-points[0]).total_seconds(),1); return {'net':balance-capital,'return':(balance-capital)/capital*100,'dd':dd,'dd_pct':dd/capital*100,'avg_commit':area/duration}


def _quantile_groups(rows:list[Trade],field:str)->list[tuple[str,list[Trade]]]:
    ordered=sorted(rows,key=lambda x:float(getattr(x,field))); n=len(ordered); return [(f"Q{i+1}",ordered[i*n//4:(i+1)*n//4]) for i in range(4)] if n else []
def _corr(xs:list[float],ys:list[float])->float:
    if len(xs) < 2:
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    denominator = math.sqrt(sum((x-mx)**2 for x in xs) * sum((y-my)**2 for y in ys))
    return sum((x-mx)*(y-my) for x,y in zip(xs,ys)) / denominator if denominator else 0.0
def _number(v:Any,default:float)->float:
    try:return float(v)
    except (TypeError,ValueError):return default
def _mean(xs:list[float])->float:return statistics.fmean(xs) if xs else 0.
def _median(xs:list[float])->float:return statistics.median(xs) if xs else 0.
def _percentile(xs:list[float],q:float)->float:
    if not xs:return 0.
    s=sorted(xs); i=(len(s)-1)*q; lo,hi=int(i),math.ceil(i); return s[lo]+(s[hi]-s[lo])*(i-lo)
def _pf(x:float)->str:return 'inf' if math.isinf(x) else f'{x:.2f}'
def _fmt(x:datetime|None)->str:return x.astimezone(BRASILIA_TZ).strftime('%d/%m/%Y %H:%M BRT') if x else 'now'
def _week(x:datetime)->str:
    i=x.isocalendar();return f'{i.year}-W{i.week:02d}'
def _write_csv(path:Path,trades:list[Trade])->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f);w.writerow(('trade_id','opened_at_brt','entry_price','exit_price','exit_reason','gross_pct','net_pct','rsi14','rsi_ma14','n_open_positions_at_entry','n_unprotected_positions_at_entry'))
        for x in trades:w.writerow((x.row.get('pair_id'),_fmt(x.opened),x.entry,x.exit,x.row.get('exit_reason'),x.gross,x.net,x.rsi if x.rsi is not None else 'unavailable',x.rsi_ma if x.rsi_ma is not None else 'unavailable',x.open_count,x.unprotected_count if x.unprotected_count is not None else 'unavailable'))
def _args()->argparse.Namespace:
    p=argparse.ArgumentParser(description='Read-only RSI / RSI-MA historical exposure study for REAL_A');p.add_argument('--since',required=True);p.add_argument('--until');p.add_argument('--ledger',default=str(PROJECT_ROOT/'data/trades/trades_B.jsonl'));p.add_argument('--config',default=str(PROJECT_ROOT/'config/config.yaml'));p.add_argument('--symbol');p.add_argument('--market-data-url');p.add_argument('--cache-dir',default=str(PROJECT_ROOT/'data/studies/real_a_rsi_exposure/klines'));p.add_argument('--offline',action='store_true');p.add_argument('--http-timeout-seconds',type=int,default=15);p.add_argument('--capital',type=float,default=100.);p.add_argument('--output',type=Path,default=PROJECT_ROOT/'data/analysis/real_a_rsi_exposure_details.csv');return p.parse_args()
if __name__=='__main__':main()
