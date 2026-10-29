#!/usr/bin/env python3
"""Install VibeCoding Go and migrate the existing VibeCue application safely."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

PROJECT = Path(__file__).resolve().parent.parent
LABELS = ('local.vibecue',)
OWNED_LABELS = frozenset(('local.vibecue', 'local.vibecue.watcher'))
APP_NAME = 'VibeCoding Go.app'
EXECUTABLE = 'VCGo'
LEGACY_APP_NAME = 'VibeCue.app'
LEGACY_EXECUTABLE = 'VibeCue'
BUNDLE_IDENTIFIER = 'local.vibecue'  # Preserve macOS identity and existing permissions.


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.vibecue-tmp')
    with temporary.open('wb') as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
    os.replace(temporary, path)


def json_write(path, data):
    private_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())


def owned_pid(home, executable):
    """The app can outlive its launchd job after Launch Services reopens it."""
    try:
        pid = int(json.loads((home / 'app-status.json').read_text())['pid'])
        result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'uid=', '-o', 'comm='],
                                capture_output=True, text=True)
        parts = result.stdout.strip().split(None, 1)
        if (pid > 1 and result.returncode == 0 and len(parts) == 2 and
                int(parts[0]) == os.getuid() and parts[1] == str(executable)):
            return pid
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def stop_owned_app(home, executable):
    pid = owned_pid(home, executable)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and owned_pid(home, executable) == pid:
        time.sleep(.1)
    if owned_pid(home, executable) == pid:
        os.kill(pid, signal.SIGKILL)


def owned_bundle(app, manifest, executable):
    """Only a recorded, exact-path application may be replaced or archived."""
    if manifest.get('app') != str(app) or app.is_symlink() or not app.is_dir():
        return False
    labels = manifest.get('labels')
    if not isinstance(labels, list) or 'local.vibecue' not in labels:
        return False
    try:
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        binary = app / 'Contents/MacOS' / executable
        return (info.get('CFBundleIdentifier') == BUNDLE_IDENTIFIER and
                info.get('CFBundleExecutable') == executable and
                binary.is_file() and not binary.is_symlink())
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        return False


def archive_legacy_app(app, manifest, home):
    if not owned_bundle(app, manifest, LEGACY_EXECUTABLE):
        return None
    backup = home / 'backups'
    backup.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    destination = backup / ('app-legacy-VibeCue-' + stamp + '.app')
    shutil.move(str(app), str(destination))
    return str(destination)


def replace_app(built, app, home):
    """Use fresh inodes; overwriting signed executable pages breaks macOS launches."""
    app.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
    staging = app.parent / ('.VCGo-install-' + stamp + '.app')
    previous = app.parent / ('.VCGo-previous-' + stamp + '.app')
    try:
        shutil.copytree(built, staging)
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(staging)], check=True)
        if app.exists():
            app.rename(previous)
        try:
            staging.rename(app)
        except OSError:
            if previous.exists() and not app.exists():
                previous.rename(app)
            raise
        if previous.exists():
            backup = home / 'backups'
            backup.mkdir(mode=0o700, exist_ok=True)
            shutil.move(str(previous), str(backup / ('app-' + stamp + '.app')))
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def hook_groups(control_path):
    handler = {'type': 'command', 'command': shlex.quote(str(control_path)) + ' hook',
               'timeout': 3, 'async': True}
    return {
        'PreToolUse': {'matcher': r'(^|.*[._])request_user_input(_async)?$',
                       'hooks': [handler.copy()]},
    }


def install_hooks(codex_home, control_path, home):
    target = codex_home / 'hooks.json'
    original = target.read_bytes() if target.exists() else None
    config = json.loads(original) if original is not None else {'hooks': {}}
    if not isinstance(config, dict) or not isinstance(config.get('hooks', {}), dict):
        raise RuntimeError('Existing hooks.json has an unexpected shape; left unchanged.')
    hooks = config.setdefault('hooks', {})
    # Retire only VibeCue's old raw approval callback. Preserve handlers from
    # other tools, including those sharing a matcher group with ours.
    own_command = shlex.quote(str(control_path)) + ' hook'
    permission_groups = hooks.get('PermissionRequest', [])
    if not isinstance(permission_groups, list):
        raise RuntimeError('Existing hook event is not a list; left unchanged.')
    retained = []
    for group in permission_groups:
        if isinstance(group, dict) and isinstance(group.get('hooks'), list):
            handlers = [h for h in group['hooks'] if not (
                isinstance(h, dict) and h.get('type') == 'command' and h.get('command') == own_command)]
            if handlers:
                retained.append(dict(group, hooks=handlers))
        else:
            retained.append(group)
    if permission_groups:
        hooks['PermissionRequest'] = retained
    groups = hook_groups(control_path)
    for event, group in groups.items():
        current = hooks.setdefault(event, [])
        if not isinstance(current, list):
            raise RuntimeError('Existing hook event is not a list; left unchanged.')
        if group not in current:
            current.append(group)
    new = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode()
    if new != original:
        if original is not None:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            private_write(home / 'backups' / ('hooks-' + stamp + '.json'), original)
        private_write(target, new)
    return {'file': str(target), 'groups': groups, 'trust_required': True}


def install(start=True, with_hooks=True, user_home=None):
    isolated = user_home is not None
    user_home = Path(user_home or Path.home()).resolve()
    home = user_home / 'Library/Application Support/VibeCue'
    previous = json.loads((home / 'install.json').read_text()) if (home / 'install.json').exists() else {}
    applications = user_home / 'Applications' if isolated else Path('/Applications')
    app = applications / APP_NAME
    legacy_app = applications / LEGACY_APP_NAME
    agents = user_home / 'Library/LaunchAgents'
    built = PROJECT / 'build' / APP_NAME
    if not (built / 'Contents/MacOS' / EXECUTABLE).exists():
        raise RuntimeError('Run scripts/build.sh first.')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(built)], check=True)
    if app.exists() and not owned_bundle(app, previous, EXECUTABLE):
        raise RuntimeError('An unrelated VibeCoding Go.app already exists; refusing to overwrite it.')
    legacy_manifest = previous
    if (previous.get('app') == str(app) and
            previous.get('legacy_app_pending') == str(legacy_app)):
        legacy_manifest = dict(previous, app=str(legacy_app))
    legacy_owned = owned_bundle(legacy_app, legacy_manifest, LEGACY_EXECUTABLE)
    record = {'version': '0.29.0', 'product_name': 'VibeCoding Go', 'short_name': 'VCGo',
              'app': str(app), 'home': str(home), 'hooks': previous.get('hooks'),
              'source_dir': str(PROJECT), 'labels': list(LABELS),
              'installed_at': datetime.now(timezone.utc).isoformat()}
    if legacy_owned:
        record['legacy_app_pending'] = str(legacy_app)
    if previous.get('legacy_app_backup'):
        record['legacy_app_backup'] = previous['legacy_app_backup']
    domain = f'gui/{os.getuid()}'
    if start:
        for label in set(LABELS) | (set(previous.get('labels', [])) & OWNED_LABELS):
            subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True)
        if owned_bundle(app, previous, EXECUTABLE):
            stop_owned_app(home, app / 'Contents/MacOS' / EXECUTABLE)
        if legacy_owned:
            stop_owned_app(home, legacy_app / 'Contents/MacOS' / LEGACY_EXECUTABLE)
    for label in set(previous.get('labels', [])) & (OWNED_LABELS - set(LABELS)):
        (agents / (label + '.plist')).unlink(missing_ok=True)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    for name in ('events', 'inflight', 'logs', 'runtime'):
        (home / name).mkdir(exist_ok=True, mode=0o700)
    runtime = home / 'runtime'
    for cache in (runtime / 'vibecue').rglob('__pycache__'):
        shutil.rmtree(cache)
    shutil.copytree(PROJECT / 'vibecue', runtime / 'vibecue', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    replace_app(built, app, home)
    # Establish ownership as soon as replacement commits. Any later optional
    # setup/archive failure must remain safely retryable using this manifest.
    json_write(home / 'install.json', record)
    # The original helper path remains stable for existing Codex hooks.
    control = home / 'vibecuectl'
    launcher = ('#!/bin/sh\nset -eu\nexport PYTHONPATH=' + shlex.quote(str(runtime)) + '\n'
                'export VIBECUE_HOME=' + shlex.quote(str(home)) + '\n'
                'exec /usr/bin/python3 -m vibecue.cli "$@"\n')
    for command in (home / 'vcgo', control):
        private_write(command, launcher.encode())
        os.chmod(command, 0o700)
    bridge_wrapper = home / 'vibecue-codex'
    bridge_launcher = ('#!/bin/sh\nset -eu\nexport PYTHONPATH=' + shlex.quote(str(runtime)) + '\n'
                      'export VIBECUE_HOME=' + shlex.quote(str(home)) + '\n'
                      'exec /usr/bin/python3 -m vibecue.providers.approval_bridge "$@"\n')
    private_write(bridge_wrapper, bridge_launcher.encode())
    os.chmod(bridge_wrapper, 0o700)
    env = {'VIBECUE_HOME': str(home), 'PYTHONPATH': str(runtime),
           'PYTHONUNBUFFERED': '1', 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin'}
    jobs = [
        {'Label': LABELS[0], 'ProgramArguments': [str(app / 'Contents/MacOS' / EXECUTABLE), '--background'],
         'KeepAlive': {'SuccessfulExit': False}, 'ProcessType': 'Interactive'},
    ]
    for job in jobs:
        job.update({'RunAtLoad': True, 'LimitLoadToSessionType': 'Aqua',
                    'EnvironmentVariables': env, 'WorkingDirectory': str(runtime),
                    'ThrottleInterval': 10,
                    'StandardOutPath': str(home / 'logs' / (job['Label'] + '.out.log')),
                    'StandardErrorPath': str(home / 'logs' / (job['Label'] + '.err.log'))})
        private_write(agents / (job['Label'] + '.plist'), plistlib.dumps(job))
    # Preserve config.toml and the existing computer-use notify command verbatim.
    hooks = install_hooks(user_home / '.codex', control, home) if with_hooks else previous.get('hooks')
    record['hooks'] = hooks
    json_write(home / 'install.json', record)
    legacy_backup = archive_legacy_app(legacy_app, legacy_manifest, home) if legacy_owned else None
    if legacy_backup:
        record['legacy_app_backup'] = legacy_backup
    record.pop('legacy_app_pending', None)
    json_write(home / 'install.json', record)
    # Only migrate machines that already opted into the verified local bridge.
    # No CC Switch auth/config files are opened by this bootstrap.
    if (home / 'ccswitch-quota-install.json').is_file():
        sys.path.insert(0, str(PROJECT))
        from vibecue.ccswitch_maintenance import configure_existing
        configure_existing(home, PROJECT / 'build/ccswitch-maintenance',
                           seed_app=PROJECT / 'build/ccswitch-bridge-target/release/bundle/macos/CC Switch.app',
                           template_file=app / 'Contents/Resources/CCSwitchIntegration/vibecue_quota.rs')
    if start:
        for label in LABELS:
            subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(agents / (label + '.plist'))], check=True)
            # A successful bootstrap can leave a newly replaced interactive job
            # registered but not started yet. Explicitly start this owned job.
            subprocess.run(['/bin/launchctl', 'kickstart', domain + '/' + label], check=True)
    print(json.dumps(record, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--without-hooks', action='store_true')
    parser.add_argument('--user-home', type=Path, help='Isolated test destination; requires --no-start')
    args = parser.parse_args()
    if args.user_home and not args.no_start:
        parser.error('--user-home requires --no-start')
    install(not args.no_start, not args.without_hooks, args.user_home)


if __name__ == '__main__':
    main()
