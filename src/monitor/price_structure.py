"""Price-only, causal telemetry. Never consulted by trading decisions."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock

HOUR = 3_600_000
VERSION = "PRICE_STRUCTURE_72H_1H_K3_V1"


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


def classify_window(window):
    """Exactly the study's strict pivots, including left-edge truncation."""
    if len(window) != 72:
        return "UNDEFINED", [], []
    highs, lows = [], []
    for i in range(3, 69):
        neighbours = window[i-3:i] + window[i+1:i+4]
        if all(window[i].high > c.high for c in neighbours):
            highs.append(i)
        if all(window[i].low < c.low for c in neighbours):
            lows.append(i)
    hi, lo = highs[-2:], lows[-2:]
    if len(hi) < 2 or len(lo) < 2:
        return "UNDEFINED", hi, lo
    dh = window[hi[1]].high - window[hi[0]].high
    dl = window[lo[1]].low - window[lo[0]].low
    return ("BULL" if dh > 0 and dl > 0 else
            "BEAR" if dh < 0 and dl < 0 else "MIXED"), hi, lo


class StructureBuffer:
    def __init__(self, candles=(), retain=None):
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
        if c.low > c.high:
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
                opens = list(range(end - 72 * HOUR, end, HOUR))
                missing = [t for t in opens if t not in self.hours]
                window = [self.hours[t] for t in opens if t in self.hours]
                label, hi, lo = classify_window(window) if not missing else ("UNDEFINED", [], [])
                def pivots(indices, kind):
                    return [{"open_ms": window[i].open_ms,
                             "at": iso(window[i].open_ms),
                             "price": getattr(window[i], kind),
                             "confirmed_ms": window[i+3].open_ms + HOUR - 1}
                            for i in indices]
                highs, lows = pivots(hi, "high"), pivots(lo, "low")
                def relation(ps, up, down):
                    if len(ps) < 2:
                        return "UNDEFINED"
                    return up if ps[1]["price"] > ps[0]["price"] else down if ps[1]["price"] < ps[0]["price"] else "EQUAL"
                self._cache[end] = dict(label=label, highs=highs, lows=lows,
                    highs_class=relation(highs, "HH", "LH"), lows_class=relation(lows, "HL", "LL"),
                    window_start_ms=end-72*HOUR, latest_closed_ms=end-1,
                    missing_hours=missing, status="MISSING_CANDLES" if missing else
                    "INSUFFICIENT_PIVOTS" if label == "UNDEFINED" else "OK")
            result = deepcopy(self._cache[end])
        return {**result, "at": iso(stamp), "version": VERSION, "source": source}


def fields(snapshot, phase):
    if not snapshot:
        return {}
    return {f"trend_{phase}": snapshot["label"], f"trend_{phase}_at": snapshot["at"],
            f"trend_{phase}_details": snapshot}


_live = None


class LiveStructure:
    def __init__(self, symbol, started_ms):
        self.symbol, self.started_ms = symbol, started_ms
        self.buffer = StructureBuffer(retain=168)

    def capture(self, symbol, at):
        # Old positions must not acquire fabricated LIVE entry telemetry at restart.
        if symbol != self.symbol or timestamp_ms(at) < self.started_ms:
            return {}
        return self.buffer.snapshot(at, "LIVE")


def install_live(provider):
    global _live
    _live = provider


def capture_live(symbol, at, phase):
    if _live is None or not at:
        return {}
    try:
        return fields(_live.capture(symbol, at), phase)
    except Exception as exc:
        # Telemetry must never interrupt an economic transition or recovery.
        return {f'trend_{phase}': 'UNDEFINED', f'trend_{phase}_at': str(at),
                f'trend_{phase}_details': dict(label='UNDEFINED', at=str(at),
                    version=VERSION, source='LIVE', status='TELEMETRY_ERROR',
                    error=type(exc).__name__, highs=[], lows=[],
                    highs_class='UNDEFINED', lows_class='UNDEFINED')}


def restore_fields(state):
    return {k: deepcopy(v) for k, v in state.items()
            if k in {f"trend_{p}{suffix}" for p in ("open", "close")
                     for suffix in ("", "_at", "_details")}}
