"""Finite process groups for explicit network diagnostics.

No work starts on import. Each operation owns one deadline and cancellation
flag; child commands never use a shell and both output pipes have hard bounds.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import selectors
import signal
import subprocess
import threading
import time

MAX_CAPTURE = 8 * 1024 * 1024
MAX_STDIN = 256 * 1024
_ACTIVE = ContextVar('vcgo_network_runtime', default=None)


class NetworkCancelled(RuntimeError):
    pass


class NetworkRuntime:
    def __init__(self, *, timeout=45, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + max(.05, min(55, float(timeout)))
        self.cancelled = False

    @staticmethod
    def current():
        return _ACTIVE.get()

    def remaining(self, maximum=45):
        self.check()
        return max(.001, min(float(maximum), self.deadline - self.clock()))

    def check(self):
        if self.cancelled:
            raise NetworkCancelled('cancelled')
        if self.clock() >= self.deadline:
            raise subprocess.TimeoutExpired([], 45)

    @contextmanager
    def signals(self):
        previous = {}
        token = _ACTIVE.set(self)
        if threading.current_thread() is threading.main_thread():
            for number in (signal.SIGTERM, signal.SIGINT):
                previous[number] = signal.getsignal(number)
                signal.signal(number, lambda *_: setattr(self, 'cancelled', True))
        try:
            yield self
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
            _ACTIVE.reset(token)

    def sleep(self, seconds):
        until = self.clock() + max(0, min(2, float(seconds)))
        while self.clock() < until:
            self.check()
            time.sleep(min(.05, until - self.clock(), max(.001, self.deadline - self.clock())))
        self.check()

    @staticmethod
    def _finish(process):
        # start_new_session gives this exact child a private process group.
        # Descendants retaining a pipe must not survive the diagnostic.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=.05)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            pass

    def capture(self, argv, *, input=b'', timeout=15, env=None, limit=1048576):
        if (not isinstance(argv, (list, tuple)) or not argv or len(argv) > 160 or
                any(not isinstance(value, str) or '\x00' in value or len(value) > 16384 for value in argv) or
                not os.path.isabs(argv[0])):
            raise ValueError('Invalid network command')
        if not isinstance(input, bytes) or len(input) > MAX_STDIN:
            raise ValueError('Invalid network stdin')
        maximum = max(1024, min(MAX_CAPTURE, int(limit)))
        duration = self.remaining(timeout)
        deadline = self.clock() + duration
        process = subprocess.Popen(list(argv), stdin=subprocess.PIPE if input else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                                   shell=False, start_new_session=True)
        selector = selectors.DefaultSelector()
        output, errors = bytearray(), bytearray()
        incoming = memoryview(input)
        oversized = False
        try:
            for pipe, destination, cap in ((process.stdout, output, maximum), (process.stderr, errors, 16384)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, (destination, cap))
            if process.stdin:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, None)
            while selector.get_map():
                self.check()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, duration)
                for key, _ in selector.select(min(.05, remaining)):
                    if key.data is None:
                        try:
                            incoming = incoming[os.write(key.fd, incoming[:65536]):]
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
                        selector.unregister(key.fileobj)
                        continue
                    destination, cap = key.data
                    available = max(0, cap - len(destination))
                    destination.extend(block[:available])
                    if len(block) > available:
                        oversized = True
                        break
                if oversized:
                    break
            while not oversized and process.poll() is None:
                self.check()
                if self.clock() >= deadline:
                    raise subprocess.TimeoutExpired(argv, duration)
                time.sleep(.01)
            if oversized:
                return subprocess.CompletedProcess(argv, -1000, stdout=bytes(output), stderr=b'')
            return subprocess.CompletedProcess(argv, process.returncode, stdout=bytes(output), stderr=bytes(errors))
        finally:
            selector.close()
            self._finish(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
