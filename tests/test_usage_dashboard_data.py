"""Dashboard queries use the durable numeric ledger, never a transcript scan."""
from contextlib import contextmanager
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import usage, usage_money


@contextmanager
def timezone(value):
    old = os.environ.get('TZ'); os.environ['TZ'] = value; time.tzset()
    try:
        yield
    finally:
        if old is None: os.environ.pop('TZ', None)
        else: os.environ['TZ'] = old
        time.tzset()


class DashboardDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.zone = timezone('Asia/Taipei'); self.zone.__enter__(); self.addCleanup(self.zone.__exit__, None, None, None)
        self.now = datetime(2026, 10, 29, 12, 34).timestamp()
        self.conn = usage.connect(self.home); self.addCleanup(self.conn.close)
        usage_money.private_write(self.home / 'usage-display.json', {
            'fx_mode': 'manual', 'manual_rate': 7,
            'pricing_sources': [{'id':'custom','name':'手动价格','kind':'custom','enabled':True}],
            'model_overrides': {'known': dict(input=2, output=10, cache_read=.5, cache_creation=3)}})
        self.network = patch.object(usage_money, 'fetch_rate', side_effect=AssertionError('query network'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.add('real', self.now - 60, 'known', [100, 20, 800, 50], provider='actual', name='Provider', status=500)
        self.add('session', self.now - 3600, 'known', [10, 2, 8, 0], provider='_codex_session', status=None)
        self.add('yesterday', datetime(2026, 10, 28, 23).timestamp(), 'known', [20, 4, 16, 0], provider='actual', name='Provider', status=200)
        self.add('old', self.now - 500 * 86400, 'known', [1, 1, 0, 0])
        self.add('unpriced', self.now - 30, 'unknown-model', [5, 1, 0, 0])
        self.conn.commit()

    def add(self, key, when, model, counts, provider=None, name=None, status=None):
        usage.write_request(self.conn, key, when, 'codex', model, key, counts)
        if provider is not None:
            self.conn.execute('INSERT INTO usage_request_details(request_id,provider_id,provider_name,status_code) VALUES(?,?,?,?)',
                              (key, provider, name, status))

    def query(self, **kwargs):
        return usage.query(self.home, now=self.now, **kwargs)

    def test_custom_selection_keeps_full_heatmap_and_marks_other_dates(self):
        full = self.query(); chosen = self.query(period='custom', start_date='2026-10-28', end_date='2026-10-28')
        self.assertEqual(chosen['summary']['requests'], 1)
        self.assertEqual(chosen['heatmap_summary'], full['heatmap_summary'])
        selected = [r for r in chosen['days'] if r['in_selected_range']]
        self.assertEqual([r['date'] for r in selected], ['2026-10-28'])
        self.assertEqual(sum(r['requests'] for r in chosen['days']), 4)
        required = {'date','tokens','total_tokens','requests',*usage.TOKEN_COLUMNS,'cost_usd','cost_cny',
                    'priced_requests','unpriced_requests','cache_hit_rate','in_selected_range'}
        self.assertTrue(all(required <= row.keys() for row in chosen['days']))

    def test_heatmap_expands_in_exact_53_week_blocks_and_reports_older(self):
        first = self.query(); older = self.query(heatmap_weeks=106)
        self.assertTrue(first['heatmap_range']['has_older'])
        self.assertFalse(older['heatmap_range']['has_older'])
        a = datetime.fromisoformat(first['heatmap_range']['start_date'])
        b = datetime.fromisoformat(older['heatmap_range']['start_date'])
        self.assertEqual((a-b).days, 371)
        self.assertEqual(older['heatmap_summary']['requests'], 5)

    def test_provider_keys_and_group_counts_are_stable_across_tabs(self):
        logs = self.query(); data = self.query(view='providers', page_size=1)
        self.assertEqual(data['group_counts'], logs['group_counts'])
        self.assertEqual(data['group_counts'], dict(logs=5, models=2, sources=1, providers=2))
        self.assertEqual(data['pagination']['total'], 2)
        rows = self.query(view='providers')['providers']
        self.assertEqual({r['id'] for r in rows}, {'codex|actual', 'codex|source:codex_session'})
        self.assertEqual(sum(r['requests'] for r in rows), 5)
        filtered = self.query(provider='codex|actual')
        self.assertEqual(filtered['summary']['requests'], 2)
        self.assertEqual(filtered['heatmap_summary']['requests'], 2)
        self.assertEqual(self.query(provider='codex|source:codex_session')['summary']['requests'], 3)
        self.assertNotIn('codex|_codex_session', {r['id'] for r in data['filters']['providers']})

    def test_http_status_exact_ranges_and_null_never_guess_success(self):
        for status, count in [('success',1),('error',1),('500',1),('429',0),('unknown',3)]:
            with self.subTest(status=status):
                result = self.query(status=status)
                self.assertEqual(result['summary']['requests'], count)
                self.assertEqual(result['heatmap_summary']['requests'], count - (1 if status=='unknown' else 0))

    def test_hour_trends_include_empty_hours_and_preserve_unknown_money(self):
        today = self.query(period='today')
        self.assertEqual(today['trend_granularity'], 'hour')
        self.assertEqual(len(today['trend_points']), 13)
        self.assertEqual(today['trend_points'][0]['label'], '00:00')
        self.assertEqual(sum(r['requests'] for r in today['trend_points']), today['summary']['requests'])
        self.assertEqual(self.query(period='yesterday')['trend_points'][-1]['label'], '23:00')
        self.assertEqual(len(self.query(period='yesterday')['trend_points']), 24)
        unknown = self.query(period='today', model='unknown-model')
        self.assertIsNone(unknown['trend_points'][-1]['cost_usd'])
        self.assertEqual(unknown['trend_points'][0]['cost_usd'], 0)

    def test_rolling_day_boundary_and_all_history_trend_match_summary(self):
        self.add('before-window', self.now - 7*86400 - 60, 'known', [1, 1, 0, 0])
        self.add('inside-window', self.now - 7*86400 + 60, 'known', [2, 2, 0, 0]); self.conn.commit()
        for period in ('7d','30d','90d','month','year','all','1d'):
            with self.subTest(period=period):
                result = self.query(period=period)
                self.assertEqual(sum(row['total_tokens'] for row in result['trend_points']), result['summary']['total_tokens'])
        all_data = self.query()
        self.assertGreater(len(all_data['trend_points']), len(all_data['days']))

    def test_hour_boundaries_follow_local_time_and_dst(self):
        with timezone('America/Los_Angeles'):
            data = usage.query(self.home, period='yesterday', now=datetime(2026,3,9,12).timestamp())
            self.assertEqual(len(data['trend_points']), 23)
            self.assertNotIn('02:00', [row['label'] for row in data['trend_points']])
        with timezone('Asia/Kathmandu'):
            data = usage.query(self.home, period='today', now=datetime(2026,10,29,12,34).timestamp())
            self.assertEqual(len(data['trend_points']), 13)
            self.assertTrue(all(row['label'].endswith(':00') for row in data['trend_points']))

    def test_money_components_keep_unknowns_nullable_and_sum_to_total(self):
        rows = {r['id']: r for r in self.query()['logs']}
        real = rows['real']
        self.assertAlmostEqual(sum(real[key+'_cost_usd'] for key in usage_money.RATE_KEYS), real['cost_usd'])
        self.assertAlmostEqual(sum(real[key+'_cost_cny'] for key in usage_money.RATE_KEYS), real['cost_cny'])
        self.assertIsNone(rows['unpriced']['input_cost_usd'])
        self.assertEqual(rows['unpriced']['cache_read_cost_usd'], 0)
        self.assertIsNone(rows['unpriced']['cost_usd'])

    def test_archived_residual_increases_aggregates_without_fake_log_rows(self):
        when = datetime(2026,10,28,12).timestamp()
        self.conn.execute('INSERT INTO usage_archived_daily VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          ('archive',when,'gemini','cc_daily','known','2026-10-28',12,100,20,800,50,970,'gemini-api','Gemini'))
        self.conn.commit()
        result = self.query()
        self.assertEqual(result['summary']['requests'], 17)
        self.assertEqual(result['summary']['archived_requests'], 12)
        self.assertEqual(result['group_counts']['logs'], 5)
        self.assertEqual(result['pagination']['total'], 5)
        self.assertEqual(len(result['logs']), 5)
        self.assertEqual(sum(r['requests'] for r in result['trend_points']), 17)
        self.assertEqual(self.query(status='success')['summary']['requests'], 1)
        self.assertEqual(self.query(status='unknown')['summary']['requests'], 15)
        provider = self.query(provider='gemini|gemini-api',view='providers')
        self.assertEqual(provider['providers'][0]['requests'], 12)
        self.assertEqual(provider['group_counts']['logs'], 0)

    def test_cli_accepts_all_dashboard_arguments_and_rejects_bad_dates(self):
        with patch.dict(os.environ, {'VIBECUE_HOME': str(self.home)}), patch('sys.stdout', new_callable=io.StringIO) as output:
            code = usage.main(['query','--range','custom','--start-date','2026-10-28','--end-date','2026-10-28',
                               '--heatmap-weeks','106','--provider','codex|actual','--status','200','--view','providers'])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['summary']['requests'], 1)
        for kwargs in ({'period':'custom','start_date':'2026-02-30','end_date':'2026-10-28'}, {'status':'600'}):
            with self.assertRaises(ValueError): self.query(**kwargs)


if __name__ == '__main__': unittest.main()
