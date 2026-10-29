import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibecue.current_model_settings import ModelSettingsProjection
from vibecue.desktop_attention import AttentionProjection, DesktopAttentionService
from vibecue.model_presentation import MAX_CATALOG_BYTES, ModelPresentation


class ModelPresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / 'models_cache.json'
        self.now = 1000.0
        self.presentation = ModelPresentation(self.home, clock=lambda: self.now)

    def catalogue(self, tiers=None, **fields):
        value = {'models': [{'slug': 'model-one', 'display_name': 'Model One',
                             'service_tiers': [{'id': 'priority', 'name': 'Fast'}] if tiers is None else tiers,
                             **fields}]}
        self.path.write_text(json.dumps(value))
        return value

    def settings(self, tier='priority', **fields):
        return {'status': 'current', 'source': 'codex_desktop', 'model': 'model-one',
                'model_status': 'explicit', 'reasoning_effort': 'ultra',
                'service_tier': tier, 'speed_status': 'explicit',
                'observed_at': 900, 'verified_at': 1000, 'expires_at': 1030, **fields}

    def test_catalogue_name_and_confirmed_fast_preserve_raw_settings(self):
        self.catalogue()
        settings = self.settings()
        result = self.presentation.decorate(settings)
        self.assertEqual(result['model'], 'model-one')
        self.assertEqual(result['model_display_name'], 'Model One')
        self.assertEqual((result['speed_kind'], result['speed_label'], result['speed_matches_model']),
                         ('fast', 'Fast', True))
        self.assertEqual(result['speed_source'], 'codex_saved_thread_settings')
        self.assertIn('serviceTier=priority', result['speed_message'])
        self.assertIn('尚未提交', result['speed_message'])
        self.assertEqual(result['reasoning_effort'], 'ultra')
        self.assertNotIn('model_display_name', settings)
        for key, value in settings.items():
            self.assertEqual(result[key], value)

    def test_ultra_effort_does_not_change_fast_into_ultra_fast(self):
        self.catalogue()
        self.assertEqual(self.presentation.decorate(self.settings(reasoning_effort='ultra'))['speed_label'], 'Fast')
        self.catalogue(tiers=[{'id': 'priority', 'name': 'Ultra Fast'}])
        result = self.presentation.decorate(self.settings(reasoning_effort='high'))
        self.assertEqual((result['speed_kind'], result['speed_label']), ('fast', 'Fast'))
        self.catalogue(tiers=[{'id': 'ultrafast', 'name': 'Ultra Fast'}])
        result = self.presentation.decorate(self.settings('ultrafast', reasoning_effort='high'))
        self.assertEqual((result['speed_kind'], result['speed_label']), ('ultrafast', 'Ultra Fast'))

    def test_explicit_standard_is_distinct_from_following_default(self):
        self.catalogue(default_service_tier='priority')
        for tier in ('default', 'standard'):
            with self.subTest(tier=tier):
                result = self.presentation.decorate(self.settings(tier))
                self.assertEqual((result['speed_kind'], result['speed_label']), ('standard', '标准速度'))
        result = self.presentation.decorate(self.settings(None, speed_status='default', status='partial'))
        self.assertEqual((result['speed_kind'], result['speed_label']), ('default', '速度跟随默认'))
        self.assertIsNone(result['speed_matches_model'])
        self.assertNotIn('Fast', result['speed_label'])

    def test_missing_tier_remains_unknown_instead_of_using_model_default(self):
        self.catalogue(default_service_tier='priority')
        result = self.presentation.decorate(self.settings(None, speed_status='unknown', status='partial'))
        self.assertEqual(result['speed_kind'], 'unknown')
        self.assertIsNone(result['speed_matches_model'])

    def test_missing_or_unknown_catalogue_never_asserts_priority_means_fast(self):
        for catalogue in (None, {'models': []}, {'models': [{'slug': 'other-model', 'display_name': 'Other'}]}):
            with self.subTest(catalogue=catalogue):
                if catalogue is not None:
                    self.path.write_text(json.dumps(catalogue))
                result = self.presentation.decorate(self.settings())
                self.assertEqual((result['speed_kind'], result['speed_label']), ('unknown', '速度待核实'))
                self.assertIsNone(result['speed_matches_model'])
                self.assertNotIn('model_display_name', result)
                self.assertEqual(result['service_tier'], 'priority')

    def test_known_model_with_empty_or_other_capability_marks_mismatch(self):
        for tiers in ([], [{'id': 'ultrafast', 'name': 'Ultra Fast'}]):
            with self.subTest(tiers=tiers):
                self.catalogue(tiers=tiers)
                result = self.presentation.decorate(self.settings())
                self.assertEqual(result['speed_kind'], 'unsupported')
                self.assertFalse(result['speed_matches_model'])
                self.assertNotIn('Fast', result['speed_label'])

    def test_fast_alias_requires_its_own_catalogue_mapping(self):
        self.catalogue(tiers=[{'id': 'fast', 'name': 'Fast'}])
        self.assertEqual(self.presentation.decorate(self.settings('fast'))['speed_kind'], 'fast')
        result = self.presentation.decorate(self.settings('priority'))
        self.assertEqual(result['speed_kind'], 'unsupported')
        self.catalogue(tiers=[{'id': 'priority', 'name': 'Fast'}])
        self.assertEqual(self.presentation.decorate(self.settings('fast'))['speed_kind'], 'fast')

    def test_native_priority_identifier_takes_precedence_over_tier_name(self):
        self.catalogue(tiers=[{'id': 'priority', 'name': 'Experimental'}])
        result = self.presentation.decorate(self.settings())
        self.assertEqual(result['speed_kind'], 'fast')
        self.assertTrue(result['speed_matches_model'])

    def test_legacy_priority_name_can_match_fast_alias_without_inventing_icon(self):
        self.catalogue(tiers=[{'id': 'legacy-tier', 'name': 'Priority'}])
        result = self.presentation.decorate(self.settings('fast'))
        self.assertTrue(result['speed_matches_model'])
        self.assertEqual(result['speed_kind'], 'unknown')
        self.assertEqual(self.presentation.decorate(self.settings('priority'))['speed_kind'], 'unsupported')

    def test_catalogue_reload_uses_changed_content_even_with_preserved_mtime_and_size(self):
        self.catalogue()
        self.assertEqual(self.presentation.decorate(self.settings())['model_display_name'], 'Model One')
        info = self.path.stat()
        raw = self.path.read_text().replace('Model One', 'Model Two')
        self.path.write_text(raw)
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, info.st_size)
        self.assertEqual(self.presentation.decorate(self.settings())['model_display_name'], 'Model Two')

    def test_unchanged_catalogue_uses_cached_whitelist_without_rereading(self):
        self.catalogue()
        self.presentation.decorate(self.settings())
        with patch.object(Path, 'open', side_effect=AssertionError('unexpected reread')):
            self.assertEqual(self.presentation.decorate(self.settings())['speed_kind'], 'fast')

    def test_changed_invalid_removed_or_oversized_catalogue_clears_old_capability(self):
        for failure in ('invalid', 'removed', 'oversized', 'unreadable'):
            with self.subTest(failure=failure):
                self.catalogue()
                self.assertEqual(self.presentation.decorate(self.settings())['speed_kind'], 'fast')
                if failure == 'invalid':
                    self.path.write_text('{invalid')
                elif failure == 'removed':
                    self.path.unlink()
                elif failure == 'oversized':
                    with self.path.open('wb') as stream:
                        stream.truncate(MAX_CATALOG_BYTES + 1)
                else:
                    self.catalogue(display_name='Changed')
                if failure == 'unreadable':
                    with patch.object(Path, 'open', side_effect=PermissionError()):
                        result = self.presentation.decorate(self.settings())
                else:
                    result = self.presentation.decorate(self.settings())
                self.assertEqual(result['speed_kind'], 'unknown')
                self.assertNotIn('model_display_name', result)
                self.assertEqual(self.presentation._models, {})

    def test_malformed_capability_data_and_duplicate_slugs_are_unknown(self):
        for malformed in (None, 'priority', [{'id': 'priority'}],
                          [{'id': 'priority', 'name': 'Fast'}, {'id': 'priority', 'name': 'Other'}]):
            with self.subTest(malformed=malformed):
                self.catalogue(service_tiers=malformed)
                self.assertEqual(self.presentation.decorate(self.settings())['speed_kind'], 'unknown')
        value = self.catalogue()
        value['models'].append(value['models'][0])
        self.path.write_text(json.dumps(value))
        self.assertEqual(self.presentation.decorate(self.settings())['speed_kind'], 'unknown')

    def test_only_catalogue_whitelist_is_retained(self):
        value = self.catalogue(secret='PRIVATE', instructions='PRIVATE',
                               service_tiers=[{'id': 'priority', 'name': 'Fast', 'description': 'PRIVATE'}])
        value['identity'] = 'PRIVATE'
        self.path.write_text(json.dumps(value))
        result = self.presentation.decorate(self.settings())
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('PRIVATE', repr(vars(self.presentation)))

    def test_unavailable_stale_or_unverified_snapshot_drops_older_decoration(self):
        self.catalogue()
        original = self.presentation.decorate(self.settings())
        for fields in ({'status': 'unavailable'}, {'status': 'unknown'}, {'source': 'local-transcripts'},
                       {'expires_at': 999}, {'verified_at': 1006}, {'expires_at': 1061},
                       {'verified_at': None}, {'expires_at': None}, {'verified_at': True}):
            with self.subTest(fields=fields):
                result = self.presentation.decorate({**original, **fields})
                self.assertNotIn('speed_kind', result)
                self.assertNotIn('model_display_name', result)

    def test_owner_replacement_and_expired_snapshot_do_not_retain_fast_badge(self):
        self.catalogue()
        projection = ModelSettingsProjection('thread-one', 'owner-one', clock=lambda: self.now)
        projection.apply({'type': 'snapshot', 'revision': 1, 'conversationState': {
            'id': 'thread-one', 'resumeState': 'resumed', 'latestThreadSettings': {
                'model': 'model-one', 'effort': 'ultra', 'serviceTier': 'priority'}}})
        self.assertEqual(self.presentation.decorate(projection.snapshot())['speed_kind'], 'fast')
        self.now += 31
        self.assertNotIn('speed_kind', self.presentation.decorate(projection.snapshot()))
        replacement = ModelSettingsProjection('thread-one', 'owner-two', clock=lambda: self.now)
        self.assertNotIn('speed_kind', self.presentation.decorate(replacement.snapshot()))

    def test_desktop_service_decorates_snapshot_without_changing_projection(self):
        self.catalogue()
        service = DesktopAttentionService(self.home, self.home)
        service.model_presentation = self.presentation
        service.set_threads(['thread-one'])
        service._health['connected'] = True
        projection = AttentionProjection('thread-one', 'owner-one')
        projection.settings.clock = lambda: self.now
        projection.settings.apply({'type': 'snapshot', 'revision': 1, 'conversationState': {
            'id': 'thread-one', 'resumeState': 'resumed', 'latestThreadSettings': {
                'model': 'model-one', 'effort': 'ultra', 'serviceTier': 'priority'}}})
        projection.revision = 1
        service.projections['thread-one'] = projection
        result = service.current_settings()['thread-one']
        self.assertEqual(result['speed_kind'], 'fast')
        self.assertNotIn('speed_kind', projection.settings.snapshot())
        service._health['connected'] = False
        self.assertNotIn('speed_kind', service.current_settings()['thread-one'])


if __name__ == '__main__':
    unittest.main()
