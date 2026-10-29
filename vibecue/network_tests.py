"""Explicit, bounded network diagnostics for VCGo's Test module.

No scheduler, account tokens, retained results or shared configuration writes.
Third-party measurements run only after the user starts a test. HTTP work lives
in a bounded child so DNS and slow response bodies cannot outlive its deadline.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import ipaddress
import json
import math
import os
import re
import selectors
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

USER_AGENT = 'VCGo network-tests'
MAX_INPUT = 8192
MAX_JSON = 1_048_576
MAX_DOWNLOAD = 5 * 1024 * 1024
PROBE_LIMIT = 6
GLOBAL_SECONDS = 35
IPIFY = 'https://api.ipify.org?format=json'
TRACE = 'https://www.cloudflare.com/cdn-cgi/trace'
GLOBALPING = 'https://api.globalping.io/v1/measurements'
SOURCES = {
    'ipify': {'name': 'ipify 公网出口', 'url': 'https://www.ipify.org/'},
    'ipapi': {'name': 'ipapi.is 网络归属', 'url': 'https://ipapi.is/free-tier.html'},
    'proxycheck': {'name': 'proxycheck.io 风险信号', 'url': 'https://proxycheck.io/api/'},
    'globalping': {'name': 'Globalping 社区探针', 'url': 'https://globalping.io/docs/api.globalping.io'},
    'cloudflare': {'name': 'Cloudflare 轻量下载', 'url': 'https://github.com/cloudflare/speedtest'},
    'webrtc': {'name': 'WebRTC 泄露说明', 'url': 'https://www.rfc-editor.org/rfc/rfc8828'},
    'browserleaks': {'name': 'BrowserLeaks 浏览器检测', 'url': 'https://browserleaks.com/webrtc'},
    'dnsleaktest': {'name': 'DNSLeakTest 浏览器检测', 'url': 'https://www.dnsleaktest.com/'},
}
TITLES = {'ip_reputation': 'IP 纯净度与风险信号', 'domain': '域名可达性',
          'ip_trace': 'IP 痕迹与出口检查', 'global_ping': '全球 Ping',
          'global_http': '全球 HTTP 延迟', 'speed': '轻量下载测速'}
ALIASES = {'egress': 'ip_trace', 'download': 'speed'}
REGIONS = (('US', '美国'), ('DE', '德国'), ('SG', '新加坡'), ('JP', '日本'), ('AU', '澳大利亚'), ('BR', '巴西'))


class TestError(RuntimeError):
    """Only controlled messages, never exception strings or response bodies."""


class RateLimited(TestError):
    pass


def text(value, limit=180):
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127).strip()[:limit]


def finite(value, minimum=0, maximum=1_000_000):
    return value if type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum else None


def public_ip(value):
    if not isinstance(value, str) or '%' in value:
        raise TestError('请输入有效的公网 IP 地址。')
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        raise TestError('请输入有效的公网 IP 地址。') from None
    if not address.is_global or address.is_multicast or address.is_unspecified:
        raise TestError('远程检测仅接受公网 IP，不发送本机、私网或保留地址。')
    return str(address)


def target_host(value):
    if not isinstance(value, str):
        raise TestError('请输入域名或公网 IP，不要包含网址路径、端口或账号信息。')
    value = value.strip()
    if (not value or len(value) > 253 or any(c.isspace() or ord(c) < 33 for c in value)
            or any(c in value for c in '/\\@?#%') or value.startswith('-')):
        raise TestError('请输入域名或公网 IP，不要包含网址路径、端口或账号信息。')
    try:
        return public_ip(value)
    except TestError:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            raise TestError('远程检测仅接受公网 IP，不发送本机、私网或保留地址。') from None
    if ':' in value or '[' in value or ']' in value:
        raise TestError('请输入域名或公网 IP，不要附加端口。')
    try:
        ascii_host = value.rstrip('.').encode('idna').decode('ascii').lower()
    except UnicodeError:
        raise TestError('域名格式无法识别。') from None
    labels = ascii_host.split('.')
    if (len(ascii_host) > 253 or len(labels) < 2 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)
            or labels[-1].isdigit() or ascii_host.endswith(('.local', '.localhost', '.internal', '.test', '.invalid'))):
        raise TestError('请输入可公开解析的完整域名。')
    return ascii_host


def row(label, value, detail=''):
    result = {'label': text(label, 100), 'value': text(str(value), 400)}
    if detail:
        result['detail'] = text(detail, 500)
    return result


def result(kind, status, summary, rows=None, sources=(), notes=()):
    return {'schema_version': 1, 'kind': kind, 'status': status, 'title': TITLES.get(kind, '网络检测'),
            'summary': text(summary, 400), 'checked_at': time.time(), 'rows': (rows or [])[:100],
            'sources': [SOURCES[key] for key in dict.fromkeys(sources) if key in SOURCES],
            'notes': [text(note, 500) for note in notes][:12]}


def _proxy_url(raw):
    if not isinstance(raw, str) or len(raw) > 4096 or any(ord(c) < 33 for c in raw):
        raise TestError('代理配置格式无法识别，未绕过代理执行。')
    try:
        parsed = urllib.parse.urlsplit(raw if '://' in raw else 'http://' + raw)
        if (parsed.scheme not in ('http', 'https', 'socks4', 'socks4a', 'socks5', 'socks5h')
                or not parsed.hostname or parsed.port == 0 or parsed.path not in ('', '/')
                or parsed.query or parsed.fragment):
            raise ValueError()
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
    except (ValueError, TypeError):
        raise TestError('代理配置格式无法识别，未绕过代理执行。') from None


def _bypass(host, proxies):
    if urllib.request.proxy_bypass_environment(host, proxies):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    for entry in str(proxies.get('no', '')).split(',')[:256]:
        try:
            if '/' in entry and address in ipaddress.ip_network(entry.strip(), strict=False):
                return True
        except ValueError:
            pass
    return False


def _system_proxy(url, *, settings=None, script=None, timeout=4):
    """Use Apple's per-URL policy, including bounded native PAC evaluation.

    Values stay inside this process and its request pipe. The chosen first rule
    is authoritative; an unreachable proxy never silently falls back to DIRECT.
    Optional settings/script pointers are used only by local native fixtures.
    """
    if sys.platform != 'darwin':
        return {'proxy': '', 'source': 'direct'}
    owned = []
    cf = net = None
    try:
        cf = ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
        net = ctypes.CDLL(ctypes.util.find_library('CFNetwork'))
        ptr, index = ctypes.c_void_p, ctypes.c_long
        def bind(lib, name, args, result):
            function = getattr(lib, name); function.argtypes = args; function.restype = result
            return function
        string_new = bind(cf, 'CFStringCreateWithCString', [ptr, ctypes.c_char_p, ctypes.c_uint32], ptr)
        string_get = bind(cf, 'CFStringGetCString', [ptr, ctypes.c_char_p, index, ctypes.c_uint32], ctypes.c_bool)
        url_new = bind(cf, 'CFURLCreateWithString', [ptr, ptr, ptr], ptr)
        array_count = bind(cf, 'CFArrayGetCount', [ptr], index)
        array_get = bind(cf, 'CFArrayGetValueAtIndex', [ptr, index], ptr)
        dictionary_get = bind(cf, 'CFDictionaryGetValue', [ptr, ptr], ptr)
        equal = bind(cf, 'CFEqual', [ptr, ptr], ctypes.c_bool)
        number_get = bind(cf, 'CFNumberGetValue', [ptr, ctypes.c_int, ptr], ctypes.c_bool)
        release = bind(cf, 'CFRelease', [ptr], None)
        def constant(name):
            return ctypes.c_void_p.in_dll(net if name.startswith('kCFProxy') else cf, name).value
        def string(value):
            if not value: return ''
            buffer = ctypes.create_string_buffer(8193)
            if not string_get(value, buffer, len(buffer), 0x08000100): raise ValueError()
            return buffer.value.decode('utf-8')
        target_string = string_new(None, url.encode('utf-8'), 0x08000100); owned.append(target_string)
        target = url_new(None, target_string, None); owned.append(target)
        if not target: raise ValueError()
        if settings is None:
            copied = bind(net, 'CFNetworkCopySystemProxySettings', [], ptr)()
            if not copied: raise ValueError()
            owned.append(copied); settings = copied
        def entries(array):
            if not array or not 1 <= array_count(array) <= 64: raise ValueError()
            answer = []
            for i in range(array_count(array)):
                item = array_get(array, i)
                kind = dictionary_get(item, constant('kCFProxyTypeKey'))
                if not kind: raise ValueError()
                if equal(kind, constant('kCFProxyTypeNone')):
                    answer.append({'proxy': '', 'source': 'system'})
                elif equal(kind, constant('kCFProxyTypeAutoConfigurationURL')):
                    answer.append({'auto_url': dictionary_get(item, constant('kCFProxyAutoConfigurationURLKey'))})
                elif equal(kind, constant('kCFProxyTypeAutoConfigurationJavaScript')):
                    answer.append({'auto_script': dictionary_get(item, constant('kCFProxyAutoConfigurationJavaScriptKey'))})
                else:
                    is_socks = equal(kind, constant('kCFProxyTypeSOCKS'))
                    if not is_socks and not any(equal(kind, constant(name)) for name in ('kCFProxyTypeHTTP', 'kCFProxyTypeHTTPS')):
                        raise ValueError()
                    host = string(dictionary_get(item, constant('kCFProxyHostNameKey')))
                    port_value = dictionary_get(item, constant('kCFProxyPortNumberKey')); port = ctypes.c_longlong()
                    if not host or not port_value or not number_get(port_value, 4, ctypes.byref(port)) or not 1 <= port.value <= 65535:
                        raise ValueError()
                    username = string(dictionary_get(item, constant('kCFProxyUsernameKey')))
                    password = string(dictionary_get(item, constant('kCFProxyPasswordKey')))
                    auth = urllib.parse.quote(username, safe='') + ':' + urllib.parse.quote(password, safe='') + '@' if username or password else ''
                    endpoint = '[' + host + ']' if ':' in host else host
                    scheme = 'socks5h' if is_socks else 'http'
                    answer.append({'proxy': _proxy_url(f'{scheme}://{auth}{endpoint}:{port.value}'), 'source': 'system'})
            return answer
        if script is None:
            array = bind(net, 'CFNetworkCopyProxiesForURL', [ptr, ptr], ptr)(target, settings)
            if not array: raise ValueError()
            owned.append(array); first = entries(array)[0]
        else:
            first = {'auto_script': script}
        if 'auto_url' not in first and 'auto_script' not in first:
            return first
        completed = []
        callback_type = ctypes.CFUNCTYPE(None, ptr, ptr, ptr)
        def finished(context, array, error):
            try:
                completed.append(entries(array) if not error else None)
            except (ValueError, TypeError, TestError):
                completed.append(None)
        callback = callback_type(finished)
        class Context(ctypes.Structure):
            _fields_ = [('version', index), ('info', ptr), ('retain', ptr), ('release', ptr), ('description', ptr)]
        context = Context(0, None, None, None, None)
        function = 'CFNetworkExecuteProxyAutoConfigurationURL' if 'auto_url' in first else 'CFNetworkExecuteProxyAutoConfigurationScript'
        source_value = first.get('auto_url') or first.get('auto_script')
        if not source_value: raise ValueError()
        source = bind(net, function, [ptr, ptr, callback_type, ctypes.POINTER(Context)], ptr)(source_value, target, callback, ctypes.byref(context))
        if not source: raise ValueError()
        owned.append(source)
        loop = bind(cf, 'CFRunLoopGetCurrent', [], ptr)()
        mode = ctypes.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode').value
        add = bind(cf, 'CFRunLoopAddSource', [ptr, ptr, ptr], None)
        remove = bind(cf, 'CFRunLoopRemoveSource', [ptr, ptr, ptr], None)
        invalidate = bind(cf, 'CFRunLoopSourceInvalidate', [ptr], None)
        run = bind(cf, 'CFRunLoopRunInMode', [ptr, ctypes.c_double, ctypes.c_bool], ctypes.c_int32)
        add(loop, source, mode)
        try:
            deadline = time.monotonic() + timeout
            while not completed and time.monotonic() < deadline:
                run(mode, min(.1, max(.001, deadline - time.monotonic())), True)
        finally:
            invalidate(source); remove(loop, source, mode)
        if not completed or not completed[0]:
            raise TestError('系统自动代理规则未能在时限内解析，请检查代理应用或 PAC 配置。未绕过代理执行。')
        route = completed[0][0]
        if 'proxy' not in route: raise ValueError()
        return dict(route, source='pac')
    except TestError:
        raise
    except (AttributeError, OSError, ValueError, TypeError, UnicodeError):
        raise TestError('无法读取当前系统代理规则，未绕过配置执行检测。') from None
    finally:
        if cf:
            for value in reversed(owned):
                if value: cf.CFRelease(ctypes.c_void_p(value))


def proxy_guard(url, *, runner=None, proxies=None):
    """Select one configured route, without publishing its host or credentials."""
    host = urllib.parse.urlsplit(url).hostname if isinstance(url, str) else None
    if not host: raise TestError('检测网址无效。')
    environment = urllib.request.getproxies_environment() if proxies is None else proxies
    if _bypass(host, environment):
        return {'proxy': '', 'source': 'bypass'}
    chosen = environment.get('https') or environment.get('all') or environment.get('socks')
    if chosen:
        return {'proxy': _proxy_url(chosen), 'source': 'environment'}
    if proxies is not None or sys.platform != 'darwin':
        return {'proxy': '', 'source': 'direct'}
    return _system_proxy(url)


def route_label(route):
    source = {'environment': '环境代理', 'system': '系统代理规则', 'pac': '系统自动代理规则（PAC）',
              'bypass': 'NO_PROXY 规则直连', 'direct': '直连 / 系统网络隧道'}.get(route.get('source'), '当前网络')
    scheme = urllib.parse.urlsplit(route.get('proxy', '')).scheme.upper()
    if not scheme and route.get('source') in ('direct', 'system'):
        return '未配置显式代理 · 沿用系统 / TUN 路由'
    return source + (' · ' + scheme if scheme else ' · DIRECT')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None  # A response already proves reachability; do not visit another host.


def _curl_quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + '"'


def _bounded_capture(args, *, input, timeout, env, limit):
    """Read both pipes with fixed caps; stop the transfer before allocating more."""
    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    selector = selectors.DefaultSelector()
    output, errors = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    try:
        process.stdin.write(input); process.stdin.close()
        for pipe, target, cap in ((process.stdout, output, limit), (process.stderr, errors, 16384)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, (target, cap))
        while selector.get_map():
            left = deadline - time.monotonic()
            if left <= 0: raise subprocess.TimeoutExpired(args, timeout)
            for key, _ in selector.select(min(left, .1)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj); continue
                target, cap = key.data
                if len(target) + len(chunk) > cap:
                    process.kill(); process.wait(timeout=.5)
                    return subprocess.CompletedProcess(args, -1000, stdout=bytes(output), stderr=b'')
                target.extend(chunk)
        process.wait(timeout=max(.001, deadline - time.monotonic()))
        return subprocess.CompletedProcess(args, process.returncode, stdout=bytes(output), stderr=bytes(errors))
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            try: process.wait(timeout=.5)
            except subprocess.TimeoutExpired: pass
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe and not pipe.closed: pipe.close()


def _curl_request(spec, route, *, runner=None):
    """Credentials and POST data go through stdin, never command arguments."""
    maximum, timeout = spec['max_bytes'], spec['timeout']
    headers = ['User-Agent: ' + USER_AGENT, 'Accept: application/json',
               'Accept-Encoding: ' + ('identity' if spec.get('count_only') is True else 'gzip')]
    if spec.get('etag'): headers.append('If-None-Match: ' + spec['etag'])
    config = ['silent', 'show-error', 'include', 'suppress-connect-headers', 'globoff',
              'proto = "=https"', 'proto-redir = "=https"', 'retry = 0',
              'url = ' + _curl_quote(spec['url']), 'max-time = ' + str(timeout),
              'connect-timeout = ' + str(min(8, timeout)),
              'proxy = ' + _curl_quote(route.get('proxy', '')), 'noproxy = ""',
              'write-out = "\\nVCGO_META:%{http_code}:%{time_total}"']
    if spec['method'] == 'HEAD': config.append('head')
    else:
        config.append('request = ' + _curl_quote(spec['method']))
        config.append('max-filesize = ' + str(maximum))
    if spec.get('json_body') is not None:
        config.append('data-binary = ' + _curl_quote(json.dumps(spec['json_body'], separators=(',', ':'), ensure_ascii=True)))
        headers += ['Content-Type: application/json', 'Expect:']
    config += ['header = ' + _curl_quote(value) for value in headers]
    try:
        env = {key: value for key, value in os.environ.items() if key in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TMPDIR', 'PATH', 'TZ')}
        arguments = ['/usr/bin/curl', '--disable', '--config', '-']
        payload = ('\n'.join(config) + '\n').encode()
        if runner is None:
            process = _bounded_capture(arguments, input=payload, timeout=timeout + .3, env=env, limit=maximum + 131072)
        else:
            process = runner(arguments, input=payload, capture_output=True, timeout=timeout + .3, check=False, env=env)
    except subprocess.TimeoutExpired:
        return {'error': 'timeout'}
    except OSError:
        return {'error': 'transport_unavailable'}
    if process.returncode:
        mapping = {-1000: 'oversized', 5: 'proxy_dns', 6: 'dns', 7: 'proxy_connect' if route.get('proxy') else 'connect',
                   28: 'timeout', 35: 'tls', 51: 'certificate', 60: 'certificate', 63: 'oversized',
                   67: 'proxy_auth', 97: 'proxy_connect'}
        if route.get('proxy') and re.search(rb'(?:response|HTTP/[^ ]+)\s+407\b', process.stderr or b''):
            return {'error': 'proxy_auth'}
        return {'error': mapping.get(process.returncode, 'proxy_network' if route.get('proxy') else 'network')}
    raw = process.stdout
    if not isinstance(raw, bytes) or len(raw) > maximum + 131072:
        return {'error': 'oversized'}
    payload, marker, meta = raw.rpartition(b'\nVCGO_META:')
    if not marker: return {'error': 'invalid'}
    try:
        code, elapsed = meta.decode('ascii').strip().split(':')
        status, elapsed = int(code), float(elapsed)
        if not 100 <= status <= 599 or not math.isfinite(elapsed) or elapsed < 0: raise ValueError()
        final_headers = {}
        while payload.startswith(b'HTTP/'):
            end = payload.find(b'\r\n\r\n')
            if end < 0: return {'error': 'invalid'}
            block, payload = payload[:end], payload[end + 4:]
            if len(block) > 65536: return {'error': 'oversized'}
            final_headers = {}
            for line in block.split(b'\r\n')[1:]:
                key, separator, value = line.partition(b':')
                if separator: final_headers[key.decode('ascii').lower()] = value.strip().decode('latin-1')
        if len(payload) > maximum: return {'error': 'oversized'}
        encoding = '' if spec['method'] == 'HEAD' else final_headers.get('content-encoding', '').lower()
        if spec.get('count_only') is True and encoding not in ('', 'identity'):
            return {'error': 'encoded_speed_response'}
        if encoding == 'gzip':
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(payload, maximum + 1)
            if len(decoded) > maximum or decoder.unconsumed_tail or not decoder.eof: return {'error': 'oversized'}
            payload = decoded
        elif encoding not in ('', 'identity'):
            return {'error': 'encoding'}
        answer = {'status': status, 'etag': text(final_headers.get('etag'), 200),
                  'bytes_received': len(payload), 'elapsed': max(.000001, elapsed)}
        if spec.get('count_only') is not True: answer['body'] = base64.b64encode(payload).decode('ascii')
        return answer
    except (ValueError, TypeError, UnicodeError, zlib.error):
        return {'error': 'invalid'}


def _http_worker(spec):
    """One bounded request using the proxy route selected by the parent."""
    url, method = spec.get('url'), spec.get('method', 'GET')
    if not isinstance(url, str) or len(url) > 2048 or method not in ('GET', 'HEAD', 'POST'):
        raise TestError('请求参数无效。')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port is not None or not parsed.hostname:
        raise TestError('只允许无账号信息的 HTTPS 检测请求。')
    maximum = spec.get('max_bytes', MAX_JSON)
    timeout = finite(spec.get('timeout'), minimum=.05, maximum=35)
    if type(maximum) is not int or not 1 <= maximum <= MAX_DOWNLOAD or timeout is None:
        raise TestError('检测响应上限或时限无效。')
    if spec.get('json_body') is not None and len(json.dumps(spec['json_body']).encode()) > MAX_INPUT:
        raise TestError('检测参数过大。')
    etag = spec.get('etag')
    if etag is not None and (not isinstance(etag, str) or len(etag) > 200 or any(ord(c) < 32 for c in etag)):
        raise TestError('检测缓存标识无效。')
    route = spec.get('route') or proxy_guard(url)
    if not isinstance(route, dict) or route.get('source') not in ('environment', 'system', 'pac', 'bypass', 'direct'):
        raise TestError('检测代理路径无效。')
    if route.get('proxy'): route = dict(route, proxy=_proxy_url(route['proxy']))
    return _curl_request(dict(spec, method=method, max_bytes=maximum, timeout=timeout), route)


class HTTPClient:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep, runner=subprocess.run, proxy_check=proxy_guard):
        self.clock, self.sleep, self.runner, self.proxy_check = clock, sleep, runner, proxy_check
        self.routes = {}
        self.route_reports = []

    def ensure_route(self, url):
        if url not in self.routes:
            route = self.proxy_check(url)
            self.routes[url] = route
            if isinstance(route, dict):
                label = route_label(route)
                if label not in self.route_reports: self.route_reports.append(label)
        return self.routes[url]

    def request(self, url, *, deadline, method='GET', json_body=None, max_bytes=MAX_JSON, etag=None, count_only=False, rate_limit=True):
        route = self.ensure_route(url)
        left = deadline - self.clock()
        if left <= .05:
            raise TestError('检测已超时，请稍后重试。')
        seconds = min(12 if count_only else 20, left)
        spec = {'url': url, 'method': method, 'json_body': json_body, 'max_bytes': max_bytes,
                'etag': etag, 'count_only': count_only, 'timeout': max(.05, seconds - .5), 'route': route}
        try:
            worker = self.runner([sys.executable, '-m', 'vibecue.network_tests', '_fetch'],
                                 input=json.dumps(spec).encode(), capture_output=True,
                                 timeout=seconds, check=False)
        except (subprocess.TimeoutExpired, OSError):
            raise TestError('网络检测超时或连接不可用。') from None
        try:
            value = json.loads(worker.stdout)
        except (ValueError, TypeError):
            raise TestError('检测来源返回了无法识别的数据。') from None
        errors = {'proxy_dns': '代理服务器地址未能解析，请检查代理配置。',
                  'proxy_connect': '无法连接当前代理，或代理握手失败；未改走直连。',
                  'proxy_network': '当前代理链路未完成请求；未改走直连。',
                  'proxy_auth': '代理要求有效认证，请在原代理工具中核对。',
                  'dns': '当前网络路径未能解析检测服务域名。',
                  'connect': '检测服务连接未建立，请检查当前网络或隧道。',
                  'timeout': '当前网络或代理路径超时，请稍后重试。',
                  'certificate': 'HTTPS 证书校验失败；未关闭安全校验，请检查代理证书与系统信任。',
                  'tls': 'HTTPS / TLS 握手失败，请检查代理或服务链路。',
                  'oversized': '检测来源响应超过上限，未读取过大的数据。',
                  'encoded_speed_response': '测速响应经过压缩，未生成不准确的速度值。',
                  'transport_unavailable': '系统网络传输工具暂不可用。'}
        if worker.returncode or not isinstance(value, dict) or value.get('error'):
            raise TestError(errors.get(value.get('error') if isinstance(value, dict) else '', '检测来源返回的数据无效或连接未完成。'))
        status = value.get('status')
        if type(status) is not int or not 100 <= status <= 599:
            raise TestError('检测来源没有返回有效状态。')
        if status == 407:
            raise TestError('代理要求有效认证，请在原代理工具中核对。')
        if status == 429 and rate_limit:
            raise RateLimited('检测来源已达到请求限额，请稍后重试。')
        try:
            body = base64.b64decode(value.get('body', ''), validate=True)
        except (ValueError, TypeError):
            raise TestError('检测来源返回了无法识别的数据。') from None
        if len(body) > max_bytes:
            raise TestError('检测来源返回的数据过大。')
        value['body'] = body
        return value

    def json(self, url, *, deadline, **kwargs):
        response = self.request(url, deadline=deadline, **kwargs)
        if response['status'] not in (200, 202):
            raise TestError('检测来源暂不可用，未取得可确认的数据。')
        try:
            value = json.loads(response['body'])
        except (ValueError, UnicodeError):
            raise TestError('检测来源返回了无法识别的数据。') from None
        if not isinstance(value, dict):
            raise TestError('检测来源返回了无法识别的数据。')
        return value


def discover_ip(client, deadline):
    value = client.json(IPIFY, deadline=deadline)
    return public_ip(value.get('ip'))


def ip_reputation(params, client):
    deadline = client.clock() + 25
    address = public_ip(params['target']) if params.get('target') else discover_ip(client, deadline)
    rows = [row('检测 IP', address)]
    notes = ['风险标签反映来源数据库的记录；无标记不等于绝对纯净，也不能保证某个服务一定接受此 IP。',
             '此查询把检测 IP 发送给 ipapi.is 和 proxycheck.io；匿名次数及缓存由来源决定。']
    partial = False
    try:
        network = client.json('https://api.ipapi.is/?q=' + urllib.parse.quote(address, safe=''), deadline=deadline)
        returned = network.get('ip')
        if returned is not None and public_ip(returned) != address:
            raise TestError('来源返回的 IP 与检测目标不一致。')
        asn = network.get('asn')
        if isinstance(asn, dict):
            asn = ' · '.join(str(v) for v in (asn.get('asn'), asn.get('org')) if type(v) in (str, int))
        country = network.get('country')
        if isinstance(network.get('location'), dict):
            country = network['location'].get('country', country)
        partial = partial or not text(asn) or not text(country)
        rows.extend([row('ASN / 网络', text(asn) or '未确认'), row('地区（估计）', text(country) or '未确认')])
    except TestError:
        partial = True
        rows.append(row('网络归属', '未确认', '来源暂不可用，不使用猜测值。'))
    try:
        value = client.json('https://proxycheck.io/v3/' + urllib.parse.quote(address, safe='') + '?tag=0', deadline=deadline)
        if value.get('status') not in ('ok', 'warning') or not isinstance(value.get(address), dict):
            raise TestError('风险来源没有返回可核对的目标记录。')
        detection = value[address].get('detections')
        if not isinstance(detection, dict):
            raise TestError('风险来源缺少检测字段。')
        known, flagged = 0, []
        for key, name in (('proxy', '代理'), ('vpn', 'VPN'), ('tor', 'Tor 出口'), ('hosting', '数据中心'), ('compromised', '被滥用记录'), ('anonymous', '匿名网络')):
            mark = detection.get(key)
            if type(mark) is bool:
                known += 1
                if mark:
                    flagged.append(name)
                rows.append(row(name, '有标记' if mark else '未见标记', '来源：proxycheck.io；并非本机行为审计。'))
            else:
                rows.append(row(name, '未确认'))
        score = finite(detection.get('risk'), maximum=100)
        rows.append(row('proxycheck.io 风险分', f'{score:g} / 100' if score is not None else '未确认', '来源原始风险分，不换算为“纯净度”。'))
        partial = partial or known < 6 or score is None or value['status'] == 'warning'
        if value['status'] == 'warning':
            notes.append('来源返回 warning，结果可能受匿名查询额度或缓存限制。')
        summary = '来源标记：' + '、'.join(flagged) if flagged else '来源暂未报告已知风险标记；这不构成绝对纯净证明。' if known else '风险信号未确认。'
    except TestError as error:
        partial = True
        summary = '风险信号未确认，已保留能够取得的网络归属信息。'
        rows.append(row('风险数据', '未确认', str(error)))
    return result('ip_reputation', 'partial' if partial else 'ok', summary, rows, ('ipify', 'ipapi', 'proxycheck'), notes)


_RESOLVE_SCRIPT = "import socket,json,sys;print(json.dumps(sorted({v[4][0] for v in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))"


def resolve(host, runner=None):
    runner = runner or subprocess.run
    try:
        value = runner([sys.executable, '-c', _RESOLVE_SCRIPT, host], capture_output=True, timeout=5, check=False)
        addresses = json.loads(value.stdout)
        if value.returncode or not isinstance(addresses, list) or not addresses or len(addresses) > 64:
            raise ValueError()
        return sorted({str(ipaddress.ip_address(item)) for item in addresses})
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise TestError('本机 DNS 未取得有效地址。') from None


def tunnel_route(address, runner=None):
    """Recognize benchmark fake-IP only when its actual local route is a tunnel."""
    if sys.platform != 'darwin': return False
    runner = runner or subprocess.run
    try:
        value = runner(['/sbin/route', '-n', 'get', address], capture_output=True, timeout=2, check=False)
        output = value.stdout.decode('utf-8', errors='replace') if isinstance(value.stdout, bytes) else value.stdout
        return value.returncode == 0 and isinstance(output, str) and len(output) <= 65536 and re.search(r'^\s*interface:\s*(?:utun|tun|tap)[0-9]+\s*$', output, flags=re.M) is not None
    except (OSError, subprocess.TimeoutExpired):
        return False


def domain_test(params, client):
    host = target_host(params.get('target'))
    deadline = client.clock() + 15
    host_url = '[' + host + ']' if ':' in host else host
    if hasattr(client, 'ensure_route'):
        client.ensure_route('https://' + host_url + '/')
    route = client.ensure_route('https://' + host_url + '/') if hasattr(client, 'ensure_route') else None
    remote_dns = isinstance(route, dict) and urllib.parse.urlsplit(route.get('proxy', '')).scheme in ('http', 'https', 'socks4a', 'socks5h')
    dns_unknown = False
    try:
        addresses = resolve(host)
    except TestError:
        addresses = []; dns_unknown = True
        if not remote_dns:
            return result('domain', 'partial', '本机 DNS 未解析成功；不能据此判定全网或某地区封禁。', [row('目标', host), row('本机 DNS', '未确认')], notes=['此项仅检测本机当前网络。区域差异请使用全球 HTTP 检测。'])
    rows = [row('目标', host), row('本机 DNS', '未确认，继续核对代理端 HTTPS' if dns_unknown else '解析成功')]
    if addresses: rows.append(row('解析地址', '、'.join(addresses), '系统 DNS 的当前结果，不证明 DNS 未被干预。'))
    blocked = [address for address in addresses if not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast]
    fake_network = ipaddress.ip_network('198.18.0.0/15')
    fake_answers = blocked and all(ipaddress.ip_address(address) in fake_network for address in blocked)
    through_tunnel = fake_answers and not remote_dns and tunnel_route(blocked[0])
    fake_only = fake_answers and (remote_dns or through_tunnel)
    if blocked and not remote_dns and not fake_only:
        return result('domain', 'partial', '域名解析到非公网地址，未发起外部 HTTP 检测。', rows,
                      notes=['请在本机开发工具中检查内网服务；区域检测仅支持公网目标。'])
    if blocked and remote_dns:
        rows.append(row('代理 DNS 路径', '本机 DNS 返回非公网记录；由所选代理解析原域名', '只向代理提交已校验的公开域名，不把本机私网解析地址当作 HTTPS 目标。'))
    elif fake_only:
        if remote_dns:
            rows.append(row('代理 DNS 路径', '本机返回测试网段地址；通过代理端解析原域名', '仅向配置中的代理提交原始公网域名，不直接连接测试网段地址。'))
        else:
            rows.append(row('TUN DNS 路径', '测试网段地址由系统网络隧道路由接管', '使用原域名验证 HTTPS 证书；不会把 TUN fake-IP 误判为域名被封禁。'))
    host_url = '[' + host + ']' if ':' in host else host
    try:
        response = client.request('https://' + host_url + '/', deadline=deadline, method='HEAD', max_bytes=4096, rate_limit=False)
        code = response['status']
        rows.extend([row('HTTPS / TLS', '已建立可验证的 HTTPS 响应'), row('HTTP 状态', str(code), '403/404 等表示已到达服务；重定向不继续访问其他站点。')])
        summary = '代理路径已取得 HTTPS 响应；本机 DNS 仍未确认。' if dns_unknown else '代理路径已到达该 HTTPS 服务；本机解析地址未用作连接目标。' if blocked and remote_dns else '本机可以到达该 HTTPS 服务；不能代表所有国家、地区或账号都可访问。'
        return result('domain', 'partial' if dns_unknown else 'ok', summary, rows,
                      notes=['服务可能按地区、账号或请求方式限制访问。要对比不同地区，请运行全球 HTTP 检测。'])
    except TestError as error:
        rows.append(row('HTTPS / TLS', '未确认', str(error)))
        return result('domain', 'partial', 'DNS 已解析，但 HTTPS 尚未确认；失败不等于全网封禁。', rows,
                      notes=['证书、链路、代理、服务器配置或地区限制都可能造成失败。'])


def system_network(runner=None):
    runner = runner or subprocess.run
    rows = []
    try:
        response = runner(['/usr/sbin/scutil', '--proxy'], capture_output=True, timeout=3, check=False)
        output = response.stdout.decode('utf-8', errors='replace') if isinstance(response.stdout, bytes) else response.stdout
        flags = set()
        for key in ('HTTPEnable', 'HTTPSEnable', 'SOCKSEnable', 'ProxyAutoConfigEnable', 'ProxyAutoDiscoveryEnable'):
            match = re.search(r'^\s*' + key + r'\s*:\s*([01])\s*$', output or '', flags=re.M)
            if match and match[1] == '1':
                flags.add(key)
        if response.returncode == 0:
            names = {'HTTPEnable':'HTTP', 'HTTPSEnable':'HTTPS', 'SOCKSEnable':'SOCKS', 'ProxyAutoConfigEnable':'PAC', 'ProxyAutoDiscoveryEnable':'自动发现'}
            rows.append(row('系统代理开关', '、'.join(names[key] for key in names if key in flags) or '未启用已识别的代理开关', '仅检查开关，不读取或显示代理地址、密码或自动配置 URL。'))
        else:
            rows.append(row('系统代理开关', '未确认'))
    except (OSError, subprocess.TimeoutExpired):
        rows.append(row('系统代理开关', '未确认'))
    inherited_proxy = any(bool(os.environ.get(key)) for key in ('http_proxy','https_proxy','all_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY'))
    rows.append(row('当前进程代理环境', '已设置' if inherited_proxy else '未设置', '环境变量开关不能代表浏览器或 VPN 的实际路由。'))
    try:
        response = runner(['/usr/sbin/scutil', '--dns'], capture_output=True, timeout=3, check=False)
        output = response.stdout.decode('utf-8', errors='replace') if isinstance(response.stdout, bytes) else response.stdout
        values = []
        for value in re.findall(r'^\s*nameserver\[\d+\]\s*:\s*([^\s]+)', output or '', flags=re.M):
            try:
                value = str(ipaddress.ip_address(value.split('%', 1)[0]))
            except ValueError:
                continue
            if value not in values:
                values.append(value)
        rows.append(row('系统 DNS 配置', '、'.join(values[:8]) or '未确认', '仅显示系统配置，不证明 DNS 查询经过了哪条隧道。'))
    except (OSError, subprocess.TimeoutExpired):
        rows.append(row('系统 DNS 配置', '未确认'))
    return rows


def ip_trace(params, client):
    deadline = client.clock() + 20
    rows, ips = [], []
    try:
        first = discover_ip(client, deadline)
        ips.append(first)
        rows.append(row('ipify 观察到的出口', first))
    except TestError as error:
        rows.append(row('ipify 观察到的出口', '未确认', str(error)))
    try:
        response = client.request(TRACE, deadline=deadline, max_bytes=16384)
        if response['status'] != 200:
            raise TestError('出口来源暂不可用。')
        fields = dict(line.split('=', 1) for line in response['body'].decode('utf-8', errors='replace').splitlines() if '=' in line)
        second = public_ip(fields.get('ip'))
        ips.append(second)
        rows.append(row('Cloudflare 观察到的出口', second))
        if fields.get('loc'):
            rows.append(row('Cloudflare 地区（估计）', text(fields['loc'], 40)))
    except TestError as error:
        rows.append(row('Cloudflare 观察到的出口', '未确认', str(error)))
    rows.extend(system_network())
    complete = len(ips) == 2
    same = complete and ips[0] == ips[1]
    summary = '两个来源观察到相同出口；仍不能据此排除浏览器 WebRTC 或 DNS 泄露。' if same else '两个来源观察到不同出口，请检查分流、代理及 IPv4 / IPv6 路径；这不是“真实地址泄露”的单独证据。' if complete else '出口来源覆盖不完整，尚不能比较网络路径。'
    return result('ip_trace', 'ok' if same else 'partial', summary, rows,
                  ('ipify', 'cloudflare', 'webrtc', 'browserleaks', 'dnsleaktest'),
                  ('这里只检查 VCGo 当前进程与系统配置，不代表 Chrome、Safari 或其他浏览器。',
                   'IP 定位通常是网络归属和大致地区，不等于你的精确家庭地址。浏览器检测需在实际使用的浏览器中进行。'))


def global_test(params, client, kind):
    host = target_host(params.get('target'))
    requested = params.get('regions')
    allowed = dict(REGIONS)
    if requested is None:
        countries = list(allowed)[:params.get('probe_limit', 3)]
    elif isinstance(requested, list) and 1 <= len(requested) <= PROBE_LIMIT and all(isinstance(v, str) and v in allowed for v in requested):
        countries = list(dict.fromkeys(requested))[:params.get('probe_limit', 3)]
    else:
        raise TestError('请选择受支持的检测地区（最多 6 个）。')
    deadline = client.clock() + GLOBAL_SECONDS
    measure_type = 'ping' if kind == 'global_ping' else 'http'
    body = {'type': measure_type, 'target': host, 'limit': len(countries), 'timeout': 20,
            'locations': [{'country': country} for country in countries], 'inProgressUpdates': True}
    if measure_type == 'ping':
        body['measurementOptions'] = {'packets': 3}
    else:
        body['measurementOptions'] = {'request': {'method': 'HEAD'}, 'protocol': 'HTTPS'}
    # POST exactly once. A lost response never triggers a duplicate measurement.
    try:
        created = client.json(GLOBALPING, deadline=deadline, method='POST', json_body=body)
    except RateLimited:
        raise
    except TestError:
        raise TestError('全球测量提交结果未确认；不会自动重发。') from None
    identifier = created.get('id')
    if not isinstance(identifier, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier) is None:
        raise TestError('全球检测没有返回可核对的测量 ID。提交可能已发生；不会自动重发。')
    address = GLOBALPING + '/' + identifier
    etag, latest, successful = None, None, False
    while client.clock() + 1 < deadline:
        client.sleep(1)
        response = client.request(address, deadline=deadline, etag=etag)
        if response['status'] == 304:
            continue
        if response['status'] != 200:
            raise TestError('全球检测结果暂不可读取；不会重新提交测量。')
        try:
            value = json.loads(response['body'])
        except (ValueError, UnicodeError):
            raise TestError('全球检测返回了无法识别的结果。') from None
        if (not isinstance(value, dict) or value.get('id') != identifier
                or value.get('target') != host or value.get('type') != measure_type):
            raise TestError('全球检测结果与本次测量不一致。')
        latest = value
        etag = text(response.get('etag'), 200) or None
        if value.get('status') == 'finished':
            successful = True
            break
        if value.get('status') not in ('in-progress', None):
            break
    rows = [row('目标', host), row('地区', '、'.join(allowed[v] for v in countries)),
            row('测量 ID', identifier, '目标与结果由 Globalping 保存，可能形成公开可检索记录。')]
    results = latest.get('results') if isinstance(latest, dict) else None
    recognized = 0
    if isinstance(results, list):
        for item in results[:PROBE_LIMIT]:
            if not isinstance(item, dict):
                continue
            probe = item.get('probe') if isinstance(item.get('probe'), dict) else {}
            location = probe.get('location') if isinstance(probe.get('location'), dict) else {}
            label = ' · '.join(filter(None, (text(location.get('country'), 40), text(location.get('city'), 60)))) or '社区探针'
            answer = item.get('result') if isinstance(item.get('result'), dict) else {}
            if measure_type == 'ping':
                stats = answer.get('stats') if isinstance(answer.get('stats'), dict) else {}
                avg, loss = finite(stats.get('avg'), maximum=60000), finite(stats.get('loss'), maximum=100)
                if avg is not None:
                    recognized += 1
                rows.append(row(label, f'{avg:g} ms' if avg is not None else '未确认', f'丢包 {loss:g}%' if loss is not None else '丢包率未确认'))
            else:
                status = answer.get('statusCode')
                timings = answer.get('timings') if isinstance(answer.get('timings'), dict) else {}
                total, first = finite(timings.get('total'), maximum=120000), finite(timings.get('firstByte'), maximum=120000)
                if type(status) is int and 100 <= status <= 599:
                    recognized += 1
                else:
                    status = None
                detail = ' · '.join(filter(None, (f'总耗时 {total:g} ms' if total is not None else '', f'首字节 {first:g} ms' if first is not None else '')))
                rows.append(row(label, 'HTTP ' + str(status) if status is not None else '未确认', detail or 'HTTP 时间未确认'))
    if results is None or not rows[3:]:
        rows.append(row('探针结果', '尚未取得'))
    complete = successful and recognized == len(countries)
    return result(kind, 'ok' if complete else 'partial',
                  '已取得全球节点结果。' if complete else '已取得部分结果或仍在检测；不会自动重复提交。', rows,
                  ('globalping',), ('节点位置与覆盖由社区探针决定，不保证每次覆盖全部地区。',
                                   '部分节点超时、丢包或 HTTP 拒绝不等于域名被全网封禁。',
                                   '此项是跨地区延迟 / 可达性测量，不是全球下载带宽测速。'))


def speed_test(params, client):
    deadline = client.clock() + 12
    started = client.clock()
    download_bytes = params.get('download_bytes', 1024 * 1024)
    response = client.request('https://speed.cloudflare.com/__down?bytes=' + str(download_bytes),
                              deadline=deadline, max_bytes=download_bytes, count_only=True)
    elapsed = finite(response.get('elapsed'), minimum=.000001, maximum=12)
    size = response.get('bytes_received')
    if response['status'] != 200 or type(size) is not int or size != download_bytes or elapsed is None:
        raise TestError('轻量下载未完整完成，未生成速度数值。')
    if client.clock() - started > 12.2:
        raise TestError('轻量下载已超时，未生成速度数值。')
    mbps = size * 8 / elapsed / 1_000_000
    return result('speed', 'ok', '已完成本机出口到 Cloudflare 的单连接轻量下载。',
                  [row('下载速度', f'{mbps:.2f} Mbps'), row('下载数据', f'{download_bytes // (1024 * 1024)} MiB'), row('请求耗时', f'{elapsed:.2f} 秒')],
                  ('cloudflare',), (f'仅下载，不上传。本次 {download_bytes // (1024 * 1024)} MiB，12 秒超时；本结果不是公网峰值带宽。',
                                   'Cloudflare 会观察到本次请求的公网出口 IP；不同地区带宽需在对应节点分别测试。'))


def run(params, client=None):
    if not isinstance(params, dict):
        return result('', 'error', '检测参数必须是 JSON 对象。')
    raw_kind = params.get('kind')
    kind = ALIASES.get(raw_kind, raw_kind) if isinstance(raw_kind, str) else None
    if kind not in TITLES:
        return result('', 'error', '请选择支持的检测类型。')
    if set(params) - {'kind', 'target', 'regions', 'probe_limit', 'download_bytes'}:
        return result(kind, 'error', '检测参数包含不支持的字段。')
    if (type(params.get('probe_limit', 3)) is not int or params.get('probe_limit', 3) not in (3, 5, 6)
            or type(params.get('download_bytes', 1024 * 1024)) is not int
            or params.get('download_bytes', 1024 * 1024) not in (1024 * 1024, 3 * 1024 * 1024, MAX_DOWNLOAD)):
        return result(kind, 'error', '节点数可选 3 / 5 / 6，下载量可选 1 / 3 / 5 MiB。')
    if params.get('target') is not None and not isinstance(params.get('target'), str):
        return result(kind, 'error', '检测目标必须是文本。')
    client = client or HTTPClient()
    try:
        if kind == 'ip_reputation':
            answer = ip_reputation(params, client)
        elif kind == 'domain':
            answer = domain_test(params, client)
        elif kind == 'ip_trace':
            answer = ip_trace(params, client)
        elif kind in ('global_ping', 'global_http'):
            answer = global_test(params, client, kind)
        else:
            answer = speed_test(params, client)
    except TestError as error:
        answer = result(kind, 'error', str(error), notes=['未取得有效结果时不会显示“纯净”“未封禁”或虚构速度。'])
    except (OSError, ValueError, TypeError, OverflowError):
        answer = result(kind, 'error', '检测未完成，未使用不完整数据生成结论。')

    reports = getattr(client, 'route_reports', [])
    if reports: answer['rows'].insert(0, row('连接路径', '；'.join(reports), '按当前配置执行；不会因代理失败自动改走直连。'))
    return answer


def main(argv=None):
    parser = argparse.ArgumentParser(prog='vcgo-test', description='VibeCoding Go · 手动网络检测')
    parser.add_argument('operation', choices=('run', '_fetch'))
    args = parser.parse_args(argv)
    raw = sys.stdin.buffer.read(MAX_INPUT + 1)
    try:
        params = json.loads(raw) if len(raw) <= MAX_INPUT else None
    except (ValueError, UnicodeError):
        params = None
    if args.operation == '_fetch':
        try:
            value = _http_worker(params) if isinstance(params, dict) else {'error': 'invalid'}
        except (TestError, ValueError, TypeError, OverflowError):
            value = {'error': 'invalid'}
    else:
        value = run(params)
    print(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False))


if __name__ == '__main__':
    main()
