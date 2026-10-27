"""Opt-in startup for the current Codex CLI override, without changing permissions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import plistlib
import subprocess
import time

from .events import vibecue_directory


def find_codex_app():
    for path in (Path('/Applications/ChatGPT.app'), Path('/Applications/Codex.app')):
        try:
            info = plistlib.loads((path / 'Contents/Info.plist').read_bytes())
            executable = path / 'Contents/MacOS' / info['CFBundleExecutable']
            cli = path / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
            if executable.is_file() and cli.is_file():
                return path, executable, cli
        except (OSError, ValueError, KeyError, TypeError):
            continue
    raise RuntimeError('找不到包含 Codex CLI 的官方桌面 App。')


def app_running(executable):
    result = subprocess.run(['/bin/ps', '-axo', 'comm='], capture_output=True, text=True, check=True)
    return any(line.strip() == str(executable) for line in result.stdout.splitlines())


def write_launch_status(home, status, message):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = home / 'bridge-launch-state.json'
    temporary = path.with_name(path.name + f'.tmp-{os.getpid()}')
    with temporary.open('w', encoding='utf-8') as stream:
        os.chmod(temporary, 0o600)
        json.dump({'status': status, 'message': message, 'updated_at': time.time(), 'pid': os.getpid()},
                  stream, ensure_ascii=False)
    os.replace(temporary, path)


def launch_codex(wait_for_exit=False, home=None, timeout=1800):
    home = vibecue_directory(home)
    try:
        app, executable, _ = find_codex_app()
        wrapper = home / 'vibecue-codex'
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            raise RuntimeError('人工批准监听入口尚未安装，请先更新 VibeCue。')
        if app_running(executable):
            if not wait_for_exit:
                raise RuntimeError('请先正常退出 Codex，再通过 VibeCue 启动，以连接真实的人工批准请求。')
            write_launch_status(home, 'waiting_for_codex_exit',
                                '已准备连接。请先完成当前任务并退出 Codex；退出后会自动重新打开。')
            deadline = time.monotonic() + timeout
            while app_running(executable):
                if time.monotonic() >= deadline:
                    raise RuntimeError('等待退出已超时。准备好后可再次点击连接。')
                time.sleep(1)
        write_launch_status(home, 'launching', '正在通过人工批准监听入口打开 Codex。')
        subprocess.run(['/usr/bin/open', '--env', 'CODEX_CLI_PATH=' + str(wrapper),
                        '--env', 'VIBECUE_HOME=' + str(home), str(app)], check=True)
        write_launch_status(home, 'launched', 'Codex 已启动；收到实际请求流后会显示为已连接。')
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        write_launch_status(home, 'error', str(error))
        raise
