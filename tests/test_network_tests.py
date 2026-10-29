import base64
import gzip
import io
import json
import subprocess
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
        class FakeResponse:
            status=200
            headers={'Content-Encoding':'gzip'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return gzip.compress(b'x'*50000)
        class Opener:
            def open(self,*args,**kwargs):return FakeResponse()
        with patch.object(network,'proxy_guard'), patch.object(network.urllib.request,'build_opener',return_value=Opener()):
            result=network._http_worker({'url':'https://api.ipify.org','max_bytes':1000,'timeout':1})
        self.assertEqual(result,{'error':'oversized'})

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

    def test_all_proxy_without_a_supported_https_route_cannot_fall_back_to_direct(self):
        with patch.dict(network.os.environ,{},clear=True):
            for proxies in ({'all':'socks5://user:PRIVATE_SECRET@127.0.0.1:7890'},
                            {'https':'socks5://user:PRIVATE_SECRET@127.0.0.1:7890'}):
                with self.subTest(proxies=list(proxies)):
                    with self.assertRaises(network.TestError) as failure:
                        network.proxy_guard('https://api.ipify.org',proxies=proxies)
                    self.assertNotIn('PRIVATE_SECRET',str(failure.exception))

    def test_no_proxy_matching_a_host_is_an_explicit_user_bypass(self):
        with patch.dict(network.os.environ,{'NO_PROXY':'api.ipify.org'},clear=True):
            network.proxy_guard('https://api.ipify.org',proxies={'all':'socks5://127.0.0.1:7890'})

    def test_pac_wpad_and_socks_only_modes_are_not_silently_ignored(self):
        with patch.dict(network.os.environ,{},clear=True), patch.object(network.sys,'platform','darwin'):
            for flag in ('ProxyAutoConfigEnable','ProxyAutoDiscoveryEnable','SOCKSEnable','HTTPSEnable'):
                with self.subTest(flag=flag), self.assertRaises(network.TestError):
                    network.proxy_guard('https://api.ipify.org',proxies={},runner=lambda *a,**k:self.state({flag:1}))
            network.proxy_guard('https://api.ipify.org',proxies={},runner=lambda *a,**k:self.state({}))

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
        class FakeResponse:
            status=200
            headers={'Content-Encoding':'gzip'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,n):return gzip.compress(b'x'*100)
        class Opener:
            def open(self,request,**kwargs):
                captured.append(request.get_header('Accept-encoding'))
                return FakeResponse()
        with patch.object(network,'proxy_guard'), patch.object(network.urllib.request,'build_opener',return_value=Opener()):
            answer=network._http_worker({'url':'https://speed.cloudflare.com/__down?bytes=100',
                                         'max_bytes':1000,'timeout':1,'count_only':True})
        self.assertEqual(captured,['identity'])
        self.assertEqual(answer,{'error':'encoded_speed_response'})


if __name__=='__main__':
    unittest.main()
