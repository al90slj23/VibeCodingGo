"""Narrow adapter for the installed desktop's existing same-user owner IPC.

This is an internal desktop protocol. Identity and per-method contracts are
verified separately so a compatible desktop update needs no companion release.
It never starts an app-server, reads credentials, approves tools, changes a model,
or resumes a conversation. Snapshot bodies stay in transient memory and only
numeric/status metadata escapes this adapter.
"""
import ctypes
import json
import os
from pathlib import Path
import plistlib
import select
import socket
import stat
import struct
import sys
import time
import uuid

from .codex_control import ControlError, UnconfirmedUpdate, THREAD_ID
from .events import codex_directory
from .codex_runtime import latest_turn, status_metadata


MAX_FRAME = 8 * 1024 * 1024
# The desktop sends the entire conversation when a follower subscribes. The
# read-only attention adapter already permits this same finite snapshot budget.
# Ordinary RPC responses keep MAX_FRAME; no outbound request limit is raised.
MAX_STREAM_FRAME = 32 * 1024 * 1024
MAX_SEND = 32 * 1024
SNAPSHOT_SETTLE_SECONDS = .2
SNAPSHOT_SETTLE_FRAMES = 32
VERSIONS = {'initialize': 0, 'thread-owner-discovery': 1,
            'thread-follower-read-model-settings': 1,
            'thread-follower-start-turn': 2}
# Versions implemented by this adapter, independent of the installed app build.
KNOWN_PROTOCOL = dict(VERSIONS, **{'thread-stream-state-changed': 11,
                                 'thread-stream-following-changed': 1,
                                 'thread-follower-update-thread-settings': 2})
CAPABILITY_METHODS = {
    'reply': ('thread-owner-discovery', 'thread-follower-start-turn',
              'thread-follower-read-model-settings', 'thread-stream-state-changed',
              'thread-stream-following-changed'),
    'control': ('thread-owner-discovery', 'thread-follower-read-model-settings',
                'thread-follower-update-thread-settings'),
    'attention': ('thread-owner-discovery', 'thread-stream-state-changed',
                  'thread-stream-following-changed'),
}


class DesktopOwnerUnavailable(ControlError):
    """The desktop router is reachable, but no current window owns this chat."""


class DesktopConnectionInterrupted(ControlError):
    """A read-only operation may retry using a fresh desktop/client identity."""


class DesktopProtocolUnsupported(ControlError):
    """A specific interface contract cannot be confirmed on the verified peer."""


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
        return bundle
    except ControlError:
        raise
    except (OSError, ValueError, StopIteration, struct.error):
        raise ControlError('无法确认本机连接来自 Codex 桌面进程') from None


def _protocol_contract(bundle):
    from .desktop_protocol import read_protocol_contract, ProtocolContractError
    try:
        return read_protocol_contract(bundle)
    except ProtocolContractError as error:
        raise DesktopProtocolUnsupported(str(error)) from None


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
    return status_metadata(value)


def _snapshot_metadata(state, thread, owner, revision):
    if not isinstance(state, dict) or state.get('id') not in (None, thread):
        raise ControlError('Codex 返回的对话与所选项不一致')
    last = latest_turn(state)
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
    max_frame_size = MAX_FRAME
    def __init__(self, thread_id, codex_home=None, timeout=8, deadline=None):
        if not _identifier(thread_id):
            raise ControlError('对话标识无效')
        self.thread_id = thread_id
        self.path = codex_directory(codex_home) / 'ipc/ipc.sock'
        self.timeout = min(15, max(.1, float(timeout)))
        self.deadline = deadline
        self.socket = None
        self.client_id = None
        self.owner = None
        self.subscribed = False
        self._stream_requested = False
        self.protocol_contract = {}

    def supports(self, method, version=None):
        expected = KNOWN_PROTOCOL.get(method) if version is None else version
        # initialize is the router's read-only unversioned handshake (wire v0),
        # deliberately absent from the static method table in released builds.
        # Its UUID-bearing success is still required before any capability is used.
        actual = self.protocol_contract.get(method, 0 if method == 'initialize' and self.protocol_contract else None)
        return type(expected) is int and type(actual) is int and actual == expected

    def require_method(self, method, version=None):
        if not self.supports(method, version):
            raise DesktopProtocolUnsupported(
                "Codex 接口暂不兼容：" + method + "；后台会自动重新核对")

    @property
    def capabilities(self):
        return {name: all(self.supports(method) for method in methods)
                for name, methods in CAPABILITY_METHODS.items()}

    def require_capability(self, name):
        if name not in CAPABILITY_METHODS:
            raise ControlError("此本机连接不允许该能力")
        for method in CAPABILITY_METHODS[name]:
            self.require_method(method)

    def __enter__(self):
        self.client_id = None; self.owner = None; self.protocol_contract = {}
        try:
            _private_socket(self.path)
            self.socket = socket.socket(socket.AF_UNIX)
            self.socket.settimeout(self._remaining_timeout())
            self.socket.connect(str(self.path))
            if _peer_uid(self.socket) != os.getuid():
                raise ControlError('Codex 连接用户与当前用户不一致')
            self.protocol_contract = _protocol_contract(_peer_bundle(self.socket))
            self.require_method("initialize")
            result = self._call('initialize', {'clientType': 'vibecue'})
            identifier = result.get('clientId')
            if not _uuid(identifier):
                raise ControlError('Codex 未确认本机回复连接')
            self.client_id = identifier
            return self
        except ControlError:
            self.close(); raise
        except OSError:
            self.close(); raise DesktopConnectionInterrupted('Codex 本机回复连接尚未就绪') from None
        except (ValueError, TypeError):
            self.close(); raise ControlError('Codex 本机回复连接无效') from None

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
        self.socket = None; self.subscribed = False; self._stream_requested = False
        self.client_id = None; self.owner = None; self.protocol_contract = {}

    def _send(self, value):
        data = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(data) > MAX_SEND:
            raise ControlError('本机对话请求过大')
        if self.deadline is not None:
            self.socket.settimeout(self._remaining_timeout())
        self.socket.sendall(struct.pack('<I', len(data)) + data)

    def _remaining_timeout(self):
        if self.deadline is None:
            return self.timeout
        remaining = min(self.timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError()
        return remaining

    def _read_deadline(self):
        deadline = time.monotonic() + self.timeout
        return min(deadline, self.deadline) if self.deadline is not None else deadline

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
        limit = max(self.max_frame_size, MAX_STREAM_FRAME) if self._stream_requested else self.max_frame_size
        if not 0 < length <= limit:
            raise ControlError('Codex 本机连接返回的数据过大或无效')
        try:
            value = json.loads(self._read_exact(length, deadline))
        except (ValueError, UnicodeError):
            raise ControlError('Codex 本机连接返回了无效数据') from None
        if not isinstance(value, dict):
            raise ControlError('Codex 本机连接返回了无效数据')
        # A subscribed owner can have a final stream frame in flight after we
        # unsubscribe. Consume that bounded frame before a subsequent RPC reply,
        # while keeping oversized RPC/unrelated broadcasts rejected. Bodies are
        # never returned to the UI or persisted by this adapter.
        if length > self.max_frame_size and not self._own_stream(value):
            raise ControlError('Codex 本机连接返回的数据过大或无效')
        # We expose no server capabilities; discovery by other clients must not
        # wait for this narrow sender to provide a handler.
        if value.get('type') == 'client-discovery-request':
            self._send({'type': 'client-discovery-response', 'requestId': value.get('requestId'), 'response': {'canHandle': False}})
        return value

    def _own_stream(self, message):
        if (message.get('type') != 'broadcast' or message.get('method') != 'thread-stream-state-changed' or
                message.get('version') != 11 or message.get('sourceClientId') != self.owner or
                not isinstance(message.get('targetClientIds'), list) or self.client_id not in message['targetClientIds']):
            return False
        params = message.get('params')
        if not isinstance(params, dict) or params.get('hostId') != 'local' or params.get('conversationId') != self.thread_id:
            return False
        change = params.get('change')
        return isinstance(change, dict) and change.get('type') in ('snapshot', 'patches')

    def _call(self, method, params, *, target=None, mutation=False):
        if method not in self.method_versions:
            raise ControlError('此本机连接不允许该操作')
        # Reject unsupported writes before request IDs, bytes or mutation receipts.
        self.require_method(method, self.method_versions[method])
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
            deadline = self._read_deadline()
            while True:
                response = self._receive(deadline)
                if response.get('type') != 'response' or response.get('requestId') != request:
                    continue
                if response.get('resultType') != 'success':
                    error = response.get('error')
                    if error in ('request-timeout', 'client-disconnected', 'server-closed') and mutation:
                        raise UnconfirmedUpdate('消息已提交，桌面确认尚未返回，请核对对话')
                    if error in ('request-timeout', 'client-disconnected', 'server-closed'):
                        raise DesktopConnectionInterrupted('Codex 正在恢复本机对话连接')
                    if error in ('request-version-mismatch', 'no-handler-for-request'):
                        rejected = True  # Explicit rejection proves no write was applied.
                        raise DesktopProtocolUnsupported('Codex 接口暂不兼容：' + method + '；后台会自动重新核对')
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
            raise DesktopConnectionInterrupted('Codex 本机对话连接已中断') from None

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
        self.require_method("thread-stream-following-changed")
        self._send({'type': 'broadcast', 'method': 'thread-stream-following-changed',
                    'sourceClientId': self.client_id, 'targetClientIds': [self.owner],
                    'version': 1, 'params': {'conversationId': self.thread_id, 'hostId': 'local', 'following': following}})
        self.subscribed = following
        if following:
            self._stream_requested = True

    def runtime_snapshot(self):
        # Import only after channel/module initialization: the long-lived
        # attention adapter imports DesktopChannel. Reuse its bounded, body-free
        # projection here without introducing an initialization-time cycle.
        from .desktop_attention import AttentionProjection

        self.require_method("thread-stream-state-changed")
        self.require_method("thread-stream-following-changed")
        if not self.owner:
            self.discover()
        self._following(True)
        try:
            deadline = self._read_deadline()
            settle_deadline = None
            projection = None
            received = 0
            while True:
                if projection is not None:
                    left = min(deadline, settle_deadline) - time.monotonic()
                    readable, _, _ = select.select([self.socket], [], [], max(0, left))
                    if not readable:
                        # This state contains only projected runtime/request and
                        # model metadata; no transient conversation body escapes.
                        state = dict(projection.runtime.data, id=self.thread_id,
                                     requests=projection.requests,
                                     latestThreadSettings=projection.settings.settings)
                        state.update({key: value for key, value in projection.settings.values.items()
                                      if key in ('latestModel', 'latestReasoningEffort')})
                        return _snapshot_metadata(state, self.thread_id, self.owner, projection.revision)
                    if left <= 0:
                        raise DesktopConnectionInterrupted('Codex 对话状态仍在同步，正在重新读取')
                if received >= SNAPSHOT_SETTLE_FRAMES:
                    raise DesktopConnectionInterrupted('Codex 对话状态更新过多，正在重新读取')
                message = self._receive(min(deadline, settle_deadline) if settle_deadline is not None else deadline)
                received += 1
                if message.get('type') == 'broadcast' and message.get('method') == 'ipc-connection-reset':
                    raise DesktopConnectionInterrupted('Codex 连接已重建，正在重新读取对话状态')
                if message.get('type') == 'broadcast' and message.get('method') == 'client-status-changed':
                    params = message.get('params')
                    if isinstance(params, dict) and params.get('clientId') == self.owner and params.get('status') == 'disconnected':
                        raise DesktopConnectionInterrupted('Codex 对话窗口已变化，正在重新读取状态')
                if (message.get('type') != 'broadcast' or message.get('method') != 'thread-stream-state-changed' or
                        message.get('version') != 11 or message.get('sourceClientId') != self.owner or
                        not isinstance(message.get('targetClientIds'), list) or
                        not all(isinstance(target, str) for target in message['targetClientIds']) or
                        self.client_id not in message['targetClientIds']):
                    continue
                params = message.get('params')
                if not isinstance(params, dict) or params.get('hostId') != 'local' or params.get('conversationId') != self.thread_id:
                    continue
                change = params.get('change')
                if not isinstance(change, dict):
                    raise DesktopConnectionInterrupted('Codex 对话状态更新无效，正在重新读取')
                if projection is None and change.get('type') != 'snapshot':
                    continue
                try:
                    if change.get('type') == 'snapshot':
                        fresh = AttentionProjection(self.thread_id, self.owner)
                        fresh.apply(change)
                        projection = fresh
                        if settle_deadline is None:
                            settle_deadline = min(deadline, time.monotonic() + SNAPSHOT_SETTLE_SECONDS)
                    else:
                        projection.apply(change)
                except ControlError:
                    raise DesktopConnectionInterrupted('Codex 对话状态出现观察缺口，正在重新读取') from None
        except (OSError, TimeoutError, ConnectionError):
            raise DesktopConnectionInterrupted('Codex 对话状态读取尚未完成') from None
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
        # The desktop retains this request in its optimistic turn state.
        # Its native transcript/title consumers require text_elements even for
        # plain user text, before an app-server round trip can normalize it.
        # Keep the public sender text-only; construct empty annotations here
        # rather than accepting caller-supplied placeholders or app metadata.
        wire_request = dict(request, input=[dict(inputs[0], text_elements=[])])
        if not self.owner:
            self.discover()
        result = self._call('thread-follower-start-turn', {'conversationId': self.thread_id,
                            'turnStart': {'request': wire_request, 'context': {'inheritThreadSettings': True}}}, target=self.owner, mutation=True)
        inner = result.get('result')
        if not isinstance(inner, dict):
            raise UnconfirmedUpdate('消息已提交，但桌面未返回可核对的发送回执，请查看 Codex')
        return inner
