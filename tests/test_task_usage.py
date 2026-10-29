from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from vibecue import usage, usage_money as money, usage_pricing
from vibecue.task_usage import TaskUsageCache


class TaskUsageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.now = 1_793_300_000
        self.prefs = {'currency': 'both', 'fx_mode': 'manual', 'manual_rate': 7,
                      'pricing_sources': [dict(source, enabled=source['kind'] == 'custom')
                                          for source in usage_pricing.defaults()],
                      'model_overrides': {
                          'model-a': dict(input=2, output=10, cache_read=.5, cache_creation=3),
                          'model-b': dict(input=4, output=20, cache_read=1, cache_creation=6),
                      }}
        money.private_write(self.home / 'usage-display.json', self.prefs)
        self.cache = TaskUsageCache(self.home, self.home / 'absent-cc.sqlite3')

    def row(self, model='model-a', thread='thread', **updates):
        group = dict(model=model, requests=1, input_tokens=100, output_tokens=20,
                     cache_read_tokens=800, cache_creation_tokens=50, total_tokens=970)
        value = dict(thread_id=thread, model='later-selected-model', total_tokens=970,
                     input_tokens=100, output_tokens=20, cache_read_tokens=800, cache_creation_tokens=50,
                     request_count=1, tokens_status='complete', last_activity=self.now - 20,
                     usage_by_model=[group], content_truncated=True)
        value.update(updates)
        return value

    def ledger(self, groups=(), synced=True, **metadata):
        with closing(usage.connect(self.home)) as conn:
            if synced:
                usage.set_meta(conn, 'sync', dict(last_synced_at=self.now - 10, running=False, **metadata))
            conn.execute('INSERT OR REPLACE INTO usage_codex_sessions VALUES (?,?,?,?)', ('thread', self.now - 5000, None, 1))
            for index, (app, thread, model, counts) in enumerate(groups):
                usage.write_request(conn, str(index), self.now - 100 - index, app, model, thread, counts)
            conn.commit()

    def decorate(self, rows=None, now=None):
        with (patch.object(money, 'fetch_rate', side_effect=AssertionError('no network')),
              patch.object(usage, 'sync', side_effect=AssertionError('no source scans'))):
            return self.cache.decorate([self.row()] if rows is None else rows, self.now if now is None else now)

    def test_turn_prices_observed_models_and_mixed_unknown_subtotal(self):
        row = self.row()
        row['usage_by_model'].append(dict(row['usage_by_model'][0], model='model-b'))
        row.update(total_tokens=1940, request_count=2)
        value = self.decorate([row])[0]['turn_usage']
        self.assertAlmostEqual(value['cost_usd'], .00285)
        self.assertAlmostEqual(value['cost_cny'], .01995)
        self.assertEqual(value['priced_requests'], 2)
        self.assertEqual(value['cost_status'], 'complete')
        row['usage_by_model'][1]['model'] = 'unknown'
        value = self.decorate([row])[0]['turn_usage']
        self.assertAlmostEqual(value['cost_usd'], .00095)
        self.assertEqual(value['unpriced_requests'], 1)
        self.assertEqual(value['cost_status'], 'partial')

    def test_unknown_unpriced_and_partial_zero_have_distinct_values(self):
        rows = [self.row(tokens_status='unknown', total_tokens=None, request_count=None, usage_by_model=[]),
                self.row(model='not-priced'),
                self.row(tokens_status='partial', total_tokens=0, request_count=0, usage_by_model=[])]
        values = [row['turn_usage'] for row in self.decorate(rows)]
        self.assertIsNone(values[0]['cost_usd'])
        self.assertEqual(values[0]['cost_status'], 'unknown')
        self.assertIsNone(values[1]['cost_usd'])
        self.assertEqual(values[1]['cost_status'], 'unpriced')
        self.assertIsNone(values[2]['cost_usd'])
        self.assertEqual(values[2]['tokens_status'], 'partial')

    def test_legacy_snapshot_does_not_use_latest_model_to_price_old_usage(self):
        row = self.row(model='model-a')
        row.pop('usage_by_model')
        row['model'] = 'model-b'
        value = self.decorate([row])[0]['turn_usage']
        self.assertEqual(value['total_tokens'], 970)
        self.assertIsNone(value['cost_usd'])
        self.assertEqual(value['cost_status'], 'unpriced')

    def test_missing_and_unsynced_ledger_never_create_or_display_false_zero(self):
        value = self.decorate()[0]['history_usage']
        self.assertEqual(value['tokens_status'], 'unknown')
        self.assertIsNone(value['total_tokens'])
        self.assertFalse((self.home / 'usage.sqlite3').exists())
        self.ledger([('codex', 'thread', 'model-a', [100, 20, 800, 50])], synced=False)
        value = self.decorate()[0]['history_usage']
        self.assertIsNone(value['total_tokens'])
        self.assertIsNone(value['cost_usd'])

    def test_lifetime_is_thread_scoped_not_truncated_window_or_other_app(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 20, 800, 50]),
                     ('codex', 'thread', 'model-b', [200, 40, 1600, 100]),
                     ('codex', 'another-thread', 'model-a', [9000, 0, 0, 0]),
                     ('claude', 'thread', 'model-a', [9000, 0, 0, 0])])
        value = self.decorate()[0]['history_usage']
        self.assertEqual(value['total_tokens'], 2910)
        self.assertEqual(value['request_count'], 2)
        self.assertAlmostEqual(value['cost_usd'], .00475)
        self.assertEqual(value['tokens_status'], 'complete')
        self.assertEqual(value['updated_at'], self.now - 10)
        self.assertEqual(value['provenance'], 'synced_history')

    def test_live_turn_is_never_blindly_added_to_history(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 20, 800, 50])])
        row = self.decorate([self.row(last_activity=self.now)])[0]
        self.assertEqual(row['turn_usage']['total_tokens'], 970)
        self.assertEqual(row['history_usage']['total_tokens'], 970)
        self.assertTrue(row['history_usage']['stale'])
        self.assertEqual(row['history_usage']['tokens_status'], 'partial')

    def test_known_empty_thread_zero_differs_from_missing_thread(self):
        self.ledger()
        rows = self.decorate([self.row(), self.row(thread='missing')])
        self.assertEqual(rows[0]['history_usage']['total_tokens'], 0)
        self.assertEqual(rows[0]['history_usage']['cost_usd'], 0)
        self.assertIsNone(rows[1]['history_usage']['total_tokens'])
        self.assertIsNone(rows[1]['history_usage']['cost_usd'])

    def test_partial_and_overdue_ledger_are_explicit(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 0, 0, 0])], warnings=['historical gaps'])
        row = self.decorate()[0]['history_usage']
        self.assertEqual(row['tokens_status'], 'partial')
        row = self.decorate(now=self.now + 1300)[0]['history_usage']
        self.assertTrue(row['stale'])
        self.assertEqual(row['tokens_status'], 'partial')

    def test_cache_is_stable_reprices_settings_and_keeps_ledger_unchanged(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 20, 800, 50])])
        path = self.home / 'usage.sqlite3'
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch('vibecue.task_usage.sqlite3.connect', wraps=sqlite3.connect) as connect:
            first = self.decorate()
            second = self.decorate(now=self.now + 1)
            self.assertEqual(first, second)
            self.assertEqual(connect.call_count, 1)
        self.prefs['model_overrides']['model-a']['input'] = 4
        self.prefs['currency'] = 'USD'
        self.prefs['manual_rate'] = 8
        money.private_write(self.home / 'usage-display.json', self.prefs)
        changed = self.decorate(now=self.now + 2)[0]
        self.assertAlmostEqual(changed['turn_usage']['cost_usd'], .00115)
        self.assertAlmostEqual(changed['history_usage']['cost_cny'], .0092)
        self.assertEqual(changed['turn_usage']['currency'], 'USD')
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertNotIn('usage_by_model', changed)
        self.assertFalse((self.home / 'absent-cc.sqlite3').exists())

    def test_db_wal_commit_invalidates_cache_before_ttl(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 0, 0, 0])])
        self.assertEqual(self.decorate()[0]['history_usage']['total_tokens'], 100)
        with closing(usage.connect(self.home)) as conn:
            usage.write_request(conn, 'new-request', self.now - 5, 'codex', 'model-b', 'thread', [200, 0, 0, 0])
            conn.commit()
            self.assertEqual(self.decorate(now=self.now + 1)[0]['history_usage']['total_tokens'], 300)

    def test_public_price_cache_change_reprices_before_ttl_without_network(self):
        source = dict(id='cached', name='Cached price', kind='json', enabled=True,
                      url='https://prices.example/catalog.json')
        self.prefs['pricing_sources'] = [source]
        money.private_write(self.home / 'usage-display.json', self.prefs)
        payload = dict(fingerprint=usage_pricing.fingerprint(source),
                       models={'model-a': dict(model='model-a', input=2, output=10, cache_read=.5, cache_creation=3)},
                       updated_at=self.now - 10, attempted_at=self.now - 10, error=False)
        usage_pricing.write_cache(self.home, source, payload)
        first = self.decorate()[0]['turn_usage']
        self.assertAlmostEqual(first['cost_usd'], .00095)
        payload['models']['model-a']['input'] = 4
        usage_pricing.write_cache(self.home, source, payload)
        second = self.decorate(now=self.now + 1)[0]['turn_usage']
        self.assertAlmostEqual(second['cost_usd'], .00115)
        self.assertEqual(second['price_sources'], ['cached'])

    def test_settings_are_loaded_once_for_many_rows_and_reused_on_poll(self):
        with patch.object(money, 'settings', wraps=money.settings) as settings:
            self.decorate([self.row(thread='thread-' + str(index)) for index in range(256)])
            self.decorate(now=self.now + 5)
        self.assertEqual(settings.call_count, 1)

    def test_transient_read_error_keeps_only_explicitly_stale_known_subtotal(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 0, 0, 0])])
        self.decorate()
        with patch('vibecue.task_usage.sqlite3.connect', side_effect=sqlite3.OperationalError('busy')):
            value = self.decorate(now=self.now + 31)[0]['history_usage']
        self.assertEqual(value['total_tokens'], 100)
        self.assertTrue(value['stale'])
        self.assertEqual(value['tokens_status'], 'partial')


if __name__ == '__main__':
    unittest.main()
