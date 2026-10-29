from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from vibecue import usage_money as money


class MoneyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.cc = Path(self.temp.name) / 'cc.sqlite3'
        with sqlite3.connect(self.cc) as db:
            db.execute('CREATE TABLE model_pricing(model_id TEXT PRIMARY KEY,display_name TEXT,input_cost_per_million TEXT,output_cost_per_million TEXT,cache_read_cost_per_million TEXT,cache_creation_cost_per_million TEXT)')
            db.execute('CREATE TABLE settings(key TEXT,value TEXT)')
            db.execute('INSERT INTO settings VALUES (?,?)', ('private', 'DO-NOT-READ'))
            db.execute('INSERT INTO model_pricing VALUES (?,?,?,?,?,?)', ('model-a', 'Model A', '2', '10', '.5', '3'))
        self.now = datetime(2026, 10, 28, 8, tzinfo=timezone.utc).timestamp()

    def rates(self, **updates):
        return dict(input=2, output=10, cache_read=.5, cache_creation=3, **updates)

    def row(self, model='model-a', requests=1, **updates):
        return dict(model=model, source='codex_session', app='codex', requests=requests,
                    input_tokens=100, output_tokens=20, cache_read_tokens=800,
                    cache_creation_tokens=50, **updates)

    def cache(self, age=0, **updates):
        data = dict(base='USD', quote='CNY', rate=7.1, date='2026-10-28',
                    updated_at=self.now-age, attempted_at=self.now-age)
        data.update(updates)
        money.private_write(self.home/'usage-exchange.json', data)

    def settings(self):
        return money.settings(self.home, self.cc, self.now)

    def test_safe_exact_cc_catalog_read_only(self):
        before = hashlib.sha256(self.cc.read_bytes()).hexdigest()
        snapshot = self.settings()
        self.assertEqual(snapshot['pricing_status'], 'available')
        self.assertEqual(snapshot['pricing'], [dict(model='model-a',name='Model A',source='ccswitch',input=2,output=10,cache_read=.5,cache_creation=3,
                                                  source_name='CC Switch 本机价格',source_updated_at=None,source_stale=False)])
        self.assertNotIn('DO-NOT-READ', json.dumps(snapshot))
        self.assertEqual(hashlib.sha256(self.cc.read_bytes()).hexdigest(), before)
        prices, _ = money.catalog(self.home, self.cc)
        self.assertIsNone(money.price_decimal(self.row('MODEL-A'), prices))
        self.assertIsNone(money.price_decimal(self.row('model-a-2026-10-28'), prices))

    def test_catalog_invalid_rows_are_unpriced_instead_of_zero(self):
        with sqlite3.connect(self.cc) as db:
            db.execute('INSERT INTO model_pricing VALUES (?,?,?,?,?,?)', ('bad', 'Bad', 'NaN', '1', '1', '1'))
        snapshot = self.settings()
        self.assertEqual(snapshot['pricing_status'], 'partial')
        self.assertEqual(snapshot['invalid_prices'], 1)
        self.assertEqual([r['model'] for r in snapshot['pricing']], ['model-a'])

    def test_missing_cc_does_not_create_database(self):
        absent = self.cc.with_name('absent.sqlite3')
        self.assertEqual(money.settings(self.home, absent)['pricing_status'], 'unavailable')
        self.assertFalse(absent.exists())

    def test_custom_prices_override_only_exact_model_and_persist_private(self):
        override = dict(input=1.25,output=7.5,cache_read=.125,cache_creation=0)
        money.save_preferences(self.home, {'currency':'both','fx_mode':'manual','manual_rate':7.2,'model_overrides':{'model-a':override,'custom-model':override}})
        snapshot = self.settings()
        self.assertEqual(snapshot['prefs']['currency'], 'both')
        self.assertEqual(snapshot['fx']['rate'], 7.2)
        self.assertTrue(all(row['source']=='custom' for row in snapshot['pricing']))
        self.assertEqual((self.home/'usage-display.json').stat().st_mode&0o777, 0o600)
        self.assertFalse(list(self.home.glob('.usage-display.json-*')))
        money.save_preferences(self.home, {'currency':'USD'})
        self.assertEqual(money.preferences(self.home)[0]['model_overrides']['model-a'], override)

    def test_invalid_save_does_not_change_previous_file(self):
        money.save_preferences(self.home, {'currency':'USD'})
        before = (self.home/'usage-display.json').read_bytes()
        invalid = [{'currency':'EUR'},{'fx_mode':'live'},{'manual_rate':0},{'manual_rate':True},
                   {'manual_rate':float('nan')},{'manual_rate':'Infinity'}, {'url':'https://example.invalid'},
                   {'model_overrides':{'model-a':{'input':1}}}, {'model_overrides':{'model-a':dict(input=-1,output=1,cache_read=0,cache_creation=0)}},
                   {'model_overrides':{'bad\nmodel':dict(input=1,output=1,cache_read=0,cache_creation=0)}}]
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(money.MoneyError):money.save_preferences(self.home,value)
                self.assertEqual((self.home/'usage-display.json').read_bytes(),before)

    def test_manual_mode_needs_positive_rate(self):
        with self.assertRaises(money.MoneyError):money.save_preferences(self.home, {'fx_mode':'manual'})
        self.assertFalse((self.home/'usage-display.json').exists())
        money.save_preferences(self.home, {'manual_rate':7,'fx_mode':'manual'})
        with patch.object(money, 'fetch_rate', side_effect=AssertionError('no network')):
            self.assertEqual(money.refresh(self.home, force=True, now=self.now)['fx']['rate'],7)

    def test_formula_counts_disjoint_cache_once_with_decimal(self):
        prices,_ = money.catalog(self.home,self.cc)
        # .0002 input + .0002 output + .0004 cache read + .00015 creation
        self.assertEqual(money.price_decimal(self.row(),prices), Decimal('.00095'))
        with sqlite3.connect(self.cc) as db:
            db.execute("UPDATE model_pricing SET input_cost_per_million='0.000001'")
        prices,_ = money.catalog(self.home,self.cc)
        tiny = self.row();tiny.update(input_tokens=1,output_tokens=0,cache_read_tokens=0,cache_creation_tokens=0)
        cost = money.folded_cost([tiny]*1000, prices, 7)
        self.assertAlmostEqual(cost['cost_usd'], 1e-9, places=20)
        self.assertAlmostEqual(cost['cost_cny'], 7e-9, places=20)

    def test_mixed_summary_model_and_source_costs_cover_full_groups(self):
        self.cache()
        first=self.row(requests=2)
        second=self.row('unknown',requests=3)
        third=self.row(requests=4);third.update(app='claude',source='claude_session')
        result={'summary':{'requests':9},'logs':[first.copy(),second.copy()],
                'models':[{'model':'model-a'},{'model':'unknown'}],
                'sources':[{'source':'codex_session','app':'codex'},{'source':'claude_session','app':'claude'}]}
        with patch.object(money,'fetch_rate',side_effect=AssertionError('query must not fetch')):
            value=money.decorate(self.home,result,[first,second,third],self.cc,self.now)
        self.assertAlmostEqual(value['summary']['cost_usd'],.0019)
        self.assertAlmostEqual(value['summary']['cost_cny'],.01349)
        self.assertEqual(value['summary']['priced_requests'],6)
        self.assertEqual(value['summary']['unpriced_requests'],3)
        self.assertTrue(value['summary']['cost_partial'])
        self.assertEqual(value['models'][0]['priced_requests'],6)
        self.assertIsNone(value['models'][1]['cost_usd'])
        self.assertEqual(value['sources'][0]['unpriced_requests'],3)
        self.assertEqual(value['logs'][0]['priced_requests'],1)
        self.assertIsNone(value['logs'][1]['cost_usd'])

    def test_missing_fx_keeps_usd_and_unknown_does_not_become_free(self):
        result={'summary':{},'logs':[],'models':[],'sources':[]}
        value=money.decorate(self.home,result,[self.row()],self.cc,self.now)
        self.assertAlmostEqual(value['summary']['cost_usd'],.00095)
        self.assertIsNone(value['summary']['cost_cny'])
        self.assertIsNone(value['money']['fx']['rate'])
        value=money.decorate(self.home,{'summary':{}},[self.row('unknown')],self.cc,self.now)
        self.assertIsNone(value['summary']['cost_usd'])
        self.assertEqual(value['summary']['unpriced_requests'],1)

    def test_zero_priced_model_is_distinguished_from_unknown(self):
        with sqlite3.connect(self.cc) as db:
            db.execute("UPDATE model_pricing SET input_cost_per_million='0',output_cost_per_million='0',cache_read_cost_per_million='0',cache_creation_cost_per_million='0'")
        result=money.decorate(self.home,{'summary':{}},[self.row()],self.cc,self.now)
        self.assertEqual(result['summary']['cost_usd'],0)
        self.assertEqual(result['summary']['priced_requests'],1)
        self.assertFalse(result['summary']['cost_partial'])

    def test_rate_validation_checks_pair_finite_positive_and_date(self):
        valid=dict(base='USD',quote='CNY',rate=7.1,date='2026-10-28')
        self.assertEqual(money.validate_rate_payload(valid,self.now),valid)
        for change in [{'base':'EUR'},{'base':1},{'quote':None},{'quote':'USD'},{'rate':0},{'rate':-1},{'rate':True},
                       {'rate':'NaN'},{'date':'2026-10-29'},{'date':'2026-02-30'},{'date':'bad'}]:
            with self.subTest(change=change):
                with self.assertRaises(money.MoneyError):money.validate_rate_payload(dict(valid,**change),self.now)

    def test_refresh_cache_ttl_and_private_atomic_cache(self):
        self.cache(age=money.RATE_TTL-1)
        with patch.object(money,'fetch_rate',side_effect=AssertionError('fresh cache')):
            money.refresh(self.home,now=self.now)
        self.cache(age=money.RATE_TTL)
        reply=dict(base='USD',quote='CNY',rate=7.05,date='2026-10-28')
        with patch.object(money,'fetch_rate',return_value=reply) as fetch:
            result=money.refresh(self.home,now=self.now)
        fetch.assert_called_once()
        self.assertEqual(result['fx']['rate'],7.05)
        self.assertFalse(result['fx']['stale'])
        self.assertEqual((self.home/'usage-exchange.json').stat().st_mode&0o777,0o600)
        self.assertFalse(list(self.home.glob('.usage-exchange.json-*')))

    def test_failure_preserves_old_rate_backoff_and_never_exposes_error_body(self):
        self.cache(age=money.RATE_TTL+1)
        with patch.object(money,'fetch_rate',side_effect=RuntimeError('SECRET RESPONSE')) as fetch:
            first=money.refresh(self.home,now=self.now)
            second=money.refresh(self.home,now=self.now+money.FAILURE_TTL-1)
            self.assertEqual(fetch.call_count,1)
            money.refresh(self.home,now=self.now+money.FAILURE_TTL)
            self.assertEqual(fetch.call_count,2)
        self.assertEqual(first['fx']['rate'],7.1)
        self.assertTrue(first['fx']['stale'])
        self.assertFalse(second['fx']['refresh_due'])
        self.assertNotIn('SECRET',json.dumps(first))
        self.assertNotIn('SECRET',(self.home/'usage-exchange.json').read_text())

    def test_forced_failure_of_fresh_cache_retries_after_backoff(self):
        self.cache()
        with patch.object(money,'fetch_rate',side_effect=TimeoutError) as fetch:
            money.refresh(self.home,force=True,now=self.now)
            money.refresh(self.home,now=self.now+money.FAILURE_TTL-1)
            self.assertEqual(fetch.call_count,1)
            money.refresh(self.home,now=self.now+money.FAILURE_TTL)
            self.assertEqual(fetch.call_count,2)

    def test_empty_usage_is_zero_with_no_unpriced_requests(self):
        prices,_=money.catalog(self.home,self.cc)
        row=money.folded_cost([],prices,7)
        self.assertEqual(row['cost_usd'],0)
        self.assertEqual(row['cost_cny'],0)
        self.assertEqual(row['unpriced_requests'],0)

    def test_no_previous_rate_failure_is_unavailable_and_recovers(self):
        with patch.object(money,'fetch_rate',side_effect=TimeoutError):
            result=money.refresh(self.home,now=self.now)
        self.assertIsNone(result['fx']['rate'])
        self.assertTrue(result['fx']['error'])
        reply=dict(base='USD',quote='CNY',rate=7.05,date='2026-10-28')
        with patch.object(money,'fetch_rate',return_value=reply):
            result=money.refresh(self.home,force=True,now=self.now+10)
        self.assertEqual(result['fx']['rate'],7.05)
        self.assertIsNone(result['fx']['error'])

    def test_query_reads_cache_only_and_marks_old_reference_date_stale(self):
        self.cache(date='2026-10-01')
        with patch.object(money,'fetch_rate',side_effect=AssertionError('no query network')):
            result=self.settings()
        self.assertTrue(result['fx']['stale'])
        self.assertTrue(result['fx']['refresh_due'])

    def test_fetch_fixed_pair_no_redirect_and_bounded_response(self):
        class Response(io.BytesIO):
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
        class Opener:
            def __init__(self,raw):self.raw=raw;self.calls=[]
            def open(self,request,timeout):
                self.calls.append((request.full_url,request.data,timeout))
                return Response(self.raw)
        opener=Opener(json.dumps(dict(base='USD',quote='CNY',rate=7.1,date='2026-10-28')).encode())
        with patch.object(money.urllib.request,'build_opener',return_value=opener):
            result=money.fetch_rate(self.now)
        self.assertEqual(opener.calls,[(money.RATE_URL,None,8)])
        self.assertEqual(result['rate'],7.1)
        with patch.object(money.urllib.request,'build_opener',return_value=Opener(b' '* (money.MAX_JSON+1))):
            with self.assertRaises(money.MoneyError):money.fetch_rate(self.now)
        with self.assertRaises(money.MoneyError):money._NoRedirect().redirect_request(None,None,302,'',{},'https://evil.invalid')

    def test_corrupt_prefs_or_cache_does_not_invent_exchange_rate(self):
        (self.home/'usage-display.json').write_text('{bad')
        (self.home/'usage-exchange.json').write_text('{bad')
        snapshot=self.settings()
        self.assertTrue(snapshot['settings_warning'])
        self.assertIsNone(snapshot['fx']['rate'])
        self.assertTrue(snapshot['fx']['refresh_due'])
        self.assertEqual((self.home/'usage-display.json').read_text(),'{bad')

    def ledger(self):
        from vibecue import usage
        rows = [('codex-a', 'codex', 'codex_session', 'model-a', self.now-100),
                ('codex-b', 'codex', 'codex_session', 'model-a', self.now-200),
                ('unknown', 'codex', 'codex_session', 'unknown-model', self.now-50),
                ('claude-a', 'claude', 'claude_session', 'model-a', self.now-300),
                ('future', 'codex', 'codex_session', 'model-a', self.now+100),
                ('old', 'codex', 'codex_session', 'model-a', self.now-40*86400)]
        with usage.connect(self.home) as db:
            for identifier, app, source, model, timestamp in rows:
                db.execute('INSERT INTO usage_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                           (identifier,timestamp,app,source,model,'thread-'+identifier,
                            100,20,800,50,970,datetime.fromtimestamp(timestamp).date().isoformat()))
        return usage

    def test_full_query_prices_complete_filter_not_only_visible_page(self):
        usage = self.ledger()
        money.save_preferences(self.home, {'fx_mode':'manual','manual_rate':7.1})
        original_catalog = money.catalog
        cc_before = hashlib.sha256(self.cc.read_bytes()).hexdigest()
        with sqlite3.connect(self.home/'usage.sqlite3') as db:
            before = db.execute('SELECT * FROM usage_requests ORDER BY id').fetchall()
        # Use a test CC catalog and reject any network: this exercises the real
        # query SQL/filter/page pipeline without the user's files or prices.
        with patch.object(money,'catalog',side_effect=lambda home=None,cc_path=None,prefs=None,now=None: original_catalog(home,self.cc,prefs,now)), \
             patch.object(money,'fetch_rate',side_effect=AssertionError('queries never fetch')):
            first = usage.query(self.home,period='7d',app='codex',page_size=1,page=1,now=self.now)
            second = usage.query(self.home,period='7d',app='codex',page_size=1,page=2,now=self.now)
            models = usage.query(self.home,period='7d',app='codex',view='models',now=self.now)
            sources = usage.query(self.home,period='7d',app='codex',view='sources',now=self.now)
        self.assertEqual(first['summary']['requests'],3)
        self.assertEqual(len(first['logs']),1)
        self.assertIsNone(first['logs'][0]['cost_usd'])
        self.assertAlmostEqual(second['logs'][0]['cost_usd'],.00095)
        for result in [first,second,models,sources]:
            self.assertAlmostEqual(result['summary']['cost_usd'],.0019)
            self.assertAlmostEqual(result['summary']['cost_cny'],.01349)
            self.assertEqual(result['summary']['priced_requests'],2)
            self.assertEqual(result['summary']['unpriced_requests'],1)
        self.assertAlmostEqual(next(row for row in models['models'] if row['model']=='model-a')['cost_usd'],.0019)
        self.assertAlmostEqual(sources['sources'][0]['cost_usd'],.0019)
        with sqlite3.connect(self.home/'usage.sqlite3') as db:
            self.assertEqual(db.execute('SELECT * FROM usage_requests ORDER BY id').fetchall(),before)
        self.assertEqual(hashlib.sha256(self.cc.read_bytes()).hexdigest(),cc_before)

    def test_full_query_unpriced_and_empty_filters_remain_distinct(self):
        usage = self.ledger()
        original_catalog = money.catalog
        with patch.object(money,'catalog',side_effect=lambda home=None,cc_path=None,prefs=None,now=None: original_catalog(home,self.cc,prefs,now)), \
             patch.object(money,'fetch_rate',side_effect=AssertionError('queries never fetch')):
            unknown = usage.query(self.home,model='unknown-model',now=self.now)
            absent = usage.query(self.home,source='missing-provider',now=self.now)
        self.assertEqual(unknown['summary']['requests'],1)
        self.assertIsNone(unknown['summary']['cost_usd'])
        self.assertEqual(unknown['summary']['unpriced_requests'],1)
        self.assertEqual(unknown['summary']['priced_requests'],0)
        self.assertEqual(absent['summary']['requests'],0)
        self.assertEqual(absent['summary']['cost_usd'],0)
        self.assertEqual(absent['summary']['unpriced_requests'],0)


if __name__=='__main__':
    unittest.main()
