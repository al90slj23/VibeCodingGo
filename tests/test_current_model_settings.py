import json
import unittest

from vibecue.codex_control import ControlError
from vibecue.current_model_settings import ModelSettingsProjection, unavailable_settings


class ModelSettingsProjectionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.projection = ModelSettingsProjection('thread-one', 'owner-one', clock=lambda: self.now)

    def snapshot(self, **state):
        self.projection.apply({'type': 'snapshot', 'revision': 10,
                               'conversationState': {'id': 'thread-one', 'resumeState': 'resumed', **state}})
        return self.projection.snapshot()

    def patches(self, *patches):
        self.projection.apply({'type': 'patches', 'baseRevision': self.projection.revision,
                               'revision': self.projection.revision + 1, 'patches': list(patches)})
        return self.projection.snapshot()

    def test_owner_snapshot_exposes_only_current_setting_metadata(self):
        result = self.snapshot(latestModel='runtime-model', latestReasoningEffort='low',
            latestThreadSettings={'model': 'selected-model', 'effort': 'ultra', 'serviceTier': 'priority',
                                  'cwd': '/PRIVATE', 'approvalPolicy': 'PRIVATE', 'token': 'PRIVATE'},
            turns=[{'items': [{'text': 'PRIVATE BODY'}]}], requests=[{'params': {'secret': 'PRIVATE'}}])
        self.assertEqual((result['model'], result['reasoning_effort'], result['service_tier']),
                         ('selected-model', 'ultra', 'priority'))
        self.assertEqual(result['status'], 'current')
        self.assertEqual(result['thread_id'], 'thread-one')
        self.assertEqual(result['owner_client_id'], 'owner-one')
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('PRIVATE', repr(vars(self.projection)))

    def test_next_turn_selection_is_not_overwritten_by_running_turn_runtime(self):
        self.snapshot(latestModel='old-runtime', latestReasoningEffort='low',
                      latestCollaborationMode={'settings': {'model': 'old-runtime', 'reasoning_effort': 'low'}},
                      latestThreadSettings={'model': 'next-model', 'effort': 'ultra', 'serviceTier': 'default'})
        result = self.patches({'op': 'replace', 'path': ['latestModel'], 'value': 'old-model'},
            {'op': 'replace', 'path': ['latestReasoningEffort'], 'value': 'minimal'},
            {'op': 'replace', 'path': ['latestCollaborationMode'], 'value': {'settings': {'model': 'old-model', 'reasoning_effort': 'minimal'}}})
        self.assertEqual((result['model'], result['reasoning_effort']), ('next-model', 'ultra'))

    def test_settings_patch_updates_without_new_turn_context_or_dedicated_read(self):
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'default'})
        self.now += 2
        result = self.patches({'op': 'replace', 'path': ['latestThreadSettings', 'model'], 'value': 'two'},
            {'op': 'replace', 'path': ['latestThreadSettings', 'effort'], 'value': 'max'},
            {'op': 'replace', 'path': ['latestThreadSettings', 'serviceTier'], 'value': 'fast'})
        self.assertEqual((result['model'], result['reasoning_effort'], result['service_tier']), ('two', 'max', 'fast'))
        self.assertEqual(result['observed_at'], self.now)

    def test_explicit_null_effort_follows_default_not_old_runtime(self):
        result = self.snapshot(latestReasoningEffort='high',
            latestThreadSettings={'model': 'one', 'effort': None, 'serviceTier': 'default'})
        self.assertEqual(result['reasoning_effort'], 'default')
        self.assertEqual(result['status'], 'current')

    def test_null_tier_follows_default_without_claiming_standard_or_fast(self):
        result = self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': None})
        self.assertIsNone(result['service_tier'])
        self.assertEqual(result['speed_status'], 'default')
        self.assertEqual(result['status'], 'partial')

    def test_missing_tier_is_unknown_not_inherited(self):
        result = self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high'})
        self.assertIsNone(result['service_tier'])
        self.assertEqual(result['speed_status'], 'unknown')
        self.assertEqual(result['status'], 'partial')

    def test_explicit_standard_tier_remains_exact(self):
        for tier in ('default', 'standard', 'priority', 'fast', 'ultrafast'):
            with self.subTest(tier=tier):
                result = self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': tier})
                self.assertEqual(result['service_tier'], tier)
                self.assertEqual(result['speed_status'], 'explicit')

    def test_runtime_collaboration_only_fills_absent_selections(self):
        result = self.snapshot(latestModel='old', latestReasoningEffort='low',
            latestCollaborationMode={'settings': {'model': 'collab', 'reasoning_effort': 'high', 'developer_instructions': 'PRIVATE'}})
        self.assertEqual((result['model'], result['reasoning_effort']), ('collab', 'high'))
        self.assertEqual(result['status'], 'partial')
        self.assertNotIn('PRIVATE', repr(vars(self.projection)))

    def test_thread_collaboration_fills_missing_direct_selection(self):
        result = self.snapshot(latestModel='old', latestReasoningEffort='low',
            latestCollaborationMode={'settings': {'model': 'runtime', 'reasoning_effort': 'low'}},
            latestThreadSettings={'collaborationMode': {'settings': {'model': 'selected', 'reasoning_effort': 'max'}}, 'serviceTier': 'priority'})
        self.assertEqual((result['model'], result['reasoning_effort']), ('selected', 'max'))

    def test_nested_collaboration_patches_do_not_retain_instructions(self):
        self.snapshot(latestCollaborationMode={'settings': {'model': 'old', 'reasoning_effort': 'low'}})
        result = self.patches({'op': 'replace', 'path': ['latestCollaborationMode', 'settings', 'model'], 'value': 'new'},
            {'op': 'replace', 'path': ['latestCollaborationMode', 'settings', 'reasoning_effort'], 'value': 'ultra'},
            {'op': 'replace', 'path': ['latestCollaborationMode', 'settings', 'developer_instructions'], 'value': 'PRIVATE'})
        self.assertEqual((result['model'], result['reasoning_effort']), ('new', 'ultra'))
        self.assertNotIn('PRIVATE', repr(vars(self.projection)))

    def test_whole_thread_setting_replacement_drops_removed_values(self):
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'priority'})
        result = self.patches({'op': 'replace', 'path': ['latestThreadSettings'], 'value': {'model': 'two'}})
        self.assertEqual(result['model'], 'two')
        self.assertIsNone(result['reasoning_effort'])
        self.assertIsNone(result['service_tier'])

    def test_removing_tier_never_keeps_old_lightning_state(self):
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'priority'})
        result = self.patches({'op': 'remove', 'path': ['latestThreadSettings', 'serviceTier']})
        self.assertIsNone(result['service_tier'])
        self.assertEqual(result['speed_status'], 'unknown')

    def test_malformed_primary_fields_do_not_restore_old_runtime_settings(self):
        result = self.snapshot(latestModel='old', latestReasoningEffort='high',
                              latestThreadSettings={'model': {'text': 'PRIVATE'}, 'effort': 123, 'serviceTier': ['priority']})
        self.assertIsNone(result['model']); self.assertIsNone(result['reasoning_effort'])
        self.assertIsNone(result['service_tier'])
        self.assertNotIn('PRIVATE', repr(vars(self.projection)))

    def test_control_characters_and_overlong_values_are_discarded(self):
        result = self.snapshot(latestThreadSettings={'model': 'x\nPRIVATE', 'effort': 'x' * 100, 'serviceTier': 'fast\x00PRIVATE'})
        self.assertIsNone(result['model']); self.assertIsNone(result['reasoning_effort'])
        self.assertIsNone(result['service_tier'])

    def test_unrelated_streaming_patch_updates_validity_without_claiming_setting_change(self):
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'default'})
        observed = self.projection.observed_at
        self.now += 10
        result = self.patches({'op': 'replace', 'path': ['turns', 0, 'items', 0, 'text'], 'value': 'PRIVATE STREAM'})
        self.assertEqual(result['observed_at'], observed)
        self.assertEqual(result['verified_at'], self.now)
        self.assertNotIn('PRIVATE', repr(vars(self.projection)))

    def test_live_idle_subscription_refreshes_validity_without_changing_observed_time(self):
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'default'})
        self.now += 50
        self.assertEqual(self.projection.snapshot()['status'], 'unavailable')
        self.projection.mark_live()
        result = self.projection.snapshot()
        self.assertEqual(result['status'], 'current')
        self.assertEqual(result['observed_at'], 1000)
        self.assertEqual(result['expires_at'], 1080)

    def test_expired_snapshot_contains_no_old_current_values(self):
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'priority'})
        self.now += 31
        result = self.projection.snapshot()
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn('model', result); self.assertNotIn('service_tier', result)

    def test_invalidation_cannot_be_revived_by_heartbeat(self):
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'priority'})
        self.projection.invalidate(); self.projection.mark_live()
        self.assertEqual(self.projection.snapshot()['status'], 'unavailable')
        replacement = ModelSettingsProjection('thread-one', 'owner-two', clock=lambda: self.now)
        replacement.mark_live()
        self.assertEqual(replacement.snapshot()['status'], 'unavailable')

    def test_wrong_thread_and_revision_gaps_are_rejected(self):
        with self.assertRaises(ControlError):
            self.snapshot(id='wrong-thread')
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high'})
        for revision in (True, -1, '12'):
            with self.subTest(revision=revision), self.assertRaises(ControlError):
                self.projection.apply({'type': 'patches', 'baseRevision': 10, 'revision': revision, 'patches': []})
        with self.assertRaises(ControlError):
            self.projection.apply({'type': 'patches', 'baseRevision': 8, 'revision': 12, 'patches': []})
        self.assertEqual(self.projection.revision, 10)

    def test_invalid_patch_batch_is_atomic(self):
        before = self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'default'})
        with self.assertRaises(ControlError):
            self.patches({'op': 'replace', 'path': ['latestThreadSettings', 'effort'], 'value': 'max'},
                         {'op': 'replace', 'path': [], 'value': {'body': 'PRIVATE'}})
        self.assertEqual(self.projection.snapshot(), before)

    def test_empty_and_disconnected_settings_are_explicitly_unavailable(self):
        self.assertEqual(self.projection.snapshot()['status'], 'unavailable')
        self.assertEqual(self.snapshot(requests=[])['status'], 'unavailable')
        self.assertEqual(unavailable_settings(), {'status': 'unavailable', 'source': 'codex_desktop', 'reason': 'unavailable', 'message': 'unavailable'})
        self.assertIn('尚未在 Codex 就绪', unavailable_settings(reason='not_resumed')['message'])

    def test_unresumed_or_unknown_readiness_never_exposes_cached_choices_as_current(self):
        for readiness in ('needs_resume', 'resuming', 'error', None, True):
            with self.subTest(readiness=readiness):
                result = self.snapshot(resumeState=readiness,
                    latestThreadSettings={'model': 'cached', 'effort': 'high', 'serviceTier': 'priority'})
                self.assertEqual(result['status'], 'unavailable')
                self.assertNotIn('model', result); self.assertNotIn('service_tier', result)

    def test_readiness_patches_invalidate_and_restore_only_same_owner_settings(self):
        self.snapshot(latestThreadSettings={'model': 'one', 'effort': 'high', 'serviceTier': 'priority'})
        self.now += 2
        result = self.patches({'op': 'replace', 'path': ['resumeState'], 'value': 'needs_resume'})
        self.assertEqual(result['status'], 'unavailable')
        self.projection.mark_live()
        self.assertEqual(self.projection.snapshot()['status'], 'unavailable')
        result = self.patches({'op': 'replace', 'path': ['resumeState'], 'value': 'resumed'})
        self.assertEqual(result['status'], 'current')
        self.assertEqual(result['model'], 'one')
        self.assertEqual(result['observed_at'], 1000)
        result = self.patches({'op': 'remove', 'path': ['resumeState']})
        self.assertEqual(result['status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
