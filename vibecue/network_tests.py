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
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
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

from .network_runtime import NetworkRuntime, NetworkCancelled

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
    'cloudflare_trace': {'name': 'Cloudflare 出口观测', 'url': 'https://developers.cloudflare.com/fundamentals/reference/cdn-cgi-endpoint/'},
    'webrtc': {'name': 'WebRTC 泄露说明', 'url': 'https://www.rfc-editor.org/rfc/rfc8828'},
    'browserleaks': {'name': 'BrowserLeaks 浏览器检测', 'url': 'https://browserleaks.com/webrtc'},
    'dnsleaktest': {'name': 'DNSLeakTest 浏览器检测', 'url': 'https://www.dnsleaktest.com/'},
    'cf_doh': {'name': 'Cloudflare 公共 DNS 对照', 'url': 'https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/'},
    'google_doh': {'name': 'Google 公共 DNS 对照', 'url': 'https://developers.google.com/speed/public-dns/docs/doh/json'},
    'tor': {'name': 'Tor 官方出口列表', 'url': 'https://blog.torproject.org/changes-tor-exit-list-service/'},
}
TITLES = {'ip_reputation': 'IP 纯净度与风险信号', 'domain': '域名可达性',
          'ip_trace': 'IP 痕迹与出口检查', 'global_ping': '全球 Ping',
          'global_http': '全球 HTTP 延迟', 'speed': '轻量下载测速'}
ALIASES = {'egress': 'ip_trace', 'download': 'speed'}
REGIONS = (('US', '美国'), ('DE', '德国'), ('SG', '新加坡'), ('JP', '日本'), ('AU', '澳大利亚'), ('BR', '巴西'))


class TestError(RuntimeError):
    """Controlled messages and fixed numeric/status metadata only."""
    def __init__(self, message, *, code=None, status=None, status_code=None,
                 retry_after=None, retry_after_seconds=None, quota=None):
        super().__init__(message)
        self.code = code
        self.status = self.status_code = status_code if status_code is not None else status
        self.retry_after = self.retry_after_seconds = retry_after_seconds if retry_after_seconds is not None else retry_after
        self.quota = dict(quota or {})


class Cancelled(TestError):
    def __init__(self, message='检测已取消。', **kwargs):
        super().__init__(message, code='cancelled', **kwargs)


CancelledError = Cancelled


class RateLimited(TestError):
    def __init__(self, message, **kwargs):
        kwargs.setdefault('code', 'rate_limited')
        kwargs.setdefault('status_code', 429)
        super().__init__(message, **kwargs)


def text(value, limit=180):
    if not isinstance(value, str):
        return ''
    return ''.join(c for c in value if ord(c) >= 32 and ord(c) != 127).strip()[:limit]


def finite(value, minimum=0, maximum=1_000_000):
    if type(value) not in (int,float): return None
    try: return value if math.isfinite(value) and minimum <= value <= maximum else None
    except (OverflowError,ValueError): return None


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


def _bypass(host, proxies, port=None):
    authority = ("[" + host + "]" if ":" in host else host) + (":" + str(port) if port else "")
    if urllib.request.proxy_bypass_environment(authority, proxies) or urllib.request.proxy_bypass_environment(host, proxies):
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
    operation = NetworkRuntime.current()
    if operation:
        try: timeout = operation.remaining(timeout)
        except NetworkCancelled: raise Cancelled() from None
        except subprocess.TimeoutExpired: raise TestError('自动代理检查已达到检测时限。', code='timeout') from None
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
                if operation and operation.cancelled: raise Cancelled()
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
    try:
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        host = parsed.hostname if parsed else None
        port = parsed.port or (443 if parsed.scheme == 'https' else 80) if parsed else None
    except ValueError:
        raise TestError('检测网址无效。') from None
    if not host: raise TestError('检测网址无效。')
    environment = urllib.request.getproxies_environment() if proxies is None else proxies
    if _bypass(host, environment, port):
        return {'proxy': '', 'source': 'bypass'}
    chosen = environment.get(parsed.scheme) or environment.get('all') or environment.get('socks')
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


def _transport_environment():
    allowed = ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_CTYPE', 'TMPDIR', 'TZ')
    env = {key: value for key in allowed if (value := os.environ.get(key)) is not None}
    env.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', LC_ALL='C')
    return env


def _runtime(value=None, timeout=45):
    return value or NetworkRuntime.current() or NetworkRuntime(timeout=timeout)


def _bounded_capture(args, *, input, timeout, env, limit, runtime=None):
    return _runtime(runtime, timeout=timeout).capture(args, input=input, timeout=timeout, env=env, limit=limit)


def _retry_after(value):
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        if value.strip().isdigit():
            seconds = int(value.strip())
        else:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                return None
            seconds = max(0, math.ceil(stamp.timestamp() - time.time()))
        return min(86400, seconds) if seconds >= 0 else None
    except (ValueError, OverflowError, TypeError):
        return None


def _safe_redirect(raw, origin):
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    try:
        value = urllib.parse.urlsplit(urllib.parse.urljoin(origin, raw))
        if value.scheme not in ('http', 'https') or not value.hostname or value.username or value.password:
            return None
        host = value.hostname.encode('idna').decode('ascii')
        if ':' in host: host = '[' + host + ']'
        port = ':' + str(value.port) if value.port is not None else ''
        return value.scheme + '://' + host + port
    except (ValueError, UnicodeError, RecursionError):
        return None


def _curl_request(spec, route, *, runner=None, runtime=None):
    """One finite HTTPS transfer; private configuration travels only on stdin."""
    maximum, timeout = spec['max_bytes'], spec['timeout']
    headers = ['User-Agent: ' + USER_AGENT, 'Accept: ' + spec.get('accept', 'application/json'),
               'Accept-Encoding: ' + ('identity' if spec.get('count_only') is True else 'gzip')]
    if spec.get('etag'): headers.append('If-None-Match: ' + spec['etag'])
    config = ['silent', 'show-error', 'include', 'suppress-connect-headers', 'globoff',
              'proto = "=https"', 'proto-redir = "=https"', 'retry = 0',
              'url = ' + _curl_quote(spec['url']), 'max-time = ' + str(timeout),
              'connect-timeout = ' + str(min(8, timeout)),
              'proxy = ' + _curl_quote(route.get('proxy', '')), 'noproxy = ""',
              'write-out = "\\nVCGO_META:%{http_code}:%{time_total}:%{time_namelookup}:%{time_connect}:%{time_appconnect}:%{time_starttransfer}:%{size_download}"']
    if spec['method'] == 'HEAD': config.append('head')
    else:
        config.append('request = ' + _curl_quote(spec['method']))
        config.append('max-filesize = ' + str(maximum))
    if spec.get('json_body') is not None:
        config.append('data-binary = ' + _curl_quote(json.dumps(spec['json_body'], separators=(',', ':'), ensure_ascii=True)))
        headers += ['Content-Type: application/json', 'Expect:']
    config += ['header = ' + _curl_quote(value) for value in headers]
    try:
        env = _transport_environment()
        arguments = ['/usr/bin/curl', '--disable', '--config', '-']
        payload = ('\n'.join(config) + '\n').encode()
        if runner is None:
            process = _bounded_capture(arguments, input=payload, timeout=timeout + .3, env=env,
                                       limit=maximum + 131072, runtime=runtime)
        else:
            process = runner(arguments, input=payload, capture_output=True, timeout=timeout + .3, check=False, env=env)
    except NetworkCancelled:
        return {'error': 'cancelled'}
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
        fields = meta.decode('ascii').strip().split(':')
        if len(fields) not in (2, 7): return {'error': 'invalid'}
        status, elapsed = int(fields[0]), float(fields[1])
        if not 100 <= status <= 599 or not math.isfinite(elapsed) or elapsed < 0: raise ValueError()
        timings = {'total': elapsed}
        if len(fields) == 7:
            dns, connect, appconnect, first, size = map(float, fields[2:])
            if any(not math.isfinite(value) or value < 0 for value in (dns, connect, appconnect, first, size)):
                raise ValueError()
            if any(value > elapsed + .001 for value in (dns, connect, appconnect, first)) or size > maximum:
                raise ValueError()
            timings.update(dns=dns, tcp=max(0, connect-dns), tls=max(0, appconnect-connect) if appconnect else 0,
                           first_byte=first, transfer=max(0, elapsed-first))
        final_headers = {}
        while payload.startswith(b'HTTP/'):
            end = payload.find(b'\r\n\r\n')
            if end < 0: return {'error': 'invalid'}
            block, payload = payload[:end], payload[end + 4:]
            if len(block) > 65536: return {'error': 'oversized'}
            lines = block.split(b'\r\n')
            match = re.fullmatch(rb'HTTP/[0-9.]+\s+([0-9]{3})(?:\s+[^\r\n]*)?', lines[0])
            if not match: return {'error': 'invalid'}
            block_status = int(match[1]); final_headers = {}
            for line in lines[1:]:
                key, separator, value = line.partition(b':')
                if separator: final_headers[key.decode('ascii').lower()] = value.strip().decode('latin-1')
            if 100 <= block_status < 200 and block_status != 101:
                continue
            if block_status != status: return {'error': 'invalid'}
            break  # An HTTP-looking response body is still body, not another header.
        else:
            return {'error': 'invalid'}
        if len(payload) > maximum: return {'error': 'oversized'}
        encoding = '' if spec['method'] == 'HEAD' or status in (204,304) else final_headers.get('content-encoding', '').lower()
        if spec.get('count_only') is True and encoding not in ('', 'identity'):
            return {'error': 'encoded_speed_response'}
        if encoding == 'gzip':
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(payload, maximum + 1)
            if len(decoded) > maximum or decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
                return {'error': 'oversized'}
            payload = decoded
        elif encoding not in ('', 'identity'):
            return {'error': 'encoding'}
        answer = {'status': status, 'etag': text(final_headers.get('etag'), 200),
                  'bytes_received': len(payload), 'elapsed': elapsed, 'timings': timings}
        retry = _retry_after(final_headers.get('retry-after'))
        if retry is not None: answer['retry_after_seconds'] = retry
        content_type = text(final_headers.get('content-type'), 120)
        if content_type: answer['content_type'] = content_type
        length = final_headers.get('content-length', '')
        if re.fullmatch(r'[0-9]{1,13}', length): answer['content_length'] = int(length)
        redirect = _safe_redirect(final_headers.get('location'), spec['url'])
        if redirect: answer['redirect'] = redirect
        quota = {}
        for name in ('limit', 'remaining'):
            value = final_headers.get('ratelimit-' + name, final_headers.get('x-ratelimit-' + name, ''))
            if re.fullmatch(r'[0-9]{1,12}', value): quota[name] = int(value)
        if quota: answer['quota'] = quota
        if spec.get('count_only') is not True: answer['body'] = base64.b64encode(payload).decode('ascii')
        return answer
    except (ValueError, TypeError, UnicodeError, zlib.error):
        return {'error': 'invalid'}


def _http_worker(spec, *, runtime=None):
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
    if spec.get('accept', 'application/json') not in ('application/json', 'application/dns-json'):
        raise TestError('检测响应类型无效。')
    route = spec.get('route') or proxy_guard(url)
    if not isinstance(route, dict) or route.get('source') not in ('environment', 'system', 'pac', 'bypass', 'direct'):
        raise TestError('检测代理路径无效。')
    if route.get('proxy'): route = dict(route, proxy=_proxy_url(route['proxy']))
    operation = _runtime(runtime, timeout=min(35, timeout + .5))
    with operation.signals():
        return _curl_request(dict(spec, method=method, max_bytes=maximum, timeout=timeout), route, runtime=operation)


class HTTPClient:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep, runner=None, proxy_check=proxy_guard, runtime=None):
        self.clock, self.runner, self.proxy_check = clock, runner, proxy_check
        self.runtime = runtime or NetworkRuntime(timeout=45, clock=clock)
        self._custom_sleep = None if sleep is time.sleep else sleep
        self.routes = {}
        self.route_reports = []
        self.last_response_meta = {}

    @property
    def cancelled(self):
        return self.runtime.cancelled

    def is_cancelled(self):
        return self.cancelled

    def _check(self):
        try:
            self.runtime.check()
        except NetworkCancelled:
            raise Cancelled() from None
        except subprocess.TimeoutExpired:
            raise TestError('网络检测已达到总时限，请稍后重试。', code='timeout') from None

    @contextmanager
    def operation_scope(self):
        with self.runtime.signals():
            yield self

    signals = operation_scope

    def sleep(self, seconds):
        self._check()
        try:
            if self._custom_sleep is None:
                self.runtime.sleep(seconds)
            else:
                self._custom_sleep(seconds)
        except NetworkCancelled:
            raise Cancelled() from None
        except subprocess.TimeoutExpired:
            raise TestError('网络检测已达到总时限。', code='timeout') from None
        self._check()

    def ensure_route(self, url):
        self._check()
        if url not in self.routes:
            try:
                with self.runtime.signals():
                    route = self.proxy_check(url)
            except NetworkCancelled:
                raise Cancelled() from None
            except subprocess.TimeoutExpired:
                raise TestError('代理规则检查已达到检测时限。', code='timeout') from None
            self.routes[url] = route
            if isinstance(route, dict):
                label = route_label(route)
                if label not in self.route_reports: self.route_reports.append(label)
        self._check()
        return self.routes[url]

    def request(self, url, *, deadline, method='GET', json_body=None, max_bytes=MAX_JSON, etag=None,
                count_only=False, rate_limit=True, accept='application/json'):
        self.last_response_meta = {}
        route = self.ensure_route(url)
        self._check()
        left = min(deadline - self.clock(), self.runtime.deadline - self.runtime.clock())
        if left <= .05:
            raise TestError('检测已超时，请稍后重试。', code='timeout')
        seconds = min(12 if count_only else 20, left)
        spec = {'url': url, 'method': method, 'json_body': json_body, 'max_bytes': max_bytes,
                'etag': etag, 'count_only': count_only, 'timeout': max(.05, seconds - .5), 'route': route,
                'accept': accept}
        try:
            if self.runner is None:
                # curl itself is a bounded DNS/network subprocess. Do not start
                # another Python interpreter for every request or poll.
                value = _http_worker(spec, runtime=self.runtime)
                returncode = 0
            else:
                # Retain the injection boundary used by deterministic fixtures.
                worker = self.runner([sys.executable, '-m', 'vibecue.network_tests', '_fetch'],
                                     input=json.dumps(spec).encode(), capture_output=True,
                                     timeout=seconds, check=False)
                returncode = worker.returncode
                if not isinstance(worker.stdout, bytes) or len(worker.stdout) > 8 * 1024 * 1024:
                    raise TestError('检测来源返回的数据过大。', code='oversized')
                value = json.loads(worker.stdout)
        except NetworkCancelled:
            raise Cancelled() from None
        except subprocess.TimeoutExpired:
            raise TestError('网络检测超时或连接不可用。', code='timeout') from None
        except OSError:
            raise TestError('系统网络传输暂不可用。', code='transport_unavailable') from None
        except (ValueError, TypeError, RecursionError):
            raise TestError('检测来源返回了无法识别的数据。', code='invalid') from None
        self._check()
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
        error = value.get('error') if isinstance(value, dict) else 'invalid'
        if error == 'cancelled': raise Cancelled()
        if returncode or not isinstance(value, dict) or error:
            raise TestError(errors.get(error, '检测来源返回的数据无效或连接未完成。'), code=error or 'invalid')
        status = value.get('status')
        if type(status) is not int or not 100 <= status <= 599:
            raise TestError('检测来源没有返回有效状态。', code='invalid')
        encoded = value.get('body', '')
        if not isinstance(encoded, str) or len(encoded) > ((max_bytes + 2) // 3) * 4:
            raise TestError('检测来源返回的数据过大。', code='oversized')
        try:
            body = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, RecursionError):
            raise TestError('检测来源返回了无法识别的数据。', code='invalid') from None
        if len(body) > max_bytes:
            raise TestError('检测来源返回的数据过大。', code='oversized')
        value['body'] = body
        self.last_response_meta = {key: value[key] for key in
                                  ('status', 'elapsed', 'timings', 'retry_after_seconds', 'quota', 'content_type', 'content_length', 'redirect')
                                  if key in value}
        if status == 407:
            raise TestError('代理要求有效认证，请在原代理工具中核对。', code='proxy_auth', status=status)
        if status == 429 and rate_limit:
            raise RateLimited('检测来源已达到请求限额，请稍后重试。', status=status,
                              retry_after_seconds=value.get('retry_after_seconds'), quota=value.get('quota'))
        return value

    def json(self, url, *, deadline, **kwargs):
        response = self.request(url, deadline=deadline, **kwargs)
        if response['status'] not in (200, 202):
            raise TestError('检测来源暂不可用，未取得可确认的数据。', code='source_http_status', status=response['status'])
        try:
            value = json.loads(response['body'])
        except (ValueError, UnicodeError, RecursionError):
            raise TestError('检测来源返回了无法识别的数据。', code='invalid') from None
        if not isinstance(value, dict):
            raise TestError('检测来源返回了无法识别的数据。', code='invalid')
        return value


def discover_ip(client, deadline):
    value = client.json(IPIFY, deadline=deadline)
    return public_ip(value.get('ip'))


def ip_reputation(params, client):
    from .network_inspection import ip_reputation as implementation
    return implementation(params, client, api=sys.modules[__name__])


_RESOLVE_SCRIPT = "import socket,json,sys;print(json.dumps(sorted({v[4][0] for v in socket.getaddrinfo(sys.argv[1],443,type=socket.SOCK_STREAM)})))"


def _local_capture(args, *, timeout, limit=65536, runner=None, deadline=None):
    if deadline is not None:
        operation = NetworkRuntime.current()
        now = operation.clock() if operation is not None else time.monotonic()
        left = deadline - now
        if not math.isfinite(left) or left <= .001:
            raise subprocess.TimeoutExpired(args, 0)
        timeout = min(timeout, left)
    if runner is not None:
        return runner(args, capture_output=True, timeout=timeout, check=False)
    operation = _runtime(timeout=timeout)
    try:
        with operation.signals():
            return operation.capture(args, timeout=timeout, input=b'', env=_transport_environment(), limit=limit)
    except NetworkCancelled:
        raise Cancelled() from None


def resolve(host, runner=None, *, deadline=None):
    try:
        value = _local_capture([sys.executable, '-I', '-c', _RESOLVE_SCRIPT, host], timeout=5, runner=runner, deadline=deadline)
        addresses = json.loads(value.stdout)
        if value.returncode or not isinstance(addresses, list) or not addresses or len(addresses) > 64:
            raise ValueError()
        return sorted({str(ipaddress.ip_address(item)) for item in addresses})
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        raise TestError('本机 DNS 未取得有效地址。') from None


def tunnel_route(address, runner=None, *, deadline=None):
    """Recognize benchmark fake-IP only when its actual local route is a tunnel."""
    if sys.platform != 'darwin': return False
    try:
        value = _local_capture(['/sbin/route', '-n', 'get', address], timeout=2, runner=runner, deadline=deadline)
        output = value.stdout.decode('utf-8', errors='replace') if isinstance(value.stdout, bytes) else value.stdout
        return value.returncode == 0 and isinstance(output, str) and len(output) <= 65536 and re.search(r'^\s*interface:\s*(?:utun|tun|tap)[0-9]+\s*$', output, flags=re.M) is not None
    except (OSError, subprocess.TimeoutExpired):
        return False


def domain_test(params, client):
    from .network_inspection import domain_test as implementation
    return implementation(params, client, api=sys.modules[__name__])


def system_network(runner=None, *, deadline=None):
    rows = []
    try:
        response = _local_capture(['/usr/sbin/scutil', '--proxy'], timeout=3, runner=runner, deadline=deadline)
        output = response.stdout.decode('utf-8', errors='replace') if isinstance(response.stdout, bytes) else response.stdout
        if not isinstance(output, str) or len(output) > 65536:
            output = ''; response.returncode = -1
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
        response = _local_capture(['/usr/sbin/scutil', '--dns'], timeout=3, runner=runner, deadline=deadline)
        output = response.stdout.decode('utf-8', errors='replace') if isinstance(response.stdout, bytes) else response.stdout
        if response.returncode or not isinstance(output, str) or len(output) > 65536: output = ''
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
    from .network_inspection import ip_trace as implementation
    return implementation(params, client, api=sys.modules[__name__])


def global_test(params, client, kind):
    from .network_measurements import global_test as implementation
    return implementation(params, client, kind, api=sys.modules[__name__])


def speed_test(params, client):
    from .network_measurements import speed_test as implementation
    return implementation(params, client, api=sys.modules[__name__])


def run(params, client=None):
    if not isinstance(params, dict):
        return result('', 'error', '检测参数必须是 JSON 对象。')
    raw_kind = params.get('kind')
    kind = ALIASES.get(raw_kind, raw_kind) if isinstance(raw_kind, str) else None
    if kind not in TITLES:
        return result('', 'error', '请选择支持的检测类型。')
    if set(params) - {'kind', 'target', 'regions', 'probe_limit', 'download_bytes', 'measurement_id'}:
        return result(kind, 'error', '检测参数包含不支持的字段。')
    if (type(params.get('probe_limit', 3)) is not int or params.get('probe_limit', 3) not in (3, 5, 6)
            or type(params.get('download_bytes', 1024 * 1024)) is not int
            or params.get('download_bytes', 1024 * 1024) not in (1024 * 1024, 3 * 1024 * 1024, MAX_DOWNLOAD)):
        return result(kind, 'error', '节点数可选 3 / 5 / 6，下载量可选 1 / 3 / 5 MiB。')
    if params.get('target') is not None and not isinstance(params.get('target'), str):
        return result(kind, 'error', '检测目标必须是文本。')
    if params.get('measurement_id') is not None and kind not in ('global_ping','global_http'):
        return result(kind, 'error', '测量 ID 只能用于读取已有全球测量。')
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
        state = 'cancelled' if isinstance(error,Cancelled) else 'rate_limited' if isinstance(error,RateLimited) else 'error'
        answer = result(kind, state, str(error), notes=['未取得有效结果时不会显示“纯净”“未封禁”或虚构速度。'])
        answer['error_code'] = error.code
        if error.retry_after_seconds is not None: answer['retry_after_seconds'] = error.retry_after_seconds
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
    except (ValueError, UnicodeError, RecursionError):
        params = None
    if args.operation == '_fetch':
        try:
            value = _http_worker(params) if isinstance(params, dict) else {'error': 'invalid'}
        except (TestError, ValueError, TypeError, OverflowError):
            value = {'error': 'invalid'}
    else:
        client = HTTPClient()
        with client.operation_scope():
            value = run(params,client)
    print(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False))


if __name__ == '__main__':
    main()
