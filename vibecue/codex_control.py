"""Explicit, local Codex thread settings control; never sends conversation input.

Only an opt-in desktop-owned relay can apply settings. A catalog cache is useful
for preparing a batch while disconnected, but is never treated as proof that a
change reached Codex. No authentication file is opened; full turns are never
requested, and conversation text is never exposed or persisted by this module.
"""
from collections import OrderedDict
import argparse
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time

from .events import vibecue_directory, codex_directory

SCHEMA_VERSION = 1
MAX_THREADS = 200
MAX_MESSAGE = 2 * 1024 * 1024
THREAD_ID = re.compile(r'^[A-Za-z0-9_-]{1,160}$')


class ControlError(Exception):
    pass


class UnconfirmedUpdate(ControlError):
    """The update may have reached Codex; it must not be retried as unsent."""
    pass


def clean(value, maximum=180):
    return ' '.join(str(value or '').replace('\x00', '').split())[:maximum]


def unique_threads(values):
    if not isinstance(values, list) or len(values) > MAX_THREADS * 8:
        raise ControlError('批量对话列表无效或过长')
    result = []
    for value in values:
        if not isinstance(value, str) or not THREAD_ID.fullmatch(value):
            raise ControlError('对话标识无效')
        if value not in result:
            result.append(value)
    if not result or len(result) > MAX_THREADS:
        raise ControlError('请选择 1–200 个不同的对话')
    return result


def normalize_model(value, cached=False):
    if not isinstance(value, dict):
        return None
    identifier = value.get('slug') if cached else value.get('model') or value.get('id')
    if not isinstance(identifier, str) or not identifier or len(identifier) > 180:
        return None
    if value.get('hidden') or value.get('visibility') == 'hide':
        return None
    effort_rows = value.get('supported_reasoning_levels' if cached else 'supportedReasoningEfforts', [])
    efforts = []
    for row in effort_rows if isinstance(effort_rows, list) else []:
        effort = (row.get('effort') if cached else row.get('reasoningEffort', row.get('effort'))) if isinstance(row, dict) else None
        if isinstance(effort, str) and 0 < len(effort) <= 40 and effort not in [r['id'] for r in efforts]:
            efforts.append({'id': effort, 'name': effort, 'description': clean(row.get('description'), 300)})
    tiers = []
    for row in value.get('service_tiers' if cached else 'serviceTiers', []) or []:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            continue
        tiers.append({'id': clean(row['id'], 40), 'name': clean(row.get('name') or row['id'], 80),
                      'description': clean(row.get('description'), 300)})
    fast = next((row['id'] for row in tiers if row['id'] in ('priority', 'fast')), None)
    return {'id': identifier, 'name': clean(value.get('display_name' if cached else 'displayName') or identifier),
            'efforts': efforts, 'default_effort': clean(value.get('default_reasoning_level' if cached else 'defaultReasoningEffort'), 40),
            'service_tiers': tiers, 'fast_tier': fast, 'source': 'cache' if cached else 'live'}


def cached_catalog(codex_home=None):
    path = codex_directory(codex_home) / 'models_cache.json'
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return []
        data = json.loads(path.read_text())
        return [model for raw in data.get('models', []) if (model := normalize_model(raw, cached=True))]
    except (OSError, ValueError, TypeError):
        return []


def settings_subset(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for source, target in [('model', 'model'), ('effort', 'effort'), ('reasoningEffort', 'effort'),
                           ('serviceTier', 'service_tier')]:
        if source in value and (value[source] is None or isinstance(value[source], str)):
            result[target] = clean(value[source]) if value[source] is not None else None
    return result


def fields_match(settings, patch):
    for key, value in patch.items():
        output_key = 'service_tier' if key == 'serviceTier' else key
        if output_key not in settings:
            return False
        actual = settings[output_key]
        if key == 'serviceTier' and value in ('priority', 'fast') and actual in ('priority', 'fast'):
            continue
        if actual != value:
            return False
    return True


class Controller:
    """Runs inside the owner relay; rpc never starts or resumes a thread."""
    def __init__(self, channel, deadline=None):
        self.channel = channel
        self.deadline = deadline

    def rpc(self, method, params):
        remaining = 8 if self.deadline is None else self.deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError('本轮批量操作已到时；此项未继续发送，请核对已完成项目后重试剩余项。')
        return self.channel.request(method, params, timeout=min(8, remaining))

    def catalog(self):
        models, cursor = OrderedDict(), None
        for _ in range(10):
            params = {'includeHidden': False, 'limit': 100}
            if cursor:
                params['cursor'] = cursor
            data = self.rpc('model/list', params)
            for raw in data.get('data', []):
                model = normalize_model(raw)
                if model:
                    models[model['id']] = model
            next_cursor = data.get('nextCursor')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return {'schema_version': SCHEMA_VERSION, 'connected': True, 'models': list(models.values()),
                'message': '已连接 Codex；模型、强度及快速档位来自当前客户端能力。'}

    def inspect_one(self, identifier):
        # IncludeTurns=false prevents this control surface from reading text.
        data = self.rpc('thread/read', {'threadId': identifier, 'includeTurns': False})
        thread = data.get('thread', {})
        if not isinstance(thread, dict) or thread.get('id') != identifier:
            raise ControlError('Codex 返回的对话与所选项不一致')
        live, _ = self.channel.settings(identifier)
        return {**settings_subset(thread), **live}

    def loaded_ids(self):
        result, cursor = set(), None
        for _ in range(10):
            data = self.rpc('thread/loaded/list', {'cursor': cursor} if cursor else {})
            result.update(value for value in data.get('data', []) if isinstance(value, str))
            next_cursor = data.get('nextCursor')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return result

    def inspect(self, params):
        results = []
        loaded = self.loaded_ids()
        for identifier in unique_threads(params.get('thread_ids')):
            try:
                results.append({'thread_id': identifier, 'settings': self.inspect_one(identifier),
                                'loaded': identifier in loaded, 'status': 'read'})
            except ControlError as error:
                results.append({'thread_id': identifier, 'status': 'failed', 'message': clean(error, 300)})
        return {'schema_version': SCHEMA_VERSION, 'connected': True, 'results': results}

    def apply(self, params):
        if self.deadline is None:
            self.deadline = time.monotonic() + 85
        identifiers = unique_threads(params.get('thread_ids'))
        unknown = set(params) - {'thread_ids', 'model', 'effort', 'speed'}
        if unknown:
            raise ControlError('批量操作只允许修改模型、强度和快速档位')
        speed = params.get('speed', 'keep')
        if speed not in ('keep', 'standard', 'fast'):
            raise ControlError('快速档位无效')
        for field in ('model', 'effort'):
            if field in params and (not isinstance(params[field], str) or not params[field] or len(params[field]) > 180):
                raise ControlError('模型或强度无效')
        if 'model' not in params and 'effort' not in params and speed == 'keep':
            raise ControlError('请选择至少一项需要修改的设置')
        models = {model['id']: model for model in self.catalog()['models']}
        loaded = self.loaded_ids()
        results = []
        for identifier in identifiers:
            try:
                if identifier not in loaded:
                    raise ControlError('此对话尚未载入当前 Codex；请先打开对话，再重新应用。')
                current = self.inspect_one(identifier)
                model_id = params.get('model') or current.get('model')
                model = models.get(model_id)
                if model is None:
                    raise ControlError('当前客户端没有可用的目标模型，未修改此对话')
                effort = params.get('effort') or current.get('effort')
                if effort and effort not in [row['id'] for row in model['efforts']]:
                    raise ControlError('目标模型不支持此强度，请选择兼容的强度')
                patch = {field: params[field] for field in ('model', 'effort') if field in params}
                if speed == 'standard':
                    # Clearing an override can restore a model's Fast default.
                    # Explicit default is the request-level Standard override.
                    patch['serviceTier'] = 'default'
                elif speed == 'fast':
                    if not model['fast_tier']:
                        raise ControlError('此模型未提供快速档位，未修改此对话')
                    patch['serviceTier'] = model['fast_tier']
                _, revision = self.channel.settings(identifier)
                try:
                    self.rpc('thread/settings/update', {'threadId': identifier, **patch})
                except UnconfirmedUpdate as error:
                    results.append({'thread_id': identifier, 'status': 'unverified', 'verified': False,
                                    'settings': settings_subset(patch), 'message': clean(error, 300)})
                    continue
                left = max(0, self.deadline - time.monotonic())
                confirmed = self.channel.wait_settings(identifier, revision, patch, timeout=min(2, left))
                results.append({'thread_id': identifier, 'status': 'verified' if confirmed else 'unverified',
                                'verified': bool(confirmed), 'settings': confirmed or settings_subset(patch),
                                'message': '已由 Codex 通知确认，下轮生效。' if confirmed else 'Codex 已接收，但未收到设置回读；请在 Codex 中核对。'})
            except ControlError as error:
                results.append({'thread_id': identifier, 'status': 'failed', 'verified': False,
                                'message': clean(error, 300)})
        return {'schema_version': SCHEMA_VERSION, 'connected': True, 'results': results,
                'summary': {'selected': len(identifiers), **{key: sum(row['status'] == key for row in results)
                                                        for key in ('verified', 'unverified', 'failed')}}}


def connections(home=None):
    folder = vibecue_directory(home) / 'control'
    result = []
    try:
        paths = sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:64]
    except OSError:
        return []
    for path in paths:
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 or metadata.st_size > 65536:
                continue
            value = json.loads(path.read_text())
            if value.get('ready') is not True or not -2 <= time.time() - float(value.get('last_seen', 0)) <= 15:
                continue
            pid = int(value['pid'])
            backend_pid = int(value['backend_pid'])
            session = value['session_id']
            boot = float(value['boot_time'])
            if (pid <= 1 or backend_pid <= 1 or not isinstance(session, str) or not re.fullmatch(r'[0-9a-f]{32}', session)
                    or not math.isfinite(boot) or abs((time.time() - time.monotonic()) - boot) > 120):
                continue
            os.kill(pid, 0)
            os.kill(backend_pid, 0)
            sock = Path(value['socket_path'])
            expected_name = str(pid) + '-' + session[:8]
            if path.name != expected_name + '.json' or sock.name != expected_name + '.sock':
                continue
            st = sock.lstat()
            parent = sock.parent.lstat()
            if (not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077 or
                    not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077):
                continue
            result.append(value)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return result


def socket_call(connection, operation, params, timeout=90):
    if timeout <= 1:
        raise ControlError('本轮批量时间已到，剩余对话未发送设置。')
    request = json.dumps({'operation': operation, 'params': params,
                          'deadline': time.monotonic() + min(85, timeout - 1)}, separators=(',', ':')).encode() + b'\n'
    if len(request) > 65536:
        raise ControlError('批量操作数据过大')
    sent = False
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(timeout); sock.connect(connection['socket_path']); sock.sendall(request)
            sent = True
            stream = sock.makefile('rb')
            data = stream.readline(MAX_MESSAGE + 1)
            if not data.endswith(b'\n') or len(data) > MAX_MESSAGE:
                raise ControlError('Codex 控制响应无效或过大')
            response = json.loads(data)
            if not isinstance(response, dict):
                raise ControlError('Codex 控制响应无效')
            if response.get('error'):
                raise ControlError(clean(response['error'], 300))
            return response
    except (OSError, ValueError, TypeError, KeyError) as error:
        if sent and operation == 'apply':
            identifiers = unique_threads(params.get('thread_ids'))
            return {'schema_version': SCHEMA_VERSION, 'connected': False,
                    'results': [{'thread_id': identifier, 'status': 'unverified', 'verified': False,
                                 'message': '批次已提交，但本机连接中断；部分对话可能已变更，请在 Codex 中核对。'}
                                for identifier in identifiers],
                    'summary': {'selected': len(identifiers), 'verified': 0, 'unverified': len(identifiers), 'failed': 0}}
        raise ControlError('本机 Codex 控制连接已中断，请重新连接后重试') from error


def run(operation, params=None, home=None, codex_home=None):
    params = params or {}
    deadline = time.monotonic()+90
    live = connections(home)
    if operation == 'catalog':
        for connection in live:
            try:
                return socket_call(connection, operation, {}, timeout=min(15,deadline-time.monotonic()))
            except ControlError:
                continue
        return {'schema_version': SCHEMA_VERSION, 'connected': False, 'models': cached_catalog(codex_home),
                'message': '尚未连接 Codex 控制通道；当前为本机模型缓存。请通过 VibeCue 接通后再应用。'}
    if operation not in ('apply', 'inspect'):
        raise ControlError('不支持的批量操作')
    identifiers = unique_threads(params.get('thread_ids'))
    if not live:
        return {'schema_version': SCHEMA_VERSION, 'connected': False,
                'results': [{'thread_id': identifier, 'status': 'failed', 'verified': False,
                             'message': '尚未连接 Codex 控制通道，没有修改此对话。'} for identifier in identifiers],
                'summary': {'selected': len(identifiers), 'verified': 0, 'unverified': 0, 'failed': len(identifiers)}}
    if len(live) == 1:
        return socket_call(live[0], operation, {**params, 'thread_ids': identifiers}, timeout=90)
    # A desktop normally owns one local backend. Multiple live owners are not
    # guessed: inspect safely, then send the patch only to a reader that reports
    # the thread as loaded. This never resumes a thread to claim ownership.
    results = []
    for identifier in identifiers:
        routed = False
        last = None
        for connection in live:
            try:
                answer = socket_call(connection, operation, {**params, 'thread_ids': [identifier]}, timeout=deadline-time.monotonic())
                row = next((item for item in answer.get('results', []) if item.get('thread_id') == identifier), None)
                if row is not None and row.get('status') != 'failed':
                    results.append(row); routed = True; break
                if row is not None:
                    last = row
            except ControlError as error:
                last = {'thread_id': identifier, 'status': 'failed', 'verified': False, 'message': clean(error, 300)}
        if not routed:
            results.append(last if last is not None else
                           {'thread_id': identifier, 'status': 'failed', 'verified': False, 'message': '此对话没有可用的 Codex 控制连接。'})
    return {'schema_version': SCHEMA_VERSION, 'connected': True, 'results': results,
            'summary': {'selected': len(identifiers), **{key: sum(row.get('status') == key for row in results)
                                                    for key in ('verified', 'unverified', 'failed')}}}


def main(argv=None):
    parser = argparse.ArgumentParser(description='VibeCue Codex 批量设置')
    parser.add_argument('operation', choices=('catalog', 'inspect', 'apply'))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        raw = b'' if args.operation == 'catalog' else sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ControlError('批量操作数据过大')
        params = {} if args.operation == 'catalog' else json.loads(raw)
        if not isinstance(params, dict):
            raise ControlError('批量操作需要 JSON 对象')
        print(json.dumps(run(args.operation, params), ensure_ascii=False))
    except (ControlError, OSError, ValueError, TypeError) as error:
        print(json.dumps({'schema_version': SCHEMA_VERSION, 'connected': False, 'error': clean(error, 300)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
