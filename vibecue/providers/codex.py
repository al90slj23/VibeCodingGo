"""Read-only incremental observer for local Codex transcript events.

This adapter deliberately never attaches to or resumes a Codex thread, never
reads credentials, and never writes Codex state. Transcript formats are internal:
unknown records are ignored and status reports this source explicitly.
"""
from __future__ import annotations

import argparse
import fcntl
from contextlib import closing
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import time

from ..events import (vibecue_directory, codex_directory, emit_event, clean_text,
                     resolve_title, is_main_source, classify_final_message,
                     completion_event_id, input_event_id, state_databases)
from .approval_status import approval_status
from .task_details import TaskDetails, bounded_details, bound_status_previews
from .task_recovery import TaskRecovery, codex_process_snapshot

MAX_READ = 1_048_576
MAX_RECORD = 2_097_152
MAX_PRIME = 2_097_152
ACTIVITY_TTL = 24 * 60 * 60
MAX_PENDING_DELIVERIES = 512
DELIVERY_TTL = 120


def record_time(item):
    value = item.get('timestamp')
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            result = float(value) / 1000 if value > 10_000_000_000 else float(value)
        else:
            result = datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass
class ThreadMetadata:
    thread_id: str
    path: Path
    title: str = ''
    source: object = ''
    updated_at: float = 0


@dataclass
class TailReader:
    path: Path
    offset: int = 0
    inode: int = 0
    partial: bytes = b''
    dropping: bool = False
    last_observed_size: int = 0
    last_read_at: float = 0.0
    last_progress_at: float = 0.0
    read_error: str = ''
    read_error_at: float = 0.0

    def baseline(self):
        """Start at EOF, including skipping continuation of an old partial row."""
        stat = self.path.stat()
        self.inode, self.offset = stat.st_ino, stat.st_size
        self.last_observed_size = stat.st_size
        self.last_progress_at = time.time()
        self.partial = b''
        self.dropping = False
        if stat.st_size:
            with self.path.open('rb') as stream:
                stream.seek(-1, os.SEEK_END)
                self.dropping = stream.read(1) != b'\n'

    def read_records(self):
        """Read bounded bytes; preserve incomplete rows; survive replace/truncate."""
        self.last_read_at = time.time()
        try:
            stat = self.path.stat()
            self.last_observed_size = stat.st_size
            if self.inode != stat.st_ino or stat.st_size < self.offset:
                self.offset, self.inode, self.partial, self.dropping = 0, stat.st_ino, b'', False
            if stat.st_size == self.offset:
                self.read_error = ''
                return []
            with self.path.open('rb') as stream:
                stream.seek(self.offset)
                chunk = stream.read(MAX_READ)
            self.offset += len(chunk)
            if chunk:
                self.last_progress_at = time.time()
                self.read_error = ''
            else:
                self.read_error = 'UnexpectedEOF: file size exceeds readable bytes'
                self.read_error_at = time.time()
        except OSError as error:
            self.read_error = f'{type(error).__name__}: errno={error.errno} {error.strerror or "read failed"}'
            self.read_error_at = time.time()
            return []
        data = self.partial + chunk
        parts = data.split(b'\n')
        self.partial = parts.pop()
        result = []
        for line in parts:
            if self.dropping:
                self.dropping = False
                continue
            if not line or len(line) > MAX_RECORD:
                continue
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    result.append(value)
            except (ValueError, UnicodeDecodeError):
                continue
        if len(self.partial) > MAX_RECORD:
            self.partial = b''
            self.dropping = True
        return result


def strip_js_literals(code):
    """Keep call expressions, remove strings/comments so examples don't alert."""
    return re.sub(r"/\*[\s\S]*?\*/|//[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`",
                  ' ', str(code))


_JS_INPUT = re.compile(r'\bawait\s+tools\.(?:functions__)?request_user_input(?:_async)?\s*\(')
_JS_BLOCKING_INPUT = re.compile(r'\bawait\s+tools\.(?:functions__)?request_user_input\s*\(')
_INPUT_NAMES = frozenset(('request_user_input', 'request_user_input_async'))


class Interpreter:
    def __init__(self, metadata, emit, started_at=0):
        self.metadata = metadata
        self.emit = emit
        self.started_at = started_at
        self.turn_id = ''
        self.last_final = ''
        self.main = is_main_source(metadata.source)
        self.pending = deque()
        self.pending_ids = set()
        self.next_retry = 0.0
        self.dropped_deliveries = 0
        self.activity_state = 'idle'
        self.activity_turn_id = ''
        self.activity_started_at = 0.0
        self.last_activity = 0.0
        self.activity_watermark = 0.0
        self.activity_terminal_at = 0.0
        self.activity_terminal_kind = ''
        self.activity_work_at = 0.0
        self.blocking_calls = set()
        self.context_readable = True
        self.details = TaskDetails()

    def prime_context(self):
        """Recover bounded tail state without replaying historical alerts.

        No terminal/start marker in the window means unknown/idle unless a
        definite commentary or tool call establishes current work. Token/status
        refreshes alone cannot invent a task or restart a completed turn.
        """
        self.activity_state = 'idle'
        self.activity_turn_id = ''
        self.activity_started_at = self.last_activity = 0.0
        self.activity_watermark = self.activity_terminal_at = 0.0
        self.activity_terminal_kind = ''
        self.activity_work_at = 0.0
        self.blocking_calls.clear()
        self.context_readable = False
        self.details.reset()
        try:
            with self.metadata.path.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                start = max(0, size - MAX_PRIME)
                self.details.reset(window_truncated=start > 0)
                stream.seek(start)
                data = stream.read(MAX_PRIME)
                lines = data.split(b'\n')[:-1]
                if start and lines:
                    lines.pop(0)
                for line in lines:
                    try:
                        item = json.loads(line)
                        if not isinstance(item, dict):
                            continue
                        payload = item.get('payload', {})
                        if not isinstance(payload, dict):
                            continue
                        self._observe_activity(item)
                        if self.main and is_main_source(self.metadata.source):
                            self.details.observe(item, record_time(item))
                        if record_time(item) is not None:
                            turn_id = payload.get('turn_id') or payload.get('root_turn_id')
                            if turn_id:
                                self.turn_id = str(turn_id)
                    except (ValueError, TypeError):
                        continue
            self.context_readable = True
        except OSError:
            pass

    def _observe_activity(self, item):
        payload = item.get('payload')
        if not isinstance(payload, dict):
            return
        category, event_type = item.get('type'), payload.get('type')
        if category == 'session_meta':
            if not payload.get('id') or str(payload['id']) == self.metadata.thread_id:
                self.main = is_main_source(payload.get('source', self.metadata.source))
                if not self.main:
                    self.activity_state = 'idle'
                    self.blocking_calls.clear()
            return
        if not self.main or not is_main_source(self.metadata.source):
            return
        timestamp = record_time(item)
        if timestamp is None or timestamp < self.activity_watermark:
            return
        turn = str(payload.get('turn_id') or payload.get('root_turn_id') or '')
        starts = category == 'turn_context' or (category == 'event_msg' and event_type == 'task_started')
        ends = category == 'event_msg' and event_type in ('task_complete', 'task_completed', 'turn_aborted', 'task_aborted')
        final = category == 'response_item' and event_type == 'message' and payload.get('role') == 'assistant' and payload.get('phase') == 'final_answer'
        if ends or final:
            if turn and self.activity_turn_id and turn != self.activity_turn_id:
                return
            self.activity_state = 'idle'
            self.activity_watermark = self.activity_terminal_at = timestamp
            self.activity_terminal_kind = event_type if ends else 'final_answer'
            self.blocking_calls.clear()
            return
        if starts:
            if self.activity_state == 'idle' or (turn and turn != self.activity_turn_id):
                self.activity_started_at = timestamp
            self.activity_turn_id = turn or self.activity_turn_id or self.turn_id
            self.activity_state = 'running'
            self.last_activity = self.activity_watermark = timestamp
            self.activity_work_at = timestamp
            self.blocking_calls.clear()
            return
        if category != 'response_item':
            # These are activity only inside an already-established live turn.
            if self.activity_state == 'running' and category == 'event_msg' and event_type in ('token_count', 'agent_reasoning'):
                self.last_activity = self.activity_watermark = timestamp
            return
        name = str(payload.get('name', '')).split('.')[-1].removeprefix('functions__')
        call_id = str(payload.get('call_id') or payload.get('tool_call_id') or payload.get('id') or '')
        output = event_type in ('function_call_output', 'custom_tool_call_output', 'tool_result')
        if output:
            if self.activity_state == 'waiting' and call_id and call_id in self.blocking_calls:
                self.blocking_calls.remove(call_id)
                self.activity_watermark = timestamp
                if not self.blocking_calls:
                    self.activity_state = 'running'
                    self.last_activity = timestamp
                    self.activity_work_at = timestamp
            return
        calling = event_type in ('function_call', 'custom_tool_call')
        commentary = event_type == 'message' and payload.get('role') == 'assistant' and payload.get('phase') == 'commentary'
        if not calling and not commentary:
            return
        if timestamp <= self.activity_terminal_at:
            return
        blocking = ((event_type == 'function_call' and name == 'request_user_input') or
                    (event_type == 'custom_tool_call' and name == 'exec' and
                     _JS_BLOCKING_INPUT.search(strip_js_literals(payload.get('input', '')))))
        if self.activity_state == 'idle':
            self.activity_started_at = timestamp
        self.activity_turn_id = turn or self.activity_turn_id or self.turn_id
        self.activity_watermark = self.last_activity = timestamp
        self.activity_work_at = timestamp
        if blocking:
            self.activity_state = 'waiting'
            if call_id:
                self.blocking_calls.add(call_id)
        else:
            self.activity_state = 'running'
            self.blocking_calls.clear()

    def running_task(self, now=None):
        now = time.time() if now is None else now
        if (self.activity_state != 'running' or not self.main or not self.context_readable or
                not is_main_source(self.metadata.source) or not -60 <= now - self.last_activity <= ACTIVITY_TTL):
            return None
        task = {'thread_id': self.metadata.thread_id, 'title': self.metadata.title,
                'provider': 'codex', 'turn_id': self.activity_turn_id,
                'started_at': self.activity_started_at, 'last_activity': self.last_activity}
        detail = self.detail_snapshot(now)
        if detail:
            task.update({key: value for key, value in detail.items() if key not in ('started_at', 'turn_id', 'last_activity', 'messages', 'last_message')})
        return task

    def detail_snapshot(self, now=None):
        if not self.main or not is_main_source(self.metadata.source):
            return None
        return self.details.snapshot(self.metadata.thread_id, self.metadata.title, self.activity_state,
                                     time.time() if now is None else now)

    def _discard_pending(self, dropped=False):
        _, event = self.pending.popleft()
        self.pending_ids.discard(event['event_id'])
        if dropped:
            self.dropped_deliveries += 1

    def flush_pending(self):
        """Retry private-spool failures without rereading/losing transcript rows."""
        now = time.monotonic()
        while self.pending and now - self.pending[0][0] > DELIVERY_TTL:
            self._discard_pending(dropped=True)
        if now < self.next_retry:
            return
        # A large backlog must not monopolize the polling loop.
        for _ in range(32):
            if not self.pending:
                return
            _, event = self.pending[0]
            try:
                # False means deduplicated or intentionally disabled; consume it.
                self.emit(**event)
            except (OSError, sqlite3.Error):
                self.next_retry = time.monotonic() + 1.0
                return
            self._discard_pending()
        self.next_retry = 0.0

    def send(self, kind, message, event_id):
        if not self.main or event_id in self.pending_ids:
            return
        if len(self.pending) >= MAX_PENDING_DELIVERIES:
            self._discard_pending(dropped=True)
        event = dict(kind=kind, thread_id=self.metadata.thread_id,
                     turn_id=self.turn_id, title=self.metadata.title,
                     message=message, event_id=event_id, source='transcript')
        self.pending.append((time.monotonic(), event))
        self.pending_ids.add(event_id)
        self.flush_pending()

    def process(self, item):
        timestamp = record_time(item)
        if timestamp is not None and timestamp < self.started_at:
            return
        payload = item.get('payload') or {}
        if not isinstance(payload, dict):
            return
        category = item.get('type')
        event_type = payload.get('type')
        self._observe_activity(item)
        if self.main and is_main_source(self.metadata.source):
            self.details.observe(item, timestamp)
        if category == 'session_meta':
            return
        if not self.main:
            return
        if category == 'turn_context' or (category == 'event_msg' and event_type == 'task_started'):
            self.turn_id = str(payload.get('turn_id') or payload.get('root_turn_id') or '')
            self.last_final = ''
            return
        if category == 'event_msg' and event_type in ('task_complete', 'task_completed'):
            self.turn_id = str(payload.get('turn_id') or payload.get('root_turn_id') or self.turn_id)
            text = payload.get('last_agent_message') or payload.get('last_assistant_message') or self.last_final
            kind, message = classify_final_message(text)
            self.send(kind, message, completion_event_id(self.metadata.thread_id, self.turn_id))
            return
        if category == 'event_msg' and event_type in ('turn_aborted', 'task_aborted'):
            # A user interrupt is not task completion or a request for attention.
            return
        if category == 'response_item' and event_type == 'message':
            if payload.get('role') == 'assistant' and payload.get('phase') == 'final_answer':
                self.last_final = '\n'.join(str(c.get('text', '')) for c in (payload.get('content') or [])
                                            if isinstance(c, dict))
            return
        if category != 'response_item':
            return
        name = str(payload.get('name', '')).split('.')[-1]
        call_id = str(payload.get('call_id') or payload.get('id') or '')
        if event_type == 'function_call' and name in _INPUT_NAMES:
            self.send('input', '有问题需要你回答',
                      input_event_id(self.metadata.thread_id, self.turn_id, call_id))
        elif event_type == 'custom_tool_call' and name == 'exec':
            # Code-mode invocation is visible before nested execution. Only an
            # explicit awaited tool call is recognized, not arbitrary mentions.
            if _JS_INPUT.search(strip_js_literals(payload.get('input', ''))):
                self.send('input', '侦测到提问工具调用（依本机运行记录判断）',
                          input_event_id(self.metadata.thread_id, self.turn_id, call_id))


class Watcher:
    def __init__(self, codex_home=None, vibecue_home=None, emit=None, interval=1.0):
        self.codex_home = codex_directory(codex_home)
        self.vibecue_home = vibecue_directory(vibecue_home)
        self.vibecue_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.interval = max(.25, interval)
        self.started_at = time.time()
        self.watched = {}
        self.stopping = False
        self.running = False
        self._emit = emit or (lambda **kwargs: emit_event(**kwargs, vibecue_home=self.vibecue_home))
        self.last_discovery = 0.0
        self.last_status = 0.0
        self.recovery = TaskRecovery(self.vibecue_home, self.started_at)

    def metadata(self):
        for db in state_databases(self.codex_home):
            try:
                with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as connection:
                    columns = {row[1] for row in connection.execute('PRAGMA table_info(threads)')}
                    name = 'name' if 'name' in columns else "''"
                    query = f'SELECT id,rollout_path,title,{name},source,updated_at FROM threads WHERE archived=0 ORDER BY updated_at DESC LIMIT 256'
                    rows = connection.execute(query).fetchall()
                    return [ThreadMetadata(str(tid), Path(path), clean_text(n or title, 160), source, updated)
                            for tid, path, title, n, source, updated in rows if is_main_source(source)]
            except (OSError, sqlite3.Error):
                continue
        # Fallback for CLI installations without a threads metadata database.
        paths = []
        now = datetime.now()
        for delta in range(3):
            day = now - timedelta(days=delta)
            folder = self.codex_home / 'sessions' / day.strftime('%Y/%m/%d')
            try:
                paths.extend(folder.glob('rollout-*.jsonl'))
            except OSError:
                continue
        result = []
        for path in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:256]:
            try:
                with path.open('rb') as stream:
                    item = json.loads(stream.readline(131072))
                payload = item.get('payload', {})
                if item.get('type') == 'session_meta' and is_main_source(payload.get('source', '')):
                    tid = str(payload.get('id', ''))
                    result.append(ThreadMetadata(tid, path, resolve_title(tid, self.codex_home), payload.get('source', '')))
            except (OSError, ValueError, TypeError):
                continue
        return result

    def discover(self, initial=False):
        self.recovery.apply_remembered()
        discovered = self.metadata()
        # Recovery candidates remain observable after leaving Codex's recent
        # metadata window. Read only the already-known transcript path.
        present = {metadata.thread_id for metadata in discovered}
        for saved in self.recovery.retained_metadata():
            if saved['thread_id'] not in present:
                discovered.append(ThreadMetadata(saved['thread_id'], Path(saved['transcript_path']),
                                                 saved['title'], 'cli' if saved['source_kind'] == 'cli' else 'vscode'))
        discovered_ids = {metadata.thread_id for metadata in discovered}
        # Archived/moved rollouts can disappear after they leave the metadata
        # window. Drop only proven-missing paths; absent metadata alone is not
        # enough, and PermissionError must remain visible in health diagnostics.
        for tid, (tail, _) in list(self.watched.items()):
            if tid not in discovered_ids:
                try:
                    tail.path.stat()
                except (FileNotFoundError, NotADirectoryError):
                    del self.watched[tid]
                except OSError:
                    pass
        for metadata in discovered:
            tid = metadata.thread_id
            if tid in self.watched:
                tail, interpreter = self.watched[tid]
                interpreter.metadata.title = metadata.title
                if tail.path != metadata.path:
                    tail.path = metadata.path
                    tail.inode = 0
                    interpreter.metadata.path = metadata.path
                    interpreter.context_readable = False
                continue
            tail = TailReader(metadata.path)
            try:
                if initial:
                    tail.baseline()
                else:
                    stat = metadata.path.stat()
                    tail.inode = stat.st_ino
                    # Newly discovered old threads can have fresh appended work.
                    # Read only a bounded tail and timestamp-filter pre-launch rows.
                    if stat.st_size > MAX_READ:
                        tail.offset = stat.st_size - MAX_READ
                        tail.dropping = True
            except OSError:
                continue
            interpreter = Interpreter(metadata, self._emit, self.started_at)
            interpreter.prime_context()
            self.watched[tid] = (tail, interpreter)
        if len(self.watched) > 384:
            keep = {m.thread_id for m in discovered}
            for tid in list(self.watched):
                if tid not in keep:
                    del self.watched[tid]
                    if len(self.watched) <= 256:
                        break
        self.last_discovery = time.monotonic()

    def poll_once(self):
        if time.monotonic() - self.last_discovery >= 3:
            self.discover()
        for tail, interpreter in list(self.watched.values()):
            interpreter.flush_pending()
            old_inode, old_offset = tail.inode, tail.offset
            records = tail.read_records()
            if not tail.read_error and (not interpreter.context_readable or
                                        tail.inode != old_inode or tail.offset < old_offset):
                interpreter.prime_context()
            for item in records:
                try:
                    interpreter.process(item)
                except (TypeError, ValueError, KeyError, OverflowError):
                    # Internal transcript shapes can drift. One malformed row
                    # must not discard the rest of an already-read batch.
                    continue
        if time.monotonic() - self.last_status >= 5:
            self.write_status()

    def write_status(self):
        approvals = approval_status(self.vibecue_home)
        waiting_approval = set(approvals.get('pending_thread_ids', []))
        now = time.time()
        tasks = []
        detail_rows = []
        observations = []
        for tid, (tail, interpreter) in self.watched.items():
            task = interpreter.running_task(now)
            if task and not tail.read_error and tid not in waiting_approval:
                task['last_work_at'] = interpreter.activity_work_at
            else:
                task = None
            observations.append({'thread_id': tid, 'task': task,
                                 'state': interpreter.activity_state,
                                 'watermark': interpreter.activity_watermark,
                                 'terminal_at': interpreter.activity_terminal_at,
                                 'terminal_kind': interpreter.activity_terminal_kind,
                                 'source_readable': not bool(tail.read_error) and interpreter.context_readable,
                                 'source_kind': 'cli' if interpreter.metadata.source in ('cli', 'exec') else 'desktop',
                                 'transcript_path': str(interpreter.metadata.path)})
            detail = interpreter.detail_snapshot(now)
            if detail:
                detail['source_readable'] = not bool(tail.read_error) and interpreter.context_readable
                if tid in waiting_approval:
                    detail['status'] = 'waiting'
                detail_rows.append(detail)
        tasks, interrupted = self.recovery.reconcile(observations, codex_process_snapshot(now), now,
                                                     waiting=waiting_approval)
        details_by_id = {row['thread_id']: row for row in detail_rows}
        for row in interrupted:
            detail = details_by_id.get(row['thread_id'])
            if detail:
                detail.update(status=row['status'], finished_at=row['finished_at'],
                              elapsed_seconds=row.get('elapsed_seconds', 0))
        state = {
            'pid': os.getpid(), 'heartbeat': now, 'running': self.running,
            'started_at': self.started_at, 'watched_threads': len(self.watched),
            'running_tasks': tasks if self.running else [],
            'interrupted_tasks': interrupted,
            'task_recovery_error': self.recovery.error,
            'task_details': bounded_details(detail_rows),
            'source': 'local-transcripts', 'provider': 'codex',
            'supports': ['turn-completion', 'structured-question', 'final-text-input-heuristic', 'running-task-lifecycle', 'durable-interrupted-task-recovery', 'per-turn-usage', 'visible-conversation-preview'],
            'permission_status': 'connected' if approvals['connected'] else 'requires-codex-bridge-launch',
            'manual_approval': approvals,
            'pending_deliveries': sum(len(i.pending) for _, i in self.watched.values()),
            'dropped_deliveries': sum(i.dropped_deliveries for _, i in self.watched.values()),
            'read_error_count': sum(bool(tail.read_error) for tail, _ in self.watched.values()),
            'cursors': {tid: {'path': str(tail.path), 'offset': tail.offset, 'inode': tail.inode,
                               'last_observed_size': tail.last_observed_size,
                               'last_read_at': tail.last_read_at, 'last_progress_at': tail.last_progress_at,
                               'read_error': tail.read_error, 'read_error_at': tail.read_error_at}
                        for tid, (tail, _) in self.watched.items()},
        }
        temporary = self.vibecue_home / '.watcher-state.tmp'
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(bound_status_previews(state), stream)
            temporary.chmod(0o600)
            os.replace(temporary, self.vibecue_home / 'watcher-state.json')
        except OSError:
            pass
        self.last_status = time.monotonic()

    def snapshot_once(self):
        """Never replace a live daemon's heartbeat with a diagnostic snapshot."""
        descriptor = os.open(self.vibecue_home / 'watcher.lock', os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            self.discover(initial=True)
            self.write_status()
            return True

    def run(self):
        # launchd and a manual invocation may overlap during install/restart.
        # Only the lock owner can write heartbeat state or observe transcripts.
        lock_path = self.vibecue_home / 'watcher.lock'
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('VibeCue watcher: already running', file=sys.stderr, flush=True)
                return
            self.running = True
            try:
                self.discover(initial=True)
                self.write_status()
                while not self.stopping:
                    parent = os.environ.get('VIBECUE_PARENT_PID', '')
                    if parent.isdigit() and int(parent) > 1:
                        try:
                            os.kill(int(parent), 0)
                        except ProcessLookupError:
                            # A crashed UI must not leave its private helper
                            # reading transcripts without a visible owner.
                            break
                        except PermissionError:
                            pass
                    try:
                        self.poll_once()
                    except (OSError, ValueError, sqlite3.Error) as error:
                        print(f'VibeCue watcher: {type(error).__name__}', file=sys.stderr, flush=True)
                    time.sleep(self.interval)
            finally:
                self.running = False
                self.write_status()
                fcntl.flock(lock, fcntl.LOCK_UN)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home')
    parser.add_argument('--vibecue-home')
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--probe-read', metavar='THREAD_ID', help='Read a bounded transcript chunk and print diagnostics only; never emit events')
    parser.add_argument('--probe-offset', type=int, default=0)
    parser.add_argument('--once', action='store_true', help='Baseline and write a health snapshot without alerts')
    args = parser.parse_args(argv)
    watcher = Watcher(args.codex_home, args.vibecue_home, interval=args.interval)
    if args.probe_read:
        metadata = next((m for m in watcher.metadata() if m.thread_id == args.probe_read), None)
        if metadata is None:
            print(json.dumps({'thread_id': args.probe_read, 'error': 'thread metadata not found'}))
            return 1
        tail = TailReader(metadata.path, offset=max(0, args.probe_offset))
        try:
            tail.inode = metadata.path.stat().st_ino
        except OSError:
            pass
        records = tail.read_records()
        print(json.dumps({'thread_id': args.probe_read, 'path': str(tail.path), 'offset': tail.offset,
                          'inode': tail.inode, 'last_observed_size': tail.last_observed_size,
                          'records_read': len(records), 'partial_bytes': len(tail.partial),
                          'read_error': tail.read_error, 'last_read_at': tail.last_read_at}))
        return 1 if tail.read_error else 0
    if args.once:
        watcher.snapshot_once()
        return 0
    def stop(*_):
        watcher.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    watcher.run()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
