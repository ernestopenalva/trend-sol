"""Read-only forward report for the BE030 and BE-OFF ladder cohort."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger

ARMS = (
    ("REAL_A", "data/trades/trades_B.jsonl", "data/state/open_positions.json"),
    ("BE030_SHADOW", "data/trades/trades_be030_shadow.jsonl", "data/state/be030_shadow.json"),
    ("BE_OFF_SHADOW", "data/trades/trades_be_off_shadow.jsonl", "data/state/be_off_shadow.json"),
    ("BE_OFF_CB_SHADOW", "data/trades/trades_be_off_cb_shadow.jsonl", "data/state/be_off_cb_shadow.json"),
)
REASONS = ("HARD_STOP", "BREAKEVEN", "PROFIT_LOCK", "TRAILING")

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", required=True, help="BRT DD/MM/AAAA HH:MM or ISO timestamp")
    p.add_argument("--until")
    p.add_argument("--capital", type=float, default=100.0)
    p.add_argument("--audit-opened", help="BRT/ISO opening minute to audit across matching source candles.")
    a = p.parse_args(); since, until = _time(a.since), _time(a.until)
    print("TREND-SOL | REAL_A ladder forward cohort")
    print(f"Cohort opened_at: {a.since}" + (f" -> {a.until}" if a.until else ""))
    rows: dict[str, list[dict[str, Any]]] = {}
    for name, ledger, state_file in ARMS:
        rows[name] = _records(ROOT / ledger, since, until, real=name == "REAL_A")
    _normalized(rows, a.capital)
    print("\nOperational ledger/Testnet view (secondary; REAL_A uses exchange fills)")
    print("arm | closed | open | gross $/trade | net $/trade | realized max DD $ | PF | HS | BE | PL | TRAIL | avg/median age min | capacity | spacing | same-5m | avg/max simultaneous")
    for name, _ledger, state_file in ARMS:
        _line(name, rows[name], _state(ROOT / state_file), a.capital, real=name == "REAL_A")
    _transitions(rows["REAL_A"], rows["BE030_SHADOW"], "REAL_A -> BE030")
    _transitions(rows["REAL_A"], rows["BE_OFF_SHADOW"], "REAL_A -> BE_OFF")
    if a.audit_opened:
        _audit_opened(rows, _time(a.audit_opened))

def _records(path: Path, since: datetime | None, until: datetime | None, *, real: bool) -> list[dict[str, Any]]:
    records = TradeLedger(ROOT, path).load()
    if real: records = [x for x in records if x.get("position_type") == "BOT_EXIT" and not x.get("phantom") and not x.get("shadow_kind")]
    return [x for x in records if (t := _parse(x.get("opened_at"))) and (since is None or t >= since) and (until is None or t <= until)]

def _line(name: str, rows: list[dict[str, Any]], state: dict[str, Any], capital: float, *, real: bool) -> None:
    n=len(rows); gross=[_num(x.get("realized_pnl_abs")) for x in rows]; net=[_num(x.get("net_pnl_pct"))*_num(x.get("position_notional_usdt"))/100 for x in rows]
    gross=[x for x in gross if x is not None]; net=[x for x in net if x is not None]; reasons=Counter(str(x.get("exit_reason")) for x in rows)
    ages=[_num(x.get("age_seconds")) for x in rows]; ages=[x/60 for x in ages if x is not None]
    wins=sum(x for x in net if x>0); losses=-sum(x for x in net if x<0); bal=peak=capital; dd=0.
    for _, value in sorted(((_parse(x.get("closed_at")), value) for x,value in zip(rows,net)), key=lambda z:z[0] or datetime.min.replace(tzinfo=timezone.utc)):
        bal+=value; peak=max(peak,bal); dd=max(dd,peak-bal)
    opens=_opens(state, real); fields=("blocked_capacity","blocked_spacing","blocked_same_5m","max_simultaneous_positions")
    counters="-/-/-/-" if real else "/".join(str(state.get(x,0)) for x in fields)
    print(f"{name} | {n} | {opens} | ${sum(gross):+.4f}/${_mean(gross):+.4f} | ${sum(net):+.4f}/${_mean(net):+.4f} | ${dd:.4f} | {wins/losses if losses else float('inf'):.4f} | " + " | ".join(f"{reasons[r]} ({(reasons[r]/n*100 if n else 0):.1f}%)" for r in REASONS) + f" | {_mean(ages):.1f}/{median(ages) if ages else 0:.1f} | {counters}")

def _normalized(rows: dict[str, list[dict[str, Any]]], capital: float) -> None:
    """Primary economic view: signal entry and tick/trigger exit in every arm.

    REAL_A's Testnet quantity and fills are deliberately not reused. Each row is
    repriced at its recorded EntrySignal price and fixed recorded notional.
    Older REAL_A rows can use a same-source ladder shadow's signal price; rows
    lacking either source are reported as unavailable rather than estimated.
    """
    by_source = {
        int(row["source_candle_open_time"]): _num(row.get("signal_price")) or _num(row.get("entry_price"))
        for name, values in rows.items() if name != "REAL_A" for row in values
        if row.get("source_candle_open_time") is not None
    }
    print("Normalized phantom comparison (primary; homogeneous signal-entry / tick-exit convention)")
    print("arm | normalized closed | net $ | net/trade $ | realized max DD $ | PF")
    totals: dict[str, float] = {}
    for name, values in rows.items():
        events=[]
        for row in values:
            event=_normalized_event(row, name, by_source)
            if event is not None: events.append(event)
        if len(events) != len(values):
            print(f"{name} | unavailable ({len(events)}/{len(values)} exact signal prices) | — | — | — | —")
            continue
        net, dd, pf = _economic_summary(events, capital)
        totals[name] = net
        print(f"{name} | {len(events)} | ${net:+.4f} | ${net/len(events) if events else 0:+.4f} | ${dd:.4f} | {pf:.4f}")
    if "REAL_A" in totals:
        for name in ("BE030_SHADOW", "BE_OFF_SHADOW", "BE_OFF_CB_SHADOW"):
            if name in totals:
                print(f"{name} - REAL_A | ${totals[name] - totals['REAL_A']:+.4f}")
    real_by_source={row.get("source_candle_open_time"):row for row in rows["REAL_A"] if row.get("source_candle_open_time") is not None}
    print("Pairwise normalized comparison (primary where both arms opened the same source candle)")
    print("pair | common closed | REAL_A net $ / max DD $ | shadow net $ / max DD $ | shadow - REAL_A $")
    for name in ("BE030_SHADOW", "BE_OFF_SHADOW", "BE_OFF_CB_SHADOW"):
        shadow_by_source={row.get("source_candle_open_time"):row for row in rows[name] if row.get("source_candle_open_time") is not None}
        common=sorted(set(real_by_source) & set(shadow_by_source))
        real_events=[event for source in common if (event:=_normalized_event(real_by_source[source], "REAL_A", by_source)) is not None]
        shadow_events=[event for source in common if (event:=_normalized_event(shadow_by_source[source], name, by_source)) is not None]
        if len(real_events) != len(common) or len(shadow_events) != len(common):
            print(f"REAL_A x {name} | unavailable ({len(real_events)}/{len(common)} exact pairs) | — | — | —")
            continue
        real_net, real_dd, _ = _economic_summary(real_events, capital)
        shadow_net, shadow_dd, _ = _economic_summary(shadow_events, capital)
        print(f"REAL_A x {name} | {len(common)} | ${real_net:+.4f} / ${real_dd:.4f} | ${shadow_net:+.4f} / ${shadow_dd:.4f} | ${shadow_net-real_net:+.4f}")

def _normalized_event(row: dict[str, Any], name: str, by_source: dict[int, float | None]) -> tuple[datetime, float] | None:
    source=row.get("source_candle_open_time")
    entry=_num(row.get("signal_price"))
    if entry is None:
        if name == "REAL_A" and source is not None:
            entry=by_source.get(int(source))
        elif name != "REAL_A":
            # Historical phantom ledgers predate signal_price.  Their recorded
            # entry_price is precisely the phantom EntrySignal price.
            entry=_num(row.get("entry_price"))
    exit_price=_num(row.get("exit_trigger_price")) if name == "REAL_A" else _num(row.get("exit_price"))
    notional,fee,closed=_num(row.get("position_notional_usdt")),_num(row.get("estimated_fees_pct")),_parse(row.get("closed_at"))
    if None in (entry,exit_price,notional,fee,closed): return None
    return closed, (exit_price-entry)*(notional/entry)-notional*fee/100

def _economic_summary(events: list[tuple[datetime, float]], capital: float) -> tuple[float, float, float]:
    balance=peak=capital;dd=0.;wins=losses=0.
    for _,net in sorted(events):
        balance+=net;peak=max(peak,balance);dd=max(dd,peak-balance)
        wins+=max(net,0);losses+=max(-net,0)
    return balance-capital, dd, wins/losses if losses else float("inf")

def _audit_opened(rows: dict[str, list[dict[str, Any]]], opened: datetime | None) -> None:
    if opened is None: return
    target=opened.replace(second=0,microsecond=0)
    real=[row for row in rows["REAL_A"] if (_parse(row.get("opened_at")) or target).replace(second=0,microsecond=0)==target]
    if not real:
        print(f"\nEntry audit: no REAL_A trade opened at {_fmt_brt(target)}")
        return
    source=real[0].get("source_candle_open_time")
    shadow_signal = next(
        (
            _num(row.get("signal_price")) or _num(row.get("entry_price"))
            for name, values in rows.items() if name != "REAL_A"
            for row in values if row.get("source_candle_open_time") == source
        ),
        None,
    )
    print(f"\nEntry audit | REAL_A opened {_fmt_brt(target)} | source candle={source}")
    print("arm | source candle BRT | admission/opened_at | signal price | recorded entry | entry source | delta from signal")
    for name, values in rows.items():
        for row in values:
            if row.get("source_candle_open_time") != source: continue
            entry=_num(row.get("entry_price"))
            signal=_num(row.get("signal_price"))
            if signal is None:
                signal=shadow_signal if name == "REAL_A" else entry
            delta=(entry-signal) if entry is not None and signal is not None else None
            entry_source="Testnet market fill" if name == "REAL_A" else "phantom signal"
            print(f"{name} | {_fmt_ms(source)} | {row.get('opened_at')} | {signal if signal is not None else 'unavailable'} | {entry} | {entry_source} | {delta if delta is not None else 'n/a'}")

def _transitions(base: list[dict[str, Any]], arm: list[dict[str, Any]], title: str) -> None:
    def key(x: dict[str,Any]): return x.get("source_candle_open_time")
    a={key(x):x for x in base if key(x) is not None}; b={key(x):x for x in arm if key(x) is not None}; common=set(a)&set(b)
    c=Counter((str(a[k].get("exit_reason")),str(b[k].get("exit_reason"))) for k in common)
    print(f"\n{title}: common closed={len(common)} | REAL_A-only={len(set(a)-common)} | shadow-only={len(set(b)-common)}")
    if "BE030" in title:
        pairs=(("BREAKEVEN","HARD_STOP"),("TRAILING","BREAKEVEN"),("BREAKEVEN","TRAILING"),("TRAILING","TRAILING"))
    else:
        pairs=(("BREAKEVEN","HARD_STOP"),("BREAKEVEN","PROFIT_LOCK"),("BREAKEVEN","TRAILING"),("HARD_STOP","HARD_STOP"),("TRAILING","TRAILING"))
    print(" | ".join(f"{x}->{y}={c[(x,y)]}" for x,y in pairs))

def _state(path: Path) -> dict[str,Any]:
    try:
        x=json.loads(path.read_text(encoding="utf8")); return x if isinstance(x,dict) else {}
    except (OSError,ValueError): return {}
def _opens(state:dict[str,Any], real:bool)->int:
    raw=state if isinstance(state,list) else state.get("positions",[])
    return sum(x.get("status")=="OPEN" and (not real or (x.get("label")=="B" and not x.get("phantom"))) for x in raw)
def _parse(v:Any)->datetime|None:
    try: return datetime.fromisoformat(str(v).replace("Z","+00:00")).replace(tzinfo=datetime.fromisoformat(str(v).replace("Z","+00:00")).tzinfo or timezone.utc).astimezone(timezone.utc)
    except (TypeError,ValueError): return None
def _time(v:str|None)->datetime|None:
    if not v:return None
    try:return datetime.strptime(v,"%d/%m/%Y %H:%M").replace(tzinfo=BRASILIA_TZ).astimezone(timezone.utc)
    except ValueError:
        x=_parse(v)
        if not x:raise SystemExit(f"Invalid timestamp: {v}")
        return x
def _num(v:Any)->float|None:
    try:return float(v)
    except (TypeError,ValueError):return None
def _mean(v:list[float])->float:return sum(v)/len(v) if v else 0.
def _fmt_brt(value:datetime)->str:return value.astimezone(BRASILIA_TZ).strftime("%d/%m/%Y %H:%M:%S BRT")
def _fmt_ms(value:Any)->str:
    try:return _fmt_brt(datetime.fromtimestamp(int(value)/1000,timezone.utc))
    except (TypeError,ValueError):return "unavailable"
if __name__=="__main__":main()
