"""Price-only, causal telemetry. Never consulted by trading decisions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from math import isfinite

HOUR = 3_600_000
VERSION = "TREND_72H_2X36H_V1"
STATES = ('BULL', 'BEAR', 'EXPA', 'COMP')


@dataclass(frozen=True)
class StructureRule:
    version: str
    interval_ms: int
    count: int
    conflict_ms: int
    prefix: str = 'trend'


TIDE = StructureRule(VERSION, HOUR, 72, 4*HOUR)
WAVE_VERSION = 'TREND_12H_2X6H_15M_V1'
WAVE = StructureRule(WAVE_VERSION, 900_000, 48, 45*60_000, 'wave')


def timestamp_ms(value):
    if isinstance(value, (int, float)):
        return int(value)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Structure timestamps must include timezone")
    return int(dt.timestamp() * 1000)


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Hour:
    open_ms: int
    high: float
    low: float
    closed: bool = True
    available_ms: int | None = None


def classify_window(window, rule=TIDE):
    """Indices of two half-window extremes; ties select the oldest candle."""
    if len(window) != rule.count:
        return "UNDEFINED", [], []
    half=rule.count//2
    hi = [max(range(start, start+half), key=lambda i: window[i].high) for start in (0,half)]
    lo = [min(range(start, start+half), key=lambda i: window[i].low) for start in (0,half)]
    dh = window[hi[1]].high - window[hi[0]].high
    dl = window[lo[1]].low - window[lo[0]].low
    return ("UNDEFINED" if dh == 0 or dl == 0 else
            "BULL" if dh > 0 and dl > 0 else "BEAR" if dh < 0 and dl < 0 else
            "EXPA" if dh > 0 and dl < 0 else "COMP"), hi, lo


def classify(candles, at, source, rule=TIDE):
    """Single causal implementation for LIVE, BACKFILL and REPLAY.

    A gap never silently changes the 72-hour horizon. Hour.open_ms denotes the
    candle open (not the intrahour time at which its high/low was observed).
    """
    stamp = timestamp_ms(at)
    interval=rule.interval_ms
    end = stamp // interval * interval
    by = {c.open_ms:c for c in candles if c.closed and c.open_ms+interval-1 < stamp
          and (rule==TIDE or c.available_ms is None or c.available_ms<=stamp)}
    opens = list(range(end-rule.count*interval, end, interval))
    missing = [t for t in opens if t not in by]
    window = [by[t] for t in opens if t in by]
    latest = max((c.open_ms for c in window),default=None)
    result = dict(label='UNDEFINED', version=rule.version, rule_version=rule.version, at=iso(stamp),
        source=source, window_start_ms=end-rule.count*interval, window_end_ms=end,
        midpoint_ms=end-rule.count//2*interval, expected_latest_closed_ms=end-1,
        latest_closed_ms=latest+interval-1 if latest is not None else None, latest_closed_open_ms=latest,
        tie_convention='OLDEST_CANDLE_WITHIN_HALF', missing_hours=missing,
        highs=[], lows=[], highs_class='UNDEFINED', lows_class='UNDEFINED',
        topo_1=None,topo_2=None,fundo_1=None,fundo_2=None,
        top_distance_hours=None, bottom_distance_hours=None,
        top_boundary_conflict_4h=None, bottom_boundary_conflict_4h=None,
        undefined_reason=None)
    if rule!=TIDE:
        result.pop('missing_hours');result['missing_candles']=missing
        result.pop('top_boundary_conflict_4h');result.pop('bottom_boundary_conflict_4h')
        result.update(interval_ms=interval,candle_count=rule.count,conflict_threshold_ms=rule.conflict_ms,
                      top_distance_minutes=None,bottom_distance_minutes=None,
                      top_boundary_conflict_45m=None,bottom_boundary_conflict_45m=None)
    if missing:
        result.update(status='MISSING_CANDLES', undefined_reason=
            'INSUFFICIENT_HISTORY' if not by or min(by)>opens[0] else 'MISSING_CANDLES')
        if rule!=TIDE:result['label']='UNAVAILABLE'
        return result
    if any(not isfinite(c.high) or not isfinite(c.low) or c.low > c.high for c in window):
        result.update(status='INVALID_CANDLES', undefined_reason='INVALID_OHLC')
        if rule!=TIDE:result['label']='UNAVAILABLE'
        return result
    name, hi, lo = classify_window(window,rule)
    def extremes(indices, kind):
        return [dict(open_ms=window[i].open_ms, at=iso(window[i].open_ms),
                     price=getattr(window[i],kind), closed_ms=window[i].open_ms+interval-1)
                for i in indices]
    highs, lows = extremes(hi,'high'), extremes(lo,'low')
    dh, dl = highs[1]['price']-highs[0]['price'], lows[1]['price']-lows[0]['price']
    top_distance = abs(highs[1]['open_ms']-highs[0]['open_ms'])/HOUR
    bottom_distance = abs(lows[1]['open_ms']-lows[0]['open_ms'])/HOUR
    result.update(label=name, highs=highs, lows=lows,
        topo_1=highs[0], topo_2=highs[1], fundo_1=lows[0], fundo_2=lows[1],
        highs_class='HH' if dh>0 else 'LH' if dh<0 else 'EQUAL',
        lows_class='HL' if dl>0 else 'LL' if dl<0 else 'EQUAL',
        top_distance_hours=top_distance, bottom_distance_hours=bottom_distance,
        top_boundary_conflict_4h=top_distance<4, bottom_boundary_conflict_4h=bottom_distance<4,
        status='OK' if name!='UNDEFINED' else 'EQUAL_EXTREME',
        undefined_reason='EQUAL_EXTREME' if name=='UNDEFINED' else None)
    if rule!=TIDE:
        result.pop('top_boundary_conflict_4h');result.pop('bottom_boundary_conflict_4h')
        result.update(top_distance_minutes=top_distance*60,bottom_distance_minutes=bottom_distance*60,
                      top_boundary_conflict_45m=top_distance*HOUR<rule.conflict_ms,
                      bottom_boundary_conflict_45m=bottom_distance*HOUR<rule.conflict_ms)
    return result


class StructureBuffer:
    def __init__(self, candles=(), retain=None, rule=TIDE):
        candles = list(candles)
        if len({c.open_ms for c in candles}) != len(candles):
            raise ValueError('Duplicate hourly candles')
        self.hours = {c.open_ms: c for c in candles}
        self.retain = retain
        self.rule = rule
        self._cache = {}
        self._lock = RLock()

    def add_kline(self, kline, now_ms):
        at, close = int(kline[0]), int(kline[6])
        interval=self.rule.interval_ms
        if at % interval or close != at + interval - 1:
            raise ValueError("Not a complete UTC-aligned 1h candle")
        if close >= now_ms:
            return False
        c = Hour(at, float(kline[2]), float(kline[3]),available_ms=now_ms if self.rule!=TIDE else None)
        if c.low > c.high or not isfinite(c.low) or not isfinite(c.high):
            raise ValueError("Invalid OHLC")
        with self._lock:
            if at in self.hours:
                old=self.hours[at]
                if (old.high,old.low)!=(c.high,c.low):raise ValueError("Conflicting closed candle")
                return True  # Duplicate reconnect delivery must not move first availability.
            self.hours[at] = c
            if self.retain and len(self.hours) > self.retain:
                for old in sorted(self.hours)[:-self.retain]:
                    del self.hours[old]
            self._cache.clear()
        return True

    def snapshot(self, at, source):
        stamp = timestamp_ms(at)
        interval=self.rule.interval_ms
        end = stamp // interval * interval
        key=end if self.rule==TIDE else stamp
        with self._lock:
            if key not in self._cache:
                # Scan at most the exact 72 slots, not the complete replay cache.
                relevant=[self.hours[t] for t in range(end-self.rule.count*interval,end,interval) if t in self.hours]
                self._cache[key] = classify(relevant, end if self.rule==TIDE else stamp, source,self.rule)
                if len(self._cache)>512:self._cache.pop(next(iter(self._cache)))
            result = deepcopy(self._cache[key])
        return {**result, "at": iso(stamp), "version": self.rule.version, "source": source}


def fields(snapshot, phase, rule=TIDE):
    if not snapshot:
        return {}
    prefix=rule.prefix
    result = {f"{prefix}_{phase}": snapshot["label"], f"{prefix}_{phase}_at": snapshot["at"],
              f"{prefix}_{phase}_details": snapshot}
    if phase == 'open':
        result.update({f'{prefix}_close':'OPEN',f'{prefix}_close_at':None,
                      f'{prefix}_close_details':dict(label='OPEN',at=None,version=rule.version,
                                               rule_version=rule.version,source=snapshot['source'])})
    if rule!=TIDE:result[f'{prefix}_rule_version']=rule.version
    return result


_live = None


class LiveStructure:
    def __init__(self, symbol, started_ms):
        self.symbol, self.started_ms = symbol, started_ms
        self.buffer = StructureBuffer(retain=168)
        self.wave_buffer = StructureBuffer(retain=192,rule=WAVE)

    def capture(self, symbol, at, phase='open'):
        # Old positions must not acquire fabricated LIVE entry telemetry at restart.
        if symbol != self.symbol or (phase=='open' and timestamp_ms(at) < self.started_ms):
            return {}
        return self.buffer.snapshot(at, "LIVE")

    def capture_wave(self,symbol,at,phase='open'):
        if symbol!=self.symbol or (phase=='open' and timestamp_ms(at)<self.started_ms):return {}
        return self.wave_buffer.snapshot(at,'LIVE')


def install_live(provider):
    global _live
    _live = provider


def _capture_tide(symbol, at, phase):
    if _live is None or not at:
        return {}
    try:
        return fields(_live.capture(symbol, at, phase), phase)
    except Exception as exc:
        # Telemetry must never interrupt an economic transition or recovery.
        snapshot=dict(label='UNDEFINED', at=str(at),
                    version=VERSION, rule_version=VERSION, source='LIVE', status='TELEMETRY_ERROR',
                    undefined_reason='TELEMETRY_ERROR',
                    error=type(exc).__name__, highs=[], lows=[],
                    highs_class='UNDEFINED', lows_class='UNDEFINED',
                    topo_1=None,topo_2=None,fundo_1=None,fundo_2=None,
                    top_distance_hours=None,bottom_distance_hours=None,
                    top_boundary_conflict_4h=None,bottom_boundary_conflict_4h=None)
        return fields(snapshot,phase)


def capture_live(symbol,at,phase):
    result=_capture_tide(symbol,at,phase)
    if _live is None or not at:return result
    try:
        result.update(fields(_live.capture_wave(symbol,at,phase),phase,WAVE))
    except Exception as exc:
        result.update(fields(dict(label='UNAVAILABLE',at=str(at),version=WAVE_VERSION,
            rule_version=WAVE_VERSION,source='LIVE',status='TELEMETRY_ERROR',
            undefined_reason='TELEMETRY_ERROR',error=type(exc).__name__),phase,WAVE))
    return result


def restore_fields(state):
    result = {}
    for phase in ('open','close'):
        details = state.get(f'trend_{phase}_details') or {}
        if details.get('version') == VERSION:
            result.update({k:deepcopy(state[k]) for k in
                (f'trend_{phase}',f'trend_{phase}_at',f'trend_{phase}_details') if k in state})
        wave=state.get(f'wave_{phase}_details') or {}
        if wave.get('version')==WAVE_VERSION:
            result.update({k:deepcopy(state[k]) for k in
                (f'wave_{phase}',f'wave_{phase}_at',f'wave_{phase}_details') if k in state})
            result['wave_rule_version']=WAVE_VERSION
    return result
