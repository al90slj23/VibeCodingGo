"""Local control and passive hook adapters for VibeCue."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .events import (vibecue_directory, classify_final_message, clean_text,
                     completion_event_id, emit_event, resolve_title)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.replace(temporary, path)


def _main_thread(thread_id, transcript=None):
    from .events import is_main_thread
    return is_main_thread(thread_id, transcript_path=transcript)


def handle_hook(payload):
    """Never return approval, denial, continuation, or model-visible content."""
    event = payload.get('hook_event_name', '')
    thread_id = str(payload.get('session_id') or '')
    turn_id = str(payload.get('turn_id') or '')
    if not thread_id or not _main_thread(thread_id, payload.get('transcript_path')):
        return
    health_path = vibecue_directory() / 'hook-health.json'
    try:
        health = json.loads(health_path.read_text())
    except (OSError, ValueError):
        health = {}
    health[event] = {'at': time.time(), 'thread_id': thread_id}
    atomic_json(health_path, health)
    if event == 'Stop':
        # The transcript task_complete event is more precise than Stop when
        # another hook continues the task. Leave completion to the watcher.
        return
    if event == 'PermissionRequest':
        # This is a *request*, not proof human approval remains pending. Auto
        # review can resolve it; the banner explicitly says to inspect Codex.
        tool = clean_text(payload.get('tool_name'), 64)
        request_key = str(payload.get('tool_use_id') or payload.get('call_id') or '')
        if not request_key:
            encoded = json.dumps(payload.get('tool_input', {}), sort_keys=True, ensure_ascii=False)
            # Do not store commands/arguments. Distinct requests in one turn
            # must not collapse; an identical later retry should also alert.
            request_key = hashlib.sha256(encoded.encode()).hexdigest() + ':' + str(int(time.time() / 5))
        emit_event('permission', thread_id, turn_id,
                   message='有權限請求，請查看 Codex 是否仍需要你批准' + (f' · {tool}' if tool else ''),
                   event_id=f'permission:{thread_id}:{turn_id}:{tool}:{request_key}',
                   source='PermissionRequest hook')
        return
    if event == 'PreToolUse':
        name = str(payload.get('tool_name') or '')
        if name.rsplit('.', 1)[-1].rsplit('__', 1)[-1] not in ('request_user_input', 'request_user_input_async'):
            return
        emit_event('input', thread_id, turn_id,
                   message='Codex 已提出問題，請回到對話回答或選擇',
                   event_id=f'input:{thread_id}:{turn_id}:{payload.get("tool_use_id", "")}',
                   source='PreToolUse hook')


def handle_notify(payload, chain=True):
    """Optional legacy notify bridge; installing Beacon does not replace notify."""
    if payload.get('type') == 'agent-turn-complete':
        thread_id = str(payload.get('thread-id') or '')
        turn_id = str(payload.get('turn-id') or '')
        if thread_id and _main_thread(thread_id):
            kind, message = classify_final_message(payload.get('last-assistant-message', ''))
            emit_event(kind, thread_id, turn_id, message=message,
                       event_id=completion_event_id(thread_id, turn_id), source='notify')
    if chain:
        config = vibecue_directory() / 'legacy-notify.json'
        try:
            command = json.loads(config.read_text())
            if isinstance(command, list) and command and all(isinstance(s, str) for s in command):
                subprocess.Popen(command + [json.dumps(payload, ensure_ascii=False)],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        except (OSError, ValueError):
            pass


def status():
    home = vibecue_directory()
    result = {'home': str(home)}
    for key, filename in [('app', 'app-status.json'), ('watcher', 'watcher-state.json'),
                          ('hooks_observed', 'hook-health.json'), ('install', 'install.json')]:
        try:
            result[key] = json.loads((home / filename).read_text())
        except (OSError, ValueError):
            result[key] = None
    if result.get('watcher'):
        result['watcher'].pop('cursors', None)
        result['watcher']['healthy'] = bool(result['watcher'].get('running')) and (
            time.time() - result['watcher'].get('heartbeat', 0) < 15) and (
            result['watcher'].get('read_error_count', 0) == 0)
    result['disabled'] = (home / 'disabled').exists()
    return result


def control(action):
    domain = f'gui/{os.getuid()}'
    labels = ('local.vibecue',)
    for label in labels:
        service = domain + '/' + label
        if action == 'stop':
            subprocess.run(['/bin/launchctl', 'bootout', service], capture_output=True)
        else:
            plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
            result = subprocess.run(['/bin/launchctl', 'print', service], capture_output=True)
            if result.returncode:
                subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist)], check=True)
            else:
                subprocess.run(['/bin/launchctl', 'kickstart', service], check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='VibeCue 本機對話提醒')
    sub = parser.add_subparsers(dest='command', required=True)
    watch = sub.add_parser('watch', help='被動觀察本機 Codex 對話')
    watch.add_argument('--interval', type=float, default=1.0)
    demo = sub.add_parser('test', help='測試畫面與聲音')
    demo.add_argument('kind', choices=('complete', 'input', 'permission', 'error'))
    demo.add_argument('--title', default='VibeCue · 提醒效果測試')
    demo.add_argument('--thread-id', default='')
    demo.add_argument('--message', default='這是一則測試提醒。')
    sub.add_parser('status', help='顯示本機程序與事件來源狀態')
    sub.add_parser('hook', help='Codex 的唯讀提醒 hook（JSON stdin）')
    notify = sub.add_parser('notify', help='可選的 notify 串接介面（JSON argument）')
    notify.add_argument('payload')
    sub.add_parser('stop', help='停止 App 與其監看程序')
    sub.add_parser('start', help='重新啟動 App 與其監看程序')
    args = parser.parse_args(argv)
    if args.command == 'watch':
        from .providers.codex import main as watch_main
        return watch_main(['--interval', str(max(0.25, args.interval))])
    if args.command == 'test':
        emit_event(args.kind, args.thread_id, title=args.title, message=args.message,
                   event_id=f'test:{time.time_ns()}', source='manual test')
    elif args.command == 'status':
        print(json.dumps(status(), ensure_ascii=False, indent=2))
    elif args.command == 'hook':
        try:
            payload = json.loads(sys.stdin.read(1_048_576))
            if isinstance(payload, dict):
                handle_hook(payload)
        except Exception:
            # A reminder must never disrupt the task or leak input to a log.
            pass
        print('{}')
    elif args.command == 'notify':
        try:
            handle_notify(json.loads(args.payload))
        except Exception:
            pass
    elif args.command in ('start', 'stop'):
        control(args.command)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
