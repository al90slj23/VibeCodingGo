import json
import fcntl
import os
from unittest.mock import patch
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone

from vibecue.events import (emit_event, completion_event_id, classify_final_message,
                           is_main_thread, resolve_title, prune_event_spool, state_databases)
from vibecue.providers.codex import Interpreter, TailReader, ThreadMetadata, Watcher


def record(kind, payload, timestamp=None):
    return {'type': kind, 'timestamp': timestamp or datetime.now(timezone.utc).isoformat(), 'payload': payload}


def line(value):
    return json.dumps(value).encode() + b'\n'


class TailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'rollout.jsonl'
        self.path.write_bytes(b'')

    def test_no_replay_and_partial_append(self):
        old = record('event_msg', {'type': 'task_complete', 'turn_id': 'old'})
        new = record('event_msg', {'type': 'task_complete', 'turn_id': 'new'})
        self.path.write_bytes(line(old))
        tail = TailReader(self.path)
        tail.baseline()
        self.assertEqual(tail.read_records(), [])
        encoded = line(new)
        with self.path.open('ab') as f:
            f.write(encoded[:30])
        self.assertEqual(tail.read_records(), [])
        with self.path.open('ab') as f:
            f.write(encoded[30:])
        self.assertEqual(tail.read_records(), [new])
        self.assertEqual(tail.read_records(), [])

    def test_baseline_partial_does_not_replay_old_record(self):
        old = record('event_msg', {'type': 'task_complete', 'turn_id': 'old'})
        encoded = line(old)
        self.path.write_bytes(encoded[:30])
        tail = TailReader(self.path)
        tail.baseline()
        new = record('event_msg', {'type': 'task_complete', 'turn_id': 'new'})
        with self.path.open('ab') as f:
            f.write(encoded[30:] + line(new))
        self.assertEqual(tail.read_records(), [new])

    def test_read_failure_has_diagnostics_and_recovers_without_advancing(self):
        new = record('event_msg', {'type': 'task_complete', 'turn_id': 'new'})
        self.path.write_bytes(line(new))
        tail = TailReader(self.path, inode=self.path.stat().st_ino)
        with patch.object(Path, 'open', side_effect=PermissionError(13, 'Permission denied')):
            self.assertEqual(tail.read_records(), [])
        self.assertEqual(tail.offset, 0)
        self.assertEqual(tail.last_observed_size, self.path.stat().st_size)
        self.assertIn('errno=13', tail.read_error)
        self.assertGreater(tail.last_read_at, 0)
        self.assertEqual(tail.read_records(), [new])
        self.assertEqual(tail.read_error, '')
        self.assertGreater(tail.last_progress_at, 0)

    def test_replacement_and_truncation(self):
        self.path.write_bytes(line({'old': 'x' * 100}))
        tail = TailReader(self.path)
        tail.baseline()
        self.path.write_bytes(line({'new': 1}))
        self.assertEqual(tail.read_records(), [{'new': 1}])
        replacement = self.path.with_suffix('.tmp')
        replacement.write_bytes(line({'new_inode': 2}))
        replacement.replace(self.path)
        self.assertEqual(tail.read_records(), [{'new_inode': 2}])


class InterpretationTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.interpreter = Interpreter(ThreadMetadata('parent', Path('/unused'), '示範對話', 'vscode'), self.events.append)
        # Interpreter emits kwargs; capture without a wrapper that changes data.
        self.interpreter.emit = lambda **kwargs: self.events.append(kwargs)
        self.interpreter.process(record('event_msg', {'type': 'task_started', 'turn_id': 'turn-1'}))

    def test_completed_turn_and_input_heuristic_have_same_dedupe_id(self):
        self.interpreter.process(record('event_msg', {'type': 'task_complete', 'turn_id': 'turn-1', 'last_agent_message': '已完成並驗證。'}))
        self.assertEqual(self.events[0]['kind'], 'complete')
        self.assertEqual(self.events[0]['event_id'], completion_event_id('parent', 'turn-1'))
        self.interpreter.process(record('event_msg', {'type': 'task_complete', 'turn_id': 'turn-1', 'last_agent_message': '請你選擇下一步。'}))
        self.assertEqual(self.events[1]['kind'], 'input')
        self.assertEqual(self.events[0]['event_id'], self.events[1]['event_id'])
        self.assertIn('依回复文本判断', self.events[1]['message'])

    def test_structured_question_and_async_question(self):
        for name in ('request_user_input', 'request_user_input_async'):
            self.interpreter.process(record('response_item', {'type': 'function_call', 'name': name, 'call_id': name}))
        self.assertEqual([e['kind'] for e in self.events], ['input', 'input'])
        self.assertNotEqual(self.events[0]['event_id'], self.events[1]['event_id'])

    def test_nested_calls_ignore_literals_and_comments(self):
        examples = [
            'await tools.exec_command({cmd:"await tools.request_user_input_async({})"});',
            '// await tools.request_user_input_async({})\ntext(1);',
            'text(`await tools.request_user_input_async({})`);',
        ]
        for code in examples:
            self.interpreter.process(record('response_item', {'type': 'custom_tool_call', 'name': 'exec', 'input': code}))
        self.assertEqual(self.events, [])
        self.interpreter.process(record('response_item', {'type': 'custom_tool_call', 'name': 'exec', 'call_id': 'real', 'input': 'await tools.request_user_input_async({questions: [{title: "請選擇"}]});'}))
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['kind'], 'input')

    def test_subagent_and_interrupt_do_not_alert(self):
        child = Interpreter(ThreadMetadata('child', Path('/unused'), source={'subagent': {'thread_spawn': {'parent_thread_id': 'parent'}}}), lambda **kw: self.events.append(kw))
        child.process(record('event_msg', {'type': 'task_complete', 'turn_id': 'child-turn'}))
        self.interpreter.process(record('event_msg', {'type': 'turn_aborted', 'turn_id': 'turn-1'}))
        self.assertEqual(self.events, [])

    def test_permission_not_guessed_from_escalated_shell(self):
        self.interpreter.process(record('response_item', {'type': 'custom_tool_call', 'name': 'exec', 'input': 'await tools.exec_command({cmd:"long command",sandbox_permissions:"require_escalated"});'}))
        self.assertEqual(self.events, [])

    def test_ignore_prelaunch_records(self):
        self.interpreter.started_at = time.time()
        self.interpreter.process(record('event_msg', {'type': 'task_complete'}, '2020-01-01T00:00:00Z'))
        self.assertEqual(self.events, [])

    def test_final_questions_need_explicit_user_request(self):
        for text in ('測試通過。', '如何運作？以下已說明。', '不需要你確認。', '> 請你確認。', '```\n請你選擇\n```'):
            self.assertEqual(classify_final_message(text)[0], 'complete', text)
        for text in ('請你確認再繼續。', '需要您選擇。', '请你确认再继续。', '需要您选择。', 'Please confirm the destination.', 'I need your approval.'):
            self.assertEqual(classify_final_message(text)[0], 'input', text)


class MetadataAndSpoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_dedupe_survives_consumption_and_process_lifetime(self):
        args = dict(kind='complete', thread_id='thread', turn_id='turn', title='對話', vibecue_home=self.home)
        self.assertTrue(emit_event(**args))
        files = list((self.home / 'events').glob('*.json'))
        self.assertEqual(len(files), 1)
        event = json.loads(files[0].read_text())
        self.assertEqual(event['title'], '對話')
        files[0].unlink()
        self.assertFalse(emit_event(**args))
        self.assertEqual(list((self.home / 'events').glob('*.json')), [])
        self.assertEqual((self.home / 'emitted.sqlite').stat().st_mode & 0o777, 0o600)

    def test_disabled_marker_skips_without_poisoning_dedupe(self):
        (self.home / 'disabled').touch()
        args = dict(kind='input', thread_id='thread', turn_id='turn', title='對話', vibecue_home=self.home)
        self.assertFalse(emit_event(**args))
        self.assertFalse((self.home / 'emitted.sqlite').exists())
        (self.home / 'disabled').unlink()
        self.assertTrue(emit_event(**args))

    def test_spool_prunes_expired_and_caps_backlog(self):
        spool = self.home / 'events'
        spool.mkdir()
        now = time.time()
        for i in range(505):
            path = spool / f'{i:04d}.json'
            path.write_text('{}')
            os.utime(path, (now - i / 100, now - i / 100))
        expired = spool / 'expired.json'
        expired.write_text('{}')
        os.utime(expired, (now - 121, now - 121))
        prune_event_spool(spool, now)
        self.assertFalse(expired.exists())
        self.assertEqual(len(list(spool.glob('*.json'))), 500)
        self.assertTrue((spool / '0000.json').exists())
        self.assertFalse((spool / '0504.json').exists())
        self.assertTrue(emit_event('complete', 'thread', 'new', title='新事件', vibecue_home=self.home))
        self.assertEqual(len(list(spool.glob('*.json'))), 500)

    def test_singleton_does_not_overwrite_running_owner_status(self):
        vibecue_home = self.home / 'vibecue'
        vibecue_home.mkdir()
        status = vibecue_home / 'watcher-state.json'
        status.write_text('{"owner":"existing"}')
        lock_path = vibecue_home / 'watcher.lock'
        with lock_path.open('w') as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            watcher = Watcher(self.home, vibecue_home)
            watcher.run()
            self.assertFalse(watcher.snapshot_once())
            self.assertEqual(json.loads(status.read_text()), {'owner': 'existing'})
        watcher = Watcher(self.home, vibecue_home)
        watcher.stopping = True
        watcher.run()
        self.assertFalse(json.loads(status.read_text())['running'])

    def test_newest_numeric_schema_is_preferred(self):
        for version, title in ((5, 'old'), (10, 'new')):
            db = self.home / f'state_{version}.sqlite'
            with closing(sqlite3.connect(db)) as c, c:
                c.execute('CREATE TABLE threads(id TEXT, title TEXT, name TEXT, source TEXT, rollout_path TEXT, updated_at REAL, archived INTEGER)')
                c.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)',
                          ('thread', title, None, 'vscode', '/unused', time.time(), 0))
        self.assertEqual([p.name for p in state_databases(self.home)], ['state_10.sqlite', 'state_5.sqlite'])
        self.assertEqual(resolve_title('thread', self.home), 'new')
        watcher = Watcher(self.home, self.home / 'vibecue')
        self.assertEqual(watcher.metadata()[0].title, 'new')

    def test_transient_emit_failure_retries_and_preserves_later_batch_events(self):
        transcript = self.home / 'thread.jsonl'
        transcript.write_bytes(b'')
        attempts, delivered = [], []
        def flaky(**event):
            attempts.append(event['event_id'])
            if len(attempts) == 1:
                raise sqlite3.OperationalError('database is locked')
            delivered.append(event['event_id'])
        watcher = Watcher(self.home, self.home / 'vibecue', emit=flaky)
        watcher.metadata = lambda: [ThreadMetadata('main', transcript, '對話', 'vscode')]
        watcher.discover(initial=True)
        with transcript.open('ab') as stream:
            stream.write(line(record('event_msg', {'type': 'task_complete', 'turn_id': 'one'})))
            stream.write(line(record('event_msg', {'type': 'task_complete', 'turn_id': 'two'})))
        watcher.poll_once()
        interpreter = watcher.watched['main'][1]
        self.assertEqual(len(interpreter.pending), 2)
        self.assertEqual(delivered, [])
        interpreter.next_retry = 0
        watcher.poll_once()
        self.assertEqual(delivered, ['finished:main:one', 'finished:main:two'])
        self.assertEqual(len(interpreter.pending), 0)
        self.assertEqual(len(attempts), 3)

    def test_pending_delivery_expires_instead_of_retrying_forever(self):
        def unavailable(**event):
            raise OSError('spool unavailable')
        interpreter = Interpreter(ThreadMetadata('main', Path('/unused'), '對話', 'vscode'), unavailable)
        with patch('vibecue.providers.codex.time.monotonic', return_value=10):
            interpreter.send('input', '需要回答', 'question')
        with patch('vibecue.providers.codex.time.monotonic', return_value=131):
            interpreter.flush_pending()
        self.assertEqual(len(interpreter.pending), 0)
        self.assertEqual(interpreter.dropped_deliveries, 1)

    def test_malformed_record_does_not_discard_later_completion(self):
        transcript = self.home / 'thread.jsonl'
        transcript.write_bytes(b'')
        delivered = []
        watcher = Watcher(self.home, self.home / 'vibecue', emit=lambda **event: delivered.append(event))
        watcher.metadata = lambda: [ThreadMetadata('main', transcript, '對話', 'vscode')]
        watcher.discover(initial=True)
        with transcript.open('ab') as stream:
            stream.write(line(record('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'final_answer', 'content': 15})))
            stream.write(line(record('event_msg', {'type': 'task_complete', 'turn_id': 'after-malformed'})))
        watcher.poll_once()
        self.assertEqual([e['turn_id'] for e in delivered], ['after-malformed'])

    def test_read_only_metadata_and_subagent_transcript_priority(self):
        db = self.home / 'state_5.sqlite'
        with closing(sqlite3.connect(db)) as c, c:
            c.execute('CREATE TABLE threads(id TEXT, title TEXT, name TEXT, source TEXT)')
            c.execute('INSERT INTO threads VALUES (?,?,?,?)', ('parent', 'generated title', '使用者命名', 'vscode'))
        self.assertEqual(resolve_title('parent', self.home), '使用者命名')
        transcript = self.home / 'child.jsonl'
        transcript.write_bytes(line(record('session_meta', {'source': {'subagent': {'thread_spawn': {}}}})))
        self.assertFalse(is_main_thread('parent', transcript, self.home))
        self.assertTrue(is_main_thread('parent', codex_home=self.home))

    def test_startup_baselines_existing_threads_then_catches_new_completion(self):
        db = self.home / 'state_5.sqlite'
        transcript = self.home / 'thread.jsonl'
        transcript.write_bytes(line(record('event_msg', {'type': 'task_complete', 'turn_id': 'old'})))
        with closing(sqlite3.connect(db)) as c, c:
            c.execute('CREATE TABLE threads(id TEXT, rollout_path TEXT, title TEXT, name TEXT, source TEXT, updated_at REAL, archived INTEGER)')
            c.execute('INSERT INTO threads VALUES (?,?,?,?,?,?,?)', ('main', str(transcript), '標題', None, 'vscode', time.time(), 0))
        events = []
        watcher = Watcher(self.home, self.home / 'vibecue', emit=lambda **kwargs: events.append(kwargs))
        watcher.discover(initial=True)
        watcher.poll_once()
        self.assertEqual(events, [])
        self.assertEqual(watcher.watched['main'][1].turn_id, 'old')
        with transcript.open('ab') as f:
            f.write(line(record('event_msg', {'type': 'task_complete', 'turn_id': 'new'})))
        watcher.poll_once()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['title'], '標題')


if __name__ == '__main__':
    unittest.main()
