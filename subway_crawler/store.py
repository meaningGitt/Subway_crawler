from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .source import digest, encoded


def atomic_write(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(body if isinstance(body, bytes) else body.encode('utf-8'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'collection.sqlite', timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS records (
          source TEXT, record_key TEXT, line TEXT, reference_at TEXT, payload TEXT,
          content_hash TEXT, first_collected_at TEXT, last_collected_at TEXT,
          PRIMARY KEY(source, record_key));
        CREATE TABLE IF NOT EXISTS history (
          source TEXT, record_key TEXT, run_id TEXT, collected_at TEXT,
          reference_at TEXT, payload TEXT, content_hash TEXT,
          PRIMARY KEY(source, record_key, run_id));
        CREATE TABLE IF NOT EXISTS source_state (source TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, started_at TEXT,
          ended_at TEXT, status TEXT, payload TEXT);
        CREATE TABLE IF NOT EXISTS source_runs (run_id TEXT, source TEXT, status TEXT,
          payload TEXT, PRIMARY KEY(run_id,source));
        ''')

    def state(self, source):
        row = self.db.execute('SELECT payload FROM source_state WHERE source=?', (source,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_state(self, source, state):
        self.db.execute('INSERT OR REPLACE INTO source_state VALUES (?,?)', (source, encoded(state)))

    def count(self, source=None):
        return self.db.execute('SELECT count(*) FROM records' + (' WHERE source=?' if source else ''),
                               (source,) if source else ()).fetchone()[0]

    def merge(self, source, records, run_id, at):
        added = changed = 0
        for record in records:
            payload = encoded(record)
            content_hash = digest(payload.encode())
            old = self.db.execute('SELECT content_hash FROM records WHERE source=? AND record_key=?',
                                  (source, record['key'])).fetchone()
            if old and old[0] == content_hash:
                self.db.execute('UPDATE records SET last_collected_at=? WHERE source=? AND record_key=?',
                                (at, source, record['key']))
                continue
            added += old is None
            changed += old is not None
            self.db.execute('''INSERT INTO records VALUES (?,?,?,?,?,?,?,?)
              ON CONFLICT(source,record_key) DO UPDATE SET line=excluded.line,
              reference_at=excluded.reference_at,payload=excluded.payload,content_hash=excluded.content_hash,
              last_collected_at=excluded.last_collected_at''',
              (source, record['key'], record['line'], record['reference_at'], payload, content_hash, at, at))
            self.db.execute('INSERT INTO history VALUES (?,?,?,?,?,?,?)',
                            (source, record['key'], run_id, at, record['reference_at'], payload, content_hash))
        return added, changed

    def raw(self, source, body, suffix):
        path = self.root / 'raw' / source / (digest(body) + suffix)
        if not path.exists():
            atomic_write(path, body)
        return str(path.relative_to(self.root))

    def export(self):
        # CSV formula neutralization for spreadsheet readers; SQLite/raw retain exact values.
        def safe(value):
            return "'" + value if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')) else value
        for table in ('records', 'history', 'runs', 'source_runs'):
            cursor = self.db.execute(f'SELECT * FROM {table} ORDER BY 1,2')
            buf = io.StringIO(newline='')
            writer = csv.writer(buf)
            writer.writerow([c[0] for c in cursor.description])
            writer.writerows([safe(v) for v in row] for row in cursor)
            atomic_write(self.root / 'exports' / f'{table}.csv', '\ufeff' + buf.getvalue())
        # Flattened, human-usable station/line fields alongside timestamps.
        for source in self.db.execute('SELECT DISTINCT source FROM records').fetchall():
            rows = self.db.execute('SELECT * FROM records WHERE source=? ORDER BY record_key', source).fetchall()
            decoded = [(r, json.loads(r['payload'])) for r in rows]
            fields = sorted({k for _, p in decoded for k in p['data']})
            buf = io.StringIO(newline=''); writer = csv.writer(buf)
            writer.writerow(['record_key', 'line', 'first_collected_at', 'last_collected_at'] + fields)
            for r, p in decoded:
                writer.writerow([safe(r[k]) for k in ('record_key', 'line', 'first_collected_at', 'last_collected_at')]
                                + [safe(p['data'].get(k, '')) for k in fields])
            atomic_write(self.root / 'exports' / f'{source[0]}.csv', '\ufeff' + buf.getvalue())

    def close(self):
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.db.close()
