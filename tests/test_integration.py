from contextlib import redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
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
        if not (PROJECT/'build/VibeCoding Go.app').exists():self.skipTest('Build app first')
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory);built=PROJECT/'build/VibeCoding Go.app';target=home/'Applications/VibeCoding Go.app'
            installer.replace_app(built,target,home)
            original=(target/'Contents/MacOS/VCGo').stat().st_ino
            installer.replace_app(built,target,home)
            self.assertNotEqual((target/'Contents/MacOS/VCGo').stat().st_ino,original)
            self.assertTrue(list((home/'backups').glob('app-*.app')))
            self.assertFalse(list(target.parent.glob('.VCGo-install-*.app')))

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
        if not (PROJECT / 'build/VibeCoding Go.app').exists():
            self.skipTest('Build app before running installer integration test')
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            user = Path(directory).resolve()
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
            self.assertEqual(manifest['product_name'], 'VibeCoding Go')
            self.assertEqual(manifest['app'], str(user / 'Applications/VibeCoding Go.app'))
            self.assertIsNotNone(manifest['hooks'])
            self.assertEqual(manifest['labels'], ['local.vibecue'])
            self.assertFalse((user / 'Library/LaunchAgents/local.vibecue.watcher.plist').exists())
            self.assertEqual((codex / 'config.toml').read_bytes(), config)
            self.assertFalse(any('decision' in handler for group in manifest['hooks']['groups'].values()
                                 for handler in group['hooks']))


class BrandMigrationTests(unittest.TestCase):
    """Use disposable stub bundles, never the build or installed user app."""
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.project = self.root / 'source'
        self.user = self.root / 'user'
        self.home = self.user / 'Library/Application Support/VibeCue'
        self.home.mkdir(parents=True)
        (self.project / 'vibecue').mkdir(parents=True)
        (self.project / 'vibecue/__init__.py').write_text('')
        self.built = self.project / 'build/VibeCoding Go.app'
        self.new = self.user / 'Applications/VibeCoding Go.app'
        self.old = self.user / 'Applications/VibeCue.app'
        self.bundle(self.built, 'VCGo')
        project = patch.object(installer, 'PROJECT', self.project)
        project.start(); self.addCleanup(project.stop)
        commands = patch.object(installer.subprocess, 'run')
        self.commands = commands.start(); self.addCleanup(commands.stop)
        self.commands.return_value = type('Result', (), {'returncode': 0})()

    def bundle(self, target, executable, identifier='local.vibecue'):
        (target / 'Contents/MacOS').mkdir(parents=True)
        (target / 'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier': identifier, 'CFBundleExecutable': executable}))
        (target / 'Contents/MacOS' / executable).write_bytes(b'stub app')

    def manifest(self, app):
        value = {'version': '0.25.1', 'app': str(app), 'labels': ['local.vibecue']}
        (self.home / 'install.json').write_text(json.dumps(value))
        return value

    def install(self, **kwargs):
        with redirect_stdout(io.StringIO()):
            installer.install(start=False, with_hooks=False, user_home=self.user, **kwargs)
        return json.loads((self.home / 'install.json').read_text())

    def test_upgrade_archives_only_recorded_legacy_bundle_and_preserves_data(self):
        self.bundle(self.old, 'VibeCue')
        self.manifest(self.old)
        original = {'settings.json': b'{"enabled":true}',
                    'conversation-drafts.json': '草稿内容'.encode(),
                    'task-recovery.json': b'ongoing task',
                    'usage.sqlite': b'historical usage'}
        for name, content in original.items():
            (self.home / name).write_bytes(content)
        record = self.install()
        self.assertEqual(record['app'], str(self.new))
        self.assertEqual(record['version'], '0.28.0')
        self.assertEqual(record['short_name'], 'VCGo')
        self.assertFalse(self.old.exists())
        backup = Path(record['legacy_app_backup'])
        self.assertEqual((backup / 'Contents/MacOS/VibeCue').read_bytes(), b'stub app')
        self.assertTrue(self.new.is_dir())
        for name, content in original.items():
            self.assertEqual((self.home / name).read_bytes(), content)
        for name in ('vcgo', 'vibecuectl', 'vibecue-codex'):
            self.assertTrue(os.access(self.home / name, os.X_OK))
        job = plistlib.loads((self.user / 'Library/LaunchAgents/local.vibecue.plist').read_bytes())
        self.assertEqual(job['ProgramArguments'], [str(self.new / 'Contents/MacOS/VCGo'), '--background'])
        self.assertEqual(job['EnvironmentVariables']['VIBECUE_HOME'], str(self.home))
        repeated = self.install()
        self.assertEqual(repeated['legacy_app_backup'], str(backup))
        self.assertEqual(len(list((self.home / 'backups').glob('app-legacy-*.app'))), 1)
        self.assertTrue(list((self.home / 'backups').glob('app-20*.app')))

    def test_unrelated_old_bundle_is_preserved_even_with_an_old_path_in_manifest(self):
        self.bundle(self.old, 'VibeCue', identifier='other.application')
        self.manifest(self.old)
        record = self.install()
        self.assertTrue(self.old.is_dir())
        self.assertNotIn('legacy_app_backup', record)

    def test_unrecorded_legacy_bundle_is_not_claimed_by_matching_bundle_identifier(self):
        self.bundle(self.old, 'VibeCue')
        self.install()
        self.assertTrue(self.old.is_dir())

    def test_unrelated_new_target_is_rejected_before_startup_or_data_mutation(self):
        self.bundle(self.old, 'VibeCue')
        self.manifest(self.old)
        self.bundle(self.new, 'VCGo')
        original = (self.home / 'install.json').read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'unrelated'):
            self.install()
        self.assertTrue(self.old.is_dir())
        self.assertEqual((self.home / 'install.json').read_bytes(), original)
        self.assertFalse((self.home / 'runtime').exists())

    def test_upgrade_stops_verified_old_executable_before_new_launch(self):
        self.bundle(self.old, 'VibeCue')
        self.manifest(self.old)
        with patch.object(installer, 'stop_owned_app') as stop, redirect_stdout(io.StringIO()):
            installer.install(start=True, with_hooks=False, user_home=self.user)
        stop.assert_called_once_with(self.home, self.old / 'Contents/MacOS/VibeCue')
        calls = [call.args[0] for call in self.commands.call_args_list]
        self.assertTrue(any(command[:2] == ['/bin/launchctl', 'bootout'] for command in calls))
        self.assertTrue(any(command[:2] == ['/bin/launchctl', 'bootstrap'] for command in calls))

    def test_optional_ccswitch_failure_leaves_new_app_owned_and_retryable(self):
        self.bundle(self.old, 'VibeCue')
        self.manifest(self.old)
        (self.home / 'ccswitch-quota-install.json').write_text('{}')
        with patch('vibecue.ccswitch_maintenance.configure_existing', side_effect=RuntimeError('optional setup failed')):
            with self.assertRaisesRegex(RuntimeError, 'optional setup failed'):
                self.install()
        record = json.loads((self.home / 'install.json').read_text())
        self.assertEqual(record['app'], str(self.new))
        self.assertTrue(installer.owned_bundle(self.new, record, 'VCGo'))
        self.assertFalse(self.old.exists())
        with patch('vibecue.ccswitch_maintenance.configure_existing'):
            self.assertEqual(self.install()['app'], str(self.new))

    def test_interrupted_legacy_archive_retains_verified_pending_migration_for_retry(self):
        self.bundle(self.old, 'VibeCue')
        self.manifest(self.old)
        with patch.object(installer, 'archive_legacy_app', side_effect=OSError('temporary archive failure')):
            with self.assertRaisesRegex(OSError, 'temporary archive failure'):
                self.install()
        record = json.loads((self.home / 'install.json').read_text())
        self.assertEqual(record['legacy_app_pending'], str(self.old))
        self.assertTrue(installer.owned_bundle(self.new, record, 'VCGo'))
        self.assertTrue(self.old.exists())
        recovered = self.install()
        self.assertNotIn('legacy_app_pending', recovered)
        self.assertFalse(self.old.exists())
        self.assertTrue(Path(recovered['legacy_app_backup']).exists())


if __name__ == '__main__':
    unittest.main()
