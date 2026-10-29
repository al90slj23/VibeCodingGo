"""Preserve CC's non-native daily archive without inventing request rows."""
from collections import defaultdict
from datetime import date, datetime, time as day_time
import hashlib
import json

NATIVE = ('codex','claude','opencode')
COUNTS = ('input_tokens','output_tokens','cache_read_tokens','cache_creation_tokens')


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS usage_archived_daily (
      id TEXT PRIMARY KEY,timestamp REAL NOT NULL,app TEXT NOT NULL,source TEXT NOT NULL,
      model TEXT NOT NULL,date TEXT NOT NULL,request_count INTEGER NOT NULL,
      input_tokens INTEGER NOT NULL,output_tokens INTEGER NOT NULL,cache_read_tokens INTEGER NOT NULL,
      cache_creation_tokens INTEGER NOT NULL,total_tokens INTEGER NOT NULL,provider_id TEXT,provider_name TEXT)''')


def sync(conn, source_conn, provider_names=None):
    from .usage_ccswitch import APP_NAMES,count,text
    ensure_schema(conn)
    report={'available':False,'groups':0,'requests':0,'invalid':0,'note':'归档仅补足其他工具的日汇总，不生成请求明细；原生三种工具不叠加 CC 归档。'}
    columns={row[1] for row in source_conn.execute('PRAGMA table_info(usage_daily_rollups)')}
    required={'date','app_type','model','provider_id','request_count',*COUNTS}
    if not required <= columns: return report
    selection=','.join(sorted(required | ({'input_token_semantics'} if 'input_token_semantics' in columns else set())))
    marks=','.join('?' for _ in NATIVE)
    raw_groups={}
    for raw in source_conn.execute('SELECT '+selection+' FROM usage_daily_rollups WHERE app_type NOT IN ('+marks+')',NATIVE):
        row=dict(raw);app=text(row['app_type'],40)
        if app not in APP_NAMES: continue
        try:
            d=date.fromisoformat(row['date'])
            if d.isoformat()!=row['date']: raise ValueError()
        except (ValueError,TypeError): report['invalid']+=1;continue
        values=[count(row[key]) for key in COUNTS];requests=count(row['request_count'])
        if requests is None or any(value is None for value in values): report['invalid']+=1;continue
        if app in ('gemini','grokbuild') and row.get('input_token_semantics',0)!=2:
            deduction=values[2]+(values[3] if row.get('input_token_semantics')==1 else 0)
            if values[0]<deduction: report['invalid']+=1;continue
            values[0]-=deduction
        key=(d.isoformat(),app,text(row['provider_id']),text(row['model']) or 'unknown')
        target=raw_groups.setdefault(key,[0,0,0,0,0])
        for index,value in enumerate((requests,*values)): target[index]+=value
    if not raw_groups:
        report['available']=True
        return report
    # CC's daily rows aggregate records it has removed from request logs. We
    # may already retain some of those records: subtract only retained records
    # no longer present in CC, never the still-live requests from the same day.
    current={hashlib.sha256(text(raw[0],240).encode()).hexdigest() for raw in source_conn.execute('SELECT request_id FROM proxy_request_logs WHERE app_type NOT IN ('+marks+')',NATIVE)}
    retained=defaultdict(lambda:[0,0,0,0,0])
    exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_cc_metrics'").fetchone()
    if exists:
        for raw in conn.execute('SELECT * FROM usage_cc_metrics WHERE app NOT IN ('+marks+')',NATIVE):
            row=dict(raw)
            if row['cc_id'] in current: continue
            # Only subtract records that really entered our durable ledger.
            if not conn.execute('SELECT 1 FROM usage_requests WHERE id=?',('ccswitch:'+row['cc_id'],)).fetchone(): continue
            key=(datetime.fromtimestamp(row['second']).date().isoformat(),row['app'],row['provider_id'],row['model'])
            target=retained[key]
            for index,value in enumerate((1,*(row[k] for k in COUNTS))): target[index]+=value
    for key,total in raw_groups.items():
        identity='ccarchive:'+hashlib.sha256(json.dumps(key,separators=(',',':')).encode()).hexdigest()
        values=[left-right for left,right in zip(total,retained[key])]
        if any(value<0 for value in values) or (values[0]==0 and any(values[1:])):
            conn.execute('DELETE FROM usage_archived_daily WHERE id=?',(identity,));report['invalid']+=1;continue
        if not values[0]: conn.execute('DELETE FROM usage_archived_daily WHERE id=?',(identity,));continue
        day,app,provider,model=key
        provider_name=(provider_names or {}).get((app,provider)) or ('会话日志' if provider.startswith('_') else provider or '未记录供应商')
        stamp=datetime.combine(date.fromisoformat(day),day_time(12)).timestamp()
        record=(identity,stamp,app,'ccswitch:archive',model,day,values[0],*values[1:],sum(values[1:]),provider,provider_name)
        conn.execute('INSERT OR REPLACE INTO usage_archived_daily VALUES('+','.join('?' for _ in record)+')',record)
        report['groups']+=1;report['requests']+=values[0]
    report['available']=True
    if report['invalid']:report['note']+=' 部分归档与已保留明细不一致，保留明细并跳过不确定残量。'
    return report
