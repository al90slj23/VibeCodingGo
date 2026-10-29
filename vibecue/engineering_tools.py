"""Explicit, finite engineering tools; no work occurs merely by opening a tab."""
from __future__ import annotations

import argparse
from collections import defaultdict, deque
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time

from .engineering_runtime import EngineeringError, MAX_INPUT, Runtime, redact

SCHEMA_VERSION = 1
CORE_TOOLS = ('node', 'npm', 'pnpm', 'python3', 'go', 'git')
OPTIONAL_TOOLS = ('mise', 'hurl', 'lnav')
MAX_PROJECT_FILE = 128 * 1024
MAX_LOG_FILE = 512 * 1024
MAX_LOG_TOTAL = 2 * 1024 * 1024
MAX_LOG_LINES = 1000
MAX_ROW_TEXT = 2400


def response(kind, title):
    return {'schema_version': SCHEMA_VERSION, 'kind': kind, 'status': 'success',
            'title': title, 'summary': '', 'engine': 'builtin', 'items': [],
            'rows': [], 'warnings': [], 'metrics': {'duration_ms': 0, 'bytes_read': 0, 'truncated': False}}


def absolute(value, *, directory=False):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > 4096:
        raise EngineeringError('请选择有效的绝对路径。')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise EngineeringError('请选择绝对路径，不能使用相对路径。')
    try:
        path = path.resolve(strict=True)
        valid = path.is_dir() if directory else path.is_file()
    except (OSError, RuntimeError):
        valid = False
    if not valid:
        raise EngineeringError('所选目录不可读。' if directory else '所选文件不可读或不是普通文件。')
    return path


def _read_project(path):
    # Known files only. Do not follow a repository symlink outside its project.
    try:
        meta = path.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > MAX_PROJECT_FILE:
            return None
        with path.open('rb') as stream:
            data = stream.read(MAX_PROJECT_FILE+1)
        if len(data) > MAX_PROJECT_FILE:
            return None
        return data.decode('utf-8')
    except (OSError, UnicodeError):
        return None


def _requirements(project):
    requirements = []
    positions = {}
    warnings = []
    def add(tool, expected, source):
        tool = {'nodejs': 'node', 'python': 'python3'}.get(tool, tool)
        if tool in CORE_TOOLS and isinstance(expected, str) and 0 < len(expected) <= 160:
            row = {'tool': tool, 'expected': redact(expected.strip(),160), 'source': source}
            key = (tool, source)
            if key in positions: requirements[positions[key]] = row
            else: positions[key] = len(requirements); requirements.append(row)
    package = _read_project(project/'package.json')
    if package is not None:
        try:
            parsed = json.loads(package)
            engines = parsed.get('engines', {})
            if isinstance(engines, dict):
                for tool, version in engines.items(): add(tool, version, 'package.json · engines')
            manager = parsed.get('packageManager')
            if isinstance(manager, str) and '@' in manager:
                name, version = manager.split('@', 1)
                add(name, version.split('+', 1)[0], 'package.json · packageManager')
        except (ValueError, AttributeError):
            warnings.append('package.json 格式不可读，未执行其中脚本。')
    for name, tool in [('.nvmrc', 'node'), ('.node-version', 'node'), ('.python-version', 'python3'), ('.go-version', 'go')]:
        value = _read_project(project/name)
        if value is not None and value.strip(): add(tool, value.strip().splitlines()[0], name)
    version_file = _read_project(project/'.tool-versions')
    if version_file is not None:
        for line in version_file.splitlines()[:100]:
            bits = line.split('#', 1)[0].split()
            if len(bits) >= 2: add(bits[0], ' || '.join(bits[1:]), '.tool-versions')
    for name in ['mise.toml', '.mise.toml']:
        value = _read_project(project/name)
        if value is None: continue
        section = ''
        for line in value.splitlines()[:2000]:
            line = line.strip()
            if line.startswith('['): section = line; continue
            if section != '[tools]': continue
            match = re.fullmatch(r'["\']?(node|nodejs|python|python3|go|npm|pnpm|git)["\']?\s*=\s*["\']([^"\']{1,160})["\']\s*(?:#.*)?', line)
            if match: add(match[1], match[2], name+' · tools')
        if '{{' in value or 'exec(' in value:
            warnings.append(name+' 含动态表达式；仅检查静态工具版本，不执行表达式。')
    go_mod = _read_project(project/'go.mod')
    if go_mod:
        match = re.search(r'(?m)^go\s+(\d+(?:\.\d+){1,2})\s*$', go_mod)
        if match: add('go', '>='+match[1], 'go.mod · go')
    for name in ['package.json', '.nvmrc', '.node-version', '.python-version', '.go-version', '.tool-versions', 'mise.toml', '.mise.toml', 'go.mod']:
        path = project/name
        if path.exists() and _read_project(path) is None:
            warnings.append(name+' 超过读取上限、为符号链接或不可读，已跳过。')
    return requirements, warnings


def version_match(expected, actual):
    """Strict small semver subset; unsupported syntax remains unverified."""
    found = re.search(r'(?<![A-Za-z0-9_.])(?:v|go)?(\d+(?:\.\d+){0,2})(-[A-Za-z0-9.]+)?(?![A-Za-z0-9_.])', str(actual))
    if found is None or found[2]: return None
    current = tuple(int(x) for x in found[1].split('.'))
    current += (0,)*(3-len(current))
    def conjunction(group):
        atoms = group.strip().split()
        if not atoms: return None
        answers = []
        for atom in atoms:
            match = re.fullmatch(r'(>=|<=|>|<|\^|~|=)?v?(\d+)(?:\.(\d+|x|\*))?(?:\.(\d+|x|\*))?', atom)
            if not match: return None
            op, *pieces = match.groups()
            numeric = []
            for piece in pieces:
                if piece is None or piece in ('x', '*'): break
                numeric.append(int(piece))
            base = tuple(numeric + [0]*(3-len(numeric)))
            # Partial > / <= ranges have different expansion rules across
            # npm SemVer and Python constraints. Do not guess by zero padding.
            if op in ('>', '<=') and len(numeric) < 3:
                return None
            if op in ('>=', '>', '<=', '<'):
                answers.append({'>=': current >= base, '>': current > base, '<=': current <= base, '<': current < base}[op])
            elif op in ('^', '~'):
                if op == '~': upper = (base[0]+1, 0, 0) if len(numeric)==1 else (base[0],base[1]+1,0)
                elif base[0] > 0: upper = (base[0]+1, 0, 0)
                elif len(numeric)==1: upper = (1,0,0)
                elif base[1] > 0 or len(numeric)==2: upper = (0,base[1]+1,0)
                else: upper = (0,0,base[2]+1)
                answers.append(base <= current < upper)
            else: answers.append(current[:len(numeric)] == tuple(numeric))
        return all(answers)
    values = [conjunction(group) for group in expected.split('||')]
    if any(value is True for value in values): return True
    return None if None in values else False


def capabilities(payload=None, runtime=None):
    runtime = runtime or Runtime(timeout=15)
    result = response('capabilities', '工程工具能力')
    result['capabilities'] = {name: runtime.capability(name) for name in OPTIONAL_TOOLS}
    for name, capability in result['capabilities'].items():
        confirmed = capability.get('available') is True and bool(capability.get('version'))
        value = capability['version'] if confirmed else '已找到，版本未核实' if capability.get('available') else '未安装'
        result['items'].append({'label': name, 'value': value,
                                'status': 'success' if confirmed else 'unknown',
                                'detail': capability.get('path') or '可使用原生后备；安装开源 CLI 后按需启用。'})
        if not confirmed:
            result['status'] = 'partial'

    result['summary'] = '仅本次探测 · 不安装工具、不启动后台服务'
    return result


def environment(payload, runtime=None):
    runtime = runtime or Runtime(timeout=22)
    project = absolute(payload.get('project_path'), directory=True)
    began = time.monotonic(); result = response('environment', '环境体检')
    requirements, warnings = _requirements(project)
    result['project_path'] = str(project)
    result['warnings'] = warnings
    checked = {name: runtime.capability(name) for name in CORE_TOOLS + OPTIONAL_TOOLS}
    result['capabilities'] = checked
    # Reuse this run's already bounded probes. Showing optional engine identity
    # must never trigger another process or imply that an installed engine was
    # used for the project checks below.
    for name in OPTIONAL_TOOLS:
        tool = checked[name]
        confirmed = tool.get('available') is True and bool(tool.get('version'))
        detail = tool.get('path') or '未找到；本次运行保留原生后备。'
        if tool.get('version_error'):
            detail += ' · ' + tool['version_error']
        result['items'].append({'label': '工程引擎 · '+name,
                                'value': tool['version'] if confirmed else '已找到，版本未核实' if tool.get('available') else '未安装',
                                'status': 'success' if confirmed else 'unknown',
                                'detail': detail})
    for name in CORE_TOOLS:
        tool = checked[name]
        result['items'].append({'label': name, 'value': tool['version'] or ('已找到，版本未核实' if tool['available'] else '未找到'),
                                'status': 'success' if tool['version'] else 'unknown',
                                'detail': tool.get('version_error') or tool['path'] or '未在常用工具目录找到。'})
    issues = 0
    for requirement in requirements:
        tool = checked[requirement['tool']]
        matches = version_match(requirement['expected'], tool['version']) if tool['version'] else None
        status = 'success' if matches is True else 'error' if matches is False or not tool['available'] else 'unknown'
        issues += status != 'success'
        requirement['status'] = status
        requirement['actual'] = tool['version']
        result['items'].append({'label': requirement['tool']+' · '+requirement['source'],
                                'value': '要求 '+requirement['expected'], 'status': status,
                                'detail': '当前 '+str(tool['version'] or '未核实')+' · '+('符合' if status=='success' else '不符合或工具缺失' if status=='error' else '约束需要人工核对')})
    result['requirements'] = requirements
    mise = checked['mise']
    if mise['available'] and not runtime.cancelled:
        installed = 0; parsed_tools = 0
        with tempfile.TemporaryDirectory(prefix='vcgo-mise-readonly-') as directory:
            for tool_name in ('node', 'python', 'go'):
                run = runtime.run([mise['path'], '--no-config', 'ls', '--installed', '--json', tool_name],
                                  timeout=1, max_output=65536, cwd=directory,
                                  env={'MISE_NO_CONFIG':'1', 'MISE_PARANOID':'1', 'MISE_OFFLINE':'1', 'MISE_AUTO_INSTALL':'false', 'MISE_CACHE_DIR':directory+'/cache', 'MISE_STATE_DIR':directory+'/state'})
                if run.returncode != 0 or run.truncated:
                    result['warnings'].append('mise · '+tool_name+' 清单未完成，已保留原生静态体检。')
                    continue
                try:
                    parsed = json.loads(run.stdout)
                    if not isinstance(parsed, list) or len(parsed)>1024: raise ValueError
                    versions = [redact(row['version'],80) for row in parsed[:32] if isinstance(row,dict) and row.get('installed') is not False and isinstance(row.get('version'),str)]
                    parsed_tools += 1; installed += len(versions)
                    result['items'].append({'label':'mise · '+tool_name, 'value':', '.join(versions)[:400] or '无已安装版本', 'status':'success' if versions else 'unknown', 'detail':'已安装版本清单；不代表当前项目已激活该版本。'})
                except (ValueError, TypeError):
                    result['warnings'].append('mise · '+tool_name+' 清单格式暂不支持，已保留原生静态体检。')
        if parsed_tools:
            result['engine'] = 'builtin+mise'
            result['engine_detail'] = 'mise --no-config 读取已安装版本；项目需求由本机静态解析。'
            result['mise_installed_versions'] = installed
    else:
        result['warnings'].append('mise 未安装；当前使用原生静态体检。安装后可额外读取其已安装版本清单。')
    if runtime.cancelled: result['status'] = 'cancelled'
    elif issues or result['warnings']: result['status'] = 'partial'
    result['summary'] = str(len(requirements))+' 项版本要求 · '+str(issues)+' 项需要处理或核对 · 只读检查'
    if not requirements: result['warnings'].append('未发现支持的静态版本声明；当前结果为实际工具探测。')
    result['metrics']['duration_ms'] = round((time.monotonic()-began)*1000, 2)
    return result


def _level(text):
    match = re.search(r'\b(fatal|critical|crit|error|err|warning|warn|info|debug|trace)\b', text, re.I)
    if not match: return 'unknown'
    value = match[1].lower()
    return {'fatal':'error', 'critical':'error','crit':'error','err':'error','warn':'warning'}.get(value,value)


def _timestamp(text):
    match = re.match(r'\[?(\d{4}-\d\d-\d\d[T ][0-2]\d:[0-5]\d:[0-5]\d(?:[.,]\d{1,9})?(?:Z|[+-]\d\d:?\d\d)?)',text)
    return match[1] if match else None


def _tail(path, budget):
    # O_NONBLOCK avoids waiting on a replaced FIFO; fstat checks the actual fd.
    descriptor = os.open(path, os.O_RDONLY | getattr(os,'O_NONBLOCK',0) | getattr(os,'O_NOFOLLOW',0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode): raise EngineeringError('只支持普通日志文件。')
        count = min(before.st_size, MAX_LOG_FILE, budget)
        offset = max(0,before.st_size-count)
        os.lseek(descriptor, offset, os.SEEK_SET)
        data = bytearray()
        while len(data) < count:
            block = os.read(descriptor,min(65536,count-len(data)))
            if not block: break
            data.extend(block)
        after = os.fstat(descriptor)
        try: named = Path(path).stat()
        except OSError: named = None
        rotated = named is None or (named.st_dev,named.st_ino)!=(before.st_dev,before.st_ino)
        changed = (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or rotated
        consumed = len(data)
        if offset:
            linebreak = data.find(b'\n')
            data = data[linebreak+1:] if linebreak >= 0 else bytearray()
        text = bytes(data).decode('utf-8','replace')
        return text, consumed, {'source':str(path),'size_bytes':before.st_size,'tail_offset_bytes':offset,
                                'line_scope':'tail' if offset else 'file','changed_during_read':changed,
                                'rotated_during_read':rotated,'modified_at':before.st_mtime}
    finally:
        os.close(descriptor)


def _lnav_enrich(tails, rows, runtime):
    executable = runtime.find_tool('lnav')
    if not executable or not rows: return 0, None
    with tempfile.TemporaryDirectory(prefix='vcgo-log-snapshot-') as directory:
        root = Path(directory).resolve(); files=[]; sources={}
        for index,(source,text) in enumerate(tails):
            file=root/('part-'+str(index)+'.log')
            # Only bounded, already redacted snapshots reach the optional tool.
            file.write_text(text);files.append(str(file));sources[str(file)]=source
        sql=';SELECT log_path,log_time,log_level,log_text FROM all_logs ORDER BY log_line LIMIT 1000'
        run=runtime.run([executable,'-n','-N','-q','-c',sql,'-c',':write-json-to -',*files],timeout=5,max_output=MAX_LOG_TOTAL,
                        cwd=directory,env={'HOME':directory,'XDG_CONFIG_HOME':directory+'/config','XDG_CACHE_HOME':directory+'/cache','XDG_STATE_HOME':directory+'/state'})
        if run.returncode != 0 or run.truncated: return 0, 'lnav 未完成或输出超过上限，已保留原生诊断。'
        try:
            parsed=json.loads(run.stdout)
            if not isinstance(parsed,list) or len(parsed)>MAX_LOG_LINES:raise ValueError
            lookup=defaultdict(deque)
            for row in rows:lookup[(row['source'],row['text'])].append(row)
            matched=0
            for item in parsed:
                if not isinstance(item,dict):continue
                source=sources.get(item.get('log_path'))
                text=item.get('log_text')
                if not source or not isinstance(text,str):continue
                choices=lookup.get((source,redact(text,MAX_ROW_TEXT)))
                if not choices:continue
                row=choices.popleft();level=str(item.get('log_level') or '').lower()
                if level in ('fatal','critical','error','warning','warn','info','debug','trace'):
                    row['level']='warning' if level=='warn' else 'error' if level in ('fatal','critical') else level
                if row['timestamp'] is None and isinstance(item.get('log_time'),str):row['timestamp']=redact(item['log_time'],80)
                row['parser']='lnav';matched+=1
            return matched, None if matched else 'lnav 未识别所选尾部格式，已保留原生诊断。'
        except (ValueError,TypeError):return 0,'lnav 返回格式暂不支持，已保留原生诊断。'


def logs(payload, runtime=None):
    runtime=runtime or Runtime(timeout=15);began=time.monotonic();result=response('logs','日志诊断')
    paths=payload.get('paths')
    if not isinstance(paths,list) or not 1<=len(paths)<=5:raise EngineeringError('请选择 1 至 5 个日志文件。')
    query=payload.get('query','')
    if not isinstance(query,str) or len(query)>512:raise EngineeringError('查找内容最多 512 字符。')
    level=payload.get('level','all')
    if level not in ('all','error','warning'):raise EngineeringError('日志级别筛选无效。')
    maximum=payload.get('maximum_lines',200)
    if type(maximum) is not int or not 1<=maximum<=MAX_LOG_LINES:raise EngineeringError('最多显示 1 至 1000 行。')
    unique=[];seen=set()
    for value in paths:
        path=absolute(value)
        if str(path) not in seen:unique.append(path);seen.add(str(path))
    tails=[];rows=[];bytes_read=0;sources=[]
    for path in unique:
        if runtime.cancelled:break
        budget=MAX_LOG_TOTAL-bytes_read
        if budget<=0:
            result['warnings'].append('达到本次 2 MiB 读取上限，其余文件未读取。');result['metrics']['truncated']=True;break
        try:text,consumed,meta=_tail(path,budget)
        except (OSError,EngineeringError):
            result['warnings'].append(path.name+' 不可读，未尝试授权或重试。');continue
        bytes_read+=consumed;sources.append(meta)
        if meta['changed_during_read']:result['warnings'].append(path.name+' 在读取期间变化；结果来自打开文件的有限快照，请重新运行核对。')
        raw_lines=redact(text,MAX_LOG_FILE*2).splitlines()
        # Keep the newest bounded lines per source before invoking any parser.
        start=max(0,len(raw_lines)-MAX_LOG_LINES);safe_lines=[]
        for number,line in enumerate(raw_lines[start:],start+1):
            if len(line)>MAX_ROW_TEXT: result['metrics']['truncated']=True
            safe=redact(line,MAX_ROW_TEXT);safe_lines.append(safe)
            rows.append({'timestamp':_timestamp(safe),'level':_level(safe),'source':str(path),'line':number,
                         'line_scope':meta['line_scope'],'text':safe,'parser':'builtin'})
        tails.append((str(path),'\n'.join(safe_lines)+'\n'))
        result['metrics']['truncated']=result['metrics']['truncated'] or meta['tail_offset_bytes']>0 or start>0
    enriched,warning=_lnav_enrich(tails,rows,runtime)
    if warning:result['warnings'].append(warning)
    if enriched:result['engine']='builtin+lnav'
    if not runtime.find_tool('lnav'):result['warnings'].append('lnav 未安装；当前使用原生有界尾读、时间与级别识别。')
    selected=[row for row in rows if (level=='all' or row['level']==level) and query.casefold() in row['text'].casefold()]
    # ISO-like timestamps sort naturally; unknown timestamps remain stable.
    def order(row):
        try:
            stamp=datetime.fromisoformat((row['timestamp'] or '').replace('Z','+00:00').replace(',','.')).timestamp()
        except (ValueError,TypeError,OverflowError,OSError):stamp=float('-inf')
        return stamp,row['source'],row['line']
    selected.sort(key=order)
    if len(selected)>maximum:result['metrics']['truncated']=True
    selected=selected[-maximum:]
    # Bound the final UI document independently of source and parser budgets.
    output=[];characters=0
    for row in reversed(selected):
        cost=len(json.dumps(row,ensure_ascii=False,separators=(',',':')).encode('utf-8'))+1
        if characters+cost>180000:
            result['metrics']['truncated']=True;break
        output.append(row);characters+=cost
    result['rows']=list(reversed(output));result['sources']=sources
    result['engine_detail']='lnav 解析 '+str(enriched)+' 行；其余保留原生识别。' if enriched else '原生有界尾读；没有扫描目录或完整历史。'
    result['metrics'].update(duration_ms=round((time.monotonic()-began)*1000,2),bytes_read=bytes_read,lnav_enriched_rows=enriched)
    result['items']=[{'label':'读取范围','value':str(len(sources))+' 个文件 · '+str(bytes_read)+' bytes','status':'success','detail':'每文件最多 512 KiB，本次总计最多 2 MiB。'},
                     {'label':'行号范围','value':'原文件或尾部片段行号','status':'unknown' if any(s['line_scope']=='tail' for s in sources) else 'success','detail':'截取尾部时不扫描前文，line_scope=tail 的行号不是原文件绝对行号。'}]
    result['summary']=str(len(output))+' 行匹配 · '+str(len(sources))+' 个文件 · 单次读取'
    if runtime.cancelled:result['status']='cancelled'
    elif not sources:result['status']='error'
    elif result['warnings'] or result['metrics']['truncated']:result['status']='partial'
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description='VibeCoding Go 工程工具')
    parser.add_argument('command',choices=('environment','api','logs','capabilities'))
    args=parser.parse_args(argv)
    runtime=Runtime(timeout=40)
    from .engineering_install import tool_use_lock, InstallError as ToolInstallError
    with runtime.signals():
        try:
            data=sys.stdin.buffer.read(MAX_INPUT+1)
            if len(data)>MAX_INPUT:raise EngineeringError('工程工具输入超过 256 KiB 上限。')
            payload=json.loads(data) if data.strip() else {}
            if not isinstance(payload,dict):raise EngineeringError('工程工具输入必须是 JSON 对象。')
            configured_home=os.environ.get('VIBECUE_HOME','')
            home=Path(configured_home) if os.path.isabs(configured_home) else Path.home()/'Library/Application Support/VibeCue'
            with tool_use_lock(home,nonblocking=True) as acquired:
                if not acquired:raise EngineeringError('工程工具正在切换版本，请稍后重试。')
                if args.command=='api':
                    from . import engineering_api
                    result=engineering_api.run(payload,runtime=runtime)
                else:result={'environment':environment,'logs':logs,'capabilities':capabilities}[args.command](payload,runtime=runtime)
            result.setdefault('schema_version',SCHEMA_VERSION);result.setdefault('kind',args.command)
            print(json.dumps(result,ensure_ascii=False,separators=(',',':'),allow_nan=False))
            return 0
        except (EngineeringError,ToolInstallError,ValueError,TypeError,OSError,RecursionError) as error:
            result=response(args.command,'工程工具')
            message=str(error) if isinstance(error,(EngineeringError,ToolInstallError)) else '输入无效或文件暂不可读，请检查所选内容后重试。'
            result.update(status='cancelled' if runtime.cancelled else 'error',summary=message)
            print(json.dumps(result,ensure_ascii=False,separators=(',',':')))
            return 0


if __name__=='__main__':raise SystemExit(main())
