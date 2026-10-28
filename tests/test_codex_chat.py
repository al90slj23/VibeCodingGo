import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vibecue.codex_chat import ChatController, MAX_TEXT_BYTES, run, send_params, socket_call
from vibecue.codex_control import ControlError, UnconfirmedUpdate
from vibecue.codex_control_relay import ControlHub, framed_pipe_write

REQUEST = 'b5cc11b4-7314-4fc6-9fc7-f7e90eb984f6'


class FakeChannel:
    def __init__(self, state='idle', loaded=True, flags=None):
        self.state, self.is_loaded, self.flags = state, loaded, flags or []
        self.calls = []
        self.fail_mutation = None

    def request(self, method, params, timeout=8):
        self.calls.append((method, dict(params)))
        if method == 'thread/loaded/list': return {'data': ['thread-one'] if self.is_loaded else []}
        if method == 'thread/read':
            return {'thread': {'id': 'thread-one', 'preview': 'PRIVATE HISTORY',
                               'status': {'type': self.state, 'activeFlags': self.flags}}}
        if method == 'thread/resume':
            self.is_loaded = True
            return {'thread': {'id': 'thread-one', 'preview': 'PRIVATE HISTORY'}}
        if method in ('turn/start', 'turn/steer'):
            if self.fail_mutation: raise self.fail_mutation
            return {'turn': {'id': 'turn-new'}} if method == 'turn/start' else {'turnId': 'turn-active'}
        raise AssertionError(method)

    def chat_activity(self, identifier): return {'state': 'active', 'turn_id': 'turn-active'}


class ChatControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def send(self, channel, **extra):
        return ChatController(channel, self.home).send({'thread_id': 'thread-one', 'text': '继续核对最后一个问题', 'request_id': REQUEST, **extra})

    def test_status_is_metadata_only_and_never_loads_or_generates(self):
        channel = FakeChannel()
        result = ChatController(channel, self.home).status({'thread_id': 'thread-one'})
        self.assertTrue(result['can_send']); self.assertEqual(result['state'], 'idle')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual([m for m, p in channel.calls], ['thread/loaded/list', 'thread/read'])
        self.assertEqual(channel.calls[-1][1], {'threadId': 'thread-one', 'includeTurns': False})

    def test_unloaded_window_never_implicitly_resumes(self):
        channel = FakeChannel(loaded=False)
        result = ChatController(channel, self.home).status({'thread_id': 'thread-one'})
        self.assertFalse(result['can_send']); self.assertTrue(result['needs_load'])
        self.assertEqual([m for m, p in channel.calls], ['thread/loaded/list'])
        result = self.send(channel)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(any(m in ('thread/resume', 'turn/start', 'turn/steer') for m, p in channel.calls))

    def test_explicit_load_only_resumes_selected_thread_with_no_overrides_or_body(self):
        channel = FakeChannel(loaded=False)
        result = ChatController(channel, self.home).load({'thread_id': 'thread-one'})
        self.assertTrue(result['can_send'])
        loads = [p for m, p in channel.calls if m == 'thread/resume']
        self.assertEqual(loads, [{'threadId': 'thread-one', 'excludeTurns': True}])
        self.assertFalse(any(m.startswith('turn/') for m, p in channel.calls))
        with self.assertRaises(ControlError): ChatController(channel, self.home).load({'thread_id': 'thread-one', 'approvalPolicy': 'never'})

    def test_idle_send_preserves_settings_and_returns_only_verified_turn_metadata(self):
        channel = FakeChannel()
        result = self.send(channel)
        self.assertEqual(result['status'], 'accepted'); self.assertEqual(result['turn_id'], 'turn-new')
        sent = [p for m, p in channel.calls if m == 'turn/start']
        self.assertEqual(sent, [{'threadId': 'thread-one', 'input': [{'type': 'text', 'text': '继续核对最后一个问题'}], 'clientUserMessageId': REQUEST}])
        self.assertNotIn('继续核对', (self.home/'control/chat-receipts.json').read_text())
        self.assertEqual((self.home/'control/chat-receipts.json').stat().st_mode & 0o777, 0o600)

    def test_active_send_steers_exact_turn_and_never_falls_back_to_start(self):
        channel = FakeChannel('active')
        result = self.send(channel, expected_turn_id='turn-active')
        self.assertEqual(result['status'], 'accepted')
        sent = [(m, p) for m, p in channel.calls if m.startswith('turn/')]
        self.assertEqual(len(sent), 1); self.assertEqual(sent[0][0], 'turn/steer')
        self.assertEqual(sent[0][1]['expectedTurnId'], 'turn-active')

    def test_active_race_or_finished_expected_turn_rejects_without_generation(self):
        for state, extra in [('active', {}), ('active', {'expected_turn_id': 'old-turn'}), ('idle', {'expected_turn_id': 'turn-active'})]:
            channel = FakeChannel(state)
            self.assertEqual(self.send(channel, **extra)['status'], 'failed')
            self.assertFalse(any(m.startswith('turn/') for m, p in channel.calls))

    def test_waiting_approval_unknown_and_unknown_active_turn_never_send(self):
        for channel in [FakeChannel('active', flags=['waitingOnApproval']), FakeChannel('systemError')]:
            self.assertFalse(ChatController(channel, self.home).status({'thread_id': 'thread-one'})['can_send'])
            self.assertEqual(self.send(channel)['status'], 'failed')
        channel = FakeChannel('active'); channel.chat_activity = lambda _: {}
        self.assertFalse(ChatController(channel, self.home).status({'thread_id': 'thread-one'})['can_send'])

    def test_accepted_and_uncertain_receipts_are_idempotent_across_worker_restart(self):
        channel = FakeChannel(); channel.fail_mutation = UnconfirmedUpdate('sent but no acknowledgement')
        self.assertEqual(self.send(channel)['status'], 'unverified')
        channel.fail_mutation = None
        result = self.send(channel)
        self.assertEqual(result['status'], 'unverified'); self.assertTrue(result['duplicate'])
        self.assertEqual(sum(m == 'turn/start' for m, p in channel.calls), 1)
        with self.assertRaises(ControlError):
            ChatController(channel, self.home).send({'thread_id': 'thread-one', 'text': 'different input', 'request_id': REQUEST})

    def test_definite_rejection_does_not_retry_and_response_does_not_echo_input(self):
        channel = FakeChannel(); channel.fail_mutation = ControlError('PRIVATE INPUT ECHO')
        result = self.send(channel)
        self.assertEqual(result['status'], 'failed'); self.assertNotIn('PRIVATE', json.dumps(result))
        self.send(channel)
        self.assertEqual(sum(m == 'turn/start' for m, p in channel.calls), 1)

    def test_input_validation_rejects_overrides_invalid_ids_and_overlong_text(self):
        base = {'thread_id': 'thread-one', 'text': 'hello', 'request_id': REQUEST}
        for extra in [{'thread_id': '../path'}, {'approvalPolicy': 'never'}, {'text': ''}, {'text': '中' * (MAX_TEXT_BYTES//3 + 1)}, {'request_id': 'bad'}]:
            with self.assertRaises(ControlError): send_params({**base, **extra})

    def test_corrupt_receipts_do_not_get_replaced_or_allow_a_second_send(self):
        folder = self.home/'control'; folder.mkdir(mode=0o700)
        target = folder/'chat-receipts.json'; target.write_text('corrupt private data')
        channel = FakeChannel()
        with self.assertRaises(ControlError): self.send(channel)
        self.assertEqual(target.read_text(), 'corrupt private data'); self.assertEqual(channel.calls, [])


class ChatRoutingTests(unittest.TestCase):
    def test_legacy_bridge_is_detected_without_probing_unsupported_rpcs(self):
        with patch('vibecue.codex_chat.connections', return_value=[{'socket_path': '/fixture'}]), patch('vibecue.codex_chat.socket_call') as call:
            result = run('status', {'thread_id': 'thread-one'})
        self.assertEqual(result['state'], 'legacy_bridge'); self.assertFalse(result['can_send']); call.assert_not_called()

    def test_multiple_unloaded_owners_do_not_choose_a_resume_target(self):
        with patch('vibecue.codex_chat.connections', return_value=[{'chat_version': 1}, {'chat_version': 1}]), \
             patch('vibecue.codex_chat.socket_call', return_value={'supported': True, 'state': 'not_loaded', 'can_send': False}) as call:
            result = run('load', {'thread_id': 'thread-one'})
        self.assertEqual(result['state'], 'ambiguous_owner'); self.assertEqual(call.call_count, 2)
        self.assertTrue(all(c.args[1] == 'status' for c in call.call_args_list))

    def test_mixed_legacy_and_capable_owners_never_resume_into_a_second_owner(self):
        with patch('vibecue.codex_chat.connections', return_value=[{'chat_version': 1}, {}]), patch('vibecue.codex_chat.socket_call') as call:
            result = run('load', {'thread_id': 'thread-one'})
        self.assertEqual(result['state'], 'ambiguous_owner'); call.assert_not_called()

    def test_disconnect_after_socket_submission_is_uncertain_not_retryable(self):
        class Client:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, v): pass
            def connect(self, p): pass
            def sendall(self, b): pass
            def makefile(self, mode): raise ConnectionResetError()
        with patch('vibecue.codex_chat.socket.socket', return_value=Client()):
            result = socket_call({'socket_path': '/fixture'}, 'send', {'request_id': REQUEST})
        self.assertEqual(result['status'], 'unverified')


class ChatRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.sent = []; self.hub = ControlHub(self.sent.append, os.getpid(), home=Path(self.temp.name)/'home')
        self.addCleanup(self.hub.close)
        self.hub.client_forwarded(b'{"id":"init","method":"initialize","params":{}}\n')
        self.hub.server_chunk(b'{"id":"init","result":{}}\n')
        self.hub.client_forwarded(b'{"method":"initialized"}\n')

    def params(self): return {'threadId': 'thread-one', 'input': [{'type': 'text', 'text': 'hello'}], 'clientUserMessageId': REQUEST}

    def test_chat_activity_notifications_preserve_original_bytes_and_drop_bodies(self):
        raw = json.dumps({'method': 'turn/started', 'params': {'threadId': 'thread-one', 'turn': {'id': 'turn-active', 'items': ['PRIVATE BODY']}}}).encode()+b'\n'
        self.assertEqual(self.hub.server_chunk(raw), raw)
        self.assertEqual(self.hub.chat_activity('thread-one'), {'state': 'active', 'turn_id': 'turn-active'})
        self.assertNotIn('PRIVATE', json.dumps(self.hub.thread_activity))

    def test_chat_rpc_allowlist_prohibits_auth_permissions_tools_and_load_overrides(self):
        bad = [('thread/resume', {'threadId': 'thread-one', 'model': 'override'}),
               ('turn/start', {**self.params(), 'approvalPolicy': 'never'}),
               ('turn/start', {**self.params(), 'input': [{'type': 'text', 'text': 'ok'}, {'type': 'localImage', 'path': '/private'}]}),
               ('turn/steer', self.params()), ('account/read', {})]
        for method, params in bad:
            with self.assertRaises(ControlError): self.hub.request(method, params, timeout=.01)
        self.assertEqual(self.sent, [])

    def test_partial_chat_finishes_after_timeout_before_any_desktop_bytes(self):
        began, release = threading.Event(), threading.Event()
        def write(payload, deadline, commit):
            self.assertTrue(commit()); self.sent.append(payload[:30]); began.set()
            self.assertTrue(release.wait(2)); self.sent.append(payload[30:])
        self.hub.write_chat = write
        outcome = []
        def request():
            try: self.hub.request('turn/start', self.params(), timeout=.08)
            except UnconfirmedUpdate: outcome.append('uncertain')
        caller = threading.Thread(target=request); caller.start(); self.assertTrue(began.wait(1))
        caller.join(1); self.assertEqual(outcome, ['uncertain'])
        desktop = b'{"id":"desktop-input","method":"example"}\n'
        forwarder = threading.Thread(target=lambda: self.hub.forward_client(desktop)); forwarder.start()
        time.sleep(.02); self.assertEqual(len(self.sent), 1)
        release.set(); forwarder.join(1)
        combined = b''.join(self.sent); lines = combined.splitlines()
        self.assertEqual(len(lines), 2); control = json.loads(lines[0]); self.assertEqual(control['method'], 'turn/start')
        self.assertEqual(lines[1]+b'\n', desktop)
        late = json.dumps({'id': control['id'], 'result': {'turn': {'id': 'turn-new'}}}).encode()+b'\n'
        self.assertEqual(self.hub.server_chunk(late), b'')

    def test_chat_waiting_for_a_partial_desktop_line_expires_without_late_send(self):
        self.hub.forward_client(b'{"id":1,"method":"example","params":{"text":"')
        with self.assertRaises(ControlError): self.hub.request('turn/start', self.params(), timeout=.02)
        self.hub.forward_client(b'original"}}\n'); time.sleep(.04)
        lines = b''.join(self.sent).splitlines()
        self.assertEqual(len(lines), 1); self.assertEqual(json.loads(lines[0])['id'], 1)

    def test_observed_desktop_turn_start_before_commit_prevents_second_start(self):
        self.hub.thread_activity['thread-one'] = {'state': 'active', 'turn_id': 'desktop-turn'}
        with self.assertRaises(ControlError): self.hub.request('turn/start', self.params(), timeout=.02)
        self.assertEqual(self.sent, [])

    def test_framed_writer_keeps_committed_bytes_after_deadline_and_restores_flags(self):
        # Deterministic simulated short writes exercise EAGAIN/backpressure.
        writes, calls = [], [0]
        def write(fd, value):
            calls[0] += 1
            if calls[0] == 2: raise BlockingIOError(errno.EAGAIN, 'busy')
            part = bytes(value[:3]); writes.append(part); return len(part)
        with patch('vibecue.codex_control_relay.fcntl.fcntl', return_value=0) as flags, \
             patch('vibecue.codex_control_relay.select.select', return_value=([], [10], [])), \
             patch('vibecue.codex_control_relay.os.write', side_effect=write), \
             patch('vibecue.codex_control_relay.time.monotonic', side_effect=[0, 2, 3, 4]):
            framed_pipe_write(10, b'123456789', 1, lambda: True)
        self.assertEqual(b''.join(writes), b'123456789')
        self.assertEqual(flags.call_args.args[-1], 0)

    def test_near_limit_chinese_input_is_utf8_not_expanded_json_escapes(self):
        frames = []
        def write(payload, deadline, commit):
            self.assertTrue(commit()); frames.append(payload)
            request = json.loads(payload)
            self.hub.server_chunk(json.dumps({'id': request['id'], 'result': {'turn': {'id': 'turn-new'}}}).encode()+b'\n')
        self.hub.write_chat = write
        params = self.params(); params['input'][0]['text'] = '中' * (MAX_TEXT_BYTES//3)
        self.assertEqual(self.hub.request('turn/start', params)['turn']['id'], 'turn-new')
        self.assertLess(len(frames[0]), 16*1024)
        self.assertIn('中'.encode(), frames[0]); self.assertNotIn(b'\\u4e2d', frames[0])
        self.assertEqual(json.loads(frames[0])['params']['input'], params['input'])

    def test_private_chat_socket_roundtrip_uses_only_simulated_backend(self):
        loaded = [False]; methods = []
        def backend(payload):
            request = json.loads(payload); method = request['method']; methods.append(method)
            if method == 'thread/loaded/list': result = {'data': ['thread-one'] if loaded[0] else []}
            elif method == 'thread/read': result = {'thread': {'id': 'thread-one', 'status': {'type': 'idle'}}}
            elif method == 'thread/resume':
                loaded[0] = True; result = {'thread': {'id': 'thread-one', 'status': {'type': 'idle'}}}
            elif method == 'turn/start': result = {'turn': {'id': 'turn-new'}}
            else: raise AssertionError(method)
            self.hub.server_chunk(json.dumps({'id': request['id'], 'result': result}).encode()+b'\n')
        self.hub.write_backend = backend
        self.hub.write_control = lambda payload, deadline: backend(payload)
        self.hub.write_chat = lambda payload, deadline, commit: backend(payload) if commit() else None
        try:
            self.hub.start(); self.hub._health()
        except PermissionError:
            self.skipTest('sandbox does not allow local Unix socket bind')
        except OSError as error:
            if error.errno == errno.EPERM: self.skipTest('sandbox does not allow local Unix socket bind')
            raise
        result = run('status', {'thread_id': 'thread-one'}, home=self.hub.home)
        self.assertTrue(result['needs_load']); self.assertNotIn('thread/resume', methods)
        result = run('load', {'thread_id': 'thread-one'}, home=self.hub.home)
        self.assertTrue(result['can_send']); self.assertEqual(methods.count('thread/resume'), 1)
        params = {'thread_id': 'thread-one', 'text': 'fixture-only text', 'request_id': REQUEST}
        result = run('send', params, home=self.hub.home)
        self.assertEqual(result['status'], 'accepted'); self.assertEqual(result['thread_id'], 'thread-one')
        self.assertEqual(result['request_id'], REQUEST)
        duplicate = run('send', params, home=self.hub.home)
        self.assertTrue(duplicate['duplicate']); self.assertEqual(methods.count('turn/start'), 1)


if __name__ == '__main__':
    unittest.main()
