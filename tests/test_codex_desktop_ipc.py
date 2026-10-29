import json
import os
from pathlib import Path
import socket
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import mock_open, patch

from vibecue import codex_desktop_ipc as ipc
from vibecue.codex_control import ControlError, UnconfirmedUpdate


CLIENT = '11111111-1111-4111-8111-111111111111'
OWNER = '22222222-2222-4222-8222-222222222222'
OTHER = '33333333-3333-4333-8333-333333333333'
MESSAGE = '44444444-4444-4444-8444-444444444444'
THREAD = 'thread-one'


def frame(value):
    data = json.dumps(value, ensure_ascii=False).encode()
    return struct.pack('<I', len(data)) + data


class FakeSocket:
    def __init__(self, handler=None):
        self.pending = bytearray()
        self.sent = []
        self.closed = False
        self.handler = handler
        self.state = {'id': THREAD, 'resumeState': 'resumed',
                      'threadRuntimeStatus': {'type': 'idle'}, 'requests': [],
                      'latestModel': 'test-model', 'latestReasoningEffort': 'high',
                      'turns': [{'turnId': 'turn-one', 'status': 'completed', 'items': [{'text': 'PRIVATE BODY'}]}]}

    def settimeout(self, value):
        self.timeout = value

    def connect(self, path):
        self.path = path

    def sendall(self, data):
        size = struct.unpack('<I', data[:4])[0]
        assert size == len(data) - 4
        value = json.loads(data[4:])
        self.sent.append(value)
        if self.handler and self.handler(self, value):
            return
        if value['type'] == 'request':
            method = value['method']
            result = {'clientId': CLIENT} if method == 'initialize' else {'supportsUntrustedAppInput': True} if method == 'thread-owner-discovery' else {'settings': {'resumeState': 'resumed'}} if method == 'thread-follower-read-model-settings' else {'result': {'turn': {'id': 'turn-two'}}}
            self.pending.extend(frame({'type': 'response', 'requestId': value['requestId'],
                                       'resultType': 'success', 'method': method,
                                       'handledByClientId': OWNER, 'result': result}))
        elif value['type'] == 'broadcast' and value['params']['following']:
            self.pending.extend(frame(self.snapshot()))

    def snapshot(self):
        return {'type': 'broadcast', 'method': 'thread-stream-state-changed',
                'version': 11, 'sourceClientId': OWNER, 'targetClientIds': [CLIENT],
                'params': {'conversationId': THREAD, 'hostId': 'local',
                           'change': {'type': 'snapshot', 'revision': 7, 'conversationState': self.state}}}

    def recv(self, size):
        # Every frame is deliberately fragmented across headers and UTF-8 text.
        size = min(size, 3, len(self.pending))
        result = bytes(self.pending[:size]); del self.pending[:size]
        return result

    def close(self):
        self.closed = True


class LargeFrameSocket(FakeSocket):
    def recv(self, size):
        size = min(size, 32768, len(self.pending))
        result = bytes(self.pending[:size]); del self.pending[:size]
        return result


class DesktopIPCTests(unittest.TestCase):
    def connect(self, fake=None):
        fake = fake or FakeSocket()
        patches = [patch.object(ipc, '_private_socket'),
                   patch.object(ipc, '_peer_uid', return_value=os.getuid()),
                   patch.object(ipc, '_peer_bundle', return_value=Path('/Applications/ChatGPT.app')),
                   patch.object(ipc.socket, 'socket', return_value=fake),
                   patch.object(ipc.select, 'select', side_effect=lambda read, write, error, timeout:
                                ([fake] if fake.pending else [], [], []))]
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        return fake, ipc.DesktopChannel(THREAD, codex_home=Path('/private/tmp/ipc-fixture'))

    def request(self):
        return {'threadId': THREAD, 'input': [{'type': 'text', 'text': '用户明确发送'}], 'clientUserMessageId': MESSAGE}

    def test_readonly_snapshot_is_owner_bound_and_discards_bodies(self):
        fake, channel = self.connect()
        with channel:
            metadata = channel.runtime_snapshot()
        self.assertEqual(metadata['resume_state'], 'resumed')
        self.assertEqual(metadata['thread_runtime_status'], {'type': 'idle'})
        self.assertEqual(metadata['last_turn'], {'turnId': 'turn-one', 'status': 'completed'})
        self.assertFalse(metadata['requests_pending'])
        self.assertNotIn('PRIVATE BODY', json.dumps(metadata))
        methods = [row['method'] for row in fake.sent]
        self.assertEqual(methods, ['initialize', 'thread-owner-discovery', 'thread-stream-following-changed', 'thread-stream-following-changed'])
        self.assertEqual(fake.sent[0]['params'], {'clientType': 'vibecue'})
        self.assertFalse(fake.sent[-1]['params']['following'])
        self.assertEqual(fake.sent[-1]['targetClientIds'], [OWNER])
        self.assertTrue(fake.closed)

    def test_unrelated_snapshot_is_ignored(self):
        def handler(sock, value):
            if value['type'] == 'broadcast' and value['params']['following']:
                wrong = sock.snapshot(); wrong['sourceClientId'] = OTHER
                wrong['params']['change']['conversationState'] = {'resumeState': 'BAD SOURCE'}
                sock.pending.extend(frame(wrong)); sock.pending.extend(frame(sock.snapshot())); return True
        fake, channel = self.connect(FakeSocket(handler))
        with channel:
            self.assertEqual(channel.runtime_snapshot()['resume_state'], 'resumed')

    def test_snapshot_settle_uses_low_full_baseline_and_contiguous_live_delta(self):
        def handler(sock, value):
            if value['type'] == 'broadcast' and value['params']['following']:
                high = sock.snapshot()
                high['params']['change']['revision'] = 7805
                sock.pending.extend(frame(high))
                low = sock.snapshot()
                low['params']['change']['revision'] = 322
                low['params']['change']['conversationState'] = {
                    'id': THREAD, 'resumeState': 'resumed', 'requests': [],
                    'threadRuntimeStatus': {'type': 'active', 'activeFlags': []},
                    'latestThreadSettings': {'model': 'live-model', 'effort': 'ultra'},
                    'turns': [{'turnId': 'live-turn', 'status': 'inProgress', 'items': [{'text': 'PRIVATE LIVE BODY'}]}]}
                sock.pending.extend(frame(low))
                delta = sock.snapshot()
                delta['params']['change'] = {'type': 'patches', 'baseRevision': 322, 'revision': 323,
                    'patches': [{'op': 'replace', 'path': ['threadRuntimeStatus'], 'value': {'type': 'idle'}},
                                {'op': 'replace', 'path': ['turns', 0, 'status'], 'value': 'completed'},
                                {'op': 'replace', 'path': ['latestThreadSettings', 'serviceTier'], 'value': 'priority'}]}
                sock.pending.extend(frame(delta))
                return True
        fake, channel = self.connect(FakeSocket(handler))
        with channel:
            metadata = channel.runtime_snapshot()
        self.assertEqual(metadata['revision'], 323)
        self.assertEqual(metadata['thread_runtime_status'], {'type': 'idle'})
        self.assertEqual(metadata['last_turn'], {'turnId': 'live-turn', 'status': 'completed'})
        self.assertEqual(metadata['thread_settings'], {'model': 'live-model', 'effort': 'ultra', 'serviceTier': 'priority'})
        self.assertFalse(metadata['requests_pending'])
        self.assertNotIn('PRIVATE', json.dumps(metadata))
        self.assertEqual([row['method'] for row in fake.sent],
                         ['initialize', 'thread-owner-discovery', 'thread-stream-following-changed', 'thread-stream-following-changed'])

    def test_wrong_identity_snapshots_after_initial_do_not_replace_verified_metadata(self):
        def handler(sock, value):
            if value['type'] == 'broadcast' and value['params']['following']:
                sock.pending.extend(frame(sock.snapshot()))
                for field, replacement in [('sourceClientId', OTHER), ('targetClientIds', [OTHER]),
                                           ('targetClientIds', [CLIENT, None]), ('version', 12)]:
                    wrong = sock.snapshot()
                    wrong[field] = replacement
                    wrong['params']['change']['revision'] = 999
                    sock.pending.extend(frame(wrong))
                for field, replacement in [('conversationId', 'wrong-thread'), ('hostId', 'other-host')]:
                    wrong = sock.snapshot()
                    wrong['params'][field] = replacement
                    wrong['params']['change']['revision'] = 999
                    sock.pending.extend(frame(wrong))
                return True
        _, channel = self.connect(FakeSocket(handler))
        with channel:
            self.assertEqual(channel.runtime_snapshot()['revision'], 7)

    def test_gap_or_frame_budget_exhaustion_never_returns_cached_snapshot(self):
        for invalid in ('gap', 'too-many-frames'):
            with self.subTest(invalid=invalid):
                def handler(sock, value):
                    if value['type'] == 'broadcast' and value['params']['following']:
                        sock.pending.extend(frame(sock.snapshot()))
                        for index in range(1 if invalid == 'gap' else ipc.SNAPSHOT_SETTLE_FRAMES):
                            delta = sock.snapshot()
                            delta['params']['change'] = {'type': 'patches', 'baseRevision': 0 if invalid == 'gap' else 7 + index,
                                                       'revision': 8 + index, 'patches': []}
                            sock.pending.extend(frame(delta))
                        return True
                fake, channel = self.connect(FakeSocket(handler))
                with channel:
                    with self.assertRaises(ipc.DesktopConnectionInterrupted):
                        channel.runtime_snapshot()
                self.assertFalse(any(row['method'] == 'thread-follower-start-turn' for row in fake.sent))

    def test_real_socket_partial_frame_after_snapshot_rejects_old_metadata(self):
        # This uses real sockets/select/read deadlines independently of the
        # in-memory fixture's deterministic readiness adapter.
        for partial in (b'\x20\x00', struct.pack('<I', 32) + b'{'):
            with self.subTest(partial_header=len(partial) < 4):
                connection, server = socket.socketpair()
                channel = ipc.DesktopChannel(THREAD, timeout=1)
                channel.socket, channel.client_id, channel.owner = connection, CLIENT, OWNER
                try:
                    server.sendall(frame(FakeSocket().snapshot()) + partial)
                    with self.assertRaises(ipc.DesktopConnectionInterrupted):
                        channel.runtime_snapshot()
                finally:
                    channel.close(); server.close()

    def test_pending_or_malformed_requests_never_look_clear(self):
        for requests in [[{'completed': False}], [None], None]:
            raw = {'requests': requests}
            self.assertTrue(ipc._snapshot_metadata(raw, THREAD, OWNER, 1)['requests_pending'])
        for flags in [None, 'waitingOnApproval', [{}], ['waitingOnApproval'] * 21]:
            self.assertIsNone(ipc._status_copy({'type': 'active', 'activeFlags': flags})['activeFlags'])

    def test_canonical_tail_provides_current_active_turn_without_bodies(self):
        raw = {'requests': [], 'turns': [{'turnId': 'stale', 'status': 'completed'}],
               'turnHistory': {'kind': 'canonical', 'history': {
                   'entitiesByKey': {'old': {'turnId': 'older', 'status': 'completed'},
                                     'new': {'turnId': 'current', 'status': 'inProgress', 'items': [{'text': 'CANONICAL PRIVATE'}]}},
                   'islands': [{'entries': [{'value': 'old'}]}, {'entries': [{'value': 'new'}]}]}}}
        metadata = ipc._snapshot_metadata(raw, THREAD, OWNER, 4)
        self.assertEqual(metadata['last_turn'], {'turnId': 'current', 'status': 'inProgress'})
        self.assertNotIn('CANONICAL PRIVATE', json.dumps(metadata))

    def test_canonical_empty_tail_does_not_reuse_stale_legacy_turn(self):
        raw = {'requests': [], 'turns': [{'turnId': 'stale'}], 'turnHistory': {'kind': 'canonical', 'history': {'entitiesByKey': {}, 'islands': []}}}
        self.assertEqual(ipc._snapshot_metadata(raw, THREAD, OWNER, 1)['last_turn'], {})

    def test_no_owner_has_distinct_exception_and_no_mutation(self):
        def handler(sock, value):
            if value.get('method') == 'thread-owner-discovery':
                sock.pending.extend(frame({'type': 'response', 'requestId': value['requestId'], 'resultType': 'error', 'error': 'no-client-found'})); return True
        fake, channel = self.connect(FakeSocket(handler))
        with channel:
            with self.assertRaises(ipc.DesktopOwnerUnavailable):
                channel.discover()
        self.assertNotIn('thread-follower-start-turn', [row['method'] for row in fake.sent])

    def test_restarted_router_errors_are_retryable_only_before_mutation(self):
        def handler(sock, value):
            if value.get('method') == 'thread-owner-discovery':
                sock.pending.extend(frame({'type': 'response', 'requestId': value['requestId'], 'resultType': 'error', 'error': 'client-disconnected'})); return True
        fake, channel = self.connect(FakeSocket(handler))
        with channel:
            with self.assertRaises(ipc.DesktopConnectionInterrupted):
                channel.discover()
        self.assertFalse(any(row.get('method') == 'thread-follower-start-turn' for row in fake.sent))

    def test_status_session_deadline_expires_before_any_desktop_bytes(self):
        fake, channel = self.connect()
        channel.deadline = 99
        with patch.object(ipc.time, 'monotonic', return_value=100):
            with self.assertRaises(ipc.DesktopConnectionInterrupted):
                with channel:
                    pass
        self.assertEqual(fake.sent, [])
        self.assertTrue(fake.closed)

    def test_other_peer_uid_is_rejected_before_initialization(self):
        fake, channel = self.connect()
        with patch.object(ipc, '_peer_uid', return_value=os.getuid() + 1):
            with self.assertRaises(ControlError):
                with channel:
                    pass
        self.assertEqual(fake.sent, [])
        self.assertTrue(fake.closed)

    def test_only_verified_codex_peer_build_mismatch_has_unsupported_type(self):
        def proc_pidpath(pid, buffer, size):
            buffer.value = b'/Applications/Codex.app/Contents/MacOS/Codex'
            return len(buffer.value)
        peer = SimpleNamespace(getsockopt=lambda *args: struct.pack('i', 42))
        libproc = SimpleNamespace(proc_pidpath=proc_pidpath)
        info = {'CFBundleIdentifier': 'com.openai.codex',
                'CFBundleShortVersionString': '99.0.0', 'CFBundleVersion': '99999'}
        with patch.object(ipc.sys, 'platform', 'darwin'), patch.object(ipc.ctypes, 'CDLL', return_value=libproc), \
                patch.object(Path, 'resolve', return_value=Path('/Applications/Codex.app')), \
                patch.object(Path, 'open', mock_open(read_data=b'plist')), patch.object(ipc.plistlib, 'load', return_value=info):
            with self.assertRaises(ipc.DesktopProtocolUnsupported):
                ipc._peer_bundle(peer)
            info['CFBundleIdentifier'] = 'com.example.other'
            with self.assertRaises(ControlError) as caught:
                ipc._peer_bundle(peer)
            self.assertNotIsInstance(caught.exception, ipc.DesktopProtocolUnsupported)

    def test_private_socket_rejects_symlink_and_broad_permissions(self):
        folder = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=os.getuid())
        good = SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid())
        with patch.object(Path, 'lstat', side_effect=[folder, good]):
            ipc._private_socket(Path('/private/tmp/fixture/ipc.sock'))
        for target in [SimpleNamespace(st_mode=stat.S_IFLNK | 0o600, st_uid=os.getuid()), SimpleNamespace(st_mode=stat.S_IFSOCK | 0o666, st_uid=os.getuid()), SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600, st_uid=os.getuid() + 1)]:
            with patch.object(Path, 'lstat', side_effect=[folder, target]), self.assertRaises(ControlError):
                ipc._private_socket(Path('/private/tmp/fixture/ipc.sock'))

    def test_start_uses_same_owner_text_and_inherited_settings_only(self):
        fake, channel = self.connect()
        with channel:
            self.assertEqual(channel.start_turn(self.request()), {'turn': {'id': 'turn-two'}})
        sent = fake.sent[-1]
        self.assertEqual(sent['targetClientId'], OWNER)
        self.assertEqual(sent['version'], 2)
        self.assertEqual(sent['params'], {'conversationId': THREAD, 'turnStart': {'request': self.request(), 'context': {'inheritThreadSettings': True}}})
        self.assertFalse(set(sent) & {'hostId', 'model', 'approvalPolicy', 'sandbox'})

    def test_start_rejects_overrides_wrong_target_and_invalid_uuid(self):
        fake, channel = self.connect()
        with channel:
            bad = self.request(); bad['model'] = 'other-model'
            with self.assertRaises(ControlError): channel.start_turn(bad)
            bad = self.request(); bad['threadId'] = 'other-thread'
            with self.assertRaises(ControlError): channel.start_turn(bad)
            bad = self.request(); bad['clientUserMessageId'] = '-' * 36
            with self.assertRaises(ControlError): channel.start_turn(bad)
        self.assertEqual(len(fake.sent), 1)

    def test_sent_but_disconnected_is_unconfirmed_and_never_retried(self):
        def handler(sock, value):
            return value.get('method') == 'thread-follower-start-turn'
        fake, channel = self.connect(FakeSocket(handler))
        with channel:
            with self.assertRaises(UnconfirmedUpdate): channel.start_turn(self.request())
        self.assertEqual(sum(row.get('method') == 'thread-follower-start-turn' for row in fake.sent), 1)

    def test_missing_or_malformed_mutation_receipt_is_unconfirmed(self):
        for result in [None, {}, {'result': None}, {'result': 'unexpected'}]:
            def handler(sock, value, result=result):
                if value.get('method') == 'thread-follower-start-turn':
                    sock.pending.extend(frame({'type': 'response', 'requestId': value['requestId'], 'resultType': 'success', 'method': value['method'], 'handledByClientId': OWNER, 'result': result})); return True
            fake, channel = self.connect(FakeSocket(handler))
            with channel:
                with self.assertRaises(UnconfirmedUpdate): channel.start_turn(self.request())

    def test_wrong_owner_response_is_unconfirmed_after_send(self):
        def handler(sock, value):
            if value.get('method') == 'thread-follower-start-turn':
                sock.pending.extend(frame({'type': 'response', 'requestId': value['requestId'], 'resultType': 'success', 'method': value['method'], 'handledByClientId': OTHER, 'result': {'result': {'turn': {'id': 'turn-two'}}}})); return True
        fake, channel = self.connect(FakeSocket(handler))
        with channel:
            with self.assertRaises(UnconfirmedUpdate): channel.start_turn(self.request())

    def test_internal_owner_error_after_send_is_unconfirmed(self):
        def handler(sock, value):
            if value.get('method') == 'thread-follower-start-turn':
                sock.pending.extend(frame({'type': 'response', 'requestId': value['requestId'], 'resultType': 'error', 'error': 'unknown-owner-delivery'})); return True
        fake, channel = self.connect(FakeSocket(handler))
        with channel:
            with self.assertRaises(UnconfirmedUpdate): channel.start_turn(self.request())

    def test_unknown_method_cannot_cross_socket(self):
        fake, channel = self.connect()
        with channel:
            for method in ['turn/interrupt', 'thread-follower-command-approval-decision', 'thread/resume', 'account/login/start']:
                with self.assertRaises(ControlError): channel._call(method, {})
        self.assertEqual(len(fake.sent), 1)

    def test_oversize_frame_is_rejected(self):
        fake, channel = self.connect()
        with channel:
            fake.pending.extend(struct.pack('<I', ipc.MAX_FRAME + 1))
            with self.assertRaises(ControlError): channel._receive(10**12)

    def test_long_conversation_snapshot_is_reduced_to_metadata(self):
        fake = LargeFrameSocket()
        fake.state['turns'][0]['items'][0]['text'] = 'PRIVATE BODY ' * 700000
        self.assertGreater(len(frame(fake.snapshot())) - 4, ipc.MAX_FRAME)
        fake, channel = self.connect(fake)
        with channel:
            metadata = channel.runtime_snapshot()
        self.assertEqual(metadata['last_turn'], {'turnId': 'turn-one', 'status': 'completed'})
        self.assertEqual(metadata['model'], 'test-model')
        self.assertNotIn('PRIVATE BODY', json.dumps(metadata))
        self.assertTrue(fake.closed)
        self.assertFalse(fake.sent[-1]['params']['following'])
        self.assertNotIn('thread-follower-start-turn', [row.get('method') for row in fake.sent])

    def test_long_stream_budget_requires_the_requested_thread_and_owner(self):
        fake, channel = self.connect(LargeFrameSocket())
        with channel:
            channel.runtime_snapshot()
            for wrong_owner in (True, False):
                wrong = fake.snapshot()
                wrong['params']['change']['conversationState'] = {'body': 'private ' * 1100000}
                if wrong_owner: wrong['sourceClientId'] = OTHER
                else: wrong['params']['conversationId'] = 'other-thread'
                fake.pending.extend(frame(wrong))
                with self.assertRaises(ControlError): channel._receive(10**12)

    def test_late_long_stream_is_consumed_before_rpc_without_raising_rpc_limit(self):
        fake, channel = self.connect(LargeFrameSocket())
        with channel:
            channel.runtime_snapshot()
            late = fake.snapshot()
            late['params']['change'] = {'type': 'patches', 'revision': 8, 'patches': [{'body': 'private ' * 1100000}]}
            fake.pending.extend(frame(late))
            self.assertEqual(channel.read_settings(), {'resumeState': 'resumed'})
            fake.pending.extend(frame({'type': 'response', 'result': {'body': 'private ' * 1100000}}))
            with self.assertRaises(ControlError): channel._receive(10**12)
        self.assertEqual(sum(row.get('method') == 'thread-follower-read-model-settings' for row in fake.sent), 1)

    def test_snapshot_stream_retains_finite_header_limit_and_invalid_json_rejection(self):
        fake, channel = self.connect()
        with channel:
            channel.runtime_snapshot()
            fake.pending.extend(struct.pack('<I', ipc.MAX_STREAM_FRAME + 1))
            with self.assertRaises(ControlError): channel._receive(10**12)
            fake.pending.extend(struct.pack('<I', 2) + b'{{')
            with self.assertRaises(ControlError): channel._receive(10**12)


if __name__ == '__main__':
    unittest.main()
