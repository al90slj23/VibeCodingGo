import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from vibecue.usage import connect, write_request
from vibecue.usage_opencode import discover, normalize_message, scan


def assistant(identifier='msg-one', session='ses-one', output=20, reasoning=7,
              created=1790816400000, provider='openai'):
    return {'id': identifier, 'sessionID': session, 'role': 'assistant',
            'time': {'created': created}, 'modelID': 'model-one', 'providerID': provider,
            'tokens': {'input': 100, 'output': output, 'reasoning': reasoning,
                       'cache': {'read': 900, 'write': 50}}}


class OpenCodeUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'opencode'
        self.root.mkdir()
        self.path = self.root / 'opencode.db'
        self.home = self.base / 'home'
        self.ledger = connect(self.home)
        self.addCleanup(self.ledger.close)

    def database(self, version=1, path=None):
        conn = sqlite3.connect(path or self.path)
        if version == 1:
            conn.execute('''CREATE TABLE IF NOT EXISTS message (
                id TEXT PRIMARY KEY,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT)''')
        else:
            conn.execute('''CREATE TABLE IF NOT EXISTS session_message (
                id TEXT PRIMARY KEY,session_id TEXT,type TEXT,seq INTEGER,time_created INTEGER,time_updated INTEGER,data TEXT)''')
        conn.commit()
        self.addCleanup(conn.close)
        return conn

    def insert(self, conn, value, version=1):
        identifier, session = value['id'], value['sessionID']
        created = value.get('time', {}).get('created', 1790816400000)
        if version == 1:
            conn.execute('INSERT OR REPLACE INTO message VALUES (?,?,?,?,?)',
                         (identifier, session, created, created, json.dumps(value)))
        else:
            conn.execute('INSERT OR REPLACE INTO session_message VALUES (?,?,?,?,?,?,?)',
                         (identifier, session, value.get('type', 'assistant'), 1, created, created, json.dumps(value)))
        conn.commit()

    def run_scan(self, path=None, kind='sqlite', writer=write_request):
        report = {}
        with self.ledger:
            scan(self.ledger, path or self.path, report, writer, kind=kind)
        return report

    def rows(self):
        return list(self.ledger.execute("SELECT * FROM usage_requests WHERE app='opencode' ORDER BY timestamp,id"))

    def test_numeric_normalization_includes_reasoning_once_and_keeps_fresh_input(self):
        record = normalize_message(assistant())
        self.assertEqual(record['counts'], [100, 27, 900, 50])
        self.assertEqual(sum(record['counts']), 1077)
        self.assertEqual(record['source'], 'opencode_provider:openai')
        self.assertEqual(record['timestamp'], 1790816400)

    def test_legacy_negative_visible_output_requires_exact_nonnegative_total(self):
        for visible, reasoning in [(-827, 917), (-251, 717), (-109, 127), (-338, 634)]:
            with self.subTest(visible=visible):
                value = assistant(output=visible, reasoning=reasoning)
                value['tokens']['total'] = 100 + visible + reasoning + 900 + 50
                record = normalize_message(value)
                self.assertEqual(record['counts'][1], visible + reasoning)
                self.assertEqual(sum(record['counts']), value['tokens']['total'])
                self.assertTrue(record['legacy_negative_output'])
                value['tokens']['total'] += 1
                self.assertIsNone(normalize_message(value))
                value['tokens'].pop('total')
                self.assertIsNone(normalize_message(value))

    def test_v1_sqlite_usage_and_provider_are_read_only(self):
        source = self.database()
        value = assistant()
        value['private_text'] = 'PRIVATE-ASSISTANT-TEXT-NOT-A-USAGE-FIELD'
        source.execute('CREATE TABLE auth_secret (value TEXT)')
        source.execute("INSERT INTO auth_secret VALUES ('PRIVATE-CREDENTIAL-NEVER-QUERY')")
        self.insert(source, value)
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        report = self.run_scan()
        self.assertEqual(report['imported'], 1)
        self.assertEqual(self.rows()[0]['total_tokens'], 1077)
        self.assertEqual(self.rows()[0]['source'], 'opencode_provider:openai')
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(), before)
        self.ledger.commit()
        dump = '\n'.join(self.ledger.iterdump())
        self.assertNotIn('PRIVATE-ASSISTANT', dump)
        self.assertNotIn('PRIVATE-CREDENTIAL', dump)

    def test_unchanged_source_is_skipped_and_restart_is_idempotent(self):
        source = self.database()
        self.insert(source, assistant())
        self.run_scan()
        self.assertEqual(self.run_scan(), {})
        # A fresh SQLite connection is sufficient: no process-local cursor.
        with connect(self.home) as restarted:
            scan(restarted, self.path, {}, write_request)
        self.assertEqual(len(self.rows()), 1)

    def test_streaming_message_final_snapshot_updates_one_request(self):
        source = self.database()
        self.insert(source, assistant(output=2))
        self.run_scan()
        self.insert(source, assistant(output=200))
        report = self.run_scan()
        self.assertEqual(report['updated'], 1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['output_tokens'], 207)

    def test_historical_backfill_older_than_previous_watermark_is_imported(self):
        source = self.database()
        self.insert(source, assistant())
        self.run_scan()
        self.insert(source, assistant(identifier='msg-older', created=1780816400000))
        self.run_scan()
        self.assertEqual(len(self.rows()), 2)

    def test_active_wal_is_included(self):
        source = self.database()
        source.execute('PRAGMA journal_mode=WAL')
        source.execute('PRAGMA wal_autocheckpoint=0')
        self.insert(source, assistant())
        self.run_scan()
        # The new message is only in the live WAL, not a checkpointed DB copy.
        self.insert(source, assistant(identifier='msg-wal'))
        self.run_scan()
        self.assertEqual(len(self.rows()), 2)

    def test_v2_compaction_completed_and_failed_usage_is_included(self):
        source = self.database(version=2)
        for status in ('completed', 'failed', 'pending'):
            value = assistant(identifier='compaction-' + status)
            value.pop('role'); value['type'] = 'compaction'; value['status'] = status
            value['model'] = value.pop('modelID')
            self.insert(source, value, version=2)
        self.run_scan()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(sum(row['total_tokens'] for row in self.rows()), 2154)
        self.assertTrue(all(row['model'] == 'model-one' for row in self.rows()))

    def test_v2_model_shape_and_v1_migration_copy_do_not_double_count(self):
        legacy = self.database()
        self.insert(legacy, assistant())
        current = self.database(version=2)
        value = assistant(output=30)
        value.pop('role'); value['type'] = 'assistant'
        value['model'] = {'id': value.pop('modelID'), 'providerID': value.pop('providerID')}
        value['content'] = [{'type': 'text', 'text': 'PRIVATE-V2-BODY'}]
        self.insert(current, value, version=2)
        self.run_scan()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]['output_tokens'], 37)
        self.assertEqual(self.rows()[0]['model'], 'model-one')
        self.assertNotIn('PRIVATE-V2-BODY', '\n'.join(self.ledger.iterdump()))

    def test_json_migration_copy_and_sqlite_copy_share_stable_id(self):
        source = self.database()
        value = assistant()
        self.insert(source, value)
        self.run_scan()
        legacy = self.root / 'storage/message/ses-one/msg-one.json'
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(value))
        self.run_scan(legacy, kind='json')
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(discover(self.root), [('sqlite', self.path.resolve()), ('json', legacy.resolve())])

    def test_only_message_storage_is_discovered(self):
        (self.root / 'auth.json').write_text('do not read this')
        (self.root / 'storage/part').mkdir(parents=True)
        (self.root / 'storage/part/part.json').write_text('do not read this')
        self.assertEqual(discover(self.root), [])

    def test_malformed_missing_negative_and_user_usage_do_not_fabricate_rows(self):
        for field, value in [('input', -1), ('input', True), ('output', None), ('reasoning', '7')]:
            record = assistant()
            record['tokens'][field] = value
            self.assertIsNone(normalize_message(record))
        self.assertIsNone(normalize_message({'role': 'user', **{'tokens': assistant()['tokens']}}))
        self.assertIsNone(normalize_message({**assistant(), 'id': ''}))
        self.assertIsNone(normalize_message({**assistant(), 'time': {'created': 'invalid'}}))
        source = self.database()
        source.execute("INSERT INTO message VALUES ('bad','ses',1,1,'not-json')")
        source.commit()
        report = self.run_scan()
        self.assertEqual(report['malformed_records'], 1)
        self.assertEqual(len(self.rows()), 0)

    def test_source_scan_and_cursor_roll_back_together_on_writer_failure(self):
        source = self.database()
        self.insert(source, assistant())
        def fail(*args, **kwargs):
            raise RuntimeError('fixture interrupted writer')
        with self.assertRaises(RuntimeError):
            self.run_scan(writer=fail)
        self.run_scan()
        self.assertEqual(len(self.rows()), 1)

    def test_zero_usage_and_session_step_aggregates_are_not_extra_requests(self):
        source = self.database()
        value = assistant()
        value['tokens'] = {'input': 0, 'output': 0, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}}
        self.insert(source, value)
        source.execute('CREATE TABLE session (tokens_input INTEGER,tokens_output INTEGER)')
        source.execute('INSERT INTO session VALUES (999999,999999)')
        source.execute('CREATE TABLE part (data TEXT)')
        source.execute('INSERT INTO part VALUES (?)', (json.dumps({'type': 'step-finish', 'tokens': assistant()['tokens']}),))
        source.commit()
        self.run_scan()
        self.assertEqual(len(self.rows()), 0)


if __name__ == '__main__':
    unittest.main()
