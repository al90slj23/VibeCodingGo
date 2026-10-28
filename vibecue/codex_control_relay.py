"""Owner-bound local control for an explicitly opted-in Codex stdio bridge.

The control socket is private and accepts three high-level operations only.
Backend RPCs are restricted to model catalog, loaded/read metadata, and sparse
thread settings. No turn, input, resume, permission decision or auth RPC is ever
generated. Normal desktop messages are returned byte-for-byte unchanged.
"""
from collections import OrderedDict
from contextlib import suppress
import json
import fcntl
import math
import os
from pathlib import Path
import socket
import select
import stat
import tempfile
import threading
import time
import uuid

from .events import vibecue_directory
from .codex_control import Controller, ControlError, UnconfirmedUpdate, fields_match, settings_subset, THREAD_ID

MAX_FRAME = 8 * 1024 * 1024
MAX_CONTROL_REQUEST = 65536
ALLOWED_METHODS = frozenset(('model/list', 'thread/loaded/list', 'thread/read', 'thread/settings/update'))


def atomic_pipe_write(fd, payload, deadline):
    """Bounded injection that can never leave a partial JSON frame in stdin."""
    atomic_size = os.fpathconf(fd, 'PC_PIPE_BUF')
    if len(payload) > atomic_size:
        raise ControlError('此设置请求过长，无法安全插入 Codex 连接；未发送。')
    original = fcntl.fcntl(fd, fcntl.F_GETFL)
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, original | os.O_NONBLOCK)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
                raise ControlError('Codex 输入连接繁忙，设置请求未发送。')
            try:
                written = os.write(fd, payload)
            except BlockingIOError:
                continue
            if written != len(payload):
                raise OSError('atomic pipe write was incomplete')
            return
    finally:
        with suppress(OSError): fcntl.fcntl(fd, fcntl.F_SETFL, original)


def private_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError('Codex 控制目录必须由当前用户私有持有')
    return path


class ControlHub:
    def __init__(self, write_backend, backend_pid, home=None, clock=time.time, write_control=None):
        self.write_backend = write_backend
        self.write_control = write_control or (lambda payload, deadline: self.write_backend(payload))
        self.backend_pid = backend_pid
        self.home = vibecue_directory(home)
        self.folder = private_directory(self.home / 'control')
        self.session = uuid.uuid4().hex
        self.prefix = 'vibecue-control:' + self.session + ':'
        name = str(os.getpid()) + '-' + self.session[:8]
        candidate = self.folder / (name + '.sock')
        if len(os.fsencode(candidate)) >= 100:
            candidate = private_directory(Path('/private/tmp') / ('vibecue-control-' + str(os.getuid()))) / (name + '.sock')
        self.socket_path = candidate
        self.health_path = self.folder / (name + '.json')
        self.clock = clock
        self.boot_time = self.clock() - time.monotonic()
        self.closed = threading.Event()
        self.condition = threading.Condition(threading.RLock())
        self.wire_lock = threading.RLock()
        self.client_boundary = True
        self.send_queue = OrderedDict()
        self.pending = {}
        self.thread_settings = OrderedDict()
        self.revision = 0
        self.counter = 0
        self.initialize_id = None
        self.initialize_ack = False
        self.initialized_client = False
        self.protocol_valid = True
        self.client_buffer = bytearray()
        self.client_drain = False
        self.server_buffer = bytearray()
        self.server_drain = False
        self.server_drop = False
        self.listener = None
        self.bound = False
        self.wrote_health = False
        self.active_client = None
        self.worker = self.heartbeat_worker = None

    def start(self):
        if self.closed.is_set() or self.listener is not None:
            raise OSError('Codex 控制服务已关闭或启动')
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.socket_path)); self.bound = True
        os.chmod(self.socket_path, 0o600)
        self.listener.listen(4); self.listener.settimeout(.25)
        self.worker = threading.Thread(target=self._serve, name='vibecue-codex-control', daemon=True)
        self.heartbeat_worker = threading.Thread(target=self._heartbeat, name='vibecue-codex-control-health', daemon=True)
        self.worker.start()
        self.heartbeat_worker.start()

    @property
    def ready(self):
        return not self.closed.is_set() and self.protocol_valid and self.initialize_ack and self.initialized_client

    def _health(self):
        value = {'version': 1, 'pid': os.getpid(), 'backend_pid': self.backend_pid,
                 'last_seen': self.clock(), 'ready': self.ready, 'socket_path': str(self.socket_path),
                 'session_id': self.session, 'boot_time': self.boot_time}
        fd, temporary = tempfile.mkstemp(prefix='.control-', dir=self.folder)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(value, stream)
            os.replace(temporary, self.health_path)
            self.wrote_health = True
        finally:
            with suppress(FileNotFoundError): os.unlink(temporary)

    def client_forwarded(self, chunk):
        """Observe only initialization after the desktop's bytes were copied."""
        for part in bytes(chunk).splitlines(keepends=True):
            complete = part.endswith(b'\n')
            if not self.client_drain:
                self.client_buffer.extend(part)
                if len(self.client_buffer) > MAX_FRAME:
                    self.client_buffer.clear(); self.client_drain = True
            if not complete:
                continue
            if not self.client_drain:
                try:
                    value = json.loads(self.client_buffer)
                    if isinstance(value, dict):
                        if value.get('method') == 'initialize' and 'id' in value:
                            self.initialize_id = value['id']
                        elif value.get('method') == 'initialized':
                            self.initialized_client = True
                except (ValueError, UnicodeError, RecursionError):
                    pass
            self.client_buffer.clear(); self.client_drain = False

    def forward_client(self, chunk):
        """Serialize control injection only between complete desktop JSON lines.

        A shared per-chunk lock alone is insufficient: a user request can span
        many chunks. Queued controls wait until the original line is complete.
        """
        with self.wire_lock:
            self.write_backend(chunk)
            self.client_forwarded(chunk)
            if chunk:
                self.client_boundary = bytes(chunk).endswith(b'\n')
            if self.client_boundary:
                queued = list(self.send_queue.items())
                self.send_queue.clear()
                for identifier, item in queued:
                    payload, deadline = item
                    with self.condition:
                        pending = self.pending.get(identifier)
                        if pending is None or deadline <= time.monotonic():
                            continue
                        pending['sent'] = True
                    try:
                        self.write_control(payload, min(deadline, time.monotonic()+.1))
                    except (OSError, ControlError) as error:
                        # A busy control path must not break desktop forwarding.
                        with self.condition:
                            pending['response'] = {'error': {'message': str(error)}}
                            pending['event'].set()

    def _send_control(self, identifier, payload, deadline):
        if not self.wire_lock.acquire(timeout=max(0, deadline-time.monotonic())):
            raise ControlError('Codex 正在转发较长请求，设置请求尚未发送。')
        try:
            if self.client_boundary:
                with self.condition:
                    self.pending[identifier]['sent'] = True
                self.write_control(payload, min(deadline, time.monotonic()+.1))
            else:
                if len(self.send_queue) >= 128:
                    raise ControlError('Codex 控制请求过多，请稍后重试')
                self.send_queue[identifier] = (payload, deadline)
        finally:
            self.wire_lock.release()

    def _receive(self, frame):
        try:
            value = json.loads(frame)
        except (ValueError, UnicodeError, RecursionError):
            return False
        if not isinstance(value, dict):
            return False
        identifier = value.get('id')
        if isinstance(identifier, str) and identifier.startswith(self.prefix) and value.get('method') is None:
            with self.condition:
                pending = self.pending.get(identifier)
                if pending is not None:
                    pending['response'] = value
                    pending['event'].set()
            return True
        if value.get('method') is None and identifier == self.initialize_id and 'result' in value:
            self.initialize_ack = True
        if value.get('method') == 'thread/settings/updated':
            params = value.get('params')
            if isinstance(params, dict) and isinstance(params.get('threadId'), str):
                settings = settings_subset(params.get('threadSettings'))
                with self.condition:
                    self.revision += 1
                    self.thread_settings[params['threadId']] = (settings, self.revision)
                    self.thread_settings.move_to_end(params['threadId'])
                    while len(self.thread_settings) > 512:
                        self.thread_settings.popitem(last=False)
                    self.condition.notify_all()
        return False

    def server_chunk(self, chunk):
        """Remove our own RPC replies; preserve every other protocol byte.

        Frames are bounded. An oversized normal desktop frame passes through
        and disables control instead of interrupting the user's work. Current
        Codex serializes response IDs before results; an oversized identified
        control response is drained privately and fails its request.
        """
        output = bytearray()
        for part in bytes(chunk).splitlines(keepends=True):
            complete = part.endswith(b'\n')
            if self.server_drain:
                if not self.server_drop:
                    output.extend(part)
            else:
                self.server_buffer.extend(part)
                if len(self.server_buffer) > MAX_FRAME:
                    self.protocol_valid = False
                    marker = self.prefix.encode()
                    self.server_drop = marker in self.server_buffer[:65536]
                    if not self.server_drop:
                        output.extend(self.server_buffer)
                    self.server_buffer.clear(); self.server_drain = True
            if not complete:
                continue
            if not self.server_drain and not self._receive(self.server_buffer):
                output.extend(self.server_buffer)
            self.server_buffer.clear(); self.server_drain = self.server_drop = False
        return bytes(output)

    def _validate_rpc(self, method, params):
        if method not in ALLOWED_METHODS or not isinstance(params, dict):
            raise ControlError('Codex 控制通道禁止此操作')
        if method == 'model/list':
            if set(params) - {'cursor', 'includeHidden', 'limit'} or params.get('includeHidden') is not False or params.get('limit') != 100:
                raise ControlError('模型查询参数无效')
        elif method == 'thread/loaded/list':
            if set(params) - {'cursor'} or ('cursor' in params and not isinstance(params['cursor'], str)):
                raise ControlError('对话查询参数无效')
        else:
            tid = params.get('threadId')
            if not isinstance(tid, str) or not THREAD_ID.fullmatch(tid):
                raise ControlError('对话标识无效')
            if method == 'thread/read':
                if set(params) != {'threadId', 'includeTurns'} or params['includeTurns'] is not False:
                    raise ControlError('控制通道不读取对话正文')
            else:
                if set(params) - {'threadId', 'model', 'effort', 'serviceTier'} or len(params) < 2:
                    raise ControlError('只允许修改模型、强度和快速档位')
                for field in ('model', 'effort', 'serviceTier'):
                    if field in params and not (isinstance(params[field], str) and 0 < len(params[field]) <= 180) and not (field == 'serviceTier' and params[field] is None):
                        raise ControlError('模型设置参数无效')

    def request(self, method, params, timeout=8):
        self._validate_rpc(method, params)
        if not self.ready:
            raise ControlError('Codex 桌面控制连接尚未就绪')
        with self.condition:
            self.counter += 1
            identifier = self.prefix + str(self.counter)
            pending = {'event': threading.Event()}
            self.pending[identifier] = pending
        deadline = time.monotonic()+timeout
        try:
            payload = json.dumps({'id': identifier, 'method': method, 'params': params}, separators=(',', ':')).encode() + b'\n'
            try:
                self._send_control(identifier, payload, deadline)
            except (OSError, ValueError) as error:
                if method == 'thread/settings/update' and pending.get('sent'):
                    raise UnconfirmedUpdate('设置请求可能已发送，连接已中断；请在 Codex 中核对，勿自动重试。') from error
                raise ControlError('Codex 控制请求未能发送') from error
            if not pending['event'].wait(max(0, deadline-time.monotonic())) or self.closed.is_set():
                if method == 'thread/settings/update' and pending.get('sent'):
                    raise UnconfirmedUpdate('设置请求已发送，但确认超时或连接已关闭；请在 Codex 中核对。')
                raise ControlError('Codex 控制请求超时或连接已关闭')
            response = pending.get('response', {})
            if 'error' in response:
                error = response['error']
                message = error.get('message') if isinstance(error, dict) else None
                raise ControlError(str(message or 'Codex 拒绝了此设置')[:300])
            if not isinstance(response.get('result'), dict):
                raise ControlError('Codex 控制响应无效')
            return response['result']
        finally:
            with self.condition:
                self.pending.pop(identifier, None)
            if self.wire_lock.acquire(blocking=False):
                try:self.send_queue.pop(identifier, None)
                finally:self.wire_lock.release()

    def settings(self, identifier):
        with self.condition:
            value, revision = self.thread_settings.get(identifier, ({}, 0))
            return dict(value), revision

    def wait_settings(self, identifier, after, patch, timeout=2):
        deadline = time.monotonic() + timeout
        with self.condition:
            while not self.closed.is_set():
                settings, revision = self.thread_settings.get(identifier, ({}, 0))
                if revision > after and fields_match(settings, patch):
                    return dict(settings)
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.condition.wait(left)
        return None

    def _serve_client(self, client):
        with client:
            client.settimeout(10)
            try:
                stream = client.makefile('rb')
                raw = stream.readline(MAX_CONTROL_REQUEST + 1)
                if len(raw) > MAX_CONTROL_REQUEST or not raw.endswith(b'\n'):
                    raise ControlError('控制请求过大或不完整')
                value = json.loads(raw)
                if not isinstance(value, dict) or set(value) != {'operation', 'params', 'deadline'} or not isinstance(value['params'], dict):
                    raise ControlError('控制请求无效')
                deadline = value['deadline']
                if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline) or deadline <= time.monotonic() or deadline > time.monotonic()+90:
                    raise ControlError('批量请求已到时或期限无效，没有继续发送设置。')
                if not self.ready:
                    raise ControlError('Codex 桌面控制连接尚未就绪')
                controller = Controller(self, deadline=min(deadline, time.monotonic()+85))
                operation = value['operation']
                if operation == 'catalog':
                    response = controller.catalog()
                elif operation == 'inspect':
                    response = controller.inspect(value['params'])
                elif operation == 'apply':
                    response = controller.apply(value['params'])
                else:
                    raise ControlError('不支持的控制操作')
            except (ControlError, OSError, ValueError, TypeError, KeyError) as error:
                response = {'connected': False, 'error': str(error)[:300]}
            with suppress(OSError):
                client.sendall(json.dumps(response, ensure_ascii=False).encode() + b'\n')

    def _serve(self):
        while not self.closed.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # Serial batches prevent this tool from racing with itself. The
            # server relay and the actual desktop remain independent threads.
            self.active_client = client
            try:
                self._serve_client(client)
            finally:
                self.active_client = None

    def _heartbeat(self):
        while not self.closed.is_set():
            with suppress(OSError): self._health()
            self.closed.wait(1)

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.listener is not None:
            with suppress(OSError): self.listener.close()
        if self.active_client is not None:
            with suppress(OSError): self.active_client.shutdown(socket.SHUT_RDWR)
        with self.condition:
            for pending in self.pending.values():
                pending['event'].set()
            self.condition.notify_all()
        if self.heartbeat_worker is not None and threading.current_thread() is not self.heartbeat_worker:
            self.heartbeat_worker.join(timeout=1)
        for path in ([self.socket_path] if self.bound else []) + ([self.health_path] if self.wrote_health else []):
            with suppress(OSError): path.unlink()
