"""Observe a desktop-owned Codex stdio stream without making approval decisions.

The wire is copied before a bounded, separate observer sees each chunk. This
approval observer never sends decisions. An independently allowlisted local
control service may change explicitly selected thread settings on this owner
connection; it cannot send input, resume threads, or approve requests. Only the
client-facing request is positive attention evidence. No command, prompt,
response body, or raw protocol line is persisted.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid


SETTLE_SECONDS = 0.350
MAX_LINE_BYTES = 1_048_576
MAX_QUEUE_CHUNKS = 64
CHUNK_BYTES = 65_536
MAX_PENDING = 512
MAX_RECENT_RESPONSES = 1024
SOURCE = 'codex client approval stream'
APPROVAL_METHODS = frozenset((
    'item/commandExecution/requestApproval',
    'item/fileChange/requestApproval',
    'item/permissions/requestApproval',
    'execCommandApproval',
    'applyPatchApproval',
))
INPUT_METHODS = frozenset((
    'mcpServer/elicitation/request',
    'item/tool/requestUserInput',
    'tool/requestUserInput',
    'item/tool/requestOptionPicker',
))
ATTENTION_METHODS = APPROVAL_METHODS | INPUT_METHODS


def _id_token(value):
    # bool is an int in Python, but is not a JSON-RPC request ID.
    if isinstance(value, str) and 0 < len(value) <= 512:
        return ('string', value)
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        return ('integer', value)
    return None


def _identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 512 else ''


def _emit_permission(thread_id, turn_id, raw_key, home):
    from ..events import emit_event
    return emit_event(
        'permission', thread_id, turn_id,
        message='Codex 正在等待你手动批准', event_id=raw_key, source=SOURCE,
        requires_user_action=True, vibecue_home=home,
    )


def _emit_input(thread_id, turn_id, raw_key, home):
    from ..events import emit_event
    return emit_event(
        'input', thread_id, turn_id,
        message='Codex 正在等待你回答或确认',
        event_id=raw_key, source=SOURCE, requires_user_action=True,
        vibecue_home=home,
    )


def _event_identifier(raw_key):
    from ..events import event_identifier
    return event_identifier(raw_key, provider='codex')


def _emit_resolution(target_id, thread_id, home):
    from ..events import emit_resolution
    return emit_resolution(target_id, thread_id=thread_id,
                           vibecue_home=home, source=SOURCE)


@dataclass
class _Pending:
    token: tuple
    thread_id: str
    turn_id: str
    kind: str
    raw_key: str
    observed_at: float
    wire_at: float
    target_id: str = ''
    emitted: bool = False
    alert_kind: str = 'permission'


class ApprovalStreamMonitor:
    """Small protocol interpreter, also usable with deterministic test clocks.

    Request IDs are scoped to this one backend process. Request bodies are never
    retained. A response may arrive in the observer queue just before the request
    that triggered it, because two relay threads forward independently; timestamp
    tombstones cover that race without confusing opposite-direction RPC IDs.
    """

    def __init__(self, home=None, session_id=None, emit=None, resolve=None,
                 identifier=None, clock=time.monotonic, wall_clock=time.time,
                 can_emit=None, emit_input=None):
        self.home = home
        self.session_id = session_id or str(uuid.uuid4())
        self.emit = emit or _emit_permission
        self.emit_input = emit_input or _emit_input
        self.resolve = resolve or _emit_resolution
        self.identifier = identifier or _event_identifier
        self.clock = clock
        self.wall_clock = wall_clock
        self.can_emit = can_emit or (lambda: True)
        self.pending = OrderedDict()
        self.recent_responses = OrderedDict()
        self.connected = False
        self.last_protocol_at = None
        self.closed = False

    @property
    def pending_count(self):
        return len(self.pending)

    def _forget(self, token):
        pending = self.pending.pop(token, None)
        if pending is not None and pending.target_id:
            try:
                self.resolve(pending.target_id, pending.thread_id, self.home)
            except Exception:
                # Failure in this tool's disk spool cannot affect Codex's pipe.
                pass

    def gap(self):
        """Missing/invalid observation can hide a resolution: discard all waits."""
        for token in list(self.pending):
            self._forget(token)
        self.recent_responses.clear()

    def close(self):
        self.closed = True
        self.connected = False
        self.gap()

    def _remember_response(self, token, observed_at, thread_id=''):
        self.recent_responses[token] = (observed_at, thread_id)
        self.recent_responses.move_to_end(token)
        while len(self.recent_responses) > MAX_RECENT_RESPONSES:
            self.recent_responses.popitem(last=False)

    def _clear_turn(self, params):
        thread_id = _identifier(params.get('threadId') or params.get('conversationId'))
        turn = params.get('turn')
        turn_id = _identifier(params.get('turnId') or
                              (turn.get('id') if isinstance(turn, dict) else None))
        if not thread_id and not turn_id:
            return
        for token, pending in list(self.pending.items()):
            if ((not thread_id or pending.thread_id == thread_id) and
                    (not turn_id or not pending.turn_id or pending.turn_id == turn_id)):
                self._forget(token)

    def handle_message(self, direction, message, observed_at=None, wire_at=None):
        if self.closed or not isinstance(message, dict):
            return
        now = self.clock() if observed_at is None else observed_at
        order = now if wire_at is None else wire_at
        method = message.get('method')
        token = _id_token(message.get('id'))
        response = (method is None and token is not None and
                    ('result' in message or 'error' in message))
        if direction == 'client':
            # A client request using the same numeric ID is a different RPC and
            # must never resolve a server request.
            if response:
                self._remember_response(token, order)
                pending = self.pending.get(token)
                if pending is not None and order >= pending.wire_at:
                    self._forget(token)
            return
        if direction != 'server':
            return
        params = message.get('params')
        if response or (isinstance(method, str) and isinstance(params, dict)):
            self.connected = True
            self.last_protocol_at = self.wall_clock()
        if not isinstance(params, dict):
            return
        if method == 'serverRequest/resolved':
            resolved = _id_token(params.get('requestId'))
            if resolved is not None:
                thread_id = _identifier(params.get('threadId') or params.get('conversationId'))
                self._remember_response(resolved, order, thread_id)
                pending = self.pending.get(resolved)
                if (pending is not None and order >= pending.wire_at and
                        (not thread_id or thread_id == pending.thread_id)):
                    self._forget(resolved)
            return
        if method in ('turn/completed', 'turn/interrupted', 'turn/aborted'):
            self._clear_turn(params)
            return
        if method == 'thread/closed':
            self._clear_turn(params)
            return
        if not isinstance(method, str) or method not in ATTENTION_METHODS or token is None:
            return
        thread_id = _identifier(params.get('threadId') or params.get('conversationId'))
        if not thread_id:
            return
        previous_response = self.recent_responses.get(token)
        if previous_response is not None:
            responded_at, response_thread = previous_response
            if responded_at >= order and (not response_thread or response_thread == thread_id):
                return
        if token in self.pending:
            return
        while len(self.pending) >= MAX_PENDING:
            self._forget(next(iter(self.pending)))
        turn_id = _identifier(params.get('turnId'))
        raw_key = 'approval:' + json.dumps(
            [self.session_id, token[0], token[1], thread_id],
            ensure_ascii=True, separators=(',', ':'),
        )
        correlated_input = method in ('item/tool/requestUserInput', 'tool/requestUserInput') and _identifier(params.get('itemId'))
        if correlated_input:
            # The transcript tool call uses the same item/call ID. One question
            # must not alert twice just because its client request is also seen.
            from ..events import input_event_id
            raw_key = input_event_id(thread_id, turn_id, correlated_input)
        self.pending[token] = _Pending(
            token, thread_id, turn_id, method, raw_key, now, order,
            target_id=self.identifier(raw_key) if correlated_input else '',
            alert_kind='input' if method in INPUT_METHODS else 'permission',
        )

    def flush(self, now=None):
        if self.closed or not self.can_emit():
            return
        now = self.clock() if now is None else now
        for token, pending in list(self.pending.items()):
            if (pending.emitted or now - pending.observed_at < SETTLE_SECONDS or
                    not self.can_emit()):
                continue
            try:
                pending.target_id = self.identifier(pending.raw_key)
                emitter = self.emit_input if pending.alert_kind == 'input' else self.emit
                emitter(pending.thread_id, pending.turn_id, pending.raw_key, self.home)
                pending.emitted = True
            except Exception:
                # An atomic emitter may have succeeded before reporting an
                # error. Best-effort resolution also covers that unknown result.
                self._forget(token)


class _LineReader:
    def __init__(self, monitor, direction):
        self.monitor = monitor
        self.direction = direction
        self.partial = bytearray()
        self.dropping = False

    def feed(self, chunk, wire_at, forwarded_at=None):
        forwarded_at = wire_at if forwarded_at is None else forwarded_at
        parts = chunk.split(b'\n')
        for index, part in enumerate(parts):
            complete = index < len(parts) - 1
            if not self.dropping:
                if len(self.partial) + len(part) > MAX_LINE_BYTES:
                    self.partial.clear()
                    self.dropping = True
                    self.monitor.gap()
                else:
                    self.partial.extend(part)
            if not complete:
                continue
            if not self.dropping and self.partial:
                try:
                    message = json.loads(self.partial)
                    if isinstance(message, dict):
                        self.monitor.handle_message(self.direction, message, forwarded_at, wire_at)
                except (ValueError, UnicodeError, RecursionError):
                    self.monitor.gap()
            self.partial.clear()
            self.dropping = False


class _Observer:
    """One bounded monitor worker; forwarding never waits for it or for disk."""

    def __init__(self, backend_pid, home=None):
        self.home = home
        self.backend_pid = backend_pid
        self.session_id = str(uuid.uuid4())
        self.chunks = queue.Queue(maxsize=MAX_QUEUE_CHUNKS)
        self.invalidated = threading.Event()
        self.finished = threading.Event()
        self.monitor = ApprovalStreamMonitor(
            home=home, session_id=self.session_id,
            can_emit=lambda: not (self.invalidated.is_set() or self.finished.is_set()),
        )
        self.thread = threading.Thread(target=self._run, name='vibecue-approval-observer', daemon=True)
        self.thread.start()

    def offer(self, direction, chunk, wire_at, forwarded_at=None):
        if self.invalidated.is_set() or self.finished.is_set():
            return
        try:
            self.chunks.put_nowait((direction, chunk, wire_at, forwarded_at))
        except queue.Full:
            # Dropping an arbitrary response could leave a stale alert. Disable
            # this observer for the connection instead of trusting partial data.
            self.invalidated.set()
        except Exception:
            self.invalidated.set()

    def close(self):
        self.finished.set()

    def join(self):
        self.thread.join(timeout=2.0)

    def _health(self):
        from ..events import vibecue_directory
        home = vibecue_directory(self.home)
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory = home / 'bridges'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / f'{os.getpid()}-{self.session_id}.json'
        healthy = not (self.finished.is_set() or self.invalidated.is_set())
        payload = {
            'version': 1, 'pid': os.getpid(), 'backend_pid': self.backend_pid,
            'parent_pid': os.getppid(), 'connected': healthy and self.monitor.connected,
            'transport': 'stdio', 'last_seen': time.time(),
            'last_protocol_at': self.monitor.last_protocol_at,
            'pending_human_requests': self.monitor.pending_count if healthy else 0,
            # Only settled, actually emitted human waits affect the running
            # task list. Fast automatic approvals never enter this set.
            'pending_thread_ids': sorted({pending.thread_id for pending in self.monitor.pending.values()
                                          if pending.emitted}) if healthy else [],
        }
        fd, temporary = tempfile.mkstemp(prefix='.bridge-', dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.write('\n')
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _run(self):
        readers = {direction: _LineReader(self.monitor, direction)
                   for direction in ('client', 'server')}
        next_health = 0.0
        try:
            while not self.finished.is_set():
                if self.invalidated.is_set():
                    self.monitor.close()
                try:
                    item = self.chunks.get(timeout=0.025)
                except queue.Empty:
                    item = None
                if item is not None and not self.invalidated.is_set():
                    direction, chunk, wire_at, forwarded_at = item
                    try:
                        readers[direction].feed(chunk, wire_at, forwarded_at)
                    except Exception:
                        self.invalidated.set()
                        self.monitor.close()
                # Consume everything already observed before settling a wait.
                if self.chunks.empty() and not self.invalidated.is_set():
                    self.monitor.flush()
                if time.monotonic() >= next_health:
                    try:
                        self._health()
                    except Exception:
                        pass
                    next_health = time.monotonic() + 1.0
        finally:
            self.monitor.close()
            try:
                self._health()
            except Exception:
                pass


def _is_stdio_app_server(args):
    """Recognize a real server invocation, never an exec prompt or schema tool."""
    value_flags = frozenset((
        '-c', '--config', '-C', '--cd', '-p', '--profile', '-m', '--model',
        '-a', '--ask-for-approval', '-s', '--sandbox', '--enable', '--disable',
        '-i', '--image', '--add-dir', '--remote', '--remote-auth-token-env',
    ))
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in value_flags:
            index += 2
            continue
        if arg.startswith('-'):
            index += 1
            continue
        if arg != 'app-server':
            return False
        remaining = args[index + 1:]
        if any(value in ('generate-json-schema', 'generate-ts', 'daemon', '-h', '--help')
               for value in remaining):
            return False
        for position, value in enumerate(remaining):
            if value == '--listen':
                if position + 1 >= len(remaining) or remaining[position + 1] != 'stdio://':
                    return False
            elif value.startswith('--listen=') and value != '--listen=stdio://':
                return False
        return True
    return False


def _write_all(fd, chunk):
    view = memoryview(chunk)
    while view:
        try:
            written = os.write(fd, view)
        except InterruptedError:
            continue
        if written <= 0:
            raise BrokenPipeError('stdio closed')
        view = view[written:]


def _status(code):
    return 128 - code if code < 0 else code


def run_bridge(real_cli, args, home=None):
    """Run the official CLI, observing only a fresh desktop stdio transport.

    The launcher must opt into this entrypoint via the desktop's CLI override.
    This function never attaches to or restarts an existing Codex process.
    """
    real_cli = os.fspath(real_cli)
    args = [os.fspath(arg) for arg in args]
    environment = dict(os.environ)
    environment['CODEX_CLI_PATH'] = real_cli
    if not _is_stdio_app_server(args):
        try:
            return _status(subprocess.call([real_cli] + args, env=environment))
        except OSError:
            return 127
    try:
        process = subprocess.Popen([real_cli] + args, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=None, env=environment,
                                   bufsize=0)
    except OSError:
        return 127
    observer = None
    try:
        observer = _Observer(process.pid, home=home)
    except Exception:
        # Observability is optional. The official process still owns execution.
        pass
    control = None
    try:
        from ..codex_control_relay import ControlHub, atomic_pipe_write, framed_pipe_write
        control = ControlHub(lambda chunk: _write_all(process.stdin.fileno(), chunk), process.pid, home=home,
                             write_control=lambda chunk, deadline: atomic_pipe_write(process.stdin.fileno(), chunk, deadline),
                             write_chat=lambda chunk, deadline, commit: framed_pipe_write(process.stdin.fileno(), chunk, deadline, commit, control.closed))
        control.start()
    except Exception:
        if control is not None:
            control.close()
        control = None
    wire_closed = threading.Event()
    wire_failed = threading.Event()
    stdout_done = threading.Event()

    def relay(source, destination, direction):
        try:
            while not wire_closed.is_set():
                try:
                    chunk = os.read(source, CHUNK_BYTES)
                except InterruptedError:
                    continue
                if not chunk or wire_closed.is_set():
                    break
                wire_at = time.monotonic()
                if direction == 'client' and control is not None:
                    control.forward_client(chunk)
                    forwarded = chunk
                else:
                    forwarded = control.server_chunk(chunk) if control is not None else chunk
                    if forwarded:
                        _write_all(destination, forwarded)
                forwarded_at = time.monotonic()
                if observer is not None and forwarded:
                    try:
                        observer.offer(direction, forwarded, wire_at, forwarded_at)
                    except Exception:
                        # Monitoring is never allowed to stop a relay.
                        observer.close()
        except (OSError, ValueError):
            if not wire_closed.is_set():
                wire_failed.set()
        finally:
            if observer is not None:
                observer.close()
            if control is not None:
                control.close()
            if direction == 'client':
                try:
                    if control is not None:
                        with control.wire_lock:
                            process.stdin.close()
                    else:
                        process.stdin.close()
                except (OSError, ValueError):
                    pass
            else:
                stdout_done.set()

    old_handlers = {}
    termination = []

    def forward_signal(number, _frame):
        if not termination:
            termination.append(time.monotonic())
        if observer is not None:
            observer.close()
        try:
            process.send_signal(number)
        except OSError:
            pass

    try:
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                old_handlers[number] = signal.signal(number, forward_signal)
        inbound = threading.Thread(target=relay,
                                   args=(sys.stdin.fileno(), process.stdin.fileno(), 'client'),
                                   name='vibecue-codex-stdin', daemon=True)
        outbound = threading.Thread(target=relay,
                                    args=(process.stdout.fileno(), sys.stdout.fileno(), 'server'),
                                    name='vibecue-codex-stdout', daemon=True)
        inbound.start()
        outbound.start()
        while process.poll() is None or not stdout_done.is_set():
            if wire_failed.is_set() and not termination:
                termination.append(time.monotonic())
                try:
                    process.terminate()
                except OSError:
                    pass
            if termination and time.monotonic() - termination[0] > 2.0 and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            time.sleep(0.025)
        return _status(process.wait())
    finally:
        wire_closed.set()
        if control is not None:
            control.close()
        if observer is not None:
            observer.close()
            observer.join()
        for number, handler in old_handlers.items():
            signal.signal(number, handler)
        for stream in (process.stdin, process.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _find_real_cli():
    # Never use CODEX_CLI_PATH here: in an opted-in desktop process it points
    # back to this wrapper. Keep the original signed bundle as the backend.
    for directory in (Path('/Applications'), Path.home() / 'Applications'):
        for bundle in ('ChatGPT.app', 'Codex.app'):
            resources = directory / bundle / 'Contents' / 'Resources'
            for relative in ('codex-cli/CodexCLI.app/Contents/MacOS/codex',
                             'codex-cli/bin/codex', 'codex'):
                candidate = resources / relative
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    return None


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    real_cli = _find_real_cli()
    if real_cli is None:
        sys.stderr.write('未找到 Codex 的官方命令行程序，请先安装 Codex。\n')
        return 127
    if not _is_stdio_app_server(args):
        environment = dict(os.environ)
        environment['CODEX_CLI_PATH'] = real_cli
        try:
            os.execve(real_cli, [real_cli] + args, environment)
        except OSError:
            sys.stderr.write('VibeCoding Go 无法启动 Codex。\n')
            return 127
    return run_bridge(real_cli, args)


if __name__ == '__main__':
    raise SystemExit(main())
