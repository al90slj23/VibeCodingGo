"""Read-only OpenCode message usage, without opening parts or credential tables.

The v1 ``message`` and v2 ``session_message`` tables can coexist during a
migration. A message ID identifies one usage snapshot across both tables,
database copies and the older storage/message JSON files. Source changes cause
a numeric projection scan, rather than a timestamp high-water mark: restored
historical messages may retain timestamps older than the previous scan.

Token semantics: OpenCode stores fresh input, visible output, separate
reasoning, and cache read/write. Its canonical total adds all five categories.
See packages/core/src/session/usage.ts and packages/schema/src/token-usage.ts
in https://github.com/anomalyco/opencode. We include reasoning in the ledger's
output column once; the session or step-finish aggregate is never added again.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3


IMPORT_VERSION = 1
MAX_JSON_BYTES = 8 * 1024 * 1024


def discover(root):
    """Return supported usage files only, never arbitrary configuration JSON."""
    root = Path(root).expanduser().resolve()
    if root.is_file():
        # OPENCODE_DB is an explicit file override and may have no extension.
        return [('sqlite', root)]
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.glob('opencode*.db')):
        if path.is_file():
            found.append(('sqlite', path.resolve()))
    # The oldest OpenCode layout was storage/message/<session>/<message>.json.
    # Accept the data root, storage root, or message directory explicitly.
    legacy = root if root.name == 'message' else root / 'message' if root.name == 'storage' else root / 'storage/message'
    if legacy.is_dir():
        found.extend(('json', path.resolve()) for path in sorted(legacy.rglob('*.json')) if path.is_file())
    return list(dict.fromkeys(found))


def _fingerprint(path, kind):
    entries = []
    for candidate in [path] + ([Path(str(path) + '-wal')] if kind == 'sqlite' else []):
        try:
            stat = candidate.stat()
            entries.append([stat.st_ino, stat.st_size, stat.st_mtime_ns])
        except FileNotFoundError:
            entries.append(None)
    return json.dumps([IMPORT_VERSION, kind, entries], separators=(',', ':'))


def normalize_message(value, message_id=None, session_id=None, created=None, role=None):
    """Produce only identifiers, model/source labels, time and token numbers."""
    # Lazy import avoids a dependency cycle when usage.sync loads this adapter.
    from .usage import number, stamp, label, digest
    if not isinstance(value, dict):
        return None
    message_role = role or value.get('role') or value.get('type')
    if message_role not in ('assistant', 'compaction'):
        return None
    if message_role == 'compaction' and value.get('status') not in ('completed', 'failed'):
        return None
    tokens = value.get('tokens')
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get('cache', {})
    if not isinstance(cache, dict):
        return None
    visible = tokens.get('output')
    # Some v1 snapshots persist a negative visible category alongside positive
    # reasoning. Accept that observed old shape
    # only if the merged output is nonnegative AND the explicit stored total
    # exactly corroborates all categories. Do not clamp a negative component
    # to zero: that would increase this recorded usage by its absolute value.
    negative_visible = isinstance(visible, (int, float)) and not isinstance(visible, bool) and visible < 0
    parsed_visible = number(-visible) if negative_visible else number(visible)
    if negative_visible and parsed_visible is not None:
        parsed_visible = -parsed_visible
    counts = [number(tokens.get('input')), parsed_visible,
              number(tokens.get('reasoning', 0)), number(cache.get('read', 0)),
              number(cache.get('write', 0))]
    if any(value is None for value in counts):
        return None
    # Both v1's stored total and v2's canonical TokenUsage total include
    # reasoning separately from visible output. Input is already non-cached.
    counts = [counts[0], counts[1] + counts[2], counts[3], counts[4]]
    if counts[1] < 0 or (negative_visible and number(tokens.get('total')) != sum(counts)):
        return None
    if not any(counts):
        return None
    identifier = label(message_id or value.get('id'), 250)
    session = label(session_id or value.get('sessionID'), 200)
    time = value.get('time') if isinstance(value.get('time'), dict) else {}
    timestamp = stamp(time.get('created', created))
    if not identifier or not session or timestamp is None:
        return None
    model = value.get('model') if isinstance(value.get('model'), dict) else {}
    model_id = label(value.get('modelID') or model.get('id') or (value.get('model') if isinstance(value.get('model'), str) else None), 180) or 'unknown'
    provider = label(value.get('providerID') or model.get('providerID'), 180)
    return {'id': 'opencode-message:' + digest(identifier), 'timestamp': timestamp,
            'model': model_id, 'thread_id': session, 'counts': counts,
            'legacy_negative_output': negative_visible,
            'source': 'opencode_provider:' + provider if provider else 'opencode_session'}


def _database_rows(path, report):
    # mode=ro is essential: immutable=1 would ignore an active WAL and miss new
    # requests. query_only adds a second guard against accidental SQL writes.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as source:
        source.row_factory = sqlite3.Row
        source.execute('PRAGMA query_only=ON')
        source.execute('BEGIN')
        tables = {row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('message','session_message')")}
        if not tables:
            raise ValueError('OpenCode 数据库没有受支持的消息表')
        # Read legacy first; v2 is the current representation during migration.
        for table in ('message', 'session_message'):
            if table not in tables:
                continue
            columns = {row[1] for row in source.execute('PRAGMA table_info(' + table + ')')}
            if not {'id', 'session_id', 'time_created', 'data'}.issubset(columns):
                raise ValueError('OpenCode 消息表结构尚不受支持')
            bad = source.execute('SELECT COUNT(*) FROM ' + table + ' WHERE NOT json_valid(data)').fetchone()[0]
            report['malformed_records'] = report.get('malformed_records', 0) + bad
            role = "type" if table == 'session_message' and 'type' in columns else "json_extract(data,'$.role')"
            # No SELECT data: project only the usage and identity fields inside
            # SQLite. v2 data can contain the full assistant text and tools.
            query = '''SELECT id,session_id,time_created,''' + role + ''' AS message_role,
                json_extract(data,'$.tokens','$.time.created','$.modelID',
                    '$.providerID','$.model.id','$.model.providerID','$.status') AS usage,
                CASE WHEN json_type(data,'$.model')='text' THEN json_extract(data,'$.model') END AS model_string
                FROM ''' + table + ''' WHERE json_valid(data) AND ''' + role + " IN ('assistant','compaction') ORDER BY id"
            for row in source.execute(query):
                tokens, created, model_id, provider_id, v2_model, v2_provider, status = json.loads(row['usage'])
                yield {'role': row['message_role'], 'status': status, 'id': row['id'], 'sessionID': row['session_id'],
                       'tokens': tokens, 'time': {'created': created if created is not None else row['time_created']},
                       'modelID': model_id or v2_model or row['model_string'], 'providerID': provider_id or v2_provider}


def scan(conn, path, report, writer, kind='sqlite'):
    """Import one source inside the caller's transaction using its row writer.

    writer(conn, id, timestamp, 'opencode', model, session, counts, source=...)
    must upsert by the supplied stable ID and return imported/updated/skipped.
    No source cursor is advanced if the caller rolls back a failed import.
    """
    path = Path(path).expanduser().resolve()
    if kind not in ('sqlite', 'json'):
        raise ValueError('不支持的 OpenCode 用量来源')
    conn.execute('''CREATE TABLE IF NOT EXISTS usage_opencode_files (
        path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)''')
    before = _fingerprint(path, kind)
    saved = conn.execute('SELECT fingerprint FROM usage_opencode_files WHERE path=?', (str(path),)).fetchone()
    if saved is not None and saved[0] == before:
        return
    if kind == 'sqlite':
        records = _database_rows(path, report)
    else:
        if path.stat().st_size > MAX_JSON_BYTES:
            report['oversized_records'] = report.get('oversized_records', 0) + 1
            return
        try:
            records = [json.loads(path.read_text())]
        except (ValueError, UnicodeError):
            report['malformed_records'] = report.get('malformed_records', 0) + 1
            return
    for value in records:
        record = normalize_message(value)
        if record is None:
            report['skipped'] = report.get('skipped', 0) + 1
            continue
        if record['legacy_negative_output']:
            report['opencode_legacy_output_records'] = report.get('opencode_legacy_output_records', 0) + 1
        result = writer(conn, record['id'], record['timestamp'], 'opencode',
                        record['model'], record['thread_id'], record['counts'], source=record['source'])
        report[result] = report.get(result, 0) + 1
    # Store the pre-read fingerprint: writes that arrive during this snapshot
    # guarantee another pass next sync instead of being hidden by the cursor.
    conn.execute('INSERT OR REPLACE INTO usage_opencode_files VALUES (?,?)', (str(path), before))
