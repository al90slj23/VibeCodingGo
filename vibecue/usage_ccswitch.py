"""Read-only CC Switch numeric usage interoperability.

Native Codex/Claude/OpenCode counts remain authoritative. Existing CC session
counts for those tools are never imported again. Exact, unique usage identities
can supply timing metadata; other tools can reuse CC's numeric log records.
No settings, account, authorization, error body, prompt or response is read.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time

NATIVE_APPS = frozenset(('codex', 'claude', 'opencode'))
APP_NAMES = {'codex': 'Codex', 'claude': 'Claude Code', 'opencode': 'OpenCode',
             'claude-desktop': 'Claude Desktop', 'gemini': 'Gemini CLI', 'grokbuild': 'Grok Build',
             'openclaw': 'OpenClaw', 'hermes': 'Hermes', 'mcode': 'MiniMax Code', 'kimi': 'Kimi Code',
             'pi': 'Pi', 'agy': 'Antigravity', 'omp': 'Oh My Pi'}
MAX_ROWS = 2_000_000
ENRICHMENT_LIMIT = 500_000
COUNTS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')


def text(value, maximum=180):
    return ' '.join(str(value or '').replace('\x00', '').split())[:maximum]


def count(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 9e15 or int(value) != value:
        return None
    return int(value)


def milliseconds(value, allow_zero=False):
    number = count(value)
    return number if number is not None and (number > 0 or allow_zero) and number <= 604_800_000 else None


def ensure_schema(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS usage_cc_metrics (
        cc_id TEXT PRIMARY KEY, origin_request_id TEXT, app TEXT NOT NULL, thread_id TEXT NOT NULL,
        model TEXT NOT NULL, second INTEGER NOT NULL, input_tokens INTEGER NOT NULL,
        output_tokens INTEGER NOT NULL, cache_read_tokens INTEGER NOT NULL, cache_creation_tokens INTEGER NOT NULL,
        duration_ms INTEGER, first_token_ms INTEGER, status_code INTEGER, provider_id TEXT, provider_name TEXT,
        timing_estimated INTEGER NOT NULL, quality INTEGER NOT NULL, external_source TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS usage_request_details (
        request_id TEXT PRIMARY KEY, cc_id TEXT, origin_request_id TEXT, duration_ms INTEGER, first_token_ms INTEGER,
        status_code INTEGER, provider_id TEXT, provider_name TEXT, timing_estimated INTEGER NOT NULL DEFAULT 0,
        provenance TEXT);
      CREATE INDEX IF NOT EXISTS usage_details_status ON usage_request_details(status_code,request_id);
      CREATE INDEX IF NOT EXISTS usage_details_provider ON usage_request_details(provider_id,request_id);
      CREATE INDEX IF NOT EXISTS usage_details_cc ON usage_request_details(cc_id);
      CREATE TABLE IF NOT EXISTS usage_cc_import_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS usage_cc_detail_signatures(request_id TEXT PRIMARY KEY,signature TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS usage_cc_models(cc_id TEXT PRIMARY KEY,requested_model TEXT,pricing_model TEXT);
      CREATE TABLE IF NOT EXISTS usage_cc_gemini_keys (
        cc_id TEXT PRIMARY KEY,kind TEXT,model TEXT,created_at REAL NOT NULL,
        input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,cache_read_tokens INTEGER NOT NULL,
        cache_creation_tokens INTEGER NOT NULL,successful_proxy INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS usage_cc_gemini_lookup
        ON usage_cc_gemini_keys(kind,input_tokens,output_tokens,cache_read_tokens,created_at);
      CREATE TABLE IF NOT EXISTS usage_cc_gemini_mirrors(cc_id TEXT PRIMARY KEY);
    ''')


def sync_gemini_mirrors(conn):
    """Use CC's verified Gemini read-side rule on retained raw numeric keys.

    CC's usage_stats.rs effective_usage_log_filter uses raw counts, a ten-minute
    window and a zero session cache-creation wildcard. Its rollup applies the
    same rule before pruning both records. Retaining keys keeps that exclusion
    valid after the source detail rows disappear; changed live keys can undo it.
    """
    current = {row[0] for row in conn.execute('''
      SELECT DISTINCT s.cc_id FROM usage_cc_gemini_keys p
      JOIN usage_cc_gemini_keys s ON s.kind='gemini_session'
        AND s.input_tokens=p.input_tokens AND s.output_tokens=p.output_tokens
        AND s.cache_read_tokens=p.cache_read_tokens
        AND s.created_at BETWEEN p.created_at-600 AND p.created_at+600
        AND (s.cache_creation_tokens=p.cache_creation_tokens OR s.cache_creation_tokens=0)
        AND (LOWER(s.model)=LOWER(p.model) OR LOWER(s.model)='unknown' OR LOWER(p.model)='unknown')
      WHERE p.kind='proxy' AND p.successful_proxy=1''')}
    previous = {row[0] for row in conn.execute('SELECT cc_id FROM usage_cc_gemini_mirrors')}
    conn.executemany('DELETE FROM usage_cc_gemini_mirrors WHERE cc_id=?',((value,) for value in previous-current))
    conn.executemany('INSERT INTO usage_cc_gemini_mirrors VALUES(?)',((value,) for value in current-previous))
    # These IDs belong only to the bridge; native ledgers are never removed.
    for table,key in (('usage_requests','id'),('usage_request_details','request_id'),('usage_cc_detail_signatures','request_id')):
        conn.execute('DELETE FROM '+table+' WHERE '+key+" IN (SELECT 'ccswitch:'||cc_id FROM usage_cc_gemini_mirrors)")
    return len(current)


def normalized(row, provider_names=None):
    app = text(row.get('app_type'), 40)
    if app not in APP_NAMES: return None
    values = [count(row.get(key, 0)) for key in COUNTS]
    if any(value is None for value in values): return None
    inputs, outputs, cached, created = values
    semantics = row.get('input_token_semantics', 0)
    if app in ('codex', 'gemini', 'grokbuild') and semantics != 2:
        deduction = cached + (created if semantics == 1 else 0)
        if inputs < deduction: return None
        inputs -= deduction
    timestamp = row.get('created_at')
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp <= 0: return None
    try: datetime.fromtimestamp(timestamp)
    except (ValueError, OverflowError, OSError): return None
    origin = text(row.get('request_id'), 240)
    if not origin: return None
    cc_id = hashlib.sha256(origin.encode()).hexdigest()
    source = text(row.get('data_source'), 80) or 'proxy'
    estimated = source != 'proxy'
    duration = milliseconds(row.get('duration_ms')) or milliseconds(row.get('latency_ms'))
    first = milliseconds(row.get('first_token_ms'), allow_zero=True)
    if duration is not None and first is not None and first > duration: first = None
    status = count(row.get('status_code')) if not estimated else None
    if status is not None and not 100 <= status <= 599: status = None
    provider_id = text(row.get('provider_id'))
    provider_name = '会话日志' if estimated else (provider_names or {}).get((app, provider_id), provider_id or '未记录供应商')
    quality = (10 if not estimated else 0) + (2 if duration is not None else 0) + (1 if first is not None else 0)
    return dict(cc_id=cc_id, origin_request_id=origin, app=app, thread_id=text(row.get('session_id')),
                model=text(row.get('model')) or 'unknown', second=int(timestamp), input_tokens=inputs, output_tokens=outputs,
                cache_read_tokens=cached, cache_creation_tokens=created, duration_ms=duration, first_token_ms=first,
                status_code=status, provider_id=provider_id, provider_name=text(provider_name), timing_estimated=int(estimated),
                quality=quality, external_source=source)


def sync(conn, cc_path=None, progress=None):
    started=time.monotonic()
    def phase(name):
        if progress is not None: progress(name,round(time.monotonic()-started,2))
    ensure_schema(conn)
    conn.execute('PRAGMA cache_size=-65536')
    phase('schema')
    if cc_path is not None:
        path=Path(cc_path)
    else:
        from .events import vibecue_directory
        database=next((row[2] for row in conn.execute('PRAGMA database_list') if row[1]=='main'), '')
        home=Path(database).parent if database else Path('.')
        path=None
        try:
            config=home/'usage-sources.json'
            if config.stat().st_size<=1_048_576:
                value=json.loads(config.read_text()).get('ccswitch_usage_db')
                if isinstance(value,str) and value: path=Path(value).expanduser()
        except (OSError,ValueError,TypeError,AttributeError): pass
        if path is None:
            # An explicitly isolated ledger must not silently read another
            # user's default CC database (including unit-test scratch homes).
            path=Path.home()/'.cc-switch/cc-switch.db' if home.resolve()==vibecue_directory().resolve() else home/'cc-switch-usage.sqlite3'
    report = {'available': False, 'scanned': 0, 'imported': 0, 'matched': 0, 'invalid': 0,
              'note': 'CC Switch 仅补充数值与时序；原生三种工具不会重复导入其会话用量。'}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.5)) as source:
            source.row_factory = sqlite3.Row; source.execute('PRAGMA query_only=ON')
            columns = {row[1] for row in source.execute('PRAGMA table_info(proxy_request_logs)')}
            required = {'request_id', 'app_type', 'model', 'created_at', *COUNTS}
            if not required <= columns: return report
            allowed = ('request_id','app_type','session_id','model','request_model','pricing_model',*COUNTS,'input_token_semantics','created_at',
                       'latency_ms','duration_ms','first_token_ms','status_code','provider_id','data_source')
            selection = ','.join(key for key in allowed if key in columns)
            providers = {}
            provider_columns = {row[1] for row in source.execute('PRAGMA table_info(providers)')}
            if {'id','app_type','name'} <= provider_columns:
                for row in source.execute('SELECT id,app_type,name FROM providers LIMIT 10000'):
                    providers[(text(row['app_type'],40),text(row['id']))] = text(row['name'])
            fields = ('cc_id','origin_request_id','app','thread_id','model','second',*COUNTS,'duration_ms','first_token_ms',
                      'status_code','provider_id','provider_name','timing_estimated','quality','external_source')
            updates = [key for key in fields if key != 'cc_id']
            sql = 'INSERT INTO usage_cc_metrics ('+','.join(fields)+') VALUES ('+','.join('?' for _ in fields)+') ON CONFLICT(cc_id) DO UPDATE SET '+','.join(key+'=excluded.'+key for key in updates)+' WHERE '+ ' OR '.join('usage_cc_metrics.'+key+' IS NOT excluded.'+key for key in updates)
            # The Gemini rule needs original input semantics, so backfill its
            # raw numeric keys once when upgrading an existing bridge ledger.
            stat=path.stat(); identity=[str(path.resolve()),stat.st_dev,stat.st_ino,sorted(columns),'gemini-dedup-v1']
            saved=conn.execute("SELECT value FROM usage_cc_import_state WHERE key='source'").fetchone()
            try: state=json.loads(saved[0]) if saved else {}
            except (ValueError,TypeError): state={}
            max_row=source.execute('SELECT MAX(rowid) FROM proxy_request_logs').fetchone()[0] or 0
            since=state.get('last_rowid',0) if state.get('identity')==identity and max_row>=state.get('last_rowid',0) else 0
            if since:
                # Gemini rereads old session files and updates existing rows
                # without changing rowid or created_at. Recheck that app's
                # numeric keys so corrected historical mirrors can be restored.
                source_sql='SELECT rowid AS _rowid,'+selection+' FROM proxy_request_logs WHERE rowid>? UNION SELECT rowid AS _rowid,'+selection+' FROM proxy_request_logs WHERE created_at>=? UNION SELECT rowid AS _rowid,'+selection+" FROM proxy_request_logs WHERE app_type='gemini' LIMIT ?"
                source_params=(since,int(time.time())-7200,MAX_ROWS+1)
            else:
                source_sql='SELECT rowid AS _rowid,'+selection+' FROM proxy_request_logs NOT INDEXED LIMIT ?'
                source_params=(MAX_ROWS+1,)
            watermark=since
            batch = []; model_batch = []; gemini_batch = []
            gemini_sql = '''INSERT INTO usage_cc_gemini_keys VALUES(?,?,?,?,?,?,?,?,?)
              ON CONFLICT(cc_id) DO UPDATE SET kind=excluded.kind,model=excluded.model,created_at=excluded.created_at,
                input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,cache_read_tokens=excluded.cache_read_tokens,
                cache_creation_tokens=excluded.cache_creation_tokens,successful_proxy=excluded.successful_proxy
              WHERE kind IS NOT excluded.kind OR model IS NOT excluded.model OR created_at IS NOT excluded.created_at
                OR input_tokens IS NOT excluded.input_tokens OR output_tokens IS NOT excluded.output_tokens
                OR cache_read_tokens IS NOT excluded.cache_read_tokens OR cache_creation_tokens IS NOT excluded.cache_creation_tokens
                OR successful_proxy IS NOT excluded.successful_proxy'''
            for raw in source.execute(source_sql, source_params):
                report['scanned'] += 1
                if report['scanned'] > MAX_ROWS:
                    report['note'] += ' 较大的 CC 数值目录本轮读取已达上限。'; break
                row = normalized(dict(raw), providers)
                watermark=max(watermark,raw['_rowid'])
                if row is None: report['invalid'] += 1; continue
                batch.append(tuple(row[key] for key in fields))
                if raw['app_type'] == 'gemini':
                    kind = raw['data_source'] if 'data_source' in columns and raw['data_source'] is not None else 'proxy'
                    gemini_batch.append((row['cc_id'],kind,raw['model'],raw['created_at'],
                                         *(raw[key] for key in COUNTS),int(kind=='proxy' and row['status_code'] is not None and 200<=row['status_code']<=299)))
                if 'request_model' in columns or 'pricing_model' in columns:
                    model_batch.append((row['cc_id'],text(raw['request_model']) if 'request_model' in columns else '',text(raw['pricing_model']) if 'pricing_model' in columns else ''))
                if len(batch) >= 5000:
                    conn.executemany(sql,batch); batch=[]
                    if model_batch: conn.executemany('INSERT OR REPLACE INTO usage_cc_models VALUES(?,?,?)',model_batch);model_batch=[]
                    if gemini_batch: conn.executemany(gemini_sql,gemini_batch);gemini_batch=[]
            if batch: conn.executemany(sql,batch); conn.commit()
            if model_batch: conn.executemany('INSERT OR REPLACE INTO usage_cc_models VALUES(?,?,?)',model_batch)
            if gemini_batch: conn.executemany(gemini_sql,gemini_batch)
            conn.execute("INSERT OR REPLACE INTO usage_cc_import_state VALUES('source',?)",(json.dumps({'identity':identity,'last_rowid':watermark}),))
            conn.commit()
            report['available'] = True
            report['suppressed_gemini_mirrors'] = sync_gemini_mirrors(conn)
            from . import usage_ccrollups
            report['archived_daily']=usage_ccrollups.sync(conn,source,providers)
            phase('read_numeric_source')
    except (OSError, sqlite3.Error, ValueError, TypeError):
        report['note'] = 'CC Switch 数值目录暂不可读，保留之前已读取的明细；原生用量继续可用。'
        return report
    # Count new tools only. Reusing CC session counts for native tools would
    # reintroduce their fork/cumulative duplicates into our canonical ledger.
    native = ','.join("'" + app + "'" for app in sorted(NATIVE_APPS))
    before = conn.execute("SELECT COUNT(*) FROM usage_requests WHERE id LIKE 'ccswitch:%'").fetchone()[0]
    conn.execute('''INSERT INTO usage_requests
      (id,timestamp,app,source,model,thread_id,input_tokens,output_tokens,cache_read_tokens,cache_creation_tokens,total_tokens,date)
      SELECT 'ccswitch:'||cc_id,second,app,'ccswitch:'||external_source,model,thread_id,input_tokens,output_tokens,
             cache_read_tokens,cache_creation_tokens,input_tokens+output_tokens+cache_read_tokens+cache_creation_tokens,
             date(second,'unixepoch','localtime') FROM usage_cc_metrics WHERE app NOT IN ('''+native+''')
        AND cc_id NOT IN (SELECT cc_id FROM usage_cc_gemini_mirrors)
      ON CONFLICT(id) DO UPDATE SET timestamp=excluded.timestamp,model=excluded.model,thread_id=excluded.thread_id,
        input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,cache_read_tokens=excluded.cache_read_tokens,
        cache_creation_tokens=excluded.cache_creation_tokens,total_tokens=excluded.total_tokens,date=excluded.date
      WHERE usage_requests.timestamp IS NOT excluded.timestamp OR usage_requests.model IS NOT excluded.model
        OR usage_requests.thread_id IS NOT excluded.thread_id OR usage_requests.input_tokens IS NOT excluded.input_tokens
        OR usage_requests.output_tokens IS NOT excluded.output_tokens OR usage_requests.cache_read_tokens IS NOT excluded.cache_read_tokens
        OR usage_requests.cache_creation_tokens IS NOT excluded.cache_creation_tokens OR usage_requests.date IS NOT excluded.date''')
    report['imported'] = conn.execute("SELECT COUNT(*) FROM usage_requests WHERE id LIKE 'ccswitch:%'").fetchone()[0]-before
    phase('other_tools')
    # Match sequential scans in memory rather than random-joining two large
    # disk indexes. Bound new enrichment to 500k identities per scan; previous
    # detail snapshots outside that window are retained, never counted again.
    # Both source and native identities must be unique.
    keys = ('app','thread_id','model',*COUNTS,'second')
    metadata = ('cc_id','origin_request_id','duration_ms','first_token_ms','status_code','provider_id','provider_name','timing_estimated')
    matches = {}; active_cc_ids = set()
    cursor = conn.execute('SELECT * FROM usage_cc_metrics WHERE app IN ('+native+') ORDER BY rowid DESC LIMIT ?', (ENRICHMENT_LIMIT,))
    columns = [col[0] for col in cursor.description]
    for values in cursor:
        row = dict(zip(columns,values)); key=tuple(row[k] for k in keys)
        active_cc_ids.add(row['cc_id'])
        prior=matches.get(key)
        if prior is None or row['quality'] > prior[1]:
            matches[key]=[tuple(row[k] for k in metadata),row['quality'],False,None,False]
        elif row['quality']==prior[1]: prior[2]=True
    phase('metadata_lookup')
    native_cursor=conn.execute('SELECT app,thread_id,model,'+','.join(COUNTS)+',CAST(timestamp AS INTEGER),id FROM usage_requests NOT INDEXED WHERE app IN ('+native+')')
    for row in native_cursor:
        match=matches.get(tuple(row[:-1]))
        if match is not None:
            if match[2]: continue
            if match[3] is None: match[3]=row[-1]
            else: match[4]=True
    assignments = {native_id:value[0] for value,quality,source_ambiguous,native_id,native_ambiguous in matches.values()
                   if not source_ambiguous and not native_ambiguous and native_id is not None}
    # A streaming correction or a revised timestamp can invalidate a former
    # exact match without producing an ambiguous replacement. Revoke only
    # snapshots whose CC identity was actually rechecked in this window.
    # A sequential detail scan avoids hundreds of thousands of random seeks;
    # unchanged snapshots and identities outside the window are never rewritten.
    stale_ids = [request_id for request_id,cc_id in conn.execute('SELECT request_id,cc_id FROM usage_request_details NOT INDEXED')
                 if cc_id in active_cc_ids and assignments.get(request_id) != cc_id]
    if stale_ids:
        conn.executemany('DELETE FROM usage_request_details WHERE request_id=?',((value,) for value in stale_ids))
        conn.executemany('DELETE FROM usage_cc_detail_signatures WHERE request_id=?',((value,) for value in stale_ids))
    phase('native_match')
    pending=[]; signatures=[]
    known=dict(conn.execute('SELECT request_id,signature FROM usage_cc_detail_signatures'))
    detail_fields=('cc_id','origin_request_id','duration_ms','first_token_ms','status_code','provider_id','provider_name','timing_estimated','provenance')
    detail_sql='INSERT INTO usage_request_details (request_id,'+','.join(detail_fields)+') VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET '+','.join(k+'=excluded.'+k for k in detail_fields)+' WHERE '+' OR '.join('usage_request_details.'+k+' IS NOT excluded.'+k for k in detail_fields)
    for value,quality,source_ambiguous,native_id,native_ambiguous in matches.values():
        if source_ambiguous or native_ambiguous or native_id is None: continue
        provenance='CC Switch · 会话时间戳推算' if value[-1] else 'CC Switch · 代理实测'
        signature=hashlib.sha256(json.dumps((value,provenance),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        if known.get(native_id)==signature: continue
        pending.append((native_id,*value,provenance))
        signatures.append((native_id,signature))
        if len(pending)>=5000:
            conn.executemany(detail_sql,pending);conn.executemany('INSERT OR REPLACE INTO usage_cc_detail_signatures VALUES(?,?)',signatures);pending=[];signatures=[]
    if pending: conn.executemany(detail_sql,pending)
    if signatures: conn.executemany('INSERT OR REPLACE INTO usage_cc_detail_signatures VALUES(?,?)',signatures)
    phase('detail_updates')
    report['matched'] = conn.execute('SELECT COUNT(*) FROM usage_request_details').fetchone()[0]
    conn.execute('''INSERT OR REPLACE INTO usage_request_details
      (request_id,cc_id,origin_request_id,duration_ms,first_token_ms,status_code,provider_id,provider_name,timing_estimated,provenance)
      SELECT 'ccswitch:'||cc_id,cc_id,origin_request_id,duration_ms,first_token_ms,status_code,provider_id,provider_name,
             timing_estimated,CASE WHEN timing_estimated=1 THEN 'CC Switch · 会话时间戳推算' ELSE 'CC Switch · 代理实测' END
        FROM usage_cc_metrics WHERE app NOT IN ('''+native+''')
          AND cc_id NOT IN (SELECT cc_id FROM usage_cc_gemini_mirrors)''')
    conn.commit()
    return report


def decorate_logs(conn, rows):
    if not rows: return rows
    present = conn.execute("SELECT 1 FROM sqlite_master WHERE name='usage_request_details' AND type='table'").fetchone()
    if not present: return rows
    ids = [row['id'] for row in rows if row.get('id')]
    if not ids: return rows
    query = conn.execute('SELECT * FROM usage_request_details WHERE request_id IN ('+','.join('?' for _ in ids)+')',ids)
    names = [column[0] for column in query.description]
    details = {value[0]:dict(zip(names,value)) for value in query}
    cc_ids=[row['cc_id'] for row in details.values() if row.get('cc_id')]
    models={row[0]:row[1:] for row in conn.execute('SELECT cc_id,requested_model,pricing_model FROM usage_cc_models WHERE cc_id IN ('+','.join('?' for _ in cc_ids)+')',cc_ids)} if cc_ids else {}
    for row in rows:
        extra = details.get(row.get('id'))
        if extra is None: continue
        row.update({key:value for key,value in extra.items() if key not in ('request_id','cc_id')})
        if extra.get('cc_id') in models:
            row['requested_model'],row['pricing_model']=models[extra['cc_id']]
        duration, first, output = extra.get('duration_ms'),extra.get('first_token_ms'),row.get('output_tokens',0)
        estimated = bool(extra.get('timing_estimated'))
        speed = None
        if estimated and output >= 200 and duration is not None and duration >= 1000: speed = output * 1000 / duration
        elif not estimated and output >= 100 and duration is not None and first is not None and duration-first >= 100: speed = output * 1000/(duration-first)
        row['speed_tps'] = speed; row['timing_estimated'] = estimated
    return rows
