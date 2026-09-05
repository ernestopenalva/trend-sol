"""Read-only forward report for REAL_A versus the frozen circuit-breaker shadow."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path: sys.path.insert(0, str(PROJECT_ROOT))

from src.console_utils import BRASILIA_TZ
from src.trade_ledger import TradeLedger
from tools.trades_report import _parse_since


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward report: REAL_A vs REAL_A_CB_SHADOW.")
    parser.add_argument("--since", required=True, help="BRT DD/MM/AAAA HH:MM or ISO timestamp.")
    parser.add_argument("--capital", type=float, default=100.0)
    args = parser.parse_args(); since = _parse_since(args.since)
    if since is None or args.capital <= 0: raise SystemExit("--since and positive --capital are required")
    real = [x for x in TradeLedger(PROJECT_ROOT).load() if _real(x) and _opened_after(x, since)]
    cb = [x for x in TradeLedger(PROJECT_ROOT, PROJECT_ROOT / "data/trades/trades_circuit_breaker_shadow.jsonl").load() if _opened_after(x, since)]
    state = _state(PROJECT_ROOT / "data/state/circuit_breaker_shadow.json")
    events = _events(PROJECT_ROOT / "data/telemetry/circuit_breaker_shadow_events.jsonl", since)
    pending = _state(PROJECT_ROOT / "data/state/circuit_breaker_shadow.json.pending")
    print("TREND-SOL | REAL_A vs REAL_A_CB_SHADOW | FORWARD ONLY")
    print(f"Cohort by opened_at: {_fmt(since)} -> now | initial capital each: ${args.capital:.2f}")
    print("Frozen rule: COMBO_DD1P5_PNL4H0P5_MIN2 + 6h; telemetry market context never participates in admission.")
    pending_count = len(state.get('pending_closes', []))
    issue = _integrity_issue(state, pending)
    print(f"Integrity: {issue or 'checkpoint committed'} | closes awaiting next minute={pending_count}")
    if issue:
        print('DO NOT INTERPRET economic comparison until this integrity issue is reconciled.')
    print("arm | closed | open now | net closed $ | realized balance $ | return % | realized max DD $/% | PF | avg/max simultaneous")
    for name, rows, opens in (("REAL_A", real, _real_opens(since)), ("REAL_A_CB_SHADOW", cb, _cb_opens(state, since))):
        row = _metrics(rows, opens, args.capital, since)
        print(f"{name} | {row['closed']} | {row['open']} | ${row['net']:+.4f} | ${row['balance']:.4f} | {row['return']:+.4f}% | ${row['dd']:.4f}/{row['dd_pct']:.4f}% | {row['pf']} | {row['avg_sim']:.3f}/{row['max_sim']}")
    real_keys, cb_keys = {_key(x) for x in real if _key(x) is not None}, {_key(x) for x in cb if _key(x) is not None}
    real_only = [x for x in real if _key(x) in real_keys - cb_keys]; cb_only = [x for x in cb if _key(x) in cb_keys - real_keys]
    print("\nTrade/path overlap (source candle; closed trades):")
    print(f"common={len(real_keys & cb_keys)} | REAL_A only={len(real_keys-cb_keys)} (${sum(_net(x) for x in real_only):+.4f}) | CB only={len(cb_keys-real_keys)} (${sum(_net(x) for x in cb_only):+.4f})")
    all_real = [*real, *_real_opens(since)]
    all_cb = [*cb, *_cb_opens(state,since)]
    paths = _path_attribution(all_real,all_cb,events)
    print('\nAdmission/path overlap (includes still-open positions; not closed-only):')
    for label, count in sorted(paths.items()):
        print(f'{label}={count}')
    triggers = [x for x in events if x.get("event") == "CIRCUIT_BREAKER_TRIGGERED"]
    blocks = [x for x in events if x.get("event") == "ENTRY_BLOCKED_CIRCUIT_BREAKER"]
    print(f"\nCircuit breaker: crises accumulated={len(triggers)} | signals blocked={len(blocks)} | currently paused={'yes' if state.get('circuit_breaker_active') else 'no'} | pause until={_fmt_ts(state.get('circuit_breaker_until'))}")
    _market_context(state, triggers, events)
    _reopen_diagnostics(triggers, events, real, cb)
    print("\nForward criterion (frozen before the run): FAVORABLE only if CB has higher final net AND lower realized max DD, without material PF deterioration; DESFAVORABLE if persistent lower net, equal/higher DD, or missed-trade cost clearly exceeds avoided damage; INCONCLUSIVE with few crises. No event count is statistical validation.")


def _metrics(rows: list[dict[str, Any]], opens: list[dict[str, Any]], capital: float, since: datetime) -> dict[str, Any]:
    equity = peak = capital; dd = 0.; values=[]
    for item in sorted(rows, key=lambda x: str(x.get("closed_at", ""))):
        pnl=_net(item); values.append(pnl); equity += pnl; peak=max(peak,equity); dd=max(dd,peak-equity)
    intervals=[]
    for item in [*rows,*opens]:
        start=_ts(item.get("opened_at") or item.get("open_ts")); end=_ts(item.get("closed_at")) or datetime.now(timezone.utc)
        if start and end > start: intervals.append((max(start,since),end))
    points=sorted({x for interval in intervals for x in interval} | {since,datetime.now(timezone.utc)})
    weighted=0.; maximum=0
    for left,right in zip(points,points[1:]):
        active=sum(1 for start,end in intervals if start <= left < end); weighted += active*(right-left).total_seconds(); maximum=max(maximum,active)
    gains=sum(x for x in values if x>0); losses=-sum(x for x in values if x<0)
    return {"closed":len(rows),"open":len(opens),"net":equity-capital,"balance":equity,"return":(equity-capital)/capital*100,"dd":dd,"dd_pct":dd/capital*100,"pf":"inf" if not losses and gains else ("n/a" if not losses else f"{gains/losses:.3f}"),"avg_sim":weighted/max((datetime.now(timezone.utc)-since).total_seconds(),1),"max_sim":maximum}

def _market_context(state: dict[str,Any], triggers: list[dict[str,Any]], events:list[dict[str,Any]]) -> None:
    start, price = state.get("cohort_started_at"), _number(state.get("cohort_start_price")); points=state.get("market_points",[])
    last = _number(points[-1][1]) if isinstance(points,list) and points else None
    change=(last/price-1)*100 if price and last else None
    print(f"\nSOL cohort telemetry: start={_fmt_ts(start)} @ {_fmt_price(price)} | latest={_fmt_price(last)} | change={_fmt_pct(change)}")
    if triggers:
        print("trigger | price | SOL prior 1h | prior 4h | prior 12h | release | SOL cooldown 6h")
        releases={str(x.get('trigger_time')):x for x in events if x.get('event')=='CIRCUIT_BREAKER_RELEASED'}
        for item in triggers:
            release=releases.get(str(item.get('trigger_time')), {})
            print(f"{_fmt_ts(item.get('trigger_time'))} | {_fmt_price(item.get('price'))} | {_fmt_pct(item.get('sol_return_1h_pct'))} | {_fmt_pct(item.get('sol_return_4h_pct'))} | {_fmt_pct(item.get('sol_return_12h_pct'))} | {_fmt_ts(release.get('release_time'))} | {_fmt_pct(release.get('sol_return_cooldown_pct'))}")

def _reopen_diagnostics(triggers:list[dict[str,Any]], events:list[dict[str,Any]], real:list[dict[str,Any]], cb:list[dict[str,Any]]) -> None:
    if not triggers: return
    real_by_key={_key(x):x for x in real if _key(x) is not None}; cb_keys={_key(x) for x in cb if _key(x) is not None}
    releases={str(x.get('trigger_time')):x for x in events if x.get('event')=='CIRCUIT_BREAKER_RELEASED'}
    total_pause=0.
    print("\nReopening diagnostics (path explanation only; not a decision criterion):")
    triggers=sorted(triggers,key=lambda x:str(x.get('trigger_time','')))
    for index,item in enumerate(triggers):
        start=_ts(item.get('trigger_time')); release=releases.get(str(item.get('trigger_time')), {}); end=_ts(release.get('release_time'))
        next_trigger=_ts(triggers[index+1].get('trigger_time')) if index+1<len(triggers) else None
        block_end=min(x for x in (end,next_trigger) if x is not None) if end or next_trigger else None
        blocked=[x for x in events if x.get('event')=='ENTRY_BLOCKED_CIRCUIT_BREAKER' and start and _ts(x.get('ts')) and _ts(x.get('ts'))>=start and (not block_end or _ts(x.get('ts'))<block_end)]
        blocked_keys={_key(x) for x in blocked if _key(x) is not None}; blocked_real=[real_by_key[key] for key in blocked_keys if key in real_by_key]
        replacements=sorted([x for x in cb if end and _ts(x.get('opened_at')) and _ts(x.get('opened_at'))>=end and (not next_trigger or _ts(x.get('opened_at'))<next_trigger)],key=lambda x:str(x.get('opened_at','')))
        first=replacements[:3]; cb_only=[x for x in replacements if _key(x) not in { _key(x) for x in real }]
        print(f"trigger={_fmt_ts(item.get('trigger_time'))} | release={_fmt_ts(release.get('release_time'))} | blocked={len(blocked)} | matched REAL_A blocked net=${sum(_net(x) for x in blocked_real):+.4f} | first CB after release={','.join(_fmt_ts(x.get('opened_at')) for x in first) or 'none'} | CB-only in this post-release interval={len(cb_only)} (${sum(_net(x) for x in cb_only):+.4f}); temporal association, not proven causal replacements")
    active_start=None
    for event in sorted(events,key=lambda x:str(x.get('ts',''))):
        moment=_ts(event.get('ts'))
        if event.get('event')=='CIRCUIT_BREAKER_TRIGGERED' and active_start is None:
            active_start=moment
        elif event.get('event')=='CIRCUIT_BREAKER_RELEASED' and active_start and moment:
            total_pause += (moment-active_start).total_seconds()/3600
            active_start=None
    print(f"total completed pause hours={total_pause:.2f}")


def _integrity_issue(state, pending):
    if state.get('cb_schema') != 2:
        return 'legacy/missing checkpoint; engineering equivalence not established'
    if int(pending.get('sequence',0)) > int(state.get('sequence',0)):
        return 'input being committed or interrupted; retry, investigate if persistent'
    expected = float(state.get('clock',{}).get('equity',0)) + sum(float(x['net']) for x in state.get('pending_closes',[]))
    capital = float(state.get('clock',{}).get('capital',100))
    ledger_equity = capital + sum(_net(x) for x in state.get('closed_records',[]))
    if abs(expected-ledger_equity)>1e-9:
        return 'ledger and detector equity mismatch'
    return None


def _path_attribution(real, cb, events):
    from collections import Counter
    real_keys={_key(x) for x in real if _key(x) is not None}
    cb_keys={_key(x) for x in cb if _key(x) is not None}
    decisions={_key(x):x.get('event') for x in events if str(x.get('event','')).startswith('ENTRY_BLOCKED_')}
    outcomes={_key(x):x.get('outcome') for x in events if x.get('event')=='REAL_A_ADMISSION_OBSERVED'}
    result=Counter(common=len(real_keys&cb_keys))
    for key in real_keys-cb_keys:
        reason=decisions.get(key)
        result['REAL_A-only/'+(reason or 'UNEXPLAINED_CHECK_INTEGRATION')] += 1
    for key in cb_keys-real_keys:
        outcome=outcomes.get(key)
        reason={'blocked':'real admission declined (independent path)',
                'order_rejected':'real execution rejected',
                'opened':'REAL_A ledger/state missing despite successful admission'}.get(outcome,'UNEXPLAINED_CHECK_INTEGRATION')
        result['CB-only/'+reason] += 1
    return dict(result)

def _events(path:Path,since:datetime)->list[dict[str,Any]]:
    if not path.exists(): return []
    out=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        try: row=json.loads(line)
        except json.JSONDecodeError: continue
        if _ts(row.get('ts')) and _ts(row.get('ts')) >= since: out.append(row)
    return out
def _state(path:Path)->dict[str,Any]:
    try: value=json.loads(path.read_text(encoding='utf-8')); return value if isinstance(value,dict) else {}
    except (OSError,json.JSONDecodeError): return {}
def _real(x:dict[str,Any])->bool:return not x.get('phantom') and not x.get('shadow_kind') and x.get('position_type')=='BOT_EXIT'
def _opened_after(x:dict[str,Any], since:datetime)->bool:
    opened = _ts(x.get('opened_at') or x.get('open_ts'))
    return bool(opened and opened >= since)
def _real_opens(since:datetime)->list[dict[str,Any]]:
    rows = _rows(PROJECT_ROOT/'data/state/open_positions.json')
    return [x for x in rows if x.get('status')=='OPEN' and x.get('label')=='B' and not x.get('phantom') and _opened_after(x,since)]

def _rows(path:Path)->list[dict[str,Any]]:
    try: value=json.loads(path.read_text(encoding='utf-8'))
    except (OSError,json.JSONDecodeError): return []
    return [item for item in value if isinstance(item,dict)] if isinstance(value,list) else []
def _cb_opens(state:dict[str,Any], since:datetime)->list[dict[str,Any]]: return [x for x in state.get('positions',[]) if x.get('status')=='OPEN' and _opened_after(x,since)]
def _key(x:dict[str,Any])->int|None:
    try:return int(x.get('source_candle_open_time'))
    except (TypeError,ValueError):return None
def _net(x:dict[str,Any])->float:
    value=_number(x.get('net_pnl_usdt'))
    if value is not None:return value
    return (_number(x.get('net_pnl_pct')) or 0)*(_number(x.get('position_notional_usdt')) or 0)/100
def _number(x:Any)->float|None:
    try:return float(x) if x is not None else None
    except (TypeError,ValueError):return None
def _ts(x:Any)->datetime|None:
    if not x:return None
    try:
        item=datetime.fromisoformat(str(x).replace('Z','+00:00'));return item.replace(tzinfo=item.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError:return None
def _fmt(x:datetime)->str:return x.astimezone(BRASILIA_TZ).strftime('%d/%m/%Y %H:%M BRT')
def _fmt_ts(x:Any)->str:return _fmt(_ts(x)) if _ts(x) else 'n/a'
def _fmt_price(x:float|None)->str:return f'{x:.4f}' if x is not None else 'n/a'
def _fmt_pct(x:float|None)->str:return f'{x:+.3f}%' if x is not None else 'n/a'
if __name__=='__main__': main()
