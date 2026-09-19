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
    a = p.parse_args(); since, until = _time(a.since), _time(a.until)
    print("TREND-SOL | REAL_A ladder forward cohort")
    print(f"Cohort opened_at: {a.since}" + (f" -> {a.until}" if a.until else ""))
    print("arm | closed | open | gross $/trade | net $/trade | realized max DD $ | PF | HS | BE | PL | TRAIL | avg/median age min | capacity | spacing | same-5m | avg/max simultaneous")
    rows: dict[str, list[dict[str, Any]]] = {}
    for name, ledger, state_file in ARMS:
        rows[name] = _records(ROOT / ledger, since, until, real=name == "REAL_A")
        _line(name, rows[name], _state(ROOT / state_file), a.capital, real=name == "REAL_A")
    _transitions(rows["REAL_A"], rows["BE030_SHADOW"], "REAL_A -> BE030")
    _transitions(rows["REAL_A"], rows["BE_OFF_SHADOW"], "REAL_A -> BE_OFF")

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
if __name__=="__main__":main()
