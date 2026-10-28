import json
import os
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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


class DesktopIPCTests(unittest.TestCase):
    def connect(self, fake=None):
        fake = fake or FakeSocket()
        patches = [patch.object(ipc, '_private_socket'),
                   patch.object(ipc, '_peer_uid', return_value=os.getuid()),
                   patch.object(ipc, '_peer_bundle', return_value=Path('/Applications/ChatGPT.app')),
                   patch.object(ipc.socket, 'socket', return_value=fake)]
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

    def test_other_peer_uid_is_rejected_before_initialization(self):
        fake, channel = self.connect()
        with patch.object(ipc, '_peer_uid', return_value=os.getuid() + 1):
            with self.assertRaises(ControlError):
                with channel:
                    pass
        self.assertEqual(fake.sent, [])
        self.assertTrue(fake.closed)

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


if __name__ == '__main__':
    unittest.main()
