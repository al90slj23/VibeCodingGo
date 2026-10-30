import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibecue import codex_control, desktop_control
from vibecue.codex_control import ControlError, UnconfirmedUpdate
from vibecue.desktop_control import DesktopControlChannel
from vibecue.codex_desktop_ipc import DesktopChannel, KNOWN_PROTOCOL


class VerifiedContractFixture:
    protocol_contract = dict(KNOWN_PROTOCOL)
    supports = DesktopChannel.supports
    require_method = DesktopChannel.require_method
    require_capability = DesktopChannel.require_capability
    capabilities = DesktopChannel.capabilities


class FakeChannel(VerifiedContractFixture):
    rows = {}
    updates = []
    reads = []
    fail = None
    applied = True
    verify = True
    saved_override = None
    composer_override = None
    entered = []

    def __init__(self, thread_id, **kwargs):
        self.thread_id = thread_id

    def __enter__(self):
        self.entered.append(self.thread_id)
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        pass

    def read_settings(self):
        self.reads.append(self.thread_id)
        return self.rows.get(self.thread_id, {}).copy()

    def update_settings(self, values):
        self.updates.append((self.thread_id, values.copy()))
        if self.fail:
            raise self.fail
        if self.applied and self.verify:
            self.rows[self.thread_id].update({'reasoningEffort' if key == 'effort' else key: value
                                             for key, value in values.items()})
        return self.applied

    def runtime_snapshot(self):
        row = self.rows.get(self.thread_id, {})
        return {'thread_id': self.thread_id, 'resume_state': row.get('resumeState'),
                'thread_settings': self.saved_override if self.saved_override is not None else row.copy(),
                'composer_settings': self.composer_override if self.composer_override is not None else
                    {'model': row.get('model'), 'effort': row.get('reasoningEffort')}}


class DesktopControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        environment = patch.dict(os.environ, {'VIBECUE_HOME': str(self.home)})
        environment.start(); self.addCleanup(environment.stop)
        (self.home / 'ipc').mkdir(); (self.home / 'ipc/ipc.sock').touch()
        (self.home / 'models_cache.json').write_text(json.dumps({'models': [
            {'slug': 'model-one', 'display_name': 'One', 'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'max'}],
             'default_reasoning_level': 'high', 'service_tiers': [{'id': 'priority'}]},
            {'slug': 'model-two', 'supported_reasoning_levels': [{'effort': 'high'}]}]}))
        FakeChannel.rows = {'one': {'model': 'model-one', 'reasoningEffort': 'high', 'resumeState': 'resumed'},
                            'two': {'model': 'model-one', 'reasoningEffort': 'high', 'resumeState': 'resumed'}}
        FakeChannel.updates = []; FakeChannel.reads = []; FakeChannel.entered = []
        FakeChannel.protocol_contract = dict(KNOWN_PROTOCOL)
        FakeChannel.fail = None; FakeChannel.applied = True; FakeChannel.verify = True
        FakeChannel.saved_override = None; FakeChannel.composer_override = None
        helper = patch('vibecue.desktop_control.DesktopControlChannel', FakeChannel)
        helper.start(); self.addCleanup(helper.stop)

    def call(self, operation, params=None):
        return desktop_control.run(operation, params, codex_home=self.home)

    def test_catalog_does_not_claim_control_when_update_contract_changed(self):
        FakeChannel.protocol_contract['thread-follower-update-thread-settings'] = 3
        result = self.call('catalog')
        self.assertFalse(result['connected'])
        self.assertEqual(FakeChannel.updates, [])
        self.assertEqual(FakeChannel.reads, [])

    def test_apply_checks_changed_contract_before_submitting_or_marking_uncertain(self):
        FakeChannel.protocol_contract['thread-follower-update-thread-settings'] = 3
        result = self.call('apply', {'thread_ids':['one'], 'effort':'max'})
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.assertEqual(FakeChannel.updates, [])
        self.assertEqual(self.call('inspect', {'thread_ids':['one']})['results'][0]['status'], 'read')

    def test_speed_change_never_submits_when_readback_stream_is_incompatible(self):
        FakeChannel.protocol_contract['thread-stream-state-changed'] = 12
        result = self.call('apply', {'thread_ids':['one'], 'speed':'standard'})
        self.assertEqual(result['results'][0]['status'], 'failed')
        self.assertEqual(FakeChannel.updates, [])
        plain = self.call('apply', {'thread_ids':['one'], 'effort':'max'})
        self.assertEqual(plain['results'][0]['status'], 'failed')
        self.assertEqual(FakeChannel.updates, [])

    def test_catalog_auto_connects_without_loading_or_discovering_a_chat(self):
        result = self.call('catalog')
        self.assertTrue(result['connected'])
        self.assertEqual(result['transport'], 'desktop_ipc')
        self.assertEqual(result['models'][0]['source'], 'cache')
        self.assertIn('缓存', result['message'])
        self.assertEqual(FakeChannel.entered, ['vibecue-control-probe'])
        self.assertEqual(FakeChannel.reads, []); self.assertEqual(FakeChannel.updates, [])

    def test_inspect_returns_only_settings_from_existing_owner(self):
        FakeChannel.rows['one']['private'] = 'PRIVATE BODY OR TOKEN'
        result = self.call('inspect', {'thread_ids': ['one', 'one', 'two']})
        self.assertEqual(len(result['results']), 2)
        self.assertTrue(all(row['loaded'] for row in result['results']))
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(FakeChannel.updates, [])

    def test_apply_deduplicates_and_changes_only_selected_settings(self):
        result = self.call('apply', {'thread_ids': ['one', 'one', 'two'], 'effort': 'max', 'speed': 'fast'})
        self.assertEqual(result['summary'], {'selected': 2, 'verified': 2, 'unverified': 0, 'failed': 0})
        self.assertEqual(FakeChannel.updates, [('one', {'effort': 'max', 'serviceTier': 'priority'}),
                                              ('two', {'effort': 'max', 'serviceTier': 'priority'})])

    def test_no_permission_input_path_or_active_turn_override_is_allowed(self):
        for patch_value in ({'approvalPolicy': 'never'}, {'activeTurnId': 'active'}, {'input': 'hello'}, {'cwd': '/private'}):
            with self.assertRaises(ControlError):
                self.call('apply', {'thread_ids': ['one'], 'effort': 'high', **patch_value})
        self.assertEqual(FakeChannel.entered, []); self.assertEqual(FakeChannel.updates, [])

    def test_unresumed_unknown_model_or_effort_is_not_changed(self):
        FakeChannel.rows['one']['resumeState'] = 'needs-resume'
        self.assertEqual(self.call('apply', {'thread_ids': ['one'], 'effort': 'high'})['summary']['failed'], 1)
        FakeChannel.rows['one']['resumeState'] = 'resumed'
        for params in ({'model': 'unknown'}, {'effort': 'not-supported'}, {'model': 'model-two', 'speed': 'fast'}):
            self.assertEqual(self.call('apply', {'thread_ids': ['one'], **params})['summary']['failed'], 1)
        self.assertEqual(FakeChannel.updates, [])

    def test_keep_omits_tier_and_standard_is_explicit(self):
        self.assertEqual(self.call('apply', {'thread_ids': ['one'], 'effort': 'high'})['summary']['verified'], 1)
        self.assertEqual(FakeChannel.updates[-1][1], {'effort': 'high'})
        self.assertEqual(self.call('apply', {'thread_ids': ['one'], 'speed': 'standard'})['summary']['verified'], 1)
        self.assertEqual(FakeChannel.updates[-1][1], {'serviceTier': 'default'})

    def test_submitted_timeout_is_unverified_and_is_not_retried(self):
        FakeChannel.fail = UnconfirmedUpdate('timeout')
        result = self.call('apply', {'thread_ids': ['one'], 'effort': 'max'})
        self.assertEqual(result['summary']['unverified'], 1)
        self.assertEqual(len(FakeChannel.updates), 1)
        self.assertEqual(FakeChannel.entered, ['one'])

    def test_readback_mismatch_is_unverified_not_success(self):
        FakeChannel.verify = False
        result = self.call('apply', {'thread_ids': ['one'], 'effort': 'max'})
        self.assertEqual(result['summary']['unverified'], 1)
        self.assertFalse(result['results'][0]['verified'])
        self.assertEqual(len(FakeChannel.updates), 1)

    def test_saved_echo_cannot_hide_native_readback_mismatch(self):
        FakeChannel.verify = False
        FakeChannel.saved_override = {'model': 'model-one', 'effort': 'max', 'serviceTier': 'default'}
        result = self.call('apply', {'thread_ids': ['one'], 'effort': 'max', 'speed': 'standard'})
        row = result['results'][0]
        self.assertEqual(row['status'], 'unverified')
        self.assertEqual(row['settings']['effort'], 'high')
        self.assertEqual(row['saved_settings']['effort'], 'max')

    def test_composer_must_match_even_when_saved_and_dedicated_settings_match(self):
        FakeChannel.composer_override = {'model': 'model-one', 'effort': 'high'}
        result = self.call('apply', {'thread_ids': ['one'], 'effort': 'max'})
        self.assertEqual(result['results'][0]['status'], 'unverified')
        self.assertEqual(result['results'][0]['composer_settings']['effort'], 'high')

    def test_progress_precedes_writes_and_emits_each_result_before_next_item(self):
        events = []
        def progress(event):
            events.append((event.copy(), len(FakeChannel.updates)))
        answer = desktop_control.run('apply', {'thread_ids': ['one', 'one', 'two'], 'effort': 'max'},
                                     codex_home=self.home, progress=progress)
        self.assertEqual(answer['summary']['verified'], 2)
        writes = [(event, count) for event, count in events if event['phase'] == 'writing']
        self.assertEqual([count for _, count in writes], [0, 1])
        completed = [event for event, _ in events if event['phase'] == 'completed']
        self.assertEqual([event['completed'] for event in completed], [1, 2])
        self.assertEqual([event['result']['thread_id'] for event in completed], ['one', 'two'])
        self.assertTrue(all(event['total'] == 2 for event, _ in events))

    def test_inspect_has_native_composer_and_tier_without_any_mutation(self):
        FakeChannel.rows['one']['serviceTier'] = 'priority'
        result = self.call('inspect', {'thread_ids': ['one']})['results'][0]
        self.assertEqual(result['composer_settings'], {'model': 'model-one', 'effort': 'high'})
        self.assertEqual(result['settings']['service_tier'], 'priority')
        self.assertEqual(FakeChannel.updates, [])

    def test_unready_items_have_actionable_failure_and_progress_is_not_lost(self):
        FakeChannel.rows['one']['resumeState'] = 'needs-resume'
        events = []
        result = desktop_control.run('apply', {'thread_ids': ['one', 'two'], 'effort': 'max'},
                                     codex_home=self.home, progress=events.append)
        self.assertEqual(result['results'][0]['error_kind'], 'not_loaded')
        self.assertEqual(result['summary'], {'selected': 2, 'verified': 1, 'failed': 1, 'unverified': 0})
        self.assertEqual([e['completed'] for e in events if e['phase'] == 'completed'], [1, 2])
        self.assertEqual([id for id, _ in FakeChannel.updates], ['two'])

    def test_unloaded_items_can_be_queued_without_navigation_or_updates(self):
        FakeChannel.rows['one']['resumeState'] = 'needs-resume'
        events = []
        result = desktop_control.run('apply', {'thread_ids': ['one'], 'effort': 'max', 'defer_unloaded': True},
                                     home=self.home, codex_home=self.home, progress=events.append)
        self.assertEqual(result['summary']['queued'], 1)
        self.assertEqual(result['summary']['verified'], 0)
        self.assertEqual(FakeChannel.updates, [])
        self.assertFalse(any(event['phase'] == 'opening' for event in events))
        stored = json.loads((self.home / 'codex-model-queue.json').read_text())
        self.assertEqual(stored['records'][0]['params'], {'effort': 'max'})

    def test_auto_open_flags_are_rejected_before_connecting(self):
        with self.assertRaises(ControlError):
            self.call('apply', {'thread_ids': ['one'], 'effort': 'max', 'open_unloaded': True})
        for invalid in ['yes', 1, None]:
            with self.assertRaises(ControlError):
                self.call('apply', {'thread_ids': ['one'], 'effort': 'max', 'defer_unloaded': invalid})
        self.assertEqual(FakeChannel.entered, [])

    def test_declined_update_is_failed_and_never_retried(self):
        FakeChannel.applied = False
        result = self.call('apply', {'thread_ids': ['one'], 'effort': 'max'})
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(len(FakeChannel.updates), 1)

    def test_deadline_prevents_remaining_mutations(self):
        with patch('vibecue.desktop_control.time.monotonic', return_value=100):
            result = desktop_control.run('apply', {'thread_ids': ['one'], 'effort': 'max'}, codex_home=self.home, deadline=99)
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(FakeChannel.entered, []); self.assertEqual(FakeChannel.updates, [])

    def test_absent_socket_returns_none_without_initialization(self):
        (self.home / 'ipc/ipc.sock').unlink()
        self.assertIsNone(self.call('catalog'))
        self.assertEqual(FakeChannel.entered, [])

    def test_control_run_automatically_uses_desktop_when_stdio_unavailable(self):
        expected = {'connected': True, 'transport': 'desktop_ipc'}
        with patch('vibecue.codex_control.connections', return_value=[]), \
                patch('vibecue.codex_control.desktop_run', return_value=expected) as desktop:
            self.assertIs(codex_control.run('catalog', codex_home=self.home), expected)
            self.assertIs(codex_control.run('apply', {'thread_ids': ['one'], 'effort': 'max'}, codex_home=self.home), expected)
        self.assertEqual(desktop.call_count, 2)


class DesktopControlProtocolTests(unittest.TestCase):
    def test_update_only_next_turn_settings_has_exact_owner_payload(self):
        channel = DesktopControlChannel('one'); channel.owner = 'owner-id'
        with patch.object(channel, '_call', return_value={'applied': True}) as call:
            self.assertTrue(channel.update_settings({'model': 'model-one', 'effort': 'high', 'serviceTier': 'priority'}))
        call.assert_called_once_with('thread-follower-update-thread-settings',
                                    {'conversationId': 'one', 'threadSettings': {'model': 'model-one', 'effort': 'high', 'serviceTier': 'priority'}},
                                    target='owner-id', mutation=True)

    def test_update_rejects_permission_fields_and_malformed_ack(self):
        channel = DesktopControlChannel('one'); channel.owner = 'owner-id'
        with patch.object(channel, '_call', return_value={'applied': 'yes'}) as call:
            for values in ({'approvalPolicy': 'never'}, {'serviceTier': 'unavailable'}, {'effort': None}, {}):
                with self.assertRaises(ControlError): channel.update_settings(values)
            call.assert_not_called()
            with self.assertRaises(UnconfirmedUpdate): channel.update_settings({'effort': 'high'})
        self.assertEqual(set(channel.method_versions), {'initialize', 'thread-owner-discovery',
                                                       'thread-follower-read-model-settings', 'thread-follower-update-thread-settings'})


if __name__ == '__main__':
    unittest.main()
