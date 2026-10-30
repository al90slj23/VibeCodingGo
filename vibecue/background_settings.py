"""Finite native settings worker for local chats with no desktop owner/writer.

The installed CLI loads metadata without turns, applies only next-turn model
settings, exits, then a fresh worker checks persistence. No input, interruption,
authentication, ownership takeover, window navigation or daemon management RPC
is exposed. Codex's own exclusive writer lock is the final race arbiter.
"""
from contextlib import closing, contextmanager
import fcntl
import json
import os
from pathlib import Path
import plistlib
import select
import signal
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from .codex_control import ControlError, UnconfirmedUpdate, fields_match, settings_subset
from .events import codex_directory, vibecue_directory

MAX_FRAME = 4 * 1024 * 1024
MAX_WIRE = 16 * 1024 * 1024
_CONTRACTS = set()


class BackgroundUnavailable(ControlError):
    """Nothing was submitted; a later finite attempt is safe."""


class BackgroundUpdateUnconfirmed(UnconfirmedUpdate):
    def __init__(self, patch):
        super().__init__('后台设置可能已保存；只重新核验，不自动重发。')
        self.patch = dict(patch)


def native_binary():
    for bundle in (Path('/Applications/ChatGPT.app'), Path('/Applications/Codex.app')):
        try:
            info = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
            binary = bundle / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
            if (info.get('CFBundleIdentifier') == 'com.openai.codex' and binary.is_file()
                    and binary.resolve().is_relative_to(bundle.resolve())):
                return binary
        except (OSError, ValueError):
            continue
    raise BackgroundUnavailable('尚未找到已安装的 Codex 配置工具，后台会自动重试。')


def _remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise BackgroundUnavailable('后台配置等待已到时，稍后自动重试，不切换窗口。')
    return left


def require_contract(binary, deadline):
    """Check this executable's actual protocol, never a hardcoded build number."""
    meta = binary.stat()
    identity = (str(binary), meta.st_ino, meta.st_size, meta.st_mtime_ns)
    if identity in _CONTRACTS:
        return
    with tempfile.TemporaryDirectory(prefix='vcgo-settings-contract-') as folder:
        try:
            subprocess.run([str(binary), 'app-server', 'generate-json-schema', '--experimental', '--out', folder],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=min(5, _remaining(deadline)), check=True)
            root = Path(folder) / 'v2'
            required = {
                'ThreadReadParams': {'threadId', 'includeTurns'},
                'ThreadResumeParams': {'threadId', 'excludeTurns'},
                'ThreadSettingsUpdateParams': {'threadId', 'model', 'effort', 'serviceTier'},
            }
            for name, keys in required.items():
                path = root / (name + '.json')
                if path.stat().st_size > MAX_FRAME:
                    raise ValueError()
                document = json.loads(path.read_text())
                props = document.get('properties', {}) if isinstance(document, dict) else None
                if not isinstance(props, dict) or not keys <= props.keys() or 'threadId' not in document.get('required', []):
                    raise ValueError()
                for key in keys & {'includeTurns', 'excludeTurns'}:
                    if props[key].get('type') != 'boolean':
                        raise ValueError()
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            raise BackgroundUnavailable('Codex 后台配置协议暂不可用，会自动重新检查兼容性。') from None
    if len(_CONTRACTS) >= 8:
        _CONTRACTS.clear()
    _CONTRACTS.add(identity)


def check_writer(identifier, codex_home):
    """Do not load a chat owned by any desktop, TUI or background executor."""
    path = codex_home / 'thread-writer-locks' / (identifier + '.lock')
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return
    except OSError:
        raise BackgroundUnavailable('无法确认此对话的运行者，暂缓后台配置。') from None
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid():
            raise BackgroundUnavailable('无法确认此对话的运行者，暂缓后台配置。')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackgroundUnavailable('此对话已有运行者；等待原生连接恢复后设置，不接管或中断任务。') from None
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def check_local_record(identifier, codex_home):
    """A settings edit must not restore an archived or nonexistent chat."""
    database = codex_home / 'state_5.sqlite'
    try:
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=.1)) as connection:
            row = connection.execute('SELECT archived FROM threads WHERE id=?', (identifier,)).fetchone()
    except (OSError, ValueError, sqlite3.Error):
        raise BackgroundUnavailable('本机历史索引暂不可读，稍后自动重试。') from None
    if row is None or row[0] != 0:
        raise ControlError('对话不存在或已归档；模型设置不会恢复或取消归档。')


@contextmanager
def worker_lease(home):
    root = vibecue_directory(home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / 'codex-background-settings.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackgroundUnavailable('后台配置工具正在处理另一项，稍后自动继续。') from None
        yield


class MetadataWorker:
    """A bounded, low-priority child. The only mutation is settings/update."""
    def __init__(self, binary, codex_home, deadline):
        self.binary, self.codex_home, self.deadline = binary, codex_home, deadline
        self.process = None
        self.buffer = b''
        self.received = self.serial = 0

    def __enter__(self):
        env = {key: value for key, value in os.environ.items()
               if key in ('HOME', 'PATH', 'TMPDIR', 'LANG', 'LC_ALL', 'USER', 'LOGNAME',
                          'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                          'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
        env['CODEX_HOME'] = str(self.codex_home)
        try:
            self.process = subprocess.Popen([str(self.binary), 'app-server'], env=env,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.DEVNULL, bufsize=0, start_new_session=True)
            if hasattr(os, 'setpriority'):
                try:
                    os.setpriority(os.PRIO_PROCESS, self.process.pid, 10)
                except OSError:
                    pass
            self.rpc('initialize', {'clientInfo': {'name': 'vibecoding_go_settings', 'version': '1'},
                                    'capabilities': {'experimentalApi': True}})
            self.process.stdin.write(b'{"method":"initialized"}\n')
            self.process.stdin.flush()
            return self
        except Exception:
            self.close()
            raise

    def __exit__(self, *args):
        self.close()

    def close(self):
        if self.process is None:
            return
        process, self.process = self.process, None
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            # Only this worker's new session, including its own helper children.
            # Never kill by executable name or touch the desktop's process tree.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
            raise ControlError('后台配置工具未正常退出，请重新核验，设置不会自动重发。') from None
        finally:
            process.stdout.close()

    def rpc(self, method, params):
        shapes = {'initialize': {'clientInfo', 'capabilities'},
                  'thread/read': {'threadId', 'includeTurns'},
                  'thread/resume': {'threadId', 'excludeTurns'},
                  'thread/settings/update': {'threadId', 'model', 'effort', 'serviceTier'}}
        if method not in shapes or set(params) - shapes[method]:
            raise ControlError('后台配置不允许此操作')
        if ((method == 'thread/read' and params.get('includeTurns') is not False)
                or (method == 'thread/resume' and params.get('excludeTurns') is not True)):
            raise ControlError('后台配置只允许读取无正文的元数据')
        mutation = method == 'thread/settings/update'
        attempted = False
        try:
            self.serial += 1
            request = json.dumps({'id': self.serial, 'method': method, 'params': params}).encode() + b'\n'
            _remaining(self.deadline)
            attempted = True
            self.process.stdin.write(request); self.process.stdin.flush()
            while True:
                while b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1)
                    if len(line) > MAX_FRAME:
                        raise ControlError('Codex 配置响应超过读取范围')
                    message = json.loads(line)
                    if not isinstance(message, dict) or message.get('id') != self.serial:
                        continue
                    if 'error' in message:
                        error = message['error']
                        text = error.get('message', '') if isinstance(error, dict) else ''
                        if method == 'thread/resume' and ('active writer' in text or 'live local writer' in text or 'is closing' in text):
                            raise BackgroundUnavailable('此对话已被其他运行者载入，稍后自动通过原生连接设置。')
                        raise ControlError('Codex 拒绝了后台配置请求：' + method)
                    result = message.get('result')
                    if not isinstance(result, dict):
                        raise ControlError('Codex 后台配置响应无效')
                    return result
                if len(self.buffer) > MAX_FRAME:
                    raise ControlError('Codex 配置响应超过读取范围')
                if not select.select([self.process.stdout], [], [], _remaining(self.deadline))[0]:
                    raise BackgroundUnavailable('后台配置等待已到时，稍后自动重试。')
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise BackgroundUnavailable('Codex 后台配置连接中断，稍后自动重试。')
                self.received += len(chunk)
                if self.received > MAX_WIRE:
                    raise ControlError('Codex 后台配置响应超过批次读取范围')
                self.buffer += chunk
        except (ControlError, OSError, ValueError, TypeError):
            if attempted and mutation:
                raise UnconfirmedUpdate('后台配置已提交但未获完整确认；只重新核验，不自动重发。') from None
            raise

    def load(self, identifier):
        check_local_record(identifier, self.codex_home)
        check_writer(identifier, self.codex_home)
        data = self.rpc('thread/read', {'threadId': identifier, 'includeTurns': False})
        thread = data.get('thread', {})
        status = thread.get('status') if isinstance(thread, dict) else None
        if (not isinstance(thread, dict) or thread.get('id') != identifier or thread.get('parentThreadId')
                or thread.get('ephemeral') is True or not isinstance(status, dict) or status.get('type') != 'notLoaded'):
            raise ControlError('此对话不适合后台配置，未载入或修改。')
        self.indexed_settings = settings_subset(thread)
        # The native exclusive writer check inside resume closes the race after
        # this preflight. Never stop/release another writer or retry its resume.
        check_writer(identifier, self.codex_home)
        result = self.rpc('thread/resume', {'threadId': identifier, 'excludeTurns': True})
        state = result.get('thread', {})
        status = state.get('status') if isinstance(state, dict) else None
        if (not isinstance(state, dict) or state.get('id') != identifier or state.get('turns') != []
                or not isinstance(status, dict) or status.get('type') != 'idle'):
            raise ControlError('后台载入状态未确认为空闲，未提交模型设置。')
        return result


def run(identifier, params=None, *, home=None, codex_home=None, deadline=None, phase=None):
    """Apply an explicit patch or inspect via fresh, writer-free native loads."""
    try:
        if str(uuid.UUID(identifier)) != identifier.lower():
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise BackgroundUnavailable('后台配置需要有效的本机 Codex 对话标识。') from None
    if params is not None:
        from .desktop_control import _validate_apply
        if not isinstance(params, dict):
            raise ControlError('后台模型设置参数无效')
        _validate_apply(params)
    root = codex_directory(codex_home)
    deadline = time.monotonic() + 20 if deadline is None else deadline
    submitted = False
    patch = {}
    with worker_lease(home):
        _remaining(deadline)
        check_local_record(identifier, root)
        check_writer(identifier, root)
        binary = native_binary()
        require_contract(binary, deadline)
        try:
            with MetadataWorker(binary, root, deadline) as worker:
                if phase:
                    phase('loading')
                before = worker.load(identifier)
                current = settings_subset(before)
                if params is not None:
                    from .desktop_control import resolve_patch
                    from .codex_control import cached_catalog
                    patch = resolve_patch(params, current, cached_catalog(root))
                    if phase:
                        phase('writing')
                    submitted = True
                    worker.rpc('thread/settings/update', {'threadId': identifier, **patch})
            # Closing the first process flushes native persistence and releases
            # its writer. Verification never reuses that process's saved echo.
            if phase:
                phase('verifying')
            with MetadataWorker(binary, root, deadline) as reader:
                persisted = reader.load(identifier)
                current = settings_subset(persisted)
                metadata = reader.rpc('thread/read', {'threadId': identifier, 'includeTurns': False}).get('thread', {})
                status = metadata.get('status') if isinstance(metadata, dict) else None
                if not isinstance(metadata, dict) or metadata.get('id') != identifier or not isinstance(status, dict) or status.get('type') != 'idle':
                    raise ControlError('持久化回读的对话或状态不匹配')
                indexed = reader.indexed_settings
                expected = patch if params is not None else {key: value for key, value in current.items() if value is not None}
                indexed_patch = {k: v for k, v in expected.items() if k not in ('serviceTier', 'service_tier')}
                mode = persisted.get('collaborationMode')
                composer = settings_subset(mode.get('settings', {})) if isinstance(mode, dict) else None
                if isinstance(mode, dict) and isinstance(mode.get('settings'), dict):
                    effort = mode['settings'].get('reasoning_effort')
                    if effort is None or isinstance(effort, str):
                        composer['effort'] = effort
                protected = ('cwd', 'modelProvider', 'approvalPolicy', 'approvalsReviewer', 'sandbox', 'activePermissionProfile', 'disabledPluginIds')
                verified = (fields_match(current, expected) and fields_match(indexed, indexed_patch)
                            and fields_match(settings_subset(metadata), indexed_patch)
                            and (composer is None or fields_match(composer, indexed_patch))
                            and all(before.get(key) == persisted.get(key) for key in protected))
            return {'thread_id': identifier, 'status': ('verified' if verified else 'unverified') if params is not None else 'read',
                    'verified': verified, 'loaded': False, 'verification_basis': 'persisted_settings',
                    'settings': current, 'saved_settings': current, 'indexed_settings': indexed,
                    'expected_settings': settings_subset(patch),
                    'message': '已在后台保存，并在配置进程退出后重新载入核验；下轮生效。' if verified else '持久化回读未完整匹配，只重新核验，不自动重发。'}
        except (ControlError, OSError, ValueError, TypeError) as error:
            if submitted:
                raise BackgroundUpdateUnconfirmed(patch) from None
            raise error
