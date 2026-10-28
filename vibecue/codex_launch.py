"""Opt-in startup for the current Codex CLI override, without changing permissions."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import plistlib
import subprocess
import time

from .events import vibecue_directory


def application_directories():
    return (Path('/Applications'), Path.home() / 'Applications')


def find_codex_app():
    # Match approval_bridge._find_real_cli's supported signed-bundle layouts.
    # CODEX_CLI_PATH can point back to our wrapper and must never be followed.
    for directory in application_directories():
        for name in ('ChatGPT.app', 'Codex.app'):
            path = directory / name
            try:
                info = plistlib.loads((path / 'Contents/Info.plist').read_bytes())
                executable_name = info['CFBundleExecutable']
                if not isinstance(executable_name, str) or Path(executable_name).name != executable_name:
                    continue
                executable = path / 'Contents/MacOS' / executable_name
                if not executable.is_file() or not os.access(executable, os.X_OK):
                    continue
                for relative in ('codex-cli/CodexCLI.app/Contents/MacOS/codex',
                                 'codex-cli/bin/codex', 'codex'):
                    cli = path / 'Contents/Resources' / relative
                    if cli.is_file() and os.access(cli, os.X_OK):
                        return path, executable, cli
            except (OSError, ValueError, KeyError, TypeError):
                continue
    raise RuntimeError('找不到包含 Codex CLI 的官方桌面 App。')


def app_running(executable):
    result = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True, text=True, check=True)
    return any(line.strip() == str(executable) for line in result.stdout.splitlines())


def write_launch_status(home, status, message, **details):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = home / 'bridge-launch-state.json'
    temporary = path.with_name(path.name + f'.tmp-{os.getpid()}')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump({'status': status, 'message': message, 'updated_at': time.time(), 'pid': os.getpid(), **details},
                  stream, ensure_ascii=False)
    os.replace(temporary, path)


def launcher_parent():
    """Bind a UI-owned waiter to its original kernel parent relationship.

    A parent's PID being reused is insufficient: after the original app exits,
    macOS reparents this child, so getppid() no longer matches that instance.
    Manual CLI calls without VIBECUE_PARENT_PID remain explicitly standalone.
    """
    value = os.environ.get('VIBECUE_PARENT_PID')
    if value is None:
        return None
    try:
        parent = int(value)
    except (ValueError, TypeError):
        raise RuntimeError('连接启动来源无效，请从 VibeCue 重新连接。') from None
    if parent <= 1 or os.getppid() != parent:
        raise RuntimeError('原 VibeCue 实例已退出，请从当前 VibeCue 重新连接。')
    return parent


def launch_codex(wait_for_exit=False, home=None, timeout=None):
    home = vibecue_directory(home)
    try:
        parent = launcher_parent()
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
            raise RuntimeError('等待退出的时限无效。')
        app, executable, _ = find_codex_app()
        wrapper = home / 'vibecue-codex'
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            raise RuntimeError('人工批准监听入口尚未安装，请先更新 VibeCue。')
        if app_running(executable):
            if not wait_for_exit:
                raise RuntimeError('请先正常退出 Codex，再通过 VibeCue 启动，以连接真实的人工批准请求。')
            message = '已准备连接。请先完成当前任务并退出 Codex；退出后会自动重新打开。'
            write_launch_status(home, 'waiting_for_codex_exit', message, parent_pid=parent)
            deadline = None if timeout is None else time.monotonic() + timeout
            heartbeat = time.monotonic()
            while app_running(executable):
                if parent is not None and os.getppid() != parent:
                    write_launch_status(home, 'cancelled', '原 VibeCue 实例已退出，已取消等待；不会自动打开 Codex。')
                    return 0
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise RuntimeError('等待退出已超时。准备好后可再次点击连接。')
                if now - heartbeat >= 10:
                    write_launch_status(home, 'waiting_for_codex_exit', message, parent_pid=parent)
                    heartbeat = now
                time.sleep(1)
        if parent is not None and os.getppid() != parent:
            write_launch_status(home, 'cancelled', '原 VibeCue 实例已退出，已取消连接；不会自动打开 Codex。')
            return 0
        write_launch_status(home, 'launching', '正在通过人工批准监听入口打开 Codex。')
        subprocess.run(['/usr/bin/open', '--env', 'CODEX_CLI_PATH=' + str(wrapper),
                        '--env', 'VIBECUE_HOME=' + str(home), str(app)], check=True)
        write_launch_status(home, 'launched', 'Codex 已启动；收到实际请求流后会显示为已连接。')
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        write_launch_status(home, 'error', str(error))
        raise
