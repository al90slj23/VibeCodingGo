"""Discover native temporary side chats without reading or retaining bodies.

Ordinary desktop log identity events supply candidates only. An existing owner
snapshot must prove the side-conversation flags and parent identity. Discovery
uses a separate, small read-only connection so it cannot delay approval alerts
or consume the main conversation subscription budget.
"""
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
import json
import math
import os
import re
import select
import socket
import threading
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from .codex_control import ControlError
from .codex_desktop_ipc import DesktopOwnerUnavailable
from .events import codex_directory, vibecue_directory

SOURCE = 'codex-desktop-owner-stream'
MAX_CANDIDATES = 48
MAX_SIDE_CHATS = 12
MAX_LOG_FILES = 12
MAX_LOG_TAIL = 4 * 1024 * 1024
MAX_LOG_LINE = 8192
SCAN_SECONDS = 30
RETRY_SECONDS = 180
RECONNECT_SECONDS = 2
UUID_PATTERN = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
LOG_ID = re.compile(rb'\bconversationId=(' + UUID_PATTERN.encode() + rb')(?=\s|$)')
OWNER_ROLE = re.compile(rb'\brole=owner(?=\s|$)')
LOG_EVENT = re.compile(rb'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z info \[electron-message-handler\] thread_stream_role_changed\s')


def identifier(value):
    if not isinstance(value, str) or re.fullmatch(UUID_PATTERN, value) is None:
        return None
    try:
        return value if str(uuid.UUID(value)) == value else None
    except ValueError:
        return None


def relationship_metadata(state, thread):
    """Reject generic forks, prediction forks, subagents and ambiguous parents."""
    if (not isinstance(state, dict) or state.get('id') != thread or not identifier(thread)
            or state.get('sideConversation') is not True or state.get('ephemeral') is not True
            or state.get('hostId') not in (None, 'local') or state.get('parentThreadId') is not None):
        return None
    source = state.get('source')
    if isinstance(source, dict) and any(key in source for key in ('subAgent', 'subagent')):
        return None
    if state.get('threadSource') not in (None, 'user'):
        return None
    parent = identifier(state.get('forkedFromId'))
    if state.get('forkedFromId') is not None and parent is None:
        return None
    path = state.get('sideConversationParentNavigationPath')
    path_parent = None
    if path is not None:
        if not isinstance(path, str) or len(path) > 1024:
            return None
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return None
        match = re.fullmatch('/local/(' + UUID_PATTERN + ')', parsed.path)
        if not match:
            return None
        path_parent = identifier(match[1])
    if parent is None and path_parent is None or parent and path_parent and parent != path_parent:
        return None
    parent = parent or path_parent
    if parent == thread:
        return None
    return {'thread_id': thread, 'parent_thread_id': parent, 'ephemeral': True,
            'title': '侧边聊天 · ' + thread[:8], 'source': SOURCE}


def own_input_title(state, thread):
    """At most 80 visible characters from a complete first *child* turn.

    Native tab titles use the first own prompt; state.title can still be the
    parent's title. Input is inspected only after relationship verification,
    then discarded. Incomplete/multi-island history has no safe first turn.
    """
    canonical = state.get('turnHistory')
    history = canonical.get('history') if isinstance(canonical, dict) and canonical.get('kind') == 'canonical' else None
    if not isinstance(history, dict) or history.get('isComplete') is not True:
        return None
    islands, entities = history.get('islands'), history.get('entitiesByKey')
    if not isinstance(islands, list) or len(islands) != 1 or not isinstance(entities, dict):
        return None
    entries = islands[0].get('entries') if isinstance(islands[0], dict) else None
    first = entries[0].get('value') if isinstance(entries, list) and entries and isinstance(entries[0], dict) else None
    turn = entities.get(first) if isinstance(first, str) else None
    params = turn.get('params') if isinstance(turn, dict) else None
    pagination = turn.get('itemsPagination') if isinstance(turn, dict) else None
    if isinstance(pagination, dict) and pagination.get('hasLoadedOldest') is False:
        return None
    if not isinstance(params, dict) or params.get('threadId') != thread:
        return None
    inputs = params.get('input')
    if (not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], dict)
            or inputs[0].get('type') != 'text' or not isinstance(inputs[0].get('text'), str)):
        return None
    text = inputs[0]['text']
    if len(text) > 16384:
        return None
    marker = list(re.finditer(r'## My request(?: for Codex)?:', text))
    if marker:
        text = text[marker[-1].end():]
    if re.search(r'</?(?:additional_context|environment_context|page-comment-context|app-context|untrusted_text)\b', text, re.I):
        return None
    from .providers.task_details import visible_text
    from .message_display import question_reply_text
    title = ' '.join(visible_text(question_reply_text(text)).split())[:80]
    return title or None


class SideChatCandidates:
    """Inspect bounded identity-log tails across the local/UTC day boundary."""
    def __init__(self, root=None, *, clock=time.time):
        self.root = Path(root) if root is not None else Path.home() / 'Library/Logs/com.openai.codex'
        self.clock = clock
        self.positions = {}

    def scan(self):
        day = datetime.fromtimestamp(self.clock())
        roots = [self.root / (day - timedelta(days=offset)).strftime('%Y/%m/%d') for offset in (0, 1)]
        try:
            files = sorted((p for root in roots for p in root.glob('codex-desktop-*.log') if not p.is_symlink() and p.stat().st_size > 0),
                           key=lambda p: p.stat().st_mtime_ns, reverse=True)[:MAX_LOG_FILES]
        except OSError:
            return []
        results = OrderedDict()
        active = set()
        # Retain newest identities last, including across rotated/day files.
        for path in reversed(files):
            try:
                stat = path.stat()
                if not path.is_file() or stat.st_size == 0:
                    continue
                identity = (stat.st_dev, stat.st_ino)
                active.add(identity)
                previous = self.positions.get(identity)
                start = previous if previous is not None and previous <= stat.st_size else max(0, stat.st_size - MAX_LOG_TAIL)
                start = max(start, stat.st_size - MAX_LOG_TAIL)
                with path.open('rb') as stream:
                    stream.seek(start)
                    if start and previous != start:
                        stream.readline(MAX_LOG_LINE + 1)  # Discard a partial first line.
                    while stream.tell() < stat.st_size:
                        offset = stream.tell()
                        line = stream.readline(min(MAX_LOG_LINE + 1, stat.st_size - offset))
                        if not line.endswith(b'\n'):
                            if len(line) <= MAX_LOG_LINE:
                                stream.seek(offset)
                                break  # Complete it on the next scan.
                            while line and not line.endswith(b'\n') and stream.tell() < stat.st_size:
                                line = stream.readline(min(MAX_LOG_LINE + 1, stat.st_size - stream.tell()))
                            continue
                        if len(line) > MAX_LOG_LINE or not LOG_EVENT.match(line) or not OWNER_ROLE.search(line):
                            continue
                        match = LOG_ID.search(line)
                        if match:
                            thread = match[1].decode('ascii')
                            results[thread] = self.clock()
                            results.move_to_end(thread)
                            while len(results) > MAX_CANDIDATES:
                                results.popitem(last=False)
                    self.positions[identity] = stream.tell()
            except OSError:
                continue
        self.positions = {key: value for key, value in self.positions.items() if key in active}
        return list(results)


class NotSideChat(ControlError):
    pass


class SideChatProjection:
    def __init__(self, thread, owner, *, clock=time.time, previous=None):
        from .desktop_attention import AttentionProjection
        self.thread, self.owner, self.clock = thread, owner, clock
        self.attention = AttentionProjection(thread, owner)
        self.attention.runtime.clock = clock
        # A cached relationship is an identity constraint, never a live lease.
        self.relation = ({key: previous[key] for key in ('thread_id', 'parent_thread_id', 'ephemeral', 'title', 'source')}
                         if previous else None)
        self.observed_at = previous.get('observed_at') if previous else None
        self.verified_at = previous.get('verified_at') if previous else None
        self.invalidated = False

    @property
    def revision(self):
        return self.attention.revision

    def apply(self, change):
        if not isinstance(change, dict):
            raise ControlError('侧聊订阅更新无效')
        relation = None
        if change.get('type') == 'snapshot':
            state = change.get('conversationState')
            relation = relationship_metadata(state, self.thread)
            if relation is None or self.relation is not None and any(relation[key] != self.relation[key] for key in relation if key != 'title'):
                raise NotSideChat('当前 owner 未确认此临时侧聊的父子身份')
            title = own_input_title(state, self.thread)
            if title:
                relation['title'] = title
            elif self.relation:
                relation['title'] = self.relation['title']
        elif change.get('type') == 'patches':
            for patch in change.get('patches', []) if isinstance(change.get('patches'), list) else []:
                path = patch.get('path') if isinstance(patch, dict) else None
                if not path or path[0] in ('id', 'ephemeral', 'sideConversation', 'forkedFromId',
                                           'sideConversationParentNavigationPath', 'parentThreadId', 'source', 'threadSource', 'hostId'):
                    self.invalidated = True
                    raise ControlError('侧聊身份已变化，需要重新核对')
        attention = self.attention
        revision = change.get('revision')
        if (relation is not None and self.revision is not None and type(revision) is int
                and 0 <= revision < self.revision):
            # The native stream treats a complete snapshot as a new baseline.
            # Validate every projected field before replacing the old epoch;
            # following again can replay the same high cached initial snapshot.
            from .desktop_attention import AttentionProjection
            attention = AttentionProjection(self.thread, self.owner)
            attention.runtime.clock = self.clock
        attention.apply(change)
        self.attention = attention
        if relation is not None:
            self.relation = relation
            if self.observed_at is None:
                self.observed_at = self.clock()
        self.verified_at = self.clock()

    def mark_live(self):
        self.attention.settings.mark_live()
        self.attention.runtime.mark_live()
        if not self.invalidated and self.attention.runtime.snapshot().get('verified_at') is not None:
            self.verified_at = self.clock()

    def snapshot(self, connected=True):
        if self.relation is None:
            return None
        runtime = self.attention.runtime.snapshot()
        fresh = (connected and self.revision is not None and not self.invalidated and self.verified_at is not None
                 and self.verified_at - 5 <= self.clock() <= self.verified_at + 30)
        raw = runtime.get('state') if fresh else None
        status = {'running': 'running', 'waiting': 'waiting', 'complete': 'completed', 'aborted': 'interrupted'}.get(raw)
        if status is None:
            status = 'idle' if fresh and self.attention.runtime.data.get('resumeState') == 'resumed' else 'disconnected'
        return dict(self.relation, status=status, observed_at=self.observed_at, verified_at=self.verified_at,
                    expires_at=self.verified_at + 30 if self.verified_at is not None else None,
                    can_open=fresh, can_interact=fresh, turn_id=runtime.get('turn_id', ''))


class SideChatService:
    """A separate read connection plus a private, metadata-only relation cache."""
    def __init__(self, codex_home=None, *, home=None, channel_factory, scanner=None, log_root=None, clock=time.time):
        self.codex_home, self.channel_factory, self.clock = codex_home, channel_factory, clock
        self.home = vibecue_directory(home)
        self.cache_path = self.home / 'side-chats.json'
        # A fixture/custom Codex home must never inspect this user's real logs.
        if log_root is None and (codex_directory(codex_home).resolve() != (Path.home() / '.codex').resolve()
                or vibecue_directory(home).resolve() != (Path.home() / 'Library/Application Support/VibeCue').resolve()):
            log_root = vibecue_directory(home) / 'desktop-logs'
        self.scanner = scanner or SideChatCandidates(log_root, clock=clock)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.channel = None
        self.connected = False
        self.projections = {}
        self.known = OrderedDict()
        self.candidates = OrderedDict()
        self.next_attempt = {}
        self.subscribed_at = {}
        self.next_owner_check = {}
        self.rejected = set()
        self.next_scan = 0
        self.next_probe = 0
        self.message = '正在查找 Codex 侧边聊天'
        self.cache_dirty = False
        self.next_cache_write = 0
        self._load_cache()

    def _load_cache(self):
        try:
            stat = self.cache_path.lstat()
            if self.cache_path.is_symlink() or not self.cache_path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077 or stat.st_size > 65536:
                return
            data = json.loads(self.cache_path.read_bytes())
            rows = data.get('relations') if isinstance(data, dict) and data.get('version') == 1 else None
            if not isinstance(rows, list) or len(rows) > MAX_CANDIDATES:
                return
            for row in rows:
                if not isinstance(row, dict) or row.get('source') != SOURCE or row.get('ephemeral') is not True:
                    continue
                thread, parent = identifier(row.get('thread_id')), identifier(row.get('parent_thread_id'))
                title = row.get('title')
                if not thread or not parent or thread == parent or not isinstance(title, str) or not 0 < len(title) <= 80 or any(ord(c) < 32 for c in title):
                    continue
                times = {key: row.get(key) for key in ('observed_at', 'verified_at', 'expires_at')}
                if any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in times.values()):
                    continue
                self.known[thread] = {'thread_id': thread, 'parent_thread_id': parent, 'title': title,
                    'source': SOURCE, 'ephemeral': True, **times, 'status': 'disconnected',
                    'can_open': False, 'can_interact': False, 'turn_id': ''}
        except (OSError, ValueError, TypeError, RecursionError):
            return

    def _save_cache(self, force=False):
        if not self.cache_dirty or not force and self.clock() < self.next_cache_write:
            return
        self.next_cache_write = self.clock() + 5
        allowed = ('thread_id', 'parent_thread_id', 'title', 'status', 'source', 'ephemeral',
                   'observed_at', 'verified_at', 'expires_at')
        with self.lock:
            rows = list(self.known.values())[-MAX_CANDIDATES:]
            payload = {'version': 1, 'relations': [{key: row[key] for key in allowed if key in row} for row in rows]}
        temporary = None
        try:
            self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.side-chats-', dir=self.home)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.write('\n')
            os.replace(temporary, self.cache_path)
            self.cache_dirty = False
        except OSError:
            pass
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name='vibecue-side-chats', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        channel = self.channel
        if channel and channel.socket:
            try:
                channel.socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)

    def snapshot(self):
        with self.lock:
            rows = dict(self.known)
            for thread, projection in self.projections.items():
                value = projection.snapshot(self.connected)
                if value:
                    rows[thread] = value
            groups = {}
            for thread, row in rows.items():
                row = dict(row)
                if (not self.connected or thread not in self.projections or not row.get('can_interact')
                        or not row.get('expires_at') or self.clock() > row['expires_at']):
                    row.update(status='disconnected', can_open=False, can_interact=False)
                groups.setdefault(row['parent_thread_id'], []).append(row)
            for rows in groups.values():
                rows.sort(key=lambda row: (row.get('observed_at') or 0, row['thread_id']))
            return {'state': 'connected' if self.connected else 'disconnected' if self.known else 'discovering',
                    'message': self.message, 'by_parent': groups}

    def _follow(self, thread, owner, value):
        self.channel.require_method("thread-stream-following-changed")
        if value: self.channel.require_method("thread-stream-state-changed")
        self.channel._send({'type': 'broadcast', 'sourceClientId': self.channel.client_id,
                            'targetClientIds': [owner], 'method': 'thread-stream-following-changed', 'version': 1,
                            'params': {'conversationId': thread, 'hostId': 'local', 'following': value}})

    def _on_message(self, message):
        if not isinstance(message, dict):
            return
        if message.get('type') == 'vibecue-observation-gap':
            raise ControlError('侧聊快照过大，暂时无法读取')
        if message.get('type') != 'broadcast':
            return
        method, params = message.get('method'), message.get('params')
        if method == 'ipc-connection-reset':
            raise ControlError('Codex 侧聊连接已重建')
        if method == 'client-status-changed' and isinstance(params, dict) and params.get('status') == 'disconnected':
            if any(row.owner == params.get('clientId') for row in self.projections.values()):
                raise ControlError('Codex 侧聊 owner 已离线')
        if method != 'thread-stream-state-changed' or message.get('version') != 11 or not isinstance(params, dict) or params.get('hostId') != 'local':
            return
        projection = self.projections.get(params.get('conversationId'))
        targets = message.get('targetClientIds')
        if (projection is None or projection.invalidated or projection.thread in self.rejected
                or message.get('sourceClientId') != projection.owner or not isinstance(targets, list)
                or self.channel.client_id not in targets):
            return
        with self.lock:
            try:
                projection.apply(params.get('change'))
            except NotSideChat:
                projection.invalidated = True
                self.rejected.add(projection.thread)
                return
            except ControlError:
                # Patch gaps and invalid projected state require a fresh
                # subscription; complete snapshots reset their baseline above.
                projection.invalidated = True
                self.next_attempt[projection.thread] = self.clock() + RECONNECT_SECONDS
                return
            value = projection.snapshot()
            if value:
                self.known[projection.thread] = value
                self.next_attempt.pop(projection.thread, None)
                self.cache_dirty = True
                while len(self.known) > MAX_CANDIDATES:
                    self.known.popitem(last=False)

    def _discover(self):
        self.channel.require_capability("attention")
        now = self.clock()
        if now >= self.next_scan:
            self.next_scan = now + SCAN_SECONDS
            for thread in self.scanner.scan():
                self.candidates[thread] = now
                self.candidates.move_to_end(thread)
            while len(self.candidates) > MAX_CANDIDATES:
                self.candidates.popitem(last=False)
            active = set(self.candidates) | set(self.known) | set(self.projections)
            self.next_attempt = {key: value for key, value in self.next_attempt.items() if key in active}
        for thread, projection in list(self.projections.items()):
            expired = projection.revision is None and now - self.subscribed_at.get(thread, now) > 5
            invalid = projection.invalidated or projection.attention.runtime.invalidated or projection.attention.settings.invalidated
            if thread in self.rejected or expired or invalid:
                with self.lock:
                    projection.invalidated = True
                    value = projection.snapshot(False)
                    if value:
                        self.known[thread] = value
                        self.cache_dirty = True
                    self.projections.pop(thread, None)
                if thread in self.rejected:
                    self.candidates.pop(thread, None)
                    self.next_attempt[thread] = now + RETRY_SECONDS
                else:
                    self.next_attempt[thread] = now + (RECONNECT_SECONDS if thread in self.known else RETRY_SECONDS)
                self.rejected.discard(thread)
                self.subscribed_at.pop(thread, None)
                self.next_owner_check.pop(thread, None)
                self._follow(thread, projection.owner, False)
        if now < self.next_probe or any(p.revision is None for p in self.projections.values()):
            return
        # A live socket alone does not prove an ephemeral tab still exists.
        # Recheck one current owner's identity periodically, without resuming it.
        for thread, projection in list(self.projections.items()):
            if projection.relation is None or now < self.next_owner_check.get(thread, 0):
                continue
            self.next_probe = now + 1
            self.next_owner_check[thread] = now + 15
            self.channel.thread_id = thread
            try:
                self.channel.discover()
                if self.channel.owner != projection.owner:
                    raise DesktopOwnerUnavailable('侧聊 owner 已变化')
            except DesktopOwnerUnavailable:
                with self.lock:
                    projection.invalidated = True
                    self.known[thread] = projection.snapshot(False)
                    self.projections.pop(thread, None)
                    self.cache_dirty = True
                self.subscribed_at.pop(thread, None)
                self.next_owner_check.pop(thread, None)
                self.next_attempt[thread] = now + RECONNECT_SECONDS
                self._follow(thread, projection.owner, False)
            return
        if sum(p.relation is not None for p in self.projections.values()) >= MAX_SIDE_CHATS:
            return
        # Start with known relationships and newest log candidates, but serve
        # never/least-recently attempted chats before retrying missing owners.
        # Otherwise two closed known tabs can consume every 2-second retry.
        candidates = list(reversed(self.known)) + list(reversed(self.candidates))
        candidates = sorted(dict.fromkeys(candidates), key=lambda thread: self.next_attempt.get(thread, 0))
        for thread in candidates:
            if thread in self.projections or now < self.next_attempt.get(thread, 0):
                continue
            self.next_probe = now + 1
            self.next_attempt[thread] = now + (RECONNECT_SECONDS if thread in self.known else RETRY_SECONDS)
            self.channel.thread_id = thread
            try:
                self.channel.discover()
                projection = SideChatProjection(thread, self.channel.owner, clock=self.clock,
                                                previous=self.known.get(thread))
                with self.lock:
                    self.projections[thread] = projection
                self.subscribed_at[thread] = now
                self.next_owner_check[thread] = now + 15
                self._follow(thread, projection.owner, True)
            except DesktopOwnerUnavailable:
                pass  # Keep a verified relation unavailable; never infer archive/completion.
            return

    def _poll_timeout(self):
        """Bound idle wakeups while keeping socket delivery and due work live."""
        now, delay = self.clock(), 1.0
        with self.lock:
            delay = min(delay, max(.01, self.next_scan - now))
            if self.cache_dirty:
                delay = min(delay, max(.01, self.next_cache_write - now))
            awaiting = False
            for thread, projection in self.projections.items():
                if projection.revision is None:
                    awaiting = True
                    delay = min(delay, max(.01, self.subscribed_at.get(thread, now) + 5 - now))
            if not awaiting:
                for thread, projection in self.projections.items():
                    if projection.relation is not None:
                        due = max(self.next_probe, self.next_owner_check.get(thread, 0))
                        delay = min(delay, max(.1, due - now))
                if sum(p.relation is not None for p in self.projections.values()) < MAX_SIDE_CHATS:
                    for thread in set(self.known) | set(self.candidates):
                        if thread not in self.projections:
                            due = max(self.next_probe, self.next_attempt.get(thread, 0))
                            delay = min(delay, max(.1, due - now))
        return max(.01, min(1.0, delay))

    def _run(self):
        delay = 1
        while not self.stop_event.is_set():
            try:
                channel = self.channel_factory('vibecue-sidechat-health', self.codex_home, timeout=2, on_message=self._on_message)
                self.channel = channel
                with channel:
                    channel.require_capability("attention")
                    with self.lock:
                        self.connected = True
                        self.message = '已连接 Codex 侧边聊天'
                    while not self.stop_event.is_set():
                        self._discover()
                        readable, _, _ = select.select([channel.socket], [], [], self._poll_timeout())
                        drained = 0
                        while readable and drained < 32 and not self.stop_event.is_set():
                            channel._receive(time.monotonic() + 2)
                            drained += 1
                            readable, _, _ = select.select([channel.socket], [], [], 0)
                        with self.lock:
                            for projection in self.projections.values():
                                projection.mark_live()
                        self._save_cache()
                        delay = 1
            except (ControlError, OSError, ValueError, TypeError, RecursionError, OverflowError):
                with self.lock:
                    self.message = 'Codex 侧聊连接暂不可用，正在自动重试'
            finally:
                channel = self.channel
                if channel and channel.socket:
                    for thread, projection in list(self.projections.items()):
                        try:
                            self._follow(thread, projection.owner, False)
                        except (OSError, ControlError):
                            break
                    channel.close()
                with self.lock:
                    for thread, projection in self.projections.items():
                        value = projection.snapshot(False)
                        if value:
                            self.known[thread] = value
                            self.cache_dirty = True
                    self.connected = False
                    self.projections.clear()
                    self.subscribed_at.clear()
                    self.next_owner_check.clear()
                    # A new socket has new owner discovery and subscriptions;
                    # an old candidate cooldown must not delay known chats.
                    for thread in self.known:
                        if thread not in self.rejected:
                            self.next_attempt.pop(thread, None)
                    self.next_probe = 0
                    self.channel = None
                self._save_cache(force=True)
            if self.stop_event.wait(delay):
                break
            delay = min(30, delay * 2)
