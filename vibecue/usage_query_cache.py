"""Exact, disposable numeric SQL projections shared by CLI query processes.

A cache entry belongs to a database identity and a transactional mutation epoch.
It is never a source of usage truth: absent/invalid state falls back to SQL.
Prices, exchange rates, wall-clock display values and visible log pages are not
cached here. Cache writes use private files, never upgrade a WAL read snapshot.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import uuid

VERSION = 1
MAX_BYTES = 8 * 1024 * 1024
MAX_ROWS = 30000
MAX_FILES = 32
MAX_TOTAL_BYTES = 32 * 1024 * 1024
TABLES = ('usage_requests', 'usage_request_details', 'usage_archived_daily')
TRIGGERS = {f'usage_query_epoch_{table}_{operation.lower()}':
            f'CREATE TRIGGER usage_query_epoch_{table}_{operation.lower()} AFTER {operation} ON {table} '
            'BEGIN UPDATE usage_query_epoch SET generation=generation+1 WHERE singleton=1; END'
            for table in TABLES for operation in ('INSERT', 'UPDATE', 'DELETE')}


def _normal(value):
    return ' '.join(value.split()).lower()


def _install_schema(conn):
    """Install epoch invalidation without scanning or changing numeric records.

    Missing/replaced triggers retire the identity, because writes may have
    happened while invalidation was absent. Trigger effects roll back with the
    write, including corrections, replacements and deletes from any connection.
    """
    conn.execute('CREATE TABLE IF NOT EXISTS usage_query_epoch ('
                 'singleton INTEGER PRIMARY KEY CHECK(singleton=1), '
                 'identity TEXT NOT NULL, generation INTEGER NOT NULL)')
    if conn.execute('SELECT 1 FROM usage_query_epoch WHERE singleton=1').fetchone() is None:
        conn.execute('INSERT INTO usage_query_epoch VALUES(1,?,0)', (str(uuid.uuid4()),))
    present = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'usage_query_epoch_%'"))
    if any(_normal(present.get(name, '')) != _normal(sql) for name, sql in TRIGGERS.items()):
        for name, sql in TRIGGERS.items():
            conn.execute('DROP TRIGGER IF EXISTS ' + name)
            conn.execute(sql)
        conn.execute('UPDATE usage_query_epoch SET identity=?,generation=0 WHERE singleton=1', (str(uuid.uuid4()),))


def ensure_schema(conn):
    # A failed derived-cache migration must not disable the numeric ledger or
    # leave half-installed trigger state. The constructor validates eligibility
    # again and falls back to direct SQL when the metadata is unavailable.
    conn.execute('SAVEPOINT usage_query_cache_schema')
    try:
        _install_schema(conn)
        conn.execute('RELEASE usage_query_cache_schema')
        return True
    except sqlite3.Error:
        conn.execute('ROLLBACK TO usage_query_cache_schema')
        conn.execute('RELEASE usage_query_cache_schema')
        return False


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


class ExactQueryCache:
    """Only use inside the same BEGIN snapshot as the consuming numeric query."""
    def __init__(self, conn, home, now):
        self.conn, self.directory = conn, Path(home) / 'usage-query-cache'
        self.context = None
        self.hits = self.misses = 0
        try:
            if not conn.in_transaction:
                return
            # Reading the epoch establishes the WAL snapshot before any cache
            # lookup. Later SELECTs (including pages) see exactly that snapshot.
            identity, generation = conn.execute('SELECT identity,generation FROM usage_query_epoch WHERE singleton=1').fetchone()
            present = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'usage_query_epoch_%'"))
            if any(_normal(present.get(name, '')) != _normal(sql) for name, sql in TRIGGERS.items()):
                return
            database = Path(home) / 'usage.sqlite3'
            meta = database.stat()
            # Timestamp upper bounds can share a key while the selected set is
            # unchanged. Crossing even an archived/future row changes this
            # indexed watermark. Rolling lower bounds remain literal and exact.
            current = conn.execute('SELECT MAX(timestamp) FROM usage_requests WHERE timestamp<=?', (now,)).fetchone()[0]
            archived = conn.execute('SELECT MAX(timestamp) FROM usage_archived_daily WHERE timestamp<=?', (now,)).fetchone()[0]
            watermark = max((value for value in (current, archived) if value is not None), default=None)
            self.context = (VERSION, str(database.resolve()), meta.st_dev, meta.st_ino,
                            meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns, identity, generation,
                            conn.execute('PRAGMA schema_version').fetchone()[0], watermark)
        except (OSError, sqlite3.Error, TypeError, ValueError):
            pass

    def rows(self, sql, params=(), *, upper_bounds=()):
        """Reuse raw rows; upper_bounds identifies only timestamp<=now params."""
        key = None
        if self.context is not None:
            normalized = list(params)
            for index in upper_bounds:
                normalized[index] = {'timestamp_watermark': self.context[-1]}
            key = hashlib.sha256(_encoded((self.context, sql, normalized))).hexdigest()
            cached = self._read(key)
            if cached is not None:
                self.hits += 1
                return cached
        self.misses += 1
        rows = [dict(row) for row in self.conn.execute(sql, params)]
        if key is not None:
            self._write(key, rows)
        return rows

    def _private_directory(self):
        try:
            meta = self.directory.lstat()
            return (stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.getuid() and
                    stat.S_IMODE(meta.st_mode) & 0o077 == 0)
        except OSError:
            return False

    def _read(self, key):
        try:
            if not self._private_directory():
                return None
            path = self.directory / (key + '.json')
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                meta = os.fstat(stream.fileno())
                if (not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_BYTES or
                        meta.st_uid != os.getuid() or stat.S_IMODE(meta.st_mode) & 0o077):
                    return None
                data = json.loads(stream.read(MAX_BYTES + 1))
            rows = data['rows']
            if (data.get('key') != key or not isinstance(rows, list) or len(rows) > MAX_ROWS or
                    any(not isinstance(row, dict) for row in rows) or
                    data.get('digest') != hashlib.sha256(_encoded(rows)).hexdigest()):
                return None
            return rows
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            return None

    def _write(self, key, rows):
        temporary = None
        try:
            if len(rows) > MAX_ROWS:
                return
            encoded = _encoded(rows)
            if len(encoded) > MAX_BYTES - 256:
                return
            data = _encoded({'key': key, 'digest': hashlib.sha256(encoded).hexdigest(), 'rows': rows})
            if len(data) > MAX_BYTES:
                return
            self.directory.mkdir(mode=0o700, exist_ok=True)
            if not self._private_directory():
                return
            fd, temporary = tempfile.mkstemp(prefix='.query-', dir=self.directory)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            os.replace(temporary, self.directory / (key + '.json'))
            temporary = None
            # Entries are disposable. Pruning is bounded and ignores unrelated
            # files; concurrent queries may lose cache hits, never numeric truth.
            files = []
            with os.scandir(self.directory) as entries:
                for _, entry in zip(range(256), entries):
                    if len(entry.name) == 69 and entry.name.endswith('.json') and entry.is_file(follow_symlinks=False):
                        detail = entry.stat(follow_symlinks=False)
                        files.append((detail.st_mtime_ns, detail.st_size, entry.path))
            files.sort(reverse=True)
            total = 0
            for index, (_, size, path) in enumerate(files):
                total += size
                if index >= MAX_FILES or total > MAX_TOTAL_BYTES:
                    try: os.unlink(path)
                    except OSError: pass
        except (OSError, ValueError, TypeError, RecursionError):
            pass
        finally:
            if temporary is not None:
                try: os.unlink(temporary)
                except OSError: pass
