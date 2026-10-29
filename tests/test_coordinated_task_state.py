"""Synthetic app-tool dispatches; no real chat mutations or desktop restart."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibecue.codex_runtime import RuntimeProjection
from vibecue.codex_control import ControlError
from vibecue.desktop_attention import AttentionProjection, DesktopAttentionMonitor, DesktopAttentionService
from vibecue.providers.codex import Interpreter, ThreadMetadata, TailReader, Watcher
from vibecue.providers.task_recovery import TaskRecovery


NOW = 1_900_000_000.0


def item(category, payload, timestamp=NOW):
    return {'type': category, 'timestamp': timestamp, 'payload': payload}


def snapshot(turn='receiver-turn', status='inProgress', kind='active', flags=None, revision=1):
    return {'type': 'snapshot', 'revision': revision, 'conversationState': {
        'id': 'receiver', 'resumeState': 'resumed', 'requests': [],
        'threadRuntimeStatus': {'type': kind, 'activeFlags': flags or []},
        'turnHistory': {'kind': 'canonical', 'history': {
            'entitiesByKey': {'tail': {'turnId': turn, 'status': status, 'items': [{'text': 'PRIVATE BODY'}]}},
            'islands': [{'entries': [{'value': 'tail'}]}]}}}}


class CoordinatedTranscriptTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.interpreter = Interpreter(ThreadMetadata('receiver', Path('/unused'), 'receiver', 'vscode'),
                                       lambda **event: self.events.append(event))

    def start(self, turn='receiver-turn', timestamp=NOW):
        self.interpreter.process(item('event_msg', {'type': 'task_started', 'turn_id': turn,
            'root_turn_id': 'dispatcher-turn', 'turn_attribution': {
                'turn_id': turn, 'root_turn_id': 'dispatcher-turn', 'parent_turn_id': 'dispatcher-turn',
                'turn_trigger': 'app_tool_send_message'}}, timestamp))

    def test_receiver_uses_own_turn_and_late_old_completion_cannot_emit_todo(self):
        self.start('old-turn', NOW - 10)
        self.interpreter.process(item('event_msg', {'type': 'task_complete', 'turn_id': 'old-turn'}, NOW - 9))
        self.events.clear()
        self.start()
        self.interpreter.process(item('event_msg', {'type': 'task_complete', 'turn_id': 'old-turn',
            'root_turn_id': 'dispatcher-turn', 'last_agent_message': '请你确认。'}, NOW + 1))
        self.assertEqual(self.interpreter.turn_id, 'receiver-turn')
        self.assertEqual(self.interpreter.running_task(NOW + 2)['turn_id'], 'receiver-turn')
        self.assertEqual(self.events, [])

    def test_context_and_running_steer_do_not_resolve_question_or_restart_clock(self):
        self.start()
        self.interpreter.process(item('response_item', {'type': 'function_call', 'name': 'request_user_input', 'call_id': 'question'}, NOW + 1))
        self.interpreter.process(item('turn_context', {'turn_id': 'receiver-turn', 'root_turn_id': 'dispatcher-turn'}, NOW + 2))
        self.assertEqual(self.interpreter.activity_state, 'waiting')
        self.assertEqual(self.interpreter.blocking_calls, {'question'})
        self.interpreter.process(item('response_item', {'type': 'function_call_output', 'call_id': 'question'}, NOW + 3))
        self.interpreter.process(item('turn_context', {'turn_id': 'receiver-turn'}, NOW + 4))
        self.assertEqual(self.interpreter.running_task(NOW + 5)['started_at'], NOW)

    def test_late_context_never_restarts_finished_turn(self):
        self.start()
        self.interpreter.process(item('event_msg', {'type': 'task_complete', 'turn_id': 'receiver-turn'}, NOW + 1))
        self.interpreter.process(item('turn_context', {'turn_id': 'receiver-turn'}, NOW + 2))
        self.assertIsNone(self.interpreter.running_task(NOW + 3))
        self.assertEqual(self.interpreter.detail_snapshot(NOW + 3)['status'], 'complete')

    def test_dispatch_wait_output_is_parent_activity_and_never_receiver_completion(self):
        self.start()
        parent = Interpreter(ThreadMetadata('dispatcher', Path('/unused'), source='vscode'), lambda **event: None)
        parent.process(item('event_msg', {'type': 'task_started', 'turn_id': 'dispatcher-turn'}))
        for name in ('mcp__codex_app__send_message_to_thread', 'mcp__codex_app__create_thread', 'mcp__codex_app__wait_threads'):
            parent.process(item('response_item', {'type': 'function_call', 'name': name, 'call_id': name}, NOW + 1))
            parent.process(item('response_item', {'type': 'function_call_output', 'call_id': name,
                'output': json.dumps({'threadId': 'receiver', 'status': 'complete'})}, NOW + 2))
        self.assertEqual(parent.activity_state, 'running')
        self.assertEqual(self.interpreter.activity_state, 'running')
        self.assertEqual(self.events, [])

    def test_two_new_dispatches_retire_both_old_turns(self):
        self.start('first')
        self.start('second', NOW + 1)
        self.start('third', NOW + 2)
        for turn in ('first', 'second'):
            self.interpreter.process(item('event_msg', {'type': 'task_complete', 'turn_id': turn}, NOW + 3))
        self.assertEqual(self.events, [])
        self.assertEqual(self.interpreter.running_task(NOW + 4)['turn_id'], 'third')
        self.interpreter.process(item('event_msg', {'type': 'task_complete', 'turn_id': 'third'}, NOW + 5))
        self.assertEqual([event['turn_id'] for event in self.events], ['third'])

    def test_native_new_turn_supersedes_old_complete_and_rejects_late_file_event(self):
        self.start('old-turn', NOW - 10)
        self.interpreter.process(item('event_msg', {'type': 'task_complete', 'turn_id': 'old-turn'}, NOW - 9))
        self.events.clear()
        projection = RuntimeProjection('receiver', 'owner', clock=lambda: NOW)
        projection.apply(snapshot())
        self.interpreter.observe_runtime(projection.snapshot(), NOW)
        detail = self.interpreter.detail_snapshot(NOW + 1)
        self.assertEqual(detail['turn_id'], 'receiver-turn')
        self.assertEqual(detail['status'], 'running')
        self.assertIsNone(detail['finished_at'])
        self.assertIsNone(detail['total_tokens'])
        self.interpreter.process(item('event_msg', {'type': 'task_complete', 'turn_id': 'old-turn'}, NOW + 1))
        self.assertEqual(self.events, [])
        # Snapshot receipt time must not reject earlier-written new-turn rows.
        self.interpreter.process(item('event_msg', {'type': 'task_started', 'turn_id': 'receiver-turn'}, NOW - .1))
        self.interpreter.process(item('response_item', {'type': 'function_call', 'name': 'exec_command'}, NOW + 2))
        self.assertEqual(self.interpreter.running_task(NOW + 3)['turn_id'], 'receiver-turn')

    def test_native_old_complete_cannot_end_new_running_or_revive_final(self):
        self.start()
        projection = RuntimeProjection('receiver', 'owner', clock=lambda: NOW + 2)
        projection.apply(snapshot('old-turn', 'completed', 'idle'))
        self.interpreter.observe_runtime(projection.snapshot(), NOW + 2)
        self.assertIsNotNone(self.interpreter.running_task(NOW + 2))
        self.interpreter.process(item('event_msg', {'type': 'task_complete', 'turn_id': 'receiver-turn'}, NOW + 3))
        projection.apply(snapshot(revision=2))
        self.interpreter.observe_runtime(projection.snapshot(), NOW + 3)
        self.assertIsNone(self.interpreter.running_task(NOW + 4))

    def test_earlier_native_running_does_not_resolve_newer_blocking_question(self):
        self.start()
        projection = RuntimeProjection('receiver', 'owner', clock=lambda: NOW)
        projection.apply(snapshot())
        self.interpreter.process(item('response_item', {'type': 'function_call', 'name': 'request_user_input', 'call_id': 'question'}, NOW + 1))
        self.interpreter.observe_runtime(projection.snapshot(), NOW + 2)
        self.assertEqual(self.interpreter.activity_state, 'waiting')
        self.assertEqual(self.interpreter.blocking_calls, {'question'})


class NativeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.now = NOW
        self.projection = RuntimeProjection('receiver', 'owner', clock=lambda: self.now)

    def test_canonical_order_status_patches_and_streaming_bodies_are_discarded(self):
        update = snapshot()
        update['conversationState']['turns'] = [{'turnId': 'legacy-stale', 'status': 'completed'}]
        self.projection.apply(update)
        self.assertEqual(self.projection.snapshot()['state'], 'running')
        self.now += 1
        self.projection.apply({'type': 'patches', 'baseRevision': 1, 'revision': 2, 'patches': [
            {'op': 'replace', 'path': ['turnHistory', 'history', 'entitiesByKey', 'tail', 'items'], 'value': 'PRIVATE DELTA'},
            {'op': 'add', 'path': ['turnHistory', 'history', 'entitiesByKey', 'new'], 'value': {'turnId': 'next-turn', 'status': 'inProgress', 'text': 'PRIVATE TURN'}},
            {'op': 'add', 'path': ['turnHistory', 'history', 'islands', 0, 'entries', 1], 'value': {'value': 'new'}}]})
        self.assertEqual(self.projection.snapshot()['turn_id'], 'next-turn')
        self.assertNotIn('PRIVATE', json.dumps(self.projection.data))
        self.now += 1
        self.projection.apply({'type': 'patches', 'baseRevision': 2, 'revision': 3, 'patches': [
            {'op': 'replace', 'path': ['turnHistory', 'history', 'entitiesByKey', 'new', 'status'], 'value': 'completed'},
            {'op': 'replace', 'path': ['threadRuntimeStatus'], 'value': {'type': 'idle'}}]})
        self.assertEqual(self.projection.snapshot()['state'], 'complete')

    def test_canonical_missing_or_empty_tail_unknown_does_not_use_legacy_completion(self):
        for history in ({}, {'entitiesByKey': {}, 'islands': []}):
            update = snapshot()
            update['conversationState']['turns'] = [{'turnId': 'old', 'status': 'completed'}]
            update['conversationState']['turnHistory']['history'] = history
            self.projection.apply(update)
            self.assertFalse(self.projection.snapshot()['authoritative'])

    def test_flags_missing_and_stale_subscription_supply_no_completion_evidence(self):
        update = snapshot()
        del update['conversationState']['threadRuntimeStatus']['activeFlags']
        self.projection.apply(update)
        self.assertEqual(self.projection.snapshot()['state'], 'unknown')
        self.projection.apply(snapshot())
        self.now += 31
        self.projection.mark_live()
        self.assertFalse(self.projection.snapshot()['authoritative'])
        with self.assertRaises(ControlError):
            self.projection.apply({'type': 'patches', 'baseRevision': 1, 'revision': 2, 'patches': []})
        self.projection.apply(snapshot(revision=2))
        self.assertTrue(self.projection.snapshot()['authoritative'])

    def test_handoff_starts_new_owner_epoch_without_old_runtime_or_request(self):
        first = AttentionProjection('receiver', 'owner-one')
        first.apply(snapshot('old-turn', 'completed', 'idle'))
        second = AttentionProjection('receiver', 'owner-two')
        self.assertFalse(second.runtime.snapshot()['authoritative'])
        self.assertEqual(second.requests, [])
        second.apply(snapshot('new-turn'))
        self.assertEqual(second.runtime.snapshot()['turn_id'], 'new-turn')
        self.assertEqual(second.runtime.owner, 'owner-two')

    def test_only_surviving_current_turn_request_is_human_wait(self):
        service = DesktopAttentionService('/fixture')
        service._health['connected'] = True
        service.set_threads(['receiver'])
        projection = AttentionProjection('receiver', 'owner')
        service.projections['receiver'] = projection
        service.monitor.clock = lambda: self.now
        update = snapshot(flags=['waitingOnApproval'])
        request = {'id': 1, 'method': 'item/commandExecution/requestApproval',
                   'params': {'threadId': 'receiver', 'turnId': 'receiver-turn'}}
        update['conversationState']['requests'] = [request]
        with patch('vibecue.codex_runtime.time.time', return_value=NOW):
            projection.runtime.clock = lambda: self.now
            projection.apply(update)
        service.monitor.update(projection)
        self.assertEqual(service.current_runtime()['receiver']['state'], 'running')
        self.assertEqual(service.snapshot()['pending_count'], 0)
        self.now += 1
        self.assertEqual(service.current_runtime()['receiver']['state'], 'waiting')
        self.assertEqual(service.snapshot()['pending_count'], 1)
        update = snapshot(revision=2)
        request['params']['turnId'] = 'old-turn'
        update['conversationState']['requests'] = [request]
        projection.apply(update); service.monitor.update(projection)
        self.assertEqual(service.snapshot()['pending_count'], 0)
        service._health['connected'] = False
        self.assertEqual(service.current_runtime(), {})


class RuntimeRecoveryTests(unittest.TestCase):
    def test_unknown_or_old_terminal_after_restart_retains_interrupted_record(self):
        with tempfile.TemporaryDirectory() as directory:
            recovery = TaskRecovery(Path(directory), NOW - 10)
            task = {'thread_id': 'receiver', 'turn_id': 'unfinished', 'provider': 'codex',
                    'title': 'receiver', 'started_at': NOW - 5, 'last_activity': NOW,
                    'last_work_at': NOW, 'source_kind': 'desktop'}
            runtime = {'available': True, 'desktop': [{'id': 'old-owner', 'started_at': NOW - 100}]}
            observation = {'thread_id': 'receiver', 'state': 'running', 'task': task, 'source_readable': True}
            recovery.reconcile([observation], runtime, NOW)
            runtime['desktop'] = [{'id': 'new-owner', 'started_at': NOW + 1}]
            _, interrupted = recovery.reconcile([dict(observation, task=None)], runtime, NOW + 2)
            self.assertEqual(len(interrupted), 1)
            for observation in [
                {'thread_id': 'receiver', 'state': 'unknown'},
                {'thread_id': 'receiver', 'state': 'idle', 'terminal_at': NOW + 4,
                 'terminal_turn_id': 'previously-completed', 'terminal_kind': 'task_complete'}]:
                _, interrupted = recovery.reconcile([observation], runtime, NOW + 5)
                self.assertEqual(interrupted[0]['turn_id'], 'unfinished')

    def test_watcher_exports_receiver_current_state_using_existing_subscription(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); transcript = home / 'receiver.jsonl'; transcript.write_text('')
            with patch('vibecue.providers.codex.DesktopAttentionService') as desktop, \
                    patch('vibecue.providers.codex.approval_status', return_value={'connected': False}), \
                    patch('vibecue.providers.codex.time.time', return_value=NOW), \
                    patch('vibecue.providers.codex.codex_process_snapshot', return_value={'available': False}):
                watcher = Watcher(home / 'codex', home / 'cue')
                watcher.running = True
                interpreter = Interpreter(ThreadMetadata('receiver', transcript, 'receiver', 'vscode'), lambda **event: None)
                watcher.watched = {'receiver': (TailReader(transcript), interpreter)}
                projection = RuntimeProjection('receiver', 'owner', clock=lambda: NOW)
                projection.apply(snapshot())
                desktop.return_value.current_runtime.return_value = {'receiver': projection.snapshot()}
                desktop.return_value.current_settings.return_value = {}
                desktop.return_value.snapshot.return_value = {'connected': False, 'capabilities': {'attention': False}}
                watcher.write_status()
                state = json.loads((home / 'cue/watcher-state.json').read_text())
                self.assertEqual(state['running_tasks'][0]['turn_id'], 'receiver-turn')
                self.assertTrue(state['current_task_states']['receiver']['authoritative'])
                self.assertEqual(state['current_task_states']['receiver']['source'], 'codex-desktop-owner-stream')
                desktop.return_value.start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
