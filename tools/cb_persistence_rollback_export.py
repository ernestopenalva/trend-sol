"""Offline export of a v3 checkpoint to schema 2. Never modify the source.

Stop the owning writer first. Export goes to a NEW directory, not live state.
This preserves post-migration trades (unlike restoring the pre-v3 backup alone).
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.monitor.circuit_breaker_shadow import _atomic_json


def export(state_path, output):
    if output.exists():
        raise ValueError('Rollback output must not already exist')
    state = json.loads(state_path.read_text(encoding='utf8'))
    if state.get('cb_schema') != 3:
        raise ValueError('Expected schema 3 checkpoint')
    journal = state_path.with_suffix('.history.sqlite')
    db = sqlite3.connect(journal.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        owner = db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()
        if owner is None or owner[0] != journal.name:
            raise ValueError('Rollback journal identity mismatch')
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('History journal integrity failed')
        history = {}
        for kind in ('ledger', 'events'):
            rows = db.execute('''SELECT h.ordinal,h.payload FROM history h
                JOIN (SELECT ordinal,MAX(revision) r FROM history
                      WHERE kind=? AND revision<=? GROUP BY ordinal) v
                ON h.ordinal=v.ordinal AND h.revision=v.r WHERE h.kind=? ORDER BY h.ordinal''',
                (kind, state['history_revision'], kind)).fetchall()
            if [r[0] for r in rows] != list(range(state['history_counts'][kind])):
                raise ValueError('Rollback history/checkpoint reconciliation failed')
            history[kind] = [json.loads(r[1]) for r in rows]
        row = db.execute('SELECT payload FROM pending WHERE singleton=1').fetchone()
        pending = json.loads(row[0]) if row else None
        old_pending_path = state_path.with_suffix(state_path.suffix+'.pending')
        if old_pending_path.exists():
            legacy = json.loads(old_pending_path.read_text(encoding='utf8'))
            if pending is None or legacy['sequence'] > pending['sequence']:
                pending = legacy
            elif legacy['sequence'] == pending['sequence'] and legacy != pending:
                raise ValueError('Conflicting pending inputs')
    finally:
        db.close()
    state.update(cb_schema=2, closed_records=history['ledger'], audit_events=history['events'])
    state.pop('history_revision'); state.pop('history_counts')
    output.mkdir(parents=True)
    _atomic_json(output/state_path.name, json.dumps(state, ensure_ascii=False))
    for kind in ('ledger', 'events'):
        _atomic_json(output/(kind+'.jsonl'), ''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in history[kind]))
    if pending is not None:
        _atomic_json(output/(state_path.name+'.pending'), json.dumps(pending, ensure_ascii=False))
    return {'checkpoint': str(output/state_path.name), 'ledger_count': len(history['ledger']),
            'event_count': len(history['events']), 'sequence': state['sequence'],
            'pending_sequence': pending['sequence'] if pending else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(args.state, args.output)))


if __name__ == '__main__':
    main()
