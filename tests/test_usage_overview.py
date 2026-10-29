"""Conditional pages prove every skipped overview against exact current inputs.

All data are a private synthetic ledger. No account, transcript or network is
used, including the independent unoptimized full-response oracle.
"""
from contextlib import contextmanager
from datetime import datetime
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import usage, usage_money, usage_query_cache, usage_overview, usage_pricing


@contextmanager
def timezone(value):
    old = os.environ.get('TZ'); os.environ['TZ'] = value; time.tzset()
    try: yield
    finally:
        if old is None: os.environ.pop('TZ', None)
        else: os.environ['TZ'] = old
        time.tzset()


class ConditionalOverviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.zone = timezone('Asia/Taipei'); self.zone.__enter__(); self.addCleanup(self.zone.__exit__, None, None, None)
        self.now = datetime(2026,10,30,12,34).timestamp()
        self.conn = usage.connect(self.home); self.addCleanup(self.conn.close)
        self.prefs = {'fx_mode':'manual','manual_rate':7,
                      'pricing_sources':[{'id':'custom','name':'Custom','kind':'custom','enabled':True}],
                      'model_overrides':{'m':dict(input=2,output=8,cache_read=.5,cache_creation=3)}}
        usage_money.private_write(self.home/'usage-display.json', self.prefs)
        for n in range(8): self.add(str(n), self.now-3600*(n+1))
        self.conn.execute('INSERT INTO usage_request_details(request_id,provider_id,provider_name,status_code) VALUES(?,?,?,?)',('0','real','Provider',200))
        self.conn.commit()
        self.network = patch.object(usage_money, 'fetch_rate', side_effect=AssertionError('no network'))
        self.network.start(); self.addCleanup(self.network.stop)

    def add(self, key, when):
        usage.write_request(self.conn,key,when,'codex','m',key,(10,2,20,0))

    def full(self, **kwargs):
        kwargs.setdefault('now',self.now)
        return usage.query(self.home,overview_protocol=True,**kwargs)

    def page(self, base, **kwargs):
        kwargs.setdefault('now',self.now)
        return usage.query(self.home,overview_protocol=True,overview_revision=base['overview_revision'],**kwargs)

    def merge(self, base, delta):
        result=copy.deepcopy(base)
        for key in ('logs','pagination','sync','money'): result[key]=delta[key]
        result['overview_unchanged']=False
        return result

    def assert_full(self, base, **kwargs):
        result=self.page(base,**kwargs)
        self.assertFalse(result['overview_unchanged'])
        self.assertEqual(result,self.full(**kwargs))
        return result

    def test_default_protocol_is_unchanged_and_opt_in_full_matches_it(self):
        original=usage.query(self.home,now=self.now)
        current=self.full()
        for key in ('overview_protocol','overview_revision','overview_unchanged'): current.pop(key)
        self.assertEqual(current,original)
        self.assertEqual(usage.query(self.home,now=self.now,overview_revision='a'*64),original)

    def test_exact_revision_page_changes_only_logs_and_metadata(self):
        for options in ({},{'period':'today'},{'status':'success'},{'provider':'codex|real'},
                        {'period':'custom','start_date':'2026-10-30','end_date':'2026-10-30'}):
            with self.subTest(options=options):
                base=self.full(**options)
                delta=self.page(base,page=2,page_size=2,now=self.now+1,**options)
                self.assertTrue(delta['overview_unchanged'])
                self.assertNotIn('days',delta); self.assertNotIn('summary',delta);self.assertNotIn('filters',delta)
                self.assertEqual(self.merge(base,delta),self.full(page=2,page_size=2,now=self.now+1,**options))

    def test_partial_page_never_rebuilds_calendar_or_all_money(self):
        base=self.full()
        with patch.object(usage_money,'decorate',side_effect=AssertionError('overview price fold')):
            delta=self.page(base,page=2,page_size=2)
        self.assertTrue(delta['overview_unchanged']);self.assertEqual(len(delta['logs']),2)

    def test_request_insert_update_delete_and_rollback_are_exact(self):
        base=self.full()
        self.add('new',self.now-10);self.conn.commit();base=self.assert_full(base)
        self.conn.execute("UPDATE usage_requests SET input_tokens=9007199254740993,total_tokens=9007199254741015 WHERE id='0'")
        self.conn.commit();base=self.assert_full(base)
        self.conn.execute("DELETE FROM usage_requests WHERE id='1'");self.conn.commit();base=self.assert_full(base)
        self.conn.execute("UPDATE usage_requests SET input_tokens=999,total_tokens=1021 WHERE id='0'");self.conn.rollback()
        # A rollback may change physical metadata conservatively; whichever
        # branch runs, the merged complete numeric response must be identical.
        result=self.page(base)
        self.assertEqual(self.merge(base,result) if result['overview_unchanged'] else result,self.full())

    def test_provider_status_and_archived_corrections_invalidate(self):
        base=self.full()
        self.conn.execute("UPDATE usage_request_details SET provider_name='Changed',status_code=500 WHERE request_id='0'")
        self.conn.commit();base=self.assert_full(base)
        values=('archive',self.now-30,'gemini','ccswitch:archive','m','2026-10-30',4,10,2,20,0,32,'api','API')
        self.conn.execute('INSERT INTO usage_archived_daily VALUES('+','.join('?' for _ in values)+')',values)
        self.conn.commit();base=self.assert_full(base)
        self.conn.execute("DELETE FROM usage_archived_daily WHERE id='archive'");self.conn.commit();self.assert_full(base)

    def test_future_request_and_archive_crossing_without_writes_invalidate(self):
        self.add('future',self.now+10)
        values=('future-archive',self.now+20,'gemini','ccswitch:archive','m','2026-10-30',4,10,2,20,0,32,'api','API')
        self.conn.execute('INSERT INTO usage_archived_daily VALUES('+','.join('?' for _ in values)+')',values);self.conn.commit()
        base=self.full();self.assertTrue(self.page(base,now=self.now+5)['overview_unchanged'])
        base=self.assert_full(base,now=self.now+11)
        self.assert_full(base,now=self.now+21)

    def test_prices_fx_currency_queue_and_source_status_are_current(self):
        base=self.full()
        for key,value in [('manual_rate',8),('currency','USD')]:
            self.prefs[key]=value;usage_money.private_write(self.home/'usage-display.json',self.prefs);base=self.assert_full(base)
        self.prefs['model_overrides']['m']['input']=4
        usage_money.private_write(self.home/'usage-display.json',self.prefs);self.assert_full(base)

    def test_public_price_cache_rewrite_queue_order_and_enabled_state_invalidate(self):
        first=dict(id='first',name='First',kind='json',enabled=True,url='https://prices.example/first.json')
        second=dict(id='second',name='Second',kind='json',enabled=True,url='https://prices.example/second.json')
        def cached(source, incoming):
            models={'m':dict(model='m',name='M',input=incoming,output=8,cache_read=.5,cache_creation=3)}
            usage_pricing.write_cache(self.home,source,dict(fingerprint=usage_pricing.fingerprint(source),models=models,
                                      updated_at=self.now,attempted_at=self.now,error=False,invalid_prices=0))
        cached(first,3);cached(second,5)
        self.prefs['pricing_sources']=[first,second,self.prefs['pricing_sources'][0]]
        usage_money.private_write(self.home/'usage-display.json',self.prefs);base=self.full()
        cached(first,7);changed=self.assert_full(base)
        self.assertNotEqual(changed['summary']['cost_usd'],base['summary']['cost_usd'])
        self.prefs['pricing_sources']=[second,first,self.prefs['pricing_sources'][2]]
        usage_money.private_write(self.home/'usage-display.json',self.prefs);base=self.assert_full(changed)
        second['enabled']=False
        usage_money.private_write(self.home/'usage-display.json',self.prefs);self.assert_full(base)

    def test_price_cache_stale_boundary_invalidates_without_a_write(self):
        source=dict(id='public',name='Public',kind='json',enabled=True,url='https://prices.example/a.json')
        self.prefs.update(pricing_sources=[source],pricing_refresh_seconds=1800)
        usage_money.private_write(self.home/'usage-display.json',self.prefs)
        usage_pricing.write_cache(self.home,source,dict(fingerprint=usage_pricing.fingerprint(source),
            models={'m':dict(model='m',name='M',input=2,output=8,cache_read=.5,cache_creation=3)},
            updated_at=self.now-1799.5,attempted_at=self.now-1799.5,error=False,invalid_prices=0))
        base=self.full();self.assertFalse(base['money']['source_status'][0]['stale'])
        changed=self.assert_full(base,now=self.now+1)
        self.assertTrue(changed['money']['source_status'][0]['stale'])

    def test_exchange_cache_rewrite_and_stale_boundary_invalidate(self):
        self.prefs['fx_mode']='auto';usage_money.private_write(self.home/'usage-display.json',self.prefs)
        rate=dict(base='USD',quote='CNY',rate=7,date=datetime.utcfromtimestamp(self.now).date().isoformat(),
                  updated_at=self.now-usage_money.RATE_TTL+.5,attempted_at=self.now-usage_money.RATE_TTL+.5)
        usage_money.private_write(self.home/'usage-exchange.json',rate)
        base=self.full();self.assertFalse(base['money']['fx']['stale'])
        changed=self.assert_full(base,now=self.now+1);self.assertTrue(changed['money']['fx']['stale'])
        rate.update(rate=8,updated_at=self.now,attempted_at=self.now)
        usage_money.private_write(self.home/'usage-exchange.json',rate);self.assert_full(changed,now=self.now+1)

    def test_due_scheduler_deadline_is_refreshed_without_repricing_overview(self):
        original=usage_money.settings
        def due(home,cc_path=None,now=None):
            result=original(home,cc_path,now);result.update(pricing_refresh_due=True,pricing_next_refresh_at=now);return result
        with patch.object(usage_money,'settings',side_effect=due):
            base=self.full();delta=self.page(base,now=self.now+1)
            self.assertTrue(delta['overview_unchanged']);self.assertEqual(delta['money']['pricing_next_refresh_at'],self.now+1)
            self.assertEqual(self.merge(base,delta),self.full(now=self.now+1))

    def test_rolling_lower_bound_is_literal_not_bucketed(self):
        for period in ('1d','7d','30d','90d'):
            base=self.full(period=period);self.assert_full(base,period=period,now=self.now+.1)

    def test_midnight_hour_and_timezone_change_invalidate(self):
        base=self.full();self.assert_full(base,now=datetime(2026,10,31,0,0).timestamp())
        base=self.full(period='today');self.assert_full(base,period='today',now=datetime(2026,10,30,13,0).timestamp())
        base=self.full()
        with timezone('Asia/Kathmandu'):self.assert_full(base)
        with timezone('Asia/Kathmandu'):
            now=datetime(2026,10,30,12,59,59).timestamp()
            base=self.full(period='today',now=now)
            self.assert_full(base,period='today',now=now+1)

    def test_filter_view_heatmap_and_sync_metadata_changes_invalidate(self):
        base=self.full()
        for options in ({'app':'claude'},{'model':'missing'},{'source':'other'},{'status':'success'},
                        {'provider':'codex|real'},{'view':'models'},{'heatmap_weeks':106},{'period':'month'}):
            self.assert_full(base,**options)
        usage.set_meta(self.conn,'sync',{'running':False,'last_synced_at':self.now,'warnings':['changed']});self.conn.commit();self.assert_full(base)

    def test_bad_token_missing_epoch_and_unsupported_projection_use_full_query(self):
        for token in (None,'','bad','g'*64,'a'*65,'a'*64):
            self.assertFalse(usage.query(self.home,now=self.now,overview_protocol=True,overview_revision=token)['overview_unchanged'])
        base=self.full(view='models');self.assert_full(base,view='models')
        with patch.object(usage_query_cache.ExactQueryCache,'__init__',lambda c,conn,home,now: (setattr(c,'conn',conn),setattr(c,'directory',home/'unused'),setattr(c,'context',None),setattr(c,'hits',0),setattr(c,'misses',0))[-1]):
            self.assert_full(self.full())

    def test_writer_after_revision_keeps_page_in_same_wal_snapshot(self):
        base=self.full();original=usage_overview.matches
        def commit_after_match(expected,current):
            matched=original(expected,current)
            self.add('parallel',self.now-5);self.conn.commit()
            return matched
        with patch.object(usage_overview,'matches',side_effect=commit_after_match):
            delta=self.page(base,page=1,page_size=2)
        self.assertTrue(delta['overview_unchanged']);self.assertNotIn('parallel',{r['id'] for r in delta['logs']})
        self.assertEqual(delta['pagination']['total'],8)
        self.assert_full(base)

    def test_cli_accepts_conditional_protocol_without_changing_default_cli(self):
        with patch.dict(os.environ,{'VIBECUE_HOME':str(self.home)}),patch('sys.stdout',new_callable=io.StringIO) as output:
            with patch.object(usage.time,'time',return_value=self.now):
                self.assertEqual(usage.main(['query','--overview-protocol']),0)
                base=json.loads(output.getvalue());output.seek(0);output.truncate(0)
                self.assertEqual(usage.main(['query','--overview-protocol','--overview-revision',base['overview_revision'],'--page','2','--page-size','2']),0)
                self.assertTrue(json.loads(output.getvalue())['overview_unchanged'])
