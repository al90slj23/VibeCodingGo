"""Validate packaged recording integrity, usable signal, and source provenance."""
import array
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest
from urllib.parse import urlparse
import wave


ASSETS = Path(__file__).resolve().parents[1] / 'assets/Sounds'
RECORDINGS = json.loads((ASSETS / 'manifest.json').read_text())['recordings']


class RecordedSoundTests(unittest.TestCase):
    def test_recordings_have_distinct_audio_and_auditable_public_sources(self):
        self.assertGreaterEqual(len(RECORDINGS), 24)
        self.assertEqual(len({e['id'] for e in RECORDINGS}), len(RECORDINGS))
        self.assertEqual(len({e['sha256'] for e in RECORDINGS}), len(RECORDINGS))
        for entry in RECORDINGS:
            with self.subTest(id=entry['id']):
                self.assertEqual(entry['license'], 'CC0-1.0')
                self.assertEqual(entry['source_type'], 'recording')
                self.assertTrue(entry['author'])
                self.assertIn('CC0', entry['source_license_notice'])
                for key in ['source_page', 'download_url', 'source_license_page']:
                    url = urlparse(entry[key])
                    self.assertEqual(url.scheme, 'https')
                    self.assertEqual(url.hostname, 'bigsoundbank.com')
                self.assertEqual(len(entry['original_sha256']), 64)

    def test_every_manifest_clip_is_bundled_decodable_short_and_not_clipped(self):
        self.assertEqual({p.name for p in ASSETS.glob('*.wav')}, {e['file'] for e in RECORDINGS})
        for entry in RECORDINGS:
            with self.subTest(id=entry['id']):
                self.assertEqual(entry['file'], Path(entry['file']).name)
                path = ASSETS / entry['file']
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry['sha256'])
                with wave.open(str(path), 'rb') as handle:
                    self.assertEqual((handle.getnchannels(), handle.getsampwidth(), handle.getframerate()), (1, 2, 48000))
                    seconds = handle.getnframes() / handle.getframerate()
                    self.assertAlmostEqual(seconds, entry['duration_seconds'], places=5)
                    self.assertGreaterEqual(seconds, 1)
                    self.assertLessEqual(seconds, 4)
                    pcm = array.array('h'); pcm.frombytes(handle.readframes(handle.getnframes()))
                if sys.byteorder != 'little':
                    pcm.byteswap()
                peak = max(abs(value) for value in pcm)
                rms = math.sqrt(sum((value / 32767) ** 2 for value in pcm) / len(pcm))
                self.assertGreater(peak, 1000)
                self.assertLessEqual(peak, round(.65 * 32767))
                self.assertGreater(rms, .005)
                self.assertEqual(pcm[0], 0)
                self.assertEqual(pcm[-1], 0)
                self.assertFalse(entry['processing']['looped'])
                self.assertFalse(entry['processing']['pitch_or_timing_changed'])

    def test_frog_bird_insect_and_forest_choices_are_independent_recordings(self):
        by_id = {e['id']: e for e in RECORDINGS}
        self.assertIn('nature-frog-solo', by_id)
        self.assertIn('nature-frog-chorus', by_id)
        for tag, minimum in [('forest', 3), ('birds', 8), ('insects', 4), ('water', 5)]:
            entries = [e for e in RECORDINGS if tag in e['tags']]
            self.assertGreaterEqual(len(entries), minimum)
            self.assertEqual(len({e['download_url'] for e in entries}), len(entries))


if __name__ == '__main__':
    unittest.main()
