"""User-selected history sync and numeric display cadence.

This file only stores scheduling preferences. Changing it never rewrites the
usage ledger, changes prices, or starts a scan on its own.
"""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from .events import vibecue_directory

DEFAULTS = {'auto_sync': True, 'sync_interval_seconds': 600,
            'display_interval_seconds': 30}
LIMITS = {'sync_interval_seconds': (30, 86400),
          'display_interval_seconds': (5, 3600)}
MAX_BYTES = 16 * 1024


class ScheduleError(ValueError):
    pass


def validate_patch(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ScheduleError('统计频率设置包含未知字段')
    result = {}
    for key, raw in value.items():
        if key == 'auto_sync':
            if not isinstance(raw, bool):
                raise ScheduleError('自动同步开关无效')
            result[key] = raw
        else:
            low, high = LIMITS[key]
            if (isinstance(raw, bool) or not isinstance(raw, (int, float)) or
                    not low <= raw <= high or raw != int(raw)):
                raise ScheduleError('同步或刷新间隔超出有效秒数范围')
            result[key] = int(raw)
    return result


def settings(home=None):
    prefs, warning = dict(DEFAULTS), None
    path = vibecue_directory(home) / 'usage-schedule.json'
    try:
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ScheduleError('统计频率设置文件过大')
        prefs.update(validate_patch(json.loads(raw)))
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, RecursionError):
        warning = '统计频率设置暂不可读，当前使用默认间隔；未修改原文件。'
    return {'schema_version': 1, 'prefs': prefs, 'settings_warning': warning}


def save(home, patch):
    home = vibecue_directory(home)
    checked = validate_patch(patch)
    prefs = settings(home)['prefs']
    prefs.update(checked)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.usage-schedule-', dir=home)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(prefs, stream, ensure_ascii=False, allow_nan=False)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, home / 'usage-schedule.json')
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return settings(home)


def main(argv=None):
    parser = argparse.ArgumentParser(description='VibeCoding Go 用量统计同步频率')
    parser.add_argument('command', choices=('settings', 'save'))
    parser.add_argument('--home')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == 'settings':
            result = settings(args.home)
        else:
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ScheduleError('统计频率设置过大')
            result = save(args.home, json.loads(raw))
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, RecursionError):
        print(json.dumps({'error': '统计频率设置未保存，请检查输入后重试。'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
