import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vibecue.codex_control import ControlError
from vibecue.desktop_attention import (AttentionProjection, DesktopAttentionMonitor,
                                       DesktopAttentionService, _AttentionChannel, request_metadata)
from vibecue.codex_desktop_ipc import DesktopChannel, DesktopOwnerUnavailable

THREAD = 'thread-one'
OWNER = '22222222-2222-4222-8222-222222222222'
CLIENT = '11111111-1111-4111-8111-111111111111'


def request(identifier=1, method='item/commandExecution/requestApproval'):
    return {'id': identifier, 'method': method,
            'params': {'threadId': THREAD, 'turnId': 'turn-one', 'itemId': 'call-one',
                       'command': 'PRIVATE COMMAND', 'question': 'PRIVATE QUESTION',
                       'api_key': 'PRIVATE KEY', 'challenge': 'PRIVATE CHALLENGE'}}


def change(revision=1, requests=None):
    return {'type': 'snapshot', 'revision': revision,
            'conversationState': {'id': THREAD, 'requests': requests or [],
                                  'turns': [{'items': [{'text': 'PRIVATE BODY'}]}]}}


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.projection = AttentionProjection(THREAD, OWNER)

    def test_snapshot_only_keeps_request_identifiers_not_bodies_or_params(self):
        self.projection.apply(change(requests=[request()]))
        raw = json.dumps(vars(self.projection))
        self.assertNotIn('PRIVATE', raw)
        self.assertEqual(self.projection.requests[0]['params'], {'threadId': THREAD, 'turnId': 'turn-one', 'itemId': 'call-one'})

    def test_requests_list_add_replace_remove_and_body_deltas(self):
        self.projection.apply(change(requests=[request()]))
        self.projection.apply({'type': 'patches', 'baseRevision': 1, 'revision': 2,
                               'patches': [{'op': 'add', 'path': ['requests', 1], 'value': request(2)},
                                           {'op': 'replace', 'path': ['turnHistory', 'entitiesByKey', 'key', 'items'], 'value': [{'text': 'PRIVATE BODY'}]}]})
        self.assertEqual(len(self.projection.requests), 2)
        self.assertNotIn('PRIVATE', json.dumps(vars(self.projection)))
        self.projection.apply({'type': 'patches', 'baseRevision': 2, 'revision': 3,
                               'patches': [{'op': 'remove', 'path': ['requests', 0]}, {'op': 'replace', 'path': ['requests', 0], 'value': request(3)}]})
        self.assertEqual(self.projection.requests[0]['id'], 3)

    def test_partial_request_fields_preserve_safe_projection(self):
        self.projection.apply(change(requests=[request()]))
        self.projection.apply({'type': 'patches', 'baseRevision': 1, 'revision': 2,
                               'patches': [{'op': 'replace', 'path': ['requests', 0, 'completed'], 'value': True},
                                           {'op': 'replace', 'path': ['requests', 0, 'params', 'question'], 'value': 'PRIVATE'},
                                           {'op': 'replace', 'path': ['requests', 0, 'params', 'turnId'], 'value': 'new-turn'}]})
        self.assertTrue(self.projection.requests[0]['completed'])
        self.assertEqual(self.projection.requests[0]['params']['turnId'], 'new-turn')
        self.assertNotIn('PRIVATE', json.dumps(vars(self.projection)))

    def test_missing_revision_or_invalid_request_state_cannot_replay(self):
        self.projection.apply(change())
        for update in [{'type': 'patches', 'baseRevision': 0, 'revision': 2, 'patches': []},
                       {'type': 'patches', 'baseRevision': 1, 'revision': True, 'patches': []},
                       change(0), change(2, [None]), change(2, [request()] * 513)]:
            with self.assertRaises(ControlError):
                self.projection.apply(update)

    def test_cross_thread_snapshot_and_discontinuous_patch_rejected(self):
        wrong = change(); wrong['conversationState']['id'] = 'other-thread'
        with self.assertRaises(ControlError): self.projection.apply(wrong)
        self.projection.apply(change())
        with self.assertRaises(ControlError):
            self.projection.apply({'type': 'patches', 'baseRevision': 1, 'revision': 2,
                                   'patches': [{'op': 'remove', 'path': ['requests', 7]}]})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.events, self.resolutions = [], []
        self.monitor = DesktopAttentionMonitor('/fixture', clock=lambda: self.now,
                                              emit=lambda **value: self.events.append(value),
                                              resolve=lambda *args, **kwargs: self.resolutions.append((args, kwargs)))
        self.projection = AttentionProjection(THREAD, OWNER)

    def update(self, requests, revision=1):
        self.projection.apply(change(revision, requests))
        self.monitor.update(self.projection)

    def test_true_pending_request_alerts_once_and_resolution_dismisses(self):
        self.update([request()]); self.monitor.flush()
        self.assertEqual(self.events, [])
        self.now = 1; self.monitor.flush(); self.monitor.flush()
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['kind'], 'permission')
        self.assertTrue(self.events[0]['requires_user_action'])
        self.update([], 2)
        self.assertEqual(len(self.resolutions), 1)
        self.assertEqual(self.resolutions[0][1]['source'], 'codex client approval stream')

    def test_auto_approved_resolved_before_settle_never_alerts(self):
        self.update([request()]); self.now = .1; self.update([], 2)
        self.now = 1; self.monitor.flush()
        self.assertEqual(self.events, [])
        self.assertEqual(self.monitor.pending, {})

    def test_completed_unknown_or_cross_thread_requests_never_alert(self):
        done = request(); done['completed'] = True
        wrong = request(2); wrong['params']['threadId'] = 'another-thread'
        self.update([done, wrong, {'id': 3, 'method': 'account/login', 'params': {}}, {'id': True, 'method': request()['method']}])
        self.now = 1; self.monitor.flush()
        self.assertEqual(self.events, [])

    def test_request_input_correlates_existing_tool_call_and_mcp_is_input(self):
        self.update([request(1, 'item/tool/requestUserInput'), request(2, 'mcpServer/elicitation/request')])
        self.now = 1; self.monitor.flush()
        self.assertEqual([event['kind'] for event in self.events], ['input', 'input'])
        self.assertIn('call-one', self.events[0]['event_id'])
        self.assertNotIn('PRIVATE', json.dumps(self.events))

    def test_stdio_active_does_not_double_emit_and_gap_dismisses(self):
        self.monitor.emit_enabled = False
        self.update([request()]); self.now = 1; self.monitor.flush()
        self.assertEqual(self.events, [])
        self.monitor.emit_enabled = True; self.monitor.flush()
        self.assertEqual(len(self.events), 1)
        self.monitor.gap()
        self.assertEqual(len(self.resolutions), 1)
        self.assertEqual(self.monitor.pending, {})

    def test_failed_local_spool_cannot_crash_subscription_loop(self):
        def failed(**kwargs): raise RuntimeError('simulated local spool failure')
        self.monitor.emit = failed
        self.update([request()]); self.now = 1
        self.monitor.flush()
        self.assertEqual(self.monitor.pending, {})


class ServiceTests(unittest.TestCase):
    class Channel:
        def __init__(self, on_message, server_list):
            self.socket, self.server = socket.socketpair()
            self.client_id, self.owner = CLIENT, OWNER
            self.on_message = on_message
            self.messages = []
            self.sent = []
            self.thread_id = None
            server_list.append(self)
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def discover(self): return {'owner_client_id': OWNER}
        def close(self):
            if self.socket is not None: self.socket.close()
            self.server.close(); self.socket = None
        def _send(self, value):
            self.sent.append(value)
            if value.get('params', {}).get('following') is True:
                self.messages.append({'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                                      'sourceClientId': OWNER, 'targetClientIds': [CLIENT],
                                      'params': {'hostId': 'local', 'conversationId': THREAD, 'change': change()}})
                self.server.send(b'x')
        def _receive(self, deadline):
            if not self.socket.recv(1): raise ConnectionError()
            message = self.messages.pop(0); self.on_message(message); return message

    def wait_until(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition(): return
            time.sleep(.01)
        self.fail('condition did not become true')

    def create(self, home):
        channels = []
        def factory(*args, on_message, **kwargs): return self.Channel(on_message, channels)
        service = DesktopAttentionService(home, channel_factory=factory)
        service.monitor.emit = lambda **value: self.fail('fixture cannot produce a real alert')
        service.set_threads([THREAD]); service.start(); self.addCleanup(service.stop)
        self.wait_until(lambda: service.snapshot()['capabilities']['attention'])
        return service, channels

    def test_same_private_connection_recovers_automatically_after_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            service, channels = self.create(Path(directory))
            channels[0].server.close()
            self.wait_until(lambda: len(channels) >= 2 and service.snapshot()['capabilities']['attention'])
            self.assertEqual(service.snapshot()['status'], 'connected')
            self.assertEqual(service.snapshot()['pending_count'], 0)
            self.assertTrue(all(row.get('method') == 'thread-stream-following-changed' for channel in channels for row in channel.sent))

    def test_manual_retry_file_created_after_start_resets_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); service, channels = self.create(home)
            self.wait_until(lambda: service._reconnect_seen)
            (home / 'desktop-reconnect.request').write_text('retry')
            self.wait_until(lambda: len(channels) >= 2 and service.snapshot()['capabilities']['attention'])

    def test_wrong_owner_or_target_snapshot_is_ignored(self):
        service = DesktopAttentionService('/fixture')
        service.channel = type('Channel', (), {'client_id': CLIENT})()
        service.projections[THREAD] = AttentionProjection(THREAD, OWNER)
        for source, targets in [('other-owner', [CLIENT]), (OWNER, ['other-client']), (OWNER, CLIENT)]:
            service._on_message({'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                                 'sourceClientId': source, 'targetClientIds': targets,
                                 'params': {'hostId': 'local', 'conversationId': THREAD, 'change': change(requests=[request()])}})
        self.assertEqual(service.monitor.pending, {})
        self.assertIsNone(service.projections[THREAD].revision)

    def test_initial_snapshot_is_pending_for_only_one_candidate(self):
        service = DesktopAttentionService('/fixture')
        service.channel = type('Channel', (), {'discover': lambda self: self.fail('unexpected discovery')})()
        service.projections[THREAD] = AttentionProjection(THREAD, OWNER)
        service.subscribed_at[THREAD] = time.monotonic()
        service._discover([THREAD, 'another-thread'])
        self.assertEqual(set(service.projections), {THREAD})

    def test_oversized_initial_frame_quarantines_only_pending_candidate(self):
        service = DesktopAttentionService('/fixture')
        healthy = AttentionProjection(THREAD, OWNER); healthy.apply(change())
        service.projections[THREAD] = healthy
        service.projections['oversized'] = AttentionProjection('oversized', OWNER)
        service._on_message({'type': 'vibecue-observation-gap', 'reason': 'oversized-frame'})
        self.assertGreater(service.quarantine_until['oversized'], time.monotonic() + 290)
        self.assertNotIn(THREAD, service.quarantine_until)

    def test_failed_readonly_candidate_is_quarantined_before_socket_reset(self):
        service = DesktopAttentionService('/fixture')
        class Channel:
            def discover(self): raise ControlError('Codex 本机对话连接已中断')
        service.channel = Channel()
        with self.assertRaises(ControlError): service._discover([THREAD])
        self.assertGreater(service.quarantine_until[THREAD], time.monotonic() + 290)
        # A new connection skips that candidate rather than repeating the loop.
        service._discover([THREAD])

    def test_discovery_router_deadline_has_slack_and_router_timeout_is_local(self):
        sent = []
        channel = _AttentionChannel(THREAD, timeout=3, on_message=lambda message: None)
        channel.socket = type('Socket', (), {'sendall': lambda self, raw: sent.append(json.loads(raw[4:]))})()
        channel._send({'type': 'request', 'method': 'thread-owner-discovery', 'requestId': 'discovery-one', 'timeoutMs': 3000})
        self.assertLess(sent[0]['timeoutMs'], 3000)
        with patch.object(DesktopChannel, '_receive', return_value={'type': 'response', 'requestId': 'discovery-one', 'resultType': 'error', 'error': 'request-timeout'}):
            self.assertEqual(channel._receive(0)['error'], 'no-client-found')

    def test_readonly_timeout_only_skips_candidate_at_empty_header_boundary(self):
        channel = _AttentionChannel(THREAD, timeout=1, on_message=lambda message: None)
        class Socket:
            def settimeout(self, value): pass
            def recv(self, count): raise TimeoutError()
        channel.socket = Socket(); channel._reading_header = True
        with self.assertRaises(TimeoutError): channel._read_exact(4, time.monotonic() + 1)
        self.assertTrue(channel.clean_read_timeout)
        channel.clean_read_timeout = False; channel._reading_header = False
        with self.assertRaises(TimeoutError): channel._read_exact(4, time.monotonic() + 1)
        self.assertFalse(channel.clean_read_timeout)
        channel._reading_header = True
        with patch.object(Socket, 'recv', side_effect=[b'ab', TimeoutError()]):
            with self.assertRaises(TimeoutError): channel._read_exact(4, time.monotonic() + 1)
        self.assertFalse(channel.clean_read_timeout)
        def timeout(_):
            channel.clean_read_timeout = True
            raise ControlError('timeout')
        with patch.object(DesktopChannel, 'discover', timeout):
            with self.assertRaises(DesktopOwnerUnavailable): channel.discover()


class WatcherIntegrationTests(unittest.TestCase):
    def test_old_recovery_navigation_does_not_force_live_full_snapshot(self):
        from vibecue.providers import codex
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); path = home / 'log.jsonl'; path.write_text('')
            with patch.object(codex, 'DesktopAttentionService') as service:
                watcher = codex.Watcher(home / 'codex', home / 'cue')
                old = codex.ThreadMetadata('old-recovery', path, 'old', 'vscode', time.time() - 3 * 86400)
                recent = codex.ThreadMetadata('current-waiting', path, 'recent', 'vscode', time.time())
                first, second = codex.Interpreter(old, lambda **kwargs: None), codex.Interpreter(recent, lambda **kwargs: None)
                first.activity_state = 'running'; first.last_activity = old.updated_at
                second.activity_state = 'waiting'; second.last_activity = recent.updated_at
                watcher.watched = {old.thread_id: (codex.TailReader(path), first), recent.thread_id: (codex.TailReader(path), second)}
                with patch.object(watcher, 'metadata', return_value=[old, recent]): watcher.discover()
                service.return_value.set_threads.assert_called_with(['current-waiting'])
                self.assertIn('old-recovery', watcher.watched)

    def test_status_reuses_desktop_attention_without_starting_from_probe(self):
        from vibecue.providers import codex
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            desktop = {'connected': True, 'status': 'connected', 'last_seen': time.time(),
                       'pending_count': 1, 'pending_thread_ids': [THREAD],
                       'capabilities': {'reply': True, 'control': True, 'attention': True}}
            stdio = {'connected': False, 'pending_count': 0, 'pending_thread_ids': [], 'last_seen': 0}
            with patch.object(codex, 'DesktopAttentionService') as service, patch.object(codex, 'approval_status', return_value=stdio):
                service.return_value.snapshot.return_value = desktop
                watcher = codex.Watcher(home / 'codex', home / 'cue')
                watcher.write_status()
                state = json.loads((home / 'cue/watcher-state.json').read_text())
                self.assertTrue(state['manual_approval']['connected'])
                self.assertEqual(state['manual_approval']['pending_thread_ids'], [THREAD])
                self.assertEqual(state['desktop_connection'], desktop)
                self.assertEqual(state['permission_status'], 'connected')
                service.return_value.start.assert_not_called()
                service.return_value.set_stdio_connected.assert_called_with(False)

    def test_existing_stdio_is_preferred_to_avoid_count_and_alert_duplication(self):
        from vibecue.providers import codex
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            desktop = {'connected': True, 'status': 'connected', 'last_seen': time.time(),
                       'pending_count': 1, 'pending_thread_ids': [THREAD],
                       'capabilities': {'reply': True, 'control': True, 'attention': True}}
            stdio = {'connected': True, 'pending_count': 1, 'pending_thread_ids': [THREAD], 'last_seen': time.time(), 'source': 'stdio'}
            with patch.object(codex, 'DesktopAttentionService') as service, patch.object(codex, 'approval_status', return_value=stdio):
                service.return_value.snapshot.return_value = desktop
                watcher = codex.Watcher(home / 'codex', home / 'cue')
                watcher.write_status()
                state = json.loads((home / 'cue/watcher-state.json').read_text())
                self.assertEqual(state['manual_approval'], stdio)
                service.return_value.set_stdio_connected.assert_called_with(True)


if __name__ == '__main__': unittest.main()
