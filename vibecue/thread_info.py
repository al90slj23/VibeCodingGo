"""Bounded read-only conversation excerpts and verified local path metadata.

No owner is attached, turn resumed, auth file read or transcript changed. Text
is returned over stdout only; it is never saved in VibeCue's usage ledger.
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import sqlite3
import subprocess
import sys
import time

from .events import clean_text, is_main_source, state_databases
from .providers.task_details import message_text, visible_text
from .providers.codex import record_time
from .usage import zstd_program
from .message_display import question_reply_text

ID = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
INITIAL_TAIL = 2 * 1024 * 1024
MAX_TAIL = 8 * 1024 * 1024
MAX_MESSAGES = 60
MAX_CHARACTERS = 128_000
MAX_MESSAGE_CHARACTERS = 48_000
MAX_ACTIVITIES = 120
DECODE_TIMEOUT = 20
DISPLAY_SCHEMA = 2

# These blocks are host context, not messages typed by the person. Remove only
# explicit wrappers; a genuine request before/after such a block is retained.
_CONTEXT_TAGS = ('environment_context', 'turn_aborted', 'in-app-browser-context',
                 'external_codex_apps_open_page', 'codex_apps_open_page_context',
                 'permissions instructions', 'app-context', 'system-reminder',
                 'codex_apps_client_time_context', 'collaboration_mode',
                 'subagent_notification', 'background_task_notification',
                 'skills_instructions', 'codex_apps_open_page_instructions',
                 'image_resize_notice')
_CONTEXT = re.compile('|'.join(
    r'<'+re.escape(tag)+r'(?:\s[^>]*)?>[\s\S]*?</'+re.escape(tag)+r'\s*>'
    for tag in _CONTEXT_TAGS), re.I)
_HOST_ONLY = ('# AGENTS.md instructions for ', '<skills_instructions>',
              '<environment_context>', '<permissions instructions>',
              '<in-app-browser-context', '<turn_aborted>',
              '<external_codex_apps_open_page>')
_INTERNAL = re.compile(r'<oai-mem-citation(?:\s[^>]*)?>[\s\S]*?</oai-mem-citation\s*>', re.I)
_FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')
_FILE_HEADER = '# Files mentioned by the user:'
_FILE_ROW = re.compile(r'^## (.+?): (/[^\n]+)\n(?:Image attachment: (true|false)\n)?', re.M)
_IMAGE_WRAPPER = re.compile(r'<image\b[^>]*\bpath="([^"\n]+)"[^>]*>\s*</image\s*>', re.I)
_TOOL_NAME = re.compile(r'[A-Za-z0-9_.:-]{1,160}\Z')


class ThreadInfoError(ValueError):
    pass


def thread_id(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ThreadInfoError('对话标识无效')
    return value


def absolute_path(value):
    if not isinstance(value, str) or not value or len(value) > 8192 or '\x00' in value:
        return None
    path = Path(value)
    return str(path) if path.is_absolute() else None


def metadata(identifier, codex_home=None):
    identifier = thread_id(identifier)
    for database in state_databases(codex_home):
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)) as conn:
                conn.execute('PRAGMA query_only=ON')
                columns = {r[1] for r in conn.execute('PRAGMA table_info(threads)')}
                if not {'id', 'rollout_path', 'title'}.issubset(columns):
                    continue
                selected = [key if key in columns else "NULL AS " + key
                            for key in ('id', 'title', 'name', 'cwd', 'rollout_path', 'source', 'archived', 'updated_at')]
                conn.row_factory = sqlite3.Row
                found = conn.execute('SELECT '+','.join(selected)+' FROM threads WHERE id=?', (identifier,)).fetchone()
            if found is None:
                raise ThreadInfoError('本机尚未找到这个对话的记录')
            found = dict(found)
            if not is_main_source(found.get('source', '')):
                raise ThreadInfoError('此记录属于子代理，不能作为主对话打开')
            updated = found.get('updated_at')
            return {'thread_id': identifier, 'title': clean_text(found.get('name') or found.get('title'), 300) or 'Codex 对话',
                    'cwd': absolute_path(found.get('cwd')), 'transcript_path': absolute_path(found.get('rollout_path')),
                    'archived': bool(found.get('archived')), 'updated_at': updated if isinstance(updated, (int, float)) and math.isfinite(updated) else None}
        except (OSError, sqlite3.Error):
            continue
    raise ThreadInfoError('本机对话索引暂时无法读取')


def _source(path):
    if not path:
        raise ThreadInfoError('此对话尚未记录本机日志路径')
    source = Path(path)
    # A malformed metadata row must not turn this viewer into an arbitrary
    # file reader (especially an authorization/configuration file reader).
    if not str(source).endswith(('.jsonl', '.jsonl.zst')):
        raise ThreadInfoError('此记录不是支持的 Codex 会话日志路径')
    if source.suffix == '.zst' and source.is_file():
        if not str(source.resolve()).endswith(('.jsonl', '.jsonl.zst')):
            raise ThreadInfoError('会话日志链接目标不是支持的记录格式')
        return source, True
    if source.is_file():
        if not str(source.resolve()).endswith(('.jsonl', '.jsonl.zst')):
            raise ThreadInfoError('会话日志链接目标不是支持的记录格式')
        return source, False
    compressed = source.with_name(source.name + '.zst')
    if compressed.is_file():
        if not str(compressed.resolve()).endswith(('.jsonl', '.jsonl.zst')):
            raise ThreadInfoError('会话日志链接目标不是支持的记录格式')
        return compressed, True
    raise ThreadInfoError('会话日志当前不存在，仍可在 Codex 中尝试打开')


def _fingerprint(info, source):
    stat = source.stat()
    values = (str(source), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns,
              info.get('title'), info.get('cwd'), info.get('updated_at'), info.get('archived'), DISPLAY_SCHEMA)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _plain_tail(source, limit):
    with source.open('rb') as stream:
        size = stream.seek(0, 2)
        offset = max(0, size-limit)
        stream.seek(offset)
        data = stream.read(limit)
    return data, size, bool(offset)


def _compressed_tail(source, limit=MAX_TAIL, timeout=DECODE_TIMEOUT):
    program = zstd_program()
    if not program:
        raise ThreadInfoError('旧会话已压缩；本机没有可用的 zstd 解码器')
    process = subprocess.Popen([program, '-dq', '-c', '--', str(source)], stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin:/opt/homebrew/bin', 'LC_ALL': 'C'})
    chunks, kept, total = deque(), 0, 0
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ThreadInfoError('旧会话解码超时；保留原记录，请在 Codex 中打开')
            chunk = os.read(process.stdout.fileno(), 256 * 1024)
            if not chunk:
                break
            total += len(chunk)
            chunks.append(chunk); kept += len(chunk)
            while chunks and kept-len(chunks[0]) >= limit:
                kept -= len(chunks.popleft())
        if process.wait(timeout=max(.05, deadline-time.monotonic())) != 0:
            raise ThreadInfoError('旧会话压缩日志暂时无法解码')
        data = b''.join(chunks)[-limit:]
        return data, total, total > len(data)
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()


def _records(data, partial):
    if partial:
        data = data.split(b'\n', 1)[-1] if b'\n' in data else b''
    incomplete = bool(data and not data.endswith(b'\n'))
    lines = data.split(b'\n')
    if incomplete:
        lines.pop()
    malformed, result = 0, []
    for line in lines:
        if not line:
            continue
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                result.append(item)
            else:
                malformed += 1
        except (ValueError, UnicodeError, RecursionError):
            malformed += 1
    return result, malformed, incomplete


def _outside_fences(value, transform):
    """Internal display markers are hidden, but quoted source code survives."""
    output, outside, fence = [], [], None
    for line in value.splitlines(keepends=True):
        match = _FENCE.match(line)
        if fence is None and match:
            output.append(transform(''.join(outside))); outside = []
            fence = match.group(1); output.append(line)
        elif fence is not None:
            output.append(line)
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
        else:
            outside.append(line)
    output.append(transform(''.join(outside)))
    return ''.join(output)


def display_text(value):
    value = _outside_fences(value, lambda text: _INTERNAL.sub('', text)).strip()
    return visible_text(value)


def user_content(value):
    value = _outside_fences(value, lambda text: _CONTEXT.sub('', text)).strip()
    if not value or value.startswith(_HOST_ONLY):
        return '', []
    value = question_reply_text(value)
    attachments = []
    if value.startswith(_FILE_HEADER) and '\n## My request:' in value:
        header, value = value.split('\n## My request:', 1)
        for match in _FILE_ROW.finditer(header[:32_768]):
            name, path, image = match.groups()
            path = absolute_path(path.strip())
            if path and len(attachments) < 8:
                attachments.append({'name': clean_text(name, 240), 'path': path,
                                    'kind': 'image' if image == 'true' else 'file'})
        known_paths = {row['path'] for row in attachments}
        value = _outside_fences(value, lambda text: _IMAGE_WRAPPER.sub(
            lambda match: '' if match.group(1) in known_paths else match.group(0), text))
    return display_text(value), attachments


def user_text(value):
    return user_content(value)[0]


def _tool_label(name):
    short = name.replace('__', '.').rsplit('.', 1)[-1]
    if short in ('exec_command', 'write_stdin'):
        return '运行命令'
    if short in ('read_file', 'read_text', 'view_image', 'read_mcp_resource'):
        return '读取文件'
    if short in ('apply_patch', 'write_file', 'edit_file'):
        return '修改文件'
    if short in ('spawn_agent', 'send_message', 'followup_task', 'wait_agent'):
        return '协作处理'
    if short in ('request_user_input', 'request_user_input_async'):
        return '等待你的回答'
    if 'web' in name or 'search' in short:
        return '搜索资料'
    if short in ('js', 'exec'):
        return '使用工具'
    return '调用工具'


def conversation(records, include_activities=False):
    messages, current_turn, status = [], '', 'unknown'
    last_activity, last_terminal, truncated = None, 0, False
    malformed = 0
    waiting = set()
    activities, activity_by_call = [], {}
    for item in records:
        payload = item.get('payload')
        if not isinstance(payload, dict):
            continue
        at = record_time(item)
        if at is None:
            malformed += 1
            continue
        category, kind = item.get('type'), payload.get('type')
        turn = clean_text(payload.get('turn_id') or payload.get('root_turn_id'), 160)
        if category == 'event_msg' and kind == 'task_started':
            current_turn = turn or current_turn; status = 'running'; last_activity = at
            waiting.clear()
        elif category == 'turn_context':
            current_turn = turn or current_turn
        elif category == 'event_msg' and kind in ('task_complete', 'task_completed', 'turn_aborted', 'task_aborted'):
            if current_turn and turn and current_turn != turn:
                continue
            current_turn = turn or current_turn
            status = 'interrupted' if kind in ('turn_aborted', 'task_aborted') else 'complete'
            last_activity = last_terminal = at
            waiting.clear()
        role, text, mirror, identifier, phase, attachments = None, '', False, '', 'final_answer', []
        if category == 'response_item' and kind == 'message' and payload.get('role') in ('user', 'assistant'):
            role = payload['role']; text = message_text(payload)
            if role == 'user':
                text, attachments = user_content(text)
            else:
                text = display_text(text)
                phase = payload.get('phase') if payload.get('phase') in ('commentary', 'final_answer') else 'final_answer'
            identifier = clean_text(payload.get('id'), 180)
            if role == 'assistant' and payload.get('phase') == 'final_answer':
                status = 'complete'; last_terminal = at
        elif category == 'event_msg' and kind in ('agent_message', 'user_message'):
            role = 'assistant' if kind == 'agent_message' else 'user'
            text = str(payload.get('message') or '')
            if role == 'user':
                text, attachments = user_content(text)
            else:
                text = display_text(text)
                phase = payload.get('phase') if payload.get('phase') in ('commentary', 'final_answer') else 'final_answer'
            mirror = True
        elif category == 'event_msg' and kind in ('task_complete', 'task_completed'):
            role, text, mirror = 'assistant', display_text(payload.get('last_agent_message') or payload.get('last_assistant_message') or ''), True
        if category == 'response_item' and kind in ('function_call', 'custom_tool_call') and at > last_terminal:
            name = str(payload.get('name') or '').split('.')[-1]
            call = str(payload.get('call_id') or payload.get('id') or '')
            safe_name = str(payload.get('name') or '')
            effective_turn = turn or current_turn
            activity_key = (effective_turn, call)
            if call and len(call) <= 512 and _TOOL_NAME.fullmatch(safe_name) and activity_key not in activity_by_call:
                activity = {'id': hashlib.sha256(json.dumps([effective_turn, call]).encode()).hexdigest()[:32],
                            'turn_id': effective_turn, 'name': safe_name, 'label': _tool_label(safe_name),
                            'status': 'running', 'created_at': at}
                activities.append(activity); activity_by_call[activity_key] = activity
                while len(activities) > MAX_ACTIVITIES:
                    removed = activities.pop(0)
                    activity_by_call = {key: value for key, value in activity_by_call.items() if value is not removed}
                    truncated = True
            if kind == 'function_call' and name == 'request_user_input' and call:
                waiting.add(call)
            status = 'waiting' if waiting else 'running'; last_activity = at
        elif category == 'response_item' and kind in ('function_call_output', 'custom_tool_call_output', 'tool_result'):
            call = str(payload.get('call_id') or payload.get('tool_call_id') or payload.get('id') or '')
            activity = activity_by_call.get((turn or current_turn, call))
            if activity is not None:
                activity.update(status='complete', finished_at=at)
            if call in waiting:
                waiting.discard(call); status = 'waiting' if waiting else 'running'; last_activity = at
        if not role or not text and not attachments:
            continue
        last_activity = at
        effective_turn = turn or current_turn
        text_key = hashlib.sha256(text.encode()).hexdigest()
        if len(text) > MAX_MESSAGE_CHARACTERS:
            text = '…（前文已截断）\n' + text[-MAX_MESSAGE_CHARACTERS:]; truncated = True
        if identifier:
            old = next((r for r in reversed(messages) if r.get('id') == identifier and r['turn_id'] == effective_turn and r['role'] == role), None)
            if old is not None:
                old.update(text=text, phase='user' if role == 'user' else phase,
                           attachments=attachments, _text_key=text_key)
                while sum(len(row['text']) for row in messages) > MAX_CHARACTERS:
                    messages.pop(0); truncated = True
                continue
        duplicate = next((row for row in reversed(messages)
                          if row['role'] == role and row['turn_id'] == effective_turn
                          and row.get('_text_key') == text_key
                          and not (attachments and row.get('attachments') and attachments != row['attachments'])
                          and (mirror or row.get('_mirror') or row['created_at'] == at)), None)
        if duplicate is not None:
            if role == 'assistant' and phase == 'final_answer':
                duplicate['phase'] = phase
            if not mirror and duplicate.get('_mirror'):
                duplicate.update(_mirror=False, phase='user' if role == 'user' else phase,
                                 attachments=attachments)
                if identifier:
                    duplicate['id'] = identifier
            if attachments and not duplicate.get('attachments'):
                duplicate['attachments'] = attachments
            continue
        messages.append({'role': role, 'phase': 'user' if role == 'user' else phase,
                         'text': text, 'attachments': attachments, 'created_at': at, 'turn_id': effective_turn,
                         'id': identifier or hashlib.sha256(json.dumps([role, effective_turn, at, text], ensure_ascii=False).encode()).hexdigest()[:32],
                         '_mirror': mirror, '_text_key': text_key})
        while len(messages) > MAX_MESSAGES or sum(len(row['text']) for row in messages) > MAX_CHARACTERS:
            messages.pop(0); truncated = True
    for row in messages:
        row.pop('_mirror', None)
        row.pop('_text_key', None)
    result = (messages, current_turn, status, last_activity, truncated, malformed)
    return result + (activities,) if include_activities else result


def read(identifier, codex_home=None, if_fingerprint=None, metadata_only=False):
    info = metadata(identifier, codex_home)
    result = {**info, 'actual_transcript_path': None, 'source_readable': False,
              'messages': [], 'activities': [], 'last_turn_id': '', 'state': 'unknown', 'last_activity': None,
              'coverage': {'partial': True, 'truncated': False, 'compressed': False, 'malformed_records': 0},
              'message': ''}
    try:
        source, compressed = _source(info['transcript_path'])
        result.update(actual_transcript_path=str(source), fingerprint=_fingerprint(info, source))
        result['coverage']['compressed'] = compressed
        if metadata_only or (isinstance(if_fingerprint, str) and if_fingerprint == result['fingerprint']):
            result.update(unchanged=not metadata_only, metadata_only=bool(metadata_only), source_readable=True)
            return result
        if compressed:
            data, size, partial = _compressed_tail(source)
        else:
            data, size, partial = _plain_tail(source, INITIAL_TAIL)
            records, _, _ = _records(data, partial)
            # The detail pane is a recent excerpt, not a whole-log history
            # export. Expand only when a large context row hides visible text.
            if partial and not any(r.get('type') == 'response_item' and isinstance(r.get('payload'), dict) and r['payload'].get('type') == 'message' for r in records):
                data, size, partial = _plain_tail(source, MAX_TAIL)
        records, malformed, incomplete = _records(data, partial)
        messages, turn, status, last, truncated, extra_malformed, activities = conversation(records, include_activities=True)
        result.update(source_readable=True, messages=messages, activities=activities, last_turn_id=turn, state=status, last_activity=last)
        result['coverage'].update(partial=bool(partial), truncated=bool(truncated), malformed_records=malformed+extra_malformed,
                                  incomplete_last_record=incomplete, bytes_read=len(data), source_bytes=size)
        if not messages:
            result['message'] = '当前读取范围内尚无可显示的用户或助手消息。'
        elif partial or truncated:
            result['message'] = '显示本机保留的最近对话片段；更早内容请在 Codex 中查看。'
        if result['fingerprint'] != _fingerprint(info, source):
            # A subsequent refresh must not believe an incomplete concurrent
            # read is the complete version of this file.
            result.pop('fingerprint', None)
            result['coverage']['partial'] = True
        return result
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        result['message'] = str(error) if isinstance(error, ThreadInfoError) else '会话日志暂时无法读取；原始记录未修改。'
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='VibeCue 本机对话详情与路径，只读')
    parser.add_argument('command', choices=('read',))
    parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ThreadInfoError('对话查询参数过大')
        params = json.loads(raw)
        if not isinstance(params, dict) or set(params)-{'thread_id', 'if_fingerprint', 'metadata_only'}:
            raise ThreadInfoError('对话查询参数无效')
        if 'metadata_only' in params and not isinstance(params['metadata_only'], bool):
            raise ThreadInfoError('对话查询参数无效')
        print(json.dumps(read(params.get('thread_id'), if_fingerprint=params.get('if_fingerprint'),
                              metadata_only=params.get('metadata_only', False)), ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError) as error:
        message = str(error) if isinstance(error, ThreadInfoError) else '本机对话详情暂时不可用'
        print(json.dumps({'source_readable': False, 'error': message}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
