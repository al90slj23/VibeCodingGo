"""Explicit model-setting intents, retried through owner or cold metadata worker.

No navigation, chat input or independent polling is provided.
The existing app health snapshot admits one finite, writer-guarded drain.
Submitting is persisted before a write; a lost process is never blindly retried.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import stat
import time
import uuid

from .codex_control import ControlError, THREAD_ID
from .events import vibecue_directory
from .cli import atomic_json

MAX_RECORDS = 400
MAX_BYTES = 1_048_576


def _load(home):
    path = vibecue_directory(home) / 'codex-model-queue.json'
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return {}
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > MAX_BYTES:
            raise ControlError('后台模型设置记录的权限或大小无效，原记录已保留')
        value = json.loads(stream.read(MAX_BYTES + 1))
    rows = value.get('records') if isinstance(value, dict) else None
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(rows, list) or len(rows) > MAX_RECORDS:
        raise ControlError('后台模型设置记录无效，原记录已保留')
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get('thread_id'), str) or
                not THREAD_ID.fullmatch(row['thread_id']) or row['thread_id'] in result or
                not isinstance(row.get('queue_id'), str) or
                row.get('status') not in ('queued', 'submitting', 'verified', 'unverified', 'failed')):
            raise ControlError('后台模型设置条目无效，原记录已保留')
        params = row.get('params')
        if not isinstance(params, dict) or set(params) - {'model', 'effort', 'speed'}:
            raise ControlError('后台模型设置参数无效，原记录已保留')
        from .desktop_control import _validate_apply
        _validate_apply({'thread_ids': [row['thread_id']], **params})
        result[row['thread_id']] = row
    return result


@contextmanager
def _locked(home):
    root = vibecue_directory(home); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / 'codex-model-queue.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = _load(home)
        yield rows
        payload = {'schema_version': 1, 'records': list(rows.values())}
        if len(json.dumps(payload, ensure_ascii=False, indent=2).encode()) + 1 > MAX_BYTES:
            raise ControlError('后台模型设置记录已满，请先处理或取消待处理项')
        atomic_json(root / 'codex-model-queue.json', payload)


def enqueue(identifier, params, home=None):
    from .desktop_control import _validate_apply
    if not isinstance(params, dict) or set(params) - {'thread_ids', 'model', 'effort', 'speed', 'defer_unloaded'}:
        raise ControlError('后台模型设置只允许模型、强度和速度')
    values = {key: params[key] for key in ('model', 'effort', 'speed') if key in params}
    _validate_apply({'thread_ids': [identifier], **values})
    if not isinstance(identifier, str) or not THREAD_ID.fullmatch(identifier):
        raise ControlError('后台模型设置对话无效')
    queue_id = str(uuid.uuid4())
    result = {'thread_id': identifier, 'queue_id': queue_id, 'status': 'queued', 'verified': False,
              'requested_parameters': values,
              'message': '连接或运行者暂不可用，已后台排队并自动重试，无需打开对话。'}
    with _locked(home) as rows:
        if identifier not in rows and len(rows) >= MAX_RECORDS:
            removable = [key for key, row in rows.items() if row['status'] not in ('queued', 'submitting')]
            if not removable:
                raise ControlError('后台模型设置队列已满，现有待处理项已保留')
            del rows[removable[0]]
        rows[identifier] = {'thread_id': identifier, 'queue_id': queue_id, 'status': 'queued',
                            'params': values, 'updated_at': time.time(), 'retry_at': 0, 'result': result}
    return result


def supersede(identifier, home=None):
    # A later explicit configuration takes precedence over any old pending one.
    if not (vibecue_directory(home) / 'codex-model-queue.json').exists():
        return
    if identifier not in _load(home):
        return
    with _locked(home) as rows:
        rows.pop(identifier, None)


def cancel(items, home=None):
    if not isinstance(items, list) or len(items) > 200:
        raise ControlError('取消后台模型设置的参数无效')
    results = []
    with _locked(home) as rows:
        for item in items:
            if not isinstance(item, dict):
                raise ControlError('取消后台模型设置的参数无效')
            row = rows.get(item.get('thread_id'))
            if row is None or row['queue_id'] != item.get('queue_id'):
                continue
            if row['status'] == 'queued':
                row.update(status='failed', updated_at=time.time(), result={
                    'thread_id': row['thread_id'], 'queue_id': row['queue_id'], 'status': 'failed',
                    'verified': False, 'error_kind': 'cancelled', 'message': '已取消后台排队，未修改此对话。'})
            results.append(row['result'])
    return {'results': results}


def drain(identifiers, home=None, codex_home=None):
    """At most one owner/cold worker, with a finite twenty-second budget."""
    from .codex_control import unique_threads
    from .desktop_control import run as apply_native
    eligible = unique_threads(identifiers)
    root = vibecue_directory(home); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / 'codex-batch.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'busy': True, 'results': []}
        chosen = None
        with _locked(home) as rows:
            for identifier in eligible:
                row = rows.get(identifier)
                if row and row['status'] == 'queued' and row.get('retry_at', 0) <= time.time():
                    chosen = dict(row)
                    row.update(status='submitting', updated_at=time.time(), result={
                        'thread_id': identifier, 'queue_id': row['queue_id'], 'status': 'unverified',
                        'requested_parameters': row['params'],
                        'verified': False, 'message': '正在后台应用；确认丢失时只重新核验，不会重发。'})
                    break
        if chosen is None:
            return {'results': []}
        identifier, queue_id = chosen['thread_id'], chosen['queue_id']
        attempts = min(8, max(0, chosen.get('attempts', 0) if type(chosen.get('attempts', 0)) is int else 0) + 1)
        try:
            answer = apply_native('apply', {'thread_ids': [identifier], **chosen['params']},
                                  home=home, codex_home=codex_home, deadline=time.monotonic()+20,
                                  queued_execution=True)
            result = next((item for item in (answer or {}).get('results', []) if item.get('thread_id') == identifier), None)
            if result is None:
                result = {'thread_id': identifier, 'status': 'queued', 'verified': False,
                          'message': '等待 Codex 后台连接恢复，不切换窗口。'}
            elif result.get('status') == 'failed' and result.get('error_kind') == 'not_loaded':
                result = {**result, 'status': 'queued', 'message': 'Codex 对话暂不可用，继续后台等待。'}
        except Exception:
            # A crash or unexpected failure after admission may follow a write.
            result = {'thread_id': identifier, 'status': 'unverified', 'verified': False,
                      'message': '后台处理结果未确认，请重新核验；不会自动重发。'}
        result['queue_id'] = queue_id
        result['requested_parameters'] = chosen['params']
        with _locked(home) as rows:
            current = rows.get(identifier)
            if current and current['queue_id'] == queue_id:
                current.update(status=result['status'], result=result, updated_at=time.time(),
                               attempts=attempts,
                               retry_at=time.time()+min(600, 30 * 2 ** (attempts - 1)) if result['status'] == 'queued' else 0)
        return {'results': [result]}
