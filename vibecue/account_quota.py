"""Read subscription quota through the official Codex CLI, without owning auth.

The desktop's owner/follower socket exposes no account RPCs. This bounded
helper therefore starts the official bundled app-server, sends only account
reads, and exits. Codex keeps ownership of its existing login; this module never
opens an auth file, asks for a token, logs in, switches accounts or starts a turn.
Only whitelisted identity and numeric quota snapshots enter the private cache.
"""
from __future__ import annotations

from contextlib import contextmanager
import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import plistlib
import re
import select
import signal
import subprocess
import sys
import tempfile
import time

from .codex_launch import find_codex_app
from .events import vibecue_directory


SCHEMA_VERSION = 1
REFRESH_SECONDS = 60
FRESH_SECONDS = 120
DEADLINE_SECONDS = 20
MAX_FRAME = 262_144
MAX_CACHE = 1_048_576
MAX_ACCOUNTS = 20
SOURCE = 'codex_app_server'
DOCS_URL = 'https://learn.chatgpt.com/docs/app-server'
ALLOWED = frozenset(('initialize', 'account/read', 'account/rateLimits/read'))


class QuotaError(RuntimeError):
    """Controlled diagnostics only; raw server errors never reach the UI."""


def label(value, limit=180):
    if not isinstance(value, str):
        return ''
    return ''.join(character for character in value if ord(character) >= 32 and ord(character) != 127).strip()[:limit]


def number(value, minimum=0, maximum=253_402_300_799):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        return None
    return value


def quota_window(raw, now):
    if not isinstance(raw, dict):
        return None
    used = number(raw.get('usedPercent'), maximum=100_000)
    if used is None:
        return None  # Missing and invalid percentages are never zero usage.
    minutes = number(raw.get('windowDurationMins'), minimum=1, maximum=5_256_000)
    if minutes is not None and int(minutes) != minutes:
        minutes = None
    reset = number(raw.get('resetsAt'), minimum=1)
    if reset is not None and int(reset) != reset:
        reset = None
    # The service can report now+window as a placeholder before first usage.
    # CC Switch uses the same rule; do not present a moving fake countdown.
    reset_status = 'not_started' if used == 0 else 'scheduled' if reset is not None and reset > now else 'elapsed' if reset is not None else 'unknown'
    if used == 0:
        reset = None
    return {'kind': 'weekly' if minutes == 10080 else 'short' if minutes is not None and minutes < 10080 else 'other',
            'used_percent': used, 'remaining_percent': max(0, min(100, 100-used)),
            'window_minutes': int(minutes) if minutes is not None else None,
            'resets_at': int(reset) if reset is not None else None,
            'reset_status': reset_status}


def quota_buckets(raw, now):
    if not isinstance(raw, dict):
        raise QuotaError('Codex 没有返回可读取的订阅额度。')
    by_id = raw.get('rateLimitsByLimitId')
    if isinstance(by_id, dict) and by_id:
        candidates = [(label(key, 100), value) for key, value in list(by_id.items())[:32]]
    else:
        legacy = raw.get('rateLimits')
        candidates = [(label(legacy.get('limitId'), 100) or 'codex', legacy)] if isinstance(legacy, dict) else []
    result = []
    for key, value in candidates:
        if not key or not isinstance(value, dict):
            continue
        windows = []
        for field in ('primary', 'secondary'):
            window = quota_window(value.get(field), now)
            if window is not None:
                window['slot'] = field
                windows.append(window)
        if windows:
            result.append({'limit_id': label(value.get('limitId'), 100) or key,
                           'limit_name': label(value.get('limitName'), 120),
                           'plan_type': label(value.get('planType'), 60), 'windows': windows})
    if not result:
        raise QuotaError('当前登录方式或套餐没有提供订阅额度窗口；不能按 Token 用量估算。')
    return sorted(result, key=lambda value: (value['limit_id'] != 'codex', value['limit_id']))


def account_identity(raw):
    account = raw.get('account') if isinstance(raw, dict) else None
    if not isinstance(account, dict) or account.get('type') != 'chatgpt':
        raise QuotaError('当前 Codex 没有可读取订阅额度的 ChatGPT 登录，请先在 Codex 中确认账号。')
    email = label(account.get('email'), 254)
    if email and not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+', email):
        email = ''
    routing = raw.get('workspaceRouting')
    routing = routing if isinstance(routing, dict) else {}
    identifier = label(account.get('accountId') or routing.get('chatgptAccountId'), 180)
    return {'type':'chatgpt', 'email':email, 'plan_type':label(account.get('planType'), 60),
            'account_id':identifier or None}


def build_snapshot(before, limits, after, now):
    first, last = account_identity(before), account_identity(after)
    if first != last:
        raise QuotaError('读取期间 Codex 账号发生变化，本次额度未归入任何账号，请重试。')
    identifier = label(limits.get('accountId'), 180) if isinstance(limits, dict) else ''
    if first['account_id'] and identifier and first['account_id'] != identifier:
        raise QuotaError('Codex 返回的额度账号与当前账号不一致，本次结果未保存。')
    identifier = identifier or first['account_id']
    if identifier:
        key, identity_kind = 'account:' + identifier, 'account_id'
    elif first['email']:
        # Distinct workspaces using one email cannot be disambiguated without
        # the backend account ID. Never label this key as a confirmed ID.
        key = 'email:' + hashlib.sha256(first['email'].lower().encode()).hexdigest()[:24]
        identity_kind = 'email'
    else:
        raise QuotaError('Codex 没有提供可区分账号的标识，本次额度不会混入已保存的其他账号。')
    return {'account_key':key, 'account_id':identifier or None,
            'identity_kind':identity_kind, 'email':first['email'],
            'label':first['email'] or ('Codex 账号 · ' + identifier[-8:]),
            'plan_type':first['plan_type'], 'source':SOURCE,
            'observed_at':now, 'expires_at':now+FRESH_SECONDS,
            'buckets':quota_buckets(limits, now)}


class CodexQuotaProcess:
    """One 20-second official stdio process with a read-only RPC allowlist."""
    def __init__(self, timeout=DEADLINE_SECONDS, executable=None, process_factory=subprocess.Popen):
        self.timeout = min(DEADLINE_SECONDS, max(1, float(timeout)))
        self.executable = executable
        self.process_factory = process_factory
        self.process = None
        self.buffer = bytearray()
        self.counter = 0
        self.deadline = None

    def __enter__(self):
        if self.executable is None:
            app, _, executable = find_codex_app()
            info = plistlib.loads((app/'Contents/Info.plist').read_bytes())
            if info.get('CFBundleIdentifier') != 'com.openai.codex':
                raise QuotaError('无法确认官方 Codex 程序，未读取额度。')
        else:
            executable = Path(self.executable)
        environment = {key:value for key,value in os.environ.items() if key in
                       ('HOME','USER','LOGNAME','LANG','LC_ALL','LC_CTYPE','TZ','TMPDIR','CODEX_HOME')}
        environment['PATH'] = '/usr/bin:/bin:/usr/sbin:/sbin'
        self.deadline = time.monotonic()+self.timeout
        try:
            self.process = self.process_factory([str(executable), 'app-server', '--stdio'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=environment, start_new_session=True)
            self.call('initialize', {'clientInfo':{'name':'VibeCue','title':'VibeCue quota reader','version':'1'},
                                     'capabilities':{'experimentalApi':False}})
            self._write({'method':'initialized'})
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=.25)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate(); process.wait(timeout=.5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill(); process.wait(timeout=.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            if process.stdout:
                process.stdout.close()
            self.buffer.clear()

    def _write(self, value):
        data = json.dumps(value, ensure_ascii=False, separators=(',',':')).encode()+b'\n'
        if len(data)>4096 or self.process is None:
            raise QuotaError('Codex 额度读取连接尚未就绪。')
        fd = self.process.stdin.fileno()
        os.set_blocking(fd, False)
        offset = 0
        while offset<len(data):
            remaining=self.deadline-time.monotonic()
            if remaining<=0 or not select.select([], [fd], [], max(0,remaining))[1]:
                raise QuotaError('Codex 额度读取超时，请稍后重试。')
            try:
                count=os.write(fd,data[offset:])
            except BlockingIOError:
                continue
            if count<=0:
                raise QuotaError('Codex 额度读取连接已关闭。')
            offset+=count

    def _read(self):
        while True:
            index=self.buffer.find(b'\n')
            if index>=0:
                value=bytes(self.buffer[:index]);del self.buffer[:index+1]
                try:
                    result=json.loads(value)
                except (ValueError,UnicodeError,RecursionError):
                    raise QuotaError('Codex 返回的额度响应无法识别。') from None
                if not isinstance(result,dict):
                    raise QuotaError('Codex 返回的额度响应无法识别。')
                return result
            if len(self.buffer)>=MAX_FRAME:
                raise QuotaError('Codex 额度响应过大，未保存。')
            remaining=self.deadline-time.monotonic()
            if remaining<=0 or not select.select([self.process.stdout], [], [], max(0,remaining))[0]:
                raise QuotaError('Codex 额度读取超时，请稍后重试。')
            chunk=os.read(self.process.stdout.fileno(),min(65536,MAX_FRAME-len(self.buffer)))
            if not chunk:
                raise QuotaError('Codex 额度读取连接已关闭，请在 Codex 中确认登录状态。')
            self.buffer.extend(chunk)

    def call(self, method, params):
        if method not in ALLOWED or (method=='account/read' and params!={'refreshToken':False}) or (method=='account/rateLimits/read' and params!={}):
            raise QuotaError('额度读取仅允许账号与额度查询。')
        self.counter+=1
        identifier=self.counter
        self._write({'id':identifier,'method':method,'params':params})
        while True:
            value=self._read()
            if type(value.get('id')) is int and value['id']==identifier and 'method' not in value:
                if value.get('error') is not None:
                    raise QuotaError('Codex 未能返回订阅额度，请在 Codex 中确认登录状态后重试。')
                result=value.get('result')
                if not isinstance(result,dict):
                    raise QuotaError('Codex 返回的额度响应无法识别。')
                return result
            if value.get('method') and 'id' in value:
                # Never supply authentication, approve tools or answer any
                # unexpected server request received during a quota read.
                self._write({'id':value['id'],'error':{'code':-32601,'message':'This quota reader supports no interactive methods'}})
                raise QuotaError('Codex 需要交互处理，本次额度查询已停止；请在 Codex 中处理。')


def read_current(*, client_factory=CodexQuotaProcess, now=None):
    with client_factory() as client:
        before=client.call('account/read',{'refreshToken':False})
        account_identity(before)  # Avoid a network quota call for API-key-only auth.
        limits=client.call('account/rateLimits/read',{})
        after=client.call('account/read',{'refreshToken':False})
        return build_snapshot(before,limits,after,time.time() if now is None else now)


class QuotaStore:
    def __init__(self, home=None, clock=time.time):
        self.home=vibecue_directory(home)
        self.file=self.home/'account-quota.json'
        self.clock=clock

    def load(self):
        try:
            with self.file.open('rb') as stream:
                raw=stream.read(MAX_CACHE+1)
            if len(raw)>MAX_CACHE:
                return {}
            value=json.loads(raw)
            if not isinstance(value,dict) or value.get('schema_version')!=SCHEMA_VERSION:
                return {}
            rows=value.get('accounts')
            if not isinstance(rows,list) or len(rows)>MAX_ACCOUNTS:
                return {}
            # Only cache produced by this module is accepted. Rebuild every
            # record through the same field-level whitelist before exposing it.
            clean=[]
            for row in rows:
                if not isinstance(row,dict) or row.get('source')!=SOURCE:
                    continue
                observed=number(row.get('observed_at'),minimum=1)
                if observed is None or observed>self.clock()+300:
                    continue
                identity={'account':{'type':'chatgpt','email':row.get('email'),'planType':row.get('plan_type'),
                                     'accountId':row.get('account_id')}}
                buckets={}
                for bucket in row.get('buckets',[])[:32] if isinstance(row.get('buckets'),list) else []:
                    if not isinstance(bucket,dict):
                        continue
                    raw_bucket={'limitId':bucket.get('limit_id'),'limitName':bucket.get('limit_name'),'planType':bucket.get('plan_type')}
                    for index,window in enumerate(bucket.get('windows',[])[:2] if isinstance(bucket.get('windows'),list) else []):
                        if isinstance(window,dict):
                            raw_bucket['primary' if index==0 else 'secondary']={'usedPercent':window.get('used_percent'),'windowDurationMins':window.get('window_minutes'),'resetsAt':window.get('resets_at')}
                    buckets[label(bucket.get('limit_id'),100)]=raw_bucket
                try:
                    clean.append(build_snapshot(identity,{'accountId':row.get('account_id'),'rateLimitsByLimitId':buckets},identity,observed))
                except QuotaError:
                    continue
            return {'schema_version':SCHEMA_VERSION,'accounts':clean,
                    'current_account_key':label(value.get('current_account_key'),220) or None,
                    'refreshed_at':number(value.get('refreshed_at')),
                    'message':label(value.get('message'),300)}
        except (OSError,ValueError,TypeError,RecursionError):
            return {}

    def save(self, value):
        self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
        data=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode()
        if len(data)>MAX_CACHE:
            raise QuotaError('账号额度快照过大，未保存。')
        fd,name=tempfile.mkstemp(prefix='.account-quota-',dir=self.home)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'wb') as stream:
                stream.write(data)
            os.replace(name,self.file)
        finally:
            try:os.unlink(name)
            except FileNotFoundError:pass

    def status(self, *, refreshing=False):
        value=self.load();now=self.clock()
        current=value.get('current_account_key')
        rows=[]
        for row in sorted(value.get('accounts',[]),key=lambda row:(row['account_key']!=current,-row['observed_at'])):
            row=dict(row);is_current=row['account_key']==current
            elapsed_reset=any(window.get('reset_status')!='not_started' and window.get('resets_at') is not None and window['resets_at']<=now
                              for bucket in row['buckets'] for window in bucket['windows'])
            fresh=is_current and row['expires_at']>=now and not elapsed_reset
            row.update(current_account=is_current,status='current' if fresh else 'stale' if is_current else 'last_read')
            rows.append(row)
        available=any(row['status']=='current' for row in rows)
        return {'schema_version':SCHEMA_VERSION,'source':SOURCE,'available':available,
                'status':'refreshing' if refreshing else 'current' if available else 'stale' if rows else 'unavailable',
                'current_account_key':current,'accounts':rows,'account_count':len(rows),
                'refreshed_at':value.get('refreshed_at'),'refresh_interval_seconds':REFRESH_SECONDS,
                'multi_account_live':False,'message':value.get('message') or '点击刷新，读取 Codex 当前登录账号的订阅额度；不会切换账号或发起新授权。',
                'scope_note':'仅当前 Codex 账号可刷新；其他账号保留上次读取的快照，不代表实时余额。',
                'docs_url':DOCS_URL}

    def refresh(self, *, force=False, reader=read_current):
        self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
        descriptor=os.open(self.home/'account-quota.lock',os.O_RDWR|os.O_CREAT,0o600)
        with os.fdopen(descriptor,'w') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                return self.status(refreshing=True)
            previous=self.load();now=self.clock()
            refreshed=previous.get('refreshed_at')
            if not force and refreshed is not None and 0<=now-refreshed<REFRESH_SECONDS:
                return self.status()
            try:
                current=reader()
                # Reader is trusted protocol code; use persisted whitelist on
                # the following status() before anything reaches the UI.
                rows=[row for row in previous.get('accounts',[]) if row['account_key']!=current['account_key']]
                rows.append(current);rows=sorted(rows,key=lambda row:row['observed_at'],reverse=True)[:MAX_ACCOUNTS]
                value={'schema_version':SCHEMA_VERSION,'accounts':rows,'current_account_key':current['account_key'],
                       'refreshed_at':now,'message':'已通过 Codex 读取当前账号的订阅额度。其他账号显示各自上次读取时间。'}
            except (RuntimeError,OSError,ValueError,TypeError,subprocess.SubprocessError) as error:
                message=str(error) if isinstance(error,QuotaError) else 'Codex 额度读取未完成，请稍后重试。'
                value={'schema_version':SCHEMA_VERSION,'accounts':previous.get('accounts',[]),
                       'current_account_key':None,'refreshed_at':now,'message':message}
            try:
                self.save(value)
            except (QuotaError,OSError):
                result=self.status()
                result.update(available=False,status='unavailable',current_account_key=None,
                              message='本机额度快照未能保存，请检查 VibeCue 数据目录后重试。')
                for row in result['accounts']:
                    row.update(current_account=False,status='last_read')
                return result
            return self.status()


def main(argv=None):
    parser=argparse.ArgumentParser(description='通过官方 Codex 只读查询当前账号订阅额度')
    parser.add_argument('command',choices=('status','refresh'),nargs='?',default='status')
    parser.add_argument('--force',action='store_true')
    args=parser.parse_args(argv)
    os.umask(0o077)
    def cancelled(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,cancelled)
    try:
        store=QuotaStore()
        result=store.refresh(force=args.force) if args.command=='refresh' else store.status()
        print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
        return 0
    except KeyboardInterrupt:
        return 130


if __name__=='__main__':
    raise SystemExit(main())
