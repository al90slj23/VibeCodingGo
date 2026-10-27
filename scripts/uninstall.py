#!/usr/bin/env python3
"""Unload VibeCue and remove only its own hooks/startup entries; keep local data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install import json_write


def main():
    home = Path.home() / 'Library/Application Support/VibeCue'
    record = json.loads((home / 'install.json').read_text())
    domain = f'gui/{os.getuid()}'
    for label in record['labels']:
        if label not in ('local.vibecue', 'local.vibecue.watcher'):
            raise RuntimeError('Unexpected service label')
        subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True)
        (Path.home() / 'Library/LaunchAgents' / (label + '.plist')).unlink(missing_ok=True)
    hooks = record.get('hooks')
    if hooks:
        path = Path(hooks['file'])
        if path.exists():
            config = json.loads(path.read_text())
            for event, group in hooks['groups'].items():
                groups = config.get('hooks', {}).get(event, [])
                if group in groups:
                    groups.remove(group)
            json_write(path, config)
    print('VibeCue 已停用；应用程序与本机数据保留，可自行删除。')


if __name__ == '__main__':
    main()
