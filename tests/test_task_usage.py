from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from vibecue import usage, usage_money as money, usage_pricing
from vibecue.task_usage import (TaskUsageCache, MAX_COST_ENTRIES, MAX_COST_GROUPS,
                                MAX_COST_ENTRY_GROUPS)


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

    def test_unchanged_poll_reuses_exact_numeric_cost_without_caching_state(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 20, 800, 50])])
        first = self.decorate()
        counts = self.cache.cache_metrics
        with patch.object(money, 'folded_cost', wraps=money.folded_cost) as fold, \
                patch.object(money, 'preferences', wraps=money.preferences) as prefs:
            for second in range(1, 20):
                self.assertEqual(self.decorate(now=self.now + second), first)
            self.assertEqual(fold.call_count, 0)
            self.assertEqual(prefs.call_count, 0)
        self.assertEqual(self.cache.cache_metrics['cost_calculations'], counts['cost_calculations'])
        self.assertGreater(self.cache.cache_metrics['cost_cache_hits'], counts['cost_cache_hits'])
        self.assertEqual(self.cache.cache_metrics['ledger_reads'], 1)

    def test_numeric_cache_returns_owned_source_and_fx_containers(self):
        first = self.decorate()[0]
        first['turn_usage']['price_sources'].append('poisoned')
        first['turn_usage']['fx']['rate'] = 999
        first['turn_usage']['total_tokens'] = 0
        next_row = self.decorate(now=self.now + 1)[0]
        self.assertNotIn('poisoned', next_row['turn_usage']['price_sources'])
        self.assertEqual(next_row['turn_usage']['fx']['rate'], 7)
        self.assertEqual(next_row['turn_usage']['total_tokens'], 970)

    def test_only_changed_numeric_evidence_recalculates_cost(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 20, 800, 50])])
        self.decorate()
        row = self.row()
        row['usage_by_model'][0]['input_tokens'] += 1
        row['input_tokens'] += 1
        row['total_tokens'] += 1
        with patch.object(money, 'folded_cost', wraps=money.folded_cost) as fold:
            value = self.decorate([row], now=self.now + 1)[0]
        self.assertEqual(fold.call_count, 1)
        self.assertEqual(value['turn_usage']['total_tokens'], 971)
        self.assertEqual(value['history_usage']['total_tokens'], 970)
        self.assertAlmostEqual(value['turn_usage']['cost_usd'], .000952)

    def test_ledger_correction_deletion_and_sync_flags_invalidate_before_ttl(self):
        self.ledger([('codex', 'thread', 'model-a', [100, 0, 0, 0])])
        self.assertEqual(self.decorate()[0]['history_usage']['total_tokens'], 100)
        with closing(usage.connect(self.home)) as conn:
            conn.execute('UPDATE usage_requests SET input_tokens=200,total_tokens=200 WHERE id=?', ('0',))
            conn.commit()
            corrected = self.decorate(now=self.now + 1)[0]['history_usage']
            self.assertEqual(corrected['total_tokens'], 200)
            self.assertAlmostEqual(corrected['cost_usd'], .0004)
            usage.set_meta(conn, 'sync', dict(last_synced_at=self.now - 10, running=True))
            conn.commit()
            running = self.decorate(now=self.now + 2)[0]['history_usage']
            self.assertTrue(running['stale'])
            self.assertEqual(running['tokens_status'], 'partial')
            self.assertEqual(running['total_tokens'], 200)
            conn.execute('DELETE FROM usage_requests WHERE id=?', ('0',))
            usage.set_meta(conn, 'sync', dict(last_synced_at=self.now - 10, running=False))
            conn.commit()
            removed = self.decorate(now=self.now + 3)[0]['history_usage']
            self.assertEqual(removed['total_tokens'], 0)
            self.assertEqual(removed['cost_usd'], 0)
            self.assertEqual(removed['tokens_status'], 'complete')

    def test_price_expiry_changes_freshness_at_exact_boundary_without_repricing_numbers(self):
        source = dict(id='cached', name='Cached price', kind='json', enabled=True,
                      url='https://prices.example/catalog.json')
        self.prefs.update(pricing_sources=[source], pricing_refresh_seconds=1800)
        money.private_write(self.home / 'usage-display.json', self.prefs)
        payload = dict(fingerprint=usage_pricing.fingerprint(source),
                       models={'model-a': dict(model='model-a', input=2, output=10, cache_read=.5, cache_creation=3)},
                       updated_at=self.now - 1795, attempted_at=self.now - 1795, error=False)
        usage_pricing.write_cache(self.home, source, payload)
        first = self.decorate()[0]['turn_usage']
        self.assertFalse(first['pricing_stale'])
        calculations = self.cache.cache_metrics['cost_calculations']
        expired = self.decorate(now=self.now + 5)[0]['turn_usage']
        self.assertTrue(expired['pricing_stale'])
        self.assertEqual(expired['cost_status'], 'partial')
        self.assertEqual(first['cost_usd'], expired['cost_usd'])
        self.assertEqual(self.cache.cache_metrics['cost_calculations'], calculations)

    def test_fx_expiry_is_not_hidden_by_thirty_second_snapshot_cache(self):
        self.prefs.update(fx_mode='auto')
        money.private_write(self.home / 'usage-display.json', self.prefs)
        money.private_write(self.home / 'usage-exchange.json', dict(base='USD', quote='CNY', rate=7,
            date=datetime.fromtimestamp(self.now, timezone.utc).date().isoformat(),
            updated_at=self.now - money.RATE_TTL + 5))
        first = self.decorate()[0]['turn_usage']
        self.assertFalse(first['fx']['stale'])
        expired = self.decorate(now=self.now + 5)[0]['turn_usage']
        self.assertTrue(expired['fx']['stale'])
        self.assertEqual(first['cost_cny'], expired['cost_cny'])

    def test_utc_date_boundary_rechecks_reference_age_before_ttl(self):
        self.now = (self.now // 86400 + 1) * 86400 - 2
        self.prefs.update(fx_mode='auto')
        money.private_write(self.home / 'usage-display.json', self.prefs)
        money.private_write(self.home / 'usage-exchange.json', dict(base='USD', quote='CNY', rate=7,
            date=(datetime.fromtimestamp(self.now, timezone.utc).date() - timedelta(days=7)).isoformat(),
            updated_at=self.now - 10))
        self.assertFalse(self.decorate()[0]['turn_usage']['fx']['stale'])
        self.assertTrue(self.decorate(now=self.now + 3)[0]['turn_usage']['fx']['stale'])

    def test_cached_rows_match_fresh_exact_reference_through_data_and_price_mutations(self):
        class FreshReference(TaskUsageCache):
            def _fold_cost(self, groups, prices, rate):
                return money.folded_cost(groups, prices, rate)
        reference = FreshReference(self.home, self.home / 'absent-cc.sqlite3', ttl=0)
        self.ledger([('codex', 'thread', 'model-a', [100, 20, 800, 50]),
                     ('codex', 'other', 'model-b', [200, 40, 1600, 100])])
        for second in range(6):
            if second == 1:
                self.prefs['manual_rate'] = 8
                money.private_write(self.home / 'usage-display.json', self.prefs)
            elif second == 2:
                self.prefs['model_overrides']['model-b']['output'] = 30
                money.private_write(self.home / 'usage-display.json', self.prefs)
            elif second in (3, 4):
                with closing(usage.connect(self.home)) as conn:
                    if second == 3:
                        conn.execute('UPDATE usage_requests SET output_tokens=60,total_tokens=1960 WHERE id=?', ('1',))
                    else:
                        conn.execute('DELETE FROM usage_requests WHERE id=?', ('0',))
                    conn.commit()
            rows = [self.row(), self.row(model='model-b', thread='other')]
            if second == 5:
                rows.reverse()
            self.assertEqual(self.decorate(rows, self.now + second), reference.decorate(rows, self.now + second))

    def test_price_priority_and_currency_changes_use_exact_selected_values(self):
        source = dict(id='cached', name='Cached price', kind='json', enabled=True,
                      url='https://prices.example/catalog.json')
        custom = next(dict(row, enabled=True) for row in usage_pricing.defaults() if row['kind'] == 'custom')
        self.prefs['pricing_sources'] = [source, custom]
        money.private_write(self.home / 'usage-display.json', self.prefs)
        usage_pricing.write_cache(self.home, source, dict(fingerprint=usage_pricing.fingerprint(source),
            models={'model-a': dict(model='model-a', input=4, output=20, cache_read=1, cache_creation=6)},
            updated_at=self.now - 10, attempted_at=self.now - 10, error=False))
        first = self.decorate()[0]['turn_usage']
        self.assertEqual(first['price_sources'], ['cached'])
        self.assertAlmostEqual(first['cost_usd'], .0019)
        self.prefs['pricing_sources'] = [custom, source]
        self.prefs['currency'] = 'USD'
        money.private_write(self.home / 'usage-display.json', self.prefs)
        changed = self.decorate(now=self.now + 1)[0]['turn_usage']
        self.assertEqual(changed['price_sources'], [custom['id']])
        self.assertEqual(changed['currency'], 'USD')
        self.assertAlmostEqual(changed['cost_usd'], .00095)

    def test_equal_amount_with_different_price_source_keeps_new_provenance(self):
        self.cache._money_snapshot(self.now)
        prices = {model: dict(row) for model, row in self.cache._prices.items()}
        groups = self.row()['usage_by_model']
        first = self.cache._fold_cost(groups, prices, 7)
        prices['model-a']['source'] = 'replacement-source'
        second = self.cache._fold_cost(groups, prices, 7)
        self.assertEqual(first['cost_usd'], second['cost_usd'])
        self.assertEqual(first['cost_cny'], second['cost_cny'])
        self.assertNotEqual(first['price_sources'], second['price_sources'])
        self.assertEqual(second['price_sources'], ['replacement-source'])

    def test_group_order_is_not_canonicalized_before_decimal_folding(self):
        self.cache._money_snapshot(self.now)
        groups = [self.row()['usage_by_model'][0], self.row(model='model-b')['usage_by_model'][0]]
        first = self.cache._fold_cost(groups, self.cache._prices, 7)
        reversed_groups = list(reversed(groups))
        second = self.cache._fold_cost(reversed_groups, self.cache._prices, 7)
        self.assertEqual(first, money.folded_cost(groups, self.cache._prices, 7))
        self.assertEqual(second, money.folded_cost(reversed_groups, self.cache._prices, 7))
        self.assertEqual(self.cache.cache_metrics['cost_calculations'], 2)
        self.assertEqual(self.cache.cache_metrics['cost_entries'], 2)

    def test_numeric_cost_cache_has_entry_and_total_group_limits(self):
        self.cache._money_snapshot(self.now)
        prices = self.cache._prices
        for index in range(MAX_COST_ENTRIES + 4):
            group = dict(self.row()['usage_by_model'][0], input_tokens=index)
            self.cache._fold_cost([group], prices, 7)
        self.assertLessEqual(self.cache.cache_metrics['cost_entries'], MAX_COST_ENTRIES)
        for index in range(150):
            groups = [dict(self.row()['usage_by_model'][0], input_tokens=index + group) for group in range(32)]
            self.cache._fold_cost(groups, prices, 7)
        self.assertLessEqual(self.cache.cache_metrics['cost_groups'], MAX_COST_GROUPS)
        too_many = self.row()['usage_by_model'] * (MAX_COST_ENTRY_GROUPS + 1)
        count = self.cache.cache_metrics['cost_entries']
        expected = money.folded_cost(too_many, prices, 7)
        self.assertEqual(self.cache._fold_cost(too_many, prices, 7), expected)
        self.assertEqual(self.cache.cache_metrics['cost_entries'], count)

    def test_zero_ttl_reference_mode_still_refreshes_every_snapshot(self):
        self.cache = TaskUsageCache(self.home, self.home / 'absent-cc.sqlite3', ttl=0)
        with patch.object(money, 'settings', wraps=money.settings) as settings:
            first = self.decorate()
            second = self.decorate(now=self.now + 1)
        self.assertEqual(first, second)
        self.assertEqual(settings.call_count, 2)

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
