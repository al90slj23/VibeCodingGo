"""Read a bounded static method-version contract from the installed desktop.

The caller verifies the peer's identity; this module only reads its ASAR. It
never imports/evaluates bundled JavaScript, reads credentials, or probes a write
method. App/build numbers are deliberately not used as compatibility gates.
"""
from collections import OrderedDict
import json
import os
from pathlib import Path
import re
import stat
import struct
import threading
import time


MAX_HEADER_BYTES = 12 * 1024 * 1024
MAX_SCRIPT_BYTES = 4 * 1024 * 1024
MAX_SCAN_BYTES = 32 * 1024 * 1024
MAX_SCRIPTS = 512
MAX_CONTRACT_BYTES = 128 * 1024
MAX_METHODS = 1024
MAX_METHOD_VERSION = 1024
CACHE_ENTRIES = 8
FAILURE_RETRY_SECONDS = 30
MAIN_DIRECTORIES = ('.vite/build', 'build', 'dist/main')
_ANCHORS = frozenset(('thread-stream-state-changed', 'thread-owner-discovery',
                      'thread-stream-following-changed', 'thread-follower-start-turn',
                      'thread-follower-read-model-settings'))
_ANCHOR = re.compile(r'(?<!\\)"(?:' + '|'.join(sorted(_ANCHORS)) + r')"\s*:')
_METHOD = re.compile(r'[A-Za-z][A-Za-z0-9_./:-]{0,159}\Z')
_ASSIGNMENT_TAIL = re.compile(r'[$A-Za-z_][$A-Za-z_0-9]*\s*=\s*\Z')
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


class ProtocolContractError(ValueError):
    """The installed method contract cannot be confirmed within read bounds."""


class _Pairs(list):
    pass


def _table_from_script(text):
    """Yield complete JSON object literals with multiple protocol anchors.

    A nearby assignment is required, rather than accepting arbitrary payload
    objects/quoted documentation. JSON decoding, including duplicate detection,
    is used instead of evaluating JavaScript or extracting just known fields.
    """
    starts = set()
    decoder = json.JSONDecoder(object_pairs_hook=_Pairs)
    for match in _ANCHOR.finditer(text):
        start = text.rfind('{', max(0, match.start() - MAX_CONTRACT_BYTES), match.start())
        if start < 0 or start in starts:
            continue
        starts.add(start)
        if not _ASSIGNMENT_TAIL.search(text[max(0, start - 180):start]):
            continue
        try:
            value, end = decoder.raw_decode(text[start:start + MAX_CONTRACT_BYTES])
        except (ValueError, RecursionError):
            continue
        if not isinstance(value, _Pairs):
            continue
        keys = [key for key, _ in value]
        if len(set(keys) & _ANCHORS) < 2:
            continue
        if not value or len(value) > MAX_METHODS or len(set(keys)) != len(keys):
            raise ProtocolContractError('Codex 本机方法契约包含重复项目或超过读取范围')
        if any(not isinstance(key, str) or _METHOD.fullmatch(key) is None or
               type(version) is not int or not 0 <= version <= MAX_METHOD_VERSION
               for key, version in value):
            raise ProtocolContractError('Codex 本机方法契约不是完整的静态整数版本表')
        # The object literal must end as an assigned expression. In particular,
        # a valid JSON prefix cannot stand in for a spread/computed JS object.
        tail = text[start + end:start + end + 64].lstrip()
        if tail and tail[0] not in ';,)}\n\r':
            continue
        yield dict(value)


def _asar_entries(header):
    if not isinstance(header, dict) or not isinstance(header.get('files'), dict):
        raise ProtocolContractError('Codex 本机资源索引无效')
    found = []
    for directory in MAIN_DIRECTORIES:
        node = header
        for part in directory.split('/'):
            files = node.get('files') if isinstance(node, dict) else None
            node = files.get(part) if isinstance(files, dict) else None
        files = node.get('files') if isinstance(node, dict) else None
        if not isinstance(files, dict):
            continue
        # Only immediate main-process build entries: never recurse through the
        # renderer, webview assets or node_modules in a hundreds-of-MB archive.
        for name, item in files.items():
            if (not isinstance(name, str) or '/' in name or '\\' in name or
                    not name.endswith(('.js', '.cjs', '.mjs'))):
                continue
            if not isinstance(item, dict) or 'link' in item or item.get('unpacked'):
                continue
            size, offset = item.get('size'), item.get('offset')
            if (type(size) is not int or size < 0 or not isinstance(offset, str) or
                    not offset.isascii() or not offset.isdecimal() or len(offset) > 20):
                raise ProtocolContractError('Codex 本机主进程资源条目无效')
            if size > MAX_SCRIPT_BYTES:
                continue
            found.append((directory + '/' + name, int(offset), size))
            if len(found) > MAX_SCRIPTS:
                raise ProtocolContractError('Codex 本机主进程资源超过有界读取范围')
    return sorted(found)


def _read_contract(archive, expected):
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(archive, flags)
        with os.fdopen(fd, 'rb') as stream:
            current = os.fstat(stream.fileno())
            stamp = (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            if stamp != expected:
                raise ProtocolContractError('Codex 正在更新本机资源，稍后会自动重新核对')
            prefix = stream.read(16)
            if len(prefix) != 16:
                raise ProtocolContractError('Codex 本机资源头不完整')
            word, header_size, pickle_size, json_size = struct.unpack('<4I', prefix)
            if (word != 4 or not 8 <= header_size <= MAX_HEADER_BYTES or
                    header_size % 4 or pickle_size != header_size - 4 or
                    json_size <= 0 or not 0 <= header_size - 8 - json_size <= 3 or
                    8 + header_size > current.st_size):
                raise ProtocolContractError('Codex 本机资源头无效或超过读取范围')
            raw = stream.read(json_size)
            if len(raw) != json_size:
                raise ProtocolContractError('Codex 本机资源索引不完整')
            try:
                header = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise ProtocolContractError('Codex 本机资源索引无法解析') from None
            entries = _asar_entries(header)
            data_start, scanned, contract = 8 + header_size, 0, None
            for _, offset, size in entries:
                if data_start + offset + size > current.st_size:
                    raise ProtocolContractError('Codex 本机主进程资源位置无效')
                scanned += size
                if scanned > MAX_SCAN_BYTES:
                    raise ProtocolContractError('Codex 本机主进程资源超过有界读取范围')
                stream.seek(data_start + offset)
                source = stream.read(size)
                if len(source) != size:
                    raise ProtocolContractError('Codex 本机主进程资源不完整')
                try:
                    text = source.decode('utf-8')
                except UnicodeError:
                    raise ProtocolContractError('Codex 本机主进程资源编码无法确认') from None
                for candidate in _table_from_script(text):
                    if contract is not None and contract != candidate:
                        raise ProtocolContractError('Codex 本机方法契约存在冲突，暂未启用对应操作')
                    contract = candidate
            final = os.fstat(stream.fileno())
            if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != expected:
                raise ProtocolContractError('Codex 正在更新本机资源，稍后会自动重新核对')
            if not contract:
                raise ProtocolContractError('未能在有界的 Codex 主进程资源中确认静态方法契约')
            return contract
    except OSError:
        raise ProtocolContractError('Codex 本机资源暂不可读，稍后会自动重新核对') from None


def read_protocol_contract(bundle):
    """Return a copy of the verified static table, cached by archive identity.

    Both successes and failures are cached. Failures expire so transient file
    access errors recover even without a resource replacement. Updating app.asar changes
    the key and retries parsing automatically. The small process-local cache
    contains method names/numbers only, never conversation or account data.
    """
    try:
        root = Path(bundle).resolve()
        archive = root / 'Contents/Resources/app.asar'
        meta = archive.lstat()
        if not stat.S_ISREG(meta.st_mode):
            raise ProtocolContractError('Codex 本机资源文件类型无法确认')
        stamp = (meta.st_dev, meta.st_ino, meta.st_size, meta.st_mtime_ns)
    except (OSError, ValueError, TypeError):
        raise ProtocolContractError('Codex 本机资源暂不可读，稍后会自动重新核对') from None
    key = (str(root), *stamp)
    with _CACHE_LOCK:
        now = time.monotonic()
        cached = _CACHE.get(key)
        if cached and not cached[0] and now - cached[2] >= FAILURE_RETRY_SECONDS:
            del _CACHE[key]
        if key in _CACHE:
            success, value, checked_at = _CACHE.pop(key)
            _CACHE[key] = success, value, checked_at
        else:
            try:
                value = _read_contract(archive, stamp)
                success = True
            except ProtocolContractError as error:
                success, value = False, str(error)
            _CACHE[key] = success, value, now
            while len(_CACHE) > CACHE_ENTRIES:
                _CACHE.popitem(last=False)
        if not success:
            raise ProtocolContractError(value)
        return dict(value)
