from datetime import datetime, timedelta
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from vibecue.usage import query, sync, connect, zstd_program, number, stamp


def counts(inputs, outputs=0, cached=0, created=0, reasoning=0):
    return dict(input_tokens=inputs, output_tokens=outputs, cached_input_tokens=cached,
                cache_write_input_tokens=created, reasoning_output_tokens=reasoning)


def meta(session='s1', parent=None, timestamp='2026-10-01T00:00:00Z'):
    payload=dict(id=session,timestamp=timestamp)
    if parent:
        payload['forked_from_id']=parent
    return dict(type='session_meta',timestamp=timestamp,payload=payload)


def context(model):
    return dict(type='turn_context',payload={'model':model})


def token(total=None,last=None,timestamp='2026-10-01T01:00:00Z',bucket=None):
    info={}
    if total is not None:info['total_token_usage']=total
    if last is not None:info['last_token_usage']=last
    payload=dict(type='token_count',info=info)
    if bucket:payload['rate_limits']={'limit_id':bucket}
    return dict(type='event_msg',timestamp=timestamp,payload=payload)


def claude(identifier,output=20,timestamp='2026-10-01T01:00:00Z',model='claude-sonnet'):
    return dict(type='assistant',sessionId='claude-session',timestamp=timestamp,
                message=dict(id=identifier,model=model,usage=dict(input_tokens=100,
                  output_tokens=output,cache_read_input_tokens=900,cache_creation_input_tokens=50)))


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.home=self.base/'home';self.home.mkdir()
        self.codex=self.base/'codex';self.codex.mkdir()
        self.claude=self.base/'claude';self.claude.mkdir()
        (self.home/'usage-sources.json').write_text(json.dumps(dict(codex_roots=[str(self.codex)],claude_roots=[str(self.claude)],opencode_roots=[])))

    def write(self,name,records,app='codex',partial=b''):
        path=(self.codex if app=='codex' else self.claude)/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b''.join((json.dumps(x)+'\n').encode() for x in records)+partial)
        return path

    def result(self,**kw):
        return query(self.home,**kw)

    def test_opencode_sync_discovery_provider_filter_and_duplicate_roots(self):
        import sqlite3
        root=self.base/'opencode';root.mkdir()
        path=root/'opencode.db'
        with sqlite3.connect(path) as source:
            source.execute('CREATE TABLE message (id TEXT PRIMARY KEY,session_id TEXT,time_created INTEGER,time_updated INTEGER,data TEXT)')
            value={'role':'assistant','modelID':'model-open','providerID':'provider-open',
                   'tokens':{'input':10,'output':3,'reasoning':2,'cache':{'read':8,'write':1}}}
            source.execute('INSERT INTO message VALUES (?,?,?,?,?)',('msg-one','session-one',1790816400000,1790816400000,json.dumps(value)))
        config=self.home/'usage-sources.json'
        roots=json.loads(config.read_text());roots['opencode_roots']=[str(root),str(path)]
        config.write_text(json.dumps(roots))
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],1)
        self.assertEqual(data['summary']['total_tokens'],24)
        self.assertEqual(self.result(app='opencode')['summary']['requests'],1)
        self.assertEqual(self.result(source='opencode_provider:provider-open')['summary']['total_tokens'],24)
        self.assertIn({'id':'opencode_provider:provider-open','name':'OpenCode · provider-open'},data['filters']['sources'])
        self.assertIn('opencode',data['supported_apps'])
        coverage=next(row for row in data['sync']['coverage'] if row['app']=='opencode')
        self.assertEqual(coverage['files'],1)
        self.assertTrue(coverage['available'])
        self.assertEqual(sync(self.home)['sync']['imported'],0)

    def test_bridge_reuse_hint_only_when_native_usage_did_not_change(self):
        from vibecue import usage_ccswitch
        path = self.write('native.jsonl', [meta(), context('model'), token(last=counts(10, 1))])
        with patch.object(usage_ccswitch, 'sync', wraps=usage_ccswitch.sync) as bridge_sync:
            first = sync(self.home)
            self.assertFalse(bridge_sync.call_args.kwargs['native_unchanged'])
            second = sync(self.home)
            self.assertTrue(bridge_sync.call_args.kwargs['native_unchanged'])
            with path.open('ab') as stream:
                stream.write((json.dumps(token(last=counts(20, 2), timestamp='2026-10-01T02:00:00Z')) + '\n').encode())
            sync(self.home)
            self.assertFalse(bridge_sync.call_args.kwargs['native_unchanged'])
        self.assertEqual(second['sync']['bytes_read'], 0)
        self.assertEqual(first['summary']['requests'], 1)

    def test_coverage_warning_survives_unchanged_incremental_scan(self):
        self.write('bad.jsonl', [meta(), token(last=counts(10,1))], partial=b'not-json\n')
        first = sync(self.home)
        self.assertEqual(first['sync']['malformed_records'], 1)
        again = sync(self.home)
        self.assertEqual(again['sync']['malformed_records'], 0)
        self.assertTrue(again['sync']['historical_gaps'])
        self.assertTrue(again['sync']['warnings'])
        self.assertEqual(again['summary']['total_tokens'], 11)

    def test_codex_last_usage_cache_reasoning_and_rate_refresh(self):
        first=token(counts(1000,200,800),counts(100,20,80,5,7),bucket='a')
        second=token(counts(1100,230,850),counts(50,10,20),timestamp='2026-10-01T02:00:00Z',bucket='a')
        duplicate=token(counts(1100,230,850),counts(50,10,20),timestamp='2026-10-01T02:00:01Z',bucket='b')
        self.write('one.jsonl',[meta(),context('gpt-6'),first,second,duplicate])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],2)
        self.assertEqual(data['summary']['input_tokens'],45)
        self.assertEqual(data['summary']['output_tokens'],30)
        self.assertEqual(data['summary']['cache_read_tokens'],100)
        self.assertEqual(data['summary']['cache_creation_tokens'],5)
        self.assertEqual(data['summary']['total_tokens'],180)
        self.assertEqual(data['sync']['duplicates'],1)

    def test_cumulative_fallback_does_not_sum_snapshots(self):
        self.write('one.jsonl',[meta(),context('a'),token(counts(100,10,80)),
                    token(counts(160,20,100),timestamp='2026-10-01T02:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],2)
        self.assertEqual(data['summary']['total_tokens'],180)
        self.assertEqual(data['summary']['input_tokens'],60)

    def test_explicit_zero_last_is_authoritative_and_sets_fallback_baseline(self):
        zero=counts(0)
        zero['total_tokens']=80259
        path=self.write('one.jsonl',[meta(),token(counts(41105375,25492,28451328,reasoning=8330),zero)])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],0)
        conn=connect(self.home)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM usage_codex_fallbacks').fetchone()[0],0)
        conn.close()
        # A genuinely missing last field still permits a delta from the zero
        # snapshot's cumulative baseline, including after an importer restart.
        with path.open('a') as stream:
            stream.write(json.dumps(token(counts(41105475,25502,28451368),timestamp='2026-10-01T02:00:00Z'))+'\n')
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],1)
        self.assertEqual(data['summary']['total_tokens'],110)
        self.assertEqual(data['summary']['input_tokens'],60)
        self.assertEqual(data['summary']['cache_read_tokens'],40)

    def test_total_only_continuation_pages_share_session_highwater(self):
        self.write('a.jsonl',[meta('same-session'),token(counts(100,10))])
        self.write('b.jsonl',[meta('same-session'),token(counts(150,20),timestamp='2026-10-01T02:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['total_tokens'],170)
        self.assertEqual(data['summary']['requests'],2)
        self.assertEqual(sorted(row['total_tokens'] for row in data['logs']),[60,110])

    def test_out_of_order_continuation_repairs_previously_derived_delta(self):
        self.write('a-later.jsonl',[meta('same-session'),token(counts(150,20),timestamp='2026-10-01T02:00:00Z')])
        self.assertEqual(sync(self.home)['summary']['total_tokens'],170)
        self.write('z-earlier.jsonl',[meta('same-session'),token(counts(100,10))])
        data=sync(self.home)
        self.assertEqual(data['summary']['total_tokens'],170)
        self.assertEqual(data['summary']['requests'],2)
        self.assertEqual(sorted(row['total_tokens'] for row in data['logs']),[60,110])
        self.assertEqual(sync(self.home)['summary']['total_tokens'],170)

    def test_exact_last_page_sets_baseline_for_other_total_only_pages(self):
        self.write('a.jsonl',[meta('s'),token(counts(100,10),counts(100,10))])
        self.write('b.jsonl',[meta('s'),token(counts(150,20),timestamp='2026-10-01T02:00:00Z')])
        self.assertEqual(sync(self.home)['summary']['total_tokens'],170)

    def test_copy_with_added_last_field_replaces_fallback_representation(self):
        self.write('a.jsonl',[meta('s'),token(counts(100,10))])
        sync(self.home)
        self.write('b.jsonl',[meta('s'),token(counts(100,10),counts(100,10))])
        data=sync(self.home)
        # Exact events arriving later also need to trigger delta reconciliation.
        self.assertEqual(data['summary']['total_tokens'],110)

    def test_equal_last_only_requests_at_distinct_times_are_both_counted(self):
        self.write('one.jsonl',[meta(),token(last=counts(30,5)),
                   token(last=counts(30,5),timestamp='2026-10-01T02:00:00Z')])
        self.assertEqual(sync(self.home)['summary']['total_tokens'],70)

    def test_incremental_partial_and_restart_are_idempotent(self):
        event=token(counts(100,10),counts(100,10))
        raw=json.dumps(event).encode()
        path=self.write('one.jsonl',[meta()],partial=raw[:len(raw)//2])
        self.assertEqual(sync(self.home)['summary']['requests'],0)
        with path.open('ab') as f:f.write(raw[len(raw)//2:]+b'\n')
        self.assertEqual(sync(self.home)['summary']['total_tokens'],110)
        self.assertEqual(sync(self.home)['summary']['requests'],1)
        self.assertEqual(sync(self.home)['sync']['imported'],0)

    def test_move_copy_and_rewrite_keep_stable_request_identity(self):
        path=self.write('one.jsonl',[meta(),token(counts(100,10),counts(100,10))])
        sync(self.home)
        path.rename(path.with_name('archived.jsonl'))
        shutil.copy2(path.with_name('archived.jsonl'),path.with_name('copy.jsonl'))
        self.assertEqual(sync(self.home)['summary']['requests'],1)
        self.write('copy.jsonl',[meta(),token(counts(100,10),counts(100,10)),
                   token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z')])
        self.assertEqual(sync(self.home)['summary']['total_tokens'],170)

    def test_claude_final_update_and_subagent_logs(self):
        path=self.write('project/session/subagents/workflows/wf_1/a.jsonl',[claude('m1',1)],app='claude')
        self.assertEqual(sync(self.home)['summary']['total_tokens'],1051)
        with path.open('ab') as f:f.write((json.dumps(claude('m1',20))+'\n').encode())
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],1)
        self.assertEqual(data['summary']['total_tokens'],1070)
        self.assertEqual(data['summary']['input_tokens'],100)
        self.assertEqual(data['sync']['updated'],1)
        self.write('duplicate.jsonl',[claude('m1',2)],app='claude')
        self.assertEqual(sync(self.home)['summary']['total_tokens'],1070)

    def test_fork_prefix_is_not_charged_to_child(self):
        inherited=token(counts(100,10),counts(100,10))
        self.write('parent.jsonl',[meta('parent'),context('a'),inherited])
        self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),context('a'),inherited,
                   token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],2)
        self.assertEqual(data['summary']['total_tokens'],170)
        self.assertEqual(sum(x['total_tokens'] for x in data['logs'] if x['thread_id']=='child'),60)

    def test_fork_rewritten_same_millisecond_prefix_keeps_physical_order(self):
        events=[token(counts(100,10),counts(100,10)),
                token(counts(150,20),counts(50,10),timestamp='2026-10-01T01:10:00Z'),
                token(counts(210,30),counts(60,10),timestamp='2026-10-01T01:20:00Z')]
        replay=json.loads(json.dumps(events))
        for event in replay:event['timestamp']='2026-10-01T01:30:01Z'
        self.write('parent.jsonl',[meta('parent'),*events])
        self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),meta('parent'),*replay,
                   token(counts(280,40),counts(70,10),timestamp='2026-10-01T02:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],4)
        self.assertEqual(data['summary']['total_tokens'],320)
        self.assertEqual(sum(x['total_tokens'] for x in data['logs'] if x['thread_id']=='child'),80)
        self.assertEqual(data['sync']['deferred_files'],0)
        self.assertEqual(sync(self.home)['summary']['total_tokens'],320)

    def test_replay_alignment_resumes_after_restart_before_new_usage(self):
        events=[token(counts(100,10),counts(100,10)),
                token(counts(150,20),counts(50,10),timestamp='2026-10-01T01:10:00Z')]
        replay=json.loads(json.dumps(events))
        for event in replay:event['timestamp']='2026-10-01T01:30:01Z'
        self.write('parent.jsonl',[meta('parent'),*events])
        path=self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),replay[0]])
        self.assertEqual(sync(self.home)['summary']['total_tokens'],170)
        with path.open('a') as stream:
            stream.write(json.dumps(replay[1])+'\n')
            stream.write(json.dumps(token(counts(220,30),counts(70,10),timestamp='2026-10-01T02:00:00Z'))+'\n')
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],3)
        self.assertEqual(data['summary']['total_tokens'],250)

    def test_identical_old_usage_after_first_new_request_is_not_global_deduped(self):
        original=token(counts(100,10),counts(100,10))
        self.write('parent.jsonl',[meta('parent'),original])
        repeated=json.loads(json.dumps(original));repeated['timestamp']='2026-10-01T02:00:01Z'
        self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),
                   token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z'),repeated])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],3)
        self.assertEqual(data['summary']['total_tokens'],280)

    def test_nested_rewritten_replay_is_available_to_the_next_generation(self):
        original=token(counts(100,10),counts(100,10))
        child_replay=json.loads(json.dumps(original));child_replay['timestamp']='2026-10-01T01:30:01Z'
        child_live=token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z')
        grand_replay=json.loads(json.dumps([child_replay,child_live]))
        for event in grand_replay:event['timestamp']='2026-10-01T02:30:01Z'
        self.write('parent.jsonl',[meta('parent'),original])
        self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),child_replay,child_live])
        self.write('grand.jsonl',[meta('grand','child','2026-10-01T02:30:00Z'),*grand_replay,
                   token(counts(220,30),counts(70,10),timestamp='2026-10-01T03:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],3)
        self.assertEqual(data['summary']['total_tokens'],250)
        self.assertEqual(data['sync']['deferred_files'],0)

    def test_zero_status_before_sparse_ancestor_replay_does_not_end_prefix(self):
        original=token(counts(100,10),counts(100,10))
        self.write('ancestor.jsonl',[meta('ancestor'),original])
        # The immediate parent retained only its own suffix. Its ancestor is
        # the evidence for the child's first copied nonzero snapshot.
        self.write('parent.jsonl',[meta('parent','ancestor','2026-10-01T01:30:00Z'),
                   token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z')])
        zero=counts(0);zero['total_tokens']=93522
        copied=json.loads(json.dumps(original));copied['timestamp']='2026-10-01T02:30:01Z'
        self.write('child.jsonl',[meta('child','parent','2026-10-01T02:30:00Z'),
                   token(counts(90,9),zero,timestamp='2026-10-01T02:30:01Z'),copied,
                   token(counts(220,30),counts(70,10),timestamp='2026-10-01T03:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],3)
        self.assertEqual(data['summary']['total_tokens'],250)
        self.assertEqual(sum(x['total_tokens'] for x in data['logs'] if x['thread_id']=='child'),80)
        self.assertEqual(data['sync']['deferred_files'],0)
        self.assertEqual(sync(self.home)['summary']['total_tokens'],250)

    def test_ancestor_matches_remain_ordered_and_stop_at_first_new_request(self):
        first=token(counts(100,10),counts(100,10))
        second=token(counts(150,20),counts(50,10),timestamp='2026-10-01T01:10:00Z')
        self.write('ancestor.jsonl',[meta('ancestor'),first,second])
        self.write('parent.jsonl',[meta('parent','ancestor','2026-10-01T01:30:00Z'),
                   token(counts(220,30),counts(70,10),timestamp='2026-10-01T02:00:00Z')])
        replay=json.loads(json.dumps([second,first,second]))
        for event in replay:event['timestamp']='2026-10-01T02:30:01Z'
        self.write('child.jsonl',[meta('child','parent','2026-10-01T02:30:00Z'),*replay])
        data=sync(self.home)
        # After the inherited second event, the earlier first event cannot
        # move the replay cursor backwards. It and the later equal request
        # remain billable once the prefix has ended.
        self.assertEqual(data['summary']['requests'],5)
        self.assertEqual(data['summary']['total_tokens'],420)
        self.assertEqual(sum(x['total_tokens'] for x in data['logs'] if x['thread_id']=='child'),170)

    def test_ancestor_work_after_parent_fork_is_not_inherited_by_child(self):
        later=token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z')
        self.write('ancestor.jsonl',[meta('ancestor'),token(counts(100,10),counts(100,10)),later])
        self.write('parent.jsonl',[meta('parent','ancestor','2026-10-01T01:30:00Z'),
                   token(counts(220,30),counts(70,10),timestamp='2026-10-01T02:15:00Z')])
        same_counts=json.loads(json.dumps(later));same_counts['timestamp']='2026-10-01T03:00:01Z'
        self.write('child.jsonl',[meta('child','parent','2026-10-01T03:00:00Z'),same_counts])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],4)
        self.assertEqual(data['summary']['total_tokens'],310)
        self.assertEqual(sum(x['total_tokens'] for x in data['logs'] if x['thread_id']=='child'),60)

    def test_import_upgrade_backs_up_and_rebuilds_only_codex(self):
        self.write('parent.jsonl',[meta('parent'),token(counts(100,10),counts(100,10))])
        self.write('claude.jsonl',[claude('stable-claude')],app='claude')
        first=sync(self.home)
        reminder=self.home/'recent-alerts.json';reminder.write_text('reminder fixture')
        with connect(self.home) as conn:
            conn.execute("DELETE FROM usage_meta WHERE key='codex_import_version'")
            conn.execute("UPDATE usage_requests SET total_tokens=999 WHERE app='codex'")
            conn.commit()
        rebuilt=sync(self.home)
        self.assertTrue(rebuilt['sync']['rebuild'])
        self.assertEqual(rebuilt['summary']['total_tokens'],first['summary']['total_tokens'])
        self.assertEqual(reminder.read_text(),'reminder fixture')
        backups=list((self.home/'backups').glob('usage-before-codex-replay-v4-*.sqlite3'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].stat().st_mode&0o777,0o600)
        import sqlite3
        with sqlite3.connect(backups[0]) as saved:
            self.assertEqual(saved.execute("SELECT total_tokens FROM usage_requests WHERE app='codex'").fetchone()[0],999)
        self.assertEqual(self.result(app='claude')['summary']['requests'],1)
        self.assertNotIn('rebuild',sync(self.home)['sync'])

    def test_import_upgrade_backup_failure_preserves_existing_codex_ledger(self):
        self.write('parent.jsonl',[meta('parent'),token(counts(100,10),counts(100,10))])
        sync(self.home)
        with connect(self.home) as conn:
            conn.execute("DELETE FROM usage_meta WHERE key='codex_import_version'")
            conn.commit()
        (self.home/'backups').write_text('blocked directory fixture')
        result=sync(self.home)
        self.assertTrue(result['sync']['errors'])
        self.assertEqual(result['summary']['requests'],1)
        self.assertEqual(result['summary']['total_tokens'],110)

    def test_inherited_parent_metadata_cannot_replace_physical_file_owner(self):
        inherited=token(counts(100,10),counts(100,10))
        self.write('parent.jsonl',[meta('parent'),context('a'),inherited])
        self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),meta('parent'),
                    context('a'),inherited,token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['total_tokens'],170)
        self.assertEqual(data['sync']['deferred_files'],0)
        self.assertEqual({row['thread_id'] for row in data['logs']},{'parent','child'})
        with connect(self.home) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM usage_codex_sessions WHERE complete=0').fetchone()[0],0)

    def test_nested_fork_metadata_replay_keeps_each_generation_separate(self):
        first=token(counts(100,10),counts(100,10))
        second=token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z')
        third=token(counts(180,25),counts(30,5),timestamp='2026-10-01T03:00:00Z')
        self.write('parent.jsonl',[meta('parent'),first])
        self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),meta('parent'),first,second])
        self.write('grandchild.jsonl',[meta('grandchild','child','2026-10-01T02:30:00Z'),
                   meta('child','parent','2026-10-01T01:30:00Z'),meta('parent'),first,second,third])
        data=sync(self.home)
        self.assertEqual(data['summary']['total_tokens'],205)
        self.assertEqual(data['summary']['requests'],3)
        self.assertEqual({row['thread_id'] for row in data['logs']},{'parent','child','grandchild'})
        self.assertEqual(data['sync']['deferred_files'],0)

    def test_child_total_only_starts_at_parent_baseline(self):
        self.write('parent.jsonl',[meta('parent'),token(counts(100,10))])
        self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),
                   token(counts(150,20),timestamp='2026-10-01T02:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['total_tokens'],170)

    def test_missing_parent_is_deferred_and_retryable(self):
        self.write('child.jsonl',[meta('child','parent'),token(counts(100,10),counts(100,10))])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],0)
        self.assertEqual(data['sync']['deferred_files'],1)
        self.assertTrue(data['sync']['warnings'])
        self.write('parent.jsonl',[meta('parent')])
        self.assertEqual(sync(self.home)['summary']['requests'],1)

    def test_filters_pagination_groups_and_heatmap_range(self):
        self.write('a.jsonl',[meta('a'),context('model-a'),token(last=counts(20,3))])
        self.write('b.jsonl',[meta('b'),context('model-b'),token(last=counts(30,4))])
        self.write('c.jsonl',[claude('c')],app='claude')
        sync(self.home)
        data=self.result(app='codex',page_size=1,page=2)
        self.assertEqual(data['summary']['requests'],2)
        self.assertEqual(data['pagination']['total'],2)
        self.assertEqual(len(data['logs']),1)
        self.assertEqual(self.result(model='model-a')['summary']['total_tokens'],23)
        self.assertEqual(self.result(source='claude_session')['summary']['requests'],1)
        groups=self.result(view='models',page_size=1)
        self.assertEqual(groups['pagination']['total'],3)
        self.assertEqual(len(groups['models']),1)
        now=datetime(2026,10,2).timestamp()
        current=self.result(period='today',now=now)
        self.assertEqual(current['summary']['requests'],0)
        self.assertEqual(sum(day['requests'] for day in current['days']),3)

    def test_bad_missing_or_negative_usage_is_not_guessed(self):
        no_date=token(last=counts(10,1));no_date.pop('timestamp')
        self.write('a.jsonl',[meta(),context('a'),token(last={'input_tokens':-1,'output_tokens':1}),
                   token(last={'output_tokens':10}),token(),no_date])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],0)
        self.assertTrue(data['sync']['warnings'])

    def test_unknown_model_does_not_use_current_configuration(self):
        self.write('a.jsonl',[meta(),token(last=counts(10,1))])
        data=sync(self.home)
        self.assertEqual(data['logs'][0]['model'],'unknown')
        self.assertEqual(data['logs'][0]['source'],'codex_session')

    def test_explicit_compaction_usage_is_merged_when_regular_snapshot_covers_it(self):
        usage=counts(100,10,80)
        explicit={'type':'token_usage_record','timestamp':'2026-10-01T01:00:00Z',
                  'payload':{'usage':usage,'response_id':'response-one'}}
        self.write('a.jsonl',[meta(),context('a'),explicit,token(usage,usage)])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],1)
        self.assertEqual(data['summary']['total_tokens'],110)
        self.assertEqual(data['sync']['merged_records'],1)

    def test_explicit_compaction_survives_zero_snapshot_and_restart(self):
        usage=counts(100,10,80)
        explicit={'type':'token_usage_record','timestamp':'2026-10-01T01:00:00Z',
                  'payload':{'usage':usage,'response_id':'response-one'}}
        path=self.write('a.jsonl',[meta(),context('a'),explicit])
        self.assertEqual(sync(self.home)['summary']['total_tokens'],110)
        with path.open('ab') as f:f.write((json.dumps(token(usage,counts(0)))+'\n').encode())
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],1)
        self.assertEqual(data['summary']['total_tokens'],110)

    def test_compaction_followed_by_total_only_does_not_count_same_usage_twice(self):
        usage=counts(100,10,80)
        explicit={'type':'token_usage_record','timestamp':'2026-10-01T01:00:00Z',
                  'payload':{'usage':usage,'response_id':'response-one'}}
        self.write('a.jsonl',[meta(),context('a'),explicit,token(usage),
                    token(counts(150,20,100),timestamp='2026-10-01T02:00:00Z')])
        data=sync(self.home)
        self.assertEqual(data['summary']['requests'],2)
        self.assertEqual(data['summary']['total_tokens'],170)
        self.assertTrue(data['sync']['warnings'])

    def test_fork_does_not_reimport_inherited_explicit_usage(self):
        explicit={'type':'token_usage_record','timestamp':'2026-10-01T01:00:00Z',
                  'payload':{'usage':counts(100,10),'response_id':'response-one'}}
        self.write('parent.jsonl',[meta('parent'),explicit])
        self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),explicit,
                    token(last=counts(10,1),timestamp='2026-10-01T02:00:00Z')])
        self.assertEqual(sync(self.home)['summary']['total_tokens'],121)

    def test_nonfinite_counts_and_timestamps_are_rejected(self):
        for value in (float('nan'),float('inf'),float('-inf')):
            self.assertIsNone(number(value))
            self.assertIsNone(stamp(value))
        self.write('a.jsonl',[meta(),token(last=counts(float('nan'),1)),
                   token(last=counts(10,1),timestamp=float('nan'))])
        self.assertEqual(sync(self.home)['summary']['requests'],0)

    @unittest.skipUnless(zstd_program(),'zstd decoder unavailable')
    def test_compressed_parent_discovery_and_fork_alignment(self):
        inherited=token(counts(100,10),counts(100,10))
        self.write('parent.jsonl',[meta('parent'),inherited])
        child=self.write('child.jsonl',[meta('child','parent','2026-10-01T01:30:00Z'),inherited,
                  token(counts(150,20),counts(50,10),timestamp='2026-10-01T02:00:00Z')])
        for path in self.codex.glob('*.jsonl'):
            with path.with_suffix('.jsonl.zst').open('wb') as output:
                subprocess.run([zstd_program(),'-q','-c','--',str(path)],stdout=output,check=True)
            path.unlink()
        data=sync(self.home)
        self.assertEqual(data['summary']['total_tokens'],170)
        self.assertEqual(data['sync']['deferred_files'],0)
        self.assertEqual(sync(self.home)['sync']['bytes_read'],0)

    def test_privacy_schema_does_not_store_prompts_or_credentials(self):
        event=meta();event['payload']['base_instructions']='secret prompt'
        self.write('a.jsonl',[event,context('a'),token(last=counts(10,1))])
        sync(self.home)
        with connect(self.home) as conn:
            dump='\n'.join(conn.iterdump())
        self.assertNotIn('secret prompt',dump)
        self.assertEqual((self.home/'usage.sqlite3').stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
