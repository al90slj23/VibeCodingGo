"""Report the verified CC Switch quota-sharing capability without borrowing auth.

CC Switch 4.0.21 obtains Codex OAuth quota through a Tauri command and keeps its
successful snapshots in an in-process UsageCache. Its proxy has no quota export
route. VibeCue therefore cannot treat its SQLite price/usage ledger as a live
account-quota feed. This adapter deliberately opens only the app's public plist;
it never opens a CC database, settings file, auth file, keychain or network API.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import plistlib

VALIDATED_VERSION = '4.0.21'
BUNDLE_ID = 'com.ccswitch.desktop'
SOURCE_BASE = 'https://github.com/farion1231/cc-switch/blob/v4.0.21/src-tauri/src/'
SOURCES = [SOURCE_BASE + name for name in (
    'commands/codex_oauth.rs', 'services/usage_cache.rs', 'proxy/server.rs')]


def status(app_paths=None, data_root=None):
    """Availability metadata only; an empty list is NOT proof of zero accounts."""
    candidates = app_paths if app_paths is not None else (
        Path('/Applications/CC Switch.app'), Path.home() / 'Applications/CC Switch.app')
    installed, version = False, None
    for raw in candidates:
        app = Path(raw)
        info = app / 'Contents/Info.plist'
        try:
            if info.stat().st_size > 65_536:
                continue
            with info.open('rb') as stream:
                value = plistlib.load(stream)
            if value.get('CFBundleIdentifier') != BUNDLE_ID:
                continue
            installed = True
            raw_version = value.get('CFBundleShortVersionString')
            if isinstance(raw_version, str) and len(raw_version) <= 40:
                version = raw_version
            break
        except (OSError, ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
            continue
    root = Path(data_root) if data_root is not None else Path.home() / '.cc-switch'
    try:
        database_present = (root / 'cc-switch.db').is_file()
    except OSError:
        database_present = False
    return {
        'schema_version': 1,
        'available': False,
        'status': 'unavailable',
        'reason': 'ccswitch_no_public_quota_feed',
        'source': 'CC Switch',
        'installed': installed,
        'installed_version': version,
        'validated_version': VALIDATED_VERSION,
        'database_present': database_present,
        'accounts': [],
        'account_count': None,
        'sources': list(SOURCES),
        'message': '当前未接通 CC Switch 的账号额度共享接口。已核对的 4.0.21 版本通过内部命令查询额度，'
                   '结果只保存在运行进程内，未提供可复用的本地额度接口。\n\n'
                   '本周已用比例和重置时间，请在 CC Switch 的授权中心查看。'
                   'VibeCue 不读取或复制它的登录 Token，也不发起新的账号授权。'
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description='CC Switch 已验证的账号额度共享状态')
    parser.add_argument('command', choices=('status',), nargs='?', default='status')
    parser.parse_args(argv)
    print(json.dumps(status(), ensure_ascii=False, separators=(',', ':')))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
