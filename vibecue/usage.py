"""Read-only local usage importers and a private, durable token ledger.

No transcript body, credentials or inferred provider is stored in the ledger.
The cache keeps request detail so moving a source or rescanning old logs cannot
add an already imported request a second time. Sources are opened read-only.
"""
from __future__ import annotations

import argparse
import bisect
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

from .events import vibecue_directory, codex_directory
from . import usage_opencode

SCHEMA_VERSION = 1
CODEX_IMPORT_VERSION = 4
MAX_RECORD_BYTES = 8 * 1024 * 1024
TOKEN_COLUMNS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')
APPS = {'codex': 'Codex', 'claude': 'Claude Code', 'opencode': 'OpenCode'}
SOURCES = {'codex_session': 'Codex · 本地会话', 'claude_session': 'Claude Code · 本地会话',
           'opencode_session': 'OpenCode · 本地会话'}


def stamp(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            number = number / 1000 if number > 10_000_000_000 else number
            if not math.isfinite(number):
                return None
            datetime.fromtimestamp(number)
            return number
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def number(value):
    """Malformed, negative, non-integral and over-wide counts are not usage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value > 9_000_000_000_000_000 or not math.isfinite(value) or int(value) != value:
        return None
    return int(value)


def label(value, maximum=180):
    return ' '.join(str(value or '').replace('\x00', '').split())[:maximum]


def digest(*values):
    data = json.dumps(values, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(data.encode()).hexdigest()


def codex_counts(raw):
    if not isinstance(raw, dict):
        return None
    # Input is inclusive of cache in the Codex protocol. At least input and
    # output must be present; absent snapshots are not a fabricated zero row.
    inputs, outputs = number(raw.get('input_tokens')), number(raw.get('output_tokens'))
    if inputs is None or outputs is None:
        return None
    cached = number(raw.get('cached_input_tokens', raw.get('cache_read_input_tokens', 0)))
    created = number(raw.get('cache_write_input_tokens', raw.get('cache_creation_input_tokens', 0)))
    reasoning = number(raw.get('reasoning_output_tokens', 0))
    if cached is None or created is None or reasoning is None:
        return None
    cached = min(inputs, cached)
    created = min(max(0, inputs - cached), created)
    return [inputs, outputs, cached, created, reasoning]


def fresh_counts(counts):
    return [max(0, counts[0] - counts[2] - counts[3]), counts[1], counts[2], counts[3]]


def default_roots():
    codex = codex_directory()
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser()
    opencode = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'opencode'
    return {'codex_roots': [str(codex / 'sessions'), str(codex / 'archived_sessions')],
            'claude_roots': [str(claude / 'projects')],
            'opencode_roots': [os.environ.get('OPENCODE_DB') or str(opencode)]}


def source_roots(home):
    roots = default_roots()
    config = Path(home) / 'usage-sources.json'
    if config.exists():
        value = json.loads(config.read_text())
        if not isinstance(value, dict):
            raise ValueError('用量来源配置需要是 JSON 对象')
        for key in roots:
            if key in value:
                if not isinstance(value[key], list) or not all(isinstance(x, str) for x in value[key]):
                    raise ValueError('用量来源目录需要是字符串列表')
                roots[key] = value[key]
    result = []
    seen = set()
    for app in APPS:
        for root in roots[app + '_roots']:
            path = Path(root).expanduser().resolve()
            # Accept a tool configuration root as well as its log directory.
            if app == 'codex' and (path / 'sessions').is_dir():
                candidates = [path / 'sessions', path / 'archived_sessions']
            elif app == 'claude' and (path / 'projects').is_dir():
                candidates = [path / 'projects']
            else:
                candidates = [path]
            for candidate in candidates:
                candidate = candidate.resolve()
                key = (app, str(candidate))
                if key not in seen:
                    seen.add(key)
                    result.append((app, candidate))
    return result


def connect(home):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = home / 'usage.sqlite3'
    conn = sqlite3.connect(path, timeout=15)
    os.chmod(path, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=15000')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS usage_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS usage_files (
        path TEXT PRIMARY KEY, app TEXT NOT NULL, inode INTEGER, size INTEGER,
        mtime INTEGER, offset INTEGER NOT NULL DEFAULT 0, tail TEXT,
        state TEXT NOT NULL DEFAULT '{}');
      CREATE TABLE IF NOT EXISTS usage_requests (
        id TEXT PRIMARY KEY, timestamp REAL NOT NULL, app TEXT NOT NULL,
        source TEXT NOT NULL, model TEXT NOT NULL, thread_id TEXT NOT NULL,
        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
        cache_read_tokens INTEGER NOT NULL, cache_creation_tokens INTEGER NOT NULL,
        total_tokens INTEGER NOT NULL, date TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS usage_time ON usage_requests(timestamp DESC, id);
      CREATE INDEX IF NOT EXISTS usage_models ON usage_requests(app, model, timestamp);
      CREATE INDEX IF NOT EXISTS usage_sources ON usage_requests(source, timestamp);
      CREATE INDEX IF NOT EXISTS usage_threads ON usage_requests(app, thread_id);
      CREATE TABLE IF NOT EXISTS usage_codex_sessions (
        id TEXT PRIMARY KEY, created REAL, parent TEXT, complete INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS usage_codex_snapshots (
        session_id TEXT NOT NULL, key TEXT NOT NULL, timestamp REAL NOT NULL,
        totals TEXT, PRIMARY KEY(session_id,key));
      CREATE INDEX IF NOT EXISTS usage_snapshot_time ON usage_codex_snapshots(session_id,timestamp);
      CREATE TABLE IF NOT EXISTS usage_codex_fallbacks (
        session_id TEXT NOT NULL, key TEXT NOT NULL, timestamp REAL NOT NULL,
        model TEXT NOT NULL, PRIMARY KEY(session_id,key));
      CREATE TABLE IF NOT EXISTS usage_codex_timeline (
        position INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT UNIQUE NOT NULL, session_id TEXT NOT NULL,
        timestamp REAL NOT NULL, signature TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS usage_codex_timeline_order
        ON usage_codex_timeline(session_id,timestamp,position);
    ''')
    conn.commit()
    return conn


def set_meta(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO usage_meta VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))


def get_meta(conn, key, fallback=None):
    row = conn.execute('SELECT value FROM usage_meta WHERE key=?', (key,)).fetchone()
    return json.loads(row[0]) if row else fallback


def write_request(conn, request_id, timestamp, app, model, thread_id, counts, source=None):
    if timestamp is None or not any(counts):
        return 'skipped'
    model = label(model, 180) or 'unknown'
    row = conn.execute('SELECT input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens FROM usage_requests WHERE id=?',
                       (request_id,)).fetchone()
    if row is not None:
        # Streaming Claude blocks may initially contain only input plus a few
        # output tokens. Never replace a final snapshot by an older partial.
        counts = [max(int(row[i]), counts[i]) for i in range(4)]
        if list(row) == counts:
            return 'skipped'
    day = datetime.fromtimestamp(timestamp).date().isoformat()
    conn.execute('''INSERT OR REPLACE INTO usage_requests
      (id,timestamp,app,source,model,thread_id,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,date)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
      (request_id, timestamp, app, source or app + '_session', model, label(thread_id, 200), *counts, sum(counts), day))
    return 'updated' if row is not None else 'imported'


def parent_id(meta):
    direct = meta.get('forked_from_id') or meta.get('parent_thread_id')
    if direct:
        return label(direct, 200)
    source = meta.get('source')
    if isinstance(source, dict):
        sub = source.get('subagent')
        if isinstance(sub, dict):
            spawn = sub.get('thread_spawn')
            if isinstance(spawn, dict):
                return label(spawn.get('parent_thread_id'), 200)
    return ''


class Deferred(Exception):
    pass


def read_header(path):
    """Bounded metadata discovery; never return or persist prompt contents."""
    process = None
    try:
        if path.name.endswith('.jsonl.zst'):
            program = zstd_program()
            if not program:
                return {}
            process = subprocess.Popen([program,'-q','-d','-c','--',str(path)], stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            source = process.stdout
        else:
            source = path.open('rb')
        with source as stream:
            for _ in range(12):
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line or len(line) > MAX_RECORD_BYTES:
                    break
                try:
                    item = json.loads(line)
                    if item.get('type') == 'session_meta' and isinstance(item.get('payload'), dict):
                        data = item['payload']
                        return {'id': label(data.get('id') or data.get('session_id'), 200),
                                'parent': parent_id(data), 'created': stamp(data.get('timestamp') or item.get('timestamp'))}
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    finally:
        if process:
            if process.poll() is None:
                process.terminate()
            process.wait()
    return {}


def codex_numeric_signature(kind, total=None, last=None):
    """Usage identity for replay matching; receipt time is not request identity.

    Forked rollouts may rewrite thousands of inherited timestamps into a few
    milliseconds. Keep the complete numeric snapshots, including field presence,
    while leaving their physical sequence to the timeline.
    """
    def counters(value):
        if not isinstance(value, dict):
            return None
        return [number(value.get('input_tokens')), number(value.get('output_tokens')),
                number(value.get('cached_input_tokens', value.get('cache_read_input_tokens'))),
                number(value.get('cache_write_input_tokens', value.get('cache_creation_input_tokens'))),
                number(value.get('reasoning_output_tokens')), number(value.get('total_tokens'))]
    return digest(kind, counters(total), counters(last))


def record_codex_timeline(conn, state, timestamp, signature):
    # The ordinal is persisted with the byte cursor. Copies preserve it, and
    # same-millisecond events retain physical insertion order instead of hash ID
    # order. Non-billable token snapshots are needed to align an entire replay.
    ordinal = int(state.get('token_ordinal', 0)) + 1
    state['token_ordinal'] = ordinal
    event_id = digest(state['session'], timestamp, signature, ordinal)
    conn.execute('''INSERT OR IGNORE INTO usage_codex_timeline
        (event_id,session_id,timestamp,signature) VALUES (?,?,?,?)''',
        (event_id,state['session'],timestamp,signature))


def inherited_codex_snapshot(conn, state, key, timestamp, totals=None, signature=None, zero_usage=False):
    session = state['session']
    conn.execute('INSERT OR IGNORE INTO usage_codex_snapshots VALUES (?,?,?,?)',
                 (session,key,timestamp,json.dumps(totals) if totals else None))
    parent=state.get('parent')
    if not parent or not state.get('prefix'):
        return False
    if state.get('created') is None:
        raise Deferred('子会话缺少可靠创建时间，已暂缓继承用量对齐')
    if zero_usage:
        # A fork can prepend a status snapshot whose last fields were cleared.
        # It describes no request and cannot end or advance the replay prefix.
        if totals:
            state['highwater']=totals
        return True
    index = state.get('_runtime_parent_index')
    if index is None:
        # An immediate parent can retain only a sparse suffix. Its own fork
        # ancestry supplies the earlier history, bounded at each descendant's
        # creation time so another branch's later work is never inherited.
        lineage=[]
        ancestor,cutoff=parent,state['created']
        seen={session}
        while ancestor:
            if ancestor in seen or len(lineage)>=64:
                raise Deferred('父会话继承链无法可靠对齐，已暂缓子会话用量')
            seen.add(ancestor)
            metadata=conn.execute('SELECT parent,created,complete FROM usage_codex_sessions WHERE id=?',(ancestor,)).fetchone()
            if not metadata or not metadata['complete']:
                raise Deferred('祖先会话尚未可读，已暂缓子会话以防继承历史重复计数')
            lineage.append((ancestor,cutoff))
            ancestor,cutoff=metadata['parent'],metadata['created']
            if ancestor and cutoff is None:
                raise Deferred('祖先会话缺少可靠创建时间，已暂缓继承用量对齐')
        index = {}
        ordinal=0
        for ancestor,cutoff in reversed(lineage):
            for row in conn.execute('''SELECT signature
                FROM usage_codex_timeline WHERE session_id=? AND timestamp<=?
                ORDER BY timestamp,position''', (ancestor,cutoff)):
                index.setdefault(row['signature'], []).append(ordinal)
                ordinal+=1
        state['_runtime_parent_index'] = index
    positions = index.get(signature, ())
    offset = bisect.bisect_left(positions, int(state.get('parent_replay_cursor', 0)))
    if offset < len(positions):
        state['parent_replay_cursor'] = positions[offset] + 1
        if totals:
            state['highwater']=totals
        state['inherited_snapshots'] = int(state.get('inherited_snapshots', 0)) + 1
        return True
    if state.get('created') is None or timestamp < state['created']:
        raise Deferred('子会话包含无法与父会话对齐的早期用量，已暂缓以防重复计数')
    state['prefix']=False
    return False


def reconcile_codex_fallbacks(conn, session):
    """Rebuild only derived deltas when an earlier continuation page arrives.

    Authoritative last-usage rows remain untouched. Keeping derivation metadata
    makes out-of-order pages and later archive copies correctable; a maximum
    upsert of an already inflated delta would otherwise preserve the error.
    """
    derived={row['key']:row for row in conn.execute('SELECT * FROM usage_codex_fallbacks WHERE session_id=?',(session,))}
    if not derived:
        return
    highwater=[0]*5
    metadata=conn.execute('SELECT parent,created FROM usage_codex_sessions WHERE id=?',(session,)).fetchone()
    if metadata and metadata['parent'] and metadata['created'] is not None:
        for row in conn.execute('''SELECT totals FROM usage_codex_snapshots
             WHERE session_id=? AND timestamp<=? AND totals IS NOT NULL''',(metadata['parent'],metadata['created'])):
            totals=json.loads(row['totals'])
            highwater=[max(highwater[i],totals[i]) for i in range(5)]
    snapshots=[]
    for row in conn.execute('SELECT key,timestamp,totals FROM usage_codex_snapshots WHERE session_id=? AND totals IS NOT NULL',(session,)):
        totals=json.loads(row['totals'])
        snapshots.append((row['timestamp'],sum(totals[:2]),row['key'],totals))
    snapshots.sort(key=lambda row:(row[0],row[1],row[3],row[2]))
    # Prefer a direct last-usage record over an older representation of the
    # same timestamp and cumulative snapshot that lacked the last field.
    exact_snapshots=set()
    for timestamp,_,key,totals in snapshots:
        if key not in derived and conn.execute('SELECT 1 FROM usage_requests WHERE id=?',('codex:'+session+':'+key,)).fetchone():
            exact_snapshots.add((timestamp,tuple(totals)))
    for timestamp,_,key,totals in snapshots:
        previous=highwater
        highwater=[max(highwater[i],totals[i]) for i in range(5)]
        if key not in derived:
            continue
        request_id='codex:'+session+':'+key
        delta=[max(0,totals[i]-previous[i]) for i in range(5)]
        delta[2]=min(delta[0],delta[2]);delta[3]=min(max(0,delta[0]-delta[2]),delta[3])
        counts=fresh_counts(delta)
        if (timestamp,tuple(totals)) in exact_snapshots:
            counts=[0,0,0,0]
        if not any(counts):
            conn.execute('DELETE FROM usage_requests WHERE id=?',(request_id,))
            continue
        conn.execute('''INSERT OR REPLACE INTO usage_requests
          (id,timestamp,app,source,model,thread_id,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,date)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
          (request_id,timestamp,'codex','codex_session',derived[key]['model'],session,*counts,sum(counts),
           datetime.fromtimestamp(timestamp).date().isoformat()))


def parse_codex(conn, item, state, report):
    payload = item.get('payload')
    if not isinstance(payload, dict):
        return
    typ = item.get('type')
    if typ == 'session_meta':
        # Fork/subagent rollout files begin with their own metadata, followed
        # by a replay that can itself contain the parent's session_meta rows.
        # Those nested history headers cannot change the physical file owner.
        if state.get('session'):
            return
        state['session'] = label(payload.get('id') or payload.get('session_id'), 200)
        state['parent'] = parent_id(payload)
        state['created'] = stamp(payload.get('timestamp') or item.get('timestamp'))
        state.setdefault('prefix', bool(state['parent']))
        if state['session']:
            conn.execute('''INSERT INTO usage_codex_sessions (id,created,parent,complete) VALUES (?,?,?,0)
              ON CONFLICT(id) DO UPDATE SET created=excluded.created,parent=excluded.parent''',
              (state['session'],state['created'],state['parent']))
        if state['parent']:
            parent = conn.execute('SELECT complete FROM usage_codex_sessions WHERE id=?', (state['parent'],)).fetchone()
            if not parent or not parent[0]:
                raise Deferred('父会话尚未可读，已暂缓子会话以防继承历史重复计数')
            if not state.get('highwater') and state.get('created'):
                row = conn.execute('''SELECT totals FROM usage_codex_snapshots
                    WHERE session_id=? AND timestamp<=? AND totals IS NOT NULL
                    ORDER BY timestamp DESC LIMIT 1''', (state['parent'],state['created'])).fetchone()
                if row:
                    state['highwater'] = json.loads(row[0])
        return
    if typ == 'turn_context':
        if isinstance(payload.get('model'), str):
            state['model'] = label(payload['model'])
        return
    if typ == 'token_usage_record':
        # Newer Codex versions also emit explicit usage for remote compaction.
        # Keep that authoritative record immediately; if the following regular
        # token snapshot repeats it, merge it into the normal request identity.
        usage=codex_counts(payload.get('usage'))
        session=state.get('session')
        timestamp=stamp(item.get('timestamp') or payload.get('timestamp'))
        if not usage or not any(usage[:4]) or not session or timestamp is None:
            return
        key=digest(timestamp,None,usage)
        replay_signature = codex_numeric_signature('record', last=payload.get('usage'))
        record_codex_timeline(conn,state,timestamp,replay_signature)
        if inherited_codex_snapshot(conn,state,key,timestamp,signature=replay_signature):
            return
        response=label(payload.get('response_id'),250)
        request_id='codex-record:'+session+':'+(digest(response) if response else key)
        outcome=write_request(conn,request_id,timestamp,'codex',payload.get('model') or state.get('model') or 'unknown',
                              session,fresh_counts(usage))
        if outcome in report:
            report[outcome]+=1
        state['pending_record']={'id':request_id,'counts':usage}
        return
    if typ != 'event_msg':
        return
    event_type = payload.get('type')
    if event_type == 'thread_settings_applied':
        settings = payload.get('thread_settings')
        if isinstance(settings, dict) and settings.get('model'):
            state['model'] = label(settings['model'])
        return
    if event_type != 'token_count':
        return
    info = payload.get('info')
    if not isinstance(info, dict):
        return
    session = state.get('session')
    if not session:
        report['missing_session'] = report.get('missing_session', 0) + 1
        return
    timestamp = stamp(item.get('timestamp') or payload.get('timestamp'))
    if timestamp is None:
        report['missing_timestamp'] = report.get('missing_timestamp', 0) + 1
        return
    raw_total = codex_counts(info.get('total_token_usage'))
    raw_last = codex_counts(info.get('last_token_usage'))
    if raw_total is None and raw_last is None:
        return
    model = info.get('model') or info.get('model_name') or payload.get('model') or payload.get('model_name') or state.get('model') or 'unknown'
    state['model'] = label(model)
    signature = digest(raw_total, raw_last)
    # Stable record identity survives copy, symlink aliases and archive moves.
    key = digest(timestamp, raw_total, raw_last)
    replay_signature = codex_numeric_signature('token',info.get('total_token_usage'),info.get('last_token_usage'))
    record_codex_timeline(conn,state,timestamp,replay_signature)
    zero_usage=(raw_last is not None and not any(raw_last[:4])) or (raw_last is None and raw_total is not None and not any(raw_total[:4]))
    if inherited_codex_snapshot(conn,state,key,timestamp,raw_total,replay_signature,zero_usage):
        state['last_signature']=signature
        return
    if raw_total is not None:
        # A newly discovered direct snapshot can also repair an older derived
        # row, including a copy that gained the last-usage field later.
        state['_reconcile_fallbacks']=True
    pending_record=state.pop('pending_record',None)
    record_covers_empty_last=False
    if pending_record and raw_last is None:
        # The explicit record is exact, but this total may include it plus
        # other earlier consumption. Without a single-request snapshot, do
        # not add that potentially overlapping cumulative movement as well.
        record_covers_empty_last=True
        report['ambiguous_records']=report.get('ambiguous_records',0)+1
    elif pending_record and raw_last is not None:
        if raw_last[:4]==pending_record['counts'][:4]:
            conn.execute('DELETE FROM usage_requests WHERE id=?',(pending_record['id'],))
            report['merged_records']=report.get('merged_records',0)+1
        elif not any(raw_last[:4]):
            # The adjacent zero snapshot describes no additional request. Its
            # cumulative movement may include the explicit compaction usage.
            record_covers_empty_last=True
    # Rate-limit refreshes often repeat the identical total/last snapshot.
    # With last-only events, timestamp is needed: two equal requests are valid.
    repeat_key = signature if raw_total is not None else key
    limits = payload.get('rate_limits')
    bucket = label(limits.get('limit_id') if isinstance(limits, dict) else '', 100)
    buckets = state.setdefault('bucket_signatures', {})
    repeated = state.get('last_signature') == repeat_key or buckets.get(bucket) == repeat_key
    previous = state.get('highwater')
    if raw_total:
        state['highwater'] = [max((previous or [0]*5)[i], raw_total[i]) for i in range(5)]
    state['last_signature'] = repeat_key
    buckets[bucket] = repeat_key
    if len(buckets) > 32:
        state['bucket_signatures'] = {bucket: repeat_key}
    if repeated:
        report['duplicates'] += 1
        return
    if record_covers_empty_last:
        counts=[0,0,0,0]
    elif raw_last is not None:
        # An explicit zero last-usage snapshot is authoritative too. Rate or
        # status refreshes can carry cumulative movement without a new request.
        counts = fresh_counts(raw_last)
    elif raw_total is not None:
        delta = [max(0, raw_total[i] - (previous or [0]*5)[i]) for i in range(5)]
        # Cache counters and total input can evolve independently during resets.
        delta[2] = min(delta[0], delta[2])
        delta[3] = min(max(0,delta[0]-delta[2]), delta[3])
        counts = fresh_counts(delta)
        conn.execute('INSERT OR REPLACE INTO usage_codex_fallbacks VALUES (?,?,?,?)',
                     (session,key,timestamp,label(model)))
        state['_reconcile_fallbacks']=True
    else:
        counts = [0,0,0,0]
    outcome = write_request(conn, 'codex:' + session + ':' + key, timestamp, 'codex', model, session, counts)
    if outcome in report:
        report[outcome] += 1


def parse_claude(conn, item, state, report):
    if item.get('sessionId'):
        state['session'] = label(item['sessionId'], 200)
    if item.get('type') != 'assistant':
        return
    message = item.get('message')
    if not isinstance(message, dict) or not message.get('id'):
        return
    usage = message.get('usage')
    if not isinstance(usage, dict):
        return
    names = ('input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')
    counts = [number(usage.get(k,0)) for k in names]
    if any(v is None for v in counts):
        return
    if not any(k in usage for k in names):
        return
    timestamp = stamp(item.get('timestamp'))
    if timestamp is None:
        report['missing_timestamp'] = report.get('missing_timestamp',0) + 1
        return
    # Claude message ID identifies one API response across streamed blocks,
    # copied conversation files and subagent log locations.
    outcome = write_request(conn, 'claude:' + label(message['id'], 250), timestamp, 'claude',
                            message.get('model') or 'unknown', state.get('session',''), counts)
    if outcome in report:
        report[outcome] += 1


def tail_fingerprint(stream, offset):
    position = stream.tell()
    stream.seek(max(0,offset-4096))
    data = stream.read(min(4096,offset))
    stream.seek(position)
    return hashlib.sha256(data).hexdigest()


def zstd_program():
    for candidate in ('/opt/homebrew/bin/zstd','/usr/local/bin/zstd'):
        if os.path.isfile(candidate) and os.access(candidate,os.X_OK):
            return candidate
    return shutil.which('zstd')


def scan_file(conn, app, path, report):
    stat = path.stat()
    previous = conn.execute('SELECT * FROM usage_files WHERE path=?', (str(path),)).fetchone()
    if previous and previous['mtime'] == stat.st_mtime_ns and previous['size'] == stat.st_size:
        return
    compressed = path.name.endswith('.jsonl.zst')
    program = zstd_program() if compressed else None
    if compressed and not program:
        report['compressed_skipped'] += 1
        return
    state = {}
    offset = 0
    process = None
    try:
        if compressed:
            process = subprocess.Popen([program,'-q','-d','-c','--',str(path)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            stream = process.stdout
        else:
            stream = path.open('rb')
        with stream:
            if previous and not compressed and previous['inode'] == stat.st_ino and stat.st_size >= previous['offset']:
                old_offset = previous['offset']
                if previous['tail'] == tail_fingerprint(stream,old_offset):
                    offset = old_offset
                    state = json.loads(previous['state'])
            if not compressed:
                stream.seek(offset)
            start_offset = offset
            while True:
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_RECORD_BYTES:
                    while line and not line.endswith(b'\n'):
                        offset += len(line)
                        line = stream.readline(MAX_RECORD_BYTES + 1)
                    offset += len(line)
                    report['oversized_records'] += 1
                    continue
                if not line.endswith(b'\n'):
                    # Hold the cursor before a partial record. Re-reading it
                    # after append also repairs an intermediate JSON fragment.
                    report['partial_files'] += 1
                    break
                offset += len(line)
                try:
                    item = json.loads(line)
                except (ValueError, UnicodeDecodeError):
                    report['malformed_records'] += 1
                    continue
                if not isinstance(item, dict):
                    continue
                if app == 'codex':
                    parse_codex(conn,item,state,report)
                else:
                    parse_claude(conn,item,state,report)
            if process and process.wait() != 0:
                raise OSError('压缩记录解码失败')
            if app=='codex' and state.pop('_reconcile_fallbacks',False) and state.get('session'):
                reconcile_codex_fallbacks(conn,state['session'])
            fingerprint = None if compressed else tail_fingerprint(stream,offset)
            stored_state = {key:value for key,value in state.items() if not key.startswith('_runtime_')}
            conn.execute('''INSERT OR REPLACE INTO usage_files VALUES (?,?,?,?,?,?,?,?)''',
                         (str(path),app,stat.st_ino,stat.st_size,stat.st_mtime_ns,offset,fingerprint,json.dumps(stored_state)))
            if app == 'codex' and state.get('session'):
                conn.execute('UPDATE usage_codex_sessions SET complete=1 WHERE id=?',(state['session'],))
            report['bytes_read'] += max(0,offset-start_offset)
    finally:
        if process and process.poll() is None:
            process.terminate()
            process.wait()


def upgrade_codex_import(conn, home):
    """Back up and rebuild only the old Codex ledger before changing identity.

    Timestamp-keyed rows cannot safely be repaired in place: replay receipt
    times were rewritten and a cached cursor would hide the source prefix.
    Claude rows and the independent reminder inbox are never changed here.
    """
    if get_meta(conn, 'codex_import_version', 0) == CODEX_IMPORT_VERSION:
        return {}
    existing = conn.execute("SELECT EXISTS(SELECT 1 FROM usage_requests WHERE app='codex') OR EXISTS(SELECT 1 FROM usage_files WHERE app='codex')").fetchone()[0]
    result = {}
    if existing:
        backups = Path(home) / 'backups'
        backups.mkdir(parents=True,exist_ok=True,mode=0o700)
        os.chmod(backups,0o700)
        target = backups / ('usage-before-codex-replay-v' + str(CODEX_IMPORT_VERSION) + '-' + uuid.uuid4().hex + '.sqlite3')
        fd = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        os.close(fd)
        # A connection cannot back itself up while it owns a write transaction.
        # Finish before backup and do not delete a single old row until it succeeds.
        conn.commit()
        with closing(sqlite3.connect(target)) as saved:
            conn.backup(saved)
        os.chmod(target,0o600)
        with conn:
            conn.execute("DELETE FROM usage_requests WHERE app='codex'")
            conn.execute("DELETE FROM usage_files WHERE app='codex'")
            for table in ('usage_codex_snapshots','usage_codex_sessions','usage_codex_fallbacks','usage_codex_timeline'):
                conn.execute('DELETE FROM ' + table)
        result = {'rebuild':True,
                  'rebuild_reason':'分叉历史去重规则升级，旧统计已备份，正在重新导入 Codex 用量',
                  'rebuild_backup':target.name}
    set_meta(conn,'codex_import_version',CODEX_IMPORT_VERSION)
    conn.commit()
    return result


def sync(home=None):
    home = vibecue_directory(home)
    home.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock_path = home / 'usage-sync.lock'
    with lock_path.open('a') as lock:
        os.chmod(lock_path,0o600)
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return query(home)
        with closing(connect(home)) as conn:
            prior = get_meta(conn,'sync',{})
            report = {'running':True,'started_at':time.time(),'last_synced_at':prior.get('last_synced_at'),
                      'files_scanned':0,'files_total':0,'imported':0,'updated':0,'duplicates':0,'skipped':0,
                      'bytes_read':0,'errors':[],'warnings':[],'coverage':[],
                      'malformed_records':0,'oversized_records':0,'partial_files':0,'compressed_skipped':0,'deferred_files':0,
                      'phase':'discovering','headers_scanned':0}
            set_meta(conn,'sync',report);conn.commit()
            try:
                report.update(upgrade_codex_import(conn,home))
                set_meta(conn,'sync',report);conn.commit()
                roots = source_roots(home)
                files = []
                opencode_kinds = {}
                seen_paths = set()
                app_available = {app:False for app in APPS}
                for app, root in roots:
                    if app == 'opencode':
                        if root.is_dir() or root.is_file():
                            app_available[app] = True
                        try:
                            for kind, path in usage_opencode.discover(root):
                                resolved = path.resolve()
                                if (app, str(resolved)) not in seen_paths:
                                    files.append((app, resolved)); seen_paths.add((app, str(resolved)))
                                    opencode_kinds[str(resolved)] = kind
                        except OSError:
                            report['errors'].append('OpenCode：部分用量来源无法读取')
                        continue
                    if not root.is_dir():
                        continue
                    app_available[app] = True
                    try:
                        for path in root.rglob('*'):
                            if path.is_file() and (path.name.endswith('.jsonl') or (app=='codex' and path.name.endswith('.jsonl.zst'))):
                                resolved = path.resolve()
                                if (app,str(resolved)) not in seen_paths:
                                    files.append((app,resolved));seen_paths.add((app,str(resolved)))
                    except OSError:
                        report['errors'].append(APPS[app]+'：部分目录无法读取')
                # Register metadata first, then visit parents before descendants.
                report['files_total']=len(files)
                report['phase']='preparing'
                set_meta(conn,'sync',report);conn.commit()
                headers = {}
                cached = {row['path']:row for row in conn.execute('SELECT path,inode,size,mtime,state FROM usage_files WHERE app="codex"')}
                last_header_progress=time.monotonic()
                for app,path in files:
                    if app == 'codex':
                        saved = cached.get(str(path))
                        current = path.stat()
                        if saved and saved['inode']==current.st_ino and saved['size']==current.st_size and saved['mtime']==current.st_mtime_ns:
                            state=json.loads(saved['state'])
                            header={'id':state.get('session'), 'parent':state.get('parent',''), 'created':state.get('created')}
                        else:
                            header = read_header(path)
                        if header.get('id'):
                            headers[str(path)] = header
                            conn.execute('''INSERT INTO usage_codex_sessions (id,created,parent,complete) VALUES (?,?,?,0)
                              ON CONFLICT(id) DO UPDATE SET created=excluded.created,parent=excluded.parent''',
                              (header['id'],header.get('created'),header.get('parent','')))
                        report['headers_scanned']+=1
                        if time.monotonic()-last_header_progress>.5:
                            set_meta(conn,'sync',report);conn.commit();last_header_progress=time.monotonic()
                conn.commit()
                parents = {h['id']:h.get('parent','') for h in headers.values()}
                def depth(entry):
                    header = headers.get(str(entry[1]),{})
                    parent, chain = header.get('parent',''),set()
                    while parent and parent not in chain and len(chain)<64:
                        chain.add(parent);parent=parents.get(parent,'')
                    return len(chain)
                files.sort(key=lambda entry:(entry[0],depth(entry),str(entry[1])))
                report['files_total'] = len(files)
                report['phase']='importing'
                set_meta(conn,'sync',report);conn.commit()
                last_progress = time.monotonic()
                for app,path in files:
                    before = dict(report)
                    try:
                        with conn:
                            if app == 'opencode':
                                usage_opencode.scan(conn,path,report,write_request,kind=opencode_kinds[str(path)])
                            else:
                                scan_file(conn,app,path,report)
                    except Deferred as error:
                        for key in ('imported','updated','duplicates','bytes_read'):
                            report[key] = before[key]
                        report['deferred_files'] += 1
                        if str(error) not in report['warnings']:
                            report['warnings'].append(str(error))
                    except (OSError,sqlite3.Error,ValueError,TypeError) as error:
                        for key in ('imported','updated','duplicates','bytes_read'):
                            report[key] = before[key]
                        message = APPS[app]+'：有会话文件读取失败（'+type(error).__name__+'）'
                        if message not in report['errors']:
                            report['errors'].append(message)
                    report['files_scanned'] += 1
                    if time.monotonic()-last_progress>.5:
                        set_meta(conn,'sync',report);conn.commit();last_progress=time.monotonic()
                for app in APPS:
                    count = sum(1 for a,p in files if a==app)
                    requests = conn.execute('SELECT COUNT(*) FROM usage_requests WHERE app=?',(app,)).fetchone()[0]
                    report['coverage'].append({'app':app,'name':APPS[app],'available':app_available[app],
                                               'files':count,'requests':requests})
                if report['compressed_skipped']:
                    report['warnings'].append('部分 .zst 压缩日志未导入：本机未找到 zstd 解码器')
                # An unchanged file is skipped next time, but its previously
                # skipped records are still absent from the ledger. Keep that
                # coverage limitation visible across incremental scans.
                gaps = bool(get_meta(conn, 'historical_gaps', False) or
                            prior.get('malformed_records') or prior.get('oversized_records') or
                            report['malformed_records'] or report['oversized_records'])
                if gaps:
                    set_meta(conn, 'historical_gaps', True)
                    report['historical_gaps'] = True
                    report['warnings'].append('历史导入曾跳过损坏或过大的记录，统计覆盖可能不完整')
                if report.get('missing_session') or report.get('missing_timestamp'):
                    report['warnings'].append('部分用量缺少会话标识或时间，未猜测导入')
                if report.get('ambiguous_records'):
                    report['warnings'].append('部分压缩用量之后缺少单次快照：已保留明确记录，暂缓可能重叠的累计变化')
            except Exception as error:
                report['errors'].append('同步未完成（'+type(error).__name__+'）')
            finally:
                report['running']=False
                report['phase']='finished'
                report['finished_at']=time.time()
                if not report['errors']:
                    report['last_synced_at']=report['finished_at']
                set_meta(conn,'sync',report);conn.commit()
    return query(home)


def query(home=None, period='all', app='all', model='all', source='all', page=1,page_size=50,view='logs',now=None):
    home = vibecue_directory(home)
    now = time.time() if now is None else float(now)
    end = datetime.fromtimestamp(now)
    start = None
    if period=='today':
        start = end.replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
    elif period in ('7d','30d'):
        start = now-int(period[:-1])*86400
    clauses, params = [],[]
    for field,value in [('app',app),('model',model),('source',source)]:
        if value and value!='all':
            clauses.append(field+'=?');params.append(value)
    base_clauses,base_params = list(clauses),list(params)
    if start is not None:
        clauses.append('timestamp>=?');params.append(start)
    clauses.append('timestamp<=?');params.append(now)
    where = ' WHERE '+' AND '.join(clauses)
    page = max(1,int(page));page_size=min(200,max(1,int(page_size)))
    sums = ','.join('COALESCE(SUM('+key+'),0) AS '+key for key in TOKEN_COLUMNS+('total_tokens',))
    with closing(connect(home)) as conn:
        # A full-history timestamp upper bound matches almost every row. Its
        # timestamp index would visit the ledger in random order on disk; one
        # sequential scan is cheaper. Selective filters keep SQLite's planner.
        aggregate_table = 'usage_requests NOT INDEXED' if start is None and not base_clauses else 'usage_requests'
        price_groups = [dict(r) for r in conn.execute(
            'SELECT app,source,model,COUNT(*) AS requests,'+sums+
            ' FROM '+aggregate_table+where+' GROUP BY app,source,model', params)]
        total_columns = ('requests',)+TOKEN_COLUMNS+('total_tokens',)
        summary = {key:sum(row[key] for row in price_groups) for key in total_columns}
        cacheable = summary['input_tokens']+summary['cache_read_tokens']+summary['cache_creation_tokens']
        summary['cache_hit_rate'] = summary['cache_read_tokens']/cacheable if cacheable else 0
        # The full filtered model/source aggregate already supplies every
        # token and count needed by the summary and the visible grouping tab.
        # Fold these small groups rather than rescanning all requests twice.
        groups={'models':[],'sources':[]}
        if view in groups:
            columns = ('model',) if view=='models' else ('source','app')
            folded = {}
            for row in price_groups:
                key = tuple(row[column] for column in columns)
                target = folded.setdefault(key, dict(zip(columns,key), **{column:0 for column in total_columns}))
                for column in total_columns:
                    target[column] += row[column]
            groups[view] = sorted(folded.values(),key=lambda row:(-row['total_tokens'],)+tuple(row[column] for column in columns))
        total = summary['requests'] if view=='logs' else len(groups.get(view,[]))
        pages=max(1,(total+page_size-1)//page_size)
        page=min(page,pages)
        logs=[dict(r) for r in conn.execute('SELECT * FROM usage_requests'+where+' ORDER BY timestamp DESC,id LIMIT ? OFFSET ?',
                 params+[page_size,(page-1)*page_size])] if view=='logs' else []
        heat_start=(end.date()-timedelta(days=370)).isoformat()
        heat_clauses=base_clauses+['date>=?','timestamp<=?']
        heat_params=base_params+[heat_start,now]
        heat_table = 'usage_requests NOT INDEXED' if not base_clauses else 'usage_requests'
        days=[dict(r) for r in conn.execute('SELECT date,SUM(total_tokens) AS tokens,COUNT(*) AS requests FROM '+heat_table+' WHERE '+
               ' AND '.join(heat_clauses)+' GROUP BY date ORDER BY date',heat_params)]
        sources = dict(SOURCES)
        for source_row in conn.execute('SELECT DISTINCT source FROM usage_requests ORDER BY source'):
            source_id = source_row[0]
            sources.setdefault(source_id, 'OpenCode · ' + source_id[len('opencode_provider:'):] if source_id.startswith('opencode_provider:') else source_id)
        filters={'apps':[{'id':k,'name':v} for k,v in APPS.items()],
                 'models':[{'id':r[0],'name':r[0]} for r in conn.execute('SELECT DISTINCT model FROM usage_requests ORDER BY model')],
                 'sources':[{'id':k,'name':v} for k,v in sources.items()]}
        metadata=get_meta(conn,'sync',{'running':False,'last_synced_at':None,'errors':[],'warnings':[],'coverage':[]})
        # A terminated scanner must not leave the UI saying "同步中" forever.
        if metadata.get('running'):
            try:
                with (home/'usage-sync.lock').open('a') as check:
                    try:
                        fcntl.flock(check,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        metadata['running']=False
                        metadata['warnings']=list(metadata.get('warnings',[]))+['上次同步被中断；点击同步可从已提交进度继续']
                    except BlockingIOError:
                        pass
            except OSError:
                pass
        result={'schema_version':SCHEMA_VERSION,'summary':summary,'days':days,'logs':logs,
                'models':groups['models'][(page-1)*page_size:page*page_size] if view=='models' else groups['models'][:200],
                'sources':groups['sources'][(page-1)*page_size:page*page_size] if view=='sources' else groups['sources'][:200],
                'filters':filters,'pagination':{'total':total,'page':page,'page_size':page_size,'pages':pages},
                'sync':metadata,'supported_apps':list(APPS),'view':view,'range':period,
                'coverage_note':'读取仍保留的本机会话 usage；费用为 API 单价参考估算，不是订阅账单。'}
        from . import usage_money
        usage_money.decorate(home, result, price_groups, now=now)
        return result


def today_summary(home=None, now=None, cc_path=None):
    """Small, read-only menu-bar snapshot of the current local calendar day.

    Unlike query(), this never initializes a database, scans source histories,
    fetches an exchange rate, or reads request bodies/list rows. A successful
    initial sync is required before an empty ledger can mean zero usage.
    """
    home = vibecue_directory(home)
    now = time.time() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError('invalid summary timestamp')
    local = datetime.fromtimestamp(now)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    result = {
        'schema_version': SCHEMA_VERSION, 'available': False,
        'date': local.date().isoformat(), 'timezone': local.astimezone().tzname(),
        'utc_offset_seconds': int(local.astimezone().utcoffset().total_seconds()),
        'day_start': midnight.timestamp(),
        'day_end': (midnight + timedelta(days=1)).timestamp(),
        'generated_at': now, 'summary': {},
        'sync': {'last_synced_at': None, 'running': False, 'errors': [],
                 'warnings': [], 'coverage': []},
        'message': '尚未同步本机用量，今日数字暂不可用。',
    }
    groups = []
    path = home / 'usage.sqlite3'
    if path.is_file():
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
                raw = get_meta(conn, 'sync', {})
                if not isinstance(raw, dict):
                    raise ValueError('invalid sync metadata')
                synced = raw.get('last_synced_at')
                if isinstance(synced, bool) or not isinstance(synced, (int, float)) or not math.isfinite(synced) or not 0 < synced <= now + 60:
                    synced = None
                metadata = result['sync']
                metadata['last_synced_at'] = synced
                metadata['running'] = raw.get('running') is True
                for key in ('errors', 'warnings'):
                    metadata[key] = [label(item, 300) for item in raw.get(key, [])[:20]] if isinstance(raw.get(key), list) else []
                if isinstance(raw.get('coverage'), list):
                    metadata['coverage'] = [
                        {'app': label(row.get('app'), 40), 'available': row.get('available') is True,
                         'files': number(row.get('files')) or 0}
                        for row in raw['coverage'][:20] if isinstance(row, dict)]
                for key in ('historical_gaps', 'rebuild'):
                    metadata[key] = raw.get(key) is True
                # Only inspect an existing lock. Opening with 'a' would create
                # files as a side effect of a read-only status query.
                if metadata['running']:
                    try:
                        with (home / 'usage-sync.lock').open('rb') as check:
                            try:
                                fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
                                metadata['running'] = False
                                metadata['warnings'].append('上次同步被中断；已有统计可能未更新')
                            except BlockingIOError:
                                pass
                    except OSError:
                        metadata['running'] = False
                        metadata['warnings'].append('无法确认同步进程；已有统计可能未更新')
                if synced is not None:
                    sums = ','.join('COALESCE(SUM(' + key + '),0) AS ' + key
                                    for key in TOKEN_COLUMNS + ('total_tokens',))
                    groups = [dict(row) for row in conn.execute(
                        'SELECT model,COUNT(*) AS requests,' + sums +
                        ' FROM usage_requests WHERE timestamp>=? AND timestamp<=? GROUP BY model',
                        (result['day_start'], now))]
                    result['summary'] = {key: sum(row[key] for row in groups)
                                         for key in ('requests',) + TOKEN_COLUMNS + ('total_tokens',)}
                    result['available'] = True
                    result['message'] = ('已有统计正在后台更新。' if metadata['running'] else
                                         '部分来源记录未完整导入。' if metadata['errors'] or metadata['warnings'] else
                                         '今日全部应用的本机用量；费用按 API 参考单价估算。')
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            result['available'] = False
            result['summary'] = {}
            result['message'] = '本机用量缓存暂不可读，今日数字暂不可用。'
    from . import usage_money
    if result['available']:
        usage_money.decorate(home, result, groups, cc_path=cc_path, now=now)
    else:
        snapshot = usage_money.settings(home, cc_path=cc_path, now=now)
        snapshot.pop('pricing', None)
        result['money'] = snapshot
    # The menu needs the selected display currency/rate, not the entire custom
    # model-price editor payload on every refresh.
    prefs = result['money'].get('prefs', {})
    result['money']['prefs'] = {key: prefs[key] for key in ('currency', 'fx_mode', 'manual_rate') if key in prefs}
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='VibeCue 本机历史 Token 用量')
    parser.add_argument('command',choices=('sync','query','today-summary'))
    parser.add_argument('--range',dest='period',choices=('today','7d','30d','all'),default='all')
    parser.add_argument('--app',default='all')
    parser.add_argument('--model',default='all')
    parser.add_argument('--source',default='all')
    parser.add_argument('--page',type=int,default=1)
    parser.add_argument('--page-size',type=int,default=50)
    parser.add_argument('--view',choices=('logs','models','sources'),default='logs')
    args=parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command=='sync':
            result=sync()
        elif args.command=='today-summary':
            result=today_summary()
        else:
            result=query(period=args.period,app=args.app,model=args.model,source=args.source,
                         page=args.page,page_size=args.page_size,view=args.view)
        print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
    except (OSError,sqlite3.Error,ValueError) as error:
        print(json.dumps({'error':'本机用量缓存不可用（'+type(error).__name__+'）'},ensure_ascii=False))
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
