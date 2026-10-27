"""Protocol fixtures and a fake backend only; never start the installed Codex."""
import hashlib
import json
import os
from pathlib import Path
import queue
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from vibecue.providers.approval_bridge import (
    APPROVAL_METHODS, ApprovalStreamMonitor, MAX_LINE_BYTES, _LineReader,
    _Observer, _is_stdio_app_server, main,
)


def request(identifier=7, thread='thread', turn='turn',
            method='item/commandExecution/requestApproval'):
    return {'id': identifier, 'method': method,
            'params': {'threadId': thread, 'turnId': turn, 'itemId': 'item',
                       'command': 'PRIVATE COMMAND NEVER PERSIST',
                       'reason': 'PRIVATE REASON NEVER PERSIST'}}


class ApprovalMonitorTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.emitted = []
        self.resolved = []
        self.monitor = ApprovalStreamMonitor(
            home='private-home', session_id='test-session', clock=lambda: self.now,
            wall_clock=lambda: 1_700_000_000 + self.now,
            emit=lambda *args: self.emitted.append(args),
            resolve=lambda *args: self.resolved.append(args),
            identifier=lambda raw: hashlib.sha256(('codex:' + raw).encode()).hexdigest(),
        )

    def send(self, message, direction='server', at=None):
        self.monitor.handle_message(direction, message, self.now if at is None else at)

    def settle(self):
        self.now += 0.4
        self.monitor.flush()

    def test_auto_review_hook_and_approval_status_never_alert(self):
        for method in ('item/autoApprovalReview/started',
                       'item/autoApprovalReview/completed',
                       'autoApprovalReview/strictReviewRequired',
                       'PermissionRequest', 'thread/status/changed'):
            self.send({'id': 8, 'method': method,
                       'params': {'threadId': 'thread', 'turnId': 'turn',
                                  'status': 'waitingOnApproval', 'review': {'status': 'approved'}}})
        self.settle()
        self.assertEqual(self.emitted, [])
        self.assertEqual(self.monitor.pending_count, 0)

    def test_positive_methods_wait_for_settle_and_keep_only_identifiers(self):
        for index, method in enumerate(sorted(APPROVAL_METHODS)):
            message = request(index, method=method)
            if method in ('execCommandApproval', 'applyPatchApproval'):
                message['params']['conversationId'] = message['params'].pop('threadId')
            self.send(message)
        self.now += 0.3
        self.monitor.flush()
        self.assertEqual(self.emitted, [])
        self.now += 0.1
        self.monitor.flush()
        self.assertEqual(len(self.emitted), len(APPROVAL_METHODS))
        self.assertTrue(self.monitor.connected)
        self.assertIsNotNone(self.monitor.last_protocol_at)
        for pending in self.monitor.pending.values():
            self.assertNotIn('PRIVATE', repr(pending))
        for thread, turn, raw_key, home in self.emitted:
            self.assertEqual((thread, turn, home), ('thread', 'turn', 'private-home'))
            self.assertNotIn('PRIVATE', raw_key)
            self.assertIn('test-session', raw_key)

    def test_resolved_before_settle_never_alerts(self):
        for index, response in enumerate((
                {'id': 7, 'result': {'decision': 'accept'}},
                {'id': 7, 'error': {'code': -1}},
                {'method': 'serverRequest/resolved',
                 'params': {'requestId': 7, 'threadId': 'thread'}})):
            with self.subTest(response=index):
                self.send(request())
                self.now += 0.1
                self.send(response, 'server' if 'method' in response else 'client')
                self.settle()
                self.assertEqual(self.monitor.pending_count, 0)
        self.assertEqual(self.emitted, [])
        self.assertEqual(self.resolved, [])

    def test_shown_request_emits_matching_resolution_control(self):
        self.send(request())
        self.settle()
        raw_key = self.emitted[0][2]
        target = hashlib.sha256(('codex:' + raw_key).encode()).hexdigest()
        self.send({'method': 'serverRequest/resolved',
                   'params': {'threadId': 'thread', 'requestId': 7}})
        self.assertEqual(self.resolved, [(target, 'thread', 'private-home')])
        self.assertEqual(self.monitor.pending_count, 0)
        self.send({'id': 7, 'result': {}}, 'client')
        self.assertEqual(len(self.resolved), 1)

    def test_opposite_direction_request_and_response_ids_do_not_collide(self):
        self.send(request())
        self.send({'id': 7, 'method': 'thread/read', 'params': {'threadId': 'thread'}}, 'client')
        self.send({'id': 7, 'result': {'thread': {'id': 'thread'}}})
        self.send({'id': '7', 'result': {}}, 'client')
        self.send({'id': True, 'result': {}}, 'client')
        self.settle()
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self.monitor.pending_count, 1)
        self.send({'id': 7, 'result': {}}, 'client')
        self.assertEqual(self.monitor.pending_count, 0)

    def test_response_queued_before_its_request_suppresses_alert(self):
        self.send({'id': 7, 'result': {}}, 'client', at=100.2)
        self.send(request(), at=100.0)
        self.settle()
        self.assertEqual(self.emitted, [])
        # A genuinely later request reusing an old ID is still a new wait.
        self.send(request(), at=100.3)
        self.settle()
        self.assertEqual(len(self.emitted), 1)

    def test_eof_and_turn_completion_clear_only_matching_pending_requests(self):
        self.send(request())
        self.send(request(8, thread='other', turn='other-turn'))
        self.settle()
        self.send({'method': 'turn/completed',
                   'params': {'threadId': 'thread', 'turn': {'id': 'turn'}}})
        self.assertEqual(self.monitor.pending_count, 1)
        self.assertEqual(self.resolved[0][1], 'thread')
        self.send({'method': 'turn/interrupted',
                   'params': {'threadId': 'other', 'turnId': 'other-turn'}})
        self.assertEqual(self.monitor.pending_count, 0)
        self.send(request(9))
        self.monitor.close()
        self.settle()
        self.assertEqual(len(self.emitted), 2)
        self.assertFalse(self.monitor.connected)

    def test_eof_resolves_previously_shown_request(self):
        self.send(request())
        self.settle()
        self.monitor.close()
        self.assertEqual(len(self.resolved), 1)
        self.assertEqual(self.monitor.pending_count, 0)

    def test_parse_gap_and_oversized_line_discard_uncertain_waits(self):
        reader = _LineReader(self.monitor, 'server')
        line = json.dumps(request()).encode() + b'\n'
        reader.feed(line[:5], self.now)
        reader.feed(line[5:], self.now + 0.01)
        self.assertEqual(self.monitor.pending_count, 1)
        reader.feed(b'x' * (MAX_LINE_BYTES + 1), self.now + 0.02)
        self.assertEqual(self.monitor.pending_count, 0)
        self.assertLessEqual(len(reader.partial), MAX_LINE_BYTES)
        reader.feed(b'\n' + line, self.now + 0.03)
        self.settle()
        self.assertEqual(len(self.emitted), 1)
        reader.feed(b'invalid json\n', self.now)
        self.assertEqual(self.monitor.pending_count, 0)
        self.assertEqual(len(self.resolved), 1)

    def test_settle_starts_after_full_request_was_forwarded(self):
        reader = _LineReader(self.monitor, 'server')
        line = json.dumps(request()).encode() + b'\n'
        reader.feed(line[:20], 90.0, 90.01)
        reader.feed(line[20:], 100.0, 100.2)
        self.now = 100.4
        self.monitor.flush()
        self.assertEqual(self.emitted, [])
        self.now = 100.6
        self.monitor.flush()
        self.assertEqual(len(self.emitted), 1)

    def test_forwarding_timestamp_preserves_fast_response_race(self):
        self.monitor.handle_message('client', {'id': 7, 'result': {}},
                                    observed_at=100.15, wire_at=100.1)
        self.monitor.handle_message('server', request(),
                                    observed_at=100.2, wire_at=100.0)
        self.settle()
        self.assertEqual(self.emitted, [])

    def test_notification_without_request_id_is_not_positive(self):
        message = request()
        message.pop('id')
        self.send(message)
        message = request(10)
        message['params'].pop('threadId')
        self.send(message)
        self.settle()
        self.assertEqual(self.emitted, [])

    def test_spool_failure_does_not_escape_or_leave_a_pending_alert(self):
        self.monitor.emit = mock.Mock(side_effect=OSError('disk unavailable'))
        self.send(request())
        self.settle()
        self.assertEqual(self.monitor.pending_count, 0)
        self.assertEqual(len(self.resolved), 1)


class BridgeInvocationTests(unittest.TestCase):
    def test_only_real_stdio_server_invocations_are_observed(self):
        self.assertTrue(_is_stdio_app_server(['-c', 'features.code_mode_host=true',
                                              'app-server', '--analytics-default-enabled']))
        self.assertTrue(_is_stdio_app_server(['app-server', '--listen=stdio://']))
        for args in (['exec', 'app-server'], ['--version'],
                     ['app-server', 'generate-json-schema'],
                     ['app-server', '--listen', 'ws://127.0.0.1:1'],
                     ['app-server', '--listen=unix://'], ['app-server', '--help']):
            self.assertFalse(_is_stdio_app_server(args), args)

    def test_main_execs_non_server_backend_without_protocol_interception(self):
        with mock.patch('vibecue.providers.approval_bridge._find_real_cli', return_value='/fake/codex'), \
                mock.patch('vibecue.providers.approval_bridge.os.execve', side_effect=SystemExit(23)) as execute:
            with self.assertRaises(SystemExit):
                main(['--version'])
            path, args, environment = execute.call_args[0]
            self.assertEqual((path, args), ('/fake/codex', ['/fake/codex', '--version']))
            self.assertEqual(environment['CODEX_CLI_PATH'], '/fake/codex')

    def test_observer_health_private_and_only_connected_after_real_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = _Observer(12345, home=directory)
            try:
                deadline = time.monotonic() + 2
                while not list((Path(directory) / 'bridges').glob('*.json')) and time.monotonic() < deadline:
                    time.sleep(0.01)
                path = next((Path(directory) / 'bridges').glob('*.json'))
                self.assertFalse(json.loads(path.read_text())['connected'])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                observer.offer('server', b'{"id":0,"result":{"user":"PRIVATE"}}\n', time.monotonic())
                deadline = time.monotonic() + 2
                while not observer.monitor.connected and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(observer.monitor.connected)
            finally:
                observer.close()
                observer.join()
            payload = json.loads(path.read_text())
            self.assertFalse(payload['connected'])
            self.assertEqual(payload['backend_pid'], 12345)
            self.assertEqual(payload['transport'], 'stdio')
            self.assertIsNotNone(payload['last_protocol_at'])
            self.assertNotIn('PRIVATE', path.read_text())

    def test_queue_overflow_disables_monitor_instead_of_assuming_pending(self):
        # Construct only the enqueue boundary: no worker or Codex process needed.
        observer = object.__new__(_Observer)
        import threading
        observer.invalidated = threading.Event()
        observer.finished = threading.Event()
        observer.chunks = queue.Queue(maxsize=1)
        observer.offer('server', b'first', 1)
        observer.offer('client', b'resolution that cannot fit', 2)
        self.assertTrue(observer.invalidated.is_set())

    def test_real_private_spool_pairs_permission_and_resolution_without_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(os.environ, CODEX_HOME=str(root / 'empty-codex-home')):
                observer = _Observer(12345, home=root / 'vibecue')
                try:
                    observer.offer('server', json.dumps(request()).encode() + b'\n', time.monotonic())
                    event_dir = root / 'vibecue' / 'events'
                    deadline = time.monotonic() + 3
                    while not list(event_dir.glob('*.json')) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    event_path = next(event_dir.glob('*.json'))
                    event = json.loads(event_path.read_text())
                    self.assertEqual(event['kind'], 'permission')
                    self.assertIs(event['requires_user_action'], True)
                    self.assertEqual(event['message'], 'Codex 正在等待你手动批准')
                    self.assertNotIn('PRIVATE', event_path.read_text())
                    observer.offer('client', b'{"id":7,"result":{"decision":"accept"}}\n', time.monotonic())
                    deadline = time.monotonic() + 2
                    while not list(event_dir.glob('*resolve.json')) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    control = json.loads(next(event_dir.glob('*resolve.json')).read_text())
                    self.assertEqual(control['action'], 'resolve')
                    self.assertEqual(control['target_id'], event['id'])
                finally:
                    observer.close()
                    observer.join()

    def test_fake_backend_bytes_survive_partial_chunks_and_oversized_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = root / 'mock-codex'
            backend.write_text('''#!{python}
import os, sys
data = bytearray()
while True:
    chunk = os.read(0, 7919)
    if not chunk:
        break
    data.extend(chunk)
    view = memoryview(chunk)
    while view:
        count = os.write(1, view)
        view = view[count:]
with open(os.environ['MOCK_CAPTURE'], 'wb') as stream:
    stream.write(data)
os.write(2, ('backend=' + os.environ['CODEX_CLI_PATH'] + '\\n').encode())
sys.exit(19)
'''.format(python=sys.executable))
            backend.chmod(0o700)
            capture = root / 'captured.bin'
            environment = dict(os.environ, MOCK_CAPTURE=str(capture), CODEX_HOME=str(root / 'codex-home'),
                               VIBECUE_HOME=str(root / 'vibecue-home'), CODEX_CLI_PATH='must-not-recurse')
            script = ('from vibecue.providers.approval_bridge import run_bridge; import sys; '
                      'sys.exit(run_bridge(sys.argv[1], ["-c", "features.code_mode_host=true", '
                      '"app-server", "--analytics-default-enabled"]))')
            payload = (b'\x00\xfffirst partial\r\n' + b'x' * (MAX_LINE_BYTES + 100) + b'\n' +
                       b'{"method":"item/autoApprovalReview/completed","params":{"threadId":"mock"}}\n')
            child = subprocess.Popen([sys.executable, '-c', script, str(backend)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=environment)
            stdout, stderr = child.communicate(payload, timeout=12)
            self.assertEqual(child.returncode, 19)
            self.assertEqual(stdout, payload)
            self.assertEqual(capture.read_bytes(), payload)
            self.assertEqual(stderr, ('backend=' + str(backend) + '\n').encode())
            event_dir = root / 'vibecue-home' / 'events'
            self.assertFalse(event_dir.exists() and list(event_dir.glob('*.json')))


if __name__ == '__main__':
    unittest.main()
