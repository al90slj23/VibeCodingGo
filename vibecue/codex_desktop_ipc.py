"""Narrow adapter for the installed desktop's existing same-user owner IPC.

This is an internal desktop protocol, pinned to the inspected desktop build.
It never starts an app-server, reads credentials, approves tools, changes a model,
or resumes a conversation. Snapshot bodies stay in transient memory and only
numeric/status metadata escapes this adapter.
"""
import ctypes
import json
import os
from pathlib import Path
import plistlib
import socket
import stat
import struct
import sys
import time
import uuid

from .codex_control import ControlError, UnconfirmedUpdate, THREAD_ID
from .events import codex_directory


MAX_FRAME = 8 * 1024 * 1024
MAX_SEND = 32 * 1024
DESKTOP_BUILDS = {('26.1021.31345', '21828')}
VERSIONS = {'initialize': 0, 'thread-owner-discovery': 1,
            'thread-follower-read-model-settings': 1,
            'thread-follower-start-turn': 2}


class DesktopOwnerUnavailable(ControlError):
    """The desktop router is reachable, but no current window owns this chat."""


def _private_socket(path):
    uid = os.getuid()
    parent, target = path.parent.lstat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid or
            stat.S_IMODE(parent.st_mode) & 0o077 or
            not stat.S_ISSOCK(target.st_mode) or target.st_uid != uid or
            stat.S_IMODE(target.st_mode) & 0o077):
        raise ControlError('Codex 本机连接权限无法确认')


def _peer_uid(sock):
    if sys.platform == 'darwin':
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
            raise ControlError('无法确认 Codex 连接的本机用户')
        return uid.value
    if hasattr(socket, 'SO_PEERCRED'):
        _, uid, _ = struct.unpack('3i', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid
    raise ControlError('此系统不支持验证 Codex 本机连接')


def _peer_bundle(sock):
    if sys.platform != 'darwin':
        raise ControlError('当前仅支持 macOS 桌面对话连接')
    try:
        pid = struct.unpack('i', sock.getsockopt(0, 2, 4))[0]  # LOCAL_PEERPID
        libproc = ctypes.CDLL('/usr/lib/libproc.dylib')
        buffer = ctypes.create_string_buffer(4096)
        size = libproc.proc_pidpath(pid, buffer, len(buffer))
        path = Path(os.fsdecode(buffer.value)) if size > 0 else None
        if path is None:
            raise ValueError()
        bundle = next(parent for parent in path.parents if parent.suffix == '.app')
        if bundle.resolve() not in (Path('/Applications/ChatGPT.app'), Path('/Applications/Codex.app')):
            raise ValueError()
        with (bundle / 'Contents/Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
        if info.get('CFBundleIdentifier') != 'com.openai.codex':
            raise ValueError()
        if (str(info.get('CFBundleShortVersionString')), str(info.get('CFBundleVersion'))) not in DESKTOP_BUILDS:
            raise ControlError('当前 Codex 版本的窗口回复协议尚未核对，请更新 VibeCue')
        return bundle
    except ControlError:
        raise
    except (OSError, ValueError, StopIteration, struct.error):
        raise ControlError('无法确认本机连接来自 Codex 桌面进程') from None


def _identifier(value):
    return isinstance(value, str) and THREAD_ID.fullmatch(value) is not None


def _uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _status_copy(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    if isinstance(value.get('type'), str):
        result['type'] = value['type'][:40]
    flags = value.get('activeFlags')
    if isinstance(flags, list) and len(flags) <= 20 and all(isinstance(item, str) and len(item) <= 80 for item in flags):
        result['activeFlags'] = list(flags)
    elif value.get('type') == 'active' or 'activeFlags' in value:
        result['activeFlags'] = None
    return result


def _snapshot_metadata(state, thread, owner, revision):
    if not isinstance(state, dict) or state.get('id') not in (None, thread):
        raise ControlError('Codex 返回的对话与所选项不一致')
    turns = state.get('turns') if isinstance(state.get('turns'), list) else []
    last = next((row for row in reversed(turns) if isinstance(row, dict)), {})
    canonical = state.get('turnHistory')
    if isinstance(canonical, dict) and canonical.get('kind') == 'canonical':
        history = canonical.get('history')
        if isinstance(history, dict):
            entities, islands = history.get('entitiesByKey'), history.get('islands')
            if isinstance(entities, dict) and isinstance(islands, list):
                last = {}
                for island in reversed(islands):
                    if not isinstance(island, dict) or not isinstance(island.get('entries'), list):
                        continue
                    for entry in reversed(island['entries']):
                        key = entry.get('value') if isinstance(entry, dict) else None
                        row = entities.get(key) if isinstance(key, str) else None
                        if isinstance(row, dict):
                            last = row; break
                    if last:
                        break
    last_turn = {key: str(last[key])[:160] for key in ('turnId', 'status') if isinstance(last.get(key), str)}
    requests = state.get('requests')
    pending = not isinstance(requests, list) or any(not isinstance(row, dict) or row.get('completed') is not True for row in requests)
    return {'thread_id': thread, 'owner_client_id': owner, 'revision': revision,
            'resume_state': state.get('resumeState') if isinstance(state.get('resumeState'), str) else None,
            'thread_runtime_status': _status_copy(state.get('threadRuntimeStatus')),
            'requests_pending': pending, 'last_turn': last_turn,
            'model': state.get('latestModel') if isinstance(state.get('latestModel'), str) else None,
            'reasoning_effort': state.get('latestReasoningEffort') if isinstance(state.get('latestReasoningEffort'), str) else None,
            'thread_settings': {key: value for key, value in (state.get('latestThreadSettings') or {}).items()
                                if key in ('model', 'effort', 'serviceTier') and (value is None or isinstance(value, str))}
                               if isinstance(state.get('latestThreadSettings'), dict) else {}}


class DesktopChannel:
    method_versions = VERSIONS
    def __init__(self, thread_id, codex_home=None, timeout=8):
        if not _identifier(thread_id):
            raise ControlError('对话标识无效')
        self.thread_id = thread_id
        self.path = codex_directory(codex_home) / 'ipc/ipc.sock'
        self.timeout = min(15, max(.1, float(timeout)))
        self.socket = None
        self.client_id = None
        self.owner = None
        self.subscribed = False

    def __enter__(self):
        try:
            _private_socket(self.path)
            self.socket = socket.socket(socket.AF_UNIX)
            self.socket.settimeout(self.timeout)
            self.socket.connect(str(self.path))
            if _peer_uid(self.socket) != os.getuid():
                raise ControlError('Codex 连接用户与当前用户不一致')
            _peer_bundle(self.socket)
            result = self._call('initialize', {'clientType': 'vibecue'})
            identifier = result.get('clientId')
            if not _uuid(identifier):
                raise ControlError('Codex 未确认本机回复连接')
            self.client_id = identifier
            return self
        except ControlError:
            self.close(); raise
        except (OSError, ValueError, TypeError):
            self.close(); raise ControlError('Codex 本机回复连接尚未就绪') from None

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.socket is not None:
            if self.subscribed and self.owner and self.client_id:
                try:
                    self._following(False)
                except (OSError, ControlError):
                    pass
            self.socket.close()
        self.socket = None; self.subscribed = False

    def _send(self, value):
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(data) > MAX_SEND:
            raise ControlError('本机对话请求过大')
        self.socket.sendall(struct.pack('<I', len(data)) + data)

    def _read_exact(self, length, deadline):
        pieces = []
        while length:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError()
            self.socket.settimeout(left)
            chunk = self.socket.recv(min(length, 65536))
            if not chunk:
                raise ConnectionError()
            pieces.append(chunk); length -= len(chunk)
        return b''.join(pieces)

    def _receive(self, deadline):
        length = struct.unpack('<I', self._read_exact(4, deadline))[0]
        if not 0 < length <= MAX_FRAME:
            raise ControlError('Codex 本机连接返回的数据过大或无效')
        try:
            value = json.loads(self._read_exact(length, deadline))
        except (ValueError, UnicodeError):
            raise ControlError('Codex 本机连接返回了无效数据') from None
        if not isinstance(value, dict):
            raise ControlError('Codex 本机连接返回了无效数据')
        # We expose no server capabilities; discovery by other clients must not
        # wait for this narrow sender to provide a handler.
        if value.get('type') == 'client-discovery-request':
            self._send({'type': 'client-discovery-response', 'requestId': value.get('requestId'), 'response': {'canHandle': False}})
        return value

    def _call(self, method, params, *, target=None, mutation=False):
        if method not in self.method_versions:
            raise ControlError('此本机连接不允许该操作')
        request = str(uuid.uuid4())
        message = {'type': 'request', 'requestId': request,
                   'sourceClientId': self.client_id or 'initializing-client',
                   'version': self.method_versions[method], 'method': method, 'params': params,
                   'timeoutMs': int(self.timeout * 1000)}
        if target:
            message['targetClientId'] = target
        attempted = False
        rejected = False
        try:
            attempted = True
            self._send(message)
            deadline = time.monotonic() + self.timeout
            while True:
                response = self._receive(deadline)
                if response.get('type') != 'response' or response.get('requestId') != request:
                    continue
                if response.get('resultType') != 'success':
                    error = response.get('error')
                    if error in ('request-timeout', 'client-disconnected', 'server-closed') and mutation:
                        raise UnconfirmedUpdate('消息已提交，桌面确认尚未返回，请核对对话')
                    if error == 'no-client-found':
                        rejected = True
                        raise DesktopOwnerUnavailable('Codex 当前没有此对话的可用窗口连接，请先在 Codex 打开')
                    raise ControlError('Codex 未接受本机对话请求')
                if response.get('method') != method or (target and response.get('handledByClientId') != target):
                    if mutation:
                        raise UnconfirmedUpdate('消息响应的对话归属无法核对，请查看 Codex')
                    raise ControlError('Codex 本机响应的对话归属无法核对')
                result = response.get('result')
                if not isinstance(result, dict):
                    raise ControlError('Codex 本机响应无效')
                if method == 'thread-owner-discovery':
                    owner = response.get('handledByClientId')
                    if not _uuid(owner):
                        raise ControlError('Codex 对话归属标识无效')
                    self.owner = owner
                return result
        except ControlError as error:
            if attempted and mutation and not rejected and not isinstance(error, UnconfirmedUpdate):
                raise UnconfirmedUpdate('消息已提交，但桌面响应无法确认；请核对对话，不会重发') from None
            raise
        except (OSError, TimeoutError, ConnectionError):
            if attempted and mutation:
                raise UnconfirmedUpdate('消息已提交，连接确认中断；请核对对话，不会重发') from None
            raise ControlError('Codex 本机对话连接已中断') from None

    def discover(self):
        result = self._call('thread-owner-discovery', {'hostId': 'local', 'conversationId': self.thread_id})
        if result.get('supportsUntrustedAppInput') is not True:
            raise ControlError('当前 Codex 窗口不支持安全的外部消息输入')
        return {'owner_client_id': self.owner, 'supports_untrusted_app_input': True}

    def read_settings(self):
        if not self.owner:
            self.discover()
        result = self._call('thread-follower-read-model-settings', {'conversationId': self.thread_id}, target=self.owner)
        return result.get('settings') if isinstance(result.get('settings'), dict) else {}

    def _following(self, following):
        self._send({'type': 'broadcast', 'method': 'thread-stream-following-changed',
                    'sourceClientId': self.client_id, 'targetClientIds': [self.owner],
                    'version': 1, 'params': {'conversationId': self.thread_id, 'hostId': 'local', 'following': following}})
        self.subscribed = following

    def runtime_snapshot(self):
        if not self.owner:
            self.discover()
        self._following(True)
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                message = self._receive(deadline)
                if (message.get('type') != 'broadcast' or message.get('method') != 'thread-stream-state-changed' or
                        message.get('version') != 11 or message.get('sourceClientId') != self.owner or
                        not isinstance(message.get('targetClientIds'), list) or self.client_id not in message['targetClientIds']):
                    continue
                params = message.get('params')
                if not isinstance(params, dict) or params.get('hostId') != 'local' or params.get('conversationId') != self.thread_id:
                    continue
                change = params.get('change')
                if not isinstance(change, dict) or change.get('type') != 'snapshot':
                    continue
                revision = change.get('revision')
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
                    raise ControlError('Codex 对话快照版本无效')
                return _snapshot_metadata(change.get('conversationState'), self.thread_id, self.owner, revision)
        except (OSError, TimeoutError, ConnectionError):
            raise ControlError('Codex 对话状态读取尚未完成') from None
        finally:
            try:
                self._following(False)
            except (OSError, ControlError):
                pass

    def start_turn(self, request):
        if not isinstance(request, dict) or set(request) - {'threadId', 'input', 'clientUserMessageId'}:
            raise ControlError('窗口回复只允许发送文字')
        if request.get('threadId') != self.thread_id:
            raise ControlError('回复对话与窗口不一致')
        inputs = request.get('input')
        if (not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict) or
                set(inputs[0]) != {'type', 'text'} or inputs[0].get('type') != 'text' or
                not isinstance(inputs[0].get('text'), str) or not inputs[0]['text'].strip() or
                len(inputs[0]['text'].encode('utf-8')) > 8192 or '\x00' in inputs[0]['text']):
            raise ControlError('请输入 8192 UTF-8 字节以内的文字')
        message_id = request.get('clientUserMessageId')
        if not _uuid(message_id):
            raise ControlError('消息请求标识无效')
        if not self.owner:
            self.discover()
        result = self._call('thread-follower-start-turn', {'conversationId': self.thread_id,
                            'turnStart': {'request': request, 'context': {'inheritThreadSettings': True}}}, target=self.owner, mutation=True)
        inner = result.get('result')
        if not isinstance(inner, dict):
            raise UnconfirmedUpdate('消息已提交，但桌面未返回可核对的发送回执，请查看 Codex')
        return inner
