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


def classify_window(window):
    """Indices of two half-window extremes; ties select the oldest candle."""
    if len(window) != 72:
        return "UNDEFINED", [], []
    hi = [max(range(start, start+36), key=lambda i: window[i].high) for start in (0,36)]
    lo = [min(range(start, start+36), key=lambda i: window[i].low) for start in (0,36)]
    dh = window[hi[1]].high - window[hi[0]].high
    dl = window[lo[1]].low - window[lo[0]].low
    return ("UNDEFINED" if dh == 0 or dl == 0 else
            "BULL" if dh > 0 and dl > 0 else "BEAR" if dh < 0 and dl < 0 else
            "EXPA" if dh > 0 and dl < 0 else "COMP"), hi, lo


def classify(candles, at, source):
    """Single causal implementation for LIVE, BACKFILL and REPLAY.

    A gap never silently changes the 72-hour horizon. Hour.open_ms denotes the
    candle open (not the intrahour time at which its high/low was observed).
    """
    stamp = timestamp_ms(at)
    end = stamp // HOUR * HOUR
    by = {c.open_ms:c for c in candles if c.closed and c.open_ms+HOUR-1 < stamp}
    opens = list(range(end-72*HOUR, end, HOUR))
    missing = [t for t in opens if t not in by]
    window = [by[t] for t in opens if t in by]
    latest = max((c.open_ms for c in window),default=None)
    result = dict(label='UNDEFINED', version=VERSION, rule_version=VERSION, at=iso(stamp),
        source=source, window_start_ms=end-72*HOUR, window_end_ms=end,
        midpoint_ms=end-36*HOUR, expected_latest_closed_ms=end-1,
        latest_closed_ms=latest+HOUR-1 if latest is not None else None, latest_closed_open_ms=latest,
        tie_convention='OLDEST_CANDLE_WITHIN_HALF', missing_hours=missing,
        highs=[], lows=[], highs_class='UNDEFINED', lows_class='UNDEFINED',
        topo_1=None,topo_2=None,fundo_1=None,fundo_2=None,
        top_distance_hours=None, bottom_distance_hours=None,
        top_boundary_conflict_4h=None, bottom_boundary_conflict_4h=None,
        undefined_reason=None)
    if missing:
        result.update(status='MISSING_CANDLES', undefined_reason=
            'INSUFFICIENT_HISTORY' if not by or min(by)>opens[0] else 'MISSING_CANDLES')
        return result
    if any(not isfinite(c.high) or not isfinite(c.low) or c.low > c.high for c in window):
        result.update(status='INVALID_CANDLES', undefined_reason='INVALID_OHLC')
        return result
    name, hi, lo = classify_window(window)
    def extremes(indices, kind):
        return [dict(open_ms=window[i].open_ms, at=iso(window[i].open_ms),
                     price=getattr(window[i],kind), closed_ms=window[i].open_ms+HOUR-1)
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
    return result


class StructureBuffer:
    def __init__(self, candles=(), retain=None):
        candles = list(candles)
        if len({c.open_ms for c in candles}) != len(candles):
            raise ValueError('Duplicate hourly candles')
        self.hours = {c.open_ms: c for c in candles}
        self.retain = retain
        self._cache = {}
        self._lock = RLock()

    def add_kline(self, kline, now_ms):
        at, close = int(kline[0]), int(kline[6])
        if at % HOUR or close != at + HOUR - 1:
            raise ValueError("Not a complete UTC-aligned 1h candle")
        if close >= now_ms:
            return False
        c = Hour(at, float(kline[2]), float(kline[3]))
        if c.low > c.high or not isfinite(c.low) or not isfinite(c.high):
            raise ValueError("Invalid OHLC")
        with self._lock:
            if at in self.hours and self.hours[at] != c:
                raise ValueError("Conflicting closed 1h candle")
            self.hours[at] = c
            if self.retain and len(self.hours) > self.retain:
                for old in sorted(self.hours)[:-self.retain]:
                    del self.hours[old]
            self._cache.clear()
        return True

    def snapshot(self, at, source):
        stamp = timestamp_ms(at)
        end = stamp // HOUR * HOUR
        with self._lock:
            if end not in self._cache:
                # Scan at most the exact 72 slots, not the complete replay cache.
                relevant=[self.hours[t] for t in range(end-72*HOUR,end,HOUR) if t in self.hours]
                self._cache[end] = classify(relevant, end, source)
            result = deepcopy(self._cache[end])
        return {**result, "at": iso(stamp), "version": VERSION, "source": source}


def fields(snapshot, phase):
    if not snapshot:
        return {}
    result = {f"trend_{phase}": snapshot["label"], f"trend_{phase}_at": snapshot["at"],
              f"trend_{phase}_details": snapshot}
    if phase == 'open':
        result.update(trend_close='OPEN', trend_close_at=None,
                      trend_close_details=dict(label='OPEN',at=None,version=VERSION,
                                               rule_version=VERSION,source=snapshot['source']))
    return result


_live = None


class LiveStructure:
    def __init__(self, symbol, started_ms):
        self.symbol, self.started_ms = symbol, started_ms
        self.buffer = StructureBuffer(retain=168)

    def capture(self, symbol, at, phase='open'):
        # Old positions must not acquire fabricated LIVE entry telemetry at restart.
        if symbol != self.symbol or (phase=='open' and timestamp_ms(at) < self.started_ms):
            return {}
        return self.buffer.snapshot(at, "LIVE")


def install_live(provider):
    global _live
    _live = provider


def capture_live(symbol, at, phase):
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


def restore_fields(state):
    result = {}
    for phase in ('open','close'):
        details = state.get(f'trend_{phase}_details') or {}
        if details.get('version') == VERSION:
            result.update({k:deepcopy(state[k]) for k in
                (f'trend_{phase}',f'trend_{phase}_at',f'trend_{phase}_details') if k in state})
    return result
