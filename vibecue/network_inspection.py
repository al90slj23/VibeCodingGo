"""Evidence-scoped IP, DNS, HTTPS and egress observations.

No work at import. The caller supplies the shared finite transport and its API
namespace, so CLI execution does not create a second network_tests module.
"""
from __future__ import annotations

import ipaddress
import math
import re
import urllib.parse

CF_DOH = 'https://cloudflare-dns.com/dns-query'
GOOGLE_DOH = 'https://dns.google/resolve'
IPV6_EGRESS = 'https://api6.ipify.org?format=json'
TOR_EXITS = 'https://check.torproject.org/torbulkexitlist'
DNS_NAMES = {0:'NOERROR',1:'FORMERR',2:'SERVFAIL',3:'NXDOMAIN',4:'NOTIMP',5:'REFUSED'}


def _row(api, label, value, detail='', status='info'):
    return dict(api.row(label, value, detail), status=status)


def _deadline(client, end, seconds):
    return min(end, client.clock()+seconds)


def _cancel(api, error):
    if isinstance(error, getattr(api, 'Cancelled', ())):
        raise error


def _reason(api, error):
    code = getattr(error, 'code', None)
    status = getattr(error, 'status_code', getattr(error, 'status', None))
    retry = getattr(error, 'retry_after_seconds', None)
    if isinstance(error, api.RateLimited) or status == 429:
        value='来源限流，未自动重复查询。'
        if type(retry) in (int,float) and math.isfinite(retry) and 0 < retry <= 86400:
            value += ' 建议等待 '+str(math.ceil(retry))+' 秒。'
        return 'rate_limited', value
    if status in (401,403):
        return 'unavailable', '来源拒绝匿名访问或当前出口；未改路由，也未把拒绝当作无风险。'
    if type(status) is int and 100<=status<=599:
        return 'unavailable', '来源返回 HTTP '+str(status)+'；未取得有效数据，也未把拒绝当作检测通过。'
    reasons={'proxy_auth':'代理要求认证，请在原代理工具中核对。',
             'proxy_connect':'代理连接或握手失败；未回退直连。',
             'proxy_dns':'代理地址未解析成功；未回退直连。',
             'proxy_network':'代理链路未完成；未回退直连。',
             'timeout':'来源等待超时，已停止本次请求。',
             'certificate':'证书未通过当前系统信任校验。',
             'tls':'TLS 握手未完成。',
             'dns':'来源域名在当前路径未解析成功。',
             'oversized':'来源响应超过本次读取上限。'}
    return 'unavailable', reasons.get(code, '来源未返回可确认数据；没有使用猜测值。')


def _check(name, state, message, **values):
    return dict(provider=name,status=state,message=message,**values)


def _tor_fallback(address, client, end, api):
    response=client.request(TOR_EXITS,deadline=_deadline(client,end,4),max_bytes=524288)
    if response.get('status')!=200:
        raise api.TestError('Tor 出口列表暂不可读。')
    raw=response.get('body')
    if not isinstance(raw,bytes):
        raise api.TestError('Tor 出口列表格式无法识别。')
    try: lines=raw.decode('ascii').splitlines()
    except UnicodeError: raise api.TestError('Tor 出口列表格式无法识别。') from None
    if len(lines)>40000: raise api.TestError('Tor 出口列表超过上限。')
    exits=set();invalid=0
    for line in lines:
        value=line.strip()
        if not value or value.startswith('#'): continue
        try: exits.add(api.public_ip(value))
        except api.TestError: invalid+=1
    if not exits or invalid:
        raise api.TestError('Tor 出口列表没有可核对的完整记录。')
    return address in exits,len(exits)


def ip_reputation(params,client,*,api):
    end=client.clock()+28
    discovered=not params.get('target')
    checks=[];sources=['ipapi','proxycheck'];fallback=False
    if discovered:
        sources.insert(0,'ipify')
        try:
            address=api.discover_ip(client,_deadline(client,end,5))
            checks.append(_check('ipify','ok','已取得本应用路径的出口。'))
        except api.TestError as error:
            _cancel(api,error);state,reason=_reason(api,error)
            checks.append(_check('ipify',state,reason))
            sources.insert(1,'cloudflare_trace')
            try:
                trace=client.request(api.TRACE,deadline=_deadline(client,end,5),max_bytes=16384)
                if trace.get('status')!=200:raise api.TestError('出口来源未返回。')
                fields=dict(line.split('=',1) for line in trace['body'].decode('utf-8','replace').splitlines()[:128] if '=' in line)
                address=api.public_ip(fields.get('ip'));fallback=True
                checks.append(_check('cloudflare_trace','ok','由备用出口来源取得所查 IP。'))
            except api.TestError as alternate:
                _cancel(api,alternate)
                raise api.TestError('本次没有确认公网出口；ipify 和 Cloudflare 均未取得有效观测。',code='egress_unavailable') from None
    else:address=api.public_ip(params['target'])
    rows=[_row(api,'检测 IP',address)]
    if fallback:rows.append(_row(api,'出口观测来源','Cloudflare','ipify 暂不可用；信誉只针对该来源观察到的 IP，不代表所有分流路径。'))
    notes=['风险字段反映来源数据库；无标记不等于绝对纯净，也不是账号封禁概率。',
           '仅向归属与风险来源发送所查 IP，不发送项目、路径、对话或账号授权。',
           '匿名服务有独立限额；限流、拒绝和缺失字段会显示为未知。']
    partial=False
    try:
        network=client.json('https://api.ipapi.is/?q='+urllib.parse.quote(address,safe=''),deadline=_deadline(client,end,6))
        if not isinstance(network.get('ip'),str) or api.public_ip(network['ip'])!=address:
            raise api.TestError('来源 IP 不一致。')
        asn=network.get('asn')
        if isinstance(asn,dict):
            asn=' · '.join(str(x) for x in (asn.get('asn'),asn.get('org')) if type(x) in (str,int))
        country=network.get('country')
        if isinstance(network.get('location'),dict):country=network['location'].get('country',country)
        asn,country=api.text(asn),api.text(country)
        complete=bool(asn and country)
        partial=partial or not complete
        rows += [_row(api,'ASN / 网络',asn or '未确认',status='ok' if asn else 'unknown'),
                 _row(api,'地区（估计）',country or '未确认','仅网络归属估计，不是用户真实住址。',status='info' if country else 'unknown')]
        company=network.get('company')
        if isinstance(company,dict):company=company.get('name')
        if api.text(company):rows.append(_row(api,'归属机构',api.text(company)))
        checks.append(_check('ipapi','ok' if complete else 'partial','已读取网络归属。' if complete else '归属字段覆盖不足。'))
    except api.TestError as error:
        _cancel(api,error);partial=True
        state,reason=_reason(api,error);checks.append(_check('ipapi',state,reason))
        rows.append(_row(api,'网络归属','未确认',reason,'unknown'))
    known=0;flagged=[];risk_failed=False
    try:
        value=client.json('https://proxycheck.io/v3/'+urllib.parse.quote(address,safe='')+'?tag=0',deadline=_deadline(client,end,7))
        if value.get('status') not in ('ok','warning') or not isinstance(value.get(address),dict):
            raise api.TestError('风险来源未返回匹配记录。')
        detection=value[address].get('detections')
        if not isinstance(detection,dict):raise api.TestError('风险字段缺失。')
        for key,label in (('proxy','代理'),('vpn','VPN'),('tor','Tor 出口'),('hosting','数据中心'),('compromised','被滥用记录'),('anonymous','匿名网络')):
            mark=detection.get(key)
            if type(mark) is bool:
                known+=1
                if mark:flagged.append(label)
                rows.append(_row(api,label,'有标记' if mark else '未见标记','来源：proxycheck.io；不是本机行为审计。','warning' if mark else 'ok'))
            else:rows.append(_row(api,label,'未确认',status='unknown'))
        score=api.finite(detection.get('risk'),maximum=100)
        rows.append(_row(api,'proxycheck.io 风险分',f'{score:g} / 100' if score is not None else '未确认','来源原始分数，不换算成“纯净度”。','warning' if score is not None and score>=66 else 'info' if score is not None else 'unknown'))
        confidence=api.finite(detection.get('confidence'),maximum=100)
        if confidence is not None:rows.append(_row(api,'来源置信度',f'{confidence:g} / 100','来源自述置信度，不是封禁概率。'))
        for key,label in (('last_seen','来源最近观测'),('first_seen','来源首次观测')):
            if api.text(detection.get(key),80):rows.append(_row(api,label,api.text(detection[key],80)))
        incomplete=known<6 or score is None or value['status']=='warning'
        partial=partial or incomplete
        checks.append(_check('proxycheck','partial' if incomplete else 'ok','来源返回 warning 或字段覆盖不足。' if incomplete else '已读取目标风险记录。',known_flags=known,total_flags=6))
        if value['status']=='warning':notes.append('来源返回 warning；匿名限额、缓存或字段覆盖可能限制结果。')
        summary='来源标记：'+'、'.join(flagged) if flagged else '来源暂未报告已知风险标记；这不构成绝对纯净证明。' if known else '风险信号未确认。'
    except api.TestError as error:
        _cancel(api,error);partial=True;risk_failed=True
        state,reason=_reason(api,error);checks.append(_check('proxycheck',state,reason))
        rows.append(_row(api,'风险数据','未确认',reason,'unknown'))
        summary='风险信号未确认，已保留能够取得的网络归属信息。'
    if risk_failed and client.clock()+.2<end:
        sources.append('tor')
        try:
            matched,count=_tor_fallback(address,client,end,api)
            rows.append(_row(api,'Tor 官方出口列表','命中出口记录' if matched else '未命中当前列表',
                             '只覆盖 Tor 出口；未命中不排除 VPN、代理或其他风险。','warning' if matched else 'info'))
            checks.append(_check('tor','ok','仅列表查询；没有向该来源提交待查 IP。',entries=count,matched=matched))
            if matched:summary='命中 Tor 官方出口记录；其他风险信号仍未确认。'
        except api.TestError as error:
            _cancel(api,error);state,reason=_reason(api,error)
            checks.append(_check('tor',state,reason))
            rows.append(_row(api,'Tor 官方出口列表','未确认',reason,'unknown'))
    answer=api.result('ip_reputation','partial' if partial else 'ok',summary,rows,sources,notes)
    answer['checks']=checks
    answer['metrics']={'known_risk_flags':known,'total_risk_flags':6,'source_count':len(checks)}
    return answer


def _dns_answer(value,host,record_type,api):
    if not isinstance(value,dict) or type(value.get('Status')) is not int or not 0<=value['Status']<=15:
        raise api.TestError('公共 DNS 响应格式无法核对。')
    questions=value.get('Question')
    if not isinstance(questions,list) or not any(isinstance(q,dict) and isinstance(q.get('name'),str) and q['name'].rstrip('.').lower()==host and type(q.get('type')) is int and q.get('type')==record_type for q in questions[:4]):
        raise api.TestError('公共 DNS 响应目标不一致。')
    records=value.get('Answer',[])
    if not isinstance(records,list) or len(records)>128:raise api.TestError('公共 DNS 记录超过上限。')
    ips=[];aliases=[];ttls=[]
    allowed={host}
    for _ in range(8):
        before=len(allowed)
        for item in records[:64]:
            if (isinstance(item,dict) and item.get('type')==5 and isinstance(item.get('name'),str)
                    and item['name'].rstrip('.').lower() in allowed and isinstance(item.get('data'),str)
                    and re.fullmatch(r'[A-Za-z0-9_.-]{1,253}\.?',item['data'])):
                allowed.add(item['data'].rstrip('.').lower())
        if len(allowed)==before:break
    for item in records[:64]:
        if not isinstance(item,dict):raise api.TestError('公共 DNS 记录格式无效。')
        kind=item.get('type');data=item.get('data')
        if not isinstance(item.get('name'),str) or item['name'].rstrip('.').lower() not in allowed:continue
        if type(kind) is not int or not isinstance(data,str):continue
        if kind in (1,28):
            try:address=ipaddress.ip_address(data)
            except ValueError:raise api.TestError('公共 DNS 地址无效。') from None
            if address.version!=(4 if kind==1 else 6):raise api.TestError('公共 DNS 地址类型不一致。')
            if str(address) not in ips:ips.append(str(address))
        elif kind==5 and api.text(data,254):aliases.append(api.text(data,254).rstrip('.'))
        ttl=api.finite(item.get('TTL'),maximum=2147483647)
        if ttl is not None:ttls.append(ttl)
    return {'rcode':value['Status'],'rcode_name':DNS_NAMES.get(value['Status'],'RCODE '+str(value['Status'])),
            'addresses':ips[:32],'aliases':aliases[:8],'truncated':value.get('TC') is True or len(records)>64 or len(ips)>32 or len(aliases)>8,
            'authenticated':value.get('AD') if type(value.get('AD')) is bool else None,
            'ttl_min':min(ttls) if ttls else None}


def _public_dns(host,client,end,api):
    rows=[];checks=[];observed={}
    for provider,base,label in (('cf_doh',CF_DOH,'Cloudflare DNS'),('google_doh',GOOGLE_DOH,'Google DNS')):
        data=[];failure=None
        for kind,number in (('A',1),('AAAA',28)):
            url=base+'?'+urllib.parse.urlencode({'name':host,'type':kind,'cd':'false',**({'edns_client_subnet':'0.0.0.0/0'} if provider=='google_doh' else {})})
            try:
                value=client.json(url,deadline=_deadline(client,end,3.5),accept='application/dns-json')
                item=_dns_answer(value,host,number,api);data.append(item)
                if item['rcode']!=0:break
            except api.TestError as error:
                _cancel(api,error);failure=_reason(api,error)[1];break
        if data:
            names=list(dict.fromkeys(x['rcode_name'] for x in data))
            ips=list(dict.fromkeys(address for x in data for address in x['addresses']))
            aliases=list(dict.fromkeys(alias for x in data for alias in x['aliases']))
            valid=bool(ips) and len(data)==2 and all(x['rcode']==0 and not x['truncated'] for x in data)
            observed[provider]={'addresses':ips,'rcodes':[x['rcode'] for x in data],'complete':valid}
            detail='、'.join(ips) or '没有 A/AAAA 地址记录'
            if aliases:detail+=' · CNAME '+' → '.join(aliases)
            if any(x['truncated'] for x in data):detail+=' · 来源或显示范围受限，未标为完整'
            if failure:detail+=' · '+failure
            if any(x['authenticated'] is True for x in data):detail+=' · 解析器 AD=true（其 DNSSEC 验证信号）'
            rows.append(_row(api,label,' / '.join(names),detail,'ok' if valid else 'warning'))
            checks.append(_check(provider,'ok' if valid else 'partial','已读取公共 DNS 观测。',**observed[provider]))
        else:
            rows.append(_row(api,label,'未确认',failure or '本次时限内没有返回公共解析结果。','unknown'))
            checks.append(_check(provider,'unavailable',failure or '公共解析未完成。'))
    return rows,checks,observed


def domain_test(params,client,*,api):
    host=api.target_host(params.get('target'));end=client.clock()+30
    try:literal=ipaddress.ip_address(host)
    except ValueError:literal=None
    url='https://'+('['+host+']' if ':' in host else host)+'/'
    route=client.ensure_route(url) if hasattr(client,'ensure_route') else None
    remote_dns=isinstance(route,dict) and urllib.parse.urlsplit(route.get('proxy','')).scheme in ('http','https','socks4a','socks5h')
    addresses=[];dns_error=False
    try:addresses=api.resolve(host,deadline=end)
    except api.TestError as error:_cancel(api,error);dns_error=True
    rows=[_row(api,'目标',host),_row(api,'本机 DNS','未确认' if dns_error else 'IP 字面目标' if literal else '解析成功',status='unknown' if dns_error else 'ok')]
    checks=[_check('system_dns','unavailable' if dns_error else 'ok','系统解析器观测。',addresses=addresses)]
    if addresses:rows.append(_row(api,'解析地址','、'.join(addresses),'当前系统解析结果；不证明 DNS 未受干预。'))
    blocked=[address for address in addresses if not ipaddress.ip_address(address).is_global or ipaddress.ip_address(address).is_multicast]
    fake=ipaddress.ip_network('198.18.0.0/15')
    fake_answers=bool(blocked) and all(ipaddress.ip_address(address) in fake for address in blocked)
    tunnel=bool(fake_answers and not remote_dns and api.tunnel_route(blocked[0],deadline=end))
    allowed=(not dns_error or remote_dns) and (not blocked or remote_dns or tunnel)
    if remote_dns and (dns_error or blocked):rows.append(_row(api,'代理 DNS 路径','由代理解析原域名（公开域名）','本机 DNS 不作为代理请求的连接目标；未绕过当前代理。'))
    elif tunnel:rows.append(_row(api,'TUN DNS 路径','系统隧道接管 fake-IP','使用原始域名核对 HTTPS；不将 fake-IP 当作封禁证据。'))
    reachable=False;http_code=None
    if allowed:
        try:
            response=client.request(url,deadline=_deadline(client,end,8),method='HEAD',max_bytes=4096,rate_limit=False)
            http_code=response['status'];reachable=True
            rows += [_row(api,'HTTPS / TLS','已取得经过当前系统信任校验的 HTTPS 响应',status='ok'),
                     _row(api,'HTTP 状态',str(http_code),'403/429/5xx 是目标服务的响应，不等同于全网封禁。重定向不跟随。','warning' if http_code>=400 else 'ok')]
            timings=response.get('timings') or {}
            for name,label in (('dns','请求 DNS'),('tcp','连接阶段'),('tls','TLS 阶段'),('first_byte','首字节'),('total','请求总耗时')):
                seconds=api.finite(timings.get(name),maximum=120)
                if seconds is not None:rows.append(_row(api,label,f'{seconds*1000:.1f} ms','当前所选代理/隧道路径的计时，不冒充目标直连耗时。'))
            if response.get('redirect'):rows.append(_row(api,'重定向目标',api.text(response['redirect'],300),'仅显示脱敏来源，不继续访问。'))
            checks.append(_check('https','ok','已取得目标 HTTPS 响应。',http_status=http_code))
        except api.TestError as error:
            _cancel(api,error);_,reason=_reason(api,error)
            rows.append(_row(api,'HTTPS / TLS','未确认',reason,'unknown'))
            checks.append(_check('https','unavailable',reason))
    else:
        reason='本机 DNS 未解析成功。' if dns_error else '解析到非公网地址，未直接连接目标。'
        rows.append(_row(api,'HTTPS / TLS','未发起',reason,'unknown'));checks.append(_check('https','unavailable',reason))
    sources=[];public={}
    if not literal:
        dns_rows,dns_checks,public=_public_dns(host,client,end,api)
        rows+=dns_rows;checks+=dns_checks;sources=['cf_doh','google_doh']
        public_sets=[set(item['addresses']) for item in public.values() if item['complete']]
        if public_sets and addresses:
            local=set(addresses);overlap=any(local.intersection(other) for other in public_sets)
            rows.append(_row(api,'DNS 对照','与至少一个公共解析器存在相同地址' if overlap else '本机与公共解析地址不同',
                             'CDN、地理调度、缓存、代理 fake-IP 或分流可造成差异；差异本身不证明污染或封禁。','info'))
    coverage=literal is not None or len(public)==2 and all(x['complete'] for x in public.values())
    complete=reachable and not dns_error and coverage
    summary=((f'已通过代理路径到达 HTTPS 服务（HTTP {http_code}）；' if remote_dns else f'已到达 HTTPS 服务（HTTP {http_code}）；')+('本机 DNS 仍未确认；' if dns_error else '')+'不能代表所有地区、账号或浏览器均可访问。' if reachable else
             '本机 DNS 与 HTTPS 覆盖不完整；不能据此判定域名被全网封禁。')
    if blocked and not remote_dns and not tunnel:summary='域名解析到非公网地址；已停止目标连接并保留公共 DNS 对照。'
    notes=['此项是当前应用路径的观测；跨地区可达性请运行全球 HTTP。',
           '公共 DNS 查询会将域名发送给 Cloudflare 和 Google；不会发送项目或对话内容。',
           'NXDOMAIN、SERVFAIL、403、证书失败和网络超时分别保留；单项结果不能证明全网封禁。']
    answer=api.result('domain','ok' if complete else 'partial',summary,rows,sources,notes)
    answer['checks']=checks;answer['metrics']={'https_reachable':reachable,'http_status':http_code,'public_dns_complete':bool(coverage),'system_dns_confirmed':not dns_error}
    return answer


def ip_trace(params,client,*,api):
    end=client.clock()+25;rows=[];checks=[];observations=[]
    try:
        address=api.discover_ip(client,_deadline(client,end,6))
        observations.append(('ipify',address));rows.append(_row(api,'ipify 观察到的出口',address,'IPv'+str(ipaddress.ip_address(address).version),'ok'))
        checks.append(_check('ipify','ok','已取得本应用路径的出口。'))
    except api.TestError as error:
        _cancel(api,error);state,reason=_reason(api,error);rows.append(_row(api,'ipify 观察到的出口','未确认',reason,'unknown'));checks.append(_check('ipify',state,reason))
    try:
        response=client.request(api.TRACE,deadline=_deadline(client,end,6),max_bytes=16384)
        if response['status']!=200:raise api.TestError('Cloudflare 出口观测未返回。')
        fields=dict(line.split('=',1) for line in response['body'].decode('utf-8','replace').splitlines()[:128] if '=' in line)
        address=api.public_ip(fields.get('ip'));observations.append(('cloudflare',address))
        rows.append(_row(api,'Cloudflare 观察到的出口',address,'IPv'+str(ipaddress.ip_address(address).version),'ok'))
        if re.fullmatch('[A-Z]{2}',fields.get('loc','')):rows.append(_row(api,'Cloudflare 地区（估计）',fields['loc'],'网络出口估计，不是用户真实地址。'))
        if fields.get('warp') in ('on','off','plus'):rows.append(_row(api,'此请求的 WARP 信号',fields['warp'],'仅该来源的请求信号，不代表所有 VPN 或浏览器路径。'))
        checks.append(_check('cloudflare','ok','已取得本应用路径的出口。'))
    except api.TestError as error:
        _cancel(api,error);state,reason=_reason(api,error);rows.append(_row(api,'Cloudflare 观察到的出口','未确认',reason,'unknown'));checks.append(_check('cloudflare',state,reason))
    try:
        value=client.json(IPV6_EGRESS,deadline=_deadline(client,end,4))
        address=api.public_ip(value.get('ip'))
        if ipaddress.ip_address(address).version!=6:raise api.TestError('IPv6 来源未返回 IPv6 地址。')
        observations.append(('ipify_ipv6',address));rows.append(_row(api,'IPv6 出口观测',address,'api6.ipify.org，仅观察该请求路径。','ok'));checks.append(_check('ipify_ipv6','ok','已取得 IPv6 出口。'))
    except api.TestError as error:
        _cancel(api,error);rows.append(_row(api,'IPv6 出口观测','未确认','当前路径可能不提供 IPv6，或该来源暂不可达；未判为泄露。','unknown'));checks.append(_check('ipify_ipv6','unavailable','没有可确认的 IPv6 结果。'))
    rows.extend(dict(item,status='unknown' if item.get('value')=='未确认' else 'info') for item in api.system_network(deadline=end))
    families={4:[],6:[]}
    for _,address in observations:families[ipaddress.ip_address(address).version].append(address)
    comparable={family:values for family,values in families.items() if len(values)>=2}
    differs=any(len(set(values))>1 for values in comparable.values())
    matched=bool(comparable) and not differs
    if differs:summary='同一 IP 协议的来源观察到不同出口；可能是分流或代理负载均衡，不能单独证明真实地址泄露。'
    elif matched:summary='同一 IP 协议的来源观察到相同出口；仍不能据此排除浏览器 WebRTC 或 DNS 泄露。'
    elif observations:summary='已取得出口观测，但同一 IP 协议的来源不足以比较；IPv4 与 IPv6 不直接互判泄露。'
    else:summary='出口来源覆盖不完整，尚不能比较网络路径。'
    answer=api.result('ip_trace','ok' if matched else 'partial',summary,rows,('ipify','cloudflare_trace','webrtc','browserleaks','dnsleaktest'),
                      ('这里只检查本应用所选路径，不是 Chrome/Safari 的 WebRTC、DNS 或所有分流规则测试。',
                       'IP 归属地区不等于用户真实住址；私网 DNS 地址也不自动构成公网泄露。',
                       '浏览器检测需在实际使用的浏览器手动打开；本工具不会替你启动或公开浏览器测量。'))
    answer['checks']=checks
    answer['metrics']={'observed_ipv4':len(families[4]),'observed_ipv6':len(families[6]),'comparable_families':list(comparable),'same_family_disagreement':differs}
    return answer
