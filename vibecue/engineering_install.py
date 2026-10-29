#!/usr/bin/env python3
"""Explicit, pinned engineering CLI installation; never imported by app startup.

Official archives are SHA-256 checked before any member is read or executed.
The app only discovers engineering-tools/bin; installation is a separate action.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile

TOOLS = {
    'mise': {'version': '2026.10.23', 'repo': 'jdx/mise', 'tag': 'v2026.10.23',
             'asset': 'mise-v2026.10.23-macos-arm64.tar.xz', 'size': 28799528,
             'sha256': 'ef29b18f373ca90b8a1c9400acbe439d40758620870df705e1473095e87f461d',
             'format': 'tar', 'version_flag': '--version', 'license': 'MIT'},
    'hurl': {'version': '8.1.0', 'repo': 'Orange-OpenSource/hurl', 'tag': '8.1.0',
             'asset': 'hurl-8.1.0-aarch64-apple-darwin.tar.gz', 'size': 2874067,
             'sha256': 'f5c6bf73172779e9d22b87249727d1c0ac232137f07df0c7d73b3ec4bcaa9f3b',
             'format': 'tar', 'version_flag': '--version', 'license': 'Apache-2.0'},
    'lnav': {'version': '0.14.1', 'repo': 'tstack/lnav', 'tag': 'v0.14.1',
             'asset': 'lnav-0.14.1-aarch64-macos.zip', 'size': 16199552,
             'sha256': '3b860ec5b2368e045a500c107006eee7077f9b66692dc17fcbf0b36a859f6631',
             'format': 'zip', 'version_flag': '-V', 'license': 'BSD-2-Clause'},
}
MAX_BINARY = 180 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_MANIFEST = 65536


class InstallError(RuntimeError):
    """Only safe, fixed diagnostics, without proxy credentials or remote text."""


def source_url(spec):
    return f"https://github.com/{spec['repo']}/releases/download/{spec['tag']}/{spec['asset']}"


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def private_directory(path):
    if path.is_symlink():
        raise InstallError('工具安装目录不能是符号链接。')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise InstallError('工具安装目录无效。')


def safe_environment(*, download=False, home=None):
    allowed = ('HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ')
    result = {key: value for key in allowed if (value := os.environ.get(key)) is not None}
    result.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', NO_COLOR='1', TERM='dumb',
                  MISE_NO_CONFIG='1', MISE_AUTO_INSTALL='0', MISE_OFFLINE='1', COREPACK_ENABLE_NETWORK='0')
    if home:
        result.update(HOME=str(home), XDG_CONFIG_HOME=str(home / 'config'), XDG_DATA_HOME=str(home / 'data'),
                      XDG_CACHE_HOME=str(home / 'cache'))
    if download:
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
                    'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
            if (value := os.environ.get(key)) is not None:
                result[key] = value
        # urllib's macOS resolver also supplies configured static system proxies.
        # Existing explicit environment settings retain precedence. No .curlrc.
        import urllib.request
        for key, value in urllib.request.getproxies().items():
            if key in ('http', 'https', 'all', 'no') and isinstance(value, str):
                target = 'no_proxy' if key == 'no' else key + '_proxy'
                if target not in result and target.upper() not in result:
                    result[target] = value
    return result


def run_bounded(argv, *, timeout, env, cwd, limit=16384):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=env, cwd=cwd,
                               shell=False, start_new_session=True)
    selector = selectors.DefaultSelector()
    collected = bytearray()
    deadline = time.monotonic() + timeout
    try:
        for pipe in (process.stdout, process.stderr):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise InstallError('工具下载或版本检查超时。')
            for key, _ in selector.select(min(.1, max(.001, deadline-time.monotonic()))):
                block = os.read(key.fd, 16384)
                if not block:
                    selector.unregister(key.fileobj)
                elif len(collected) + len(block) > limit:
                    raise InstallError('工具输出超过安全上限。')
                else:
                    collected.extend(block)
        try:
            process.wait(timeout=max(.001, deadline-time.monotonic()))
        except subprocess.TimeoutExpired:
            raise InstallError('工具下载或版本检查超时。') from None
        if process.returncode:
            raise InstallError('工具下载或版本检查失败；现有安装未被替换。')
        return bytes(collected)
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        for pipe in (process.stdout, process.stderr):
            pipe.close()


def download_archive(spec, path):
    # No credentials, shell installer, package manager, or arbitrary input URL.
    command = ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--fail', '--location',
               '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '5',
               '--connect-timeout', '15', '--max-time', '180', '--max-filesize', str(spec['size']),
               '--retry', '0', '--output', str(path), source_url(spec)]
    run_bounded(command, timeout=185, env=safe_environment(download=True), cwd=str(path.parent))
    if path.stat().st_size != spec['size'] or digest(path) != spec['sha256']:
        raise InstallError('官方发布包的大小或 SHA-256 校验失败；未执行下载内容。')


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,4}\.\d{1,3}\.\d{1,3}', value):
        return None
    return tuple(map(int, value.split('.')))


def official_spec(name, version, sha256, size):
    """Derive all executable download identity from three fixed upstreams."""
    if name not in TOOLS or version_tuple(version) is None or not isinstance(sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', sha256) or type(size) is not int or not 1 <= size <= 128 * 1024 * 1024:
        raise InstallError('官方发布版本、摘要或大小无效。')
    template = TOOLS[name]
    tag = version if name == 'hurl' else 'v' + version
    asset = {'mise': 'mise-v' + version + '-macos-arm64.tar.xz',
             'hurl': 'hurl-' + version + '-aarch64-apple-darwin.tar.gz',
             'lnav': 'lnav-' + version + '-aarch64-macos.zip'}[name]
    return dict(template, version=version, tag=tag, asset=asset, sha256=sha256, size=size)


def newer_entry_spec(name, entry):
    """A later auto-installed release must survive a pinned bootstrap rerun."""
    if not isinstance(entry, dict):
        return None
    current, baseline = version_tuple(entry.get('version')), version_tuple(TOOLS[name]['version'])
    if current is None or baseline is None or current <= baseline:
        return None
    try:
        expected = official_spec(name, entry['version'], entry.get('sha256'), entry.get('size'))
        if any(entry.get(key) != expected[key] for key in ('repo', 'tag', 'asset', 'format', 'version_flag', 'license')):
            raise InstallError('较新托管版本的来源清单无法确认；保留现有版本。')
        return expected
    except (KeyError, InstallError):
        raise InstallError('较新托管版本的来源清单无法确认；保留现有版本。') from None


def valid_member(name):
    value = PurePosixPath(name)
    return (not value.is_absolute() and '\\' not in name and
            all(part not in ('..', '') for part in value.parts))


def copy_binary(source, target, size):
    if not 1 <= size <= MAX_BINARY:
        raise InstallError('发布包中的可执行文件大小无效。')
    remaining = size
    with target.open('xb') as output:
        os.chmod(target, 0o700)
        while remaining:
            block = source.read(min(1024 * 1024, remaining))
            if not block:
                raise InstallError('发布包中的可执行文件不完整。')
            output.write(block)
            remaining -= len(block)
        if source.read(1):
            raise InstallError('发布包中的可执行文件超过声明大小。')


def extract_binary(archive, name, spec, target):
    # Do not extract directory trees, links, permissions, symlinks or scripts.
    if spec['format'] == 'tar':
        with tarfile.open(archive, 'r:*') as bundle:
            candidate = None
            for count, member in enumerate(bundle, 1):
                if count > MAX_ENTRIES or not valid_member(member.name):
                    raise InstallError('发布包的文件目录无效。')
                if member.isdir():
                    continue  # Official archives may contain mise/ before mise/bin/mise.
                if PurePosixPath(member.name).name == name:
                    if candidate is not None or not member.isfile():
                        raise InstallError('发布包中的可执行文件不唯一或不是普通文件。')
                    candidate = member
            if candidate is None:
                raise InstallError('发布包中缺少预期可执行文件。')
            with bundle.extractfile(candidate) as stream:
                copy_binary(stream, target, candidate.size)
    else:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_ENTRIES or any(not valid_member(item.filename) for item in members):
                raise InstallError('发布包的文件目录无效。')
            candidates = [item for item in members if not item.is_dir() and PurePosixPath(item.filename).name == name]
            if len(candidates) != 1:
                raise InstallError('发布包中缺少唯一的预期可执行文件。')
            member = candidates[0]
            mode = member.external_attr >> 16
            if member.is_dir() or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not stat.S_ISREG(mode)):
                raise InstallError('发布包中的可执行文件不是普通文件。')
            with bundle.open(member) as stream:
                copy_binary(stream, target, member.file_size)


def verify_version(binary, name, spec):
    with tempfile.TemporaryDirectory(prefix='vcgo-engine-install-version-') as temp:
        home = Path(temp)
        output = run_bounded([str(binary), spec['version_flag']], timeout=8,
                             env=safe_environment(home=home), cwd=str(home))
    text = output.decode('utf-8', 'replace').splitlines()
    first = text[0] if text else ''
    # Exact numeric boundaries prevent accepting e.g. 18.1.0 for 8.1.0.
    if not re.search(r'(?<![\d.])' + re.escape(spec['version']) + r'(?![\d.])', first):
        raise InstallError('工具版本与固定版本不一致。')
    return ''.join(char for char in first if 32 <= ord(char) < 127)[:240]


def load_manifest(root):
    path = root / 'manifest.json'
    try:
        if path.is_symlink() or path.stat().st_size > MAX_MANIFEST:
            raise InstallError('已有工具清单无效。')
        value = json.loads(path.read_text())
        if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('tools'), dict):
            raise InstallError('已有工具清单无效。')
        return value
    except FileNotFoundError:
        return {'schema_version': 1, 'tools': {}}
    except (ValueError, TypeError):
        raise InstallError('已有工具清单无效。') from None


def verified_entry(root, name, spec, entry, *, linked=False):
    if not isinstance(entry, dict):
        return False
    if any(entry.get(key) != spec[key] for key in ('version', 'repo', 'tag', 'asset', 'size', 'sha256')):
        return False
    raw = entry.get('binary')
    if not isinstance(raw, str) or not valid_member(raw):
        return False
    parts = PurePosixPath(raw).parts
    if len(parts) != 4 or parts[0] != 'versions' or parts[2:] != ('bin', name):
        return False
    target = root.joinpath(*parts)
    try:
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                return False
        meta = target.stat()
        expected_hash = entry.get('binary_sha256', '')
        if (not stat.S_ISREG(meta.st_mode) or not meta.st_mode & stat.S_IXUSR or
                type(entry.get('binary_size')) is not int or meta.st_size != entry['binary_size'] or
                not 1 <= meta.st_size <= MAX_BINARY or not re.fullmatch(r'[a-f0-9]{64}', expected_hash) or
                digest(target) != expected_hash):
            return False
        link = root / 'bin' / name
        if linked and (not link.is_symlink() or link.resolve(strict=True) != target.resolve(strict=True)):
            return False
        return True
    except (OSError, RuntimeError):
        return False


def atomic_json(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix='.manifest-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def install_lock(root):
    path = root / '.install.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('另一个工程工具安装正在运行。') from None
        yield
    finally:
        os.close(descriptor)


@contextmanager
def tool_use_lock(home, *, exclusive=False, nonblocking=False):
    """Lease managed tool identity. Only explicit runs/updates call this."""
    root = Path(home) / 'engineering-tools'
    private_directory(root)
    path = root / '.use.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    acquired = False
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(descriptor, mode | (fcntl.LOCK_NB if nonblocking else 0))
            acquired = True
        except BlockingIOError:
            pass
        yield acquired
    finally:
        os.close(descriptor)


@contextmanager
def tool_errors(name):
    # name is always one of the fixed three tool IDs, never user or remote text.
    try:
        yield
    except InstallError as error:
        raise InstallError(name + '：' + str(error)) from None
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        raise InstallError(name + '：读取或准备官方发布包失败；现有安装保留。') from None


def install(home, *, verify_only=False, downloader=download_archive, version_check=verify_version):
    root = home / 'engineering-tools'
    if verify_only:
        if root.is_symlink() or not root.is_dir() or (root / 'bin').is_symlink() or (root / 'versions').is_symlink():
            raise InstallError('工程工具尚未完整安装。')
        manifest = load_manifest(root)
        states = {name: verified_entry(root, name, newer_entry_spec(name, manifest['tools'].get(name)) or spec, manifest['tools'].get(name), linked=True)
                  for name, spec in TOOLS.items()}
        if not all(states.values()):
            raise InstallError('工程工具校验未通过；重新运行安装器可修复缺失或损坏的工具。')
        return {'status': 'verified', 'home': str(root), 'tools': manifest['tools']}
    private_directory(root)
    private_directory(root / 'versions')
    private_directory(root / 'bin')
    with install_lock(root):
        manifest = load_manifest(root)
        entries = {}
        # Prepare all tools before publishing any path used by the app.
        with tempfile.TemporaryDirectory(prefix='.prepare-', dir=root) as temp:
            staging = Path(temp)
            for name, spec in TOOLS.items():
                with tool_errors(name):
                    old = manifest['tools'].get(name)
                    newer = newer_entry_spec(name, old)
                    if newer is not None and not verified_entry(root, name, newer, old):
                        raise InstallError('较新托管版本校验失败；保留现有版本，等待自动修复。')
                    if verified_entry(root, name, newer or spec, old):
                        entries[name] = old
                        continue
                    directory = staging / (name + '-' + spec['version'] + '-macos-arm64-' + uuid.uuid4().hex[:12])
                    (directory / 'bin').mkdir(mode=0o700, parents=True)
                    archive = staging / spec['asset']
                    downloader(spec, archive)
                    # Independently check even an injected/replaced downloader.
                    if archive.stat().st_size != spec['size'] or digest(archive) != spec['sha256']:
                        raise InstallError('官方发布包校验失败；未执行下载内容。')
                    binary = directory / 'bin' / name
                    extract_binary(archive, name, spec, binary)
                    version = version_check(binary, name, spec)
                    archive.unlink()
                    entries[name] = dict(spec, binary=str(Path('versions') / directory.name / 'bin' / name),
                                         binary_sha256=digest(binary), binary_size=binary.stat().st_size,
                                         source_url=source_url(spec), version_text=version,
                                         installed_at=datetime.now(timezone.utc).isoformat())
            # Never overwrite a user's regular file at a known bin name.
            for name in TOOLS:
                target = root / 'bin' / name
                if target.exists() and not target.is_symlink():
                    raise InstallError('工具快捷目录中存在非托管文件；未覆盖。')
            for name, entry in entries.items():
                source = staging / PurePosixPath(entry['binary']).parts[1]
                if source.exists():
                    source.rename(root / 'versions' / source.name)
            with tool_use_lock(home, exclusive=True, nonblocking=True) as acquired:
                if not acquired:
                    raise InstallError('工程任务正在使用工具；本次安装保留现有版本，稍后可重试。')
                previous = {}
                try:
                    for name, entry in entries.items():
                        target = root / 'bin' / name
                        previous[name] = os.readlink(target) if target.is_symlink() else None
                        temporary = root / 'bin' / ('.' + name + '-' + uuid.uuid4().hex)
                        try:
                            temporary.symlink_to('../' + entry['binary'])
                            os.replace(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
                    result = {'schema_version': 1, 'platform': 'macos-arm64', 'tools': entries}
                    atomic_json(root / 'manifest.json', result)
                except BaseException:
                    for name, link in previous.items():
                        target = root / 'bin' / name
                        if link is None:
                            target.unlink(missing_ok=True)
                        else:
                            temporary = root / 'bin' / ('.rollback-' + uuid.uuid4().hex)
                            temporary.symlink_to(link)
                            os.replace(temporary, target)
                    raise
        return {'status': 'installed', 'home': str(root), 'tools': entries}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path(os.environ.get('VIBECUE_HOME',
                        str(Path.home() / 'Library/Application Support/VibeCue'))))
    parser.add_argument('--verify-only', action='store_true', help='Offline hash/link check; no downloads or tool processes')
    arguments = parser.parse_args(argv)
    if sys.platform != 'darwin' or platform.machine().lower() not in ('arm64', 'aarch64'):
        parser.error('此固定工具包仅支持 Apple Silicon macOS。')
    home = arguments.home.expanduser()
    if not home.is_absolute():
        parser.error('--home 必须是绝对路径。')
    try:
        result = install(home, verify_only=arguments.verify_only)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except InstallError as error:
        print(json.dumps({'status': 'error', 'message': str(error)}, ensure_ascii=False))
        return 1
    except (OSError, tarfile.TarError, zipfile.BadZipFile):
        # Error type is useful; arbitrary OS/remote strings may contain secrets.
        print(json.dumps({'status': 'error', 'message': '工程工具安装或校验失败；已有版本保留。请检查网络、磁盘空间或官方发布包校验。'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
