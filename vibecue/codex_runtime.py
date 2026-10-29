"""Body-free projection of one verified Codex owner's current turn.

Turn IDs belong to the receiving chat. A dispatching chat's root/parent turn,
tool result, or wait operation is never evidence about the receiver's state.
"""
from copy import deepcopy
from datetime import datetime
import math
import time

from .codex_control import ControlError

MAX_TURNS = 4096
TTL_SECONDS = 30
TURN_FIELDS = frozenset(('turnId', 'status', 'startedAt', 'completedAt'))


def _text(value, limit=160):
    return value if isinstance(value, str) and 0 < len(value) <= limit and not any(ord(c) < 32 for c in value) else None


def _time(value):
    try:
        if isinstance(value, bool):
            return None
        stamp = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        stamp = stamp / 1000 if stamp > 10_000_000_000 else stamp
        return stamp if math.isfinite(stamp) and stamp > 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def status_metadata(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    kind = _text(value.get('type'), 40)
    if kind:
        result['type'] = kind
    flags = value.get('activeFlags')
    if isinstance(flags, list) and len(flags) <= 20 and all(_text(flag, 80) for flag in flags):
        result['activeFlags'] = list(flags)
    elif kind == 'active' or 'activeFlags' in value:
        result['activeFlags'] = None
    return result


def turn_metadata(value):
    if not isinstance(value, dict):
        return {}
    result = {key: value[key] for key in ('turnId', 'status') if _text(value.get(key))}
    for key in ('startedAt', 'completedAt'):
        stamp = _time(value.get(key))
        if stamp is not None:
            result[key] = stamp
    return result


def _entries(value):
    if not isinstance(value, list) or len(value) > MAX_TURNS:
        return []
    return [{'value': row.get('value')} if isinstance(row, dict) and _text(row.get('value'), 512) else {} for row in value]


def _islands(value):
    if not isinstance(value, list) or len(value) > MAX_TURNS:
        return []
    return [{'entries': _entries(row.get('entries'))} if isinstance(row, dict) else {} for row in value]


def _history(value):
    if not isinstance(value, dict):
        return {}
    entities = value.get('entitiesByKey')
    return {'entitiesByKey': {key: turn_metadata(row) for key, row in entities.items() if _text(key, 512)}
            if isinstance(entities, dict) and len(entities) <= MAX_TURNS else {},
            'islands': _islands(value.get('islands'))}


def _canonical(value):
    if not isinstance(value, dict):
        return {}
    return {'kind': value.get('kind') if value.get('kind') == 'canonical' else None,
            'history': _history(value.get('history'))}


def latest_turn(state):
    """Canonical order is authoritative even when its tail is empty."""
    canonical = state.get('turnHistory')
    if isinstance(canonical, dict) and canonical.get('kind') == 'canonical':
        history = canonical.get('history')
        if not isinstance(history, dict):
            return {}
        entities, islands = history.get('entitiesByKey'), history.get('islands')
        if not isinstance(entities, dict) or not isinstance(islands, list):
            return {}
        for island in reversed(islands):
            entries = island.get('entries') if isinstance(island, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in reversed(entries):
                key = entry.get('value') if isinstance(entry, dict) else None
                if isinstance(key, str) and isinstance(entities.get(key), dict):
                    return turn_metadata(entities[key])
        return {}
    turns = state.get('turns')
    return next((turn_metadata(row) for row in reversed(turns) if isinstance(row, dict)), {}) if isinstance(turns, list) else {}


class RuntimeProjection:
    """Retain bounded turn ordering/status metadata, never message bodies."""
    def __init__(self, thread_id, owner, *, clock=time.time):
        self.thread_id, self.owner, self.clock = thread_id, owner, clock
        self.data = {}
        self.revision = None
        self.observed_at = self.verified_at = None
        self.invalidated = False

    def apply(self, change):
        revision = change.get('revision') if isinstance(change, dict) else None
        if type(revision) is not int or revision < 0:
            raise ControlError('运行状态版本无效')
        before = self._identity()
        if change.get('type') == 'snapshot':
            if self.revision is not None and revision < self.revision:
                raise ControlError('运行状态快照早于已观察版本')
            state = change.get('conversationState')
            if not isinstance(state, dict) or state.get('id') not in (None, self.thread_id):
                raise ControlError('运行状态与所选对话不一致')
            self.data = {'resumeState': _text(state.get('resumeState'), 40),
                         'threadRuntimeStatus': status_metadata(state.get('threadRuntimeStatus')),
                         'turns': [turn_metadata(row) for row in state['turns']]
                         if isinstance(state.get('turns'), list) and len(state['turns']) <= MAX_TURNS else []}
            if 'turnHistory' in state:
                self.data['turnHistory'] = _canonical(state['turnHistory'])
        elif change.get('type') == 'patches':
            now = self.clock()
            if self.invalidated or self.verified_at is None or not self.verified_at - 5 <= now <= self.verified_at + TTL_SECONDS:
                self.invalidated = True
                raise ControlError('运行状态订阅已过期，需要重新读取快照')
            if self.revision is None or change.get('baseRevision') != self.revision or revision <= self.revision:
                raise ControlError('运行状态订阅出现版本缺口')
            patches = change.get('patches')
            if not isinstance(patches, list) or len(patches) > 10000:
                raise ControlError('运行状态增量无效')
            for patch in patches:
                self._patch(patch)
        else:
            raise ControlError('运行状态更新类型无效')
        now = self.clock()
        if change.get('type') == 'snapshot' or before != self._identity():
            self.observed_at = now
        self.verified_at, self.revision = now, revision
        self.invalidated = False

    def _identity(self):
        return (self.data.get('resumeState'), deepcopy(self.data.get('threadRuntimeStatus')), latest_turn(self.data))

    def _project(self, path, value):
        if path == ['resumeState']:
            return True, _text(value, 40)
        if path == ['threadRuntimeStatus']:
            return True, status_metadata(value)
        if path == ['threadRuntimeStatus', 'type']:
            return True, _text(value, 40)
        if path == ['threadRuntimeStatus', 'activeFlags']:
            return True, status_metadata({'type': 'active', 'activeFlags': value}).get('activeFlags')
        if len(path) == 3 and path[:2] == ['threadRuntimeStatus', 'activeFlags'] and type(path[2]) is int:
            return True, _text(value, 80)
        if path == ['turns']:
            return True, [turn_metadata(row) for row in value] if isinstance(value, list) and len(value) <= MAX_TURNS else []
        if len(path) >= 2 and path[0] == 'turns' and type(path[1]) is int:
            tail = path[2:]
        elif path == ['turnHistory']:
            return True, _canonical(value)
        elif path == ['turnHistory', 'kind']:
            return True, 'canonical' if value == 'canonical' else None
        elif path == ['turnHistory', 'history']:
            return True, _history(value)
        elif path == ['turnHistory', 'history', 'entitiesByKey']:
            return True, _history({'entitiesByKey': value})['entitiesByKey']
        elif len(path) >= 4 and path[:3] == ['turnHistory', 'history', 'entitiesByKey'] and _text(path[3], 512):
            tail = path[4:]
        elif path[:3] == ['turnHistory', 'history', 'islands']:
            tail = path[3:]
            if not tail:
                return True, _islands(value)
            if type(tail[0]) is not int:
                return False, None
            if len(tail) == 1:
                return True, {'entries': _entries(value.get('entries'))} if isinstance(value, dict) else {}
            if tail[1:] == ['entries']:
                return True, _entries(value)
            if len(tail) >= 3 and tail[1] == 'entries' and type(tail[2]) is int:
                if len(tail) == 3:
                    return True, {'value': value.get('value')} if isinstance(value, dict) and _text(value.get('value'), 512) else {}
                if tail[3:] == ['value']:
                    return True, _text(value, 512)
            return False, None
        else:
            return False, None
        if not tail:
            return True, turn_metadata(value)
        if len(tail) == 1 and tail[0] in TURN_FIELDS:
            return True, _time(value) if tail[0] in ('startedAt', 'completedAt') else _text(value)
        return False, None

    def _patch(self, patch):
        if not isinstance(patch, dict) or not isinstance(patch.get('path'), list):
            raise ControlError('运行状态增量无效')
        path, op = patch['path'], patch.get('op')
        known, value = self._project(path, patch.get('value'))
        if not known:
            return
        if op not in ('add', 'replace', 'remove'):
            raise ControlError('运行状态增量类型无效')
        parent = self.data
        for key in path[:-1]:
            if isinstance(parent, dict) and key in parent:
                parent = parent[key]
            elif isinstance(parent, list) and type(key) is int and 0 <= key < len(parent):
                parent = parent[key]
            else:
                raise ControlError('运行状态增量路径不连续')
        key = path[-1]
        if isinstance(parent, dict) and isinstance(key, str):
            if op == 'remove':
                parent.pop(key, None)
            else:
                parent[key] = value
        elif isinstance(parent, list) and type(key) is int and 0 <= key <= len(parent):
            if op == 'add' and len(parent) < MAX_TURNS:
                parent.insert(key, value)
            elif key < len(parent) and op == 'replace':
                parent[key] = value
            elif key < len(parent) and op == 'remove':
                parent.pop(key)
            else:
                raise ControlError('运行状态增量索引不连续')
        else:
            raise ControlError('运行状态增量路径无效')

    def mark_live(self, now=None):
        now = self.clock() if now is None else now
        if self.verified_at is not None and self.verified_at - 5 <= now <= self.verified_at + TTL_SECONDS:
            if not self.invalidated:
                self.verified_at = now
        elif self.revision is not None:
            self.invalidated = True

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        if self.invalidated or self.revision is None or self.verified_at is None or not self.verified_at - 5 <= now <= self.verified_at + TTL_SECONDS:
            return {'state': 'unknown', 'authoritative': False}
        turn = latest_turn(self.data)
        runtime = self.data.get('threadRuntimeStatus', {})
        kind, flags = runtime.get('type'), runtime.get('activeFlags')
        status = turn.get('status')
        state = 'unknown'
        if self.data.get('resumeState') == 'resumed':
            if kind == 'active' and turn.get('turnId') and status == 'inProgress' and isinstance(flags, list):
                state = 'waiting' if set(flags) & {'waitingOnApproval', 'waitingOnUserInput'} else 'running'
            elif kind == 'idle' and turn.get('turnId') and status in ('completed', 'interrupted', 'failed'):
                state = 'complete' if status == 'completed' else 'aborted'
        return {'thread_id': self.thread_id, 'turn_id': turn.get('turnId', ''), 'state': state,
                'source': 'codex-desktop-owner-stream', 'authoritative': state != 'unknown',
                'started_at': turn.get('startedAt'), 'finished_at': turn.get('completedAt'),
                'observed_at': self.observed_at, 'verified_at': self.verified_at,
                'expires_at': self.verified_at + TTL_SECONDS, 'revision': self.revision}
