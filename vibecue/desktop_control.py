"""Explicit model/effort/speed changes through the running desktop owner.

This adapter never sends input, resumes a chat, changes permissions, or starts a
second app-server. The model catalogue remains the desktop's local cache; its
origin is shown explicitly. A settings mutation is only verified after a matching
read from the same owner. A submitted update is never retried on another channel.
"""
import time

from .codex_control import (ControlError, UnconfirmedUpdate, SCHEMA_VERSION,
                            cached_catalog, clean, fields_match, settings_subset,
                            unique_threads)
from .codex_desktop_ipc import DesktopChannel, DesktopOwnerUnavailable, VERSIONS
from .events import codex_directory


class DesktopControlChannel(DesktopChannel):
    method_versions = {key: VERSIONS[key] for key in
                       ('initialize', 'thread-owner-discovery', 'thread-follower-read-model-settings')}
    method_versions['thread-follower-update-thread-settings'] = 2

    def update_settings(self, patch):
        if (not isinstance(patch, dict) or not patch or
                set(patch) - {'model', 'effort', 'serviceTier'}):
            raise ControlError('批量操作只允许模型、强度和速度设置')
        if any(not isinstance(value, str) or not value or len(value) > 180
               for value in patch.values()):
            raise ControlError('批量设置值无效')
        if 'serviceTier' in patch and patch['serviceTier'] not in ('default', 'priority', 'fast'):
            raise ControlError('快速档位无效')
        if not self.owner:
            self.discover()
        # Omitting activeTurnId takes the desktop's next-turn settings path;
        # the active-turn permission-update path is deliberately unreachable.
        result = self._call('thread-follower-update-thread-settings',
                            {'conversationId': self.thread_id, 'threadSettings': dict(patch)},
                            target=self.owner, mutation=True)
        applied = result.get('applied')
        if type(applied) is not bool:
            raise UnconfirmedUpdate('设置已提交，但 Codex 未返回可确认结果；不会自动重试')
        return applied


def _validate_apply(params):
    if set(params) - {'thread_ids', 'model', 'effort', 'speed'}:
        raise ControlError('批量操作只允许修改模型、强度和快速档位')
    speed = params.get('speed', 'keep')
    if speed not in ('keep', 'standard', 'fast'):
        raise ControlError('快速档位无效')
    for key in ('model', 'effort'):
        if key in params and (not isinstance(params[key], str) or not params[key] or len(params[key]) > 180):
            raise ControlError('模型或强度无效')
    if 'model' not in params and 'effort' not in params and speed == 'keep':
        raise ControlError('请选择至少一项需要修改的设置')


def _read(channel):
    raw = channel.read_settings()
    if raw.get('resumeState') != 'resumed':
        raise ControlError('此对话尚未在 Codex 就绪，请先在 Codex 打开；不会自动载入')
    result = settings_subset(raw)
    return result


def _summary(identifiers, results, connected):
    return {'schema_version': SCHEMA_VERSION, 'connected': connected,
            'transport': 'desktop_ipc', 'results': results,
            'summary': {'selected': len(identifiers),
                        **{key: sum(row.get('status') == key for row in results)
                           for key in ('verified', 'unverified', 'failed')}}}


def run(operation, params=None, home=None, codex_home=None, deadline=None):
    """Return None when no desktop socket exists, preserving legacy fallback.

    Opening this connection only initializes the current desktop IPC. It never
    launches Codex. Discovery and read-only queries can be retried by later UI
    refreshes; mutation is attempted once and is never rerouted after submission.
    """
    if not (codex_directory(codex_home) / 'ipc/ipc.sock').exists():
        return None
    params = params or {}
    if operation not in ('catalog', 'inspect', 'apply'):
        raise ControlError('不支持的批量操作')
    identifiers = [] if operation == 'catalog' else unique_threads(params.get('thread_ids'))
    if operation == 'apply':
        _validate_apply(params)
    deadline = time.monotonic() + 85 if deadline is None else deadline
    models = cached_catalog(codex_home)
    if operation == 'catalog':
        try:
            # Peer identity and audited build are sufficient to identify this
            # control capability. No fictitious thread is discovered or loaded.
            with DesktopControlChannel('vibecue-control-probe', codex_home=codex_home, timeout=4):
                pass
            return {'schema_version': SCHEMA_VERSION, 'connected': True,
                    'transport': 'desktop_ipc', 'models': models,
                    'message': '已自动复用当前 Codex；模型与档位来自本机模型缓存，应用后逐项核对。'}
        except ControlError as error:
            return {'schema_version': SCHEMA_VERSION, 'connected': False,
                    'transport': 'desktop_ipc', 'models': models, 'message': clean(error, 300)}
    by_model = {model['id']: model for model in models}
    results, connected = [], False
    for identifier in identifiers:
        submitted = False
        patch = {}
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlError('本轮批量操作已到时，此项未提交；请核对已完成项目')
            with DesktopControlChannel(identifier, codex_home=codex_home, timeout=min(8, remaining)) as channel:
                connected = True
                current = _read(channel)
                if operation == 'inspect':
                    results.append({'thread_id': identifier, 'settings': current,
                                    'loaded': True, 'status': 'read'})
                    continue
                selected = params.get('model') or current.get('model')
                model = by_model.get(selected)
                if model is None:
                    raise ControlError('本机模型缓存没有目标模型，请先在 Codex 刷新模型目录')
                effort = params.get('effort') or current.get('effort')
                if effort and effort not in [row['id'] for row in model['efforts']]:
                    raise ControlError('目标模型缓存未确认支持此强度，未修改此对话')
                patch = {key: params[key] for key in ('model', 'effort') if key in params}
                if params.get('speed') == 'standard':
                    patch['serviceTier'] = 'default'
                elif params.get('speed') == 'fast':
                    if not model['fast_tier']:
                        raise ControlError('目标模型缓存未提供快速档位，未修改此对话')
                    patch['serviceTier'] = model['fast_tier']
                if time.monotonic() >= deadline:
                    raise ControlError('本轮批量操作已到时，此项未提交')
                submitted = True
                applied = channel.update_settings(patch)
                if not applied:
                    submitted = False  # The owner explicitly declined the update.
                    raise ControlError('Codex 未应用此设置，请刷新后核对')
                # Waits for the owner's own pending setting update, then reads
                # model/effort. The snapshot carries serviceTier, which is not
                # present in the dedicated model-settings response.
                observed = _read(channel)
                if 'serviceTier' in patch:
                    snapshot = channel.runtime_snapshot()
                    if snapshot.get('thread_id') != identifier:
                        raise UnconfirmedUpdate('设置已提交，但回读的对话不匹配，请在 Codex 核对')
                    observed.update(settings_subset(snapshot.get('thread_settings', {})))
                verified = fields_match(observed, patch)
                results.append({'thread_id': identifier,
                                'status': 'verified' if verified else 'unverified',
                                'verified': verified, 'settings': observed,
                                'message': 'Codex 已应用并回读确认，下轮生效。' if verified else
                                           'Codex 已接收，回读未完整匹配；请核对，不会自动重试。'})
        except (ControlError, OSError, ValueError, TypeError) as error:
            uncertain = submitted or isinstance(error, UnconfirmedUpdate)
            results.append({'thread_id': identifier,
                            'status': 'unverified' if uncertain else 'failed',
                            'verified': False, 'settings': settings_subset(patch),
                            'message': ('设置可能已提交，请在 Codex 核对；不会自动重试。' if uncertain else clean(error, 300))})
    return _summary(identifiers, results, connected)
