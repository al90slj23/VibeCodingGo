import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from vibecue import usage_pricing as pricing


class UsagePricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = 1_800_000_000

    def source(self, identity='first', kind='json', enabled=True, **updates):
        result = dict(id=identity, name=identity.title(), kind=kind, enabled=enabled)
        if kind not in ('custom', 'ccswitch'):
            result['url'] = 'https://prices.example/' + identity + '.json'
        result.update(updates)
        return result

    def prefs(self, sources, overrides=None):
        return dict(pricing_sources=sources, pricing_refresh_seconds=pricing.REFRESH_SECONDS,
                    model_overrides=overrides or {})

    def rates(self, input=2, output=10, cache_read=.5, cache_creation=3):
        return dict(input=input, output=output, cache_read=cache_read, cache_creation=cache_creation)

    def cached(self, source, models=None, **updates):
        if models is None:
            models = {'model-a': self.rates()}
        normalized, invalid = pricing.normalize('json', {'models': models})
        raw = dict(fingerprint=pricing.fingerprint(source), models=normalized,
                   updated_at=self.now, attempted_at=self.now, error=False, invalid_prices=invalid)
        raw.update(updates)
        pricing.write_cache(self.home, source, raw)
        return raw

    def combine(self, prefs, **updates):
        arguments = dict(cc_prices={}, cc_available=False, cc_invalid=0, now=self.now)
        arguments.update(updates)
        return pricing.combine(self.home, prefs, **arguments)

    def test_default_priority_and_default_copies_are_independent(self):
        first = pricing.defaults()
        self.assertEqual([row['kind'] for row in first],
                         ['custom', 'ccswitch', 'basellm', 'models_dev', 'litellm', 'openrouter', 'vercel'])
        first[0]['enabled'] = False
        self.assertTrue(pricing.defaults()[0]['enabled'])
        self.assertEqual(pricing.validate_sources(pricing.defaults()), pricing.defaults())

    def test_queue_uses_first_exact_model_then_falls_back_and_can_be_reordered(self):
        first, second = self.source(), self.source('second')
        self.cached(first, {'model-a': self.rates(input=2)})
        self.cached(second, {'model-a': self.rates(input=7), 'model-b': self.rates(input=5)})
        prices, _ = self.combine(self.prefs([first, second]))
        self.assertEqual(prices['model-a']['input'], 2)
        self.assertEqual(prices['model-a']['source'], 'first')
        self.assertEqual(prices['model-b']['source'], 'second')
        self.assertNotIn('MODEL-A', prices)
        self.assertNotIn('model-a-2026-01-01', prices)
        prices, _ = self.combine(self.prefs([second, first]))
        self.assertEqual(prices['model-a']['input'], 7)

    def test_local_sources_follow_queue_position_and_manual_can_be_disabled(self):
        manual, cc = self.source('manual', 'custom'), self.source('cc', 'ccswitch')
        remote = self.source()
        self.cached(remote)
        overrides = {'model-a': self.rates(input=1)}
        cc_models = {'model-a': dict(model='model-a', name='Model A', **self.rates(input=8))}
        prices, _ = self.combine(self.prefs([manual, cc, remote], overrides), cc_prices=cc_models, cc_available=True)
        self.assertEqual(prices['model-a']['input'], 1)
        prices, _ = self.combine(self.prefs([cc, manual, remote], overrides), cc_prices=cc_models, cc_available=True)
        self.assertEqual(prices['model-a']['input'], 8)
        manual['enabled'] = False
        cc['enabled'] = False
        prices, status = self.combine(self.prefs([manual, cc, remote], overrides), cc_prices=cc_models, cc_available=True)
        self.assertEqual(prices['model-a']['input'], 2)
        self.assertEqual([row['status'] for row in status['source_status'][:2]], ['disabled', 'disabled'])

    def test_zero_is_a_real_price_but_missing_cache_rate_remains_unknown(self):
        first, second = self.source(), self.source('second')
        self.cached(first, {'free': self.rates(0, 0, 0, 0), 'partial': {'input': 2, 'output': 5}})
        self.cached(second, {'free': self.rates(8), 'partial': self.rates(9)})
        prices, _ = self.combine(self.prefs([first, second]))
        self.assertEqual(prices['free']['input'], 0)
        self.assertEqual(prices['free']['cache_creation'], 0)
        self.assertEqual(prices['partial']['input'], 2)
        self.assertIsNone(prices['partial']['cache_read'])
        self.assertIsNone(prices['partial']['cache_creation'])

    def test_litellm_per_token_prices_and_modes(self):
        prices, invalid = pricing.normalize('litellm', {
            'model-a': dict(mode='chat', input_cost_per_token=.000002,
                            output_cost_per_token=.00001, cache_read_input_token_cost=.0000005,
                            cache_creation_input_token_cost=.000003),
            'image': dict(mode='image_generation', input_cost_per_token=2, output_cost_per_token=10),
            'sample_spec': {'input_cost_per_token': 'placeholder', 'output_cost_per_token': 0},
        })
        self.assertEqual({k: prices['model-a'][k] for k in pricing.RATE_KEYS}, self.rates())
        self.assertNotIn('image', prices)
        self.assertEqual(invalid, 1)

    def test_models_dev_million_token_prices_and_native_namespaces(self):
        payload = {
            'discount': {'models': {'model-a': {'cost': dict(input=.01, output=.02)}}},
            'openai': {'models': {'model-a': {'name': 'Origin', 'cost': dict(input=2, output=10, cache_read=.5, cache_write=3)}}},
        }
        prices, _ = pricing.normalize('models_dev', payload)
        self.assertEqual(prices['model-a']['input'], 2)
        self.assertEqual(prices['openai/model-a']['input'], 2)
        self.assertEqual(prices['discount/model-a']['input'], .01)
        self.assertEqual(prices['model-a']['cache_creation'], 3)
        prices, _ = pricing.normalize('models_dev', {'discount': payload['discount']})
        self.assertNotIn('model-a', prices)
        self.assertIn('discount/model-a', prices)

    def test_openrouter_decimal_string_prices_are_per_token(self):
        prices, _ = pricing.normalize('openrouter', {'data': [dict(
            id='openai/model-a', pricing=dict(prompt='0.000002', completion='0.00001',
                                            input_cache_read='0.0000005', input_cache_write='0.000003'))]})
        self.assertEqual({k: prices['openai/model-a'][k] for k in pricing.RATE_KEYS}, self.rates())
        self.assertNotIn('model-a', prices)

    def test_vercel_converts_scalar_language_prices_and_skips_other_units(self):
        prices, _ = pricing.normalize('vercel', {'data': [
            dict(id='origin/model-a', type='language', pricing=dict(input='0.000002', output='0.00001',
                 input_cache_read='0.0000005', input_cache_write='0.000003', varies_by_provider=True)),
            dict(id='image-model', type='image', pricing=dict(input='2', output='10')),
            dict(id='tier-only', type='language', pricing=dict(input_tiers=[{'cost': '.1'}], output_tiers=[{'cost': '.2'}])),
        ]})
        self.assertEqual({k: prices['origin/model-a'][k] for k in pricing.RATE_KEYS}, self.rates())
        self.assertNotIn('image-model', prices)
        self.assertNotIn('tier-only', prices)

    def test_basellm_ratios_convert_to_million_tokens_and_skip_dynamic_or_fixed(self):
        models = ['standard', 'million-unit', 'dynamic', 'fixed', 'seconds', 'missing-output']
        payload = {'data': {
            'model_ratio': {model: 1 for model in models},
            'completion_ratio': {model: 5 for model in models if model != 'missing-output'},
            'cache_ratio': {'standard': .25, 'million-unit': .25},
            'create_cache_ratio': {'standard': 1.5, 'million-unit': 1.5},
            'billing_mode': {'standard': 'ratio', 'dynamic': 'tiered_expr', 'fixed': 'price'},
            'billing_expr': {'dynamic': '__import__("os").system("must-not-execute")'},
            'model_price': {'fixed': .1},
            'billing_unit': {'million-unit': 'million_tokens', 'seconds': 'second'},
        }}
        prices, _ = pricing.normalize('basellm', payload)
        self.assertEqual({k: prices['standard'][k] for k in pricing.RATE_KEYS}, self.rates())
        self.assertEqual({k: prices['million-unit'][k] for k in pricing.RATE_KEYS}, self.rates())
        self.assertEqual(set(prices), {'standard', 'million-unit'})

    def test_basellm_strict_linear_expression_coefficients_are_already_usd_per_million(self):
        payload = {'data': {
            'billing_expr': {
                'four-rates': 'tier("standard", p * 2 + cr * 0.5 + cc * 3 + c * 10)',
                'reordered': 'tier("base", 10 * c + cc * 3 + 2 * p + 0.5 * cr)',
                'free': 'tier("standard", p * 0 + cr * 0 + cc * 0 + c * 0)',
            },
            'billing_mode': {model: 'tiered_expr' for model in ['four-rates', 'reordered', 'free']},
        }}
        with patch('builtins.eval', side_effect=AssertionError('remote expressions must not execute')):
            prices, _ = pricing.normalize('basellm', payload)
        for model in ['four-rates', 'reordered']:
            self.assertEqual({key: prices[model][key] for key in pricing.RATE_KEYS}, self.rates())
        self.assertEqual({key: prices['free'][key] for key in pricing.RATE_KEYS}, self.rates(0, 0, 0, 0))

    def test_basellm_linear_expression_missing_cache_terms_inherit_prompt_rate_by_dsl_contract(self):
        payload = {'data': {'billing_expr': {
            'plain': 'tier("standard", p * 2 + c * 10)',
            'read-only': 'tier("standard", p * 2 + cr * 0.5 + c * 10)',
            'write-only': 'tier("standard", p * 2 + cc * 3 + c * 10)',
        }}}
        prices, _ = pricing.normalize('basellm', payload)
        self.assertEqual({key: prices['plain'][key] for key in pricing.RATE_KEYS}, self.rates(2, 10, 2, 2))
        self.assertEqual({key: prices['read-only'][key] for key in pricing.RATE_KEYS}, self.rates(2, 10, .5, 2))
        self.assertEqual({key: prices['write-only'][key] for key in pricing.RATE_KEYS}, self.rates(2, 10, 2, 3))

    def test_basellm_expression_allowlist_rejects_tiers_other_units_duplicates_and_code(self):
        expressions = {
            'allowed': 'tier("standard", p * 2 + c * 10)',
            'context-tier': 'len <= 200000 ? tier("standard", p * 2 + c * 10) : tier("long", p * 4 + c * 20)',
            'cache-1h': 'tier("standard", p * 2 + cr * 0.5 + cc * 3 + cc1h * 6 + c * 10)',
            'images': 'tier("standard", p * 2 + img * 1 + c * 10)',
            'fixed': 'tier("standard", usd(0.1))',
            'duplicate': 'tier("standard", p * 1 + p * 1 + c * 10)',
            'negative': 'tier("standard", p * -2 + c * 10)',
            'missing-c': 'tier("standard", p * 2 + cr * 0.5)',
            'function': 'tier("standard", max(p, 1) * 2 + c * 10)',
            'code': 'tier("standard", p * 2 + c * 10); __import__("os").system("must-not-execute")',
            'infinite': 'tier("standard", p * Infinity + c * 10)',
        }
        with patch('builtins.eval', side_effect=AssertionError('remote expressions must not execute')):
            prices, _ = pricing.normalize('basellm', {'data': {'billing_expr': expressions}})
        self.assertEqual(set(prices), {'allowed'})
        expressions.pop('allowed')
        with self.assertRaises(ValueError):
            pricing.normalize('basellm', {'data': {'billing_expr': expressions}})

    def test_basellm_malformed_optional_map_does_not_hide_other_valid_price_format(self):
        payloads = [
            {'model_ratio': ['unsupported'], 'billing_expr': {'allowed': 'tier("standard", p * 2 + c * 10)'}},
            {'model_ratio': {'allowed': 1}, 'completion_ratio': {'allowed': 5}, 'billing_expr': ['unsupported']},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                prices, _ = pricing.normalize('basellm', {'data': payload})
                self.assertEqual(prices['allowed']['input'], 2)
                self.assertEqual(prices['allowed']['output'], 10)

    def test_standard_json_keeps_base_units_and_invalid_numbers_never_become_free(self):
        payload = {'models': {'valid': self.rates(), 'zero': self.rates(0, 0, 0, 0)}}
        for index, value in enumerate([True, -1, float('nan'), float('inf'), 'Infinity', 'invalid']):
            payload['models']['bad-' + str(index)] = self.rates(input=value)
        payload['models']['no-output'] = {'input': 2}
        prices, invalid = pricing.normalize('json', payload)
        self.assertEqual(set(prices), {'valid', 'zero'})
        self.assertEqual(invalid, 7)
        self.assertEqual(prices['valid']['input'], 2)

    def test_source_validation_rejects_duplicate_identity_and_bad_local_definitions(self):
        bad = [[self.source(), self.source()],
               [self.source('one', 'custom'), self.source('two', 'custom')],
               [self.source('one', 'ccswitch', url='https://example.com/')],
               [dict(self.source(), enabled=1)], [dict(self.source(), secret='x')]]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                pricing.validate_sources(value)
        self.assertEqual(pricing.validate_sources([]), [])

    def test_public_https_url_validation(self):
        for url in ['http://example.com/a', 'file:///etc/passwd', 'https://localhost/a',
                    'https://127.0.0.1/a', 'https://[::1]/a', 'https://10.0.0.1/a',
                    'https://192.168.2.1/a', 'https://service.local/a', 'https://example.com:444/a',
                    'https://user:secret@example.com/a', 'https://example.com/a#fragment',
                    'https://example.com/a?api_key=secret', 'https://example.com/a?access_token=secret']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                pricing.validate_url(url)
        self.assertEqual(pricing.validate_url('https://prices.example/a.json?v=2'), 'https://prices.example/a.json?v=2')

    def test_cache_round_trip_is_private_and_invalid_metadata_is_not_used(self):
        source = self.source()
        self.cached(source)
        path = pricing.cache_path(self.home, source)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(pricing.read_cache(self.home, source, self.now)['models']['model-a']['input'], 2)
        raw = json.loads(path.read_bytes())
        for updates in [dict(fingerprint='wrong'), dict(updated_at=self.now + 61), dict(attempted_at=True),
                        dict(models={'invalid': {'input': -1, 'output': 0}})]:
            path.write_text(json.dumps(dict(raw, **updates)))
            self.assertEqual(pricing.read_cache(self.home, source, self.now), {})
        path.write_text('broken-json')
        self.assertEqual(pricing.read_cache(self.home, source, self.now), {})

    def test_fresh_cache_avoids_network_until_configured_six_hour_interval(self):
        source = self.source()
        self.cached(source, updated_at=self.now - pricing.REFRESH_SECONDS + 1)
        with patch.object(pricing, 'fetch_source') as fetch:
            pricing.refresh(self.home, self.prefs([source]), now=self.now)
            fetch.assert_not_called()
        normalized, _ = pricing.normalize('json', {'models': {'new-model': self.rates()}})
        with patch.object(pricing, 'fetch_source', return_value=(normalized, 0)) as fetch:
            pricing.refresh(self.home, self.prefs([source]), now=self.now + 1)
            fetch.assert_called_once()
        self.assertIn('new-model', pricing.read_cache(self.home, source, self.now + 1)['models'])

    def test_failure_preserves_last_success_and_uses_backoff_then_can_force_retry(self):
        source = self.source()
        original_time = self.now - pricing.REFRESH_SECONDS
        self.cached(source, updated_at=original_time)
        with patch.object(pricing, 'fetch_source', side_effect=OSError('offline')) as fetch:
            pricing.refresh(self.home, self.prefs([source]), now=self.now)
            self.assertEqual(fetch.call_count, 1)
            raw = pricing.read_cache(self.home, source, self.now)
            self.assertEqual(raw['updated_at'], original_time)
            self.assertTrue(raw['error'])
            self.assertEqual(raw['models']['model-a']['input'], 2)
            pricing.refresh(self.home, self.prefs([source]), now=self.now + pricing.FAILURE_SECONDS - 1)
            self.assertEqual(fetch.call_count, 1)
            pricing.refresh(self.home, self.prefs([source]), force=True, now=self.now + pricing.FAILURE_SECONDS - 1)
            self.assertEqual(fetch.call_count, 2)
        prices, state = self.combine(self.prefs([source]), now=self.now + pricing.FAILURE_SECONDS - 1)
        self.assertEqual(prices['model-a']['input'], 2)
        self.assertTrue(prices['model-a']['source_stale'])
        self.assertEqual(state['pricing_status'], 'partial')

    def test_missing_source_continues_to_next_source_with_readable_status(self):
        first, second = self.source(), self.source('second')
        pricing.write_cache(self.home, first, dict(fingerprint=pricing.fingerprint(first), attempted_at=self.now, error=True))
        self.cached(second)
        prices, state = self.combine(self.prefs([first, second]))
        self.assertEqual(prices['model-a']['source'], 'second')
        self.assertEqual(state['source_status'][0]['status'], 'error')

    def test_disabled_source_is_not_fetched_or_used_and_changed_url_has_separate_cache(self):
        source = self.source()
        self.cached(source)
        disabled = dict(source, enabled=False)
        with patch.object(pricing, 'fetch_source') as fetch:
            pricing.refresh(self.home, self.prefs([disabled]), force=True, now=self.now)
            fetch.assert_not_called()
        self.assertEqual(self.combine(self.prefs([disabled]))[0], {})
        changed = dict(source, url='https://prices.example/replacement.json')
        self.assertNotEqual(pricing.cache_path(self.home, source), pricing.cache_path(self.home, changed))
        self.assertEqual(pricing.read_cache(self.home, changed, self.now), {})
        self.assertEqual(self.combine(self.prefs([changed]))[0], {})
        renamed = dict(source, name='Renamed')
        self.assertEqual(pricing.read_cache(self.home, renamed, self.now)['models']['model-a']['input'], 2)

    def test_combine_is_local_only_and_does_not_refresh_stale_cache(self):
        source = self.source()
        self.cached(source, updated_at=self.now - pricing.REFRESH_SECONDS)
        with patch.object(pricing, 'fetch_source', side_effect=AssertionError('query network forbidden')), \
             patch.object(pricing, 'refresh', side_effect=AssertionError('query refresh forbidden')):
            prices, state = self.combine(self.prefs([source]))
        self.assertEqual(prices['model-a']['input'], 2)
        self.assertEqual(state['source_status'][0]['status'], 'stale')

    def test_refresh_lock_prevents_duplicate_network_fetch_and_is_private(self):
        source = self.source()
        with (self.home / 'usage-pricing-refresh.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(pricing, 'fetch_source') as fetch:
                pricing.refresh(self.home, self.prefs([source]), force=True, now=self.now)
                fetch.assert_not_called()
        self.assertEqual((self.home / 'usage-pricing-refresh.lock').stat().st_mode & 0o777, 0o600)

    def test_fetch_uses_anonymous_get_and_normalizes_response(self):
        source = self.source()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({'models': {'model-a': self.rates()}}).encode()
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(pricing.urllib.request, 'build_opener', return_value=opener):
            prices, invalid = pricing.fetch_source(source)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, source['url'])
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertNotIn('Authorization', request.headers)
        self.assertEqual(prices['model-a']['input'], 2)
        self.assertEqual(invalid, 0)

    def test_fetch_and_cache_enforce_byte_limits_without_trusting_response_headers(self):
        source = self.source()
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'x' * 129
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(pricing, 'MAX_RESPONSE', 128), \
             patch.object(pricing.urllib.request, 'build_opener', return_value=opener), \
             self.assertRaisesRegex(ValueError, '响应过大'):
            pricing.fetch_source(source)
        response.read.assert_called_once_with(129)
        with patch.object(pricing, 'MAX_CACHE', 8), self.assertRaisesRegex(ValueError, '过大'):
            self.cached(source)
        path = pricing.cache_path(self.home, source)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'x' * 9)
        with patch.object(pricing, 'MAX_CACHE', 8):
            self.assertEqual(pricing.read_cache(self.home, source, self.now), {})

    def test_redirects_are_not_followed_to_hidden_targets(self):
        with self.assertRaisesRegex(ValueError, '重定向'):
            pricing.NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://other.example/catalog')


if __name__ == '__main__':
    unittest.main()
