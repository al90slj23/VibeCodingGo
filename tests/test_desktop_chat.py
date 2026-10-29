import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibecue.codex_chat import DesktopChatController, desktop_run, run
from vibecue.codex_control import ControlError, UnconfirmedUpdate
from vibecue.codex_desktop_ipc import DesktopConnectionInterrupted, DesktopOwnerUnavailable


REQUEST = '51d32d31-6afc-4e54-bcb8-b9390df6cf99'


class DesktopFixture:
    thread_id = 'thread-one'

    def __init__(self, kind='idle'):
        self.snapshot = {'thread_id': self.thread_id, 'resume_state': 'resumed',
                         'thread_runtime_status': {'type': kind, 'activeFlags': []},
                         'requests_pending': False,
                         'last_turn': {'turnId': 'current-turn', 'status': 'inProgress'}}
        self.sent = []
        self.result = {'turn': {'id': 'accepted-turn'}}
        self.error = None

    def require_capability(self, name):
        if name != 'reply': raise AssertionError(name)

    def runtime_snapshot(self):
        return self.snapshot.copy()

    def start_turn(self, value):
        self.sent.append(value)
        if self.error:
            raise self.error
        return self.result


class DesktopChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.channel = DesktopFixture()
        self.controller = DesktopChatController(self.channel, home=self.home)
        self.params = {'thread_id': 'thread-one', 'text': '用户主动发送的文字', 'request_id': REQUEST}

    def test_status_and_load_only_read_existing_owner(self):
        self.assertTrue(self.controller.status({'thread_id': 'thread-one'})['can_send'])
        self.assertTrue(self.controller.load({'thread_id': 'thread-one'})['can_send'])
        self.assertEqual(self.channel.sent, [])

    def test_status_explicitly_clears_prediction_without_triggering_generation(self):
        self.channel.snapshot['last_turn'] = {'turnId': 'finished-turn', 'status': 'completed'}
        self.channel.snapshot['prediction'] = {'text': 'UNTRUSTED UNSUPPORTED FIELD'}
        result = self.controller.status({'thread_id': 'thread-one'})
        self.assertIsNone(result['prompt_suggestion'])
        self.assertEqual(result['prompt_suggestion_status'], 'not_exposed')
        self.assertEqual(result['latest_turn_id'], 'finished-turn')
        self.assertNotIn('UNTRUSTED', json.dumps(result))
        self.assertEqual(self.channel.sent, [])

    def test_running_waiting_and_unknown_status_never_reuse_ghost_text(self):
        for kind, pending in [('active', False), ('idle', True), ('unknown', False)]:
            self.channel.snapshot['thread_runtime_status'] = {'type': kind, 'activeFlags': []}
            self.channel.snapshot['requests_pending'] = pending
            result = self.controller.status({'thread_id': 'thread-one'})
            self.assertIsNone(result['prompt_suggestion'])
        self.channel.snapshot['last_turn'] = {'turnId': '../invalid'}
        self.assertIsNone(self.controller.status({'thread_id': 'thread-one'})['latest_turn_id'])
        self.assertEqual(self.channel.sent, [])

    def test_active_owner_is_sendable_only_with_known_turn(self):
        self.channel.snapshot['thread_runtime_status']['type'] = 'active'
        self.assertTrue(self.controller.status({'thread_id': 'thread-one'})['can_send'])
        self.channel.snapshot['last_turn'] = {}
        self.assertFalse(self.controller.status({'thread_id': 'thread-one'})['can_send'])
        self.channel.snapshot['last_turn'] = {'turnId': 'old-turn', 'status': 'completed'}
        self.assertFalse(self.controller.status({'thread_id': 'thread-one'})['can_send'])
        self.channel.snapshot['last_turn']['status'] = 'inProgress'
        self.channel.snapshot['thread_runtime_status'].pop('activeFlags')
        self.assertFalse(self.controller.status({'thread_id': 'thread-one'})['can_send'])

    def test_waiting_and_unconfirmed_runtime_cannot_send(self):
        self.channel.snapshot['requests_pending'] = True
        self.assertEqual(self.controller.status({'thread_id': 'thread-one'})['state'], 'waiting')
        self.assertEqual(self.controller.send(self.params)['status'], 'failed')
        self.channel.snapshot.pop('requests_pending')
        self.assertFalse(self.controller.status({'thread_id': 'thread-one'})['can_send'])
        self.channel.snapshot['requests_pending'] = False
        self.channel.snapshot['resume_state'] = 'loading'
        self.assertFalse(self.controller.status({'thread_id': 'thread-one'})['can_send'])
        self.assertEqual(self.channel.sent, [])

    def test_send_is_exact_text_and_private_receipt_deduplicates(self):
        result = self.controller.send(self.params)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(self.channel.sent, [{'threadId': 'thread-one',
                                            'input': [{'type': 'text', 'text': self.params['text']}],
                                            'clientUserMessageId': REQUEST}])
        self.assertTrue(self.controller.send(self.params)['duplicate'])
        self.assertEqual(len(self.channel.sent), 1)
        receipt = (self.home / 'control/chat-receipts.json').read_text()
        self.assertNotIn(self.params['text'], receipt)
        self.assertEqual((self.home / 'control/chat-receipts.json').stat().st_mode & 0o777, 0o600)

    def test_active_input_uses_owner_start_with_settings_inherited(self):
        self.channel.snapshot['thread_runtime_status']['type'] = 'active'
        params = dict(self.params, expected_turn_id='current-turn')
        result = self.controller.send(params)
        self.assertEqual(result['status'], 'accepted')
        self.assertEqual(result['turn_id'], 'accepted-turn')
        self.assertNotIn('expectedTurnId', self.channel.sent[0])
        self.assertEqual(set(self.channel.sent[0]), {'threadId', 'input', 'clientUserMessageId'})

    def test_changed_turn_before_submission_is_not_sent(self):
        self.channel.snapshot['thread_runtime_status']['type'] = 'active'
        result = self.controller.send(dict(self.params, expected_turn_id='earlier-turn'))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.channel.sent, [])

    def test_post_submission_timeout_or_invalid_result_never_retries(self):
        self.channel.error = UnconfirmedUpdate('simulated timeout')
        result = self.controller.send(self.params)
        self.assertEqual(result['status'], 'unverified')
        self.channel.error = None
        self.assertEqual(self.controller.send(self.params)['status'], 'unverified')
        self.assertEqual(len(self.channel.sent), 1)

    def test_malformed_acceptance_is_unverified(self):
        self.channel.result = None
        self.assertEqual(self.controller.send(self.params)['status'], 'unverified')
        self.assertEqual(self.controller.send(self.params)['status'], 'unverified')
        self.assertEqual(len(self.channel.sent), 1)

    def test_snapshot_target_mismatch_is_rejected(self):
        self.channel.snapshot['thread_id'] = 'another-thread'
        with self.assertRaises(ControlError):
            self.controller.status({'thread_id': 'thread-one'})
        with self.assertRaises(ControlError):
            self.controller.rpc('thread/resume', {})
        self.assertEqual(self.channel.sent, [])

    def test_disconnected_stdio_uses_desktop_without_starting_a_second_backend(self):
        result = {'connected': True, 'can_send': True, 'transport': 'desktop_ipc'}
        with patch('vibecue.codex_chat.connections', return_value=[]), \
                patch('vibecue.codex_chat.desktop_run', return_value=result) as desktop:
            self.assertIs(run('status', {'thread_id': 'thread-one'}, home=self.home), result)
        desktop.assert_called_once_with('status', {'thread_id': 'thread-one'}, home=self.home)


class ManagedFixture(DesktopFixture):
    def __enter__(self):
        self.closed = False
        return self

    def __exit__(self, *_):
        self.closed = True


class DesktopReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'ipc').mkdir()
        (self.home / 'ipc/ipc.sock').touch()
        directory = patch('vibecue.events.codex_directory', return_value=self.home)
        directory.start(); self.addCleanup(directory.stop)
        delay = patch('vibecue.codex_chat.time.sleep')
        delay.start(); self.addCleanup(delay.stop)

    def test_status_reconnects_with_a_new_channel_after_router_restart(self):
        first, second = ManagedFixture(), ManagedFixture()
        first.runtime_snapshot = lambda: (_ for _ in ()).throw(DesktopConnectionInterrupted('connection closed'))
        with patch('vibecue.codex_desktop_ipc.DesktopChannel', side_effect=[first, second]) as connect:
            result = desktop_run('status', {'thread_id': 'thread-one'}, home=self.home)
        self.assertEqual(connect.call_count, 2)
        self.assertTrue(result['can_send'])
        self.assertFalse(result['auto_reconnect'])
        self.assertTrue(first.closed and second.closed)
        self.assertEqual(first.sent + second.sent, [])
        self.assertEqual(connect.call_args_list[0].kwargs['deadline'], connect.call_args_list[1].kwargs['deadline'])

    def test_unready_router_has_bounded_retries_and_explicit_auto_reconnect_state(self):
        with patch('vibecue.codex_desktop_ipc.DesktopChannel', side_effect=DesktopConnectionInterrupted('closed')) as connect:
            result = desktop_run('status', {'thread_id': 'thread-one'}, home=self.home)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(result['state'], 'reconnecting')
        self.assertTrue(result['auto_reconnect'])
        self.assertEqual(result['retry_after_seconds'], 2)
        self.assertFalse(result['can_send'])

    def test_owned_conversation_restoring_after_restart_is_polled_without_loading(self):
        first, second = ManagedFixture(), ManagedFixture()
        first.snapshot['resume_state'] = 'loading'
        with patch('vibecue.codex_desktop_ipc.DesktopChannel', side_effect=[first, second]) as connect:
            restoring = desktop_run('status', {'thread_id': 'thread-one'}, home=self.home)
            ready = desktop_run('status', {'thread_id': 'thread-one'}, home=self.home)
        self.assertEqual(connect.call_count, 2)
        self.assertTrue(restoring['auto_reconnect'])
        self.assertEqual(restoring['state'], 'reconnecting')
        self.assertFalse(restoring['can_send'])
        self.assertTrue(ready['can_send'])
        self.assertFalse(ready['auto_reconnect'])
        self.assertEqual(first.sent + second.sent, [])

    def test_missing_owner_is_rechecked_without_loading_or_sending(self):
        with patch('vibecue.codex_desktop_ipc.DesktopChannel', side_effect=DesktopOwnerUnavailable('not owned')) as connect:
            result = desktop_run('status', {'thread_id': 'thread-one'}, home=self.home)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(result['state'], 'not_open')
        self.assertTrue(result['auto_reconnect'])
        self.assertFalse(result['needs_load'])

    def test_protocol_or_permission_rejection_is_not_blindly_retried(self):
        with patch('vibecue.codex_desktop_ipc.DesktopChannel', side_effect=ControlError('version not audited')) as connect:
            result = desktop_run('status', {'thread_id': 'thread-one'}, home=self.home)
        connect.assert_called_once()
        self.assertFalse(result['auto_reconnect'])
        self.assertEqual(result['state'], 'unavailable')

    def test_load_and_send_transport_failures_are_never_retried(self):
        for operation in ('load', 'send'):
            with self.subTest(operation=operation), patch('vibecue.codex_desktop_ipc.DesktopChannel', side_effect=DesktopConnectionInterrupted('closed')) as connect:
                result = desktop_run(operation, {'thread_id': 'thread-one'}, home=self.home)
                connect.assert_called_once()
                self.assertFalse(result['auto_reconnect'])

    def test_uncertain_sent_message_remains_locked_after_a_new_connection(self):
        first, second = ManagedFixture(), ManagedFixture()
        first.error = UnconfirmedUpdate('lost response')
        params = {'thread_id': 'thread-one', 'text': 'do not resend', 'request_id': REQUEST}
        with patch('vibecue.codex_desktop_ipc.DesktopChannel', side_effect=[first, second]):
            result = desktop_run('send', params, home=self.home)
            duplicate = desktop_run('send', params, home=self.home)
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(duplicate['status'], 'unverified')
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(len(first.sent), 1)
        self.assertEqual(second.sent, [])


if __name__ == '__main__':
    unittest.main()
