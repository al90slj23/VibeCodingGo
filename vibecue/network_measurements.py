"""Finite Globalping and download measurements; no work occurs on import.

The caller supplies the network API namespace to keep its error types and
transport implementation identical for CLI, tests and embedded app callers.
Only a known measurement ID is retained for explicit GET-only continuation.
"""
from __future__ import annotations

import json
import re

_ID = re.compile(r'[A-Za-z0-9_-]{1,100}')
_NODE_NAMES = {'finished': '已完成', 'failed': '检测失败', 'offline': '探针离线',
               'timeout': '节点超时', 'error': '节点错误', 'in-progress': '检测中'}


def _is_cancelled(client):
    value = getattr(client, 'cancelled', False)
    return value is True


def _count(value, maximum):
    return value if type(value) is int and 1 <= value <= maximum else None


def _quota(client):
    meta = getattr(client, 'last_response_meta', None)
    source = meta.get('quota') if isinstance(meta, dict) else None
    if not isinstance(source, dict):
        return {}
    return {key: value for key in ('limit', 'remaining', 'reset')
            if type(value := source.get(key)) in (int, float) and 0 <= value <= 10**12}


def _node(item, kind, api):
    if not isinstance(item, dict):
        return None
    probe = item.get('probe')
    answer = item.get('result')
    if not isinstance(probe, dict) or not isinstance(answer, dict):
        return None
    # MeasurementResultItem.probe is a flat ProbeLocation in the official API.
    # Accept legacy nested snapshots without mistaking an unknown shape for it.
    location = probe.get('location') if isinstance(probe.get('location'), dict) else probe
    country = api.text(location.get('country'), 2).upper()
    if re.fullmatch(r'[A-Z]{2}', country) is None:
        country = ''
    city = api.text(location.get('city'), 60)
    network = api.text(location.get('network'), 100)
    asn = location.get('asn')
    asn = asn if type(asn) is int and 1 <= asn <= 4294967295 else None
    status = api.text(answer.get('status'), 40)
    status = status if re.fullmatch(r'[a-z][a-z-]{0,39}', status) else 'unknown'
    node = {'country': country or None, 'city': city or None, 'asn': asn,
            'network': network or None, 'status': status, 'terminal': status != 'in-progress' and status != 'unknown',
            'usable': False, 'reachable': None}
    try:
        node['resolved_address'] = api.public_ip(answer.get('resolvedAddress'))
    except api.TestError:
        node['resolved_address'] = None
    details = [_NODE_NAMES.get(status, '节点状态未识别')]
    error = answer.get('error') if isinstance(answer.get('error'), dict) else {}
    error_kind = {'ENOTFOUND':'DNS 未解析','EAI_AGAIN':'DNS 暂不可用','ETIMEDOUT':'节点超时','ECONNREFUSED':'目标拒绝连接','ECONNRESET':'连接被重置','CERT_HAS_EXPIRED':'证书已过期','UNABLE_TO_VERIFY_LEAF_SIGNATURE':'证书链未通过验证','TLS_ERROR':'TLS 验证失败'}.get(error.get('type'))
    if error_kind: details.append(error_kind)
    node['error_category'] = error_kind
    if asn:
        details.append('AS' + str(asn))
    if network:
        details.append(network)
    label = ' · '.join(filter(None, (dict(api.REGIONS).get(country, country), city))) or '位置未确认的探针'
    if kind == 'ping':
        stats = answer.get('stats') if isinstance(answer.get('stats'), dict) else {}
        avg = api.finite(stats.get('avg'), maximum=60000)
        minimum = api.finite(stats.get('min'), maximum=60000)
        maximum = api.finite(stats.get('max'), maximum=60000)
        loss = api.finite(stats.get('loss'), maximum=100)
        packets = {key: value for key in ('total', 'rcv', 'drop')
                   if type(value := stats.get(key)) is int and 0 <= value <= 100}
        node.update(avg_ms=avg, min_ms=minimum, max_ms=maximum, loss_percent=loss, packets=packets)
        node['usable'] = status == 'finished' and ((avg is not None and loss is not None) or loss == 100)
        node['reachable'] = bool(avg is not None and loss is not None and loss < 100) if status == 'finished' and loss is not None else None
        value = f'{avg:g} ms' if avg is not None else '100% 丢包' if loss == 100 else _NODE_NAMES.get(status, '未确认')
        details.append(f'丢包 {loss:g}%' if loss is not None else '丢包率未确认')
        if minimum is not None and maximum is not None:
            details.append(f'最小/最大 {minimum:g}/{maximum:g} ms')
        if 'rcv' in packets and 'total' in packets:
            details.append(f'收到 {packets["rcv"]}/{packets["total"]} 包')
    else:
        code = answer.get('statusCode')
        code = code if type(code) is int and 100 <= code <= 599 else None
        timings = answer.get('timings') if isinstance(answer.get('timings'), dict) else {}
        timings = {key: value for key in ('dns', 'tcp', 'tls', 'firstByte', 'download', 'total')
                   if (value := api.finite(timings.get(key), maximum=120000)) is not None}
        tls = answer.get('tls') if isinstance(answer.get('tls'), dict) else {}
        authorized = tls.get('authorized') if type(tls.get('authorized')) is bool else None
        protocol = api.text(tls.get('protocol'), 40) or None
        node.update(http_status=code, timings_ms=timings, tls_authorized=authorized, tls_protocol=protocol)
        node['usable'] = status == 'finished' and code is not None and authorized is not False
        node['reachable'] = code is not None if status == 'finished' else None
        value = 'HTTP ' + str(code) if code is not None else _NODE_NAMES.get(status, '未确认')
        for key, title in (('dns', 'DNS'), ('tcp', 'TCP'), ('tls', 'TLS'), ('firstByte', '首字节'), ('total', '总耗时')):
            if key in timings:
                details.append(title + f' {timings[key]:g} ms')
        if authorized is not None:
            details.append('TLS 证书已验证' if authorized else 'TLS 证书未验证')
        if protocol:
            details.append(protocol)
        if code is not None and code >= 400:
            details.append('服务已响应；拒绝不等于全网封禁')
    display = api.row(label, value, ' · '.join(details))
    display['status'] = 'ok' if node['usable'] and node['reachable'] is not False else 'partial'
    if kind=='http' and node.get('http_status') is not None and node['http_status']>=400 and node['usable']:
        display['status']='warning'
    return node, display


def global_test(params, client, kind, *, api):
    host = api.target_host(params.get('target'))
    allowed = dict(api.REGIONS)
    requested = params.get('regions')
    if requested is None:
        countries = list(allowed)[:params.get('probe_limit', 3)]
    elif isinstance(requested, list) and 1 <= len(requested) <= api.PROBE_LIMIT and all(isinstance(v, str) and v in allowed for v in requested):
        countries = list(dict.fromkeys(requested))[:params.get('probe_limit', 3)]
    else:
        raise api.TestError('请选择受支持的检测地区（最多 6 个）。')
    measure_type = 'ping' if kind == 'global_ping' else 'http'
    resumed = params.get('measurement_id') is not None
    identifier = params.get('measurement_id')
    if resumed and (not isinstance(identifier, str) or _ID.fullmatch(identifier) is None):
        raise api.TestError('现有测量 ID 无效；未发起或重发测量。')
    started = client.clock()
    deadline = started + api.GLOBAL_SECONDS
    reported_count = None
    notes = []
    quota = {}
    if not resumed:
        # Exactly one probe in each requested country. Global limit cannot be
        # mixed with per-location limits and doesn't guarantee country coverage.
        body = {'type': measure_type, 'target': host, 'timeout': 15,
                'locations': [{'country': country, 'limit': 1} for country in countries],
                'inProgressUpdates': True}
        body['measurementOptions'] = {'packets': 3} if measure_type == 'ping' else {'request': {'method': 'HEAD'}, 'protocol': 'HTTPS'}
        try:
            # Reserve >= 10 seconds after the 15-second probe wall clock for API
            # finalization. A slow/lost creation response is never replayed.
            created = client.json(api.GLOBALPING, deadline=min(deadline, started + 10), method='POST', json_body=body)
        except api.RateLimited:
            raise
        except api.TestError as error:
            if getattr(error,'code',None)=='cancelled' or _is_cancelled(client): raise
            raise api.TestError('全球测量提交结果未确认；不会自动重发。') from None
        if not isinstance(created, dict):
            raise api.TestError('全球测量没有返回可核对的测量 ID；提交可能已发生，不会自动重发。')
        identifier = created.get('id')
        if not isinstance(identifier, str) or _ID.fullmatch(identifier) is None:
            raise api.TestError('全球测量没有返回可核对的测量 ID；提交可能已发生，不会自动重发。')
        reported_count = _count(created.get('probesCount'), api.PROBE_LIMIT)
        quota = _quota(client)
    address = api.GLOBALPING + '/' + identifier
    latest, etag, polls = None, None, 0
    terminal = cancelled = invalid_reply = False
    problem, retry_after = '', None
    while client.clock() + .05 < deadline:
        if _is_cancelled(client):
            cancelled = True; problem = '读取已取消，保留测量 ID；服务端测量可能继续。'; break
        try:
            if polls:
                # Wait after completion of each response, not on a fixed timer.
                client.sleep(min(.6, max(0, deadline-client.clock())))
            if client.clock() + .05 >= deadline:
                break
            response = client.request(address, deadline=deadline, etag=etag)
            polls += 1
            if response['status'] == 304:
                continue
            if response['status'] != 200:
                problem = '测量结果暂不可读取；保留已有结果和测量 ID，不重新提交。'; break
            try:
                value = json.loads(response['body'])
            except (ValueError, UnicodeError, TypeError, RecursionError):
                problem = '测量返回格式暂无法识别；未采信新数据，保留测量 ID。'; invalid_reply = True; break
            if (not isinstance(value, dict) or value.get('id') != identifier or
                    value.get('target') != host or value.get('type') != measure_type):
                problem = '测量结果的标识、目标或类型与本次不一致；未采信新数据。'; invalid_reply = True; break
            state = value.get('status')
            if not isinstance(state, str) or not re.fullmatch(r'[a-z][a-z-]{0,39}', state):
                problem = '测量状态无效；未采信新数据，保留测量 ID。'; invalid_reply = True; break
            latest = value
            etag = api.text(response.get('etag'), 200) or None
            count = _count(value.get('probesCount'), api.PROBE_LIMIT)
            if reported_count is None:
                reported_count = count
            elif count is not None and count != reported_count:
                notes.append('创建与结果返回的探针数量不一致；未将覆盖标为完整。')
            # The official contract defines every non-in-progress state as final.
            if state != 'in-progress':
                terminal = True; break
        except api.TestError as error:
            cancelled = _is_cancelled(client) or getattr(error, 'code', None) == 'cancelled' or error.__class__.__name__ in ('Cancelled', 'CancelledError')
            retry_after = api.finite(getattr(error, 'retry_after_seconds', None), maximum=86400)
            problem = '读取已取消，保留测量 ID；服务端测量可能继续。' if cancelled else str(error)
            break
    raw_results = latest.get('results') if isinstance(latest, dict) else None
    probes, displays = [], []
    if isinstance(raw_results, list):
        for item in raw_results[:api.PROBE_LIMIT]:
            parsed = _node(item, measure_type, api)
            if parsed:
                node, display = parsed; probes.append(node); displays.append(display)
        if len(raw_results) > api.PROBE_LIMIT:
            notes.append('来源结果超过本次节点上限，只显示有界片段；覆盖未确认。')
    covered = [country for country in countries if any(node['country'] == country for node in probes)]
    missing = [country for country in countries if country not in covered]
    unexpected = [node for node in probes if node['country'] not in countries]
    duplicated = any(sum(node['country'] == country for node in probes) > 1 for country in countries)
    count_consistent = reported_count is not None and reported_count == len(probes) == len(countries)
    complete_coverage = len(covered) == len(countries) and not unexpected and not duplicated and count_consistent
    all_usable = bool(probes) and all(node['usable'] for node in probes)
    good = terminal and latest.get('status') == 'finished' and complete_coverage and all_usable and all(node['reachable'] is not False for node in probes) and not notes
    if not terminal and not problem:
        problem = '本次等待窗口已结束，服务端测量可能仍在运行；可继续读取同一测量。'
    if missing:
        notes.append('未覆盖地区：' + '、'.join(allowed[country] for country in missing))
    if duplicated or unexpected:
        notes.append('实际探针地区与请求不完全一致，不将重复节点当作多个地区。')
    if reported_count is None:
        notes.append('来源未返回有效探针数量，仅保留实际看到的节点结果。')
    elif not count_consistent:
        notes.append(f'请求 {len(countries)} 个节点，来源报告 {reported_count} 个，实际看到 {len(probes)} 个；覆盖不足或不一致。')
    if problem:
        notes.append(problem)
    rows = [api.row('目标', host), api.row('地区', '、'.join(allowed[country] for country in countries)),
            api.row('测量 ID', identifier, '结果由 Globalping 保存，可能公开检索；继续读取只发 GET，不创建新测量。'),
            api.row('节点覆盖', f'{len(covered)}/{len(countries)} 个地区', f'来源节点数 {reported_count if reported_count is not None else "未知"} · 已见 {len(probes)} 个')]
    rows += displays or [api.row('探针结果', '尚未取得', problem)]
    if quota:
        rows.append(api.row('来源限额', ' · '.join(f'{key}={value:g}' for key, value in quota.items()), '来自本次来源响应，不代表通用免费额度。'))
    if cancelled:
        status, summary = 'cancelled', '已取消读取；测量 ID 和已有节点结果已保留。'
    elif invalid_reply:
        status, summary = 'error', '来源回复未通过本次测量身份或格式校验；未采信不一致结果。'
    elif good:
        status, summary = 'ok', '测量已结束，已取得所选地区的节点结果。'
    elif terminal:
        status, summary = 'partial', '测量已结束；部分节点失败、指标缺失或地区覆盖不足。'
    else:
        status, summary = 'partial', '测量尚未确认结束；已保留测量 ID 和部分结果，可继续读取。'
    notes = list(dict.fromkeys(notes))
    notes += ['节点覆盖由社区探针决定；HTTP 拒绝、ICMP 丢包或个别节点失败不是全网封禁证据。',
              '这是跨地区延迟和可达性测量，不是全球下载带宽测速。']
    report = api.result(kind, status, summary, rows, ('globalping',), notes)
    report['measurement'] = {'id': identifier, 'target': host, 'input_target': api.text(params.get('target'),253), 'type': measure_type, 'regions': countries,
                             'requested_probes': len(countries), 'probes_count': reported_count,
                             'observed_probes': len(probes), 'covered_regions': covered, 'missing_regions': missing,
                             'status': latest.get('status') if latest else 'unconfirmed', 'created_at': api.text(latest.get('createdAt'),80) if latest else '', 'complete': terminal,
                             'resume_allowed': not terminal, 'poll_count': polls, 'resumed': resumed,
                             'retry_after_seconds': retry_after, 'quota': quota, 'probes': probes}
    return report


def speed_test(params, client, *, api):
    started = client.clock()
    download_bytes = params.get('download_bytes', 1024 * 1024)
    response = client.request('https://speed.cloudflare.com/__down?bytes=' + str(download_bytes),
                              deadline=started + 12, max_bytes=download_bytes, count_only=True)
    elapsed = api.finite(response.get('elapsed'), minimum=.000001, maximum=12)
    size = response.get('bytes_received')
    if response['status'] != 200 or type(size) is not int or size != download_bytes or elapsed is None:
        raise api.TestError('轻量下载未完整完成，未生成速度数值。')
    if client.clock() - started > 12.2:
        raise api.TestError('轻量下载已超时，未生成速度数值。')
    timings = response.get('timings') if isinstance(response.get('timings'), dict) else {}
    transfer = api.finite(timings.get('transfer'), minimum=.000001, maximum=elapsed)
    first_byte = api.finite(timings.get('first_byte'), maximum=elapsed)
    if transfer is not None and first_byte is not None and abs(transfer + first_byte - elapsed) > .02:
        transfer = None
    effective_mbps = size * 8 / elapsed / 1_000_000
    transfer_mbps = size * 8 / transfer / 1_000_000 if transfer is not None else None
    short = transfer is None or transfer < .3 or size < 3 * 1024 * 1024
    rows = [api.row('下载速度', f'{effective_mbps:.2f} Mbps', '端到端有效速率，包含 DNS、代理、连接及首字节等待，不是线路峰值带宽。'),
            api.row('下载数据', f'{download_bytes // (1024 * 1024)} MiB', f'完整收到 {size:,} 字节'),
            api.row('请求耗时', f'{elapsed:.3f} 秒', '系统传输的完整请求计时')]
    if transfer_mbps is not None:
        rows.append(api.row('正文传输速率', f'{transfer_mbps:.2f} Mbps', f'正文传输 {transfer:.3f} 秒；短样本会放大瞬时波动。'))
    else:
        rows.append(api.row('正文传输速率', '未单独确认', '来源没有返回有效的正文分段计时，仅报告完整请求的有效速率。'))
    for key, title in (('dns', 'DNS'), ('tcp', 'TCP 建连'), ('tls', 'TLS 握手'), ('first_byte', '首字节')):
        value = api.finite(timings.get(key), maximum=elapsed)
        if value is not None:
            rows.append(api.row(title, f'{value*1000:.1f} ms', '首字节为从请求开始的累计时间。' if key == 'first_byte' else '当前请求路径的分段耗时。'))
    notes = [f'仅下载、不上传。本次上限 {download_bytes // (1024 * 1024)} MiB，12 秒超时；结果只代表本机出口到 Cloudflare 的单连接路径。',
             'Cloudflare 会观察到请求的公网出口 IP；这不是全球带宽测速。']
    if short:
        notes.append('本次为短样本；建议在空闲网络手动选择 3 或 5 MiB 复测，单次结果不推断宽带峰值。')
    report = api.result('speed', 'ok', '轻量下载已完整完成；短样本结果仅供参考。' if short else '轻量下载已完整完成，已区分请求与正文传输耗时。',
                        rows, ('cloudflare',), notes)
    report['metrics'] = {'bytes_received': size, 'limit_bytes': download_bytes, 'elapsed_seconds': elapsed,
                         'transfer_seconds': transfer, 'effective_mbps': effective_mbps,
                         'transfer_mbps': transfer_mbps, 'sample_quality': 'short' if short else 'limited',
                         'scope': 'local_to_cloudflare_single_connection'}
    return report
