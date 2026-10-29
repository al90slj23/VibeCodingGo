import base64
import gzip
import io
import json
import subprocess
import socketserver
import threading
import os
import sys
import time
import unittest
from unittest.mock import patch

from vibecue import network_tests as network

IP = '8.8.8.8'
OTHER_IP = '1.1.1.1'
MIB = 1024 * 1024


class FakeClient:
    def __init__(self, json_values=(), response_values=()):
        self.json_values = list(json_values)
        self.response_values = list(response_values)
        self.calls = []
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def json(self, url, **kwargs):
        self.calls.append(('json', url, kwargs))
        if not self.json_values:
            raise network.TestError('Fixture: source unavailable')
        value = self.json_values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def request(self, url, **kwargs):
        self.calls.append(('request', url, kwargs))
        if not self.response_values:
            raise network.TestError('Fixture: source unavailable')
        value = self.response_values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def response(value=None, status=200, etag=''):
    if isinstance(value, dict) and 'results' in value:
        value = dict({'id':'measurement-1','target':'example.com','type':'ping'}, **value)
    return {'status': status, 'body': json.dumps(value).encode() if value is not None else b'', 'etag': etag}


def risk(flags=None, status='ok'):
    marks = {name: False for name in ('proxy','vpn','tor','hosting','compromised','anonymous')}
    marks['risk'] = 0
    if flags:
        marks.update(flags)
    return {'status': status, IP: {'detections': marks}}


def values(result):
    return {row['label']: row['value'] for row in result['rows']}


def probe(country='US', *, http=False):
    answer = {'statusCode': 403, 'timings': {'total': 40, 'firstByte': 25},
              'headers': {'Set-Cookie':'PRIVATE_SECRET', 'statusCode':200},
              'rawOutput':'PRIVATE_RAW'} if http else {'stats': {'avg': 12.5, 'loss': 0}, 'rawOutput':'PRIVATE_RAW'}
    return {'probe': {'location': {'country': country, 'city': 'Test city'}}, 'result': answer}


class InputAndReputationTests(unittest.TestCase):
    def test_idna_and_public_ipv6_targets_are_canonical(self):
        self.assertEqual(network.target_host(' EXAMPLE.COM. '), 'example.com')
        self.assertEqual(network.target_host('例子.中国'), 'xn--fsqu00a.xn--fiqs8s')
        self.assertEqual(network.target_host('2606:4700:4700::1111'), '2606:4700:4700::1111')

    def test_invalid_or_private_targets_never_reach_external_transport(self):
        bad = ['https://example.com/path', 'user:secret@example.com', '--help', 'example.com:443',
               '127.0.0.1', '192.168.0.1', '169.254.169.254', '::1', 'fe80::1%en0',
               'localhost', 'app.local', 'foo.internal', 'example.com\nInjected: secret',
               '999.999.999.999', '1.2.3.4/24', 'foo\\bar.com']
        for target in bad:
            with self.subTest(target=target):
                client = FakeClient()
                answer = network.run({'kind':'global_ping','target':target}, client)
                self.assertEqual(answer['status'], 'error')
                self.assertEqual(client.calls, [])

    def test_malformed_ui_parameters_produce_controlled_results(self):
        for params in (None, [], {}, {'kind':[]}, {'kind':{}}, {'kind':'unknown'},
                       {'kind':'speed','probe_limit':True}, {'kind':'speed','download_bytes':999999999},
                       {'kind':'domain','target':{}}, {'kind':'speed','secret':'PRIVATE_SECRET'}):
            with self.subTest(params=params):
                client = FakeClient()
                answer = network.run(params, client)
                self.assertEqual(answer['status'], 'error')
                self.assertEqual(client.calls, [])
                self.assertNotIn('PRIVATE_SECRET', json.dumps(answer))

    def test_reputation_reports_source_risk_without_inventing_a_clean_score(self):
        client = FakeClient([{'ip':IP,'asn':'AS15169 Google','country':'United States'}, risk({'vpn':True,'risk':75})])
        answer = network.run({'kind':'ip_reputation','target':IP}, client)
        self.assertEqual(answer['status'], 'ok')
        self.assertEqual(values(answer)['VPN'], '有标记')
        self.assertEqual(values(answer)['proxycheck.io 风险分'], '75 / 100')
        self.assertIn('不等于绝对纯净', ' '.join(answer['notes']))
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[1][1].endswith('/v3/' + IP + '?tag=0'))

    def test_missing_risk_fields_and_numeric_false_are_unknown(self):
        client = FakeClient([{'asn':'AS1','country':'Test'}, {'status':'ok',IP:{'detections':{'vpn':0,'risk':0}}}])
        answer = network.run({'kind':'ip_reputation','target':IP}, client)
        self.assertEqual(answer['status'], 'partial')
        self.assertEqual(values(answer)['VPN'], '未确认')
        self.assertEqual(values(answer)['代理'], '未确认')
        self.assertEqual(answer['summary'], '风险信号未确认。')

    def test_provider_rejection_and_mismatched_ownership_do_not_turn_into_clean(self):
        for denied in ({'status':'denied','message':'PRIVATE_SECRET'}, network.RateLimited('来源已达限额')):
            client = FakeClient([{'ip':OTHER_IP,'asn':'AS1','country':'Test'}, denied])
            answer = network.run({'kind':'ip_reputation','target':IP}, client)
            self.assertEqual(answer['status'], 'partial')
            self.assertEqual(values(answer)['网络归属'], '未确认')
            self.assertEqual(values(answer)['风险数据'], '未确认')
            self.assertNotIn('PRIVATE_SECRET', json.dumps(answer))

    def test_empty_target_discovers_ip_only_after_explicit_run(self):
        client = FakeClient([{'ip':IP}, {'ip':IP,'asn':'AS1','country':'Test'}, risk()])
        self.assertEqual(client.calls, [])
        answer = network.run({'kind':'ip_reputation','target':''}, client)
        self.assertEqual(answer['status'], 'ok')
        self.assertEqual(client.calls[0][1], network.IPIFY)


class LocalNetworkTests(unittest.TestCase):
    def test_http_denials_are_reachable_not_an_invented_global_block(self):
        for status in (301,403,404,429,500):
            with self.subTest(status=status), patch.object(network, 'resolve', return_value=[IP]):
                client = FakeClient(response_values=[response(status=status)])
                answer = network.run({'kind':'domain','target':'example.com'}, client)
                self.assertEqual(answer['status'], 'ok')
                self.assertEqual(values(answer)['HTTP 状态'], str(status))
                self.assertIn('不能代表', answer['summary'])
                self.assertEqual(client.calls[0][2]['method'], 'HEAD')
                self.assertFalse(client.calls[0][2]['rate_limit'])

    def test_dns_failure_or_private_answer_does_not_send_http(self):
        for value in (network.TestError('PRIVATE_SECRET'), ['127.0.0.1']):
            with self.subTest(value=value):
                setup = {'side_effect':value} if isinstance(value,Exception) else {'return_value':value}
                with patch.object(network,'resolve',**setup):
                    client = FakeClient()
                    answer = network.run({'kind':'domain','target':'example.com'}, client)
                    self.assertEqual(answer['status'], 'partial')
                    self.assertEqual(client.calls, [])
                    self.assertNotIn('PRIVATE_SECRET', json.dumps(answer))

    def test_dns_child_uses_argument_array_and_a_deadline(self):
        seen = []
        def runner(args, **kwargs):
            seen.append((args,kwargs))
            return subprocess.CompletedProcess(args,0,stdout=json.dumps([IP]).encode())
        self.assertEqual(network.resolve('example.com',runner), [IP])
        self.assertEqual(seen[0][0][-1], 'example.com')
        self.assertEqual(seen[0][1]['timeout'],5)
        self.assertNotIn('shell',seen[0][1])

    def test_proxy_scan_only_returns_flags_and_dns_addresses(self):
        proxy = b'<dictionary> {\n HTTPEnable : 1\n HTTPProxy : PRIVATE_SECRET\n ProxyAutoConfigURLString : https://user:PRIVATE_SECRET@proxy.test\n}'
        dns = b'nameserver[0] : 192.168.1.1\n nameserver[1] : 1.1.1.1\n secret : PRIVATE_SECRET\n'
        def runner(args, **kwargs):
            return subprocess.CompletedProcess(args,0,stdout=proxy if args[-1]=='--proxy' else dns)
        with patch.dict(network.os.environ, {'HTTPS_PROXY':'https://user:PRIVATE_SECRET@proxy.test'},clear=True):
            answer = network.system_network(runner)
        rendered = json.dumps(answer,ensure_ascii=False)
        self.assertNotIn('PRIVATE_SECRET',rendered)
        self.assertNotIn('proxy.test',rendered)
        self.assertIn('192.168.1.1',rendered)
        self.assertIn('HTTP',rendered)

    def test_two_different_public_routes_are_not_claimed_as_home_address_leak(self):
        client = FakeClient([{'ip':IP}], [{'status':200,'body':b'ip=1.1.1.1\nloc=US\nsecret=PRIVATE_SECRET'}])
        with patch.object(network,'system_network',return_value=[]):
            answer = network.run({'kind':'ip_trace'},client)
        self.assertEqual(answer['status'],'partial')
        self.assertIn('不是“真实地址泄露”的单独证据',answer['summary'])
        self.assertNotIn('PRIVATE_SECRET',json.dumps(answer))
        self.assertTrue(any('不代表 Chrome' in note for note in answer['notes']))


class GlobalAndSpeedTests(unittest.TestCase):
    def test_global_ping_posts_once_then_polls_with_etag_at_one_second_intervals(self):
        client = FakeClient([{'id':'measurement-1'}], [
            response({'id':'measurement-1','status':'in-progress','results':[probe()]},etag='"first"'),
            response(status=304), response({'id':'measurement-1','status':'finished','results':[probe() for _ in range(3)]},etag='"last"')])
        answer = network.run({'kind':'global_ping','target':'example.com','probe_limit':3,'download_bytes':MIB},client)
        self.assertEqual(answer['status'],'ok')
        self.assertEqual(len([call for call in client.calls if call[0]=='json']),1)
        submitted = client.calls[0][2]['json_body']
        self.assertEqual(submitted['limit'],3)
        self.assertEqual(len(submitted['locations']),3)
        self.assertEqual(submitted['measurementOptions']['packets'],3)
        self.assertEqual(client.sleeps,[1,1,1])
        self.assertEqual(client.calls[2][2]['etag'],'"first"')
        self.assertNotIn('PRIVATE_RAW',json.dumps(answer))

    def test_global_http_uses_official_status_field_and_omits_sensitive_headers(self):
        client = FakeClient([{'id':'measurement-1'}], [response({'type':'http','status':'finished','results':[probe(http=True) for _ in range(3)]})])
        answer = network.run({'kind':'global_http','target':'example.com','probe_limit':3},client)
        self.assertEqual(answer['status'],'ok')
        self.assertEqual(client.calls[0][2]['json_body']['measurementOptions'],{'request':{'method':'HEAD'},'protocol':'HTTPS'})
        self.assertTrue(all(value['value']=='HTTP 403' for value in answer['rows'][3:]))
        self.assertNotIn('PRIVATE_',json.dumps(answer))
        self.assertNotIn('Set-Cookie',json.dumps(answer))

    def test_measurement_identifier_and_target_validation_cannot_change_the_api_host(self):
        for identifier in ('https://evil.test/path','../escape','a?secret=PRIVATE_SECRET','a/b',None):
            with self.subTest(identifier=identifier):
                client = FakeClient([{'id':identifier}])
                answer = network.run({'kind':'global_ping','target':'example.com'},client)
                self.assertEqual(answer['status'],'error')
                self.assertEqual(len(client.calls),1)
                self.assertNotIn('evil',json.dumps(answer))

    def test_lost_submission_or_poll_failure_never_replays_the_post(self):
        for submitted,poll in [(network.TestError('lost response'),[]),({'id':'measurement-1'},[network.RateLimited('source limit')]),({'id':'measurement-1'},[response({'id':'other','status':'finished'})])]:
            with self.subTest(submitted=submitted):
                client=FakeClient([submitted],poll)
                answer=network.run({'kind':'global_ping','target':'example.com'},client)
                self.assertEqual(answer['status'],'error')
                self.assertEqual(len([call for call in client.calls if call[0]=='json']),1)

    def test_ui_parameter_choices_really_limit_probe_and_download_work(self):
        for limit in (3,5,6):
            client=FakeClient([{'id':'measurement-1'}],[response({'status':'finished','results':[probe() for _ in range(limit)]})])
            answer=network.run({'kind':'global_ping','target':'example.com','probe_limit':limit,'download_bytes':MIB},client)
            self.assertEqual(answer['status'],'ok')
            self.assertEqual(client.calls[0][2]['json_body']['limit'],limit)
        for size in (MIB,3*MIB,5*MIB):
            client=FakeClient(response_values=[{'status':200,'bytes_received':size,'elapsed':1.5}])
            answer=network.run({'kind':'speed','target':'','probe_limit':3,'download_bytes':size},client)
            self.assertEqual(answer['status'],'ok')
            self.assertTrue(client.calls[0][1].endswith('bytes='+str(size)))
            self.assertEqual(client.calls[0][2]['max_bytes'],size)
            self.assertEqual(values(answer)['下载数据'],str(size//MIB)+' MiB')

    def test_speed_requires_complete_body_and_finite_timing(self):
        for size,elapsed in ((MIB-1,1),(MIB,0),(MIB,float('nan'))):
            client=FakeClient(response_values=[{'status':200,'bytes_received':size,'elapsed':elapsed}])
            answer=network.run({'kind':'speed','download_bytes':MIB},client)
            self.assertEqual(answer['status'],'error')
            self.assertNotIn('下载速度',values(answer))

    def test_real_ui_default_payload_works_for_all_kinds(self):
        payload={'probe_limit':3,'download_bytes':MIB,'target':'example.com'}
        with patch.object(network,'resolve',return_value=[IP]), patch.object(network,'system_network',return_value=[]):
            for kind in network.TITLES:
                params=dict(payload,kind=kind)
                if kind=='ip_reputation':
                    params['target']=IP
                    client=FakeClient([{'asn':'AS1','country':'Test'},risk()])
                elif kind=='ip_trace':
                    client=FakeClient([{'ip':IP}],[{'status':200,'body':('ip='+IP).encode()}])
                elif kind in ('global_ping','global_http'):
                    client=FakeClient([{'id':'measurement-1'}],[response({'type':'http' if kind=='global_http' else 'ping','status':'finished','results':[probe(http=kind=='global_http') for _ in range(3)]})])
                elif kind=='domain':client=FakeClient(response_values=[response(status=403)])
                else:client=FakeClient(response_values=[{'status':200,'bytes_received':MIB,'elapsed':1}])
                with self.subTest(kind=kind):
                    self.assertIn(network.run(params,client)['status'],('ok','partial'))


class TransportBoundaryTests(unittest.TestCase):
    def test_worker_is_time_bounded_and_never_uses_a_shell(self):
        seen=[]
        def runner(args,**kwargs):
            seen.append((args,kwargs))
            return subprocess.CompletedProcess(args,0,stdout=json.dumps({'status':200,'body':base64.b64encode(b'{}').decode()}).encode())
        client=network.HTTPClient(clock=lambda:100,runner=runner,proxy_check=lambda url:None)
        self.assertEqual(client.json('https://api.ipify.org?format=json',deadline=102),{})
        self.assertLessEqual(seen[0][1]['timeout'],2)
        self.assertNotIn('shell',seen[0][1])
        self.assertEqual(seen[0][0][-1],'_fetch')

    def test_slow_disposable_child_is_terminated_within_the_deadline(self):
        def runner(args,**kwargs):
            return subprocess.run([sys.executable,'-c','import time;time.sleep(5)'],**kwargs)
        client=network.HTTPClient(runner=runner,proxy_check=lambda url:None)
        started=time.monotonic()
        with self.assertRaises(network.TestError):
            client.request('https://api.ipify.org?format=json',deadline=started+.2)
        self.assertLess(time.monotonic()-started,1.5)

    def test_worker_failure_does_not_echo_stderr_or_raw_server_text(self):
        def runner(args,**kwargs):
            return subprocess.CompletedProcess(args,1,stdout=b'PRIVATE_BODY',stderr=b'PRIVATE_SECRET')
        client=network.HTTPClient(runner=runner,proxy_check=lambda url:None)
        answer=network.run({'kind':'speed'},client)
        self.assertEqual(answer['status'],'error')
        self.assertNotIn('PRIVATE',json.dumps(answer))

    def test_gzip_expansion_bomb_is_rejected_before_json_decoding(self):
        compressed=gzip.compress(b'x'*50000)
        def runner(args,**kwargs):
            return subprocess.CompletedProcess(args,0,stdout=b'HTTP/2 200\r\nContent-Encoding: gzip\r\n\r\n'+compressed+b'\nVCGO_META:200:0.1')
        answer=network._curl_request({'url':'https://example.com/','method':'GET','max_bytes':1000,'timeout':1},
                                    {'source':'direct','proxy':''},runner=runner)
        self.assertEqual(answer,{'error':'oversized'})

    def test_redirects_do_not_visit_another_host(self):
        request=network.urllib.request.Request('https://example.com')
        self.assertIsNone(network.NoRedirect().redirect_request(request,None,302,'Moved',{},'https://private.test'))


class ProxyRouteTests(unittest.TestCase):
    def state(self, flags):
        output='<dictionary> {\n'+''.join('  '+key+' : '+str(value)+'\n' for key,value in flags.items())+'}\n'
        return subprocess.CompletedProcess([],0,stdout=output.encode())

    def test_https_proxy_takes_precedence_over_unused_socks_and_pac_backups(self):
        secret='http://user:PRIVATE_SECRET@127.0.0.1:8080'
        with patch.dict(network.os.environ,{},clear=True), patch.object(network.sys,'platform','darwin'):
            network.proxy_guard('https://api.ipify.org',proxies={'https':secret,'all':'socks5://127.0.0.1:7890'},
                                runner=lambda *a,**k: self.fail('unused system route must not be read'))

    def test_socks_and_all_proxy_are_supported_without_direct_fallback(self):
        for key in ('all','https'):
            route=network.proxy_guard('https://example.com',proxies={key:'socks5h://user:PRIVATE_SECRET@127.0.0.1:7890'})
            self.assertEqual(route['source'],'environment')
            self.assertTrue(route['proxy'].startswith('socks5h://'))
            self.assertNotIn('PRIVATE_SECRET',network.route_label(route))
        for value in ('file:///tmp/proxy','gopher://127.0.0.1:70','http://proxy.test:0','http://proxy.test/path'):
            with self.subTest(value=value), self.assertRaises(network.TestError):
                network.proxy_guard('https://example.com',proxies={'https':value})

    def test_no_proxy_matching_a_host_is_an_explicit_user_bypass(self):
        for host,exception in (('api.ipify.org','ipify.org'),('8.8.8.8','8.8.8.0/24')):
            route=network.proxy_guard('https://'+host,proxies={'all':'socks5h://127.0.0.1:7890','no':exception})
            self.assertEqual(route,{'proxy':'','source':'bypass'})

    def test_system_and_pac_policy_are_used_when_environment_has_no_proxy(self):
        expected={'proxy':'socks5h://127.0.0.1:1080','source':'pac'}
        with patch.dict(network.os.environ,{},clear=True), patch.object(network.sys,'platform','darwin'), patch.object(network,'_system_proxy',return_value=expected) as native:
            self.assertEqual(network.proxy_guard('https://example.com/'),expected)
            native.assert_called_once_with('https://example.com/')
        with patch.dict(network.os.environ,{},clear=True), patch.object(network.sys,'platform','darwin'), patch.object(network,'_system_proxy',side_effect=network.TestError('PAC 未完成')):
            with self.assertRaises(network.TestError):network.proxy_guard('https://example.com/')

    def test_failed_route_validation_prevents_dns_and_http_work(self):
        calls=[]
        def blocked(url):raise network.TestError('代理模式不受支持，未绕过执行')
        client=network.HTTPClient(proxy_check=blocked,runner=lambda *a,**k:calls.append(a))
        with patch.object(network,'resolve',side_effect=AssertionError('DNS must not run')):
            answer=network.run({'kind':'domain','target':'example.com'},client)
        self.assertEqual(answer['status'],'error')
        self.assertEqual(calls,[])
        answer=network.run({'kind':'ip_reputation','target':''},client)
        self.assertEqual(answer['status'],'error')
        self.assertEqual(calls,[])

    def test_global_reply_must_match_id_target_and_measurement_type(self):
        baseline={'id':'measurement-1','target':'example.com','type':'ping','status':'finished','results':[probe()]*3}
        for field,value in (('id','wrong'),('target','wrong.example'),('type','http')):
            with self.subTest(field=field):
                changed=dict(baseline);changed[field]=value
                client=FakeClient([{'id':'measurement-1'}],[response(changed)])
                answer=network.run({'kind':'global_ping','target':'example.com'},client)
                self.assertEqual(answer['status'],'error')
                self.assertEqual(len([call for call in client.calls if call[0]=='json']),1)

    def test_speed_uses_identity_encoding_and_rejects_compressed_wire_results(self):
        captured=[]
        def runner(args,**kwargs):
            captured.append(kwargs['input'])
            return subprocess.CompletedProcess(args,0,stdout=b'HTTP/2 200\r\nContent-Encoding: gzip\r\n\r\n'+gzip.compress(b'x'*100)+b'\nVCGO_META:200:0.1')
        answer=network._curl_request({'url':'https://example.com/','method':'GET','max_bytes':1000,'timeout':1,'count_only':True},
                                    {'source':'direct','proxy':''},runner=runner)
        self.assertIn(b'Accept-Encoding: identity',captured[0])
        self.assertEqual(answer,{'error':'encoded_speed_response'})

    def test_curl_config_keeps_proxy_credentials_and_post_body_out_of_argv(self):
        calls=[]
        def runner(args,**kwargs):
            calls.append((args,kwargs))
            return subprocess.CompletedProcess(args,0,stdout=b'HTTP/2 202\r\nETag: "safe"\r\n\r\n{}\nVCGO_META:202:0.125')
        route={'source':'environment','proxy':'socks5h://user:PRIVATE_SECRET@127.0.0.1:1080'}
        answer=network._curl_request({'url':'https://example.com/','method':'POST','json_body':{'test':'POST_PRIVATE'},'max_bytes':1000,'timeout':2},route,runner=runner)
        args,kwargs=calls[0];config=kwargs['input'].decode()
        self.assertEqual(args,['/usr/bin/curl','--disable','--config','-'])
        self.assertNotIn('PRIVATE',str(args));self.assertNotIn('PRIVATE',str(kwargs['env']))
        self.assertIn('PRIVATE_SECRET',config);self.assertIn('POST_PRIVATE',config)
        self.assertIn('retry = 0',config);self.assertNotIn('insecure',config);self.assertNotIn('location',config)
        self.assertEqual(answer['status'],202);self.assertEqual(answer['elapsed'],.125)
        self.assertNotIn('PRIVATE',json.dumps(answer))

    def test_transport_errors_are_specific_without_exposing_stderr(self):
        for code,expected in ((5,'proxy_dns'),(7,'proxy_connect'),(28,'timeout'),(35,'tls'),(60,'certificate'),(63,'oversized')):
            with self.subTest(code=code):
                answer=network._curl_request({'url':'https://example.com/','method':'GET','max_bytes':1000,'timeout':1},
                    {'source':'environment','proxy':'http://127.0.0.1:8080'},runner=lambda *a,**k:subprocess.CompletedProcess(a,code,stdout=b'',stderr=b'PRIVATE_SECRET'))
                self.assertEqual(answer,{'error':expected})

    def test_streaming_capture_stops_oversized_output_before_accumulating_it(self):
        answer=network._bounded_capture([sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*10000000)'],
                    input=b'',timeout=2,env=os.environ.copy(),limit=2048)
        self.assertEqual(answer.returncode,-1000)
        self.assertLessEqual(len(answer.stdout),2048)

    def test_local_http_connect_proxy_is_actually_used_without_external_request(self):
        received=[]
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(2);data=b''
                while b'\r\n\r\n' not in data and len(data)<4096:data+=self.request.recv(1024)
                received.append(data)
                self.request.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        with socketserver.TCPServer(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                route={'source':'environment','proxy':'http://127.0.0.1:'+str(server.server_address[1])}
                answer=network._curl_request({'url':'https://example.com/','method':'HEAD','max_bytes':1000,'timeout':2},route)
            finally:server.shutdown();thread.join(timeout=2)
        self.assertEqual(len(received),1)
        self.assertTrue(received[0].startswith(b'CONNECT example.com:443 '))
        self.assertEqual(answer,{'error':'proxy_auth'})

    def test_local_socks5h_proxy_receives_hostname_for_remote_dns(self):
        received=[]
        class Handler(socketserver.BaseRequestHandler):
            def read(self,n):
                data=b''
                while len(data)<n:
                    part=self.request.recv(n-len(data))
                    if not part:raise OSError()
                    data+=part
                return data
            def handle(self):
                self.request.settimeout(2)
                greeting=self.read(2);self.read(greeting[1]);self.request.sendall(b'\x05\x00')
                request=self.read(4)
                if request[3]!=3:return
                host=self.read(self.read(1)[0]);self.read(2);received.append(host)
                self.request.sendall(b'\x05\x02\x00\x01\x00\x00\x00\x00\x00\x00')
        with socketserver.TCPServer(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                route={'source':'environment','proxy':'socks5h://127.0.0.1:'+str(server.server_address[1])}
                answer=network._curl_request({'url':'https://example.com/','method':'GET','max_bytes':1000,'timeout':2},route)
            finally:server.shutdown();thread.join(timeout=2)
        self.assertEqual(received,[b'example.com'])
        self.assertEqual(answer,{'error':'proxy_connect'})

    def test_proxy_remote_dns_continues_https_when_local_dns_fails(self):
        client=FakeClient(response_values=[response(status=200)])
        client.ensure_route=lambda url:{'proxy':'socks5h://127.0.0.1:1080','source':'environment'}
        with patch.object(network,'resolve',side_effect=network.TestError('本机 DNS 未确认')):
            answer=network.run({'kind':'domain','target':'example.com'},client)
        self.assertEqual(answer['status'],'partial')
        self.assertEqual(values(answer)['HTTP 状态'],'200')
        self.assertIn('代理路径',answer['summary'])

    def test_proxy_remote_dns_uses_original_fqdn_even_when_local_dns_is_private(self):
        for address in ('198.18.0.1','127.0.0.1','192.168.1.1','fd00::1'):
            client=FakeClient(response_values=[response(status=200)])
            client.ensure_route=lambda url:{'proxy':'socks5h://127.0.0.1:1080','source':'environment'}
            with self.subTest(address=address),patch.object(network,'resolve',return_value=[address]):
                answer=network.run({'kind':'domain','target':'example.com'},client)
                self.assertEqual(answer['status'],'ok')
                self.assertEqual(client.calls[0][1],'https://example.com/')
                self.assertEqual(values(answer)['HTTP 状态'],'200')
                self.assertIn('原域名',values(answer)['代理 DNS 路径'])
        for target in ('127.0.0.1','192.168.1.1','::1','localhost','router.local'):
            client=FakeClient();client.ensure_route=lambda url:{'proxy':'socks5h://127.0.0.1:1080','source':'environment'}
            answer=network.run({'kind':'domain','target':target},client)
            self.assertEqual(answer['status'],'error');self.assertEqual(client.calls,[])

    def test_head_response_can_declare_large_or_encoded_body_without_downloading_it(self):
        captured=[]
        def runner(args,**kwargs):
            captured.append(kwargs['input'].decode())
            return subprocess.CompletedProcess(args,0,stdout=b'HTTP/2 200\r\nContent-Length: 9999999\r\nContent-Encoding: gzip\r\n\r\n\nVCGO_META:200:0.1')
        answer=network._curl_request({'url':'https://example.com/','method':'HEAD','max_bytes':1000,'timeout':1},
                                    {'source':'direct','proxy':''},runner=runner)
        self.assertEqual(answer['status'],200);self.assertEqual(answer['bytes_received'],0)
        self.assertIn('head',captured[0]);self.assertNotIn('max-filesize',captured[0])

    def test_tun_fake_ip_works_without_explicit_proxy_but_non_tunnel_route_is_blocked(self):
        for tunnel in (True,False):
            client=FakeClient(response_values=[response(status=200)])
            client.ensure_route=lambda url:{'proxy':'','source':'system'}
            with self.subTest(tunnel=tunnel),patch.object(network,'resolve',return_value=['198.18.0.1']),patch.object(network,'tunnel_route',return_value=tunnel):
                answer=network.run({'kind':'domain','target':'example.com'},client)
                self.assertEqual(bool(client.calls),tunnel)
                if tunnel:self.assertIn('TUN DNS 路径',values(answer))
        self.assertIn('系统 / TUN',network.route_label({'proxy':'','source':'system'}))

    def test_tunnel_recognition_is_bounded_and_only_accepts_tunnel_interfaces(self):
        for interface,expected in (('utun4',True),('tun0',True),('en0',False),('lo0',False)):
            calls=[]
            def runner(args,**kwargs):
                calls.append((args,kwargs))
                return subprocess.CompletedProcess(args,0,stdout=(' interface: '+interface+'\n').encode())
            with self.subTest(interface=interface),patch.object(network.sys,'platform','darwin'):
                self.assertEqual(network.tunnel_route('198.18.0.1',runner),expected)
                self.assertEqual(calls[0][1]['timeout'],2)
                self.assertNotIn('shell',calls[0][1])

    @unittest.skipUnless(sys.platform=='darwin','CFNetwork native PAC fixture requires macOS')
    def test_native_pac_script_selects_socks_without_fetching_a_pac_or_external_url(self):
        source="""import ctypes,ctypes.util,json
from vibecue import network_tests as n
cf=ctypes.CDLL(ctypes.util.find_library('CoreFoundation'))
cf.CFStringCreateWithCString.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.c_uint32];cf.CFStringCreateWithCString.restype=ctypes.c_void_p
cf.CFRelease.argtypes=[ctypes.c_void_p]
script=cf.CFStringCreateWithCString(None,b'function FindProxyForURL(url,host) { return "SOCKS 127.0.0.1:1080; DIRECT"; }',0x08000100)
try:
 route=n._system_proxy('https://example.com/',script=script)
 print(json.dumps({'source':route['source'],'scheme':n.urllib.parse.urlsplit(route['proxy']).scheme}))
finally:cf.CFRelease(script)
"""
        done=subprocess.run([sys.executable,'-c',source],capture_output=True,timeout=7,check=False)
        self.assertEqual(done.returncode,0,done.stderr.decode(errors='replace')[-300:])
        self.assertEqual(json.loads(done.stdout),{'source':'pac','scheme':'socks5h'})



if __name__=='__main__':
    unittest.main()
