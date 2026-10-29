"""Read CC Switch's opt-in, credential-free Codex quota export.

CC Switch owns all OAuth refreshes and queries. This reader never opens its
authorization store, invokes login, handles tokens or changes the active account.
The small upstream integration patch lives in integrations/ccswitch/.
"""
from __future__ import annotations

from datetime import datetime
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

SOURCE = 'ccswitch_quota_bridge'
FRESH_SECONDS = 120
MAX_BYTES = 1_048_576
MAX_ACCOUNTS = 20
EXPORT_NAME = 'vibecue-codex-quota.json'
ENABLE_NAME = 'vibecue-quota-sharing.enabled'
HISTORY_NAME = 'ccswitch-quota-history.json'
HISTORY_SOURCE = 'ccswitch_quota_history'
_ID = re.compile(r'^[A-Za-z0-9_-]{1,180}$')
_EMAIL = re.compile(r'[^\s@<>]+@[^\s@<>]+')
ERRORS = {
    'query_failed': 'CC Switch 暂时无法读取该账号额度，将自动重试。',
    'authorization_unavailable': 'CC Switch 暂时无法使用该账号现有授权，请在 CC Switch 中查看状态。',
    'query_timeout': 'CC Switch 额度读取超时，将自动重试。',
    'identity_unavailable': 'CC Switch 未提供可归属的账号标识。',
    'quota_unavailable': 'CC Switch 未返回可用的订阅额度窗口。',
}


def export_path():
    """No app configuration or authorization file needs to be inspected."""
    override = os.environ.get('VIBECUE_CC_SWITCH_QUOTA_FILE')
    return Path(override).expanduser() if override else Path.home()/'.cc-switch'/EXPORT_NAME


def _number(value, maximum=253_402_300_799):
    return value if type(value) in (int, float) and 0 <= value <= maximum and math.isfinite(value) else None


def _text(value, maximum=254):
    return value if isinstance(value, str) and len(value) <= maximum and all(ord(c) >= 32 and ord(c) != 127 for c in value) else ''


def _identity(raw):
    identifier = _text(raw.get('account_id'), 180)
    local = _text(raw.get('ccswitch_account_id'), 180)
    if not _ID.fullmatch(identifier):
        identifier = ''
    if not _ID.fullmatch(local):
        local = ''
    if not identifier and not local:
        return None
    email = _text(raw.get('email'))
    if not _EMAIL.fullmatch(email):
        email = ''
    # A workspace can contain multiple independently authorized users. The
    # manager's stable local ID identifies the login, while account_id is only
    # the upstream workspace routing ID and must never collapse those users.
    key = 'ccswitch:'+local if local else 'account:'+identifier
    return {'account_key':key, 'account_id':identifier or None,
            'identity_kind':'ccswitch_account_id' if local else 'account_id',
            'ccswitch_account_id':local or None, 'email':email,
            'label':email or 'Codex 账号 · '+(identifier or local)[-8:],
            'plan_type':'', 'source':SOURCE}


def _minutes(name):
    if name == 'five_hour':return 300
    if name == 'seven_day':return 10080
    match = re.fullmatch(r'([1-9][0-9]{0,4})_(hour|day)',name or '')
    if match:
        value=int(match.group(1))*(60 if match.group(2)=='hour' else 1440)
        return value if value <= 5_256_000 else None
    return None


def _window(raw, now):
    if not isinstance(raw,dict):return None
    used=_number(raw.get('utilization'),100_000)
    if used is None:return None
    minutes=_minutes(_text(raw.get('name'),60))
    reset=None
    value=_text(raw.get('resets_at'),60)
    if value:
        try:
            date=datetime.fromisoformat(value.replace('Z','+00:00'))
            if date.tzinfo is not None:reset=_number(date.timestamp())
        except (ValueError,OverflowError):pass
    state='not_started' if used==0 else 'scheduled' if reset is not None and reset>now else 'elapsed' if reset is not None else 'unknown'
    return {'kind':'weekly' if minutes==10080 else 'short' if minutes is not None and minutes<10080 else 'other',
            'used_percent':used,'remaining_percent':max(0,min(100,100-used)),
            'window_minutes':minutes,'resets_at':int(reset) if reset is not None and used!=0 else None,
            'reset_status':state}


def _read_export(path, now):
    result={'accounts':[],'history':[],'identities':[],'errors':[],'configured_count':0,
            'bridge_status':'unavailable','source':SOURCE,
            'configuration_complete':False,'configuration_observed_at':None}
    try:
        # Verify the opened inode, not an earlier pathname check. Refuse links
        # even if the target would otherwise be private and parseable.
        descriptor=os.open(path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(descriptor,'rb') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or
                    info.st_mode & 0o077 or info.st_size>MAX_BYTES):
                result['bridge_status']='invalid';return result
            data=stream.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES:result['bridge_status']='invalid';return result
        raw=json.loads(data)
    except FileNotFoundError:return result
    except (OSError,ValueError,RecursionError):result['bridge_status']='invalid';return result
    if not isinstance(raw,dict):result['bridge_status']='invalid';return result
    generated=_number(raw.get('generated_at'))
    rows=raw.get('accounts')
    if (type(raw.get('schema_version')) is not int or raw.get('schema_version')!=1 or raw.get('source')!=SOURCE or generated is None or
            generated>now+30 or not isinstance(rows,list) or len(rows)>MAX_ACCOUNTS):
        result['bridge_status']='invalid';return result
    fresh=now-generated<=FRESH_SECONDS
    result['bridge_status']='ready' if fresh else 'stale'
    result['configuration_observed_at']=generated
    seen=set()
    for row in rows:
        if not isinstance(row,dict):continue
        identity=_identity(row)
        if identity is None or identity['account_key'] in seen:continue
        seen.add(identity['account_key']);result['identities'].append(identity)
        error=None
        observed=_number(row.get('observed_at'))
        if not fresh or observed is None or observed>now+30 or now-observed>FRESH_SECONDS:
            error='CC Switch 额度快照已过期，请确认 CC Switch 正在运行。'
        elif row.get('success') is not True:
            error=ERRORS.get(row.get('error_code'),ERRORS['query_failed'])
        elif not identity['account_id']:
            error=ERRORS['identity_unavailable']
        tiers=row.get('tiers')
        windows=[value for tier in tiers[:32] if (value:=_window(tier,now)) is not None] if isinstance(tiers,list) else []
        # Retain a successful old reading after the exporter stops, but never
        # let its existence bypass either the per-account or file freshness.
        if (error is not None and row.get('success') is True and identity['account_id'] and
                observed is not None and observed<=now and windows):
            result['history'].append(dict(identity,observed_at=observed,expires_at=observed+FRESH_SECONDS,
                                          is_live=False,status='last_read',
                                          buckets=[{'limit_id':'codex','limit_name':'','plan_type':'','windows':windows}]))
        if error is None and not windows:error=ERRORS['quota_unavailable']
        if error is not None:
            result['errors'].append({'account_key':identity['account_key'],'label':identity['label'],'message':error})
            continue
        result['accounts'].append(dict(identity,observed_at=observed,expires_at=observed+FRESH_SECONDS,
                                      buckets=[{'limit_id':'codex','limit_name':'','plan_type':'','windows':windows}]))
    result['configured_count']=len(result['identities'])
    result['configuration_complete']=fresh and len(result['identities'])==len(rows)
    return result


def _private_document(path):
    descriptor=os.open(path,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(descriptor,'rb') as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or
                info.st_mode & 0o077 or info.st_size>MAX_BYTES):
            return None
        data=stream.read(MAX_BYTES+1)
    return json.loads(data) if len(data)<=MAX_BYTES else None


def _historical_window(raw, now):
    if not isinstance(raw,dict):return None
    used=_number(raw.get('used_percent'),100_000)
    if used is None:return None
    minutes=_number(raw.get('window_minutes'),5_256_000)
    if minutes is not None and (minutes<1 or int(minutes)!=minutes):minutes=None
    reset=_number(raw.get('resets_at'))
    if reset is not None and (reset<1 or int(reset)!=reset):reset=None
    state='not_started' if used==0 else 'scheduled' if reset is not None and reset>now else 'elapsed' if reset is not None else 'unknown'
    return {'kind':'weekly' if minutes==10080 else 'short' if minutes is not None and minutes<10080 else 'other',
            'used_percent':used,'remaining_percent':max(0,min(100,100-used)),
            'window_minutes':int(minutes) if minutes is not None else None,
            'resets_at':int(reset) if reset is not None and used!=0 else None,'reset_status':state}


def _clean_snapshot(raw, now):
    """Rebuild cache rows through a whitelist; persisted flags are not trusted."""
    if not isinstance(raw,dict):return None
    identity=_identity(raw)
    observed=_number(raw.get('observed_at'))
    if identity is None or not identity['account_id'] or observed is None or observed<=0 or observed>now+30:
        return None
    buckets=raw.get('buckets')
    if not isinstance(buckets,list):return None
    windows=[]
    for bucket in buckets[:32]:
        if not isinstance(bucket,dict) or bucket.get('limit_id')!='codex':continue
        values=bucket.get('windows')
        if isinstance(values,list):
            windows.extend(value for window in values[:32] if (value:=_historical_window(window,now)) is not None)
    if not windows:return None
    return dict(identity,observed_at=observed,expires_at=observed+FRESH_SECONDS,
                buckets=[{'limit_id':'codex','limit_name':'','plan_type':'','windows':windows[:32]}])


def _load_history(path, now):
    empty={'schema_version':1,'source':HISTORY_SOURCE,'configuration_observed_at':0,'identities':[],'accounts':[]}
    try:
        raw=_private_document(path)
        if (not isinstance(raw,dict) or type(raw.get('schema_version')) is not int or raw.get('schema_version')!=1 or
                raw.get('source')!=HISTORY_SOURCE):return empty
        configured=_number(raw.get('configuration_observed_at'))
        if configured is None or configured>now+30:return empty
        identities=raw.get('identities');accounts=raw.get('accounts')
        if not isinstance(identities,list) or len(identities)>MAX_ACCOUNTS or not isinstance(accounts,list) or len(accounts)>MAX_ACCOUNTS:
            return empty
        clean_identities={value['account_key']:value for row in identities
                          if isinstance(row,dict) and (value:=_identity(row)) is not None}
        clean_accounts={value['account_key']:value for row in accounts if (value:=_clean_snapshot(row,now)) is not None}
        # Configured identities are authoritative even when the numeric cache
        # contains an older row left by an interrupted/previous writer.
        clean_accounts={key:value for key,value in clean_accounts.items()
                        if key in clean_identities and _same_identity(value,clean_identities[key])}
        return dict(empty,configuration_observed_at=configured,
                    identities=list(clean_identities.values()),accounts=list(clean_accounts.values()))
    except (OSError,ValueError,RecursionError):return empty


def _same_identity(first, second):
    return (first.get('ccswitch_account_id'),first.get('account_id'),first.get('email','').casefold())==(
        second.get('ccswitch_account_id'),second.get('account_id'),second.get('email','').casefold())


def _save_history(path, document):
    data=json.dumps(document,ensure_ascii=False,separators=(',',':')).encode()
    if len(data)>MAX_BYTES:raise ValueError('Numeric quota history exceeds its private bound')
    descriptor,temporary=tempfile.mkstemp(prefix='.ccswitch-quota-history-',dir=path.parent)
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        try:os.unlink(temporary)
        except FileNotFoundError:pass


def _with_history(result, home, now):
    home=Path(home).expanduser()
    path=home/HISTORY_NAME
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(home/'ccswitch-quota-history.lock',os.O_RDWR|os.O_CREAT | getattr(os,'O_NOFOLLOW',0),0o600)
    with os.fdopen(descriptor,'w') as lock:
        # No network operation runs while locked. Serializing the small local
        # read/replace prevents a parallel older reader restoring a deletion.
        fcntl.flock(lock,fcntl.LOCK_EX)
        previous=_load_history(path,now)
        merged,document=_merge_history(result,previous,now)
        if document!=previous:
            try:_save_history(path,document)
            except (OSError,ValueError):merged['history_cache_error']='本机历史额度暂时无法保存，已保存的记录仍可查看。'
        return merged


def _merge_history(result, previous, now):
    result=dict(result)
    result['accounts']=[dict(row) for row in result['accounts']]
    result['errors']=[dict(row) for row in result['errors']]
    identities={row['account_key']:row for row in previous['identities']}
    saved={row['account_key']:row for row in previous['accounts']}
    generated=result.get('configuration_observed_at')
    watermark=previous['configuration_observed_at']
    # A delayed/replaced file must not resurrect a user removed in a newer
    # complete configuration, or prune users using an older account list.
    regressed=generated is not None and generated<watermark
    if regressed:
        result.update(accounts=[],history=[],identities=[],errors=[],configuration_complete=False,bridge_status='stale')
    incoming={row['account_key']:row for row in result['identities']}
    if result.get('configuration_complete'):
        identities=incoming
        saved={key:row for key,row in saved.items() if key in incoming and _same_identity(row,incoming[key])}
        watermark=generated
    else:
        for key,row in incoming.items():
            if key in identities and not _same_identity(row,identities[key]):saved.pop(key,None)
            identities[key]=row
    # A lower observation time can never replace a newer last success, even
    # when the enclosing file has a new write time.
    current=[]
    for row in result['accounts']:
        key=row['account_key'];older=saved.get(key)
        if older is not None and older['observed_at']>row['observed_at']:
            result['errors'].append({'account_key':key,'label':row['label'],'message':'CC Switch 返回了较旧的额度记录，正在等待自动更新。'})
            continue
        current.append(row)
    result['accounts']=current
    for raw in current+result['history']:
        row=_clean_snapshot(raw,now)
        if row is None:continue
        key=row['account_key']
        if key not in identities:identities[key]=_identity(row)
        older=saved.get(key)
        if older is None or row['observed_at']>=older['observed_at']:saved[key]=row
    live_keys={row['account_key'] for row in current}
    histories=[];errors={row['account_key']:row for row in result['errors']}
    for key,row in saved.items():
        if key in live_keys or row['observed_at']>now:continue
        histories.append(dict(row,is_live=False,status='last_read'))
        if key not in errors:
            errors[key]={'account_key':key,'label':row['label'],'message':'CC Switch 共享正在恢复，当前显示上次读取的额度。'}
    for key,row in identities.items():
        if key not in live_keys and key not in errors:
            errors[key]={'account_key':key,'label':row['label'],'message':'CC Switch 共享正在恢复，正在等待账号额度。'}
    ordered=list(incoming)+[key for key in identities if key not in incoming]
    identities={key:identities[key] for key in ordered[:MAX_ACCOUNTS]}
    saved={key:row for key,row in saved.items() if key in identities}
    result.update(accounts=[row for row in current if row['account_key'] in identities],
                  history=[row for row in histories if row['account_key'] in identities],
                  identities=list(identities.values()),
                  errors=[row for key,row in errors.items() if key in identities],configured_count=len(identities))
    document={'schema_version':1,'source':HISTORY_SOURCE,'configuration_observed_at':watermark,
              'identities':list(identities.values()),'accounts':list(saved.values())}
    return result,document


def read_all_accounts(path=None, now=None, *, home=None):
    """Read numeric exports, optionally retaining last successes privately.

    Omitting home remains purely read-only. Production passes its VibeCoding Go home
    explicitly; the private history contains only whitelisted identity/numbers.
    Missing, failed or obsolete exports never promote a cached value to live.
    """
    path=Path(path) if path is not None else export_path()
    now=time.time() if now is None else now
    result=_read_export(path,now)
    if home is None:return result
    try:return _with_history(result,home,now)
    except (OSError,ValueError,RecursionError):
        result=_merge_history(result,_load_history(Path(home).expanduser()/HISTORY_NAME,now),now)[0]
        result['history_cache_error']='本机历史额度暂时无法保存，已保存的记录仍可查看。'
        return result
