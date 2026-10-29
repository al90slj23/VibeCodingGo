"""Exact cache invariants use a small numeric fixture, never user histories."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vibecue import usage, usage_money, usage_query_cache as cache


class SQLOnly:
    def __init__(self, conn, home, now): self.conn = conn
    def rows(self, sql, params=(), **kwargs):
        return [dict(row) for row in self.conn.execute(sql, params)]


class ExactQueryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = datetime(2026, 10, 30, 12).timestamp()
        self.conn = usage.connect(self.home); self.addCleanup(self.conn.close)
        usage_money.private_write(self.home / 'usage-display.json', {
            'fx_mode':'manual', 'manual_rate':7,
            'pricing_sources':[{'id':'custom','name':'Custom','kind':'custom','enabled':True}],
            'model_overrides':{'m':dict(input=2,output=8,cache_read=.5,cache_creation=3)}})
        for number in range(7): self.add(str(number), self.now-3600*(number+1))
        self.conn.execute('INSERT INTO usage_request_details(request_id,provider_id,provider_name,status_code) VALUES(?,?,?,?)', ('0','real','Provider',200))
        self.conn.commit()

    def add(self, identifier, when, counts=(10,2,20,0)):
        usage.write_request(self.conn, identifier, when, 'codex', 'm', identifier, counts)

    def query(self, **kwargs):
        kwargs.setdefault('now', self.now)
        return usage.query(self.home, **kwargs)

    def oracle(self, **kwargs):
        with patch.object(cache, 'ExactQueryCache', SQLOnly): return self.query(**kwargs)

    def equal(self, **kwargs):
        expected = self.oracle(**kwargs)
        self.assertEqual(self.query(**kwargs), expected)
        self.assertEqual(self.query(**kwargs), expected)

    def test_cached_default_and_all_filters_match_reference_exactly(self):
        for options in [{}, {'page':2,'page_size':2}, {'view':'models'}, {'view':'providers'},
                        {'app':'codex'}, {'model':'m'}, {'source':'codex_session'},
                        {'provider':'codex|real'}, {'provider':'codex|source:codex_session'},
                        {'status':'success'}, {'status':'unknown'}, {'status':'429'},
                        {'period':'today'}, {'period':'7d'}, {'period':'1d'},
                        {'period':'custom','start_date':'2026-10-29','end_date':'2026-10-30'}]:
            with self.subTest(options=options): self.equal(**options)

    def test_insert_correction_delete_and_rollback_invalidate_transactionally(self):
        self.query()
        initial = self.conn.execute('SELECT generation FROM usage_query_epoch').fetchone()[0]
        self.add('new', self.now-10); self.conn.commit(); self.equal()
        self.conn.execute("UPDATE usage_requests SET input_tokens=30,total_tokens=52 WHERE id='0'")
        self.conn.commit(); self.equal()
        self.conn.execute("DELETE FROM usage_requests WHERE id='1'"); self.conn.commit(); self.equal()
        before = self.conn.execute('SELECT generation FROM usage_query_epoch').fetchone()[0]
        self.conn.execute("UPDATE usage_requests SET input_tokens=999,total_tokens=1021 WHERE id='0'")
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT generation FROM usage_query_epoch').fetchone()[0], before)
        self.assertGreater(before, initial); self.equal()

    def test_detail_mutation_changes_provider_and_status_results(self):
        self.query(provider='codex|real'); self.query(status='success')
        self.conn.execute("UPDATE usage_request_details SET provider_id='other',provider_name='Changed',status_code=500 WHERE request_id='0'")
        self.conn.commit()
        for options in ({'provider':'codex|real'}, {'provider':'codex|other'}, {'status':'error'}): self.equal(**options)
        self.conn.execute("DELETE FROM usage_request_details WHERE request_id='0'"); self.conn.commit(); self.equal(status='unknown')

    def test_archive_future_boundaries_and_removal_are_exact(self):
        values=('archive',self.now+30,'gemini','ccswitch:archive','m','2026-10-30',4,10,2,20,0,32,'archive-provider','Archive')
        self.conn.execute('INSERT INTO usage_archived_daily VALUES('+','.join('?' for _ in values)+')',values)
        self.add('future', self.now+20); self.conn.commit()
        self.equal(now=self.now)
        self.equal(now=self.now+19)
        self.equal(now=self.now+21)
        self.equal(now=self.now+31)
        self.conn.execute("UPDATE usage_archived_daily SET request_count=9 WHERE id='archive'"); self.conn.commit(); self.equal(now=self.now+31)
        self.conn.execute("DELETE FROM usage_archived_daily WHERE id='archive'"); self.conn.commit(); self.equal(now=self.now+31)

    def test_rolling_lower_bound_is_never_rounded_for_cache(self):
        self.add('edge',self.now-7*86400+.5); self.conn.commit()
        before=self.query(period='7d',now=self.now)
        after=self.query(period='7d',now=self.now+1)
        self.assertEqual(before['summary']['requests']-after['summary']['requests'],1)
        self.assertEqual(after,self.oracle(period='7d',now=self.now+1))

    def test_price_and_exchange_changes_are_not_cached(self):
        first=self.query()
        prefs=json.loads((self.home/'usage-display.json').read_text())
        prefs['manual_rate']=8; prefs['model_overrides']['m']['input']=4
        usage_money.private_write(self.home/'usage-display.json',prefs)
        after=self.query(); self.assertNotEqual(after['summary']['cost_usd'],first['summary']['cost_usd'])
        self.assertNotEqual(after['summary']['cost_cny'],first['summary']['cost_cny']); self.assertEqual(after,self.oracle())

    def test_corrupt_entry_is_discarded_and_recomputed(self):
        expected=self.query()
        for path in (self.home/'usage-query-cache').glob('*.json'):
            value=json.loads(path.read_text()); value['rows']=[{'wrong':999}];path.write_text(json.dumps(value))
        self.assertEqual(self.query(),expected)

    def test_missing_trigger_retires_cache_identity_before_reuse(self):
        self.query(); old=self.conn.execute('SELECT identity FROM usage_query_epoch').fetchone()[0]
        self.conn.execute('DROP TRIGGER usage_query_epoch_usage_requests_update')
        self.conn.execute("UPDATE usage_requests SET input_tokens=100,total_tokens=122 WHERE id='0'");self.conn.commit()
        self.equal()
        self.assertNotEqual(self.conn.execute('SELECT identity FROM usage_query_epoch').fetchone()[0],old)

    def test_cache_does_not_upgrade_snapshot_while_external_writer_commits(self):
        self.query()
        reader=usage.connect(self.home)
        try:
            reader.execute('BEGIN'); view=cache.ExactQueryCache(reader,self.home,self.now)
            old=view.rows('SELECT SUM(total_tokens) AS total FROM usage_requests')
            self.add('parallel',self.now-5);self.conn.commit()
            self.assertEqual(view.rows('SELECT SUM(total_tokens) AS total FROM usage_requests'),old)
            self.assertEqual(reader.execute('SELECT SUM(total_tokens) FROM usage_requests').fetchone()[0],old[0]['total'])
        finally:reader.close()
        self.equal()

    def test_unavailable_derived_metadata_falls_back_to_sql(self):
        expected=self.query()
        self.conn.execute('DROP TABLE usage_query_epoch');self.conn.commit()
        self.conn.execute('BEGIN')
        view=cache.ExactQueryCache(self.conn,self.home,self.now)
        self.assertIsNone(view.context)
        self.assertEqual(view.rows('SELECT COUNT(*) AS count FROM usage_requests'),[{'count':7}])
        self.conn.rollback()
        with patch.object(cache,'_install_schema',side_effect=sqlite3.OperationalError('derived metadata unavailable')):
            self.assertFalse(cache.ensure_schema(self.conn))
        self.assertEqual(self.query(),expected)

    def test_shared_or_symlink_cache_cannot_supply_numeric_truth(self):
        expected=self.query();directory=self.home/'usage-query-cache'
        for mode in (0o755,0o777):
            os.chmod(directory,mode)
            with patch.object(cache.ExactQueryCache,'_write'):
                self.assertEqual(self.query(),expected)
            os.chmod(directory,0o700)
        for path in directory.glob('*.json'):os.chmod(path,0o644)
        statements=[];original=sqlite3.connect
        def traced(*args,**kwargs):
            connection=original(*args,**kwargs);connection.set_trace_callback(statements.append);return connection
        with patch.object(usage.sqlite3,'connect',side_effect=traced):self.assertEqual(self.query(),expected)
        self.assertTrue(any('GROUP BY r.app,r.source,r.model' in sql for sql in statements))
        # Directory symlinks are never followed, even to an otherwise private
        # location containing a formerly valid derived cache.
        target=self.home/'old-cache';directory.rename(target);directory.symlink_to(target,target_is_directory=True)
        self.assertEqual(self.query(),expected)

    def test_same_inode_replacement_with_restored_mtime_changes_cache_context(self):
        with usage.connect(self.home) as reader:
            reader.execute('BEGIN');before=cache.ExactQueryCache(reader,self.home,self.now).context
        path=self.home/'usage.sqlite3';old=path.stat()
        os.chmod(path,0o640);os.chmod(path,0o600);os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
        with usage.connect(self.home) as reader:
            reader.execute('BEGIN');after=cache.ExactQueryCache(reader,self.home,self.now).context
        self.assertNotEqual(before,after)
        self.assertEqual(before[3],after[3])

    def test_ready_schema_does_not_take_a_writer_lock_for_read_queries(self):
        statements=[]
        self.conn.set_trace_callback(statements.append)
        cache.ensure_schema(self.conn)
        self.conn.set_trace_callback(None)
        self.assertFalse(any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for sql in statements))
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            # A separate writer may own the WAL write reservation; initialized
            # read clients must still open and consume the committed snapshot.
            with usage.connect(self.home) as reader:
                reader.execute('BEGIN')
                self.assertEqual(reader.execute('SELECT COUNT(*) FROM usage_requests').fetchone()[0],7)
        finally:self.conn.rollback()

    def test_cached_projection_is_shared_across_fresh_cli_processes(self):
        self.query()
        code='''import json,sys
from pathlib import Path
from vibecue import usage,usage_query_cache as c
counts={'hits':0,'misses':0}; original=c.ExactQueryCache.rows
def rows(self,*args,**kwargs):
 result=original(self,*args,**kwargs);counts['hits']=self.hits;counts['misses']=self.misses;return result
c.ExactQueryCache.rows=rows
r=usage.query(Path(sys.argv[1]),now=float(sys.argv[2]),page=2,page_size=2)
print(json.dumps({'counts':counts,'result':r},sort_keys=True))
'''
        env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1'
        result=subprocess.run([sys.executable,'-c',code,str(self.home),str(self.now)],capture_output=True,text=True,env=env,timeout=15,check=True)
        data=json.loads(result.stdout);self.assertGreaterEqual(data['counts']['hits'],8)
        self.assertEqual(data['result'],self.oracle(page=2,page_size=2))
