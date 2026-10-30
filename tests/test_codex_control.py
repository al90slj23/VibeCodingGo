import json
import io
import fcntl
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from vibecue.codex_control import (Controller, ControlError, UnconfirmedUpdate,
                                  cached_catalog, connections, normalize_model, run, unique_threads, socket_call)
from vibecue.codex_control import main, progress_event
from vibecue.codex_control_relay import ControlHub, atomic_pipe_write


def model(identifier='model-one', fast=True):
    return {'id': identifier, 'model': identifier, 'displayName': identifier,
            'supportedReasoningEfforts': [{'reasoningEffort': 'high'}, {'reasoningEffort': 'max'}],
            'defaultReasoningEffort': 'high',
            'serviceTiers': [{'id': 'priority', 'name': 'Fast', 'description': '1.5x speed'}] if fast else []}


class FakeChannel:
    def __init__(self, notify=True):
        self.calls = []
        self.notify = notify
        self.live = {}
        self.revision = 0

    def request(self, method, params, timeout=8):
        self.calls.append((method, dict(params)))
        if method == 'model/list':
            return {'data': [model(), model('model-slow', False)]}
        if method == 'thread/loaded/list':
            return {'data': ['one', 'two']}
        if method == 'thread/read':
            return {'thread': {'id': params['threadId'], 'model': 'model-one', 'reasoningEffort': 'high',
                               'preview': 'PRIVATE-CONVERSATION-TEXT'}}
        if method == 'thread/settings/update':
            if self.notify:
                self.revision += 1
                value = {'model': 'model-one', 'effort': 'high', 'service_tier': None}
                value.update({('service_tier' if key == 'serviceTier' else key): val
                              for key, val in params.items() if key != 'threadId'})
                self.live[params['threadId']] = (value, self.revision)
            return {}
        raise AssertionError(method)

    def settings(self, identifier):
        return self.live.get(identifier, ({}, 0))

    def wait_settings(self, identifier, after, patch, timeout=2):
        value, revision = self.settings(identifier)
        return value if revision > after else None


class ControlPolicyTests(unittest.TestCase):
    def setUp(self):
        desktop = patch('vibecue.codex_control.desktop_run', return_value=None)
        desktop.start(); self.addCleanup(desktop.stop)

    def test_batch_deduplicates_and_changes_only_selected_settings(self):
        channel = FakeChannel()
        result = Controller(channel).apply({'thread_ids': ['one', 'one', 'two'], 'effort': 'max', 'speed': 'fast'})
        self.assertEqual(result['summary'], {'selected': 2, 'verified': 2, 'unverified': 0, 'failed': 0})
        updates = [params for method, params in channel.calls if method == 'thread/settings/update']
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[0], {'threadId': 'one', 'effort': 'max', 'serviceTier': 'priority'})
        self.assertNotIn('PRIVATE-CONVERSATION', json.dumps(result))
        self.assertTrue(all(method in ('model/list', 'thread/loaded/list', 'thread/read', 'thread/settings/update')
                            for method, _ in channel.calls))

    def test_keep_omits_tier_standard_is_explicit_and_unsupported_fast_does_not_mutate(self):
        channel = FakeChannel(); controller = Controller(channel)
        controller.apply({'thread_ids': ['one'], 'effort': 'high', 'speed': 'keep'})
        self.assertNotIn('serviceTier', channel.calls[-1][1])
        controller.apply({'thread_ids': ['one'], 'speed': 'standard'})
        self.assertEqual(channel.calls[-1][1]['serviceTier'], 'default')
        before = sum(method == 'thread/settings/update' for method, _ in channel.calls)
        result = controller.apply({'thread_ids': ['one'], 'model': 'model-slow', 'speed': 'fast'})
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(sum(method == 'thread/settings/update' for method, _ in channel.calls), before)

    def test_unknown_model_effort_and_unloaded_threads_never_resume(self):
        channel = FakeChannel(); controller = Controller(channel)
        for request in ({'thread_ids': ['one'], 'model': 'not-available'},
                        {'thread_ids': ['one'], 'effort': 'unsupported'},
                        {'thread_ids': ['not-loaded'], 'effort': 'high'}):
            self.assertEqual(controller.apply(request)['summary']['failed'], 1)
        self.assertFalse(any(method == 'thread/settings/update' for method, _ in channel.calls))
        self.assertFalse(any('resume' in method or method.startswith('turn/') for method, _ in channel.calls))

    def test_approval_or_message_fields_rejected_before_any_rpc(self):
        channel = FakeChannel()
        for extra in ({'approvalPolicy': 'never'}, {'input': 'hello'}, {'cwd': '/other'}):
            with self.assertRaises(ControlError):
                Controller(channel).apply({'thread_ids': ['one'], 'effort': 'high', **extra})
        self.assertEqual(channel.calls, [])

    def test_ack_without_new_settings_notification_is_not_verified(self):
        channel = FakeChannel(notify=False)
        result = Controller(channel).apply({'thread_ids': ['one'], 'effort': 'max'})
        self.assertEqual(result['summary']['unverified'], 1)
        self.assertFalse(result['results'][0]['verified'])

    def test_sent_timeout_is_reported_unverified_and_never_retried(self):
        channel = FakeChannel(); original = channel.request
        def request(method, params, timeout=8):
            if method == 'thread/settings/update':
                channel.calls.append((method, params))
                raise UnconfirmedUpdate('已经发送，结果待核对')
            return original(method, params)
        channel.request = request
        result = Controller(channel).apply({'thread_ids': ['one'], 'effort': 'max'})
        self.assertEqual(result['summary']['unverified'], 1)
        self.assertEqual(sum(method == 'thread/settings/update' for method, _ in channel.calls), 1)

    def test_model_cache_is_explicitly_offline_and_does_not_expose_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'models_cache.json'
            path.write_text(json.dumps({'identity': 'PRIVATE-ACCOUNT-IDENTITY', 'models': [
                {'slug': 'from-cache', 'display_name': 'Cached', 'default_reasoning_level': 'high',
                 'supported_reasoning_levels': [{'effort': 'high'}],
                 'service_tiers': [{'id': 'priority', 'name': 'Fast', 'description': '2x speed'}],
                 'model_messages': 'PRIVATE-MODEL-INSTRUCTIONS'}]}))
            result = run('catalog', home=Path(directory)/'home', codex_home=directory)
            self.assertFalse(result['connected'])
            self.assertEqual(result['models'][0]['source'], 'cache')
            self.assertEqual(result['models'][0]['fast_tier'], 'priority')
            self.assertNotIn('PRIVATE', json.dumps(result))
            outcome = run('apply', {'thread_ids': ['one', 'one'], 'effort': 'high'}, home=Path(directory)/'home')
            self.assertEqual(outcome['summary']['failed'], 1)

    def test_invalid_thread_ids_and_no_op_requests_are_rejected(self):
        for values in ([], ['../private'], [True], ['x']*1601):
            with self.assertRaises(ControlError): unique_threads(values)
        with self.assertRaises(ControlError): Controller(FakeChannel()).apply({'thread_ids':['one']})

    def test_stream_cli_emits_progress_before_final_and_keeps_private_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            params = {'thread_ids': ['one', 'two'], 'model': 'model-one', 'effort': 'max', 'speed': 'standard'}
            output = io.StringIO()
            def operation(op, request, **kwargs):
                self.assertEqual(op, 'apply'); self.assertEqual(request, params)
                callback = kwargs['progress']
                progress_event(callback, ['one', 'two'], [], 'writing', 'one')
                self.assertIn('writing', output.getvalue())
                rows = [{'thread_id': 'one', 'status': 'verified', 'verified': True},
                        {'thread_id': 'two', 'status': 'failed', 'verified': False}]
                progress_event(callback, ['one', 'two'], rows[:1], 'completed', 'one')
                progress_event(callback, ['one', 'two'], rows, 'completed', 'two')
                return {'results': rows, 'summary': {'selected': 2, 'verified': 1, 'failed': 1, 'unverified': 0}}
            stdin = io.TextIOWrapper(io.BytesIO(json.dumps(params).encode()))
            with patch.dict(os.environ, {'VIBECUE_HOME': directory}), patch('sys.stdin', stdin), \
                    patch('vibecue.codex_control.run', side_effect=operation), redirect_stdout(output):
                self.assertEqual(main(['apply', '--stream']), 0)
            lines = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([e['event'] for e in lines], ['progress', 'progress', 'progress', 'finished', 'result'])
            self.assertEqual(lines[-1]['result']['summary']['verified'], 1)
            receipt = Path(directory) / 'codex-batch-result.json'
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            stored = json.loads(receipt.read_text())
            self.assertEqual(stored['requested'], {k: params[k] for k in ['model', 'effort', 'speed']})
            self.assertEqual([row['thread_id'] for row in stored['results']], ['one', 'two'])

    def test_stream_cli_refuses_overlapping_batches_before_any_update(self):
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory) / 'codex-batch.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                output = io.StringIO()
                stdin = io.TextIOWrapper(io.BytesIO(b'{"thread_ids":["one"],"effort":"max"}'))
                with patch.dict(os.environ, {'VIBECUE_HOME': directory}), patch('sys.stdin', stdin), \
                        patch('vibecue.codex_control.run') as operation, redirect_stdout(output):
                    self.assertEqual(main(['apply', '--stream']), 1)
                operation.assert_not_called()
                self.assertIn('已有一批', json.loads(output.getvalue())['result']['error'])

    def test_module_cli_keeps_sibling_transport_errors_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'ipc').mkdir(); (root / 'ipc/ipc.sock').touch()
            environment = dict(os.environ, CODEX_HOME=directory, VIBECUE_HOME=str(root / 'state'), PYTHONDONTWRITEBYTECODE='1')
            process = subprocess.run([sys.executable, '-m', 'vibecue.codex_control', 'apply', '--stream'],
                cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True,
                input=json.dumps({'thread_ids': ['one'], 'effort': 'max', 'approvalPolicy': 'never'}).encode(), timeout=5)
            self.assertEqual(process.returncode, 1)
            frames = [json.loads(line) for line in process.stdout.splitlines()]
            self.assertEqual(frames[-1]['event'], 'result')
            self.assertIn('只允许', frames[-1]['result']['error'])
            self.assertEqual(process.stderr, b'')

    def test_native_control_is_preferred_and_never_rerouted_after_unconfirmed_submission(self):
        expected = {'results': [{'thread_id': 'one', 'status': 'unverified'}]}
        with patch('vibecue.codex_control.connections', return_value=[{'socket_path': '/legacy'}]), \
                patch('vibecue.codex_control.desktop_run', return_value=expected), \
                patch('vibecue.codex_control.socket_call') as legacy:
            self.assertIs(run('apply', {'thread_ids': ['one'], 'effort': 'max'}), expected)
        legacy.assert_not_called()

    def test_deadline_preserves_confirmed_results_and_never_sends_remaining_updates(self):
        channel=FakeChannel(); original=channel.request; current=[0]
        def request(method,params,timeout=8):
            result=original(method,params,timeout)
            if method=='thread/settings/update':current[0]=6
            return result
        channel.request=request
        with patch('vibecue.codex_control.time.monotonic',side_effect=lambda:current[0]):
            result=Controller(channel,deadline=5).apply({'thread_ids':['one','two'],'effort':'max'})
        self.assertEqual(result['summary'],{'selected':2,'verified':1,'unverified':0,'failed':1})
        updates=[params for method,params in channel.calls if method=='thread/settings/update']
        self.assertEqual([params['threadId'] for params in updates],['one'])

    def test_local_disconnect_after_batch_submission_is_unverified_not_unsent(self):
        class Client:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,value):pass
            def connect(self,path):pass
            def sendall(self,data):self.sent=data
            def makefile(self,mode):raise ConnectionResetError('connection closed after submission')
        with patch('vibecue.codex_control.socket.socket',return_value=Client()):
            result=socket_call({'socket_path':'/private/fixture'},'apply',{'thread_ids':['one','two'],'effort':'high'})
        self.assertEqual(result['summary'],{'selected':2,'verified':0,'unverified':2,'failed':0})
        self.assertTrue(all(row['status']=='unverified' for row in result['results']))


class ControlRelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.sent = []
        self.hub = ControlHub(self.sent.append, os.getpid(), home=self.home)
        self.addCleanup(self.hub.close)
        self.hub.client_forwarded(b'{"id":"desktop-init","method":"initialize","params":{}}\n')
        self.assertEqual(self.hub.server_chunk(b'{"id":"desktop-init","result":{}}\n'), b'{"id":"desktop-init","result":{}}\n')
        self.hub.client_forwarded(b'{"method":"initialized"}\n')
        self.assertTrue(self.hub.ready)

    def test_normal_bytes_survive_fragmentation_and_own_response_is_removed(self):
        ordinary = b'{"method":"item/agentMessage/delta","params":{"delta":"PRIVATE-TEXT"}}\n'
        self.assertEqual(self.hub.server_chunk(ordinary[:17]), b'')
        self.assertEqual(self.hub.server_chunk(ordinary[17:]), ordinary)
        owned = {'id': self.hub.prefix + 'late-timeout', 'result': {}}
        raw = json.dumps(owned).encode() + b'\n'
        self.assertEqual(self.hub.server_chunk(raw), b'')
        bad = b'not-json\n'
        self.assertEqual(self.hub.server_chunk(bad), bad)
        self.assertEqual(self.hub.server_buffer, b'')

    def test_settings_notifications_are_observed_but_still_forwarded(self):
        message = {'method': 'thread/settings/updated', 'params': {'threadId': 'one', 'threadSettings':
                   {'model': 'model-one', 'effort': 'max', 'serviceTier': 'priority', 'cwd':'PRIVATE-PATH'}}}
        raw = json.dumps(message).encode()+b'\n'
        self.assertEqual(self.hub.server_chunk(raw), raw)
        value, revision = self.hub.settings('one')
        self.assertEqual(revision, 1)
        self.assertNotIn('cwd', value)
        self.assertEqual(self.hub.wait_settings('one', 0, {'effort':'max'}, timeout=.01), value)
        self.assertIsNone(self.hub.wait_settings('one', 1, {'effort':'max'}, timeout=.01))

    def test_control_never_injects_inside_a_partial_desktop_json_line(self):
        request = json.dumps({'id':'desktop-call','method':'example','params':{'text':'PRIVATE-PROMPT'}}).encode()+b'\n'
        self.hub.forward_client(request[:31])
        finished = threading.Event(); result=[]
        def call():
            try: result.append(self.hub.request('thread/loaded/list', {}, timeout=1))
            finally: finished.set()
        worker=threading.Thread(target=call);worker.start()
        deadline=time.monotonic()+1
        while not self.hub.send_queue and time.monotonic()<deadline:time.sleep(.001)
        self.assertEqual(b''.join(self.sent),request[:31])
        self.hub.forward_client(request[31:])
        lines=b''.join(self.sent).splitlines()
        self.assertEqual(lines[0],request.rstrip(b'\n'))
        control=json.loads(lines[1]);self.assertEqual(control['method'],'thread/loaded/list')
        reply=json.dumps({'id':control['id'],'result':{'data':['one']}}).encode()+b'\n'
        self.assertEqual(self.hub.server_chunk(reply),b'')
        worker.join(2);self.assertTrue(finished.is_set());self.assertEqual(result,[{'data':['one']}])

    def test_unsent_queued_timeout_is_removed_and_cannot_apply_later(self):
        self.hub.forward_client(b'{"id":1,"method":"example","params":{"text":"')
        with self.assertRaises(ControlError): self.hub.request('thread/settings/update',{'threadId':'one','effort':'max'},timeout=.01)
        self.hub.forward_client(b'PRIVATE"}}\n')
        self.assertEqual(len(b''.join(self.sent).splitlines()),1)
        self.assertFalse(self.hub.send_queue)

    def test_sent_update_timeout_is_uncertain_and_late_reply_stays_private(self):
        with self.assertRaises(UnconfirmedUpdate):self.hub.request('thread/settings/update',{'threadId':'one','effort':'max'},timeout=.01)
        request=json.loads(self.sent[0])
        late=json.dumps({'id':request['id'],'result':{}}).encode()+b'\n'
        self.assertEqual(self.hub.server_chunk(late),b'')

    def test_rpc_allowlist_rejects_input_auth_resume_and_permission_changes(self):
        for method,params in [('turn/start',{}),('thread/resume',{'threadId':'one'}),('account/read',{}),
                              ('thread/read',{'threadId':'one','includeTurns':True}),
                              ('thread/settings/update',{'threadId':'one','approvalPolicy':'never'})]:
            with self.assertRaises(ControlError):self.hub.request(method,params,timeout=.01)
        self.assertEqual(self.sent,[])

    def test_expired_queued_requests_cannot_apply_after_the_client_timeout(self):
        class Client:
            def __init__(self,payload):self.payload=payload;self.output=b''
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def settimeout(self,value):pass
            def makefile(self,mode):return io.BytesIO(self.payload)
            def sendall(self,value):self.output+=value
        for deadline in [time.monotonic()-1,float('nan'),True]:
            raw=json.dumps({'operation':'apply','params':{'thread_ids':['one'],'effort':'max'},'deadline':deadline}).encode()+b'\n'
            client=Client(raw);self.hub._serve_client(client)
            self.assertIn('error',json.loads(client.output))
        self.assertEqual(self.sent,[])

    def test_oversized_desktop_frame_is_preserved_and_disables_control(self):
        raw=b'{"method":"example","params":{"text":"'+b'x'*180+b'"}}\n'
        with patch('vibecue.codex_control_relay.MAX_FRAME',80):
            result=self.hub.server_chunk(raw[:100])+self.hub.server_chunk(raw[100:])
        self.assertEqual(result,raw);self.assertFalse(self.hub.ready)

    def test_atomic_injection_rejects_oversize_without_writing_partial_json(self):
        read_fd,write_fd=os.pipe()
        try:
            size=os.fpathconf(write_fd,'PC_PIPE_BUF')
            original=fcntl.fcntl(write_fd,fcntl.F_GETFL)
            with self.assertRaises(ControlError):atomic_pipe_write(write_fd,b'x'*(size+1),time.monotonic()+1)
            self.assertEqual(fcntl.fcntl(write_fd,fcntl.F_GETFL)&os.O_NONBLOCK,original&os.O_NONBLOCK)
            payload=b'{"id":"control"}\n'
            atomic_pipe_write(write_fd,payload,time.monotonic()+1)
            self.assertEqual(os.read(read_fd,len(payload)),payload)
            self.assertEqual(fcntl.fcntl(write_fd,fcntl.F_GETFL)&os.O_NONBLOCK,original&os.O_NONBLOCK)
        finally:
            os.close(read_fd);os.close(write_fd)

    def test_private_socket_roundtrip_and_health_discovery(self):
        def backend(data):
            self.sent.append(data);request=json.loads(data)
            answer={'data':[model()]} if request['method']=='model/list' else {}
            self.assertEqual(self.hub.server_chunk(json.dumps({'id':request['id'],'result':answer}).encode()+b'\n'),b'')
        self.hub.write_backend=backend
        try:self.hub.start()
        except PermissionError:self.skipTest('sandbox does not allow a local Unix socket')
        self.hub._health()
        found=connections(self.home);self.assertEqual(len(found),1)
        self.assertEqual(self.hub.socket_path.stat().st_mode&0o777,0o600)
        self.assertEqual(self.hub.health_path.stat().st_mode&0o777,0o600)
        result=run('catalog',home=self.home);self.assertTrue(result['connected'])
        self.assertEqual(result['models'][0]['id'],'model-one')
        self.assertNotIn('PRIVATE',self.hub.health_path.read_text())
        health=json.loads(self.hub.health_path.read_text());health['boot_time']-=10000
        self.hub.health_path.write_text(json.dumps(health));self.assertEqual(connections(self.home),[])
        self.hub._health();health=json.loads(self.hub.health_path.read_text());health['session_id']='invalid'
        self.hub.health_path.write_text(json.dumps(health));self.assertEqual(connections(self.home),[])
        self.hub.close();self.assertEqual(connections(self.home),[])


if __name__ == '__main__':
    unittest.main()
