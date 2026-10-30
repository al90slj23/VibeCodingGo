import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import background_settings as background, desktop_control
from vibecue.codex_control import ControlError, UnconfirmedUpdate

THREAD = '01a161a5-8199-7d92-bc36-d9102e31cc01'


class FakeWorker:
    settings = {}
    disk = {}
    instances = []
    writes = []
    fail_write = False
    mismatch = False

    def __init__(self, *args):
        self.settings = type(self).disk.copy()
        self.index = len(type(self).instances)
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        type(self).disk = self.settings.copy()

    def load(self, identifier):
        self.indexed_settings = {'model': self.settings['model'],
                                 'effort': 'high' if self.mismatch else self.settings.get('reasoningEffort')}
        return self.settings.copy()

    def rpc(self, method, params):
        if method == 'thread/settings/update':
            type(self).writes.append(dict(params))
            if type(self).fail_write:
                raise UnconfirmedUpdate('lost confirmation')
            self.settings.update({k: v for k, v in params.items() if k != 'threadId'})
            self.settings['reasoningEffort'] = self.settings.pop('effort', self.settings.get('reasoningEffort'))
            return {}
        if method == 'thread/read':
            return {'thread': {'id': THREAD, 'status': {'type': 'idle'},
                               'model': self.settings['model'],
                               'reasoningEffort': 'high' if self.mismatch else self.settings.get('reasoningEffort')}}
        raise AssertionError('Forbidden native method: ' + method)


class BackgroundSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / 'models_cache.json').write_text(json.dumps({'models': [
            {'slug': 'model-one', 'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'max'}],
             'service_tiers': [{'id': 'priority'}]},
            {'slug': 'model-two', 'supported_reasoning_levels': [{'effort': 'high'}]}]}))
        FakeWorker.disk = {'model': 'model-one', 'reasoningEffort': 'high', 'serviceTier': 'priority'}
        FakeWorker.instances = []; FakeWorker.writes = []; FakeWorker.fail_write = False; FakeWorker.mismatch = False
        for target, replacement in [('MetadataWorker', FakeWorker), ('native_binary', lambda: Path('/fixture/codex')),
                                    ('check_local_record', lambda *args: None),
                                    ('require_contract', lambda *args: None)]:
            mocked = patch('vibecue.background_settings.' + target, replacement)
            mocked.start(); self.addCleanup(mocked.stop)

    def call(self, params=None):
        return background.run(THREAD, params, home=self.home, codex_home=self.home)

    def test_unloaded_settings_are_verified_from_a_fresh_process_after_close(self):
        result = self.call({'effort': 'max', 'speed': 'standard'})
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['verification_basis'], 'persisted_settings')
        self.assertFalse(result['loaded']); self.assertNotIn('composer_settings', result)
        self.assertEqual(len(FakeWorker.instances), 2)
        self.assertEqual(FakeWorker.writes, [{'threadId': THREAD, 'effort': 'max', 'serviceTier': 'default'}])
        self.assertEqual(result['settings'], {'model': 'model-one', 'effort': 'max', 'service_tier': 'default'})

    def test_keep_does_not_overwrite_model_or_speed(self):
        result = self.call({'effort': 'max'})
        self.assertEqual(FakeWorker.writes[0], {'threadId': THREAD, 'effort': 'max'})
        self.assertEqual(result['settings']['service_tier'], 'priority')

    def test_no_input_permissions_working_directory_or_active_turn_is_allowed(self):
        for key in ('input', 'approvalPolicy', 'activeTurnId', 'cwd', 'modelProvider', 'config', 'collaborationMode'):
            with self.assertRaises(ControlError):
                self.call({'effort': 'max', key: 'forbidden'})
        self.assertEqual(FakeWorker.instances, [])

    def test_mismatched_persistent_index_is_unverified(self):
        FakeWorker.mismatch = True
        self.assertEqual(self.call({'effort': 'max'})['status'], 'unverified')
        self.assertEqual(len(FakeWorker.writes), 1)

    def test_uncertain_write_retains_exact_expected_patch_and_never_retries(self):
        FakeWorker.fail_write = True
        with self.assertRaises(background.BackgroundUpdateUnconfirmed) as error:
            self.call({'effort': 'max', 'speed': 'standard'})
        self.assertEqual(error.exception.patch, {'effort': 'max', 'serviceTier': 'default'})
        self.assertEqual(len(FakeWorker.writes), 1)
        self.assertEqual(len(FakeWorker.instances), 1)

    def test_inspection_does_not_send_a_settings_update(self):
        result = self.call()
        self.assertEqual(result['status'], 'read'); self.assertTrue(result['verified'])
        self.assertEqual(FakeWorker.writes, [])

    def test_busy_native_writer_is_left_completely_untouched(self):
        folder = self.home / 'thread-writer-locks'; folder.mkdir()
        with (folder / (THREAD + '.lock')).open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(background.BackgroundUnavailable): self.call({'effort': 'max'})
            with self.assertRaises(BlockingIOError):
                with (folder / (THREAD + '.lock')).open('r+') as another:
                    fcntl.flock(another, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(FakeWorker.instances, [])

    def test_symlink_writer_lock_cannot_admit_a_worker(self):
        folder = self.home / 'thread-writer-locks'; folder.mkdir()
        target = self.home / 'other'; target.touch()
        (folder / (THREAD + '.lock')).symlink_to(target)
        with self.assertRaises(background.BackgroundUnavailable): self.call({'effort': 'max'})
        self.assertEqual(FakeWorker.instances, [])

    def test_only_one_background_worker_is_admitted(self):
        with background.worker_lease(self.home):
            with self.assertRaises(background.BackgroundUnavailable): self.call({'effort': 'max'})
        self.assertEqual(FakeWorker.instances, [])

    def test_unknown_model_effort_and_speed_never_write(self):
        for params in [{'model': 'unknown'}, {'effort': 'unknown'}, {'model': 'model-two', 'speed': 'fast'}]:
            with self.assertRaises(ControlError): self.call(params)
        self.assertEqual(FakeWorker.writes, [])

    def test_native_rpc_refuses_task_commands_and_history_requests_before_sending(self):
        worker = background.MetadataWorker.__new__(background.MetadataWorker)
        # MetadataWorker is mocked for high-level tests; use the original below.
        for method in ('turn/start', 'turn/steer', 'turn/interrupt', 'thread/stop', 'account/login/start'):
            with self.assertRaises(ControlError): OriginalWorker.rpc(worker, method, {})
        for method, params in [('thread/read', {'threadId': THREAD, 'includeTurns': True}),
                               ('thread/resume', {'threadId': THREAD, 'excludeTurns': False}),
                               ('thread/settings/update', {'threadId': THREAD, 'approvalPolicy': 'never'})]:
            with self.assertRaises(ControlError): OriginalWorker.rpc(worker, method, params)

    def test_archived_record_is_rejected_without_creating_a_worker(self):
        import sqlite3
        with sqlite3.connect(self.home / 'state_5.sqlite') as database:
            database.execute('CREATE TABLE threads (id TEXT, archived INTEGER)')
            database.execute('INSERT INTO threads VALUES (?,1)', (THREAD,))
        with patch('vibecue.background_settings.check_local_record', OriginalRecordCheck):
            with self.assertRaises(ControlError): self.call({'effort': 'max'})
        self.assertEqual(FakeWorker.instances, [])


OriginalWorker = background.MetadataWorker
OriginalRecordCheck = background.check_local_record


@unittest.skipUnless(os.environ.get('VIBECUE_TEST_NATIVE_CODEX') == '1', 'explicit installed native CLI test')
class InstalledNativeSettingsTests(unittest.TestCase):
    def test_native_persistence_without_turns_and_native_writer_refusal(self):
        import datetime
        import sqlite3
        with tempfile.TemporaryDirectory(prefix='vcgo-native-settings-test-') as directory:
            home = Path(directory); binary = background.native_binary()
            with OriginalWorker(binary, home, time.monotonic() + 15):
                pass
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')
            rollout = home / 'sessions/2026/10/31' / ('rollout-' + THREAD + '.jsonl')
            rollout.parent.mkdir(parents=True)
            items = [{'type': 'session_meta', 'payload': {'id': THREAD, 'timestamp': stamp, 'cwd': directory,
                      'originator': 'isolated-test', 'cli_version': 'test', 'source': 'vscode', 'model_provider': 'openai'}},
                     {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                      'content': [{'type': 'input_text', 'text': 'Synthetic fixture, never sent to a model.'}]}},
                     {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                      'content': [{'type': 'output_text', 'text': 'Fixture complete.'}]}}]
            rollout.write_text(''.join(json.dumps(dict(timestamp=stamp, **item)) + '\n' for item in items))
            database = sqlite3.connect(home / 'state_5.sqlite')
            row = {'id': THREAD, 'rollout_path': str(rollout), 'created_at': int(time.time()), 'updated_at': int(time.time()),
                   'source': 'vscode', 'model_provider': 'openai', 'cwd': directory, 'title': 'Isolated fixture',
                   'sandbox_policy': '{"type":"read-only"}', 'approval_mode': 'on-request', 'cli_version': 'test',
                   'first_user_message': 'Fixture', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high',
                   'history_mode': 'legacy', 'has_user_event': 1}
            database.execute('INSERT INTO threads (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', list(row.values()))
            database.commit()
            (home / 'models_cache.json').write_text(json.dumps({'models': [{'slug': 'gpt-6.1-sol',
                'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'max'}], 'service_tiers': [{'id': 'priority'}]}]}))
            result = background.run(THREAD, {'effort': 'max', 'speed': 'standard'}, home=home, codex_home=home)
            self.assertEqual(result['status'], 'verified', result)
            self.assertEqual(result['settings']['effort'], 'max')
            self.assertEqual(result['settings']['service_tier'], 'default')
            with OriginalWorker(binary, home, time.monotonic() + 15) as owner:
                state = owner.load(THREAD)
                self.assertEqual(state['thread']['status']['type'], 'idle')
                with self.assertRaises(background.BackgroundUnavailable):
                    background.run(THREAD, {'effort': 'high'}, home=home, codex_home=home)
                with OriginalWorker(binary, home, time.monotonic() + 15) as contender:
                    with self.assertRaises(background.BackgroundUnavailable):
                        contender.rpc('thread/resume', {'threadId': THREAD, 'excludeTurns': True})
                state = owner.rpc('thread/read', {'threadId': THREAD, 'includeTurns': False})['thread']
                self.assertEqual(state['reasoningEffort'], 'max')
                self.assertEqual(state['status']['type'], 'idle')
            row = database.execute('select model,reasoning_effort,sandbox_policy,approval_mode,tokens_used from threads where id=?', (THREAD,)).fetchone()
            self.assertEqual(row[:2], ('gpt-6.1-sol', 'max'))
            # Codex canonicalizes legacy read-only to a managed profile. Verify
            # effective access rather than comparing those two encodings.
            policy = json.loads(row[2])
            if policy['type'] == 'managed':
                self.assertEqual(policy['network'], 'restricted')
                self.assertTrue(all(entry['access'] == 'read' for entry in policy['file_system']['entries']))
            else:
                self.assertEqual(policy['type'], 'read-only')
            self.assertEqual(row[3:], ('on-request', 0))
            database.close()
            types = [json.loads(line).get('payload', {}).get('type') for line in rollout.read_text().splitlines()]
            self.assertNotIn('task_started', types); self.assertNotIn('task_complete', types)
