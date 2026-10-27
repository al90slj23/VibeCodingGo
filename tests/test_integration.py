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
    def test_passive_permission_hook_does_not_collapse_distinct_requests(self):
        payload = {'hook_event_name': 'PermissionRequest', 'session_id': 'parent',
                   'turn_id': 't1', 'tool_name': 'Bash', 'tool_input': {'command': 'first'}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'VIBECUE_HOME': directory}), \
                patch.object(cli, '_main_thread', return_value=True), patch.object(cli, 'emit_event') as emit:
            cli.handle_hook(payload)
            payload['tool_input'] = {'command': 'second'}
            cli.handle_hook(payload)
            self.assertEqual(emit.call_count, 2)
            first, second = [call.kwargs for call in emit.call_args_list]
            self.assertNotEqual(first['event_id'], second['event_id'])
            self.assertNotIn('first', first['event_id'])
            self.assertEqual(emit.call_args_list[0].args[0], 'permission')

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
