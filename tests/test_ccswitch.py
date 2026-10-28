import contextlib
import io
import json
from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from vibecue import ccswitch


class CCSwitchCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.app = self.root / 'CC Switch.app'
        self.info = self.app / 'Contents/Info.plist'
        self.info.parent.mkdir(parents=True)
        self.info.write_bytes(plistlib.dumps({
            'CFBundleIdentifier': ccswitch.BUNDLE_ID,
            'CFBundleShortVersionString': '4.0.21'}))

    def test_reports_installed_app_without_claiming_an_account_count(self):
        result = ccswitch.status([self.app], self.root)
        self.assertTrue(result['installed'])
        self.assertEqual(result['installed_version'], '4.0.21')
        self.assertFalse(result['available'])
        self.assertEqual(result['reason'], 'ccswitch_no_public_quota_feed')
        self.assertIsNone(result['account_count'])
        self.assertEqual(result['accounts'], [])

    def test_app_absence_and_corrupt_plist_are_safe(self):
        self.info.write_bytes(b'not a plist')
        result = ccswitch.status([self.root / 'missing.app', self.app], self.root)
        self.assertFalse(result['installed'])
        self.assertIsNone(result['installed_version'])
        self.assertFalse(result['available'])

    def test_wrong_bundle_is_not_treated_as_cc_switch(self):
        self.info.write_bytes(plistlib.dumps({'CFBundleIdentifier': 'another.app'}))
        self.assertFalse(ccswitch.status([self.app], self.root)['installed'])

    def test_credentials_database_and_network_are_never_opened(self):
        # Sentinel contents must remain unread; only the public Info.plist may
        # be opened, and checking database existence cannot become a DB query.
        database = self.root / 'cc-switch.db'
        database.write_bytes(b'private database sentinel')
        (self.root / 'codex_auth.json').write_bytes(b'private credential sentinel')
        original_open = Path.open
        opened = []

        def only_plist(path, *args, **kwargs):
            opened.append(path)
            self.assertEqual(path, self.info)
            return original_open(path, *args, **kwargs)

        with patch.object(Path, 'open', only_plist), \
             patch.object(sqlite3, 'connect', side_effect=AssertionError('no database read')), \
             patch.object(urllib.request, 'urlopen', side_effect=AssertionError('no network request')):
            result = ccswitch.status([self.app], self.root)
        self.assertEqual(opened, [self.info])
        self.assertTrue(result['database_present'])
        self.assertNotIn('sentinel', json.dumps(result))

    def test_newer_version_is_not_claimed_as_validated(self):
        self.info.write_bytes(plistlib.dumps({
            'CFBundleIdentifier': ccswitch.BUNDLE_ID,
            'CFBundleShortVersionString': '99.0.0'}))
        result = ccswitch.status([self.app], self.root)
        self.assertEqual(result['installed_version'], '99.0.0')
        self.assertEqual(result['validated_version'], '4.0.21')
        self.assertFalse(result['available'])

    def test_cli_emits_safe_capability_metadata(self):
        stream = io.StringIO()
        result = ccswitch.status([], self.root)
        with patch.object(ccswitch, 'status', return_value=result), contextlib.redirect_stdout(stream):
            self.assertEqual(ccswitch.main(['status']), 0)
        self.assertEqual(json.loads(stream.getvalue())['account_count'], None)


if __name__ == '__main__':
    unittest.main()
