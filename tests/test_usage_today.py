from contextlib import contextmanager
from datetime import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import usage, usage_money


@contextmanager
def local_timezone(value):
    prior = os.environ.get('TZ')
    os.environ['TZ'] = value
    time.tzset()
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = prior
        time.tzset()


class TodaySummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.cc = self.home / 'missing-pricing.sqlite3'
        self.now = datetime.fromisoformat('2026-10-28T12:00:00+08:00').timestamp()

    def ledger(self, synced=True, **metadata):
        conn = usage.connect(self.home)
        report = dict(last_synced_at=self.now - 10 if synced else None,
                      running=False, errors=[], warnings=[], coverage=[])
        report.update(metadata)
        usage.set_meta(conn, 'sync', report)
        conn.commit()
        return conn

    def summary(self, **kwargs):
        return usage.today_summary(self.home, now=kwargs.pop('now', self.now), cc_path=self.cc, **kwargs)

    def price(self):
        usage_money.private_write(self.home / 'usage-display.json', {
            'currency': 'both', 'fx_mode': 'manual', 'manual_rate': 7.2,
            'model_overrides': {'known': dict(input=2, output=10, cache_read=.5, cache_creation=3)}})

    def test_absent_database_never_creates_files_or_fabricates_zero(self):
        missing = self.home / 'not-created'
        result = usage.today_summary(missing, now=self.now, cc_path=self.cc)
        self.assertFalse(result['available'])
        self.assertEqual(result['summary'], {})
        self.assertFalse(missing.exists())
        self.assertFalse(self.cc.exists())
        self.assertIn('currency', result['money']['prefs'])

    def test_initial_sync_required_even_with_partial_imported_rows(self):
        with self.ledger(synced=False) as conn:
            usage.write_request(conn, 'partial', self.now, 'codex', 'known', 'thread', [10, 2, 8, 0])
        result = self.summary()
        self.assertFalse(result['available'])
        self.assertEqual(result['summary'], {})

    def test_successful_empty_today_is_real_zero(self):
        conn = self.ledger()
        conn.close()
        result = self.summary()
        self.assertTrue(result['available'])
        self.assertEqual(result['summary']['requests'], 0)
        self.assertEqual(result['summary']['total_tokens'], 0)
        self.assertEqual(result['summary']['cost_usd'], 0)
        self.assertFalse(result['summary']['cost_partial'])

    def test_local_midnight_inclusion_and_future_exclusion_match_dashboard(self):
        with local_timezone('Asia/Taipei'):
            start = datetime(2026, 10, 28).timestamp()
            with self.ledger() as conn:
                for identifier, when in [('before', start - .001), ('midnight', start),
                                         ('now', self.now), ('future', self.now + .001)]:
                    usage.write_request(conn, identifier, when, 'codex', 'known', 'thread', [10, 2, 8, 0])
            result = self.summary()
            dashboard = usage.query(self.home, period='today', now=self.now)
            self.assertEqual(result['date'], '2026-10-28')
            self.assertEqual(result['day_start'], start)
            self.assertEqual(result['day_end'], start + 86400)
            self.assertEqual(result['utc_offset_seconds'], 8 * 3600)
            self.assertEqual(result['summary']['requests'], 2)
            self.assertEqual(result['summary']['total_tokens'], dashboard['summary']['total_tokens'])

    def test_dst_local_day_uses_calendar_boundaries(self):
        with local_timezone('America/Los_Angeles'):
            now = datetime(2026, 3, 8, 12).timestamp()
            conn = self.ledger()
            usage.set_meta(conn, 'sync', dict(last_synced_at=now - 10))
            conn.commit(); conn.close()
            result = self.summary(now=now)
            self.assertEqual(result['date'], '2026-03-08')
            self.assertEqual(result['day_end'] - result['day_start'], 23 * 3600)

    def test_all_app_disjoint_tokens_pricing_and_unknown_model_partial(self):
        self.price()
        with self.ledger() as conn:
            for app in ('codex', 'claude', 'opencode'):
                usage.write_request(conn, app, self.now, app, 'known', 'thread', [100, 20, 800, 50])
            usage.write_request(conn, 'unpriced', self.now, 'codex', 'unknown', 'thread', [10, 2, 8, 0])
        result = self.summary()
        summary = result['summary']
        self.assertEqual(summary['requests'], 4)
        self.assertEqual(summary['total_tokens'], 3 * 970 + 20)
        self.assertEqual(summary['input_tokens'], 310)
        self.assertEqual(summary['cache_read_tokens'], 2408)
        self.assertAlmostEqual(summary['cost_usd'], .00095 * 3)
        self.assertAlmostEqual(summary['cost_cny'], .00095 * 3 * 7.2)
        self.assertEqual(summary['priced_requests'], 3)
        self.assertEqual(summary['unpriced_requests'], 1)
        self.assertTrue(summary['cost_partial'])
        self.assertEqual(result['money']['prefs']['currency'], 'both')
        self.assertNotIn('logs', result)
        self.assertNotIn('filters', result)
        self.assertNotIn('days', result)

    def test_readonly_summary_does_not_scan_sources_initialize_or_fetch(self):
        self.price()
        conn = self.ledger()
        usage.write_request(conn, 'one', self.now, 'codex', 'known', 'thread', [100, 20, 800, 50])
        conn.execute('CREATE TABLE private_messages (body TEXT)')
        conn.execute('INSERT INTO private_messages VALUES (?)', ('MUST-NOT-READ',))
        conn.commit(); conn.close()
        db = self.home / 'usage.sqlite3'
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        files = set(self.home.iterdir())
        statements = []
        original_connect = sqlite3.connect

        def readonly_connect(*args, **kwargs):
            self.assertIn('mode=ro', str(args[0]))
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            def authorize(action, first, second, database, trigger):
                if action == sqlite3.SQLITE_READ and first == 'private_messages':
                    raise AssertionError('message body read')
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                              sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE):
                    raise AssertionError('write attempted')
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            return connection

        with patch.object(usage, 'connect', side_effect=AssertionError('initialization')), \
             patch.object(usage, 'sync', side_effect=AssertionError('sync')), \
             patch.object(usage, 'source_roots', side_effect=AssertionError('source scan')), \
             patch.object(usage_money, 'fetch_rate', side_effect=AssertionError('network')), \
             patch.object(sqlite3, 'connect', side_effect=readonly_connect):
            result = self.summary()
        self.assertTrue(result['available'])
        self.assertNotIn('MUST-NOT-READ', json.dumps(result))
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
        # SQLite can create its WAL coordination files even for mode=ro when
        # the database is writable by its owner. These are engine sidecars,
        # not application writes; immutable=1 would hide live committed WAL.
        sidecars = {self.home / 'usage.sqlite3-wal', self.home / 'usage.sqlite3-shm'}
        self.assertEqual(set(self.home.iterdir()) - sidecars, files - sidecars)
        self.assertFalse(any('SELECT *' in statement.upper() for statement in statements))

    def test_readonly_summary_includes_live_committed_wal(self):
        conn = self.ledger()
        self.addCleanup(conn.close)
        conn.execute('PRAGMA wal_autocheckpoint=0')
        usage.write_request(conn, 'live-wal', self.now, 'codex', 'known', 'thread', [10, 2, 8, 0])
        conn.commit()
        self.assertTrue((self.home / 'usage.sqlite3-wal').exists())
        result = self.summary()
        self.assertTrue(result['available'])
        self.assertEqual(result['summary']['requests'], 1)
        self.assertEqual(result['summary']['total_tokens'], 20)

    def test_cli_emits_summary_command_and_invalid_cache_stays_unknown(self):
        (self.home / 'usage.sqlite3').write_bytes(b'not a database')
        with patch.dict(os.environ, {'VIBECUE_HOME': str(self.home)}), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(usage.main(['today-summary']), 0)
            result = json.loads(output.getvalue())
        self.assertFalse(result['available'])
        self.assertEqual(result['summary'], {})
        self.assertIn('不可读', result['message'])

    def test_committed_wal_usage_is_visible_without_checkpoint_or_writer_changes(self):
        conn = self.ledger()
        self.addCleanup(conn.close)
        conn.execute('PRAGMA wal_autocheckpoint=0')
        usage.write_request(conn, 'wal-one', self.now, 'codex', 'known', 'thread', [10, 2, 8, 0])
        conn.commit()
        self.assertTrue((self.home / 'usage.sqlite3-wal').exists())
        before = {name: hashlib.sha256((self.home / name).read_bytes()).hexdigest()
                  for name in ('usage.sqlite3', 'usage.sqlite3-wal')}
        result = self.summary()
        self.assertTrue(result['available'])
        self.assertEqual(result['summary']['total_tokens'], 20)
        self.assertEqual(before, {name: hashlib.sha256((self.home / name).read_bytes()).hexdigest()
                                  for name in before})

    def test_sync_coverage_issues_survive_small_snapshot_and_stale_running_is_cleared(self):
        conn = self.ledger(running=True, errors=['partial-source'], warnings=['gap'],
                           coverage=[dict(app='codex', available=True, files=5, path='DO-NOT-EXPORT')])
        conn.close()
        (self.home / 'usage-sync.lock').touch()
        result = self.summary()
        self.assertTrue(result['available'])
        self.assertFalse(result['sync']['running'])
        self.assertEqual(result['sync']['errors'], ['partial-source'])
        self.assertIn('gap', result['sync']['warnings'])
        self.assertEqual(result['sync']['coverage'], [dict(app='codex', available=True, files=5)])
        self.assertNotIn('DO-NOT-EXPORT', json.dumps(result))
        self.assertIn('部分', result['message'])

    def test_invalid_success_marker_does_not_enable_zero(self):
        for marker in (True, -1, float('nan'), self.now + 61, '2026-10-28'):
            with self.subTest(marker=marker):
                conn = self.ledger(last_synced_at=marker)
                conn.close()
                result = self.summary()
                self.assertFalse(result['available'])
                self.assertEqual(result['summary'], {})


if __name__ == '__main__':
    unittest.main()
