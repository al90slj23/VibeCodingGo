"""Private event spool shared by the watcher and notification adapters."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone

KINDS = frozenset(('complete', 'input', 'permission', 'error'))


def vibecue_directory(vibecue_home=None):
    return Path(vibecue_home or os.environ.get('VIBECUE_HOME') or
                (Path.home() / 'Library/Application Support/VibeCue')).expanduser()


def codex_directory(codex_home=None):
    return Path(codex_home or os.environ.get('CODEX_HOME') or (Path.home() / '.codex')).expanduser()


def state_databases(codex_home=None):
    """Prefer the newest schema version numerically (10 sorts above 5)."""
    home = codex_directory(codex_home)
    def version(path):
        match = re.fullmatch(r'state_(\d+)\.sqlite', path.name)
        return int(match.group(1)) if match else -1
    return sorted(home.glob('state_*.sqlite'), key=version, reverse=True)


def completion_event_id(thread_id, turn_id):
    """Same key whether a completed turn is displayed as complete or needs input."""
    return f'finished:{thread_id}:{turn_id}'


def clean_text(value, limit=160):
    return ' '.join(str(value or '').replace('\x00', '').split())[:limit]


def resolve_title(thread_id, codex_home=None):
    """Resolve the user's conversation name, reading only bounded local metadata."""
    home = codex_directory(codex_home)
    for db in state_databases(home):
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True,
                                         timeout=0.1)) as connection:
                columns = {r[1] for r in connection.execute('PRAGMA table_info(threads)')}
                fields = 'name, title' if 'name' in columns else 'title'
                row = connection.execute(f'SELECT {fields} FROM threads WHERE id=?',
                                         (thread_id,)).fetchone()
                if row:
                    for value in row:
                        if value and clean_text(value):
                            return clean_text(value, 160)
        except (OSError, sqlite3.Error):
            continue
    index = home / 'session_index.jsonl'
    try:
        with index.open('rb') as stream:
            size = index.stat().st_size
            stream.seek(max(0, size - 2_000_000))
            if size > 2_000_000:
                stream.readline()
            for line in reversed(stream.readlines()[-5000:]):
                try:
                    item = json.loads(line)
                    if item.get('id') == thread_id:
                        title = item.get('thread_name') or item.get('title')
                        if title:
                            return clean_text(title, 160)
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass
    return f'Codex 對話 {str(thread_id)[:8]}' if thread_id else 'Codex 對話'


# An explicit request to the user in the final paragraph is a heuristic, never
# proof of a protocol-level pending input. Avoid broad question-mark matching.
_ACTION_REQUEST = re.compile(
    r'(?:請|请)(?:你|您)?(?:先)?(?:確認|确认|選擇|选择|決定|决定|提供|回覆|回复|回答|批准|授權|授权|告訴我|告诉我)'
    r'|(?:需要|等候|等待)(?:你|您)(?:的)?(?:確認|确认|選擇|选择|決定|决定|輸入|输入|回覆|回复|回答|批准|授權|授权|判斷|判断)'
    r'|\b(?:please\s+(?:confirm|choose|decide|provide|reply|answer|approve)|'
    r'(?:can|could|would)\s+you\s+(?:confirm|choose|decide|provide|approve)|'
    r'(?:need|waiting\s+for)\s+your\s+(?:input|confirmation|approval|decision|answer))\b', re.I)
_NEGATED_REQUEST = re.compile(r'(?:無需|无需|不用|不必|不需要).{0,12}(?:確認|确认|選擇|选择|回覆|回复|批准|授權|授权)|\b(?:no\s+(?:input|confirmation|approval)\s+(?:is\s+)?(?:needed|required)|you\s+(?:do\s+not|don.t)\s+need\s+to)\b',re.I)


def classify_final_message(text):
    """Return display kind and a safe explanatory message; no model/API inference."""
    text = str(text or '')
    # Code samples and quoted requests are not actionable user questions.
    text = re.sub(r'```.*?```', '', text, flags=re.S)
    paragraphs = [p.strip() for p in text.strip().split('\n\n') if p.strip()]
    tail = paragraphs[-1][-700:] if paragraphs else ''
    tail = '\n'.join(l for l in tail.splitlines() if not l.lstrip().startswith('>'))
    if _ACTION_REQUEST.search(tail) and not _NEGATED_REQUEST.search(tail):
        return 'input', '回覆可能需要你的確認或選擇（依回覆文字判斷）'
    return 'complete', '這一輪已結束'


def prune_event_spool(spool, now=None, retain=500):
    """Bound unread transient titles if the display app has been offline."""
    now = time.time() if now is None else now
    recent = []
    for path in Path(spool).glob('*.json'):
        try:
            modified = path.stat().st_mtime
            if now - modified > 120:
                path.unlink(missing_ok=True)
            else:
                recent.append((modified, path))
        except OSError:
            continue
    for _, path in sorted(recent, key=lambda item: (item[0], item[1].name), reverse=True)[retain:]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def emit_event(kind, thread_id, turn_id='', title='', message='', event_id='',
               source='watcher', vibecue_home=None, provider='codex'):
    """Atomically enqueue once across concurrent watcher/hook processes.

    Returns False for an already emitted ID. Only this tool's private state is
    written. This function never sends a decision or response to Codex.
    """
    if kind not in KINDS:
        raise ValueError(f'Unsupported event kind: {kind}')
    home = vibecue_directory(vibecue_home)
    if (home / 'disabled').exists():
        return False
    spool = home / 'events'
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    spool.mkdir(mode=0o700, exist_ok=True)
    # Avoid a new SQLite journal inheriting an overly permissive process umask.
    database = home / 'emitted.sqlite'
    if not database.exists():
        try:
            fd = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
    now = time.time()
    raw_id = event_id or f'{kind}:{thread_id}:{turn_id}'
    if not thread_id and not turn_id and not event_id:
        raw_id += f':{time.time_ns()}'
    provider = clean_text(provider, 64).lower()
    identifier = hashlib.sha256((provider + ':' + raw_id).encode()).hexdigest()
    event = {
        'id': identifier, 'kind': kind, 'provider': provider,
        'title': clean_text(title or resolve_title(thread_id), 160),
        'thread_id': clean_text(thread_id, 160),
        'message': clean_text(message, 240),
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source': source,
    }
    with closing(sqlite3.connect(database, timeout=1)) as connection, connection:
        connection.execute('CREATE TABLE IF NOT EXISTS emitted (id TEXT PRIMARY KEY, at REAL NOT NULL)')
        connection.execute('BEGIN IMMEDIATE')
        prune_event_spool(spool, now, retain=499)
        if connection.execute('SELECT 1 FROM emitted WHERE id=?', (identifier,)).fetchone():
            return False
        fd, temporary = tempfile.mkstemp(prefix='.event-', dir=spool)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(event, stream, ensure_ascii=False)
                stream.write('\n')
            destination = spool / f'{time.time_ns():020d}-{identifier[:16]}.json'
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        connection.execute('INSERT INTO emitted VALUES (?,?)', (identifier, now))
        connection.execute('DELETE FROM emitted WHERE at < ?', (now - 172800,))
        connection.execute('DELETE FROM emitted WHERE id IN (SELECT id FROM emitted ORDER BY at DESC LIMIT -1 OFFSET 10000)')
    return True


def input_event_id(thread_id, turn_id='', call_id=''):
    return f'input:{thread_id}:{turn_id}:{call_id}'


def is_main_source(source):
    if isinstance(source, dict):
        return 'subagent' not in source
    try:
        decoded = json.loads(str(source))
        return not isinstance(decoded, dict) or 'subagent' not in decoded
    except (ValueError, TypeError):
        return 'subagent' not in str(source).lower()


def is_main_thread(thread_id, transcript_path=None, codex_home=None):
    """Prefer a transcript's own source: a subagent hook may pass the parent ID."""
    if transcript_path:
        try:
            with Path(transcript_path).open('rb') as stream:
                for _ in range(4):
                    line = stream.readline(131072)
                    if not line:
                        break
                    item = json.loads(line)
                    if item.get('type') == 'session_meta':
                        return is_main_source(item.get('payload', {}).get('source', ''))
        except (OSError, ValueError, TypeError):
            pass
    home = codex_directory(codex_home)
    for db in state_databases(home):
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as connection:
                row = connection.execute('SELECT source FROM threads WHERE id=?', (thread_id,)).fetchone()
                if row:
                    return is_main_source(row[0])
        except (OSError, sqlite3.Error):
            continue
    return True
