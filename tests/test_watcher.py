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
from vibecue.providers.codex import ACTIVITY_TTL, MAX_PRIME, Interpreter, TailReader, ThreadMetadata, Watcher


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


class RunningTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'thread.jsonl'
        self.path.write_bytes(b'')
        self.events = []
        self.now = time.time()
        self.interpreter = Interpreter(ThreadMetadata('main', self.path, '正在执行的对话', 'vscode'),
                                       lambda **event: self.events.append(event))

    def item(self, category, payload, delta=0):
        return record(category, payload, self.now + delta)

    def start(self, delta=0, turn='turn-one'):
        self.interpreter.process(self.item('event_msg', {'type': 'task_started', 'turn_id': turn}, delta))

    def test_running_start_and_final_clear_before_delayed_complete(self):
        self.start()
        task = self.interpreter.running_task(self.now + 1)
        self.assertEqual(task['turn_id'], 'turn-one')
        self.assertEqual(task['started_at'], self.now)
        self.assertEqual(task['provider'], 'codex')
        self.interpreter.process(self.item('response_item', {'type': 'message', 'role': 'assistant',
                                                           'phase': 'final_answer', 'content': []}, 2))
        self.assertIsNone(self.interpreter.running_task(self.now + 3))
        self.interpreter.process(self.item('event_msg', {'type': 'token_count'}, 4))
        self.assertEqual(self.interpreter.activity_state, 'idle')
        self.interpreter.process(self.item('event_msg', {'type': 'task_complete', 'turn_id': 'turn-one'}, 5))
        self.assertIsNone(self.interpreter.running_task(self.now + 6))

    def test_complete_and_abort_clear_running(self):
        for event_type in ('task_complete', 'task_completed', 'task_aborted', 'turn_aborted'):
            with self.subTest(event_type=event_type):
                self.start(1)
                self.interpreter.process(self.item('event_msg', {'type': event_type, 'turn_id': 'turn-one'}, 2))
                self.assertIsNone(self.interpreter.running_task(self.now + 3))

    def test_blocking_question_waits_and_only_matching_output_resumes(self):
        self.start()
        self.interpreter.process(self.item('response_item', {'type': 'function_call', 'name': 'request_user_input', 'call_id': 'question'}, 1))
        self.assertEqual(self.interpreter.activity_state, 'waiting')
        self.assertIsNone(self.interpreter.running_task(self.now + 2))
        self.interpreter.process(self.item('response_item', {'type': 'function_call_output', 'call_id': 'other'}, 2))
        self.assertEqual(self.interpreter.activity_state, 'waiting')
        self.interpreter.process(self.item('response_item', {'type': 'function_call_output', 'call_id': 'question'}, 3))
        self.assertIsNotNone(self.interpreter.running_task(self.now + 4))

    def test_awaited_nested_question_waits_but_async_does_not(self):
        self.start()
        self.interpreter.process(self.item('response_item', {'type': 'custom_tool_call', 'name': 'exec',
                                                           'call_id': 'sync', 'input': 'await tools.request_user_input({});'}, 1))
        self.assertEqual(self.interpreter.activity_state, 'waiting')
        self.interpreter.process(self.item('response_item', {'type': 'custom_tool_call_output', 'call_id': 'sync'}, 2))
        self.assertEqual(self.interpreter.activity_state, 'running')
        for kind, name, code in [('function_call', 'request_user_input_async', ''),
                                 ('custom_tool_call', 'exec', 'await tools.request_user_input_async({});'),
                                 ('custom_tool_call', 'exec', 'text("await tools.request_user_input({})");')]:
            self.interpreter.process(self.item('response_item', {'type': kind, 'name': name, 'input': code}, 3))
            self.assertEqual(self.interpreter.activity_state, 'running')

    def test_explicit_new_work_can_resume_a_wait_with_missing_output(self):
        self.start()
        self.interpreter.process(self.item('response_item', {'type': 'function_call', 'name': 'request_user_input', 'call_id': 'question'}, 1))
        self.interpreter.process(self.item('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'commentary', 'content': []}, 2))
        self.assertEqual(self.interpreter.activity_state, 'running')
        self.assertEqual(self.interpreter.blocking_calls, set())

    def test_old_events_cannot_reverse_newer_state_or_finish_another_turn(self):
        self.start(10, turn='new-turn')
        self.interpreter.process(self.item('event_msg', {'type': 'task_complete', 'turn_id': 'new-turn'}, 9))
        self.interpreter.process(self.item('event_msg', {'type': 'task_complete', 'turn_id': 'old-turn'}, 11))
        self.assertEqual(self.interpreter.running_task(self.now + 12)['turn_id'], 'new-turn')
        self.interpreter.process(self.item('event_msg', {'type': 'task_complete', 'turn_id': 'new-turn'}, 13))
        self.interpreter.process(self.item('response_item', {'type': 'function_call', 'name': 'exec_command'}, 12))
        self.assertIsNone(self.interpreter.running_task(self.now + 14))

    def test_subagent_and_stale_or_undated_activity_are_not_running(self):
        child = Interpreter(ThreadMetadata('child', self.path, source={'subagent': {'thread_spawn': {}}}), lambda **kw: None)
        child.process(self.item('event_msg', {'type': 'task_started', 'turn_id': 'turn'}, 0))
        self.assertIsNone(child.running_task(self.now))
        self.start(-ACTIVITY_TTL - 1)
        self.assertIsNone(self.interpreter.running_task(self.now))
        unknown = Interpreter(ThreadMetadata('unknown', self.path, source='vscode'), lambda **kw: None)
        unknown.process({'type': 'event_msg', 'payload': {'type': 'task_started'}})
        self.assertIsNone(unknown.running_task(self.now))

    def test_prime_recovers_live_turn_without_replaying_alerts(self):
        self.path.write_bytes(line(self.item('event_msg', {'type': 'task_started', 'turn_id': 'live'}, -10)) +
                              line(self.item('response_item', {'type': 'function_call', 'name': 'exec_command', 'call_id': 'tool'}, -5)))
        self.interpreter.started_at = self.now
        self.interpreter.prime_context()
        task = self.interpreter.running_task(self.now)
        self.assertEqual(task['turn_id'], 'live')
        self.assertEqual(task['last_activity'], self.now - 5)
        self.assertEqual(self.events, [])

    def test_prime_recovers_wait_and_ignores_token_only_tail(self):
        self.path.write_bytes(line(self.item('event_msg', {'type': 'task_started', 'turn_id': 'live'}, -10)) +
                              line(self.item('response_item', {'type': 'function_call', 'name': 'request_user_input', 'call_id': 'question'}, -5)))
        self.interpreter.prime_context()
        self.assertEqual(self.interpreter.activity_state, 'waiting')
        self.assertEqual(self.events, [])
        self.path.write_bytes(line(self.item('event_msg', {'type': 'task_started', 'turn_id': 'old'}, -10)) +
                              line(self.item('event_msg', {'type': 'task_complete', 'turn_id': 'old'}, -5)) +
                              line(self.item('event_msg', {'type': 'token_count'}, -1)))
        self.interpreter.prime_context()
        self.assertIsNone(self.interpreter.running_task(self.now))
        self.path.write_bytes(b' ' * (MAX_PRIME + 100) + b'\n' + line(self.item('event_msg', {'type': 'token_count'}, -1)))
        self.interpreter.prime_context()
        self.assertIsNone(self.interpreter.running_task(self.now))

    def test_bounded_prime_can_use_commentary_but_not_partial_last_record(self):
        commentary = self.item('response_item', {'type': 'message', 'role': 'assistant', 'phase': 'commentary', 'content': []}, -1)
        self.path.write_bytes(b'x' * (MAX_PRIME + 100) + b'\n' + line(commentary))
        self.interpreter.prime_context()
        self.assertIsNotNone(self.interpreter.running_task(self.now))
        self.path.write_bytes(line(commentary).rstrip(b'\n'))
        self.interpreter.prime_context()
        self.assertIsNone(self.interpreter.running_task(self.now))

    def watcher(self):
        watcher = Watcher(self.home, self.home / 'vibecue', emit=lambda **kw: self.events.append(kw))
        watcher.metadata = lambda: [self.interpreter.metadata]
        watcher.running = True
        watcher.discover(initial=True)
        return watcher

    def status(self, watcher, approvals=None):
        with patch('vibecue.providers.codex.approval_status', return_value=approvals or {'connected': False}):
            watcher.write_status()
        return json.loads((watcher.vibecue_home / 'watcher-state.json').read_text())

    def test_status_hides_read_errors_and_real_manual_waits_then_recovers(self):
        self.path.write_bytes(line(self.item('event_msg', {'type': 'task_started', 'turn_id': 'live'}, -5)))
        watcher = self.watcher()
        self.assertEqual(len(self.status(watcher)['running_tasks']), 1)
        tail, _ = watcher.watched['main']
        tail.read_error = 'PermissionError: fixture'
        self.assertEqual(self.status(watcher)['running_tasks'], [])
        tail.read_records()
        self.assertEqual(len(self.status(watcher)['running_tasks']), 1)
        self.assertEqual(self.status(watcher, {'connected': True, 'pending_thread_ids': ['main']})['running_tasks'], [])
        self.assertEqual(len(self.status(watcher, {'connected': True, 'pending_thread_ids': []})['running_tasks']), 1)
        watcher.running = False
        self.assertEqual(self.status(watcher)['running_tasks'], [])

    def test_prime_read_failure_recovers_and_replacement_clears_activity(self):
        self.path.write_bytes(line(self.item('event_msg', {'type': 'task_started', 'turn_id': 'live'}, -5)))
        watcher = self.watcher()
        _, interpreter = watcher.watched['main']
        with patch.object(Path, 'open', side_effect=PermissionError(13, 'Permission denied')):
            interpreter.prime_context()
        self.assertIsNone(interpreter.running_task(self.now))
        watcher.poll_once()
        self.assertIsNotNone(interpreter.running_task(self.now))
        replacement = self.home / 'empty.jsonl'
        replacement.write_bytes(b'')
        replacement.replace(self.path)
        watcher.poll_once()
        self.assertIsNone(interpreter.running_task(self.now))


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

    def test_archived_missing_rollout_is_retired_and_moved_path_can_be_rediscovered(self):
        path = self.home / 'old.jsonl'
        path.write_bytes(line(record('event_msg', {'type': 'task_started', 'turn_id': 'live'})))
        entries = [ThreadMetadata('main', path, '对话', 'vscode')]
        events = []
        watcher = Watcher(self.home, self.home / 'vibecue', emit=lambda **event: events.append(event))
        watcher.metadata = lambda: entries
        watcher.discover(initial=True)
        path.unlink()
        entries.clear()  # The archived conversation left active metadata.
        watcher.discover()
        self.assertNotIn('main', watcher.watched)
        moved = self.home / 'moved.jsonl'
        moved.write_bytes(line(record('event_msg', {'type': 'task_started', 'turn_id': 'resumed'})))
        entries.append(ThreadMetadata('main', moved, '恢复的对话', 'vscode'))
        watcher.discover()
        self.assertEqual(watcher.watched['main'][0].path, moved)
        self.assertEqual(watcher.watched['main'][1].running_task()['turn_id'], 'resumed')
        self.assertEqual(events, [])

    def test_missing_metadata_does_not_retire_existing_older_rollout(self):
        path = self.home / 'older.jsonl'
        path.write_bytes(line(record('event_msg', {'type': 'task_started', 'turn_id': 'live'})))
        entries = [ThreadMetadata('older', path, '较早任务', 'vscode')]
        watcher = Watcher(self.home, self.home / 'vibecue', emit=lambda **event: None)
        watcher.metadata = lambda: entries
        watcher.discover(initial=True)
        entries.clear()  # Outside the latest metadata window, but still valid.
        watcher.discover()
        self.assertIn('older', watcher.watched)
        self.assertIsNotNone(watcher.watched['older'][1].running_task())

    def test_metadata_disappearance_does_not_hide_permission_failures(self):
        path = self.home / 'protected.jsonl'
        path.write_bytes(b'')
        entries = [ThreadMetadata('protected', path, '权限问题', 'vscode')]
        watcher = Watcher(self.home, self.home / 'vibecue', emit=lambda **event: None)
        watcher.metadata = lambda: entries
        watcher.discover(initial=True)
        tail, _ = watcher.watched['protected']
        tail.read_error = 'PermissionError: errno=13 Permission denied'
        entries.clear()
        with patch.object(Path, 'stat', side_effect=PermissionError(13, 'Permission denied')):
            watcher.discover()
        self.assertIn('protected', watcher.watched)
        self.assertIn('PermissionError', watcher.watched['protected'][0].read_error)

    def test_metadata_active_missing_rollout_is_kept_for_diagnostics(self):
        path = self.home / 'active.jsonl'
        path.write_bytes(b'')
        watcher = Watcher(self.home, self.home / 'vibecue', emit=lambda **event: None)
        watcher.metadata = lambda: [ThreadMetadata('active', path, '仍活跃', 'vscode')]
        watcher.discover(initial=True)
        path.unlink()
        watcher.discover()
        self.assertIn('active', watcher.watched)


if __name__ == '__main__':
    unittest.main()
