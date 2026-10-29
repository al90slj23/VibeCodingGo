import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vibecue.codex_control import ControlError
from vibecue.desktop_attention import (AttentionProjection, DesktopAttentionMonitor,
                                       DesktopAttentionService, AttentionRevisionGap,
                                       _AttentionChannel, request_metadata)
from vibecue.codex_desktop_ipc import (DesktopChannel, DesktopOwnerUnavailable,
                                      DesktopConnectionInterrupted, DesktopProtocolUnsupported, KNOWN_PROTOCOL)



class VerifiedContractFixture:
    protocol_contract = dict(KNOWN_PROTOCOL)
    supports = DesktopChannel.supports
    require_method = DesktopChannel.require_method
    require_capability = DesktopChannel.require_capability
    capabilities = DesktopChannel.capabilities

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
            'conversationState': {'id': THREAD, 'resumeState': 'resumed', 'requests': requests or [],
                                  'turns': [{'items': [{'text': 'PRIVATE BODY'}]}]}}


def projected_json(projection):
    values = dict(vars(projection))
    values['settings'] = projection.settings.snapshot()
    values['runtime'] = projection.runtime.data
    return json.dumps(values)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.projection = AttentionProjection(THREAD, OWNER)

    def test_snapshot_only_keeps_request_identifiers_not_bodies_or_params(self):
        self.projection.apply(change(requests=[request()]))
        raw = projected_json(self.projection)
        self.assertNotIn('PRIVATE', raw)
        self.assertEqual(self.projection.requests[0]['params'], {'threadId': THREAD, 'turnId': 'turn-one', 'itemId': 'call-one'})

    def test_requests_list_add_replace_remove_and_body_deltas(self):
        self.projection.apply(change(requests=[request()]))
        self.projection.apply({'type': 'patches', 'baseRevision': 1, 'revision': 2,
                               'patches': [{'op': 'add', 'path': ['requests', 1], 'value': request(2)},
                                           {'op': 'replace', 'path': ['turnHistory', 'entitiesByKey', 'key', 'items'], 'value': [{'text': 'PRIVATE BODY'}]}]})
        self.assertEqual(len(self.projection.requests), 2)
        self.assertNotIn('PRIVATE', projected_json(self.projection))
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
        self.assertNotIn('PRIVATE', projected_json(self.projection))

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

    def test_revision_gap_and_rollback_are_typed_as_transient_projection_loss(self):
        self.projection.apply(change(100))
        for update in [change(0), {'type': 'patches', 'baseRevision': 0, 'revision': 1, 'patches': []}]:
            with self.assertRaises(AttentionRevisionGap):
                self.projection.apply(update)

    def test_repeated_initial_snapshot_does_not_create_a_revision_gap(self):
        self.projection.apply(change(0, [request()]))
        self.projection.apply(change(0, [request()]))
        self.assertEqual(self.projection.revision, 0)
        self.assertEqual(len(self.projection.requests), 1)


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


class ContractHealthTests(unittest.TestCase):
    def test_reply_contract_change_keeps_compatible_control_and_attention_discovery(self):
        service = DesktopAttentionService('/fixture')
        channel = VerifiedContractFixture()
        channel.protocol_contract = dict(KNOWN_PROTOCOL)
        channel.protocol_contract['thread-follower-start-turn'] = 3
        self.assertTrue(service._contract_health(channel))
        health = service.snapshot()
        self.assertEqual(health['capabilities'], {'reply':False, 'control':True, 'attention':False})
        self.assertTrue(health['connected'])
        self.assertEqual(health['subscribed_threads'], 0)

    def test_unknown_stream_never_claims_attention_and_keeps_compatible_control(self):
        service = DesktopAttentionService('/fixture')
        channel = VerifiedContractFixture()
        channel.protocol_contract = dict(KNOWN_PROTOCOL)
        channel.protocol_contract['thread-stream-state-changed'] = 12
        self.assertFalse(service._contract_health(channel))
        health = service.snapshot()
        self.assertFalse(health['capabilities']['reply'])
        self.assertFalse(health['capabilities']['attention'])
        self.assertTrue(health['capabilities']['control'])
        self.assertEqual(health['reason_code'], 'protocol_unsupported')
        self.assertEqual(health['pending_count'], 0)


class ServiceTests(unittest.TestCase):
    class Channel(VerifiedContractFixture):
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
        def queue(self, message):
            self.messages.append(message)
            self.server.send(b'x')

    class EpochChannel(Channel):
        def __init__(self, on_message, server_list):
            super().__init__(on_message, server_list)
            self.owners = {THREAD: OWNER, 'healthy-thread': '33333333-3333-4333-8333-333333333333'}
            self.revisions = {THREAD: 100, 'healthy-thread': 8}
        def discover(self):
            self.owner = self.owners[self.thread_id]
            return {'owner_client_id': self.owner}
        def snapshot(self, thread, *, requests=None):
            update = change(self.revisions[thread], requests)
            update['conversationState'].update(id=thread,
                latestThreadSettings={'model': 'model-' + str(self.revisions[thread]), 'effort': 'high'},
                threadRuntimeStatus={'type': 'active', 'activeFlags': []},
                turns=[{'turnId': 'turn-one' if self.revisions[thread] else 'new-epoch', 'status': 'inProgress'}])
            return {'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                    'sourceClientId': self.owners[thread], 'targetClientIds': [CLIENT],
                    'params': {'hostId': 'local', 'conversationId': thread, 'change': update}}
        def _send(self, value):
            self.sent.append(value)
            if value.get('params', {}).get('following') is True:
                self.queue(self.snapshot(value['params']['conversationId']))

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

    def create_epochs(self, home):
        channels = []
        def factory(*args, on_message, **kwargs): return self.EpochChannel(on_message, channels)
        service = DesktopAttentionService(home, channel_factory=factory)
        events, resolutions = [], []
        service.monitor.emit = lambda **value: events.append(value)
        service.monitor.resolve = lambda *args, **kwargs: resolutions.append((args, kwargs))
        service.set_threads([THREAD, 'healthy-thread']); service.start(); self.addCleanup(service.stop)
        self.wait_until(lambda: service.snapshot()['subscribed_threads'] == 2)
        return service, channels, events, resolutions

    def test_same_owner_full_snapshot_replaces_only_its_epoch_and_settles_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            service, channels, events, resolutions = self.create_epochs(Path(directory))
            channel = channels[0]
            healthy = service.projections['healthy-thread']
            channel.revisions[THREAD] = 101
            channel.queue(channel.snapshot(THREAD, requests=[request()]))
            self.wait_until(lambda: len(events) == 1)
            old = service.projections[THREAD]
            channel.revisions[THREAD] = 0
            channel.queue(channel.snapshot(THREAD))
            self.wait_until(lambda: service.projections[THREAD].revision == 0)
            self.assertEqual(service.snapshot()['status'], 'connected')
            self.assertEqual(service.snapshot()['reason_code'], 'connected')
            self.assertIsNot(service.projections[THREAD], old)
            self.assertEqual(service.monitor.pending, {})
            self.assertEqual(len(resolutions), 1)
            self.assertIs(service.projections['healthy-thread'], healthy)
            self.assertEqual(service.current_settings()['healthy-thread']['model'], 'model-8')
            self.assertEqual(len(channels), 1)
            self.assertEqual(service.current_settings()[THREAD]['model'], 'model-0')
            self.assertEqual(service.current_runtime()[THREAD]['turn_id'], 'new-epoch')
            self.assertEqual(len(events), 1)
            self.assertEqual(service.snapshot()['reason_code'], 'connected')
            self.assertNotIn(THREAD, service.quarantine_until)
            follows = [(row['params']['conversationId'], row['params']['following']) for row in channel.sent]
            self.assertEqual(follows.count((THREAD, False)), 0)
            self.assertEqual(follows.count((THREAD, True)), 1)
            self.assertEqual(follows.count(('healthy-thread', True)), 1)
            self.assertTrue(all(row['method'] == 'thread-stream-following-changed' for row in channel.sent))

    def test_cached_high_snapshot_then_low_live_snapshot_and_delta_keep_one_subscription(self):
        channels = []
        class CachedInitialChannel(self.EpochChannel):
            def _send(self, value):
                thread = value.get('params', {}).get('conversationId')
                live_follow = thread == THREAD and value.get('params', {}).get('following') is True
                if live_follow:
                    self.revisions[THREAD] = 7805
                super()._send(value)
                if live_follow:
                    self.revisions[THREAD] = 322
                    live = self.snapshot(THREAD)
                    live['params']['change']['conversationState'].update(
                        latestThreadSettings={'model': 'live-model'},
                        turns=[{'turnId': 'live-turn', 'status': 'inProgress'}])
                    self.queue(live)
                    delta = self.snapshot(THREAD)
                    delta['params']['change'] = {'type': 'patches', 'baseRevision': 322, 'revision': 323,
                        'patches': [{'op': 'replace', 'path': ['latestThreadSettings', 'effort'], 'value': 'ultra'}]}
                    self.queue(delta)
        def factory(*args, on_message, **kwargs): return CachedInitialChannel(on_message, channels)
        with tempfile.TemporaryDirectory() as directory:
            service = DesktopAttentionService(Path(directory), channel_factory=factory)
            service.monitor.emit = lambda **value: self.fail('fixture cannot produce a real alert')
            service.set_threads([THREAD, 'healthy-thread']); service.start()
            try:
                self.wait_until(lambda: service.snapshot()['subscribed_threads'] == 2 and
                                service.projections[THREAD].revision == 323)
                self.assertEqual(len(channels), 1)
                settings = service.current_settings()[THREAD]
                self.assertEqual((settings['model'], settings['reasoning_effort']), ('live-model', 'ultra'))
                self.assertEqual(service.current_runtime()[THREAD]['turn_id'], 'live-turn')
                self.assertEqual(service.current_settings()['healthy-thread']['model'], 'model-8')
                self.assertEqual(service.snapshot()['reason_code'], 'connected')
                self.assertEqual(service.resync_attempts, {})
                follows = [(row['params']['conversationId'], row['params']['following']) for row in channels[0].sent]
                self.assertEqual(follows.count((THREAD, True)), 1)
                self.assertEqual(follows.count((THREAD, False)), 0)
                self.assertEqual(follows.count(('healthy-thread', True)), 1)
            finally:
                service.stop()

    def test_patch_gap_discards_unsettled_pending_and_restarts_full_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            service, channels, events, _ = self.create_epochs(Path(directory))
            channel = channels[0]
            service.monitor.clock = lambda: 0
            channel.queue(channel.snapshot(THREAD, requests=[request()]))
            self.wait_until(lambda: bool(service.monitor.pending))
            packet = channel.snapshot(THREAD)
            packet['params']['change'] = {'type': 'patches', 'baseRevision': 2, 'revision': 3, 'patches': []}
            channel.revisions[THREAD] = 0
            channel.queue(packet)
            self.wait_until(lambda: THREAD not in service.projections)
            service.monitor.clock = lambda: 100
            service.monitor.flush()
            self.assertEqual(events, [])
            self.assertEqual(service.monitor.pending, {})
            self.wait_until(lambda: THREAD in service.projections and service.projections[THREAD].revision == 0)
            self.assertEqual(service.snapshot()['subscribed_threads'], 2)
            self.assertEqual(len(channels), 1)

    def test_owner_replacement_rediscovers_only_its_chats_and_ignores_old_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            service, channels, _, _ = self.create_epochs(Path(directory))
            channel = channels[0]
            healthy = service.projections['healthy-thread']
            replacement = '44444444-4444-4444-8444-444444444444'
            channel.owners[THREAD] = replacement
            channel.revisions[THREAD] = 0
            channel.queue({'type': 'broadcast', 'method': 'client-status-changed',
                           'params': {'clientId': OWNER, 'status': 'disconnected'}})
            self.wait_until(lambda: THREAD not in service.projections)
            self.wait_until(lambda: THREAD in service.projections and service.projections[THREAD].revision == 0)
            wrong = channel.snapshot(THREAD)
            wrong['sourceClientId'] = OWNER
            wrong['params']['change']['revision'] = 999
            channel.queue(wrong)
            time.sleep(.15)
            self.assertEqual(service.projections[THREAD].revision, 0)
            self.assertEqual(service.projections[THREAD].owner, replacement)
            self.assertIs(service.projections['healthy-thread'], healthy)
            self.assertEqual(len(channels), 1)

    def test_duplicate_initial_snapshot_then_ordered_delta_keeps_subscription_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            service, channels, events, _ = self.create_epochs(Path(directory))
            channel = channels[0]
            original = service.projections[THREAD]
            channel.queue(channel.snapshot(THREAD))
            patch_message = channel.snapshot(THREAD)
            patch_message['params']['change'] = {'type': 'patches', 'baseRevision': 100, 'revision': 101,
                'patches': [{'op': 'replace', 'path': ['latestThreadSettings', 'model'], 'value': 'ordered-model'}]}
            channel.queue(patch_message)
            self.wait_until(lambda: service.projections[THREAD].revision == 101)
            self.assertIs(service.projections[THREAD], original)
            self.assertEqual(service.current_settings()[THREAD]['model'], 'ordered-model')
            self.assertEqual(service.resync_attempts, {})
            self.assertEqual(events, [])
            self.assertEqual(len(channels), 1)

    def test_only_typed_verified_protocol_mismatch_requires_connection_diagnostics(self):
        for error, status, reason in [(ControlError('订阅版本或协议正在重新同步'), 'reconnecting', 'recovering'),
                                      (DesktopProtocolUnsupported('verified unsupported build'), 'unsupported', 'protocol_unsupported')]:
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                channels = []
                calls = []
                def factory(*args, on_message, **kwargs):
                    calls.append(None)
                    if len(calls) == 1:
                        raise error
                    return self.Channel(on_message, channels)
                service = DesktopAttentionService(Path(directory), channel_factory=factory)
                service.set_threads([THREAD]); service.start()
                try:
                    self.wait_until(lambda: service.snapshot()['message'] == str(error))
                    self.assertEqual(service.snapshot()['status'], status)
                    self.assertEqual(service.snapshot()['reason_code'], reason)
                    self.wait_until(lambda: service.snapshot()['capabilities']['attention'])
                    self.assertEqual(service.snapshot()['reason_code'], 'connected')
                finally:
                    service.stop()

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

    def test_same_owner_settings_snapshot_and_patches_feed_live_list(self):
        service = DesktopAttentionService('/fixture')
        service.channel = type('Channel', (), {'client_id': CLIENT})()
        service.set_threads([THREAD]); service._health['connected'] = True
        service.projections[THREAD] = AttentionProjection(THREAD, OWNER)
        initial = change()
        initial['conversationState']['latestThreadSettings'] = {'model': 'model-new', 'effort': 'ultra', 'serviceTier': 'priority'}
        def emit(update, owner=OWNER):
            service._on_message({'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                                 'sourceClientId': owner, 'targetClientIds': [CLIENT],
                                 'params': {'hostId': 'local', 'conversationId': THREAD, 'change': update}})
        emit(initial)
        first = service.current_settings()[THREAD]
        self.assertEqual((first['status'], first['model'], first['reasoning_effort'], first['service_tier']), ('current', 'model-new', 'ultra', 'priority'))
        update = {'type': 'patches', 'baseRevision': 1, 'revision': 2, 'patches': [
            {'op': 'replace', 'path': ['latestThreadSettings', 'model'], 'value': 'model-next'},
            {'op': 'replace', 'path': ['latestThreadSettings', 'effort'], 'value': 'high'},
            {'op': 'replace', 'path': ['latestThreadSettings', 'serviceTier'], 'value': 'default'}]}
        emit(update, owner='wrong-owner')
        self.assertEqual(service.current_settings()[THREAD]['model'], 'model-new')
        emit(update)
        current = service.current_settings()[THREAD]
        self.assertEqual((current['model'], current['reasoning_effort'], current['service_tier']), ('model-next', 'high', 'default'))
        self.assertNotIn('PRIVATE', json.dumps(current))
        service._health['connected'] = False
        unavailable = service.current_settings()[THREAD]
        self.assertEqual(unavailable['status'], 'unavailable')
        self.assertNotIn('model', unavailable)

    def test_initial_snapshot_is_pending_for_only_one_candidate(self):
        service = DesktopAttentionService('/fixture')
        service.channel = type('Channel', (), {'discover': lambda self: self.fail('unexpected discovery')})()
        service.projections[THREAD] = AttentionProjection(THREAD, OWNER)
        service.subscribed_at[THREAD] = time.monotonic()
        service._discover([THREAD, 'another-thread'])
        self.assertEqual(set(service.projections), {THREAD})

    def test_unqueried_retained_tasks_do_not_starve_behind_retried_missing_owners(self):
        service = DesktopAttentionService('/fixture')
        class Channel(VerifiedContractFixture):
            owner, client_id = OWNER, CLIENT
            def discover(self): return {'owner_client_id': OWNER}
            def _send(self, value): pass
        service.channel = Channel()
        service.next_discovery['previous-missing'] = time.monotonic() - 1
        service._discover(['previous-missing', 'not-yet-queried'])
        self.assertEqual(set(service.projections), {'not-yet-queried'})

    def test_no_owner_reports_not_loaded_instead_of_indefinite_sync(self):
        service = DesktopAttentionService('/fixture')
        class Channel(VerifiedContractFixture):
            def discover(self): raise DesktopOwnerUnavailable('no owner')
        service.channel = Channel(); service.set_threads([THREAD]); service._health['connected'] = True
        service._discover([THREAD])
        result = service.current_settings()[THREAD]
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('未在 Codex 加载', result['message'])
        self.assertNotIn('model', result)

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
        class Channel(VerifiedContractFixture):
            def discover(self): raise ControlError('Codex 本机对话连接已中断')
        service.channel = Channel()
        with self.assertRaises(ControlError): service._discover([THREAD])
        self.assertGreater(service.quarantine_until[THREAD], time.monotonic() + 290)
        # A new connection skips that candidate rather than repeating the loop.
        service._discover([THREAD])

    def test_transport_loss_during_discovery_does_not_quarantine_a_healthy_candidate(self):
        service = DesktopAttentionService('/fixture')
        class Channel(VerifiedContractFixture):
            def discover(self): raise DesktopConnectionInterrupted('router restarted')
        service.channel = Channel()
        with self.assertRaises(DesktopConnectionInterrupted): service._discover([THREAD])
        self.assertNotIn(THREAD, service.quarantine_until)

    def test_repeated_chat_projection_failures_back_off_with_a_finite_cap(self):
        service = DesktopAttentionService('/fixture')
        class Channel(VerifiedContractFixture):
            client_id = CLIENT
            def _send(self, value): pass
        service.channel = Channel()
        with patch('vibecue.desktop_attention.time.monotonic', return_value=100):
            for expected in (1, 2, 4, 8, 16, 30, 30):
                service.projections[THREAD] = AttentionProjection(THREAD, OWNER)
                service._invalidate_thread(THREAD, 'resync')
                self.assertEqual(service.next_discovery[THREAD], 100 + expected)

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

    def test_long_snapshot_settings_project_without_retaining_conversation_body(self):
        from vibecue.codex_desktop_ipc import MAX_FRAME
        update = change()
        update['conversationState'].update(latestThreadSettings={'model':'long-model','effort':'ultra','serviceTier':'priority'}, turns=[{'text':'PRIVATE BODY ' * 700000}])
        packet = {'type':'broadcast','method':'thread-stream-state-changed','version':11,
                  'sourceClientId':OWNER,'targetClientIds':[CLIENT],
                  'params':{'hostId':'local','conversationId':THREAD,'change':update}}
        body = json.dumps(packet).encode()
        self.assertGreater(len(body), MAX_FRAME)
        self.assertLess(len(body), _AttentionChannel.max_frame_size)
        projection = AttentionProjection(THREAD, OWNER)
        channel = _AttentionChannel(THREAD,on_message=lambda message:projection.apply(message['params']['change']))
        with patch.object(channel,'_read_exact',side_effect=[struct.pack('<I',len(body)),body]):channel._receive(time.monotonic()+2)
        self.assertEqual(projection.settings.snapshot()['model'],'long-model')
        self.assertNotIn('PRIVATE',projected_json(projection))


class WatcherIntegrationTests(unittest.TestCase):
    def test_explicit_list_targets_join_recent_without_loading_or_duplication(self):
        from vibecue.providers import codex
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); path = home / 'log.jsonl'; path.write_text('')
            with patch.object(codex, 'DesktopAttentionService') as service:
                watcher = codex.Watcher(home / 'codex', home / 'cue')
                (watcher.vibecue_home / 'model-settings-targets.json').write_text(json.dumps({'thread_ids': ['visible-old', 'visible-old', '../invalid']}))
                recent = codex.ThreadMetadata('current', path, 'current', 'vscode', time.time())
                interpreter = codex.Interpreter(recent, lambda **kwargs: None)
                interpreter.activity_state = 'running'; interpreter.last_activity = recent.updated_at
                watcher.watched = {recent.thread_id: (codex.TailReader(path), interpreter)}
                with patch.object(watcher, 'metadata', return_value=[recent]): watcher.discover()
                service.return_value.set_threads.assert_called_with(['current', 'current', 'visible-old'])

    def test_live_model_metadata_is_exported_even_without_log_model_context(self):
        from vibecue.providers import codex
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory); path = home / 'log.jsonl'; path.write_text('')
            with patch.object(codex, 'DesktopAttentionService') as service:
                watcher = codex.Watcher(home / 'codex', home / 'cue')
                metadata = codex.ThreadMetadata(THREAD, path, 'fixture', 'vscode', time.time())
                interpreter = codex.Interpreter(metadata, lambda **kwargs: None)
                interpreter.process({'type':'event_msg', 'timestamp':'2026-10-29T00:00:00Z', 'payload':{'type':'task_started','turn_id':'one'}})
                watcher.watched = {THREAD: (codex.TailReader(path), interpreter)}
                live = {'status':'current','source':'codex_desktop','model':'model-live','reasoning_effort':'ultra','service_tier':'priority'}
                service.return_value.snapshot.return_value = {'connected':False,'capabilities':{'attention':False}}
                service.return_value.current_settings.return_value = {THREAD: live}
                watcher.write_status()
                state = json.loads((watcher.vibecue_home / 'watcher-state.json').read_text())
                self.assertEqual(state['current_model_settings'][THREAD], live)
                self.assertEqual(state['task_details'][THREAD]['current_settings']['model'], 'model-live')

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
