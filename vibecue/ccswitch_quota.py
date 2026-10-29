"""Read CC Switch's opt-in, credential-free Codex quota export.

CC Switch owns all OAuth refreshes and queries. This reader never opens its
authorization store, invokes login, handles tokens or changes the active account.
The small upstream integration patch lives in integrations/ccswitch/.
"""
from __future__ import annotations

from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import stat
import time

SOURCE = 'ccswitch_quota_bridge'
FRESH_SECONDS = 120
MAX_BYTES = 1_048_576
MAX_ACCOUNTS = 20
EXPORT_NAME = 'vibecue-codex-quota.json'
ENABLE_NAME = 'vibecue-quota-sharing.enabled'
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
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum else None


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


def read_all_accounts(path=None, now=None):
    """Return sanitized snapshots and independent errors, without network I/O.

    accounts match account_quota.build_snapshot; fresh values alone are returned
    there. history keeps old successful values explicitly marked non-live.
    identities retain configured accounts even before their first success.
    Unknown fields and source error text are deliberately never propagated.
    """
    path=Path(path) if path is not None else export_path()
    now=time.time() if now is None else now
    result={'accounts':[],'history':[],'identities':[],'errors':[],'configured_count':0,
            'bridge_status':'unavailable','source':SOURCE}
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
    return result
