"""Finite local tool execution shared by VCGo engineering modules.

Nothing starts on import. All callers supply an argv (never shell text), and
own a one-shot Runtime whose children and descendants have a bounded lifetime.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import tempfile
import threading
import time

MAX_INPUT = 256 * 1024
MAX_OUTPUT = 2 * 1024 * 1024
TOOLS = {'node': '--version', 'npm': '--version', 'pnpm': '--version',
         'python3': '--version', 'go': 'version', 'git': '--version',
         'mise': '--version', 'hurl': '--version', 'lnav': '-V', 'curl': '--version'}
MANAGED_TOOLS = frozenset(('mise', 'hurl', 'lnav'))


class EngineeringError(ValueError):
    """A controlled diagnostic suitable for the UI."""


def redact(value, maximum=4000):
    value = str(value or '')
    # Do not preserve terminal controls or multiline secret blocks in reports.
    value = re.sub(r'-----BEGIN [^-]*(?:PRIVATE KEY|SECRET)[^-]*-----.*?-----END [^-]+-----', lambda match: '\n'.join(['[已脱敏密钥]']*(match.group(0).count('\n')+1)), value, flags=re.S)
    value = re.sub(r'(?i)((?:set-cookie|cookie)\s*[\"\']?\s*[:=]\s*)[^\r\n]+', r'\1[已脱敏]', value)
    value = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\r\n,;]+', r'\1[已脱敏]', value)
    value = re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|client[_-]?secret|secret|token)\s*["\']?\s*[:=]\s*["\']?)[^\s"\',;&}]+', r'\1[已脱敏]', value)
    value = re.sub(r'(?i)([?&](?:key|api_key|token|secret|password)=)[^&#\s]+', r'\1[已脱敏]', value)
    value = re.sub(r'(https?://)[^\s/@:]+:[^\s/@]+@', r'\1[已脱敏]@', value)
    value = re.sub(r'\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|github_pat_[A-Za-z0-9_]{12,})\b', '[已脱敏]', value)
    value = ''.join(ch for ch in value if ch in '\n\t' or (ord(ch) >= 32 and ord(ch) != 127))
    return value[:max(0, maximum)]


@dataclass
class ProcessResult:
    returncode: int
    stdout: bytes = b''
    stderr: bytes = b''
    timed_out: bool = False
    cancelled: bool = False
    truncated: bool = False
    duration_ms: float = 0


class Runtime:
    def __init__(self, *, search_path=None, timeout=40, clock=time.monotonic):
        self.search_path = search_path
        self.cancelled = False
        self.clock = clock
        self.deadline = clock() + max(1, min(55, timeout))
        self._capabilities = {}
        self._paths = {}

    def find_tool(self, name):
        if name not in TOOLS:
            return None
        if name in self._paths:
            return self._paths[name]
        home = Path.home()
        directories = (self.search_path if self.search_path is not None else
                       os.environ.get('VIBECUE_ENGINEERING_PATH', os.environ.get('PATH', ''))).split(os.pathsep)
        if self.search_path is None and name in MANAGED_TOOLS:
            configured_home = os.environ.get('VIBECUE_HOME', '')
            managed_home = (Path(configured_home) if os.path.isabs(configured_home) else
                            home / 'Library/Application Support/VibeCue')
            # Fixed, known paths only. Installation and checksum validation are
            # separate explicit operations; discovery never starts a process.
            directories.insert(0, str(managed_home / 'engineering-tools/bin'))
        directories += ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/bin',
                        str(home / '.local/bin'), str(home / '.cargo/bin')]
        # Do not enumerate installations, execute login shells, or trust a
        # current-directory PATH entry. Resolving a known executable is enough.
        seen = set()
        for directory in directories[:64]:
            if not directory or not os.path.isabs(directory) or directory in seen:
                continue
            seen.add(directory)
            if '/mise/shims' in directory or '/.asdf/shims' in directory:
                continue
            path = Path(directory) / name
            try:
                resolved = path.resolve(strict=True)
                meta = resolved.stat()
                if stat.S_ISREG(meta.st_mode) and os.access(resolved, os.X_OK):
                    self._paths[name] = str(resolved)
                    return str(resolved)
            except (OSError, RuntimeError):
                continue
        self._paths[name] = None
        return None

    def capability(self, name):
        if name in self._capabilities:
            return dict(self._capabilities[name])
        path = self.find_tool(name)
        item = {'available': bool(path), 'path': path, 'version': None}
        if path and name in TOOLS and not self.cancelled:
            with tempfile.TemporaryDirectory(prefix='vcgo-tool-version-') as temp:
                result = self.run([path, TOOLS[name]], timeout=2, max_output=16384, cwd=temp,
                                  env={'MISE_NO_CONFIG': '1', 'MISE_OFFLINE': '1', 'MISE_AUTO_INSTALL': 'false', 'NO_COLOR': '1'})
            text = (result.stdout or result.stderr).decode('utf-8', 'replace').splitlines()
            if result.returncode == 0 and not result.truncated and text:
                item['version'] = redact(text[0], 180)
            else:
                item['version_error'] = '版本探测超时' if result.timed_out else '版本信息暂不可读'
        self._capabilities[name] = item
        return dict(item)

    @staticmethod
    def child_environment(extra=None):
        allowed = ('HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ')
        result = {key: value for key in allowed if (value := os.environ.get(key)) is not None}
        result.update(PATH='/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                      NO_COLOR='1', TERM='dumb', COREPACK_ENABLE_NETWORK='0')
        if extra:
            for key, value in extra.items():
                if isinstance(key, str) and isinstance(value, str) and '\x00' not in key + value and '=' not in key:
                    result[key] = value
        return result

    @contextmanager
    def signals(self):
        previous = {}
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGINT):
                previous[number] = signal.getsignal(number)
                signal.signal(number, lambda *_: setattr(self, 'cancelled', True))
        try:
            yield self
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)

    @staticmethod
    def _kill_group(process):
        # The pgid belongs to this exact child because start_new_session=True.
        # Kill its descendants even if the leader already exited with open pipes.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        try:
            process.wait(timeout=.15)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def run(self, argv, *, input=None, timeout=15, max_output=1_048_576, cwd=None, env=None):
        if (not isinstance(argv, (list, tuple)) or not argv or len(argv) > 160 or
                any(not isinstance(arg, str) or '\x00' in arg or len(arg) > MAX_INPUT for arg in argv) or
                not os.path.isabs(argv[0])):
            raise EngineeringError('工具命令无效。')
        if input is not None and (not isinstance(input, bytes) or len(input) > MAX_INPUT):
            raise EngineeringError('工具输入超过 256 KiB 上限。')
        maximum = min(MAX_OUTPUT, max(1024, int(max_output)))
        began = self.clock()
        remaining = min(max(.01, float(timeout)), 30, self.deadline - began)
        if self.cancelled or remaining <= 0:
            return ProcessResult(-1, cancelled=self.cancelled, timed_out=remaining <= 0)
        try:
            process = subprocess.Popen(list(argv), stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                       start_new_session=True, cwd=cwd or '/', env=self.child_environment(env))
        except OSError:
            return ProcessResult(-1, stderr=b'Tool could not be started', duration_ms=(self.clock()-began)*1000)
        selector = selectors.DefaultSelector()
        outputs = {'stdout': bytearray(), 'stderr': bytearray()}
        timed_out = truncated = False
        incoming = memoryview(input or b'')
        try:
            for name in outputs:
                stream = getattr(process, name); os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, name)
            if process.stdin:
                os.set_blocking(process.stdin.fileno(), False)
                if incoming:
                    selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
                else:
                    process.stdin.close()
            expires = began + remaining
            while selector.get_map():
                if self.cancelled or self.clock() >= expires:
                    timed_out = not self.cancelled
                    break
                for key, _ in selector.select(min(.05, max(.001, expires-self.clock()))):
                    if key.data == 'stdin':
                        try:
                            sent = os.write(key.fd, incoming[:65536]); incoming = incoming[sent:]
                        except BrokenPipeError:
                            incoming = incoming[:0]
                        except BlockingIOError:
                            continue
                        if not incoming:
                            selector.unregister(key.fileobj); key.fileobj.close()
                        continue
                    try:
                        block = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj); continue
                    left = maximum - sum(len(value) for value in outputs.values())
                    outputs[key.data].extend(block[:max(0, left)])
                    if len(block) > left:
                        truncated = True; break
                if truncated:
                    break
            # An intentionally daemonizing tool must not remain running after
            # closing stdout/stderr. This worker has one finite operation only.
            while not (timed_out or truncated or self.cancelled) and process.poll() is None:
                if self.clock() >= expires:
                    timed_out = True; break
                time.sleep(.01)
            if timed_out or truncated or self.cancelled:
                self._kill_group(process)
            process.wait(timeout=.5)
        except (OSError, subprocess.TimeoutExpired):
            self._kill_group(process)
            try: process.wait(timeout=.5)
            except subprocess.TimeoutExpired: pass
        finally:
            selector.close()
            self._kill_group(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    try: stream.close()
                    except OSError: pass
        return ProcessResult(process.returncode if process.returncode is not None else -1,
                             bytes(outputs['stdout']), bytes(outputs['stderr']), timed_out,
                             self.cancelled, truncated, max(0, (self.clock()-began)*1000))
