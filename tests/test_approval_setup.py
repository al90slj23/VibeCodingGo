import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import codex_launch
from vibecue.events import emit_resolution, event_identifier
from vibecue.providers.approval_status import approval_status


class ApprovalStatusTests(unittest.TestCase):
    def test_live_stdio_source_and_stale_dead_invalid_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);folder=home/'bridges';folder.mkdir()
            now=time.time()
            live={'pid':os.getpid(),'transport':'stdio','connected':True,'last_seen':now,'pending_human_requests':2}
            (folder/'live.json').write_text(json.dumps(live))
            (folder/'stale.json').write_text(json.dumps(dict(live,last_seen=now-20)))
            (folder/'invalid.json').write_text(json.dumps(dict(live,pending_human_requests='invalid')))
            result=approval_status(home,now)
            self.assertTrue(result['connected'])
            self.assertEqual(result['live_sources'],1)
            self.assertEqual(result['pending_count'],2)
            with patch('vibecue.providers.approval_status.os.kill',side_effect=ProcessLookupError):
                self.assertFalse(approval_status(home,now)['connected'])

    def test_resolution_is_not_an_alert_and_matches_event_id(self):
        with tempfile.TemporaryDirectory() as directory:
            identifier=event_identifier('request')
            self.assertTrue(emit_resolution(identifier,'thread',directory))
            event=json.loads(next((Path(directory)/'events').glob('*.json')).read_text())
            self.assertEqual(event['action'],'resolve')
            self.assertEqual(event['target_id'],identifier)
            self.assertNotIn('kind',event)
            self.assertFalse(emit_resolution('../invalid','thread',directory))


class ApprovalLaunchTests(unittest.TestCase):
    def fixture(self,directory):
        home=Path(directory);wrapper=home/'vibecue-codex'
        wrapper.write_text('#!/bin/sh\nexit 0\n');wrapper.chmod(0o700)
        return home

    def test_running_app_is_never_killed_or_reconfigured(self):
        with tempfile.TemporaryDirectory() as directory:
            home=self.fixture(directory)
            with patch.object(codex_launch,'find_codex_app',return_value=(Path('/Applications/ChatGPT.app'),Path('/app/main'),Path('/app/codex'))), \
                    patch.object(codex_launch,'app_running',return_value=True), \
                    patch.object(codex_launch.subprocess,'run') as run:
                with self.assertRaises(RuntimeError):codex_launch.launch_codex(home=home)
                run.assert_not_called()
            self.assertEqual(json.loads((home/'bridge-launch-state.json').read_text())['status'],'error')

    def test_waits_for_user_exit_then_only_sets_cli_override(self):
        with tempfile.TemporaryDirectory() as directory:
            home=self.fixture(directory)
            app=Path('/Applications/ChatGPT.app')
            with patch.object(codex_launch,'find_codex_app',return_value=(app,Path('/app/main'),Path('/app/codex'))), \
                    patch.object(codex_launch,'app_running',side_effect=[True,True,False]), \
                    patch.object(codex_launch.time,'sleep'), \
                    patch.object(codex_launch.subprocess,'run') as run:
                self.assertEqual(codex_launch.launch_codex(True,home),0)
            command=run.call_args.args[0]
            self.assertEqual(command[0],'/usr/bin/open')
            self.assertIn('CODEX_CLI_PATH='+str(home/'vibecue-codex'),command)
            self.assertEqual(command[-1],str(app))
            self.assertFalse(any('approval' in value or 'sandbox' in value for value in command))
            self.assertEqual(json.loads((home/'bridge-launch-state.json').read_text())['status'],'launched')

    def test_wait_timeout_does_not_start_or_stop_codex(self):
        with tempfile.TemporaryDirectory() as directory:
            home=self.fixture(directory)
            with patch.object(codex_launch,'find_codex_app',return_value=(Path('/app'),Path('/app/main'),Path('/app/codex'))), \
                    patch.object(codex_launch,'app_running',return_value=True), \
                    patch.object(codex_launch.subprocess,'run') as run:
                with self.assertRaises(RuntimeError):codex_launch.launch_codex(True,home,timeout=0)
                run.assert_not_called()


if __name__=='__main__':unittest.main()
