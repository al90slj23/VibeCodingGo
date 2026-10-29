"""Read-only, reconnecting attention subscription to existing desktop owners.

One private desktop socket follows known chats without claiming ownership,
resuming them, or answering requests. Full initial snapshots are transient;
only request identifiers and runtime status survive projection. Later patches
are applied to that projection, so streaming message bodies are never cached.
"""
from collections import OrderedDict
from dataclasses import dataclass
import json
from pathlib import Path
import select
import socket
import threading
import time

from .codex_control import ControlError, THREAD_ID
from .codex_desktop_ipc import DesktopChannel, DesktopOwnerUnavailable
from .codex_runtime import RuntimeProjection
from .current_model_settings import ModelSettingsProjection, unavailable_settings
from .model_presentation import ModelPresentation
from .events import emit_event, emit_resolution, event_identifier, input_event_id
from .providers.approval_bridge import APPROVAL_METHODS, INPUT_METHODS, SETTLE_SECONDS

SOURCE = 'codex client approval stream'
MAX_THREADS = 64
MAX_REQUESTS = 512
REQUEST_METHODS = APPROVAL_METHODS | INPUT_METHODS | {'item/plan/requestImplementation'}


def request_metadata(value):
    """Keep no command, tool arguments, URL, challenge or question text."""
    if not isinstance(value, dict):
        raise ControlError('对话请求状态无效')
    result = {}
    identifier = value.get('id')
    if type(identifier) is int and -(2**63) <= identifier < 2**63 or isinstance(identifier, str) and 0 < len(identifier) <= 512:
        result['id'] = identifier
    if isinstance(value.get('method'), str) and value['method'] in REQUEST_METHODS:
        result['method'] = value['method']
    if 'completed' in value:
        result['completed'] = value['completed'] is True
    params = value.get('params')
    if isinstance(params, dict):
        result['params'] = {key: raw for key, raw in params.items()
                            if key in ('threadId', 'conversationId', 'turnId', 'itemId')
                            and isinstance(raw, str) and len(raw) <= 512}
    return result


def request_list(value):
    if not isinstance(value, list) or len(value) > MAX_REQUESTS:
        raise ControlError('对话请求列表尚不完整')
    return [request_metadata(row) for row in value]


class AttentionProjection:
    """A positional projection of only requests; never an Immer body cache."""
    def __init__(self, thread, owner):
        self.thread = thread
        self.owner = owner
        self.revision = None
        self.requests = []
        self.settings = ModelSettingsProjection(thread, owner)
        self.runtime = RuntimeProjection(thread, owner)

    def apply(self, change):
        if not isinstance(change, dict):
            raise ControlError('对话订阅更新无效')
        revision = change.get('revision')
        if type(revision) is not int or revision < 0:
            raise ControlError('对话订阅版本无效')
        if change.get('type') == 'snapshot':
            if self.revision is not None and revision < self.revision:
                raise ControlError('对话订阅快照早于已观察版本')
            state = change.get('conversationState')
            if not isinstance(state, dict) or state.get('id') not in (None, self.thread):
                raise ControlError('对话快照与所选对话不一致')
            self.requests = request_list(state.get('requests'))
        elif change.get('type') == 'patches':
            if self.revision is None or change.get('baseRevision') != self.revision or revision <= self.revision:
                raise ControlError('对话订阅出现版本缺口，正在重新连接')
            patches = change.get('patches')
            if not isinstance(patches, list) or len(patches) > 10000:
                raise ControlError('对话订阅增量无效')
            for patch in patches:
                self._patch(patch)
        else:
            raise ControlError('对话订阅更新类型尚未核对')
        self.settings.apply(change)
        self.runtime.apply(change)
        self.revision = revision

    def _patch(self, patch):
        if not isinstance(patch, dict) or not isinstance(patch.get('path'), list):
            raise ControlError('对话订阅增量无效')
        path, op = patch['path'], patch.get('op')
        if not path:
            raise ControlError('对话订阅整体替换需要重新连接')
        if path[0] != 'requests':
            return  # Message deltas and all other conversation fields die here.
        if op not in ('add', 'remove', 'replace'):
            raise ControlError('对话请求增量类型无效')
        if len(path) == 1:
            if op == 'remove':
                raise ControlError('对话请求列表被移除')
            self.requests = request_list(patch.get('value'))
            return
        index = path[1]
        if type(index) is not int or index < 0:
            raise ControlError('对话请求索引无效')
        if len(path) == 2:
            if op == 'add' and index <= len(self.requests) and len(self.requests) < MAX_REQUESTS:
                self.requests.insert(index, request_metadata(patch.get('value')))
            elif op == 'replace' and index < len(self.requests):
                self.requests[index] = request_metadata(patch.get('value'))
            elif op == 'remove' and index < len(self.requests):
                self.requests.pop(index)
            else:
                raise ControlError('对话请求索引不连续')
            return
        if index >= len(self.requests):
            raise ControlError('对话请求索引不连续')
        row = self.requests[index]
        key = path[2]
        if key not in ('id', 'method', 'completed', 'params'):
            return
        if len(path) == 3:
            if op == 'remove':
                row.pop(key, None)
            else:
                clean = request_metadata({key: patch.get('value')})
                row.pop(key, None)
                row.update(clean)
        elif key == 'params' and len(path) == 4 and path[3] in ('threadId', 'conversationId', 'turnId', 'itemId'):
            params = row.setdefault('params', {})
            params.pop(path[3], None)
            value = patch.get('value')
            if op != 'remove' and isinstance(value, str) and len(value) <= 512:
                params[path[3]] = value


@dataclass
class _Pending:
    thread: str
    turn: str
    kind: str
    raw_key: str
    observed: float
    target: str = ''
    emitted: bool = False


class DesktopAttentionMonitor:
    def __init__(self, home=None, *, clock=time.monotonic, emit=None, resolve=None):
        self.home = home
        self.clock = clock
        self.emit = emit or emit_event
        self.resolve = resolve or emit_resolution
        self.pending = OrderedDict()
        self.emit_enabled = True

    def update(self, projection):
        observed = set()
        runtime = projection.runtime.snapshot()
        for request in projection.requests:
            method = request.get('method')
            if method not in REQUEST_METHODS or 'id' not in request or request.get('completed') is True:
                continue
            params = request.get('params', {})
            if runtime.get('authoritative') and (runtime['state'] in ('complete', 'aborted') or
                    (params.get('turnId') and params['turnId'] != runtime.get('turn_id'))):
                continue
            thread = params.get('threadId') or params.get('conversationId')
            if thread and thread != projection.thread:
                continue
            identifier = request['id']
            key = (projection.thread, type(identifier).__name__, str(identifier))
            observed.add(key)
            if key in self.pending:
                continue
            turn = params.get('turnId', '')
            raw_key = 'desktop-approval:' + json.dumps([projection.owner, *key, turn, method], separators=(',', ':'))
            if method in ('item/tool/requestUserInput', 'tool/requestUserInput') and params.get('itemId'):
                raw_key = input_event_id(projection.thread, turn, params['itemId'])
            self.pending[key] = _Pending(projection.thread, turn, 'permission' if method in APPROVAL_METHODS else 'input', raw_key, self.clock())
        for key in list(self.pending):
            if key[0] == projection.thread and key not in observed:
                self._forget(key)
        while len(self.pending) > MAX_REQUESTS:
            self._forget(next(iter(self.pending)))

    def _forget(self, key):
        pending = self.pending.pop(key, None)
        if pending and pending.target:
            try:
                self.resolve(pending.target, thread_id=pending.thread, vibecue_home=self.home, source=SOURCE)
            except Exception:
                pass

    def gap(self, thread=None):
        for key in list(self.pending):
            if thread is None or key[0] == thread:
                self._forget(key)

    def settled_pending(self):
        return [pending for pending in self.pending.values()
                if self.clock() - pending.observed >= SETTLE_SECONDS]

    def flush(self):
        if not self.emit_enabled:
            return
        now = self.clock()
        for key, pending in list(self.pending.items()):
            if pending.emitted or now - pending.observed < SETTLE_SECONDS:
                continue
            try:
                pending.target = event_identifier(pending.raw_key)
                self.emit(kind=pending.kind, thread_id=pending.thread, turn_id=pending.turn,
                          event_id=pending.raw_key, message='Codex 正在等待你手动批准' if pending.kind == 'permission' else 'Codex 正在等待你回答或确认',
                          source=SOURCE, requires_user_action=True, vibecue_home=self.home)
                pending.emitted = True
            except Exception:
                self._forget(key)


class _AttentionChannel(DesktopChannel):
    # Long conversations can legitimately exceed the sender's 8 MiB limit.
    # Only this read-only stream has a larger finite budget; each snapshot is
    # immediately reduced to safe request IDs and model settings, never cached.
    max_frame_size = 32 * 1024 * 1024
    def __init__(self, *args, on_message, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_message = on_message
        self.discovery_request_id = None
        self.clean_read_timeout = False
        self._reading_header = False

    def _read_exact(self, length, deadline):
        # Read-only discovery may return no response at all when no window can
        # own an old chat. A timeout before any header byte is a clean boundary;
        # partial headers/bodies still require closing the entire connection.
        header = self._reading_header
        self._reading_header = False
        pieces = []
        try:
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
        except (TimeoutError, socket.timeout):
            self.clean_read_timeout = header and not pieces
            raise

    def discover(self):
        self.clean_read_timeout = False
        try:
            return super().discover()
        except ControlError:
            if self.clean_read_timeout:
                raise DesktopOwnerUnavailable('Codex 当前没有此对话的可用窗口连接') from None
            raise

    def _send(self, value):
        if value.get('type') == 'request' and value.get('method') == 'thread-owner-discovery':
            # The router's negative discovery response must arrive before our
            # socket read deadline; equal deadlines create timeout races.
            value = dict(value, timeoutMs=max(100, int(self.timeout * 500)))
            self.discovery_request_id = value['requestId']
        return super()._send(value)

    def _receive(self, deadline):
        self._reading_header = True
        try:
            message = super()._receive(deadline)
        except ControlError as error:
            if '数据过大或无效' in str(error):
                self.on_message({'type': 'vibecue-observation-gap', 'reason': 'oversized-frame'})
            raise
        if (message.get('type') == 'response' and message.get('requestId') == self.discovery_request_id
                and message.get('resultType') != 'success' and message.get('error') in ('request-timeout', 'client-disconnected')):
            message = dict(message, error='no-client-found')
        self.on_message(message)
        return message


class DesktopAttentionService:
    """One bounded subscription connection; automatically retries after loss."""
    def __init__(self, home=None, codex_home=None, *, channel_factory=_AttentionChannel):
        self.home, self.codex_home = home, codex_home
        self.channel_factory = channel_factory
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._threads = []
        self._stdio = False
        self._health = {'connected': False, 'status': 'reconnecting', 'message': '正在自动连接 Codex', 'last_seen': 0,
                        'capabilities': {'reply': False, 'control': False, 'attention': False}}
        self.monitor = DesktopAttentionMonitor(home)
        self.model_presentation = ModelPresentation(codex_home)
        self.channel = None
        self.projections = {}
        self.next_discovery = {}
        self.subscribed_at = {}
        self.quarantine_until = {}
        self.unavailable_reasons = {}
        self._reconnect_stamp = None
        self._reconnect_seen = False

    def set_threads(self, threads):
        values = []
        for row in threads:
            value = row.get('thread_id') if isinstance(row, dict) else row
            if isinstance(value, str) and THREAD_ID.fullmatch(value) and value not in values:
                values.append(value)
        with self._lock:
            self._threads = values[:MAX_THREADS]

    def set_stdio_connected(self, connected):
        with self._lock:
            self._stdio = connected is True

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name='vibecue-desktop-attention', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        with self._lock:
            channel = self.channel
            connection = channel.socket if channel else None
        if connection:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def snapshot(self):
        with self._lock:
            health = dict(self._health, capabilities=dict(self._health['capabilities']))
            if self._thread and self._thread.is_alive():
                health['last_seen'] = time.time()  # Live helper health, even during retry backoff.
            pending = self.monitor.settled_pending()
            health.update(pending_count=len(pending), pending_thread_ids=sorted({item.thread for item in pending}),
                          live_sources=1 if health['connected'] else 0, source='codex-desktop-owner-stream',
                          subscribed_threads=sum(projection.revision is not None for projection in self.projections.values()),
                          candidate_threads=len(self._threads), candidate_limit=MAX_THREADS,
                          quarantined_threads=sum(until > time.monotonic() for until in self.quarantine_until.values()))
            return health

    def current_settings(self):
        """Export only the settings of each verified owner subscription."""
        with self._lock:
            if not self._health['connected']:
                return {thread: unavailable_settings(reason='Codex 连接暂不可用') for thread in self._threads}
            result = {}
            for thread in self._threads:
                projection = self.projections.get(thread)
                if projection is not None and projection.revision is not None:
                    result[thread] = self.model_presentation.decorate(projection.settings.snapshot())
                else:
                    reason = self.unavailable_reasons.get(thread) or ('此对话的 Codex 快照暂不可读' if self.quarantine_until.get(thread, 0) > time.monotonic() else '正在读取此对话的 Codex 当前设置')
                    result[thread] = unavailable_settings(reason=reason)
            return result

    def current_runtime(self):
        """Export only current, owner-bound turn states on a live connection."""
        with self._lock:
            if not self._health['connected']:
                return {}
            waiting = {pending.thread for pending in self.monitor.settled_pending()}
            result = {}
            for thread, projection in self.projections.items():
                if projection.revision is None:
                    continue
                state = projection.runtime.snapshot()
                # Approval review can resolve automatically in milliseconds.
                # Only a surviving protocol request is a human wait.
                if state.get('state') == 'waiting' and thread not in waiting:
                    state = dict(state, state='running')
                elif state.get('state') in ('running', 'waiting') and thread in waiting:
                    state = dict(state, state='waiting')
                result[thread] = state
            return result

    def _on_message(self, message):
        if message.get('type') == 'vibecue-observation-gap':
            # Only one new subscription may await a snapshot. Therefore an
            # oversized initial frame can be quarantined without guessing from
            # a truncated body. Close the connection; never consume half-frame.
            with self._lock:
                pending = [projection for projection in self.projections.values() if projection.revision is None]
                if len(pending) == 1:
                    self.quarantine_until[pending[0].thread] = time.monotonic() + 300
                    self.unavailable_reasons[pending[0].thread] = '此对话的状态快照过大，暂时无法核实当前设置'
            return
        if message.get('type') != 'broadcast':
            return
        if message.get('method') == 'ipc-connection-reset':
            raise ControlError('Codex 连接已重建，正在自动重连')
        if message.get('method') == 'client-status-changed':
            params = message.get('params')
            if isinstance(params, dict) and params.get('status') == 'disconnected' and any(projection.owner == params.get('clientId') for projection in self.projections.values()):
                raise ControlError('对话窗口连接已变化，正在自动恢复')
        if message.get('method') != 'thread-stream-state-changed' or message.get('version') != 11:
            return
        params = message.get('params')
        if not isinstance(params, dict) or params.get('hostId') != 'local':
            return
        projection = self.projections.get(params.get('conversationId'))
        targets = message.get('targetClientIds')
        if projection is None or message.get('sourceClientId') != projection.owner or not isinstance(targets, list) or not all(isinstance(target, str) for target in targets) or self.channel.client_id not in targets:
            return
        with self._lock:
            try:
                projection.apply(params.get('change'))
            except ControlError:
                # One drifted thread must not repeatedly disconnect healthy
                # chats immediately after each global reconnect.
                self.quarantine_until[projection.thread] = time.monotonic() + 30
                raise
            self.monitor.update(projection)
            self.unavailable_reasons.pop(projection.thread, None)
            self._health['last_seen'] = time.time()
            self._health['capabilities']['attention'] = True

    def _follow(self, thread, owner, value):
        self.channel._send({'type': 'broadcast', 'sourceClientId': self.channel.client_id,
                            'targetClientIds': [owner], 'method': 'thread-stream-following-changed', 'version': 1,
                            'params': {'conversationId': thread, 'hostId': 'local', 'following': value}})

    def _discover(self, desired):
        with self._lock:
            existing = list(self.projections)
        for thread in existing:
            projection = self.projections[thread]
            missing_snapshot = projection.revision is None and time.monotonic() - self.subscribed_at.get(thread, 0) > 5
            invalidated = projection.settings.invalidated or projection.runtime.invalidated
            if thread not in desired or missing_snapshot or invalidated:
                with self._lock:
                    projection = self.projections.pop(thread)
                    if missing_snapshot:
                        self.unavailable_reasons[thread] = 'Codex 未返回此对话的当前状态快照'
                self._follow(thread, projection.owner, False)
                with self._lock:
                    self.monitor.gap(thread)
                    if invalidated:
                        self.next_discovery.pop(thread, None)
        # Do not send another discovery while a full initial snapshot is in
        # flight: this keeps size/gap attribution local to one candidate.
        if any(projection.revision is None for projection in self.projections.values()):
            return
        attempted = 0
        candidates = sorted(desired, key=lambda thread: self.next_discovery.get(thread, 0))
        for thread in candidates:
            if thread in self.projections or time.monotonic() < max(self.next_discovery.get(thread, 0), self.quarantine_until.get(thread, 0)):
                continue
            attempted += 1
            self.next_discovery[thread] = time.monotonic() + 15
            self.channel.thread_id = thread
            try:
                self.channel.discover()
                projection = AttentionProjection(thread, self.channel.owner)
                with self._lock:
                    self.projections[thread] = projection
                self.subscribed_at[thread] = time.monotonic()
                self._follow(thread, projection.owner, True)
            except DesktopOwnerUnavailable:
                with self._lock:
                    self.unavailable_reasons[thread] = '此对话未在 Codex 加载，打开后自动读取当前设置'
                # Not open: do not load or claim it just to obtain state.
            except ControlError as error:
                if '不支持安全的外部消息输入' not in str(error):
                    # A read deadline may expire while the router is proving
                    # no owner exists. Closing preserves frame boundaries;
                    # quarantine this candidate so reconnect can serve others.
                    self.quarantine_until[thread] = time.monotonic() + 300
                    raise
                self.quarantine_until[thread] = time.monotonic() + 30
            if attempted >= 1:
                break

    def _run(self):
        delay = 1
        self._reconnect_stamp = self._retry_stamp()
        self._reconnect_seen = True
        while not self._stop.is_set():
            opened_at = time.monotonic()
            try:
                channel = self.channel_factory('vibecue-health', self.codex_home, timeout=3, on_message=self._on_message)
                with self._lock:
                    self.channel = channel
                with channel:
                    with self._lock:
                        self._health.update(connected=True, status='connected', message='已自动复用当前 Codex 连接', last_seen=time.time(),
                                            capabilities={'reply': True, 'control': True, 'attention': False})
                    while not self._stop.is_set():
                        if time.monotonic() - opened_at >= 10:
                            delay = 1
                        stamp = self._retry_stamp()
                        if self._reconnect_seen and stamp != self._reconnect_stamp:
                            self._reconnect_stamp = stamp
                            raise ControlError('正在重新核对 Codex 连接')
                        self._reconnect_stamp = stamp
                        self._reconnect_seen = True
                        with self._lock:
                            desired, stdio = list(self._threads), self._stdio
                            self.monitor.emit_enabled = not stdio
                        self._discover(desired)
                        readable, _, _ = select.select([channel.socket], [], [], .1)
                        drained = 0
                        while readable and drained < 64 and not self._stop.is_set():
                            channel._receive(time.monotonic() + 2)
                            drained += 1
                            readable, _, _ = select.select([channel.socket], [], [], 0)
                        with self._lock:
                            # Process already-arrived resolutions before any
                            # settle-window alert, including a busy delta queue.
                            if not readable:
                                self.monitor.flush()
                            # An unchanged setting remains valid while its
                            # same-owner subscription continues on this socket.
                            # Preserve the original setting observation time.
                            for projection in self.projections.values():
                                projection.settings.mark_live()
                                projection.runtime.mark_live()
                            self._health['last_seen'] = time.time()
            except (ControlError, OSError, ValueError, TypeError, RecursionError, OverflowError) as error:
                message = str(error) if isinstance(error, ControlError) else 'Codex 连接暂不可用，正在自动重试'
                unsupported = '版本' in message or '协议' in message
                with self._lock:
                    self._health.update(connected=False, status='unsupported' if unsupported else 'reconnecting', message=message,
                                        last_seen=time.time(),
                                        capabilities={'reply': False, 'control': False, 'attention': False})
            finally:
                channel = self.channel
                if channel and channel.socket is not None:
                    for thread, projection in list(self.projections.items()):
                        try:
                            self._follow(thread, projection.owner, False)
                        except (OSError, ControlError):
                            break
                    channel.close()
                with self._lock:
                    self.monitor.gap()
                    self.projections.clear(); self.next_discovery.clear(); self.subscribed_at.clear(); self.channel = None
            if self._wait_retry(delay):
                break
            delay = min(30, delay * 2)
        with self._lock:
            self._health.update(connected=False, status='disconnected', message='本机监控已停止',
                                capabilities={'reply': False, 'control': False, 'attention': False})

    def _retry_stamp(self):
        try:
            return (Path(self.home) / 'desktop-reconnect.request').stat().st_mtime_ns
        except (OSError, TypeError):
            return None

    def _wait_retry(self, delay):
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if self._stop.wait(min(.2, max(0, deadline - time.monotonic()))):
                return True
            stamp = self._retry_stamp()
            if stamp != self._reconnect_stamp:
                self._reconnect_stamp = stamp
                return False
        return self._stop.is_set()
