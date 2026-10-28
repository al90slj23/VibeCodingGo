from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from vibecue import thread_info as info


def event(kind, at, **payload):
    return {'type': 'event_msg', 'timestamp': at, 'payload': {'type': kind, **payload}}


def message(role, text, at, phase='commentary', identifier=None):
    payload = {'type': 'message', 'role': role, 'phase': phase,
               'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}
    if identifier:
        payload['id'] = identifier
    return {'type': 'response_item', 'timestamp': at, 'payload': payload}


class ThreadInfoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.project = self.home / 'project'; self.project.mkdir()
        self.transcript = self.home / 'session.jsonl'
        self.database = self.home / 'state_5.sqlite'
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('CREATE TABLE threads (id TEXT PRIMARY KEY,title TEXT,name TEXT,cwd TEXT,rollout_path TEXT,source TEXT,archived INTEGER,updated_at REAL)')
            conn.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)',
                         ('thread-one', 'Original name', 'Visible name', str(self.project), str(self.transcript), 'vscode', 0, 1000))
            conn.commit()

    def write(self, rows):
        self.transcript.write_bytes(b''.join((json.dumps(r)+'\n').encode() for r in rows))

    def read(self, **kwargs):
        return info.read('thread-one', self.home, **kwargs)

    def test_read_only_metadata_and_visible_roles_no_tools_reasoning_or_auth(self):
        self.write([event('task_started', 1000, turn_id='turn-one'),
                    message('user', 'Please check this change', 1001),
                    {'type': 'response_item', 'timestamp': 1002, 'payload': {'type': 'reasoning', 'text': 'hidden reasoning'}},
                    {'type': 'response_item', 'timestamp': 1003, 'payload': {'type': 'function_call', 'arguments': 'private tool arguments'}},
                    message('assistant', 'Checked the requested change', 1004, 'final_answer'),
                    event('task_complete', 1005, turn_id='turn-one', last_agent_message='Checked the requested change')])
        original_db, original_log = self.database.read_bytes(), self.transcript.read_bytes()
        result = self.read()
        self.assertEqual(result['title'], 'Visible name'); self.assertEqual(result['cwd'], str(self.project))
        self.assertEqual([r['role'] for r in result['messages']], ['user', 'assistant'])
        self.assertEqual(result['state'], 'complete'); self.assertEqual(result['last_turn_id'], 'turn-one')
        self.assertNotIn('hidden reasoning', json.dumps(result)); self.assertNotIn('private tool arguments', json.dumps(result))
        self.assertEqual(original_db, self.database.read_bytes()); self.assertEqual(original_log, self.transcript.read_bytes())

    def test_identifier_validation_and_source_paths_never_come_from_query(self):
        for identifier in ['../thread', '', 'x;touch-file', True, None]:
            with self.assertRaises(info.ThreadInfoError): info.read(identifier, self.home)
        with self.assertRaises(info.ThreadInfoError): info.read('not-there', self.home)
        self.write([message('user', 'hello', 1000)])
        self.assertEqual(self.read()['actual_transcript_path'], str(self.transcript))

    def test_newest_numeric_database_is_authoritative_and_named_title_preferred(self):
        latest = self.home / 'state_10.sqlite'
        with closing(sqlite3.connect(latest)) as conn:
            conn.execute('CREATE TABLE threads (id TEXT PRIMARY KEY,title TEXT,rollout_path TEXT)')
            conn.execute('INSERT INTO threads VALUES (?,?,?)', ('thread-one', 'Newest', str(self.transcript)))
            conn.commit()
        self.write([message('assistant', 'current', 1000)])
        self.assertEqual(self.read()['title'], 'Newest')

    def test_subagent_record_is_rejected(self):
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('UPDATE threads SET source=?', ('{"subagent":{"thread_spawn":{}}}',)); conn.commit()
        with self.assertRaises(info.ThreadInfoError): self.read()

    def test_metadata_cannot_redirect_viewer_to_auth_or_a_symlink_to_config(self):
        forbidden=self.home/'auth.json';forbidden.write_text('{"secret":"never read"}')
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('UPDATE threads SET rollout_path=?',(str(forbidden),));conn.commit()
        with patch.object(Path,'open',side_effect=AssertionError('must not open the configured target')):
            result=self.read()
        self.assertFalse(result['source_readable'])
        with closing(sqlite3.connect(self.database)) as conn:
            conn.execute('UPDATE threads SET rollout_path=?',(str(self.transcript),));conn.commit()
        self.transcript.symlink_to(forbidden)
        with patch.object(Path,'open',side_effect=AssertionError('must not open a linked credential file')):
            result=self.read()
        self.assertFalse(result['source_readable'])

    def test_credentials_and_host_wrappers_are_removed_but_human_request_remains(self):
        secret = 'sk-' + 'a'*24
        self.write([event('task_started', 1000, turn_id='turn-one'),
                    message('user', '<environment_context>computer facts</environment_context>', 1001),
                    message('user', '<in-app-browser-context source="ambient-ui-state">ambient secrets</in-app-browser-context>\nPlease fix the form', 1002),
                    message('user', '<turn_aborted>host interruption</turn_aborted>', 1003),
                    message('assistant', 'api_key='+secret+'\nAuthorization: Bearer abcdefsecret', 1004),
                    message('system', 'private system instruction', 1005)])
        text='\n'.join(r['text'] for r in self.read()['messages'])
        self.assertIn('Please fix the form', text); self.assertIn('[已隐藏凭据]', text)
        for value in [secret, 'abcdefsecret', 'computer facts', 'ambient secrets', 'host interruption', 'private system instruction']:
            self.assertNotIn(value,text)

    def test_mirror_dedupe_preserves_identical_messages_in_distinct_turns(self):
        self.write([event('task_started', 1000, turn_id='one'), message('assistant','same',1001),
                    event('agent_message',1002,message='same'),event('task_complete',1003,turn_id='one',last_agent_message='same'),
                    event('task_started',1004,turn_id='two'),message('assistant','same',1005)])
        result=self.read()
        self.assertEqual(len(result['messages']),2)
        self.assertEqual([r['turn_id'] for r in result['messages']],['one','two'])

    def test_stable_message_id_replaces_streamed_body_instead_of_appending(self):
        self.write([message('assistant','partial',1000,identifier='msg-one'),message('assistant','partial answer',1001,identifier='msg-one')])
        result=self.read(); self.assertEqual(len(result['messages']),1);self.assertEqual(result['messages'][0]['text'],'partial answer')

    def test_limits_preserve_recent_messages_and_explicit_partial_coverage(self):
        self.write([message('user',str(n)+' '+('word '*5000),1000+n) for n in range(100)])
        result=self.read()
        self.assertLessEqual(len(result['messages']),info.MAX_MESSAGES)
        self.assertLessEqual(sum(len(r['text']) for r in result['messages']),info.MAX_CHARACTERS)
        self.assertTrue(result['coverage']['truncated']);self.assertTrue(result['coverage']['partial'])
        self.assertTrue(result['messages'][-1]['text'].startswith('99 '))

    def test_incomplete_last_json_record_waits_for_next_refresh(self):
        self.write([message('user','first',1000)])
        with self.transcript.open('ab') as stream:stream.write(json.dumps(message('assistant','not complete',1001)).encode()[:-2])
        result=self.read();self.assertEqual(len(result['messages']),1);self.assertTrue(result['coverage']['incomplete_last_record'])

    def test_unchanged_fingerprint_and_metadata_only_avoid_reading_bodies(self):
        self.write([message('user','real text',1000)])
        initial=self.read()
        with patch.object(info,'_plain_tail',side_effect=AssertionError('must not re-read body')):
            same=self.read(if_fingerprint=initial['fingerprint'])
            paths=self.read(metadata_only=True)
        self.assertTrue(same['unchanged']);self.assertTrue(paths['metadata_only'])
        self.assertEqual(same['messages'],[]);self.assertTrue(same['source_readable'])
        with self.transcript.open('ab') as stream:stream.write((json.dumps(message('assistant','new',1001))+'\n').encode())
        newer=self.read(if_fingerprint=initial['fingerprint']);self.assertNotEqual(initial['fingerprint'],newer['fingerprint'])
        self.assertEqual(len(newer['messages']),2)

    def test_missing_source_retains_verified_path_metadata_and_controlled_warning(self):
        result=self.read();self.assertFalse(result['source_readable']);self.assertEqual(result['cwd'],str(self.project))
        self.assertEqual(result['messages'],[]);self.assertTrue(result['message'])

    def test_compressed_fallback_is_read_only_and_keeps_bounded_tail(self):
        program=info.zstd_program()
        if not program:self.skipTest('zstd unavailable')
        self.write([message('user','compressed request',1000),message('assistant','compressed reply',1001)])
        original=self.transcript.read_bytes();compressed=self.transcript.with_name(self.transcript.name+'.zst')
        subprocess.run([program,'-q','-o',str(compressed),str(self.transcript)],check=True)
        original_compressed=compressed.read_bytes();self.transcript.unlink()
        result=self.read()
        self.assertTrue(result['coverage']['compressed']);self.assertTrue(result['source_readable'])
        self.assertEqual(result['actual_transcript_path'],str(compressed));self.assertEqual(len(result['messages']),2)
        self.assertFalse(self.transcript.exists());self.assertEqual(original_compressed,compressed.read_bytes())

    def test_compressed_failure_is_not_fake_empty_success(self):
        source=self.transcript.with_name(self.transcript.name+'.zst');source.write_bytes(b'not zstd')
        result=self.read();self.assertFalse(result['source_readable']);self.assertTrue(result['message'])

    def test_compressed_timeout_kills_decoder_and_never_leaves_a_process(self):
        original=info.subprocess.Popen;started=[]
        def launch(*args,**kwargs):
            child=original([sys.executable,'-c','import time; time.sleep(5)'],**kwargs);started.append(child);return child
        with patch.object(info,'zstd_program',return_value='/known/zstd'),patch.object(info.subprocess,'Popen',side_effect=launch):
            with self.assertRaises(info.ThreadInfoError):info._compressed_tail(self.transcript,timeout=.05)
        self.assertEqual(len(started),1);self.assertIsNotNone(started[0].poll())

    def test_concurrent_log_change_invalidates_fingerprint(self):
        self.write([message('user','first',1000)])
        original=info._plain_tail
        def append_during_read(source,limit):
            answer=original(source,limit)
            with source.open('ab') as stream:stream.write((json.dumps(message('assistant','later',1001))+'\n').encode())
            return answer
        with patch.object(info,'_plain_tail',side_effect=append_during_read):result=self.read()
        self.assertNotIn('fingerprint',result);self.assertTrue(result['coverage']['partial'])

    def test_explicit_question_waiting_clears_on_matching_answer(self):
        call={'type':'response_item','timestamp':1001,'payload':{'type':'function_call','name':'functions.request_user_input','call_id':'q1','arguments':'private'}}
        self.write([event('task_started',1000,turn_id='one'),call])
        self.assertEqual(self.read()['state'],'waiting')
        with self.transcript.open('ab') as stream:
            stream.write((json.dumps({'type':'response_item','timestamp':1002,'payload':{'type':'function_call_output','call_id':'q1','output':'private answer'}})+'\n').encode())
        self.assertEqual(self.read()['state'],'running')


if __name__=='__main__':unittest.main()
