import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from vibecue import usage


class UsageSyncStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'ledger'

    def metadata(self, **values):
        with usage.connect(self.home) as conn:
            usage.set_meta(conn, 'sync', dict(running=False, last_synced_at=1234, **values))

    def test_missing_status_does_not_create_ledger_or_lock(self):
        data = usage.sync_status(self.home)
        self.assertFalse(data['available'])
        self.assertFalse(self.home.exists())

    def test_status_reads_only_metadata_and_never_queries_history_or_prices(self):
        self.metadata(files_scanned=20, files_total=40, source_path='PRIVATE-PATH', auth='PRIVATE-KEY')
        statements = []; original = sqlite3.connect
        def guarded(*args, **kwargs):
            conn = original(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            def authorize(action, table, column, database, source):
                if action == sqlite3.SQLITE_READ and table != 'usage_meta': return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        before = hashlib.sha256((self.home / 'usage.sqlite3').read_bytes()).hexdigest()
        with patch.object(usage.sqlite3, 'connect', side_effect=guarded):
            data = usage.sync_status(self.home)
        self.assertTrue(data['available']); self.assertEqual(data['sync']['files_scanned'], 20)
        self.assertNotIn('PRIVATE', json.dumps(data))
        self.assertEqual(before, hashlib.sha256((self.home / 'usage.sqlite3').read_bytes()).hexdigest())
        self.assertFalse((self.home / 'usage-sync.lock').exists())
        self.assertTrue(any('usage_meta' in sql for sql in statements))

    def test_running_status_observes_lock_without_launching_another_sync(self):
        self.metadata()
        with usage.connect(self.home) as conn:
            usage.set_meta(conn, 'sync', dict(running=True, last_synced_at=1234, phase='importing', files_scanned=2))
        with (self.home / 'usage-sync.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data = usage.sync_status(self.home)
        self.assertTrue(data['sync']['running'])
        self.assertEqual(data['sync']['phase'], 'importing')
        self.assertFalse(usage.sync_status(self.home)['sync']['running'])

    def test_busy_sync_returns_progress_without_aggregation(self):
        self.metadata()
        with (self.home / 'usage-sync.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(usage, 'query', side_effect=AssertionError('full query during busy sync')):
                data = usage.sync(self.home)
        self.assertIn('sync', data); self.assertNotIn('summary', data)

    def test_native_mutation_bit_covers_deletes_corrections_and_rollbacks(self):
        with usage.connect(self.home) as conn:
            usage.track_native_mutations(conn)
            conn.execute("INSERT INTO usage_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ('one',1234,'codex','codex_session','m','t',1,1,0,0,2,'1970-01-01'))
            conn.commit()
            self.assertEqual(conn.execute('SELECT changed FROM usage_native_mutations').fetchone()[0], 1)
            usage.track_native_mutations(conn)
            conn.execute("UPDATE usage_requests SET output_tokens=2 WHERE id='one'")
            self.assertEqual(conn.execute('SELECT changed FROM usage_native_mutations').fetchone()[0], 1)
            conn.rollback()
            self.assertEqual(conn.execute('SELECT changed FROM usage_native_mutations').fetchone()[0], 0)
            conn.execute("DELETE FROM usage_requests WHERE id='one'")
            self.assertEqual(conn.execute('SELECT changed FROM usage_native_mutations').fetchone()[0], 1)

    def test_cli_sync_status_uses_small_reader(self):
        with patch.object(usage, 'sync_status', return_value={'sync': {'running': False}}) as status:
            with patch('builtins.print'):
                self.assertEqual(usage.main(['sync-status']), 0)
        status.assert_called_once_with()
