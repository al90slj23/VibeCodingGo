"""A bounded, owner-scoped projection of Codex's current model selections.

This module performs no I/O. The existing desktop subscription feeds snapshots
and patches into it, and marks the same verified connection live. Only model,
reasoning effort and service-tier metadata survives; conversation text, tool
arguments, permissions, authentication and global configuration never do.
"""
import time

from .codex_control import ControlError, THREAD_ID


SOURCE = 'codex_desktop'
TTL_SECONDS = 30
_INVALID = object()


def _scalar(value, limit=180):
    if value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or
            len(value.encode('utf-8')) > limit or
            any(ord(character) < 32 or ord(character) == 127 for character in value)):
        return _INVALID
    return value.strip()


def _collaboration_fields(value):
    settings = value.get('settings') if isinstance(value, dict) else None
    if not isinstance(settings, dict):
        return {}
    return {'collaboration_' + ('model' if key == 'model' else 'effort'): _scalar(settings[key], 180 if key == 'model' else 40)
            for key in ('model', 'reasoning_effort') if key in settings}


def _thread_settings(value):
    if not isinstance(value, dict):
        return {}
    result = {key: _scalar(value[key], 180 if key == 'model' else 40)
              for key in ('model', 'effort', 'serviceTier') if key in value}
    if 'collaborationMode' in value:
        result.update(_collaboration_fields(value['collaborationMode']))
    return result


def unavailable_settings(*, reason='unavailable'):
    """No old value accompanies an unavailable result and can look current."""
    messages = {'not_resumed':'此对话尚未在 Codex 就绪，无法确认当前设置',
                'stale':'Codex 设置连接已过期，正在重新读取',
                'not_observed':'尚未取得此对话的 Codex 当前设置'}
    return {'status': 'unavailable', 'source': SOURCE, 'reason': reason,
            'message': messages.get(reason, reason)}


class ModelSettingsProjection:
    """Metadata on one owner epoch, with no independent socket or thread.

    ``apply(change)`` consumes the same validated stream envelope as attention.
    ``mark_live()`` is called only while that same owner subscription is healthy;
    it extends validity without changing ``observed_at`` (the setting observation
    time). ``snapshot()`` returns the contract for the UI's ``current_settings``.
    Dispose/invalidate the projection on owner replacement or observation gaps.
    """
    def __init__(self, thread_id, owner, *, clock=time.time, ttl=TTL_SECONDS):
        if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
            raise ControlError('模型设置的对话标识无效')
        if not isinstance(owner, str) or _scalar(owner, 180) is _INVALID:
            raise ControlError('模型设置的对话归属无效')
        self.thread_id, self.owner = thread_id, owner
        self.clock, self.ttl = clock, min(60, max(5, float(ttl)))
        self.revision = None
        self.values = {}
        self.settings = {}
        self.resume_state = None
        self.observed_at = None
        self.verified_at = None
        self.invalidated = False

    def apply(self, change):
        if not isinstance(change, dict):
            raise ControlError('模型设置更新无效')
        revision = change.get('revision')
        if type(revision) is not int or revision < 0:
            raise ControlError('模型设置版本无效')
        kind = change.get('type')
        previous = (dict(self.values), dict(self.settings), self.resume_state)
        if kind == 'snapshot':
            if self.revision is not None and revision < self.revision:
                raise ControlError('模型设置快照早于已观察版本')
            state = change.get('conversationState')
            if not isinstance(state, dict) or state.get('id') not in (None, self.thread_id):
                raise ControlError('模型设置快照与所选对话不一致')
            self.values = {key: _scalar(state[key], 40 if key == 'latestReasoningEffort' else 180)
                           for key in ('latestModel', 'latestReasoningEffort') if key in state}
            if 'latestCollaborationMode' in state:
                self.values.update(_collaboration_fields(state['latestCollaborationMode']))
            self.settings = _thread_settings(state.get('latestThreadSettings'))
            readiness = _scalar(state.get('resumeState'), 40)
            self.resume_state = readiness if isinstance(readiness, str) else None
        elif kind == 'patches':
            if self.revision is None or change.get('baseRevision') != self.revision or revision <= self.revision:
                raise ControlError('模型设置订阅出现版本缺口')
            patches = change.get('patches')
            if not isinstance(patches, list) or len(patches) > 10000:
                raise ControlError('模型设置增量无效')
            try:
                for patch in patches:
                    self._patch(patch)
            except ControlError:
                self.values, self.settings, self.resume_state = previous
                raise
        else:
            raise ControlError('模型设置更新类型无效')
        now = self.clock()
        if kind == 'snapshot' or previous[:2] != (self.values, self.settings):
            self.observed_at = now
        self.revision = revision
        self.verified_at = now
        self.invalidated = False

    def _patch(self, patch):
        if not isinstance(patch, dict) or not isinstance(patch.get('path'), list) or not patch['path']:
            raise ControlError('模型设置增量路径无效')
        path, operation = patch['path'], patch.get('op')
        if path[0] not in ('latestModel', 'latestReasoningEffort', 'latestThreadSettings', 'latestCollaborationMode', 'resumeState'):
            return  # Streaming text and all unrelated state are discarded.
        if operation not in ('add', 'replace', 'remove'):
            raise ControlError('模型设置增量类型无效')
        value = patch.get('value')
        if path == ['resumeState']:
            readiness = _scalar(value, 40) if operation != 'remove' else None
            self.resume_state = readiness if isinstance(readiness, str) else None
            return
        if path[0] in ('latestModel', 'latestReasoningEffort'):
            if len(path) != 1:
                return
            if operation == 'remove':
                self.values.pop(path[0], None)
            else:
                self.values[path[0]] = _scalar(value, 40 if path[0] == 'latestReasoningEffort' else 180)
            return
        if path[0] == 'latestCollaborationMode':
            self._patch_collaboration(self.values, path[1:], operation, value)
            return
        if len(path) == 1:
            self.settings = _thread_settings(value) if operation != 'remove' else {}
        elif len(path) == 2 and path[1] in ('model', 'effort', 'serviceTier'):
            if operation == 'remove':
                self.settings.pop(path[1], None)
            else:
                self.settings[path[1]] = _scalar(value, 180 if path[1] == 'model' else 40)
        elif len(path) >= 2 and path[1] == 'collaborationMode':
            self._patch_collaboration(self.settings, path[2:], operation, value)

    @staticmethod
    def _patch_collaboration(target, path, operation, value):
        if not path or path == ['settings']:
            target.pop('collaboration_model', None); target.pop('collaboration_effort', None)
            if operation != 'remove':
                target.update(_collaboration_fields(value if not path else {'settings': value}))
        elif len(path) == 2 and path[0] == 'settings' and path[1] in ('model', 'reasoning_effort'):
            key = 'collaboration_model' if path[1] == 'model' else 'collaboration_effort'
            if operation == 'remove':
                target.pop(key, None)
            else:
                target[key] = _scalar(value, 180 if path[1] == 'model' else 40)

    def mark_live(self, now=None):
        if self.revision is not None and not self.invalidated:
            self.verified_at = self.clock() if now is None else now

    def invalidate(self):
        self.invalidated = True
        self.values.clear(); self.settings.clear()

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        if (self.invalidated or self.revision is None or self.verified_at is None or
                now < self.verified_at - 5 or now > self.verified_at + self.ttl):
            return unavailable_settings(reason='stale' if self.revision is not None else 'not_observed')
        if self.resume_state != 'resumed':
            return unavailable_settings(reason='Codex 对话尚未就绪' if self.resume_state else '尚未核实 Codex 对话的就绪状态')
        # Explicit next-turn selections survive running-turn runtime updates.
        # Runtime latest* fields must therefore only fill absent selections.
        candidates = (self.settings.get('model'), self.settings.get('collaboration_model'),
                      self.values.get('collaboration_model'), self.values.get('latestModel'))
        model = None if self.settings.get('model') is _INVALID else next((value for value in candidates if isinstance(value, str)), None)
        effort = self.settings.get('effort', self.settings.get('collaboration_effort',
                 self.values.get('collaboration_effort', self.values.get('latestReasoningEffort', _INVALID))))
        effort = 'default' if effort is None else effort if isinstance(effort, str) else None
        tier = self.settings.get('serviceTier', _INVALID)
        speed_status = 'default' if tier is None else 'explicit' if isinstance(tier, str) else 'unknown'
        tier = tier if isinstance(tier, str) else None
        known = sum((model is not None, effort is not None, speed_status == 'explicit'))
        return {'status': 'current' if known == 3 else 'partial' if known else 'unavailable',
                'source': SOURCE, 'thread_id': self.thread_id,
                'owner_client_id': self.owner, 'revision': self.revision,
                'model': model, 'reasoning_effort': effort, 'service_tier': tier,
                'speed_status': speed_status, 'observed_at': self.observed_at,
                'verified_at': self.verified_at, 'expires_at': self.verified_at + self.ttl}
