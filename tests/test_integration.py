from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibecue import cli

PROJECT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('vibecue_installer', PROJECT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class HookAdapterTests(unittest.TestCase):
    def test_raw_permission_requests_never_claim_human_wait(self):
        payload = {'hook_event_name': 'PermissionRequest', 'session_id': 'parent',
                   'turn_id': 't1', 'tool_name': 'Bash', 'tool_input': {'command': 'first'}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'VIBECUE_HOME': directory}), \
                patch.object(cli, '_main_thread', return_value=True), patch.object(cli, 'emit_event') as emit:
            for mode in ['default', 'dontAsk', 'acceptEdits', 'bypassPermissions']:
                payload['permission_mode'] = mode
                cli.handle_hook(payload)
            payload.update(tool_input={'command': 'second'}, requires_user_action=True)
            cli.handle_hook(payload)
            emit.assert_not_called()

    def test_subagent_hook_is_silent(self):
        with patch.object(cli, '_main_thread', return_value=False), patch.object(cli, 'emit_event') as emit:
            cli.handle_hook({'hook_event_name': 'PermissionRequest', 'session_id': 'parent'})
        emit.assert_not_called()

    def test_malformed_hook_input_never_controls_or_breaks_codex(self):
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO('not json')), redirect_stdout(output):
            self.assertEqual(cli.main(['hook']), 0)
        self.assertEqual(json.loads(output.getvalue()), {})


class InstallTests(unittest.TestCase):
    def test_stop_ignores_unrelated_pid_and_terminates_owned_app(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)
            executable=Path('/Applications/VibeCue.app/Contents/MacOS/VibeCue')
            (home/'app-status.json').write_text(json.dumps({'pid':2222}))
            unrelated=type('Result',(),{'returncode':0,'stdout':f'{os.getuid()} /Applications/Other.app/Contents/MacOS/Other'})()
            with patch.object(installer.subprocess,'run',return_value=unrelated),patch.object(installer.os,'kill') as kill:
                installer.stop_owned_app(home,executable)
                kill.assert_not_called()
            with patch.object(installer,'owned_pid',side_effect=[2222,None,None]),patch.object(installer.os,'kill') as kill:
                installer.stop_owned_app(home,executable)
                kill.assert_called_once_with(2222,installer.signal.SIGTERM)

    def test_signed_bundle_replacement_uses_fresh_inode_and_keeps_backup(self):
        if not (PROJECT/'build/VibeCue.app').exists():self.skipTest('Build app first')
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);built=PROJECT/'build/VibeCue.app';target=home/'Applications/VibeCue.app'
            installer.replace_app(built,target,home)
            original=(target/'Contents/MacOS/VibeCue').stat().st_ino
            installer.replace_app(built,target,home)
            self.assertNotEqual((target/'Contents/MacOS/VibeCue').stat().st_ino,original)
            self.assertTrue(list((home/'backups').glob('app-*.app')))
            self.assertFalse(list(target.parent.glob('.VibeCue-install-*.app')))

    def test_upgrade_removes_only_our_raw_permission_handler(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)
            codex=home/'.codex'
            codex.mkdir()
            control=home/'VibeCue/vibecuectl'
            own={'type':'command','command':installer.shlex.quote(str(control))+' hook','async':True,'timeout':3}
            other={'type':'command','command':'other-permission-handler'}
            config={'hooks':{'PermissionRequest':[{'matcher':'Bash','hooks':[own,other]},{'hooks':[own]}]}}
            (codex/'hooks.json').write_text(json.dumps(config))
            result=installer.install_hooks(codex,control,home/'VibeCue')
            after=json.loads((codex/'hooks.json').read_text())
            self.assertEqual(after['hooks']['PermissionRequest'],[{'matcher':'Bash','hooks':[other]}])
            self.assertNotIn('PermissionRequest',result['groups'])
            self.assertIn('PreToolUse',result['groups'])

    def test_install_merge_reinstall_and_preserve_original_notify(self):
        if not (PROJECT / 'build/VibeCue.app').exists():
            self.skipTest('Build app before running installer integration test')
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            user = Path(directory)
            codex = user / '.codex'
            codex.mkdir()
            config = b'notify = ["existing-helper", "turn-ended"]\n'
            (codex / 'config.toml').write_bytes(config)
            original = {'hooks': {'PermissionRequest': [{'hooks': [{'type': 'command', 'command': 'existing-hook'}]}]}}
            (codex / 'hooks.json').write_text(json.dumps(original))
            installer.install(start=False, user_home=user)
            installed = json.loads((codex / 'hooks.json').read_text())
            self.assertEqual(installed['hooks']['PermissionRequest'][0], original['hooks']['PermissionRequest'][0])
            self.assertEqual((codex / 'config.toml').read_bytes(), config)
            installer.install(start=False, user_home=user)
            self.assertEqual(json.loads((codex / 'hooks.json').read_text()), installed)
            installer.install(start=False, with_hooks=False, user_home=user)
            manifest = json.loads((user / 'Library/Application Support/VibeCue/install.json').read_text())
            self.assertIsNotNone(manifest['hooks'])
            self.assertEqual(manifest['labels'], ['local.vibecue'])
            self.assertFalse((user / 'Library/LaunchAgents/local.vibecue.watcher.plist').exists())
            self.assertEqual((codex / 'config.toml').read_bytes(), config)
            self.assertFalse(any('decision' in handler for group in manifest['hooks']['groups'].values()
                                 for handler in group['hooks']))


if __name__ == '__main__':
    unittest.main()
