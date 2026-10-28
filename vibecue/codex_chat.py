"""Explicit user messages over the existing opted-in Codex owner connection.

Opening a detail window only queries metadata. Loading, starting or steering
requires its own user action; this worker never opens auth files or creates a
second app-server. Message text stays in transient IPC, never in the receipt log.
"""
from contextlib import contextmanager
from collections import OrderedDict
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import time

from .codex_control import ControlError, UnconfirmedUpdate, THREAD_ID, connections
from .events import vibecue_directory

CHAT_VERSION = 1
MAX_TEXT_BYTES = 8192
MAX_INPUT_BYTES = 65536
MAX_RECEIPTS = 512
REQUEST_ID = re.compile(r'^[A-Za-z0-9_-]{16,64}$')


def thread_id(params, allowed):
    if not isinstance(params, dict) or set(params) - set(allowed):
        raise ControlError('对话操作包含不支持的字段')
    identifier = params.get('thread_id')
    if not isinstance(identifier, str) or not THREAD_ID.fullmatch(identifier):
        raise ControlError('对话标识无效')
    return identifier


def send_params(params):
    identifier = thread_id(params, ('thread_id', 'text', 'request_id', 'expected_turn_id'))
    text, request = params.get('text'), params.get('request_id')
    if not isinstance(text, str) or not text.strip() or '\x00' in text:
        raise ControlError('请输入要发送的消息')
    if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
        raise ControlError('回复不能超过 ' + str(MAX_TEXT_BYTES) + ' 个 UTF-8 字节；请缩短内容后发送')
    if not isinstance(request, str) or not REQUEST_ID.fullmatch(request):
        raise ControlError('消息请求标识无效')
    expected = params.get('expected_turn_id')
    if expected is not None and (not isinstance(expected, str) or not THREAD_ID.fullmatch(expected)):
        raise ControlError('运行轮次标识无效，请刷新后重试')
    return identifier, text, request, expected


def unavailable(state='disconnected', connected=False):
    messages = {
        'legacy_bridge': '当前 Codex 连接尚不支持窗口回复。请在方便时正常退出，再通过 VibeCue 接通 Codex；不会自动重启。',
        'disconnected': 'Codex 回复连接尚未接通。可以先查看内容、保留待续，或在 Codex 中打开。',
        'ambiguous_owner': '存在多个 Codex 实例，无法确认此对话的载入归属。请先在对应 Codex 实例打开。',
    }
    return {'schema_version': 1, 'connected': connected, 'supported': False, 'can_send': False,
            'needs_load': False, 'state': state, 'max_text_bytes': MAX_TEXT_BYTES,
            'message': messages.get(state, messages['disconnected'])}


class ChatController:
    """Owner-local operations; only send() can generate user input."""
    def __init__(self, channel, home=None, deadline=None):
        self.channel = channel
        self.home = vibecue_directory(home)
        self.deadline = deadline or time.monotonic() + 20

    def rpc(self, method, params):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ControlError('对话操作已到时，请刷新状态')
        return self.channel.request(method, params, timeout=min(8, remaining))

    def loaded(self):
        identifiers, cursor = set(), None
        for _ in range(10):
            data = self.rpc('thread/loaded/list', {'cursor': cursor} if cursor else {})
            identifiers.update(v for v in data.get('data', []) if isinstance(v, str))
            next_cursor = data.get('nextCursor')
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return identifiers

    def status(self, params):
        identifier = thread_id(params, ('thread_id',))
        base = {'schema_version': 1, 'thread_id': identifier, 'connected': True,
                'supported': True, 'can_send': False, 'needs_load': False,
                'max_text_bytes': MAX_TEXT_BYTES}
        if identifier not in self.loaded():
            return dict(base, state='not_loaded', needs_load=True,
                        message='此对话尚未载入当前 Codex。点击“载入对话”后才可回复；查看窗口不会自动载入。')
        data = self.rpc('thread/read', {'threadId': identifier, 'includeTurns': False})
        row = data.get('thread', {})
        if not isinstance(row, dict) or row.get('id') != identifier:
            raise ControlError('Codex 返回的对话与所选项不一致')
        raw = row.get('status', {})
        kind = raw.get('type') if isinstance(raw, dict) else None
        if kind == 'notLoaded':
            return dict(base, state='not_loaded', needs_load=True, message='此对话已从 Codex 内存卸载，请重新载入。')
        if kind == 'idle':
            return dict(base, state='idle', can_send=True, active_turn_id=None,
                        message='可以回复；发送后会开始新一轮，保留当前模型和权限设置。')
        if kind == 'active':
            flags = raw.get('activeFlags', [])
            if flags:
                return dict(base, state='waiting', message='Codex 正在等待批准或回答，请先处理对应请求；这里不会自动批准。')
            activity = self.channel.chat_activity(identifier)
            active_id = activity.get('turn_id')
            valid = isinstance(active_id, str) and THREAD_ID.fullmatch(active_id) and activity.get('state') == 'active'
            return dict(base, state='active', active_turn_id=active_id if valid else None,
                        can_send=bool(valid), message='发送会追加到当前运行轮次。' if valid else '对话正在运行，但尚未观察到当前轮次标识，请稍后刷新。')
        return dict(base, state='unknown', message='当前对话状态暂不可确认，请刷新或在 Codex 中打开。')

    def load(self, params):
        identifier = thread_id(params, ('thread_id',))
        if identifier not in self.loaded():
            try:
                data = self.rpc('thread/resume', {'threadId': identifier, 'excludeTurns': True})
            except UnconfirmedUpdate:
                return {'thread_id': identifier, 'status': 'unverified', 'connected': True, 'supported': True, 'can_send': False,
                        'message': '载入请求已发送，确认尚未返回；请刷新状态，不会自动重试。'}
            thread = data.get('thread', {})
            if not isinstance(thread, dict) or thread.get('id') != identifier:
                return {'thread_id': identifier, 'status': 'unverified', 'connected': True, 'supported': True, 'can_send': False,
                        'message': '载入响应无法核对，请刷新状态。'}
        return self.status({'thread_id': identifier})

    @contextmanager
    def receipt_store(self):
        folder = self.home / 'control'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / 'chat-receipts.json'
        descriptor = os.open(folder / 'chat-receipts.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            rows = OrderedDict()
            if path.exists():
                if path.stat().st_size > 1024 * 1024:
                    raise ControlError('消息回执记录过大，请先在 Codex 中核对')
                try:
                    value = json.loads(path.read_text())
                    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('receipts'), dict):
                        raise ValueError()
                    rows.update(value['receipts'])
                except (OSError, ValueError, TypeError):
                    raise ControlError('消息回执记录暂不可读，为避免重复发送，请先在 Codex 中核对') from None

            def save():
                while len(rows) > MAX_RECEIPTS:
                    rows.popitem(last=False)
                fd, temporary = tempfile.mkstemp(prefix='.chat-receipts-', dir=folder)
                try:
                    os.fchmod(fd, 0o600)
                    with os.fdopen(fd, 'w') as stream:
                        json.dump({'version': 1, 'receipts': rows}, stream, ensure_ascii=False)
                        stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary, path)
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
            yield rows, save

    def send(self, params):
        identifier, text, request, expected = send_params(params)
        fingerprint = hashlib.sha256((identifier + '\x00' + text).encode()).hexdigest()
        with self.receipt_store() as (receipts, save):
            prior = receipts.get(request)
            if prior is not None:
                if not isinstance(prior, dict) or prior.get('fingerprint') != fingerprint:
                    raise ControlError('此消息标识已用于其他内容，请重新编辑后发送')
                return dict(prior['result'], duplicate=True)
            status = self.status({'thread_id': identifier})
            if not status.get('can_send'):
                return dict(status, status='failed', request_id=request)
            if status['state'] == 'active':
                if not expected or expected != status.get('active_turn_id'):
                    return dict(status, status='failed', request_id=request, message='运行轮次已变化，请刷新后再次确认发送。')
                method = 'turn/steer'
                rpc = {'threadId': identifier, 'expectedTurnId': expected,
                       'input': [{'type': 'text', 'text': text}], 'clientUserMessageId': request}
            else:
                if expected is not None:
                    return dict(status, status='failed', request_id=request, message='之前的运行轮次已结束，请刷新后发送新一轮。')
                method = 'turn/start'
                rpc = {'threadId': identifier, 'input': [{'type': 'text', 'text': text}], 'clientUserMessageId': request}
            result = {'schema_version': 1, 'thread_id': identifier, 'request_id': request,
                      'connected': True, 'supported': True, 'status': 'unverified',
                      'message': '发送结果尚未确认，请在对话中核对；不会自动重发。'}
            receipts[request] = {'fingerprint': fingerprint, 'result': result}
            save()  # Crash-safe receipt is committed before attempting a mutation.
            try:
                reply = self.rpc(method, rpc)
                turn = reply.get('turn', {}).get('id') if method == 'turn/start' else reply.get('turnId')
                if not isinstance(turn, str) or not THREAD_ID.fullmatch(turn) or (expected and turn != expected):
                    result = dict(result, message='Codex 已返回响应，但轮次无法核对；请查看对话，不会自动重发。')
                else:
                    result = dict(result, status='accepted', turn_id=turn, message='Codex 已接收消息。')
            except UnconfirmedUpdate:
                pass
            except ControlError:
                result = dict(result, status='failed', message='Codex 未接受此消息，请刷新状态后核对。')
            receipts[request]['result'] = result
            save()
            return result


def socket_call(connection, operation, params):
    request = json.dumps({'operation': 'chat_' + operation, 'params': params,
                          'deadline': time.monotonic() + 25}, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'
    if len(request) > MAX_INPUT_BYTES:
        raise ControlError('对话操作数据过大')
    sent = False
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(30); sock.connect(connection['socket_path']); sock.sendall(request); sent = True
            data = sock.makefile('rb').readline(MAX_INPUT_BYTES + 1)
            if not data.endswith(b'\n') or len(data) > MAX_INPUT_BYTES:
                raise ValueError()
            response = json.loads(data)
            if not isinstance(response, dict): raise ValueError()
            if response.get('error'):
                raise ControlError('Codex 连接未接受此对话操作，请刷新或重新接通。')
            return response
    except (OSError, ValueError, TypeError):
        if sent and operation in ('send', 'load'):
            return {'schema_version': 1, 'status': 'unverified', 'connected': False,
                    'thread_id': params.get('thread_id'), 'request_id': params.get('request_id'), 'can_send': False,
                    'message': '请求已提交，但本机连接中断；请核对对话，不会自动重试。'}
        raise ControlError('本机 Codex 对话连接已中断') from None


def run(operation, params, home=None):
    if operation not in ('status', 'load', 'send'):
        raise ControlError('不支持的对话操作')
    identifier = thread_id(params, ('thread_id',)) if operation in ('status', 'load') else send_params(params)[0]
    live = connections(home)
    capable = [value for value in live if value.get('chat_version') == CHAT_VERSION]
    if not capable:
        return dict(unavailable('legacy_bridge' if live else 'disconnected', bool(live)), thread_id=identifier,
                    status='failed' if operation != 'status' else 'unavailable')
    if len(capable) != len(live):
        return dict(unavailable('ambiguous_owner', True), thread_id=identifier, status='failed')
    owners, unloaded = [], []
    for connection in capable:
        try:
            state = socket_call(connection, 'status', {'thread_id': identifier})
        except ControlError:
            continue
        if state.get('state') == 'not_loaded': unloaded.append((connection, state))
        elif state.get('supported'): owners.append((connection, state))
    if len(owners) > 1:
        return dict(unavailable('ambiguous_owner', True), thread_id=identifier, status='failed')
    if owners:
        connection, state = owners[0]
    elif len(capable) == 1 and unloaded:
        connection, state = unloaded[0]
    else:
        return dict(unavailable('ambiguous_owner', True), thread_id=identifier, status='failed')
    if operation == 'status': return state
    if operation == 'send' and not state.get('can_send'): return dict(state, status='failed', request_id=params.get('request_id'))
    return socket_call(connection, operation, params)


def main(argv=None):
    parser = argparse.ArgumentParser(description='VibeCue 用户主动对话操作')
    parser.add_argument('operation', choices=('status', 'load', 'send'))
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES: raise ControlError('对话请求过大')
        result = run(args.operation, json.loads(raw))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ControlError, OSError, ValueError, TypeError):
        print(json.dumps({'status': 'failed', 'can_send': False, 'message': '对话请求暂不可用，请检查输入或重新接通 Codex。'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
