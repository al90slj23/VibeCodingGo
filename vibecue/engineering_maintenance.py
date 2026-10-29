"""Finite, official engineering-engine updates and cheap settings snapshots.

No work starts on import. Native code schedules maintain only when due/idle;
this module additionally coordinates across processes and rechecks policy at
publication. The update lease is held only for the tiny link/manifest commit.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

from . import engineering_install as install
from .engineering_runtime import ProcessResult
from .engineering_compat import CompatError, verify_arm64, verify_compatibility

SCHEMA_VERSION = 1
MAX_JSON = 256 * 1024
DEFAULT_CONFIG = {'automatic_updates': True, 'interval_hours': 24}
STATES = {'current', 'updated', 'available', 'checking', 'downloading', 'validating',
          'incompatible', 'error', 'deferred', 'cancelled', 'missing', 'unchecked'}


def _root(home):
    return Path(home) / 'engineering-tools'


def _json_file(path, default):
    try:
        if path.is_symlink() or path.stat().st_size > MAX_JSON:
            raise install.InstallError('工具设置或状态文件无效。')
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise install.InstallError('工具设置或状态文件无效。')
        return value
    except FileNotFoundError:
        return dict(default)
    except (ValueError, TypeError):
        raise install.InstallError('工具设置或状态文件无法读取。') from None


def _configuration_record(home):
    value = _json_file(_root(home) / 'settings.json', DEFAULT_CONFIG)
    automatic = value.get('automatic_updates', True)
    interval = value.get('interval_hours', 24)
    if type(automatic) is not bool or type(interval) is not int or not 6 <= interval <= 168:
        raise install.InstallError('自动更新配置无效。')
    revision=value.get('revision',0)
    if type(revision) is not int or not 0 <= revision < 2**53:
        raise install.InstallError('自动更新设置版本无效。')
    return {'automatic_updates': automatic, 'interval_hours': interval}, revision


def configuration(home):
    return _configuration_record(home)[0]


def _state(home):
    value = _json_file(_root(home) / 'maintenance.json', {'tools': {}})
    if not isinstance(value.get('tools', {}), dict):
        raise install.InstallError('工具更新状态无效。')
    revision=value.get('revision',0)
    if type(revision) is not int or not 0 <= revision < 2**53:
        raise install.InstallError('工具更新状态版本无效。')
    return value


def _finite_time(value):
    return value if type(value) in (int, float) and 0 <= value < 1e12 else 0


def _next_check(config, state):
    if not config['automatic_updates']:
        return 0
    if state.get('failures', 0):
        return _finite_time(state.get('retry_after'))
    checked = _finite_time(state.get('checked_at'))
    return checked + config['interval_hours'] * 3600 if checked else 0


def _busy(root):
    """Read existing lock only; status never creates a file or directory."""
    path = root / '.maintenance.lock'
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(descriptor)


def _metadata_entry(root, name, entry):
    if not isinstance(entry, dict) or install.version_tuple(entry.get('version')) is None:
        return None
    relative = entry.get('binary')
    if not isinstance(relative, str) or not install.valid_member(relative):
        return None
    parts = PurePosixPath(relative).parts
    if len(parts) != 4 or parts[0] != 'versions' or parts[2:] != ('bin', name):
        return None
    target = root.joinpath(*parts)
    try:
        current = root
        for part in parts:
            current = current / part
            if current.is_symlink():
                return None
        meta = target.stat()
        link = root / 'bin' / name
        if not stat.S_ISREG(meta.st_mode) or not meta.st_mode & stat.S_IXUSR or not link.is_symlink() or link.resolve(strict=True) != target.resolve(strict=True):
            return None
        return target
    except (OSError, RuntimeError):
        return None


def status(home, *, busy=None, next_check_at=None):
    root = _root(home)
    if root.is_symlink():
        raise install.InstallError('工具安装目录不能是符号链接。')
    config, revision = _configuration_record(home)
    state = _state(home)
    manifest = install.load_manifest(root)
    occupied = _busy(root) if busy is None else busy
    rows = []
    for name in install.TOOLS:
        entry = manifest['tools'].get(name, {})
        target = _metadata_entry(root, name, entry)
        memo = state.get('tools', {}).get(name, {})
        if not isinstance(memo, dict):
            memo = {}
        code = memo.get('status', 'unchecked' if target else 'missing')
        if code not in STATES:
            code = 'unchecked'
        if not occupied and code in ('checking', 'downloading', 'validating'):
            code = 'cancelled'
        message = memo.get('message', '等待首次自动检查。' if target else '未发现完整的托管安装；更新检查可自动修复。')
        if not isinstance(message, str):
            message = '状态暂不可读。'
        rows.append({'name': name, 'installed_version': entry.get('version') if target else None,
                     'available_version': memo.get('available_version'), 'status': code,
                     'message': message[:240], 'checked_at': _finite_time(memo.get('checked_at')),
                     'updated_at': _finite_time(memo.get('updated_at')), 'path': str(target) if target else None,
                     'compatibility_checked_at': _finite_time(entry.get('compatibility_checked_at')),
                     'compatible': entry.get('compatibility_schema') == 1})
    due = _next_check(config, state) if next_check_at is None else next_check_at
    summary = '正在后台检查或更新；现有版本继续使用。' if occupied else '自动更新已开启；空闲时检查官方稳定版。' if config['automatic_updates'] else '自动更新已关闭；仍可立即检查。'
    return {'schema_version': 1, 'kind': 'engineering_settings', 'status': 'busy' if occupied else 'success',
            'config': config, 'config_revision': revision, 'state_revision': state.get('revision',0), 'tools': rows, 'summary': summary, 'busy': bool(occupied),
            'checked_at': _finite_time(state.get('checked_at')), 'next_check_at': due,
            'last_error': state.get('last_error', '')[:240] if isinstance(state.get('last_error', ''), str) else ''}


@contextmanager
def _settings_lock(root, *, shared=False):
    descriptor=os.open(root/'.settings.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def configure(home, payload):
    if not isinstance(payload, dict) or set(payload) - set(DEFAULT_CONFIG):
        raise install.InstallError('更新设置包含未知字段。')
    root = _root(home)
    install.private_directory(root)
    with _settings_lock(root):
        config,revision = _configuration_record(home)
        config.update(payload)
        if type(config['automatic_updates']) is not bool or type(config['interval_hours']) is not int or not 6 <= config['interval_hours'] <= 168:
            raise install.InstallError('检查周期应为 6–168 小时，自动更新应为开关。')
        install.atomic_json(root / 'settings.json', dict(config,revision=revision+1))
    return status(home)


class UpdateRunner:
    """Small one-shot runner with cancellation, a total deadline and pipe caps."""
    def __init__(self, timeout=145, clock=time.monotonic):
        self.clock = clock
        self.deadline = clock() + min(145, max(1, timeout))
        self.cancelled = False

    def run(self, argv, *, input=None, timeout=15, max_output=1048576, cwd='/', env=None):
        began = self.clock()
        budget = min(timeout, 90, self.deadline-began)
        if self.cancelled or budget <= 0:
            return ProcessResult(-1, cancelled=self.cancelled, timed_out=budget <= 0)
        if not isinstance(argv, (list, tuple)) or not argv or not os.path.isabs(str(argv[0])) or any(not isinstance(x, str) or '\x00' in x for x in argv):
            raise install.InstallError('更新工具命令无效。')
        if input is not None and (not isinstance(input, bytes) or len(input) > MAX_JSON):
            raise install.InstallError('更新工具输入过大。')
        limit = max(1024, min(2 * 1024 * 1024, max_output))
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                                   env=env or install.safe_environment(), start_new_session=True)
        selector = selectors.DefaultSelector()
        outputs = {'stdout': bytearray(), 'stderr': bytearray()}
        incoming = memoryview(input or b'')
        timed_out = truncated = False
        try:
            for key in outputs:
                stream = getattr(process, key); os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, key)
            if process.stdin:
                os.set_blocking(process.stdin.fileno(), False)
                if incoming: selector.register(process.stdin, selectors.EVENT_WRITE, 'input')
                else: process.stdin.close()
            while selector.get_map():
                if self.cancelled or self.clock()-began >= budget:
                    timed_out = not self.cancelled; break
                for key, _ in selector.select(min(.1, max(.001, budget-(self.clock()-began)))):
                    if key.data == 'input':
                        try: incoming = incoming[os.write(key.fd, incoming[:65536]):]
                        except BrokenPipeError: incoming = incoming[:0]
                        except BlockingIOError: continue
                        if not incoming: selector.unregister(key.fileobj); key.fileobj.close()
                        continue
                    try: block = os.read(key.fd, 65536)
                    except BlockingIOError: continue
                    if not block: selector.unregister(key.fileobj); continue
                    remaining = limit-sum(map(len, outputs.values()))
                    outputs[key.data].extend(block[:max(0, remaining)])
                    if len(block) > remaining: truncated = True; break
                if truncated: break
            while process.poll() is None and not (self.cancelled or timed_out or truncated):
                if self.clock()-began >= budget: timed_out=True; break
                time.sleep(.01)
        finally:
            selector.close()
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream: stream.close()
        return ProcessResult(process.returncode, bytes(outputs['stdout']), bytes(outputs['stderr']),
                             timed_out, self.cancelled, truncated, (self.clock()-began)*1000)

    @contextmanager
    def signals(self):
        previous = {}
        for number in (signal.SIGTERM, signal.SIGINT):
            previous[number] = signal.getsignal(number)
            signal.signal(number, lambda *_: setattr(self, 'cancelled', True))
        try: yield self
        finally:
            for number, handler in previous.items(): signal.signal(number, handler)


def _checked_output(runner, argv, *, timeout=15, max_output=MAX_JSON, cwd='/', env=None):
    result = runner.run(argv, timeout=timeout, max_output=max_output, cwd=cwd,
                        env=env or install.safe_environment(download=True))
    if result.cancelled:
        raise install.InstallError('更新已取消；现有版本保留。')
    if result.returncode or result.timed_out or result.truncated:
        raise install.InstallError('官方服务暂不可读、限流或网络超时；保留现有版本并自动稍后重试。')
    return result.stdout


def fetch_release(name, runner):
    repo = install.TOOLS[name]['repo']
    url = 'https://api.github.com/repos/' + repo + '/releases/latest'
    command = ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--fail', '--location',
               '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '3',
               '--connect-timeout', '5', '--max-time', '12', '--max-filesize', str(MAX_JSON),
               '--retry', '0', '-H', 'Accept: application/vnd.github+json',
               '-H', 'User-Agent: VibeCodingGo-Engineering-Updater', url]
    raw = _checked_output(runner, command, timeout=14)
    try: value = json.loads(raw)
    except (ValueError, TypeError): raise install.InstallError('官方版本目录格式无法识别。') from None
    return release_spec(name, value)


def release_spec(name, value):
    if not isinstance(value, dict) or value.get('draft') is not False or value.get('prerelease') is not False:
        raise install.InstallError('官方最新版本不是可验证的稳定发布。')
    tag = value.get('tag_name')
    if not isinstance(tag, str): raise install.InstallError('官方版本标签无效。')
    version = tag if name == 'hurl' else tag[1:] if tag.startswith('v') else ''
    if install.version_tuple(version) is None or tag != (version if name == 'hurl' else 'v'+version):
        raise install.InstallError('官方版本标签暂不支持；现有版本保留。')
    expected = install.official_spec(name, version, '0'*64, 1)
    assets = value.get('assets')
    if not isinstance(assets, list) or len(assets)>300:
        raise install.InstallError('官方发布资源目录无效。')
    chosen = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == expected['asset']]
    if len(chosen) != 1:
        raise install.InstallError('官方发布缺少唯一的 Apple Silicon 安装包；稍后自动重试。')
    asset = chosen[0]; checksum = asset.get('digest', '')
    if not isinstance(checksum, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', checksum) or asset.get('state') != 'uploaded':
        raise install.InstallError('官方发布包尚无可验证 SHA-256 摘要；未下载执行。')
    spec = install.official_spec(name, version, checksum[7:], asset.get('size'))
    if asset.get('browser_download_url') != install.source_url(spec):
        raise install.InstallError('官方发布包下载身份不一致。')
    return spec


def download(spec, path, runner):
    command = ['/usr/bin/curl', '--disable', '--silent', '--show-error', '--fail', '--location',
               '--proto', '=https', '--proto-redir', '=https', '--max-redirs', '5',
               '--connect-timeout', '8', '--max-time', '85', '--max-filesize', str(spec['size']),
               '--retry', '0', '--output', str(path), install.source_url(spec)]
    _checked_output(runner, command, timeout=88, max_output=16384, cwd=str(path.parent))
    if path.stat().st_size != spec['size'] or install.digest(path) != spec['sha256']:
        raise install.InstallError('发布包大小或 SHA-256 校验失败；未执行下载内容。')


def _verify_version(binary, name, spec, runner):
    with tempfile.TemporaryDirectory(prefix='vcgo-update-version-') as temp:
        output = _checked_output(runner, [str(binary), spec['version_flag']], timeout=5,
                                 max_output=16384, cwd=temp,
                                 env=install.safe_environment(home=Path(temp)))
    lines = output.decode('utf-8','replace').splitlines(); first = lines[0] if lines else ''
    if not re.search(r'(?<![\d.])'+re.escape(spec['version'])+r'(?![\d.])', first):
        raise install.InstallError('二进制版本与官方目录不一致。')
    return ''.join(c for c in first if 32 <= ord(c) < 127)[:240]


def _record(root, state):
    revision=state.get('revision',0)
    if type(revision) is not int or not 0 <= revision < 2**53-1:
        raise install.InstallError('工具更新状态版本无效。')
    state['revision']=revision+1
    install.atomic_json(root / 'maintenance.json', state)


def _set_tool(root, state, name, code, message, **fields):
    row = state.setdefault('tools', {}).setdefault(name, {})
    row.update(status=code, message=message, **fields)
    _record(root, state)


def _publish(home, name, entry, manifest, *, automatic, runner=None):
    root = _root(home)
    # No network, process, decompression or binary hashing under the use lease.
    with install.tool_use_lock(home, exclusive=True, nonblocking=True) as acquired:
        if not acquired:
            return False
        with _settings_lock(root,shared=True):
            if runner is not None and (runner.cancelled or runner.clock()>=runner.deadline):
                return False
            if automatic and not configuration(home)['automatic_updates']:
                return False
            current = install.load_manifest(root)
            if current['tools'].get(name) != manifest['tools'].get(name):
                raise install.InstallError('当前工具版本已发生变化；本次候选未覆盖。')
            target = root / 'bin' / name
            if target.exists() and not target.is_symlink():
                raise install.InstallError('工具入口存在非托管文件，保留当前内容。')
            previous_link = os.readlink(target) if target.is_symlink() else None
            previous = current['tools'].get(name)
            published = dict(current, schema_version=1, platform='macos-arm64', tools=dict(current['tools']))
            if previous is not None:
                entry = dict(entry, previous={key:value for key,value in previous.items() if key!='previous'})
            published['tools'][name] = entry
            temporary = root / 'bin' / ('.update-'+uuid.uuid4().hex)
            try:
                temporary.symlink_to('../'+entry['binary']); os.replace(temporary, target)
                install.atomic_json(root / 'manifest.json', published)
            except BaseException:
                if previous_link is None: target.unlink(missing_ok=True)
                else:
                    temporary.unlink(missing_ok=True); temporary.symlink_to(previous_link); os.replace(temporary,target)
                raise
            finally:
                temporary.unlink(missing_ok=True)
            manifest.clear(); manifest.update(published)
            return True


def maintain(home, *, force=False, runner=None, release_fetcher=fetch_release, downloader=download,
             compatibility=verify_compatibility, architecture=verify_arm64, now=None):
    now = time.time() if now is None else now
    config = configuration(home); state = _state(home)
    if not force and (not config['automatic_updates'] or now < _next_check(config,state)):
        return status(home)
    root = _root(home)
    install.private_directory(root); install.private_directory(root/'versions'); install.private_directory(root/'bin')
    descriptor = os.open(root/'.maintenance.lock', os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try: fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return status(home,busy=True,next_check_at=now+60)
        # A competing maintainer may have committed after our cheap precheck.
        # Reload the policy and revision baseline only once we own this lease.
        config = configuration(home)
        state = _state(home)
        if not force and (not config['automatic_updates'] or now < _next_check(config,state)):
            return status(home,busy=False)
        # Bootstrap uses this lock too; preparation may run while ordinary tools
        # continue, but two installers must never race their manifests.
        with install.install_lock(root):
            runner = runner or UpdateRunner()
            manifest = install.load_manifest(root)
            state.setdefault('tools',{})
            failures = 0; deferred = False
            for name in install.TOOLS:
                if runner.cancelled:
                    failures += 1
                    _set_tool(root,state,name,'cancelled','更新已取消；现有版本保留。',checked_at=now)
                    continue
                row = state['tools'].setdefault(name,{})
                _set_tool(root,state,name,'checking','正在读取官方稳定发布目录。',checked_at=now)
                try:
                    spec = release_fetcher(name,runner)
                    row['available_version'] = spec['version']
                    old = manifest['tools'].get(name)
                    installed = install.version_tuple(old.get('version')) if isinstance(old,dict) else None
                    offered = install.version_tuple(spec['version'])
                    if installed and offered < installed:
                        _set_tool(root,state,name,'current','本机版本比官方 latest 更高；保留现有版本。',checked_at=now)
                        continue
                    pending = row.get('candidate')
                    if isinstance(pending,dict) and pending.get('compatibility_schema')==1 and install.verified_entry(root,name,spec,pending):
                        if _publish(home,name,pending,manifest,automatic=not force,runner=runner):
                            row.pop('candidate',None)
                            _set_tool(root,state,name,'updated','已在空闲时切换到通过兼容性验证的版本。',checked_at=now,updated_at=now)
                        else:
                            deferred=True;_set_tool(root,state,name,'deferred','候选已验证；等待工程任务空闲后切换。',checked_at=now)
                        continue
                    matches = isinstance(old,dict) and old.get('version') == spec['version'] and install.verified_entry(root,name,spec,old,linked=True)
                    if matches and old.get('compatibility_schema') == 1:
                        _set_tool(root,state,name,'current','已是官方稳定版；兼容性检查已通过。',checked_at=now)
                        continue
                    if matches:
                        binary = root/old['binary']
                        _set_tool(root,state,name,'validating','正在验证当前版本的接口兼容性。',checked_at=now)
                        architecture(binary); result=compatibility(name,binary,runner)
                        if not isinstance(result,dict) or result.get('compatible') is not True:
                            raise CompatError('接口兼容性结果无效。')
                        entry=dict(old,compatibility_schema=1,compatibility_checked_at=now,
                                   compatibility_summary=str(result.get('summary','接口兼容'))[:180])
                        if not _publish(home,name,entry,manifest,automatic=not force,runner=runner):
                            deferred=True;_set_tool(root,state,name,'deferred','工程任务使用中或自动更新已关闭；稍后再验证发布。',checked_at=now)
                        else:
                            _set_tool(root,state,name,'current','当前稳定版兼容性已验证。',checked_at=now)
                        continue
                    _set_tool(root,state,name,'downloading','正在后台准备官方更新包。',checked_at=now)
                    with tempfile.TemporaryDirectory(prefix='.update-',dir=root) as temp:
                        staging=Path(temp); archive=staging/spec['asset'];downloader(spec,archive,runner)
                        if archive.stat().st_size != spec['size'] or install.digest(archive) != spec['sha256']:
                            raise install.InstallError('发布包校验失败；现有版本保留。')
                        directory=staging/(name+'-'+spec['version']+'-macos-arm64-'+uuid.uuid4().hex[:12]);(directory/'bin').mkdir(mode=0o700,parents=True)
                        binary=directory/'bin'/name;install.extract_binary(archive,name,spec,binary)
                        _set_tool(root,state,name,'validating','正在验证架构、版本与真实接口。',checked_at=now)
                        architecture(binary);version=_verify_version(binary,name,spec,runner);probe=compatibility(name,binary,runner)
                        if not isinstance(probe,dict) or probe.get('compatible') is not True:
                            raise CompatError('接口兼容性结果无效。')
                        if runner.cancelled or runner.clock()>=runner.deadline:
                            raise install.InstallError('更新已取消或达到总时限；现有版本保留。')
                        entry=dict(spec,binary=str(Path('versions')/directory.name/'bin'/name),binary_sha256=install.digest(binary),binary_size=binary.stat().st_size,
                                   source_url=install.source_url(spec),version_text=version,installed_at=datetime.fromtimestamp(now,timezone.utc).isoformat(),
                                   compatibility_schema=1,compatibility_checked_at=now,compatibility_summary=str(probe.get('summary','接口兼容'))[:180])
                        directory.rename(root/'versions'/directory.name)
                        if not _publish(home,name,entry,manifest,automatic=not force,runner=runner):
                            deferred=True;_set_tool(root,state,name,'deferred','工程任务使用中或自动更新已关闭；已保留旧版，稍后重试。',checked_at=now,candidate=entry)
                        else:
                            _set_tool(root,state,name,'updated','更新完成，真实接口兼容性检查通过。',checked_at=now,updated_at=now)
                except CompatError:
                    failures+=1;_set_tool(root,state,name,'incompatible','新版接口或架构未通过兼容性检查；保留当前可用版本，稍后自动复查。',checked_at=now)
                except install.InstallError as error:
                    failures+=1;_set_tool(root,state,name,'cancelled' if runner.cancelled else 'error',str(error)[:240],checked_at=now)
                except (OSError,ValueError,TypeError,KeyError):
                    failures+=1;_set_tool(root,state,name,'error','更新准备失败；现有可用版本保留，稍后自动重试。',checked_at=now)
            state['checked_at']=now
            state['failures']=(min(8,int(state.get('failures',0))+1) if failures else 0)
            state['last_error']='部分工具未能更新；当前版本保留。' if failures else ''
            state['retry_after']=now+(min(21600,300*(2**(state['failures']-1))) if failures else 60 if deferred else 0)
            if deferred and not failures: state['failures']=1
            _record(root,state)
        return status(home,busy=False)
    finally:
        os.close(descriptor)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('status','configure','check','maintain'))
    parser.add_argument('--home',type=Path,default=Path(os.environ.get('VIBECUE_HOME',str(Path.home()/'Library/Application Support/VibeCue'))))
    args=parser.parse_args(argv)
    try:
        if not args.home.is_absolute():raise install.InstallError('工具数据目录必须为绝对路径。')
        if args.command=='status':value=status(args.home)
        else:
            raw=sys.stdin.buffer.read(MAX_JSON+1)
            if len(raw)>MAX_JSON:raise install.InstallError('设置输入超过 256 KiB。')
            payload=json.loads(raw) if raw.strip() else {}
            if not isinstance(payload,dict):raise install.InstallError('工具设置输入无效。')
            if args.command=='configure':value=configure(args.home,payload)
            else:
                if payload:raise install.InstallError('更新检查不接受自定义来源或命令。')
                if sys.platform!='darwin' or platform.machine().lower() not in ('arm64','aarch64'):
                    raise install.InstallError('自动工具更新目前仅支持 Apple Silicon macOS。')
                runner=UpdateRunner()
                with runner.signals():value=maintain(args.home,force=args.command=='check',runner=runner)
        print(json.dumps(value,ensure_ascii=False,separators=(',',':')))
        return 0
    except install.InstallError as error:
        print(json.dumps({'schema_version':1,'kind':'engineering_settings','status':'error','summary':str(error)[:240]},ensure_ascii=False))
        return 0
    except (ValueError,OSError):
        print(json.dumps({'schema_version':1,'kind':'engineering_settings','status':'error','summary':'工程工具设置或更新未完成；现有版本保留。'},ensure_ascii=False))
        return 0


if __name__=='__main__':raise SystemExit(main())
