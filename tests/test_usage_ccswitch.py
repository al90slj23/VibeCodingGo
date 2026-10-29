import hashlib
from datetime import datetime
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import usage, usage_ccswitch as bridge


class CCUsageInteropTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)/'home'; self.source=Path(self.temp.name)/'cc.sqlite3'
        self.conn=usage.connect(self.home); self.addCleanup(self.conn.close)
        with sqlite3.connect(self.source) as db:
            db.executescript('''CREATE TABLE proxy_request_logs (
              request_id TEXT PRIMARY KEY,app_type TEXT,session_id TEXT,model TEXT,input_tokens INTEGER,
              output_tokens INTEGER,cache_read_tokens INTEGER,cache_creation_tokens INTEGER,input_token_semantics INTEGER,
              created_at INTEGER,latency_ms INTEGER,first_token_ms INTEGER,status_code INTEGER,provider_id TEXT,data_source TEXT);
              CREATE TABLE settings(key TEXT,value TEXT);INSERT INTO settings VALUES('auth','PRIVATE-NOT-READ');
              CREATE TABLE providers(id TEXT,app_type TEXT,name TEXT,settings_config TEXT);
              INSERT INTO providers VALUES('provider-one','codex','公开名称','PRIVATE-NOT-READ');''')
        self.timestamp=1793232000

    def cc(self, **updates):
        row=dict(request_id='cc-one',app_type='codex',session_id='session-one',model='model-a',input_tokens=1000,
                 output_tokens=300,cache_read_tokens=800,cache_creation_tokens=0,input_token_semantics=1,
                 created_at=self.timestamp,latency_ms=2000,first_token_ms=500,status_code=200,
                 provider_id='_codex_session',data_source='codex_session')
        row.update(updates)
        with sqlite3.connect(self.source) as db:
            db.execute('INSERT OR REPLACE INTO proxy_request_logs('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',list(row.values()))

    def native(self, identity='native-one'):
        self.conn.execute('INSERT INTO usage_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (identity,self.timestamp+.3,'codex','codex_session','model-a','session-one',200,300,800,0,1300,'2026-10-29'))
        self.conn.commit()

    def logs(self):
        rows=[dict(row) for row in self.conn.execute('SELECT * FROM usage_requests')]
        return bridge.decorate_logs(self.conn,rows)

    def test_session_timing_is_exactly_matched_without_reimporting_native_counts(self):
        self.native();self.cc();before=hashlib.sha256(self.source.read_bytes()).digest()
        report=bridge.sync(self.conn,self.source);rows=self.logs()
        self.assertEqual(len(rows),1);self.assertEqual(report['imported'],0);self.assertEqual(report['matched'],1)
        self.assertEqual(rows[0]['speed_tps'],150);self.assertTrue(rows[0]['timing_estimated'])
        self.assertIsNone(rows[0]['status_code']);self.assertEqual(rows[0]['provider_name'],'会话日志')
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(),before)
        self.assertNotIn('PRIVATE',str(rows))

    def test_unique_real_proxy_metadata_takes_precedence_over_estimated_mirror(self):
        self.native();self.cc();self.cc(request_id='proxy-one',data_source='proxy',provider_id='provider-one',status_code=500)
        bridge.sync(self.conn,self.source);row=self.logs()[0]
        self.assertFalse(row['timing_estimated']);self.assertEqual(row['status_code'],500)
        self.assertEqual(row['speed_tps'],200);self.assertEqual(row['provider_name'],'公开名称')

    def test_ambiguous_native_identity_never_guesses_a_timing(self):
        self.native();self.native('native-two');self.cc()
        bridge.sync(self.conn,self.source)
        self.assertTrue(all('duration_ms' not in row for row in self.logs()))

    def test_non_native_tool_import_is_idempotent_and_corrects_its_own_values(self):
        self.cc(app_type='gemini',data_source='gemini_session')
        first=bridge.sync(self.conn,self.source);second=bridge.sync(self.conn,self.source)
        self.assertEqual((first['imported'],second['imported']),(1,0))
        self.assertEqual(self.logs()[0]['input_tokens'],200)
        self.cc(app_type='gemini',data_source='gemini_session',output_tokens=600)
        bridge.sync(self.conn,self.source)
        self.assertEqual(len(self.logs()),1);self.assertEqual(self.logs()[0]['output_tokens'],600)

    def test_bad_missing_or_nonpositive_timing_remains_unknown(self):
        self.native();self.cc(latency_ms=0,first_token_ms=None)
        bridge.sync(self.conn,self.source);row=self.logs()[0]
        self.assertIsNone(row['duration_ms']);self.assertIsNone(row['speed_tps'])
        self.assertIsNone(bridge.normalized(dict(app_type='codex',request_id='bad',created_at=self.timestamp,input_tokens=-1)))

    def test_source_unavailable_preserves_previous_details_and_does_not_create_cc_db(self):
        self.native();self.cc();bridge.sync(self.conn,self.source)
        absent=self.source.with_name('missing.sqlite3')
        result=bridge.sync(self.conn,absent)
        self.assertFalse(result['available']);self.assertFalse(absent.exists())
        self.assertEqual(self.logs()[0]['duration_ms'],2000)

    def test_wrong_timestamp_does_not_attach_metadata_even_when_token_counts_match(self):
        self.native();self.cc(created_at=self.timestamp+5)
        bridge.sync(self.conn,self.source)
        self.assertNotIn('duration_ms',self.logs()[0])

    def test_changed_source_identity_revokes_old_enrichment_and_can_match_again(self):
        self.native()
        for correction in ({'output_tokens':301}, {'created_at':self.timestamp+5}):
            with self.subTest(correction=correction):
                self.cc();bridge.sync(self.conn,self.source)
                self.assertEqual(self.logs()[0]['duration_ms'],2000)
                self.cc(**correction);bridge.sync(self.conn,self.source)
                self.assertNotIn('duration_ms',self.logs()[0])
                self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_cc_detail_signatures').fetchone()[0],0)
                self.cc();bridge.sync(self.conn,self.source)
                self.assertEqual(self.logs()[0]['duration_ms'],2000)

    def test_changed_native_counts_revoke_old_enrichment(self):
        self.native();self.cc();bridge.sync(self.conn,self.source)
        self.conn.execute("UPDATE usage_requests SET output_tokens=301,total_tokens=1301 WHERE id='native-one'")
        self.conn.commit()
        bridge.sync(self.conn,self.source)
        self.assertNotIn('duration_ms',self.logs()[0])

    def test_current_ambiguity_revokes_previously_unique_enrichment(self):
        self.native();self.cc();bridge.sync(self.conn,self.source)
        self.cc(request_id='cc-two');bridge.sync(self.conn,self.source)
        self.assertNotIn('duration_ms',self.logs()[0])

    def test_enrichment_outside_current_window_is_preserved(self):
        self.native();self.cc();bridge.sync(self.conn,self.source)
        self.cc(request_id='newer-unmatched',created_at=self.timestamp+5)
        self.conn.execute("UPDATE usage_requests SET output_tokens=301,total_tokens=1301 WHERE id='native-one'")
        self.conn.commit()
        with patch.object(bridge,'ENRICHMENT_LIMIT',1): bridge.sync(self.conn,self.source)
        self.assertEqual(self.logs()[0]['duration_ms'],2000)

    def test_unchanged_enrichment_is_not_rewritten(self):
        self.native();self.cc();bridge.sync(self.conn,self.source)
        self.conn.executescript('''CREATE TEMP TRIGGER reject_detail_update BEFORE UPDATE ON usage_request_details
          BEGIN SELECT RAISE(FAIL,'unchanged enrichment rewritten'); END;
          CREATE TEMP TRIGGER reject_detail_delete BEFORE DELETE ON usage_request_details
          BEGIN SELECT RAISE(FAIL,'unchanged enrichment deleted'); END;''')
        bridge.sync(self.conn,self.source)
        self.assertEqual(self.logs()[0]['duration_ms'],2000)

    def gemini_session(self, **updates):
        values=dict(request_id='gemini-session',app_type='gemini',data_source='gemini_session',provider_id='_gemini_session')
        values.update(updates);self.cc(**values)

    def gemini_proxy(self, **updates):
        values=dict(request_id='gemini-proxy',app_type='gemini',data_source='proxy',provider_id='provider-one')
        values.update(updates);self.cc(**values)

    def test_gemini_session_then_proxy_removes_only_the_bridge_mirror(self):
        self.native();self.gemini_session();bridge.sync(self.conn,self.source)
        self.assertEqual(len(self.logs()),2)
        self.gemini_proxy();result=bridge.sync(self.conn,self.source)
        bridge.sync(self.conn,self.source)
        rows=self.logs()
        self.assertEqual(len(rows),2);self.assertEqual(result['suppressed_gemini_mirrors'],1)
        self.assertEqual({row['id'] for row in rows if row['app']=='codex'},{'native-one'})
        self.assertEqual([row['source'] for row in rows if row['app']=='gemini'],['ccswitch:proxy'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM usage_request_details WHERE request_id LIKE 'ccswitch:%'").fetchone()[0],1)

    def test_gemini_proxy_then_session_and_repeated_sync_count_once(self):
        self.gemini_proxy();bridge.sync(self.conn,self.source)
        self.gemini_session();bridge.sync(self.conn,self.source);bridge.sync(self.conn,self.source)
        self.assertEqual(len(self.logs()),1)
        self.assertEqual(self.logs()[0]['source'],'ccswitch:proxy')

    def test_gemini_uses_cc_raw_counts_and_zero_cache_creation_wildcard(self):
        self.gemini_session(input_tokens=1000,cache_read_tokens=300,input_token_semantics=0)
        self.gemini_proxy(input_tokens=1000,cache_read_tokens=300,cache_creation_tokens=50,input_token_semantics=1)
        bridge.sync(self.conn,self.source)
        self.assertEqual(len(self.logs()),1)
        self.assertEqual(self.logs()[0]['input_tokens'],650)
        self.assertEqual(self.logs()[0]['cache_creation_tokens'],50)

    def test_gemini_same_fingerprint_at_cc_window_boundary_is_a_mirror(self):
        self.gemini_session();self.gemini_proxy(created_at=self.timestamp+600,session_id='different-session',model='MODEL-A')
        bridge.sync(self.conn,self.source)
        self.assertEqual(len(self.logs()),1)

    def test_gemini_unknown_model_can_match_a_concrete_model(self):
        self.gemini_session();self.gemini_proxy(model='UNKNOWN')
        bridge.sync(self.conn,self.source)
        self.assertEqual(len(self.logs()),1)

    def test_gemini_distinct_requests_are_not_suppressed(self):
        for changes in ({'created_at':self.timestamp+601},{'status_code':500},{'model':'other-model'},
                        {'input_tokens':1001},{'cache_creation_tokens':1,'data_source':'other-session'}):
            with self.subTest(changes=changes):
                self.gemini_session();self.gemini_proxy(**changes);bridge.sync(self.conn,self.source)
                self.assertEqual(len(self.logs()),2)

    def test_gemini_changed_session_fingerprint_restores_its_own_request(self):
        self.timestamp=int(time.time())-10*86400
        self.gemini_session();self.gemini_proxy();bridge.sync(self.conn,self.source)
        self.assertEqual(len(self.logs()),1)
        with sqlite3.connect(self.source) as db:
            before=db.execute("SELECT rowid FROM proxy_request_logs WHERE request_id='gemini-session'").fetchone()[0]
            db.execute("UPDATE proxy_request_logs SET output_tokens=301 WHERE request_id='gemini-session'")
            self.assertEqual(db.execute("SELECT rowid FROM proxy_request_logs WHERE request_id='gemini-session'").fetchone()[0],before)
        bridge.sync(self.conn,self.source)
        self.assertEqual(len(self.logs()),2)
        self.assertEqual({row['output_tokens'] for row in self.logs()},{300,301})

    def test_gemini_mirror_stays_excluded_after_cc_archives_both_source_rows(self):
        self.gemini_session();self.gemini_proxy();bridge.sync(self.conn,self.source)
        day=datetime.fromtimestamp(self.timestamp).date().isoformat()
        with sqlite3.connect(self.source) as db:
            db.executescript('''CREATE TABLE usage_daily_rollups (
              date TEXT,app_type TEXT,model TEXT,provider_id TEXT,request_count INTEGER,input_tokens INTEGER,
              output_tokens INTEGER,cache_read_tokens INTEGER,cache_creation_tokens INTEGER,input_token_semantics INTEGER);
              DELETE FROM proxy_request_logs;''')
            db.execute('INSERT INTO usage_daily_rollups VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (day,'gemini','model-a','provider-one',1,200,300,800,0,2))
        for _ in range(2):
            result=bridge.sync(self.conn,self.source)
            self.assertEqual(result['archived_daily']['invalid'],0)
            self.assertEqual(len(self.logs()),1)
            self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM usage_archived_daily').fetchone()[0],0)


if __name__=='__main__': unittest.main()
