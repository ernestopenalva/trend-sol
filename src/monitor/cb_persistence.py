"""Durable incremental CB history/input journal. No trading decisions live here.

JSON checkpoint is the commit marker. History is durable before that marker;
revisions newer than it are discarded on restore, then the durable input replays.
"""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
from pathlib import Path


def sync_directory(path: Path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def preserve_files(paths):
    """Idempotent durable pre-migration copies; never overwrite an earlier copy."""
    for path in paths:
        if not path.exists():
            continue
        target = path.with_name(path.name + '.pre-v3')
        if target.exists():
            if not target.is_file():
                raise ValueError(f'Invalid migration backup: {target}')
            if target.read_bytes() == path.read_bytes():
                continue
            # A second migration after rollback must preserve the CURRENT legacy
            # state too; never assume the first pre-v3 snapshot is still current.
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            target = path.with_name(path.name + '.pre-v3.' + digest)
            if target.exists():
                if target.read_bytes() != path.read_bytes():
                    raise ValueError(f'Migration backup content differs: {target}')
                continue
        tmp = target.with_name(target.name + '.tmp')
        with path.open('rb') as source, tmp.open('wb') as output:
            while chunk := source.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, target)
        sync_directory(target.parent)


class CBHistoryStore:
    def __init__(self, path: Path, *, must_exist=False):
        if must_exist and not path.is_file():
            raise ValueError('CB history journal missing; reconciliation required')
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path)
        mode = self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0]
        if mode.lower() != 'wal':
            raise ValueError('CB history requires WAL mode')
        self.db.execute('PRAGMA synchronous=FULL')
        if self.db.execute('PRAGMA synchronous').fetchone()[0] != 2:
            self.db.close()
            raise ValueError('CB history requires FULL durability')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS history (
                kind TEXT NOT NULL, ordinal INTEGER NOT NULL,
                revision INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(kind, ordinal, revision));
            CREATE TABLE IF NOT EXISTS pending (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        owner = self.db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()
        if owner is None and must_exist:
            self.db.close()
            raise ValueError('CB history journal identity missing; reconciliation required')
        if owner is not None and owner[0] != path.name:
            self.db.close()
            raise ValueError('CB history journal belongs to another arm')
        if owner is None:
            with self.db:
                self.db.execute("INSERT INTO metadata VALUES ('owner',?)", (path.name,))
        sync_directory(path.parent)

    def bootstrap(self, ledger, events):
        # A legacy checkpoint remains authoritative until the v3 JSON commit.
        with self.db:
            self.db.execute('DELETE FROM history')
            self.db.executemany('INSERT INTO history VALUES (?,?,0,?)',
                [(kind, i, json.dumps(row, ensure_ascii=False))
                 for kind, rows in (('ledger', ledger), ('events', events))
                 for i, row in enumerate(rows)])

    def record_input(self, item):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO pending VALUES (1,?)',
                            (json.dumps(item, ensure_ascii=False),))

    def pending(self):
        row = self.db.execute('SELECT payload FROM pending WHERE singleton=1').fetchone()
        return json.loads(row[0]) if row else None

    def stage(self, revision, changes):
        if not any(changes.values()):
            return revision
        next_revision = revision + 1
        with self.db:
            for kind, rows in changes.items():
                self.db.executemany('INSERT INTO history VALUES (?,?,?,?)',
                    [(kind, ordinal, next_revision, json.dumps(row, ensure_ascii=False))
                     for ordinal, row in rows.items()])
        return next_revision

    def restore(self, revision, counts):
        result = {}
        for kind in ('ledger', 'events'):
            rows = self.db.execute('''SELECT h.ordinal,h.payload FROM history h
                JOIN (SELECT ordinal,MAX(revision) r FROM history
                      WHERE kind=? AND revision<=? GROUP BY ordinal) v
                ON h.ordinal=v.ordinal AND h.revision=v.r WHERE h.kind=?
                ORDER BY h.ordinal''', (kind, revision, kind)).fetchall()
            if [r[0] for r in rows] != list(range(counts[kind])):
                raise ValueError('CB history/checkpoint count reconciliation failed')
            result[kind] = [json.loads(row[1]) for row in rows]
        with self.db:
            self.db.execute('DELETE FROM history WHERE revision>?', (revision,))
        return result

    def close(self):
        self.db.close()


def append_projection(path: Path, content: str):
    """Append only the new suffix. Authoritative history is already durable."""
    existed = path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as output:
        output.write(content.encode('utf8'))
        output.flush()
        os.fsync(output.fileno())
    if not existed:
        sync_directory(path.parent)


def read_cb_checkpoint(path: Path):
    """Read-only compatibility view for audit tools; never expands on disk."""
    state = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(state, dict) or state.get('cb_schema') != 3:
        return state
    journal = path.with_suffix('.history.sqlite')
    if not journal.is_file():
        raise ValueError('Schema 3 audit requires its history journal, not JSON alone')
    db = sqlite3.connect(journal.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        owner = db.execute("SELECT value FROM metadata WHERE key='owner'").fetchone()
        if owner is None or owner[0] != journal.name:
            raise ValueError('CB read-only journal identity mismatch')
        for kind, field in (('ledger', 'closed_records'), ('events', 'audit_events')):
            rows = db.execute('''SELECT h.ordinal,h.payload FROM history h
                JOIN (SELECT ordinal,MAX(revision) r FROM history
                      WHERE kind=? AND revision<=? GROUP BY ordinal) v
                ON h.ordinal=v.ordinal AND h.revision=v.r WHERE h.kind=? ORDER BY h.ordinal''',
                (kind, state['history_revision'], kind)).fetchall()
            if [r[0] for r in rows] != list(range(state['history_counts'][kind])):
                raise ValueError('CB read-only history/checkpoint reconciliation failed')
            state[field] = [json.loads(r[1]) for r in rows]
        row = db.execute('SELECT payload FROM pending WHERE singleton=1').fetchone()
        state['_journal_pending'] = json.loads(row[0]) if row else {}
    finally:
        db.close()
    return state
