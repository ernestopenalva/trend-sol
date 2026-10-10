"""Optional read-only overlay and grouping for persisted structural telemetry."""
import json
from collections import Counter
from pathlib import Path
from functools import lru_cache
from src.monitor.price_structure import restore_fields, timestamp_ms


@lru_cache(maxsize=8)
def _sidecar(path, mtime):
    rows = [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()]
    return {(r['symbol'], str(r['pair_id']), timestamp_ms(r['opened_at'])): r for r in rows}


def overlay(row, root):
    path = Path(root)/'data/telemetry/price_structure_backfill.jsonl'
    result = dict(row)
    opened = row.get('opened_at') or row.get('open_ts')
    if not path.exists() or not opened or not row.get('pair_id'):
        return result
    old = _sidecar(str(path), path.stat().st_mtime_ns).get(
        (row.get('symbol'), str(row['pair_id']), timestamp_ms(opened)), {})
    for key, value in restore_fields(old).items():
        # Native LIVE telemetry always wins. A close sidecar must match the actual close.
        if key.startswith('trend_close'):
            closed = row.get('closed_at') or row.get('close_ts')
            if not closed or not old.get('closed_at') or timestamp_ms(closed) != timestamp_ms(old['closed_at']):
                continue
        if result.get(key) is None:
            result[key] = value
    return result


def aggregate(rows):
    rows = list(rows)
    return {"trend_open": dict(Counter(r.get('trend_open', 'UNAVAILABLE') for r in rows)),
            "trend_close": dict(Counter(r.get('trend_close', 'OPEN' if not (r.get('closed_at') or r.get('close_ts')) else 'UNAVAILABLE') for r in rows)),
            "transitions": dict(Counter(f"{r.get('trend_open', 'UNAVAILABLE')} -> {r.get('trend_close', 'OPEN' if not (r.get('closed_at') or r.get('close_ts')) else 'UNAVAILABLE')}" for r in rows))}
