import json
from pathlib import Path
import tempfile
import unittest

from vibecue.events import emit_event
from vibecue.providers import PROVIDERS


class ProviderContractTests(unittest.TestCase):
    def test_only_implemented_provider_advertised(self):
        self.assertEqual(set(PROVIDERS), {'codex'})
        self.assertEqual(PROVIDERS['codex']['status'], 'available')

    def test_provider_namespaces_event_dedupe_and_is_in_spool(self):
        with tempfile.TemporaryDirectory() as directory:
            args = dict(kind='input', thread_id='shared-id', turn_id='same-turn',
                        title='對話', vibecue_home=directory, event_id='same-id')
            self.assertTrue(emit_event(**args, provider='codex'))
            self.assertFalse(emit_event(**args, provider='codex'))
            self.assertTrue(emit_event(**args, provider='test-adapter'))
            events = [json.loads(p.read_text()) for p in (Path(directory) / 'events').glob('*.json')]
            self.assertEqual({e['provider'] for e in events}, {'codex', 'test-adapter'})
            self.assertEqual(len({e['id'] for e in events}), 2)


if __name__ == '__main__':
    unittest.main()
