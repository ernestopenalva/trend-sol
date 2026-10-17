"""Optional read-only overlay and grouping for persisted structural telemetry."""
import json
from collections import Counter
from pathlib import Path
from functools import lru_cache
from src.monitor.price_structure import restore_fields, timestamp_ms, VERSION, STATES


@lru_cache(maxsize=8)
def _sidecar(path, mtime):
    rows = [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()]
    return {(r['symbol'], str(r['pair_id']), timestamp_ms(r['opened_at'])): r for r in rows}


def overlay(row, root):
    structural_keys={f'{layer}_{p}{s}' for layer in ('trend','wave') for p in ('open','close') for s in ('','_at','_details')}|{'wave_rule_version'}
    result = {k:v for k,v in row.items() if k not in structural_keys}
    result.update(restore_fields(row))
    opened = row.get('opened_at') or row.get('open_ts')
    if not opened or not row.get('pair_id'):
        return result
    for filename in ('price_structure_backfill.jsonl','wave_structure_backfill.jsonl'):
        path=Path(root)/'data/telemetry'/filename
        if not path.exists():continue
        old = _sidecar(str(path), path.stat().st_mtime_ns).get(
            (row.get('symbol'), str(row['pair_id']), timestamp_ms(opened)), {})
        for key, value in restore_fields(old).items():
            # Native telemetry wins. A close sidecar must match the actual close.
            if key.startswith(('trend_close','wave_close')):
                closed = row.get('closed_at') or row.get('close_ts')
                if not closed or not old.get('closed_at') or timestamp_ms(closed) != timestamp_ms(old['closed_at']):continue
            if result.get(key) is None:result[key] = value
    return result


def aggregate(rows,prefix='trend'):
    rows = list(rows)
    def label(row,phase):
        if phase=='close' and not (row.get('closed_at') or row.get('close_ts')) and row.get('status')!='CLOSED':return 'OPEN'
        value=row.get(prefix+'_'+phase)
        return value if value in (*STATES,'UNDEFINED') else 'UNAVAILABLE'
    result = {prefix+"_open": dict(Counter(label(r,'open') for r in rows)),
              prefix+"_close": dict(Counter(label(r,'close') for r in rows)),
              "transitions": dict(Counter(f"{label(r,'open')} -> {label(r,'close')}" for r in rows))}
    for phase in (prefix+'_open',prefix+'_close'):
        result[phase]={name:result[phase].get(name,0) for name in (*STATES,'UNDEFINED','UNAVAILABLE','OPEN')}
    return result
