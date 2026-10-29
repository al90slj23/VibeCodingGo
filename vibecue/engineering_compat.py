"""Finite, isolated feature probes before activating managed CLI updates.

Only the Hurl probe opens a socket, bound to loopback for its two synthetic
requests. Nothing runs at import or when a settings/status document is read.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import threading
import uuid

MAX_REPORT = 256 * 1024
MAX_ARCHES = 32
ARM64 = 0x0100000C


class CompatError(ValueError):
    """Fixed diagnostics only; tool output and inherited secrets never escape."""


def verify_arm64(binary):
    """Read only bounded Mach-O headers, accepting thin or universal arm64."""
    descriptor = None
    try:
        descriptor = os.open(binary, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 32:
            raise CompatError('工具不是有效的 arm64 macOS 可执行文件。')
        header = os.pread(descriptor, 32, 0)
        thin = {b'\xcf\xfa\xed\xfe': '<', b'\xfe\xed\xfa\xcf': '>'}

        def executable_at(offset, size):
            if size < 32 or offset < 0 or offset + size > metadata.st_size:
                return False
            value = os.pread(descriptor, 32, offset)
            endian = thin.get(value[:4])
            if endian is None or len(value) != 32:
                return False
            cpu, _, filetype = struct.unpack(endian + 'III', value[4:16])
            return cpu == ARM64 and filetype == 2

        if header[:4] in thin:
            if not executable_at(0, metadata.st_size):
                raise CompatError('工具不包含可执行的 arm64 架构。')
            return {'architecture': 'arm64', 'universal': False}
        formats = {b'\xca\xfe\xba\xbe': ('>', False), b'\xbe\xba\xfe\xca': ('<', False),
                   b'\xca\xfe\xba\xbf': ('>', True), b'\xbf\xba\xfe\xca': ('<', True)}
        if header[:4] not in formats:
            raise CompatError('工具不是受支持的 macOS 可执行文件。')
        endian, wide = formats[header[:4]]
        count = struct.unpack(endian + 'I', header[4:8])[0]
        if not 1 <= count <= MAX_ARCHES:
            raise CompatError('工具的 universal 架构表无效。')
        entry_size = 32 if wide else 20
        table_size = 8 + count * entry_size
        table = os.pread(descriptor, count * entry_size, 8)
        if len(table) != count * entry_size:
            raise CompatError('工具的 universal 架构表不完整。')
        found = False
        for index in range(count):
            entry = table[index * entry_size:(index + 1) * entry_size]
            cpu = struct.unpack(endian + 'I', entry[:4])[0]
            offset, size = struct.unpack(endian + ('QQ' if wide else 'II'), entry[8:24 if wide else 16])
            if offset < table_size or size < 32 or offset + size > metadata.st_size:
                raise CompatError('工具的 universal 架构范围无效。')
            if cpu == ARM64:
                if found or not executable_at(offset, size):
                    raise CompatError('工具的 arm64 架构内容无效。')
                found = True
        if not found:
            raise CompatError('工具不包含可执行的 arm64 架构。')
        return {'architecture': 'arm64', 'universal': True}
    except (OSError, ValueError, struct.error) as error:
        if isinstance(error, CompatError):
            raise
        raise CompatError('工具的 arm64 架构检查未通过。') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _environment(root):
    # Override every common configuration home and every proxy spelling. The
    # runner has its own narrow environment allowlist; never inherit API keys.
    return {'HOME': str(root), 'XDG_CONFIG_HOME': str(root / 'config'),
            'XDG_DATA_HOME': str(root / 'data'), 'XDG_CACHE_HOME': str(root / 'cache'),
            'XDG_STATE_HOME': str(root / 'state'), 'TMPDIR': str(root),
            'MISE_NO_CONFIG': '1', 'MISE_OFFLINE': '1', 'MISE_AUTO_INSTALL': 'false',
            'MISE_PARANOID': '1', 'TZ': 'UTC', 'LANG': 'C', 'LC_ALL': 'C', 'MISE_CACHE_DIR': str(root / 'mise-cache'),
            'MISE_STATE_DIR': str(root / 'mise-state'), 'NO_COLOR': '1',
            'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '',
            'http_proxy': '', 'https_proxy': '', 'all_proxy': '',
            'NO_PROXY': '*', 'no_proxy': '*'}


def _run(runner, argv, root, *, input=None, expected=0, timeout=5):
    result = runner.run(argv, input=input, timeout=timeout, max_output=MAX_REPORT,
                        cwd=str(root), env=_environment(root))
    if result.cancelled or getattr(runner, 'cancelled', False):
        raise CompatError('兼容性验证已取消，保留当前版本。')
    if result.timed_out or result.truncated or result.returncode != expected:
        raise CompatError('工具未通过有界兼容性验证，保留当前版本。')
    if not isinstance(result.stdout, bytes) or len(result.stdout) > MAX_REPORT:
        raise CompatError('工具兼容性报告无效，保留当前版本。')
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError, RecursionError):
        raise CompatError('工具的 JSON 接口不兼容，保留当前版本。') from None


def _mise(binary, runner, root):
    for name in ('node', 'python', 'go'):
        report = _run(runner, [str(binary), '--no-config', 'ls', '--installed', '--json', name], root, timeout=3)
        if (not isinstance(report, list) or len(report) > 1024 or
                any(not isinstance(row, dict) or not isinstance(row.get('version'), str) or
                    ('installed' in row and type(row['installed']) is not bool) for row in report)):
            raise CompatError('mise 已安装版本清单格式不兼容，保留当前版本。')
    return ['offline_inventory_json_node_python_go']


def _responses(report):
    pending = [(report, 0)]
    remaining = 4096
    while pending and remaining:
        value, depth = pending.pop(); remaining -= 1
        if depth > 15:
            continue
        if isinstance(value, dict):
            response = value.get('response')
            if isinstance(response, dict):
                status = response.get('status', response.get('status_code'))
                if type(status) is int:
                    yield status
            pending.extend((item, depth + 1) for item in list(value.values())[:1000] if isinstance(item, (dict, list)))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value[:1000] if isinstance(item, (dict, list)))


def _assertions(report):
    reports = report if isinstance(report, list) else [report]
    return [assertion for item in reports[:50] if isinstance(item, dict)
            for entry in item.get('entries', [])[:50] if isinstance(item.get('entries'), list) and isinstance(entry, dict)
            for assertion in entry.get('asserts', [])[:100] if isinstance(entry.get('asserts'), list) and isinstance(assertion, dict)]


def _hurl(binary, runner, root):
    nonce = uuid.uuid4().hex
    received = []
    paths = ['/vcgo-compat-' + nonce + '/pass', '/vcgo-compat-' + nonce + '/fail']
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(1)
        def do_GET(self):
            received.append(self.path)
            body = json.dumps({'ok': True, 'vcgo_probe': nonce}).encode()
            self.send_response(200 if self.path in paths else 404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    try:
        server = HTTPServer(('127.0.0.1', 0), Handler)
    except OSError:
        raise CompatError('本机回环验证暂不可用，保留当前版本。') from None
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.02), daemon=True)
    thread.start()
    try:
        for index, expected in enumerate((0, 4)):
            content = ('GET http://127.0.0.1:' + str(server.server_port) + paths[index] + '\nHTTP 200\n[Asserts]\n'
                       'jsonpath "$.vcgo_probe" == "' + nonce + '"\njsonpath "$.ok" == ' + ('true' if index == 0 else 'false') + '\n').encode()
            argv = [str(binary), '--json', '--test', '--jobs', '1', '--retry', '0', '--max-redirs', '0',
                    '--max-time', '3000ms', '--connect-timeout', '1000ms', '--max-filesize', str(MAX_REPORT),
                    '--limit-rate', str(MAX_REPORT), '--no-color']
            report = _run(runner, argv, root, input=content, expected=expected, timeout=4)
            if not isinstance(report, (dict, list)) or list(_responses(report)) != [200]:
                raise CompatError('Hurl 响应报告格式不兼容，保留当前版本。')
            try:
                assertions = _assertions(report)
            except (TypeError, ValueError):
                raise CompatError('Hurl 断言报告格式不兼容，保留当前版本。') from None
            if (not assertions or any(type(item.get('success')) is not bool for item in assertions) or
                    (index == 0 and not all(item['success'] for item in assertions)) or
                    (index == 1 and not any(item['success'] is False for item in assertions))):
                raise CompatError('Hurl 请求或断言接口不兼容，保留当前版本。')
        if received != paths:
            raise CompatError('Hurl 未完成预期的两次本机请求，保留当前版本。')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=1.5)
    return ['local_http_response_json', 'assertion_success_and_exit4_failure', 'exactly_two_local_requests']


def _lnav(binary, runner, root):
    marker = 'vcgo-compat-' + uuid.uuid4().hex
    line = '2026-10-30T00:00:00Z ERROR ' + marker
    log = root / 'compat.log'; log.write_text(line + '\n')
    sql = ';SELECT log_path,log_time,log_level,log_text FROM all_logs ORDER BY log_line LIMIT 1000'
    report = _run(runner, [str(binary), '-n', '-N', '-q', '-c', sql, '-c', ':write-json-to -', str(log)], root, timeout=5)
    if not isinstance(report, list) or not 1 <= len(report) <= 1000:
        raise CompatError('lnav 固定 SQL 报告格式不兼容，保留当前版本。')
    matched = False
    for row in report:
        if (not isinstance(row, dict) or not isinstance(row.get('log_path'), str) or
                not isinstance(row.get('log_text'), str)):
            raise CompatError('lnav 日志字段不兼容，保留当前版本。')
        if row['log_path'] == str(log) and row['log_text'] == line:
            stamp, level = row.get('log_time'), row.get('log_level')
            matched = (isinstance(stamp, str) and re.match(r'^2026-10-30[ T]00:00:00', stamp) is not None and
                       isinstance(level, str) and level.lower() in ('error', 'err'))
    if not matched:
        raise CompatError('lnav 未识别验证日志的时间与级别，保留当前版本。')
    return ['headless_fixed_sql_json', 'source_text_timestamp_error_level']


def verify_compatibility(name, binary, runner):
    """Run only after source, archive, binary version and architecture checks.

    The caller owns a finite runner/deadline/cancellation scope. The helper
    creates no new runner, service, persistent configuration or global state.
    """
    probes = {'mise': _mise, 'hurl': _hurl, 'lnav': _lnav}
    if name not in probes or not Path(binary).is_absolute():
        raise CompatError('未知的工程工具兼容性验证。')
    try:
        with tempfile.TemporaryDirectory(prefix='vcgo-engine-compat-') as directory:
            root = Path(directory).resolve()
            checks = probes[name](Path(binary), runner, root)
            return {'schema_version': 1, 'probe_version': 1, 'compatible': True, 'tool': name, 'checks': checks}
    except CompatError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError, RecursionError):
        raise CompatError('工具兼容性验证未完成，保留当前版本。') from None
