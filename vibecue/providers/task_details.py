"""Bounded, ephemeral per-turn usage and visible conversation previews.

This is a live view, not another durable usage ledger. No tool arguments,
reasoning, system text or authentication source is read or retained here.
"""
from collections import OrderedDict, deque
import json
import re

from ..usage import codex_counts, fresh_counts, digest, label

MAX_MESSAGES = 8
MAX_THREAD_TEXT = 12_000
MAX_DETAIL_THREADS = 256
MAX_TEXT_THREADS = 48
MAX_GLOBAL_TEXT = 120_000
MAX_DETAIL_BYTES = 1_400_000
MAX_USAGE_IDENTITIES = 4096

_TOKEN_SECRET = re.compile(r'\b(?:sk-[A-Za-z0-9_-]{12,}|sk_(?:live|test)_[A-Za-z0-9]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b')
_AUTH_SECRET = re.compile(r'(?i)\b(authorization\s*[:=]\s*(?:bearer\s+)?|bearer\s+)[^\s\"\'<>]+')
_NAMED_SECRET = re.compile(r'(?i)\b((?:[a-z0-9]+_)*(?:api[_ -]?key|access[_ -]?token|auth[_ -]?token|client[_ -]?secret|secret[_ -]?key|password|passwd))\s*[:=]\s*[\"\']?[^\s\"\'<>]+')
_URL_SECRET = re.compile(r'(?i)(https?://[^/\s:@]+:)[^@\s/]+(@)')
_PRIVATE_KEY = re.compile(r'-----BEGIN [^-\n]*PRIVATE KEY-----[\s\S]*?-----END [^-\n]*PRIVATE KEY-----')


def visible_text(value):
    if not isinstance(value, str):
        return ''
    value = ''.join(c for c in value if c in '\n\t' or ord(c) >= 32)
    value = _TOKEN_SECRET.sub('[已隐藏凭据]', value)
    value = _AUTH_SECRET.sub(lambda match: match.group(1) + '[已隐藏凭据]', value)
    value = _URL_SECRET.sub(lambda match: match.group(1) + '[已隐藏凭据]' + match.group(2), value)
    value = _PRIVATE_KEY.sub('[已隐藏私钥]', value)
    return _NAMED_SECRET.sub(lambda match: match.group(1) + '=[已隐藏凭据]', value).strip()


def message_text(payload):
    content = payload.get('content')
    if isinstance(content, str):
        return visible_text(content)
    if not isinstance(content, list):
        return ''
    return visible_text('\n'.join(part['text'] for part in content
                                  if isinstance(part, dict) and part.get('type') in ('text', 'input_text', 'output_text')
                                  and isinstance(part.get('text'), str)))


class TaskDetails:
    def __init__(self):
        self.reset()

    def reset(self, window_truncated=False):
        self.turn_id = ''
        self.started_at = None
        self.finished_at = None
        self.last_activity = 0.0
        self.status = 'unknown'
        self.model = self.reasoning_effort = self.service_tier = ''
        self.messages = deque(maxlen=MAX_MESSAGES)
        self.content_truncated = False
        self._last_text_signature = None
        self._last_text_turn = ''
        self._window_truncated = window_truncated
        self._known_start = False
        self._partial_usage = False
        self._saw_usage = False
        self._counts = [0, 0, 0, 0]
        self._request_count = 0
        self._entries = OrderedDict()
        self._session_total = None
        self._last_signature = None
        self._buckets = {}
        self._pending_record = None
        self._watermark = 0.0

    def _begin(self, turn, timestamp, known):
        if self.started_at is not None and not (turn and self.turn_id and turn != self.turn_id) and self.status in ('running', 'waiting'):
            if known:
                self._known_start = True
                self.started_at = min(self.started_at, timestamp)
            self.turn_id = turn or self.turn_id
            return
        self.turn_id = turn or self.turn_id
        self.started_at = timestamp
        self.finished_at = None
        self.status = 'running'
        self._known_start = known
        self._partial_usage = False
        self._saw_usage = False
        self._counts = [0, 0, 0, 0]
        self._request_count = 0
        self._entries.clear()
        self._pending_record = None

    def _settings(self, payload):
        settings = payload.get('thread_settings')
        source = settings if isinstance(settings, dict) else payload
        collaboration = source.get('collaboration_mode')
        collaboration = collaboration.get('settings', {}) if isinstance(collaboration, dict) else {}
        if not isinstance(collaboration, dict):
            collaboration = {}
        for key, aliases in [('model', ('model',)), ('reasoning_effort', ('reasoning_effort', 'effort')),
                             ('service_tier', ('service_tier',))]:
            for candidate in (source, collaboration):
                for alias in aliases:
                    if isinstance(candidate.get(alias), str):
                        setattr(self, key, label(candidate[alias], 180))
                        break
                    if key != 'model' and alias in candidate and candidate[alias] is None:
                        setattr(self, key, 'default')
                        break

    def _message(self, role, text, timestamp, mirror=False):
        text = visible_text(text)
        if not text:
            return
        # Host-injected environment snapshots are not the human conversation.
        if role == 'user' and any(marker in text for marker in ('<environment_context>', '<permissions instructions>', '<turn_aborted>')):
            return
        signature = digest(role, text)
        if (self.messages and self._last_text_signature == signature and
                (self.messages[-1]['created_at'] == timestamp or (mirror and self._last_text_turn == self.turn_id))):
            return
        truncated = len(text) > MAX_THREAD_TEXT
        if truncated:
            text = '…（前文已截断）\n' + text[-(MAX_THREAD_TEXT - 12):]
        if len(self.messages) == MAX_MESSAGES:
            self.content_truncated = True
        self.messages.append({'role': role, 'text': text, 'created_at': timestamp, 'turn_id': self.turn_id, 'truncated': truncated})
        self._last_text_signature, self._last_text_turn = signature, self.turn_id
        self.content_truncated = self.content_truncated or truncated
        while sum(len(row['text']) for row in self.messages) > MAX_THREAD_TEXT and len(self.messages) > 1:
            self.messages.popleft()
            self.content_truncated = True

    def _add(self, key, counts):
        if key in self._entries or not any(counts):
            return
        self._entries[key] = counts
        self._request_count += 1
        self._counts = [self._counts[i] + counts[i] for i in range(4)]
        if len(self._entries) > MAX_USAGE_IDENTITIES:
            self._entries.popitem(last=False)
            self._partial_usage = True

    def _usage(self, category, payload, timestamp):
        if category == 'token_usage_record':
            usage = codex_counts(payload.get('usage'))
            if usage is None:
                self._partial_usage = True
                return
            self._saw_usage = True
            key = digest('record', payload.get('response_id') or timestamp, usage)
            self._add(key, fresh_counts(usage))
            self._pending_record = (key, usage)
            return
        info = payload.get('info')
        if not isinstance(info, dict):
            return
        total, last = codex_counts(info.get('total_token_usage')), codex_counts(info.get('last_token_usage'))
        observed_model = info.get('model') or info.get('model_name') or payload.get('model') or payload.get('model_name')
        if isinstance(observed_model, str):
            self.model = label(observed_model, 180)
        if ((info.get('total_token_usage') is not None and total is None) or
                (info.get('last_token_usage') is not None and last is None)):
            self._partial_usage = True
        if total is None and last is None:
            return
        self._saw_usage = True
        previous = self._session_total
        if total is not None:
            self._session_total = total
        key = digest(timestamp, total, last)
        repeat_key = digest(total, last) if total is not None else key
        limits = payload.get('rate_limits')
        bucket = label(limits.get('limit_id') if isinstance(limits, dict) else '', 100)
        repeated = repeat_key == self._last_signature or self._buckets.get(bucket) == repeat_key
        self._last_signature = repeat_key
        self._buckets[bucket] = repeat_key
        if len(self._buckets) > 32:
            self._buckets = {bucket: repeat_key}
        pending, self._pending_record = self._pending_record, None
        suppress_delta = False
        if pending:
            pending_key, pending_usage = pending
            if last is None:
                suppress_delta = True
                self._partial_usage = True
            elif last[:4] == pending_usage[:4]:
                removed = self._entries.pop(pending_key, None)
                if removed:
                    self._counts = [max(0, self._counts[i] - removed[i]) for i in range(4)]
                    self._request_count = max(0, self._request_count - 1)
            elif not any(last[:4]):
                suppress_delta = True
        if repeated:
            return
        if last is not None:
            counts = fresh_counts(last)
        elif suppress_delta:
            return
        elif total is not None and previous is not None and all(total[i] >= previous[i] for i in range(5)):
            delta = [total[i] - previous[i] for i in range(5)]
            delta[2] = min(delta[0], delta[2])
            delta[3] = min(max(0, delta[0] - delta[2]), delta[3])
            counts = fresh_counts(delta)
        else:
            # The first cumulative snapshot may contain months of old usage.
            # It is a baseline, never the current turn's first "request".
            self._partial_usage = True
            return
        self._add(key, counts)

    def observe(self, item, timestamp):
        payload = item.get('payload')
        if not isinstance(payload, dict) or timestamp is None or timestamp < self._watermark:
            return
        category, event_type = item.get('type'), payload.get('type')
        turn = label(payload.get('turn_id') or payload.get('root_turn_id'), 160)
        if category == 'session_meta':
            return
        if turn and self.turn_id and turn != self.turn_id and category == 'event_msg' and event_type in ('task_complete', 'task_completed', 'turn_aborted', 'task_aborted'):
            return
        self._watermark = timestamp
        self.last_activity = timestamp
        if category == 'turn_context' or (category == 'event_msg' and event_type == 'task_started'):
            self._begin(turn, timestamp, category != 'turn_context' or not self._window_truncated)
        if category == 'turn_context' or (category == 'event_msg' and event_type == 'thread_settings_applied'):
            self._settings(payload)
        if category == 'response_item' and event_type in ('function_call', 'custom_tool_call') and self.status == 'unknown':
            self._begin(turn, timestamp, False)
        if category == 'response_item' and event_type == 'message' and payload.get('role') in ('user', 'assistant'):
            role = payload['role']
            text = message_text(payload)
            self._message(role, text, timestamp)
            if role == 'assistant' and payload.get('phase') == 'commentary' and self.status == 'unknown':
                self._begin(turn, timestamp, False)
            if role == 'assistant' and payload.get('phase') == 'final_answer':
                self.status, self.finished_at = 'complete', timestamp
        if category == 'token_usage_record' or (category == 'event_msg' and event_type == 'token_count'):
            self._usage(category, payload, timestamp)
        if category == 'event_msg' and event_type in ('task_complete', 'task_completed', 'turn_aborted', 'task_aborted'):
            self.turn_id = turn or self.turn_id
            self.status = 'aborted' if event_type in ('turn_aborted', 'task_aborted') else 'complete'
            self.finished_at = timestamp
            if self.status == 'complete':
                self._message('assistant', payload.get('last_agent_message') or payload.get('last_assistant_message'), timestamp, mirror=True)

    def snapshot(self, thread_id, title, activity_state, now):
        if self.started_at is None and not self.messages and not self._saw_usage:
            return None
        status = activity_state if activity_state in ('running', 'waiting') else self.status
        end = self.finished_at if self.finished_at is not None and status not in ('running', 'waiting') else now
        elapsed = max(0.0, end - self.started_at) if self.started_at is not None else None
        last = self.messages[-1] if self.messages else {}
        return {'thread_id': thread_id, 'title': title, 'provider': 'codex', 'turn_id': self.turn_id,
                'status': status, 'started_at': self.started_at, 'finished_at': self.finished_at,
                'elapsed_seconds': elapsed, 'elapsed_status': 'complete' if self._known_start else 'partial',
                'last_activity': self.last_activity, 'model': self.model,
                'reasoning_effort': self.reasoning_effort, 'service_tier': self.service_tier,
                'total_tokens': sum(self._counts) if self._saw_usage else None,
                'request_count': self._request_count if self._saw_usage else None,
                'input_tokens': self._counts[0] if self._saw_usage else None,
                'output_tokens': self._counts[1] if self._saw_usage else None,
                'cache_read_tokens': self._counts[2] if self._saw_usage else None,
                'cache_creation_tokens': self._counts[3] if self._saw_usage else None,
                'tokens_status': ('complete' if self._known_start and not self._partial_usage else 'partial') if self._saw_usage else 'unknown',
                'last_message': last.get('text', ''), 'last_message_at': last.get('created_at'),
                'last_role': last.get('role', ''), 'messages': list(self.messages),
                'content_available': bool(self.messages), 'content_truncated': self.content_truncated}


def bounded_details(rows):
    """Keep numeric records even when old preview bodies exceed the budget."""
    selected = sorted(rows, key=lambda row: (-row.get('last_activity', 0), row['thread_id']))[:MAX_DETAIL_THREADS]
    result = {}
    remaining = MAX_GLOBAL_TEXT
    for index, row in enumerate(selected):
        row = dict(row)
        text_size = sum(len(message['text']) for message in row['messages']) + len(row['last_message'])
        if index >= MAX_TEXT_THREADS or text_size > remaining:
            row.update(messages=[], last_message='', content_available=False,
                       content_truncated=bool(row['messages']) or row['content_truncated'])
        else:
            remaining -= text_size
        result[row['thread_id']] = row
    # Covers escaped Unicode and unusually long titles/labels, not just chars.
    for row in reversed(list(result.values())):
        if len(json.dumps(result).encode()) <= MAX_DETAIL_BYTES:
            break
        row.update(messages=[], last_message='', content_available=False, content_truncated=True)
    return result


def bound_status_previews(state, maximum=1_800_000):
    """Protect the whole heartbeat's byte budget, including its diagnostics."""
    rows = list(state.get('task_details', {}).values())
    for row in reversed(rows):
        if len(json.dumps(state).encode()) <= maximum:
            break
        if row.get('messages') or row.get('last_message'):
            row.update(messages=[], last_message='', content_available=False, content_truncated=True)
    return state
