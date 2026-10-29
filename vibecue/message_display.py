"""Display-only decoding of explicit host question-reply envelopes.

Only a whole transport envelope is recognized. JSON examples, fenced code and
ordinary messages containing the marker are left intact.
"""
import json
import re

_QUESTION_REPLY = re.compile(r'\A\s*<send_user_message_question_reply>\s*([\s\S]*?)\s*</send_user_message_question_reply>\s*\Z')


def question_reply_text(value):
    if not isinstance(value, str):
        return value
    match = _QUESTION_REPLY.fullmatch(value)
    if match is None:
        return value
    if len(match.group(1)) > 65_536:
        return '问题回复内容较长，暂无法预览。'
    try:
        rows = json.loads(match.group(1))
        if not isinstance(rows, list) or not 0 < len(rows) <= 20:
            raise ValueError()
        if not all(isinstance(row, dict) and isinstance(row.get('answer'), str) for row in rows):
            raise ValueError()
        return '\n\n'.join((row.get('question', '') + '\n' if len(rows) > 1 and isinstance(row.get('question'), str) else '') + row['answer'] for row in rows)
    except (ValueError, TypeError, RecursionError):
        # Do not expose opaque transport IDs from a damaged envelope.
        return '问题回复暂无法预览。'
