"""Explicit, finite HTTP requests and Hurl collections for Engineering Tools.

Input and response bodies stay in this process. Sensitive request values never
enter argv or a report file. Importing this module starts no tool or network I/O.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

from .engineering_runtime import EngineeringError, Runtime, redact

MAX_BODY = 128 * 1024
MAX_HEADERS = 32 * 1024
MAX_COLLECTION = 128 * 1024
MAX_RESPONSE = 256 * 1024
MAX_PREVIEW = 64 * 1024
METHODS = {'GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'}
SENSITIVE = re.compile(r'(?i)(?:authorization|cookie|api[-_]?key|access[-_]?token|refresh[-_]?token|password|passwd|secret|credential|(?:^|[-_])(?:auth[-_]?)?token$|(?:^|[-_])auth$|^key$|captures|variables)')
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
REQUEST_LINE = re.compile(r'^([A-Z]+)\s+(https?://\S+)\s*$')
SAFE_HURL_OPTIONS = {'max-time', 'connect-timeout', 'compressed', 'http1.0', 'http1.1',
                     'http2', 'ipv4', 'ipv6', 'fail-with-body'}


def _string(value, label, maximum, *, default=''):
    value = default if value is None else value
    if not isinstance(value, str) or len(value.encode('utf-8')) > maximum or '\x00' in value:
        raise EngineeringError(label + '无效或超过大小上限。')
    return value


def _url(value):
    value = _string(value, '请求地址', 8192).strip()
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or
                parsed.password is not None or parsed.fragment or any(c.isspace() or ord(c) < 32 for c in value)):
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
    except (ValueError, UnicodeError):
        raise EngineeringError('请填写 HTTP 或 HTTPS 地址；账号信息请放在请求头，地址不支持账号密码或片段。') from None
    return value


def _timeout(value):
    if value is None:
        return 15.
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 1 <= value <= 30:
        raise EngineeringError('请求时限应为 1–30 秒。')
    return float(value)


def _headers(value):
    raw = _string(value, '请求头', MAX_HEADERS)
    result = []
    for line in raw.replace('\r\n', '\n').split('\n'):
        if not line.strip():
            continue
        key, colon, field = line.partition(':')
        key, field = key.strip(), field.strip()
        if not colon or not HEADER_NAME.fullmatch(key) or '\r' in field or any(ord(c) < 32 and c != '\t' for c in field):
            raise EngineeringError('请求头请每行填写“名称: 值”，不能包含控制字符。')
        result.append((key, field))
    if len(result) > 100:
        raise EngineeringError('请求头最多 100 行。')
    return result


def _secrets(headers=(), urls=(), text=''):
    values = set()
    for name, value in headers:
        if SENSITIVE.search(name) and value:
            values.add(value)
            if ' ' in value:
                values.add(value.split(' ', 1)[1])
            for part in value.split(';'):
                if '=' in part:
                    values.add(part.split('=', 1)[1].strip())
    for url in urls:
        for name, value in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query, keep_blank_values=True):
            if SENSITIVE.search(name) and value:
                values.add(value)
    # Hurl bodies/variables commonly contain JSON or key/value credentials.
    for match in re.finditer(r'''(?im)(?:["']?)(?:api[-_]?key|token|password|secret|access[-_]?token|client[-_]?secret)["']?\s*[:=]\s*["']?([^\r\n"',}\s]+)''', text):
        values.add(match.group(1))
    return tuple(sorted((value for value in values if len(value) >= 3), key=len, reverse=True))[:128]


def _safe_text(value, secrets=(), maximum=MAX_PREVIEW):
    text = str(value or '')
    for secret in secrets:
        text = text.replace(secret, '[已脱敏]')
    text = re.sub(r'(?im)((?:set-)?cookie\s*:\s*)[^\r\n]+', r'\1[已脱敏]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[已脱敏]', text)
    return redact(text, maximum)


class _JSONNumber:
    # Keep response decimal/exponent literals exact in the displayed JSON.
    # A debugging tool must not round an API's monetary value through Double.
    def __init__(self, literal): self.literal = literal


def _json_preview(value, depth=0):
    if isinstance(value, _JSONNumber):
        return value.literal
    if isinstance(value, dict):
        if not value: return '{}'
        entries = [json.dumps(k, ensure_ascii=False)+': '+_json_preview(v, depth+1) for k,v in value.items()]
        return '{\n' + '  '*(depth+1) + (',\n'+'  '*(depth+1)).join(entries) + '\n'+'  '*depth+'}'
    if isinstance(value, list):
        if not value: return '[]'
        return '[\n' + '  '*(depth+1) + (',\n'+'  '*(depth+1)).join(_json_preview(v,depth+1) for v in value) + '\n'+'  '*depth+']'
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _scrub(value, secrets=(), depth=0):
    if depth > 64:
        return '[内容层级过深]'
    if isinstance(value, dict):
        if isinstance(value.get('name'), str) and SENSITIVE.search(value['name']) and 'value' in value:
            return dict(name=value['name'], value='[已脱敏]')
        return {str(k): '[已脱敏]' if SENSITIVE.search(str(k)) or str(k) == 'curl_cmd' else _scrub(v, secrets, depth+1)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets, depth+1) for v in value]
    return _safe_text(value, secrets) if isinstance(value, str) else value


def _body_preview(raw, secrets=()):
    clipped = len(raw) > MAX_PREVIEW
    text = raw[:MAX_PREVIEW].decode('utf-8', 'replace')
    if not clipped:
        try:
            text = _json_preview(_scrub(json.loads(text, parse_float=_JSONNumber), secrets))
            return text[:MAX_PREVIEW], len(text) > MAX_PREVIEW or '[内容层级过深]' in text
        except (ValueError, RecursionError):
            pass
    safe = _safe_text(text, secrets)
    return safe, clipped or len(text) > MAX_PREVIEW


def _redacted_url(url):
    parsed = urllib.parse.urlsplit(url)
    query = [(key, '[已脱敏]' if SENSITIVE.search(key) else value)
             for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)]
    return _safe_text(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                                             urllib.parse.urlencode(query), '')), maximum=8192)


def _route(url):
    # Network diagnostics already implement Apple's bounded per-URL PAC rules.
    # HTTP APIs must use HTTP_PROXY, rather than inadvertently HTTPS_PROXY.
    from . import network_tests
    proxies = urllib.request.getproxies_environment()
    host = urllib.parse.urlsplit(url).hostname
    if network_tests._bypass(host, proxies):
        return {'proxy': '', 'source': 'bypass'}
    selected = proxies.get(urllib.parse.urlsplit(url).scheme) or proxies.get('all') or proxies.get('socks')
    if selected:
        return network_tests.proxy_guard(url, proxies={urllib.parse.urlsplit(url).scheme: selected, 'no': proxies.get('no', '')})
    return network_tests._system_proxy(url)


def _quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + '"'


def _base():
    return {'schema_version': 1, 'kind': 'api', 'status': 'error', 'title': '接口调试',
            'summary': '', 'engine': '', 'items': [], 'rows': [], 'warnings': [],
            'response': {'status_code': None, 'headers': [], 'body': '', 'truncated': False,
                         'timing_ms': None, 'bytes': 0},
            'metrics': {'duration_ms': 0, 'bytes_read': 0, 'truncated': False}}


def _request(payload, runtime, result):
    method = _string(payload.get('method'), '请求方法', 16, default='GET').upper()
    if method not in METHODS:
        raise EngineeringError('请求方法不受支持。')
    url = _url(payload.get('url'))
    headers = _headers(payload.get('headers'))
    body = _string(payload.get('body'), '请求正文', MAX_BODY)
    timeout = _timeout(payload.get('timeout_seconds'))
    curl = runtime.find_tool('curl')
    if not curl:
        raise EngineeringError('未找到系统 curl，无法运行单请求。')
    from .network_tests import route_label
    route = _route(url)
    secrets = _secrets(headers, [url], body)
    marker = 'VCGO_HTTP_' + uuid.uuid4().hex
    config = ['silent', 'show-error', 'include', 'suppress-connect-headers', 'globoff',
              'proto = "=http,https"', 'proto-redir = "=http,https"', 'retry = 0',
              'max-redirs = 0', 'url = ' + _quote(url), 'request = ' + _quote(method),
              'max-time = ' + str(timeout), 'connect-timeout = ' + str(min(8, timeout)),
              'max-filesize = ' + str(MAX_RESPONSE), 'proxy = ' + _quote(route.get('proxy', '')),
              'noproxy = ""', 'write-out = "\\n' + marker + ':%{http_code}:%{time_total}:%{size_download}"']
    if method == 'HEAD':
        config.append('head')
    if body:
        config.append('data-binary = ' + _quote(body))
    config += ['header = ' + _quote(name + ': ' + value) for name, value in headers]
    output = runtime.run([curl, '--disable', '--config', '-'], input=('\n'.join(config)+'\n').encode(),
                         timeout=min(30, timeout+.5), max_output=MAX_RESPONSE+65536)
    result['engine'] = 'curl · 系统 HTTP 引擎'
    result['items'] = [{'label': '请求', 'value': method + ' ' + _redacted_url(url), 'status': 'info', 'detail': ''},
                       {'label': '代理路径', 'value': route_label(route), 'status': 'info', 'detail': ''}]
    raw, separator, meta = output.stdout.rpartition(('\n'+marker+':').encode())
    if not separator:
        raw = output.stdout
    code = None
    duration = output.duration_ms
    received = 0
    if separator:
        parts = meta.strip().split(b':')
        if len(parts) == 3:
            try:
                code = int(parts[0]) or None; duration = float(parts[1])*1000; received = int(float(parts[2]))
            except (ValueError, OverflowError):
                pass
    body_data, response_headers, parsed_code = _split_response(raw)
    code = code or parsed_code
    if not received:
        received = len(body_data)
    safe_headers = [{'name': name, 'value': '[已脱敏]' if SENSITIVE.search(name) else _safe_text(value, secrets, 4096)}
                    for name, value in response_headers]
    preview, clipped = _body_preview(body_data, secrets)
    truncated = bool(clipped or output.truncated or output.returncode == 63)
    result['response'] = {'status_code': code, 'headers': safe_headers, 'body': preview,
                          'truncated': truncated, 'timing_ms': round(duration, 3), 'bytes': received}
    result['metrics'].update(duration_ms=round(output.duration_ms, 3), bytes_read=len(output.stdout), truncated=truncated)
    if output.cancelled:
        result.update(status='cancelled', summary='请求已取消；已经发送的服务器操作不能撤回。')
    elif output.timed_out or output.returncode == 28:
        result.update(status='error', summary='请求超时，未自动重试。')
    elif output.returncode and output.returncode != 63:
        # Do not expose arbitrary curl error text containing full secret URLs.
        names = {5: '代理地址解析失败', 6: '域名解析失败', 7: '连接失败', 35: 'TLS 连接失败',
                 51: '证书校验失败', 60: '证书校验失败', 67: '代理认证失败', 97: '代理连接失败'}
        result.update(status='error', summary=names.get(output.returncode, '请求未完整完成')+'，未自动重试。')
    elif code is None:
        result.update(status='error', summary='未取得可验证的 HTTP 响应。')
    else:
        result.update(status='error' if code >= 400 else 'partial' if truncated else 'success',
                      summary=f'HTTP {code} · {duration:.1f} ms · {received:,} bytes' + (' · 正文预览已截断' if truncated else ''))
    if truncated:
        result['warnings'].append('响应超过传输或预览上限，保留已接收部分，不代表完整正文。')
    if code and 300 <= code < 400:
        result['warnings'].append('已展示重定向响应，没有自动跳转到其他地址。')
    return result


def _split_response(raw):
    data, headers, code = raw, [], None
    for _ in range(8):
        first, boundary, rest = data.partition(b'\r\n\r\n')
        if not boundary:
            first, boundary, rest = data.partition(b'\n\n')
        lines = first.splitlines()
        match = re.match(rb'^HTTP/\S+\s+(\d{3})(?:\s|$)', lines[0] if lines else b'')
        if not boundary or not match:
            break
        code = int(match.group(1)); headers = []
        for line in lines[1:101]:
            key, colon, value = line.partition(b':')
            if colon:
                headers.append((key.decode('latin-1').strip(), value.decode('latin-1').strip()))
        data = rest
        if not 100 <= code < 200:
            break
    return data, headers, code


def _collection(payload, runtime, result):
    timeout = _timeout(payload.get('timeout_seconds'))
    expires = time.monotonic() + timeout
    raw_path = _string(payload.get('collection_path'), 'Hurl 文件', 8192).strip()
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or path.suffix.lower() != '.hurl':
        raise EngineeringError('请选择一个 .hurl 文件，不能选择目录。')
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            meta = os.fstat(stream.fileno())
            if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_COLLECTION:
                raise EngineeringError('Hurl 集合应为普通文件，最大 128 KiB。')
            content = stream.read(MAX_COLLECTION+1)
        if len(content) > MAX_COLLECTION:
            raise EngineeringError('Hurl 集合超过 128 KiB。')
        text = content.decode('utf-8')
    except (OSError, UnicodeError):
        raise EngineeringError('Hurl 文件不可读或不是 UTF-8 文本。') from None
    urls, headers = _validate_collection(text)
    hurl = runtime.find_tool('hurl')
    if not hurl:
        result.update(engine='Hurl', summary='未安装 Hurl。单请求模式仍可使用系统 curl。')
        result['items'] = [{'label': '安装说明', 'value': 'https://hurl.dev/docs/installation.html', 'status': 'missing', 'detail': '安装官方 Hurl 后重新运行集合；本工具不自动安装。'}]
        return result
    from .network_tests import route_label
    routes = []
    for url in urls:
        if getattr(runtime, 'cancelled', False):
            result.update(status='cancelled', engine='Hurl', summary='集合已取消，未发送新的请求。')
            return result
        if time.monotonic() >= expires:
            result.update(engine='Hurl', summary='代理规则检查达到集合时限，未执行请求。')
            return result
        routes.append(_route(url))
    remaining = expires - time.monotonic()
    if remaining <= 0:
        result.update(engine='Hurl', summary='代理规则检查达到集合时限，未执行请求。')
        return result
    proxies = {route.get('proxy', '') for route in routes}
    if len(proxies) != 1:
        raise EngineeringError('这个集合的地址适用不同代理/PAC 路径，请按目标拆分集合再运行。')
    proxy = next(iter(proxies))
    secrets = _secrets(headers, urls, text)
    with tempfile.TemporaryDirectory(prefix='vcgo-hurl-config-') as temp:
        # Hurl reads HOME/XDG config by default. An empty private directory
        # prevents ambient headers, output files and credentials being added.
        env = {'HOME': temp, 'XDG_CONFIG_HOME': temp, 'NO_COLOR': '1',
               'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy, 'ALL_PROXY': proxy,
               'http_proxy': proxy, 'https_proxy': proxy, 'all_proxy': proxy,
               'NO_PROXY': '' if proxy else '*', 'no_proxy': '' if proxy else '*'}
        output = runtime.run([hurl, '--json', '--test', '--jobs', '1', '--retry', '0', '--max-redirs', '0',
                              '--max-time', str(int(timeout*1000))+'ms', '--connect-timeout', str(int(min(timeout, 8)*1000))+'ms',
                              '--max-filesize', str(MAX_RESPONSE), '--limit-rate', str(MAX_RESPONSE), '--no-color'],
                             input=content, timeout=remaining, max_output=1024*1024, cwd=temp, env=env)
    result['engine'] = 'Hurl · 开源集合引擎'
    result['metrics'].update(duration_ms=round(output.duration_ms, 3), bytes_read=len(output.stdout), truncated=output.truncated)
    result['items'] = [{'label': '集合', 'value': path.name, 'status': 'info', 'detail': f'声明 {len(urls)} 个请求 · 不重复、不自动重试'},
                       {'label': '代理路径', 'value': route_label(routes[0]), 'status': 'info', 'detail': ''}]
    body = ''; report_valid = False
    try:
        report = json.loads(output.stdout, parse_float=_JSONNumber)
        report_valid = isinstance(report, (dict, list))
        body = _json_preview(_scrub(report, secrets))
        body = body[:MAX_PREVIEW]
        for index, response in enumerate(_hurl_responses(report)):
            status = response.get('status', response.get('status_code'))
            result['rows'].append({'timestamp': '', 'level': 'error' if status >= 400 else 'success', 'source': 'Hurl',
                                   'line': index+1, 'text': f'HTTP {status}'})
        entries = report.get('entries', []) if isinstance(report, dict) else []
        entries = entries if isinstance(entries, list) else []
        failed_asserts = [assertion for entry in entries[:50] if isinstance(entry, dict) and isinstance(entry.get('asserts'), list)
                         for assertion in entry['asserts'][:100] if isinstance(assertion, dict) and assertion.get('success') is False]
        for assertion in failed_asserts[:100]:
            line = assertion.get('line')
            result['rows'].append({'timestamp': '', 'level': 'error', 'source': 'Hurl · 断言',
                                   'line': line if type(line) is int else None,
                                   'text': _safe_text(assertion.get('message', '断言未通过'), secrets, 1200)})
    except (ValueError, TypeError, RecursionError):
        body = 'Hurl 没有返回可识别的完整 JSON 报告。'
    result['response'].update(body=body, timing_ms=round(output.duration_ms, 3), bytes=len(output.stdout), truncated=output.truncated or len(body) >= MAX_PREVIEW)
    if output.cancelled:
        result.update(status='cancelled', summary='集合已取消；已发送的请求不会重放。')
    elif output.timed_out:
        result.update(status='error', summary='集合达到总时限，已停止剩余请求。')
    elif output.truncated:
        result.update(status='partial', summary='集合输出达到上限，已停止执行；报告不完整。')
    elif output.returncode == 0:
        result.update(status='success' if report_valid else 'partial', summary='Hurl 集合执行及断言通过。' if report_valid else 'Hurl 已完成，结构化报告格式暂不可识别。')
    else:
        names = {1:'Hurl 版本或命令选项不兼容', 2:'Hurl 集合语法不正确', 3:'Hurl 请求运行失败', 4:'Hurl 断言未通过'}
        result.update(status='error', summary=names.get(output.returncode, 'Hurl 集合未完成'))
        # Hurl can print captured secrets or raw bodies in error context. The
        # structured report is enough; never forward stderr into diagnostic UI.
    result['warnings'].append('按所选集合执行真实请求；取消只能停止后续执行，不能撤回服务器已完成的操作。')
    return result


def _validate_collection(text):
    if '\x00' in text:
        raise EngineeringError('Hurl 文件包含无效字符。')
    urls, headers, section = [], [], ''
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1].lower(); continue
        match = REQUEST_LINE.fullmatch(stripped)
        if not match and re.match(r'^(?:GET|HEAD|POST|PUT|PATCH|DELETE|OPTIONS|TRACE|CONNECT)\s+', stripped):
            raise EngineeringError('首版集合需要固定 HTTP/HTTPS 地址，捕获变量可用于路径、请求头和正文。')
        if match:
            if match.group(1) not in METHODS:
                raise EngineeringError('集合包含不受支持的请求方法。')
            url = _url(match.group(2))
            if '{{' in urllib.parse.urlsplit(url).netloc:
                raise EngineeringError('首版集合需要固定目标主机，捕获变量可用于路径、请求头和正文。')
            urls.append(url); section = ''; continue
        if section == 'options':
            key, colon, _ = stripped.partition(':')
            if not colon or key.strip().lower() not in SAFE_HURL_OPTIONS:
                raise EngineeringError('首版集合不支持重试、循环、文件读写、跳转或其他扩展选项；请移除相应 [Options] 设置。')
        if re.search(r'(?i)(?:^|[;\s])file\s*,', stripped):
            raise EngineeringError('首版集合不支持外部文件正文或文件上传，请使用内联正文。')
        if ':' in stripped:
            name, value = stripped.split(':', 1)
            if HEADER_NAME.fullmatch(name.strip()):
                headers.append((name.strip(), value.strip()))
    if not urls or len(urls) > 50:
        raise EngineeringError('集合需要 1–50 个固定 HTTP/HTTPS 请求。')
    return urls, headers


def _hurl_responses(report):
    pending = [(report, 0)]; count = 0
    while pending and count < 100:
        value, depth = pending.pop()
        if depth > 15:
            continue
        if isinstance(value, dict):
            response = value.get('response')
            if isinstance(response, dict):
                code = response.get('status', response.get('status_code'))
                if type(code) is int and 100 <= code <= 599:
                    count += 1; yield response
            pending.extend((item, depth+1) for item in reversed(list(value.values())[:1000]) if isinstance(item, (dict, list)))
        elif isinstance(value, list):
            pending.extend((item, depth+1) for item in reversed(value[:1000]) if isinstance(item, (dict, list)))


def run(payload, runtime=None):
    result = _base(); runtime = runtime or Runtime()
    began = time.monotonic()
    try:
        if not isinstance(payload, dict):
            raise EngineeringError('接口参数需要是 JSON 对象。')
        mode = payload.get('mode', 'request')
        if mode == 'request':
            result = _request(payload, runtime, result)
        elif mode == 'hurl':
            result = _collection(payload, runtime, result)
        else:
            raise EngineeringError('未知的接口调试模式。')
    except (EngineeringError, ValueError, TypeError) as error:
        result.update(status='error', summary=_safe_text(str(error), maximum=600))
    result['metrics']['duration_ms'] = round((time.monotonic()-began)*1000, 3)
    return result
