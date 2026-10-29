"""Maintain CC Switch's opt-in numeric bridge across official app updates.

No authorization files are opened here. Builds use the exact installed official
release, a checked structural adapter, isolated caches and a narrow environment.
Installation waits for an idle, non-foreground app and uses normal termination.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import uuid

from .events import vibecue_directory

APP=Path('/Applications/CC Switch.app')
NATIVE=Path('/Applications/VibeCue.app/Contents/MacOS/VibeCue')
TEMPLATE=Path('/Applications/VibeCue.app/Contents/Resources/CCSwitchIntegration/vibecue_quota.rs')
BUNDLE_ID='com.ccswitch.desktop'
OFFICIAL_TEAM='R8UR22V2F9'
VERSION=re.compile(r'^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$')
SHA=re.compile(r'^[0-9a-f]{40}$')
INITIAL_PATCH='6b0554fb388f49e2ef66b397bb22c38b42833c93e47aa98b522f1d248785f09f'
INITIAL_PRODUCTION='59d11b48d10827fa3309cc8dba0cad102fdb2cd2c63a1f24bdfcc935a3054cbe'
MAX_JSON=2*1024*1024
RETRY_SECONDS=3600
PHASES={
    'healthy':'双账号额度共享正常，升级后会自动维护。',
    'disabled':'已暂停自动维护。',
    'detected':'检测到 CC Switch 更新，正在自动恢复额度共享。',
    'preparing':'正在准备新版 CC Switch 的额度共享。',
    'downloading':'正在获取对应官方版本，已保留上次额度。',
    'adapting':'正在核对新版接口，已保留上次额度。',
    'building':'正在为新版恢复额度共享，已保留上次额度。',
    'waiting_idle':'适配已准备，等待 CC Switch 空闲后自动应用。',
    'installing':'正在自动恢复 CC Switch 额度共享。',
    'verifying':'正在确认双账号额度共享已恢复。',
    'waiting_retry':'自动恢复暂未完成，稍后会重试；上次额度已保留。',
    'incompatible':'新版接口尚未通过兼容检查；保留官方新版与上次额度，稍后自动重试。',
    'waiting_tools':'正在等待本机所需构建工具；上次额度已保留。',
    'waiting_disk':'构建盘暂不可用或空间不足，恢复后自动重试。',
    'waiting_app':'等待 CC Switch 安装完成，随后自动检查。',
}
ACTIVE_PHASES=frozenset(PHASES)-{'healthy','disabled'}


class MaintenanceError(RuntimeError):
    def __init__(self, code, phase='waiting_retry'):
        self.code=code;self.phase=phase
        super().__init__(code)


def digest(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):result.update(data)
    return result.hexdigest()


def production_hash(path):
    try:return hashlib.sha256(Path(path).read_bytes().split(b'#[cfg(test)]',1)[0]).hexdigest()
    except OSError:return None


def read_json(path, default=None):
    try:
        fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if info.st_uid!=os.getuid() or info.st_mode&0o077:return default
            data=stream.read(MAX_JSON+1)
        if len(data)>MAX_JSON:return default
        result=json.loads(data)
        return result if isinstance(result,dict) else default
    except (OSError,ValueError,RecursionError):return default


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,name=tempfile.mkstemp(prefix='.'+path.stem+'-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            os.fchmod(stream.fileno(),0o600)
            json.dump(value,stream,ensure_ascii=False,separators=(',',':'))
        os.replace(name,path)
    finally:
        try:os.unlink(name)
        except FileNotFoundError:pass


def inspect_app(app=APP):
    app=Path(app)
    try:
        if app.is_symlink():raise ValueError()
        info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
        version=info.get('CFBundleShortVersionString')
        if info.get('CFBundleIdentifier')!=BUNDLE_ID or not isinstance(version,str) or not VERSION.fullmatch(version):raise ValueError()
        if info.get('CFBundleExecutable')!='cc-switch':raise ValueError()
        binary=app/'Contents/MacOS/cc-switch'
        if binary.is_symlink() or not binary.is_file():raise ValueError()
        return {'version':version,'build':str(info.get('CFBundleVersion','')),
                'binary_sha256':digest(binary),'architecture':platform.machine()}
    except (OSError,ValueError,plistlib.InvalidFileException):
        raise MaintenanceError('app_unavailable','waiting_app') from None


def same_app(first,second):
    return all(first.get(key)==second.get(key) for key in ('version','build','binary_sha256','architecture'))


def native(action,payload=None):
    try:
        result=subprocess.run([str(NATIVE),'--ccswitch-system-action',action],
                              input=json.dumps(payload or {}).encode(),capture_output=True,timeout=25)
        value=json.loads(result.stdout)
        if result.returncode or not isinstance(value,dict):raise ValueError()
        return value
    except (OSError,ValueError,subprocess.SubprocessError):
        raise MaintenanceError('native_helper_unavailable') from None


class OfficialFetch:
    """Only public metadata/source from the fixed upstream repository."""
    HOSTS=frozenset(('api.github.com','github.com','codeload.github.com'))
    def request(self,url,maximum):
        from urllib.parse import urlparse
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.hostname not in self.HOSTS:raise MaintenanceError('source_host_invalid')
        request=urllib.request.Request(url,headers={'User-Agent':'VibeCue-local-quota-maintenance','Accept':'application/vnd.github+json'})
        try:
            with urllib.request.urlopen(request,timeout=45) as stream:
                final=urlparse(stream.url)
                if final.scheme!='https' or final.hostname not in self.HOSTS:raise MaintenanceError('source_redirect_invalid')
                data=stream.read(maximum+1)
            if len(data)>maximum:raise MaintenanceError('source_too_large')
            return data
        except (OSError,ValueError):raise MaintenanceError('official_source_unavailable') from None
    def release(self,version):
        if not VERSION.fullmatch(version):raise MaintenanceError('version_invalid')
        base='https://api.github.com/repos/farion1231/cc-switch/git/'
        try:
            tag=json.loads(self.request(base+'ref/tags/v'+version,MAX_JSON))['object']
            for _ in range(3):
                if not SHA.fullmatch(tag['sha']):raise ValueError()
                if tag['type']=='commit':return tag['sha']
                if tag['type']!='tag':raise ValueError()
                tag=json.loads(self.request(base+'tags/'+tag['sha'],MAX_JSON))['object']
        except (KeyError,TypeError,ValueError):raise MaintenanceError('official_tag_invalid') from None
        raise MaintenanceError('official_tag_invalid')
    def source(self,commit,directory):
        if not SHA.fullmatch(commit):raise MaintenanceError('commit_invalid')
        data=self.request('https://codeload.github.com/farion1231/cc-switch/tar.gz/'+commit,150*1024*1024)
        directory=Path(directory);directory.mkdir(parents=True,exist_ok=False,mode=0o700)
        archive=directory.parent/(directory.name+'.tar.gz')
        archive.write_bytes(data)
        try:
            with tarfile.open(archive,'r:gz') as stream:
                members=stream.getmembers()
                if len(members)>50000 or sum(max(0,item.size) for item in members)>1024**3:raise MaintenanceError('source_archive_too_large')
                for item in members:
                    parts=Path(item.name).parts
                    if len(parts)<2 or '..' in parts or Path(item.name).is_absolute() or not (item.isfile() or item.isdir()):
                        if item.isdir() and len(parts)==1:continue
                        raise MaintenanceError('source_archive_invalid')
                    relative=Path(*parts[1:]);target=directory/relative
                    if item.isdir():target.mkdir(parents=True,exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True,exist_ok=True)
                        with stream.extractfile(item) as source,target.open('wb') as output:shutil.copyfileobj(source,output)
                        target.chmod(0o700 if item.mode&0o111 else 0o600)
        finally:archive.unlink(missing_ok=True)


class Maintenance:
    def __init__(self,home=None,*,app=APP,clock=time.time,fetcher=None,native_call=native):
        self.home=vibecue_directory(home);self.app=Path(app);self.clock=clock
        self.config_file=self.home/'ccswitch-maintenance-config.json'
        self.state_file=self.home/'ccswitch-maintenance-state.json'
        self.fetcher=fetcher or OfficialFetch();self.native=native_call

    def config(self):return read_json(self.config_file,{})
    def state(self):return read_json(self.state_file,{})
    def update(self,phase,**values):
        result=dict(self.state());result.update(values,phase=phase,message=PHASES.get(phase,PHASES['waiting_retry']),updated_at=self.clock())
        write_json(self.state_file,result);return result

    def matches_healthy(self,identity,state=None):
        state=self.state() if state is None else state
        expected=production_hash(self.config().get('template_file',str(TEMPLATE)))
        return (not state.get('pending_rollback') and state.get('healthy_binary_sha256')==identity['binary_sha256'] and
                state.get('installed_version',identity['version'])==identity['version'] and
                state.get('healthy_build',identity['build'])==identity['build'] and
                (expected is None or state.get('healthy_production_sha256')==expected))

    def status(self):
        config=self.config();state=self.state()
        phase=state.get('phase','healthy' if config.get('enabled') else 'disabled')
        if config.get('enabled') and phase=='healthy':
            try:
                if not self.matches_healthy(inspect_app(self.app),state):phase='detected'
            except MaintenanceError:phase='waiting_app'
        return {key:state.get(key) for key in ('target_version','installed_version','updated_at','next_retry_at','error_code')} | {
            'enabled':config.get('enabled') is True,'phase':phase,'message':PHASES.get(phase,PHASES['waiting_retry']),
            'recovering':phase in ACTIVE_PHASES,'automatic':True}

    def paths(self):
        config=self.config();root=Path(config.get('work_root',''))
        if not root.is_absolute() or str(root) in ('/',str(Path.home())) or not root.parent.exists():raise MaintenanceError('work_disk_unavailable','waiting_disk')
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if shutil.disk_usage(root).free<2*1024**3:raise MaintenanceError('work_disk_full','waiting_disk')
        return root

    def cache_manifest(self,identity):
        root=self.paths()/'cache'
        if not root.exists():return None
        for path in sorted(root.glob('*/manifest.json')):
            value=read_json(path,{})
            expected=production_hash(self.config().get('template_file',str(TEMPLATE)))
            if (value.get('version')==identity['version'] and value.get('build')==identity['build'] and
                    value.get('architecture')==identity['architecture'] and value.get('bridge_protocol')==1 and
                    (expected is None or value.get('production_sha256')==expected) and
                    identity['binary_sha256'] in (value.get('upstream_binary_sha256'),value.get('binary_sha256'))):
                bundle=path.parent/'CC Switch.app'
                try:
                    found=inspect_app(bundle)
                    if found['binary_sha256']!=value.get('binary_sha256'):continue
                    self.verify_signature(bundle)
                    return dict(value,bundle_path=str(bundle))
                except MaintenanceError:continue
        return None

    @staticmethod
    def verify_signature(app):
        try:subprocess.run(['/usr/bin/codesign','--verify','--deep','--strict',str(app)],check=True,capture_output=True,timeout=30)
        except (OSError,subprocess.SubprocessError):raise MaintenanceError('bundle_signature_invalid') from None

    def recognized(self,identity):
        cache=self.cache_manifest(identity)
        if cache and cache['binary_sha256']==identity['binary_sha256']:return True
        legacy=read_json(self.home/'ccswitch-quota-install.json',{})
        if legacy.get('patch_sha256')==INITIAL_PATCH and legacy.get('binary_sha256')==identity['binary_sha256']:
            self.verify_signature(self.app);return True
        # An older local recipe is still an authenticated input for rebuilding
        # the same official version when VibeCue ships a newer bridge template.
        for path in (self.paths()/'cache').glob('*/manifest.json'):
            value=read_json(path,{})
            if value.get('binary_sha256')==identity['binary_sha256']:
                try:
                    bundle=path.parent/'CC Switch.app'
                    self.verify_signature(bundle)
                    if inspect_app(bundle)['binary_sha256']==identity['binary_sha256']:return True
                except MaintenanceError:pass
        try:
            p=subprocess.run(['/usr/bin/codesign','-dv','--verbose=4',str(self.app)],capture_output=True,text=True,timeout=10)
            self.verify_signature(self.app)
            return p.returncode==0 and ('TeamIdentifier='+OFFICIAL_TEAM) in p.stderr.splitlines()
        except (OSError,subprocess.SubprocessError,MaintenanceError):return False

    def ensure(self,spawn=None):
        if self.config().get('enabled') is not True:return self.status()
        if not (Path.home()/'.cc-switch/vibecue-quota-sharing.enabled').is_file():
            self.update('disabled');return self.status()
        try:
            identity=inspect_app(self.app);state=self.state()
            if self.matches_healthy(identity,state):
                if state.get('phase')!='healthy':self.update('healthy',installed_version=identity['version'],error_code=None)
                return self.status()
            if state.get('target_sha256')==identity['binary_sha256'] and self.clock()<state.get('next_retry_at',0):return self.status()
            # The file lock lives for the entire detached worker. A PID from a
            # prior process cannot suppress recovery after reboot or PID reuse.
            self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
            with (self.home/'ccswitch-maintenance.lock').open('a') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:return self.status()
                self.update('detected',target_version=identity['version'],target_sha256=identity['binary_sha256'],next_retry_at=self.clock()+15,
                            healthy_binary_sha256=None)
                if spawn is not None:spawn()
                else:
                    env=self.environment(self.paths())
                    env['VIBECUE_HOME']=str(self.home)
                    subprocess.Popen([sys.executable,'-m','vibecue.ccswitch_maintenance','repair'],cwd=Path(__file__).resolve().parent.parent,
                                     env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        except MaintenanceError as error:self.update(error.phase,error_code=error.code,next_retry_at=self.clock()+300)
        except (OSError,ValueError,TypeError):self.update('waiting_retry',error_code='worker_start_failed',next_retry_at=self.clock()+300)
        return self.status()

    @staticmethod
    def environment(root):
        env={key:value for key,value in os.environ.items() if key in ('HOME','USER','LOGNAME','LANG','LC_ALL','LC_CTYPE','TZ')}
        env['PATH']=str(Path.home()/'.cargo/bin')+':/opt/homebrew/opt/node@22/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'
        env['TMPDIR']=str(Path(root)/'tmp');Path(env['TMPDIR']).mkdir(parents=True,exist_ok=True,mode=0o700)
        env['CARGO_TARGET_DIR']=str(Path(root)/'target')
        env['CARGO_HOME']=str(Path(root)/'cargo-home')
        env['CARGO_BUILD_JOBS']='4'
        env['npm_config_cache']=str(Path(root)/'npm-cache')
        env['XDG_CACHE_HOME']=str(Path(root)/'tool-cache')
        env['MACOSX_DEPLOYMENT_TARGET']='12.0'
        return env

    def command(self,args,cwd,env,timeout,code):
        log=self.paths()/'build.log'
        with log.open('ab') as output:
            os.fchmod(output.fileno(),0o600)
            try:
                process=subprocess.Popen(args,cwd=cwd,env=env,stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
                try:
                    code_result=process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    # Only our isolated compiler process group is terminated;
                    # CC Switch and other development tasks are never in it.
                    try:os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError:pass
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        try:os.killpg(process.pid,signal.SIGKILL)
                        except ProcessLookupError:pass
                        process.wait()
                    raise MaintenanceError(code)
                if code_result:raise MaintenanceError(code)
            except (OSError,subprocess.SubprocessError):raise MaintenanceError(code) from None

    def tool(self,name,env):
        configured=self.config().get('tool_paths',{}).get(name)
        if isinstance(configured,str) and Path(configured).is_absolute() and os.access(configured,os.X_OK):return configured
        found=shutil.which(name,path=env['PATH'])
        if found:return found
        if name=='pnpm':
            bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/fallback/pnpm'
            if bundled.is_file() and os.access(bundled,os.X_OK):return str(bundled)
        return None

    def build(self,identity):
        from .ccswitch_patch import adapt_source
        root=self.paths();env=self.environment(root)
        if shutil.disk_usage(root).free<12*1024**3:raise MaintenanceError('build_disk_full','waiting_disk')
        pnpm=self.tool('pnpm',env);cargo=self.tool('cargo',env)
        if not pnpm or not cargo:raise MaintenanceError('build_tools_unavailable','waiting_tools')
        self.update('downloading');commit=self.fetcher.release(identity['version'])
        template=Path(self.config().get('template_file',str(TEMPLATE)))
        recipe=digest(template)
        source=root/'sources'/(commit+'-'+recipe[:12])
        source.parent.mkdir(parents=True,exist_ok=True)
        if read_json(source/'.vibecue-source.json',{}).get('commit')!=commit:
            temporary=source.with_name('.'+commit+'-'+uuid.uuid4().hex)
            try:
                self.fetcher.source(commit,temporary)
                write_json(temporary/'.vibecue-source.json',{'commit':commit})
                if source.exists():shutil.rmtree(source)
                temporary.rename(source)
            finally:
                if temporary.exists():shutil.rmtree(temporary)
        try:package=json.loads((source/'package.json').read_text())
        except (OSError,ValueError):raise MaintenanceError('source_package_invalid') from None
        if package.get('version')!=identity['version']:raise MaintenanceError('source_version_mismatch','incompatible')
        try:
            tauri_config=json.loads((source/'src-tauri/tauri.conf.json').read_text())
            minimum=tauri_config.get('bundle',{}).get('macOS',{}).get('minimumSystemVersion')
            if isinstance(minimum,str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+){0,2}',minimum):env['MACOSX_DEPLOYMENT_TARGET']=minimum
        except (OSError,ValueError,TypeError):raise MaintenanceError('source_config_invalid','incompatible') from None
        self.update('adapting',source_commit=commit)
        try:plan=adapt_source(source,module_template=Path(self.config().get('template_file',str(TEMPLATE))))
        except (RuntimeError,ValueError,OSError):raise MaintenanceError('source_interface_changed','incompatible') from None
        self.update('building')
        self.command([pnpm,'install','--frozen-lockfile','--store-dir',str(root/'pnpm-store')],source,env,600,'dependencies_failed')
        self.command([pnpm,'run','build:renderer'],source,env,600,'renderer_failed')
        self.command([cargo,'test','--release','--locked','--manifest-path','src-tauri/Cargo.toml','--features','tauri/custom-protocol','--lib','vibecue_quota'],source,env,1800,'bridge_tests_failed')
        self.command([pnpm,'exec','tauri','build','--bundles','app','--ci','--no-sign','--config',json.dumps({'build':{'beforeBuildCommand':''},'bundle':{'createUpdaterArtifacts':False}}),'--','--locked'],source,env,1800,'bundle_build_failed')
        bundle=root/'target/release/bundle/macos/CC Switch.app'
        self.command(['/usr/bin/codesign','--force','--deep','--sign','-',str(bundle)],source,env,60,'bundle_sign_failed')
        self.verify_signature(bundle)
        built=inspect_app(bundle)
        if (built['version'],built['build'])!=(identity['version'],identity['build']):raise MaintenanceError('built_version_mismatch','incompatible')
        key=hashlib.sha256((commit+digest(Path(self.config().get('template_file',str(TEMPLATE))))+identity['architecture']).encode()).hexdigest()[:24]
        dest=root/'cache'/key;dest.mkdir(parents=True,exist_ok=True,mode=0o700)
        cached=dest/'CC Switch.app'
        if cached.exists():shutil.rmtree(cached)
        shutil.copytree(bundle,cached)
        manifest=dict(built,bridge_protocol=1,source_commit=commit,adapter_manifest=plan,built_at=self.clock(),
                      production_sha256=production_hash(self.config().get('template_file',str(TEMPLATE))),
                      upstream_binary_sha256=identity['binary_sha256'])
        write_json(dest/'manifest.json',manifest)
        return dict(manifest,bundle_path=str(cached))

    def proxy_idle(self):
        # Never read provider/auth values. The SQLite query selects only the
        # local listener address/port. Unknown schemas defer to natural exit.
        import sqlite3
        database=Path.home()/'.cc-switch/cc-switch.db'
        try:
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True,timeout=.2) as connection:
                rows=connection.execute('SELECT listen_address, listen_port FROM proxy_config').fetchall()
            listeners=set()
            for address,port in rows:
                if address not in ('127.0.0.1','localhost','0.0.0.0','::1') or type(port) is not int or not 1<=port<=65535:return False
                hosts=('127.0.0.1','::1') if address=='localhost' else ('::1',) if address=='::1' else ('127.0.0.1',)
                listeners.update((host,port) for host in hosts)
            if not listeners:return False
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            for _ in range(3):
                for host,port in listeners:
                    try:
                        target='['+host+']' if ':' in host else host
                        with opener.open('http://'+target+':'+str(port)+'/status',timeout=1) as response:
                            raw=json.loads(response.read(65537))
                        if not isinstance(raw,dict) or type(raw.get('active_connections')) is not int or raw['active_connections']!=0 or raw.get('running') is not True:return False
                    except OSError:
                        # A stopped listener is only safe when the OS confirms
                        # no process is listening; ambiguity waits for exit.
                        import socket
                        with socket.socket(socket.AF_INET6 if ':' in host else socket.AF_INET) as sock:
                            sock.settimeout(.2)
                            if sock.connect_ex((host,port))==0:return False
                time.sleep(.5)
            return True
        except (OSError,ValueError,sqlite3.Error):return False

    def install(self,identity,cache):
        if self.config().get('enabled') is not True or not (Path.home()/'.cc-switch/vibecue-quota-sharing.enabled').is_file():
            self.update('disabled');return False
        if not same_app(identity,inspect_app(self.app)):raise MaintenanceError('app_changed_during_build','detected')
        self.verify_signature(Path(cache['bundle_path']))
        cached=inspect_app(Path(cache['bundle_path']))
        if (cached['binary_sha256']!=cache['binary_sha256'] or
                (cached['version'],cached['build'])!=(identity['version'],identity['build'])):raise MaintenanceError('cache_changed')
        info=self.native('probe')
        was_running=info.get('is_running') is True
        if was_running and (info.get('is_frontmost') is not False or not self.proxy_idle()):
            self.update('waiting_idle',next_retry_at=self.clock()+60);return False
        # Prepare a fresh inode before requesting a brief normal restart.
        stamp=str(int(self.clock()))+'-'+uuid.uuid4().hex[:8]
        staging=self.app.parent/('.CCSwitch-vibecue-stage-'+stamp+'.app')
        previous=self.app.parent/('.CCSwitch-vibecue-previous-'+stamp+'.app')
        backup=self.home/'backups'/('ccswitch-before-auto-'+identity['version']+'-'+stamp+'.app')
        try:
            shutil.copytree(cache['bundle_path'],staging);self.verify_signature(staging)
            if self.config().get('enabled') is not True or not (Path.home()/'.cc-switch/vibecue-quota-sharing.enabled').is_file():
                self.update('disabled');return False
            if was_running:
                if not self.proxy_idle():
                    self.update('waiting_idle',next_retry_at=self.clock()+60);return False
                stopped=self.native('quit',{'expected_pid':info.get('pid')})
                if stopped.get('status')!='stopped':
                    self.update('waiting_idle',next_retry_at=self.clock()+60);return False
            if not same_app(identity,inspect_app(self.app)):
                if was_running:self.native('relaunch')
                raise MaintenanceError('app_changed_before_swap','detected')
            if self.config().get('enabled') is not True or not (Path.home()/'.cc-switch/vibecue-quota-sharing.enabled').is_file():
                if was_running:self.native('relaunch')
                self.update('disabled');return False
            if self.native('probe').get('is_running') is not False:
                self.update('waiting_idle',next_retry_at=self.clock()+60);return False
            self.update('installing')
            swapped_at=self.clock()
            self.app.rename(previous)
            if not same_app(identity,inspect_app(previous)):
                # Compare the actual inode moved aside, closing the update race
                # between the last check and rename. Never install an old cache
                # over an update that arrived in that interval.
                if not self.app.exists():previous.rename(self.app)
                if was_running:self.native('relaunch')
                raise MaintenanceError('app_changed_at_swap','detected')
            try:staging.rename(self.app)
            except OSError:
                if not self.app.exists():previous.rename(self.app)
                else:
                    backup.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                    shutil.move(str(previous),str(backup))
                if was_running:self.native('relaunch')
                raise
            backup.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            shutil.move(str(previous),str(backup))
            self.update('verifying',backup_path=str(backup),replacement_sha256=cache['binary_sha256'])
            if was_running:
                launched=self.native('relaunch')
                if launched.get('status') not in ('running','started') or not self.confirm_export(swapped_at,cache['binary_sha256']):
                    # A launched app might still own the singleton/credentials.
                    # Normal exit must finish before restoring the upstream app.
                    self.update('waiting_retry',pending_rollback=str(backup),rollback_sha256=cache['binary_sha256'])
                    if not self.rollback_pending():return False
                    raise MaintenanceError('restart_failed_rolled_back')
            self.update('healthy',healthy_binary_sha256=cache['binary_sha256'],healthy_production_sha256=cache.get('production_sha256'),
                        installed_version=identity['version'],healthy_build=identity['build'],next_retry_at=0,error_code=None)
            return True
        finally:
            if staging.exists():shutil.rmtree(staging)

    def repair(self):
        self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
        with (self.home/'ccswitch-maintenance.lock').open('a') as lock:
            # The parent ensure() can still own this lock for a few milliseconds
            # after spawning us. Wait briefly rather than losing that repair.
            for attempt in range(10):
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if attempt==9:return self.status()
                    time.sleep(.1)
            if self.config().get('enabled') is not True:return self.status()
            if not (Path.home()/'.cc-switch/vibecue-quota-sharing.enabled').is_file():
                self.update('disabled');return self.status()
            identity=None
            try:
                if self.state().get('pending_rollback'):
                    self.rollback_pending();return self.status()
                identity=inspect_app(self.app)
                if self.matches_healthy(identity):return self.status()
                self.update('preparing',target_version=identity['version'],target_sha256=identity['binary_sha256'])
                if not self.recognized(identity):raise MaintenanceError('installed_app_not_recognized','incompatible')
                cache=self.cache_manifest(identity) or self.build(identity)
                self.install(identity,cache)
            except MaintenanceError as error:
                delay=60 if error.phase=='detected' else RETRY_SECONDS
                self.update(error.phase,error_code=error.code,next_retry_at=self.clock()+delay)
            except Exception:self.update('waiting_retry',error_code='maintenance_incomplete',next_retry_at=self.clock()+RETRY_SECONDS)
            return self.status()

    def rollback_pending(self):
        state=self.state();backup=Path(state.get('pending_rollback',''))
        if not backup.is_absolute() or not backup.is_dir():raise MaintenanceError('rollback_backup_unavailable')
        identity=inspect_app(self.app)
        if identity['binary_sha256']!=state.get('rollback_sha256'):
            # Never overwrite a newer official update installed in the meantime.
            self.update('detected',pending_rollback=None,rollback_sha256=None,next_retry_at=0)
            return False
        info=self.native('probe');running=info.get('is_running') is True
        if running:
            if info.get('is_frontmost') is not False or not self.proxy_idle():
                self.update('waiting_idle',next_retry_at=self.clock()+60);return False
        original=inspect_app(backup)
        if (original['version'],original['build'])!=(identity['version'],identity['build']):raise MaintenanceError('rollback_version_changed')
        self.verify_signature(backup)
        failed=self.app.parent/('.CCSwitch-vibecue-failed-'+uuid.uuid4().hex+'.app')
        staging=self.app.parent/('.CCSwitch-vibecue-rollback-'+uuid.uuid4().hex+'.app')
        try:
            shutil.copytree(backup,staging);self.verify_signature(staging)
            info=self.native('probe')
            if info.get('is_running') is True:
                if info.get('is_frontmost') is not False or not self.proxy_idle():
                    self.update('waiting_idle',next_retry_at=self.clock()+60);return False
                if self.native('quit',{'expected_pid':info.get('pid')}).get('status')!='stopped':
                    self.update('waiting_idle',next_retry_at=self.clock()+60);return False
            if self.native('probe').get('is_running') is not False:
                self.update('waiting_idle',next_retry_at=self.clock()+60);return False
            self.app.rename(failed)
            if inspect_app(failed)['binary_sha256']!=state.get('rollback_sha256'):
                if not self.app.exists():failed.rename(self.app)
                self.update('detected',pending_rollback=None,rollback_sha256=None,next_retry_at=0)
                self.native('relaunch');return False
            try:staging.rename(self.app)
            except OSError:
                if not self.app.exists():failed.rename(self.app)
                self.native('relaunch');raise
        finally:
            if staging.exists():shutil.rmtree(staging)
        self.native('relaunch')
        self.update('waiting_retry',pending_rollback=None,rollback_sha256=None,
                    target_sha256=original['binary_sha256'],next_retry_at=self.clock()+RETRY_SECONDS,
                    error_code='restart_failed_rolled_back')
        return True

    def confirm_export(self,after,binary_hash):
        deadline=time.monotonic()+90
        export=Path.home()/'.cc-switch/vibecue-codex-quota.json'
        while time.monotonic()<deadline:
            value=read_json(export,{})
            if (value.get('source')=='ccswitch_quota_bridge' and value.get('schema_version')==1 and
                    type(value.get('generated_at')) in (int,float) and value['generated_at']>=after):
                return inspect_app(self.app)['binary_sha256']==binary_hash
            time.sleep(1)
        return False


def configure_existing(home,work_root,*,seed_app=None,template_file=TEMPLATE):
    """Migrate a previously verified local bridge without querying any auth."""
    instance=Maintenance(home)
    record=read_json(instance.home/'ccswitch-quota-install.json',{})
    previous=instance.config()
    if not record or previous.get('enabled') is False:return {'configured':False}
    marker=Path.home()/'.cc-switch/vibecue-quota-sharing.enabled'
    if not marker.is_file():return {'configured':False}
    config=dict(previous,enabled=True,work_root=previous.get('work_root') or str(Path(work_root).resolve()),
                template_file=str(template_file))
    config['tool_paths']={name:value for name in ('cargo','pnpm','node') if (value:=shutil.which(name)) is not None}
    write_json(instance.config_file,config)
    if instance.state_file.exists():return {'configured':True,'seeded':False}
    if record.get('patch_sha256')!=INITIAL_PATCH:return {'configured':True,'seeded':False}
    candidates=[APP]+([Path(seed_app)] if seed_app else [])
    for candidate in candidates:
        try:
            identity=inspect_app(candidate)
            if identity['binary_sha256']!=record.get('binary_sha256'):continue
            instance.verify_signature(candidate)
            original=inspect_app(Path(record['backup']))
            key=hashlib.sha256((identity['version']+identity['architecture']+identity['binary_sha256']).encode()).hexdigest()[:24]
            directory=instance.paths()/'cache'/key;directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            bundle=directory/'CC Switch.app'
            if not bundle.exists():shutil.copytree(candidate,bundle)
            instance.verify_signature(bundle)
            manifest=dict(identity,bridge_protocol=1,source_commit=record.get('official_commit'),
                          production_sha256=INITIAL_PRODUCTION,upstream_binary_sha256=original['binary_sha256'],
                          seeded_from_verified_install=True,built_at=instance.clock())
            write_json(directory/'manifest.json',manifest)
            current=inspect_app(APP)
            if current['binary_sha256']==identity['binary_sha256']:
                instance.update('healthy',healthy_binary_sha256=identity['binary_sha256'],healthy_production_sha256=INITIAL_PRODUCTION,
                                installed_version=identity['version'],healthy_build=identity['build'],next_retry_at=0,error_code=None)
            return {'configured':True,'seeded':True}
        except (MaintenanceError,OSError,KeyError):continue
    return {'configured':True,'seeded':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description='CC Switch 本机额度共享的自动升级维护')
    parser.add_argument('action',choices=('status','ensure','repair'),default='status',nargs='?')
    args=parser.parse_args(argv);os.umask(0o077)
    if args.action=='repair':
        try:os.nice(10)
        except OSError:pass
    instance=Maintenance();result=getattr(instance,args.action)()
    print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    return 0


if __name__=='__main__':raise SystemExit(main())
