import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibecue import usage_schedule as schedule


class UsageScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'

    def test_defaults_are_read_only_without_file_creation(self):
        self.assertEqual(schedule.settings(self.home)['prefs'], schedule.DEFAULTS)
        self.assertFalse(self.home.exists())

    def test_manual_and_custom_intervals_persist_privately_and_patch_preserves(self):
        schedule.save(self.home, {'auto_sync': False, 'sync_interval_seconds': 1234,
                                 'display_interval_seconds': 17})
        schedule.save(self.home, {'auto_sync': True})
        self.assertEqual(schedule.settings(self.home)['prefs'],
                         dict(auto_sync=True, sync_interval_seconds=1234, display_interval_seconds=17))
        self.assertEqual((self.home / 'usage-schedule.json').stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.home.glob('.usage-schedule-*')), [])

    def test_invalid_updates_do_not_modify_existing_preferences(self):
        schedule.save(self.home, {'auto_sync': False})
        path = self.home / 'usage-schedule.json'; before = path.read_bytes()
        bad = [{'auto_sync': 1}, {'auto_sync': 'true'}, {'sync_interval_seconds': 29},
               {'sync_interval_seconds': 86401}, {'sync_interval_seconds': True},
               {'sync_interval_seconds': 30.5}, {'sync_interval_seconds': '600'},
               {'display_interval_seconds': 4}, {'display_interval_seconds': 3601},
               {'display_interval_seconds': float('nan')}, {'display_interval_seconds': 10 ** 1000},
               {'other': 30}, [], None]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(schedule.ScheduleError): schedule.save(self.home, value)
                self.assertEqual(path.read_bytes(), before)

    def test_corrupt_or_oversized_file_falls_back_without_rewriting(self):
        self.home.mkdir()
        path = self.home / 'usage-schedule.json'
        for raw in [b'{', b'{"auto_sync":1}', b'x' * (schedule.MAX_BYTES + 1)]:
            path.write_bytes(raw)
            result = schedule.settings(self.home)
            self.assertEqual(result['prefs'], schedule.DEFAULTS)
            self.assertTrue(result['settings_warning'])
            self.assertEqual(path.read_bytes(), raw)

    def test_cli_round_trip_and_invalid_payload_returns_failure(self):
        with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(b'{"display_interval_seconds":5}'))), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(schedule.main(['save', '--home', str(self.home)]), 0)
            self.assertEqual(json.loads(out.getvalue())['prefs']['display_interval_seconds'], 5)
        with patch('sys.stdin', io.TextIOWrapper(io.BytesIO(b'[]'))), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(schedule.main(['save', '--home', str(self.home)]), 1)
            self.assertIn('error', json.loads(out.getvalue()))


if __name__ == '__main__': unittest.main()
