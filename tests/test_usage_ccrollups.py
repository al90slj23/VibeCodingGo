from pathlib import Path
import sqlite3
import tempfile
import unittest
from vibecue import usage,usage_ccswitch as cc,usage_ccrollups as archive


class CCArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.conn=usage.connect(Path(self.temp.name)/'home');self.addCleanup(self.conn.close)
        self.source=sqlite3.connect(':memory:');self.source.row_factory=sqlite3.Row;self.addCleanup(self.source.close)
        self.source.executescript('''CREATE TABLE usage_daily_rollups(date TEXT,app_type TEXT,model TEXT,provider_id TEXT,request_count INTEGER,input_tokens INTEGER,output_tokens INTEGER,cache_read_tokens INTEGER,cache_creation_tokens INTEGER,input_token_semantics INTEGER);
          CREATE TABLE proxy_request_logs(request_id TEXT,app_type TEXT);''')

    def add(self, app='gemini', requests=3, inputs=2100):
        self.source.execute('INSERT INTO usage_daily_rollups VALUES(?,?,?,?,?,?,?,?,?,?)',('2026-10-01',app,'model-a','_session',requests,inputs,300,900,0,2))

    def test_native_archives_are_not_counted_again_and_other_archives_are_not_fake_logs(self):
        self.add('codex');self.add('claude');self.add()
        r=archive.sync(self.conn,self.source)
        self.assertEqual(r['requests'],3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_requests').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT request_count FROM usage_archived_daily').fetchone()[0],3)
        archive.sync(self.conn,self.source)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_archived_daily').fetchone()[0],1)

    def test_only_retained_pruned_details_are_subtracted(self):
        self.add()
        for origin in ('pruned','live'):
            row=cc.normalized(dict(request_id=origin,app_type='gemini',session_id='s',model='model-a',input_tokens=1000,output_tokens=100,cache_read_tokens=300,cache_creation_tokens=0,input_token_semantics=1,created_at=1790812800,provider_id='_session',data_source='gemini_session'))
            keys=list(row)
            self.conn.execute('INSERT INTO usage_cc_metrics('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',list(row.values()))
            self.conn.execute('INSERT INTO usage_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('ccswitch:'+row['cc_id'],row['second'],'gemini','ccswitch:gemini_session','model-a','s',700,100,300,0,1100,'2026-10-01'))
        self.source.execute('INSERT INTO proxy_request_logs VALUES(?,?)',('live','gemini'))
        result=archive.sync(self.conn,self.source)
        self.assertEqual(result['requests'],2)
        row=self.conn.execute('SELECT request_count,input_tokens,output_tokens,cache_read_tokens FROM usage_archived_daily').fetchone()
        self.assertEqual(tuple(row),(2,1400,200,600))

    def test_inconsistent_residual_is_not_clamped_to_zero(self):
        self.add(requests=0,inputs=-1)
        result=archive.sync(self.conn,self.source)
        self.assertEqual(result['invalid'],1)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_archived_daily').fetchone()[0],0)


if __name__=='__main__':unittest.main()
