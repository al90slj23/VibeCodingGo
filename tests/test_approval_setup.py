import json
import os
from pathlib import Path
import plistlib
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import codex_launch
from vibecue.events import emit_resolution, event_identifier
from vibecue.providers.approval_status import approval_status


class ApprovalStatusTests(unittest.TestCase):
    def test_pending_threads_require_healthy_source_and_safe_identifiers(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory) / 'bridges'
            folder.mkdir()
            now = time.time()
            live = {'pid': os.getpid(), 'transport': 'stdio', 'connected': True,
                    'last_seen': now, 'pending_human_requests': 1,
                    'pending_thread_ids': ['main-thread', 'bad/path', 42]}
            (folder / 'live.json').write_text(json.dumps(live))
            (folder / 'stale.json').write_text(json.dumps(dict(live, last_seen=now - 20,
                                                              pending_thread_ids=['stale-thread'])))
            self.assertEqual(approval_status(directory, now)['pending_thread_ids'], ['main-thread'])

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

    def test_discovers_all_current_signed_bundle_cli_layouts(self):
        layouts = ('codex-cli/CodexCLI.app/Contents/MacOS/codex', 'codex-cli/bin/codex', 'codex')
        for name in ('ChatGPT.app', 'Codex.app'):
            for layout in layouts:
                with self.subTest(bundle=name, layout=layout), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    app = root / name
                    contents = app / 'Contents'
                    executable = contents / 'MacOS' / 'Desktop'
                    executable.parent.mkdir(parents=True)
                    executable.write_text('desktop'); executable.chmod(0o700)
                    (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'Desktop'}))
                    cli = contents / 'Resources' / layout
                    cli.parent.mkdir(parents=True)
                    cli.write_text('official cli'); cli.chmod(0o700)
                    with patch.object(codex_launch, 'application_directories', return_value=(root,)):
                        self.assertEqual(codex_launch.find_codex_app(), (app, executable, cli))

    def test_discovery_rejects_non_executable_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contents = root / 'Codex.app' / 'Contents'
            executable = contents / 'MacOS' / 'Desktop'
            executable.parent.mkdir(parents=True)
            executable.write_text('desktop'); executable.chmod(0o700)
            (contents / 'Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'Desktop'}))
            cli = contents / 'Resources' / 'codex'
            cli.parent.mkdir(parents=True)
            cli.write_text('not executable'); cli.chmod(0o600)
            with patch.object(codex_launch, 'application_directories', return_value=(root,)):
                with self.assertRaises(RuntimeError): codex_launch.find_codex_app()

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

    def test_default_wait_remains_armed_beyond_old_thirty_minute_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.fixture(directory)
            with patch.object(codex_launch, 'launcher_parent', return_value=None), \
                    patch.object(codex_launch, 'find_codex_app', return_value=(Path('/app'), Path('/app/main'), Path('/app/codex'))), \
                    patch.object(codex_launch, 'app_running', side_effect=[True, True, False]), \
                    patch.object(codex_launch.time, 'monotonic', side_effect=[1, 3601]), \
                    patch.object(codex_launch.time, 'sleep'), \
                    patch.object(codex_launch, 'write_launch_status', wraps=codex_launch.write_launch_status) as write, \
                    patch.object(codex_launch.subprocess, 'run') as run:
                self.assertEqual(codex_launch.launch_codex(True, home), 0)
                run.assert_called_once()
                waiting = [call for call in write.call_args_list if call.args[1] == 'waiting_for_codex_exit']
                self.assertEqual(len(waiting), 2)  # initial state and fresh heartbeat
            self.assertEqual(json.loads((home / 'bridge-launch-state.json').read_text())['status'], 'launched')

    def test_waiter_cancels_when_original_vibecue_instance_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            home = self.fixture(directory)
            with patch.dict(os.environ, {'VIBECUE_PARENT_PID': '12345'}), \
                    patch.object(codex_launch.os, 'getppid', side_effect=[12345, 1]), \
                    patch.object(codex_launch, 'find_codex_app', return_value=(Path('/app'), Path('/app/main'), Path('/app/codex'))), \
                    patch.object(codex_launch, 'app_running', return_value=True), \
                    patch.object(codex_launch.subprocess, 'run') as run:
                self.assertEqual(codex_launch.launch_codex(True, home), 0)
                run.assert_not_called()
            self.assertEqual(json.loads((home / 'bridge-launch-state.json').read_text())['status'], 'cancelled')

    def test_parent_pid_reuse_cannot_authorize_an_orphan_waiter(self):
        with patch.dict(os.environ, {'VIBECUE_PARENT_PID': '12345'}), \
                patch.object(codex_launch.os, 'getppid', return_value=1), \
                patch.object(codex_launch.os, 'kill') as kill:
            with self.assertRaises(RuntimeError): codex_launch.launcher_parent()
            kill.assert_not_called()  # PID existence alone does not prove an owner instance.


if __name__=='__main__':unittest.main()
