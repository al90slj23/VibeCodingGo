import json
import unittest
from unittest.mock import patch

from vibecue import network_tests as api
from vibecue import network_inspection as inspect

IP='8.8.8.8'
IP6='2606:4700:4700::1111'


class Client:
    def __init__(self,jsons=(),responses=(),route=None):
        self.jsons=list(jsons);self.responses=list(responses);self.calls=[];self.now=100
        self.route=route or {'source':'direct','proxy':''}
    def clock(self):return self.now
    def ensure_route(self,url):return self.route
    def json(self,url,**kwargs):
        self.calls.append(('json',url,kwargs))
        if not self.jsons:raise api.TestError('unavailable fixture')
        value=self.jsons.pop(0)
        if isinstance(value,Exception):raise value
        return value
    def request(self,url,**kwargs):
        self.calls.append(('request',url,kwargs))
        if not self.responses:raise api.TestError('unavailable fixture')
        value=self.responses.pop(0)
        if isinstance(value,Exception):raise value
        return value


def dns(kind=1,addresses=None,status=0,host='example.com',**extra):
    number=kind
    if addresses is None:addresses=[IP] if kind==1 else [IP6]
    return dict(Status=status,Question=[{'name':host+'.','type':number}],
                Answer=[{'name':host+'.','type':number,'TTL':300,'data':value} for value in addresses],**extra)


def doh():return [dns(),dns(28),dns(),dns(28)]


def risk(**extra):
    values={k:False for k in ('proxy','vpn','tor','hosting','compromised','anonymous')}
    values.update(risk=0,**extra)
    return {'status':'ok',IP:{'detections':values}}


def rows(value):return {r['label']:r['value'] for r in value['rows']}


class ReputationTests(unittest.TestCase):
    def test_anonymous_ownership_and_strict_boolean_risk_are_distinct(self):
        client=Client([{'ip':IP,'asn':'AS15169 Google','country':'United States','company':'Google'},risk(vpn=True,confidence=87)])
        value=inspect.ip_reputation({'target':IP},client,api=api)
        self.assertEqual(value['status'],'ok');self.assertEqual(rows(value)['VPN'],'有标记')
        self.assertEqual(rows(value)['来源置信度'],'87 / 100')
        self.assertEqual(value['metrics']['known_risk_flags'],6)
        self.assertEqual(len(client.calls),2);self.assertNotIn(api.SOURCES['ipify'],value['sources'])

    def test_current_exit_discovery_can_use_independent_observer_without_route_bypass(self):
        client=Client([api.TestError('ipify unavailable'),{'ip':IP,'asn':'AS1','country':'Test'},risk()],
                      [{'status':200,'body':('ip='+IP+'\n').encode()}])
        value=inspect.ip_reputation({},client,api=api)
        self.assertEqual(value['status'],'ok');self.assertEqual(rows(value)['检测 IP'],IP)
        self.assertEqual(rows(value)['出口观测来源'],'Cloudflare')
        self.assertEqual(client.calls[1][1],api.TRACE)

    def test_no_exit_observation_never_guesses_a_target_for_reputation(self):
        client=Client([api.TestError('unavailable')],[api.TestError('unavailable')])
        with self.assertRaises(api.TestError):
            inspect.ip_reputation({},client,api=api)
        self.assertEqual(len(client.calls),2)

    def test_missing_flags_never_become_negative_and_no_confidence_is_invented(self):
        client=Client([{'ip':IP,'asn':'AS1','country':'Test'},{'status':'warning',IP:{'detections':{'vpn':0,'risk':0}}}])
        value=inspect.ip_reputation({'target':IP},client,api=api)
        self.assertEqual(value['status'],'partial');self.assertEqual(rows(value)['VPN'],'未确认')
        self.assertNotIn('来源置信度',rows(value))

    def test_rate_limit_keeps_cause_and_tor_fallback_is_only_scope_it_proves(self):
        error=api.RateLimited('PRIVATE_SECRET');error.retry_after_seconds=45
        client=Client([{'ip':IP,'asn':'AS1','country':'Test'},error],[{'status':200,'body':(IP+'\n1.1.1.1\n').encode()}])
        with patch.dict(api.SOURCES,{'tor':{'name':'Tor official','url':'https://blog.torproject.org/changes-tor-exit-list-service/'}}):
            value=inspect.ip_reputation({'target':IP},client,api=api)
        self.assertEqual(value['status'],'partial');self.assertIn('Tor',value['summary'])
        self.assertEqual(rows(value)['Tor 官方出口列表'],'命中出口记录')
        self.assertIn('45',next(x['message'] for x in value['checks'] if x['provider']=='proxycheck'))
        self.assertNotIn('PRIVATE_SECRET',json.dumps(value))
        self.assertEqual(client.calls[-1][1],inspect.TOR_EXITS)
        self.assertNotIn(IP,client.calls[-1][1])

    def test_invalid_or_html_tor_list_never_claims_not_listed(self):
        client=Client([api.TestError('bad'),api.TestError('bad')],[{'status':200,'body':b'<html>blocked</html>'}])
        value=inspect.ip_reputation({'target':IP},client,api=api)
        self.assertEqual(rows(value)['Tor 官方出口列表'],'未确认')
        self.assertNotIn('未命中当前列表',json.dumps(value,ensure_ascii=False))

    def test_ownership_ip_mismatch_cannot_be_used_as_network_identity(self):
        client=Client([{'ip':'1.1.1.1','asn':'AS1','country':'Other'},risk()])
        value=inspect.ip_reputation({'target':IP},client,api=api)
        self.assertEqual(rows(value)['网络归属'],'未确认');self.assertEqual(value['status'],'partial')

    def test_cancel_is_not_swallowed_as_source_unavailable(self):
        class Cancelled(api.TestError):pass
        with patch.object(api,'Cancelled',Cancelled,create=True):
            with self.assertRaises(Cancelled):
                inspect.ip_reputation({'target':IP},Client([Cancelled('cancelled')]),api=api)


class DomainTests(unittest.TestCase):
    def test_complete_observation_separates_dns_https_and_http_denial(self):
        client=Client(doh(),[{'status':403,'timings':{'dns':.01,'tcp':.02,'tls':.04,'first_byte':.1,'total':.2}}])
        with patch.object(api,'resolve',return_value=[IP]):
            value=inspect.domain_test({'target':'example.com'},client,api=api)
        self.assertEqual(value['status'],'ok');self.assertEqual(rows(value)['HTTP 状态'],'403')
        self.assertIn('不能代表',value['summary'])
        self.assertEqual(client.calls[0][2]['method'],'HEAD')
        self.assertTrue(all(c[2]['accept']=='application/dns-json' for c in client.calls[1:]))
        self.assertIn('请求 DNS',rows(value));self.assertEqual(len(value['checks']),4)
        self.assertIn('edns_client_subnet=0.0.0.0%2F0',client.calls[-1][1])

    def test_public_nxdomain_and_servfail_remain_distinct_without_declaring_blocked(self):
        client=Client([dns(status=3,addresses=[]),dns(status=2,addresses=[])])
        with patch.object(api,'resolve',side_effect=api.TestError('PRIVATE_SECRET')):
            value=inspect.domain_test({'target':'example.com'},client,api=api)
        self.assertEqual(value['status'],'partial')
        self.assertEqual(rows(value)['Cloudflare DNS'],'NXDOMAIN');self.assertEqual(rows(value)['Google DNS'],'SERVFAIL')
        self.assertTrue(all(c[0]=='json' for c in client.calls))
        self.assertNotIn('PRIVATE_SECRET',json.dumps(value))

    def test_proxy_remote_dns_survives_local_resolution_failure(self):
        client=Client(doh(),[{'status':200}],route={'source':'environment','proxy':'socks5h://127.0.0.1:1080'})
        with patch.object(api,'resolve',side_effect=api.TestError('local failure')):
            value=inspect.domain_test({'target':'example.com'},client,api=api)
        self.assertEqual(value['status'],'partial');self.assertTrue(value['metrics']['https_reachable'])
        self.assertIn('代理 DNS 路径',rows(value));self.assertEqual(rows(value)['HTTP 状态'],'200')

    def test_private_answers_stop_target_connection_but_keep_public_comparison(self):
        client=Client(doh())
        with patch.object(api,'resolve',return_value=['127.0.0.1']):
            value=inspect.domain_test({'target':'example.com'},client,api=api)
        self.assertEqual(value['status'],'partial');self.assertFalse(value['metrics']['https_reachable'])
        self.assertTrue(all(c[0]=='json' for c in client.calls))

    def test_tun_fake_ip_uses_domain_tls_without_false_private_failure(self):
        client=Client(doh(),[{'status':200}])
        with patch.object(api,'resolve',return_value=['198.18.0.3']),patch.object(api,'tunnel_route',return_value=True):
            value=inspect.domain_test({'target':'example.com'},client,api=api)
        self.assertEqual(value['status'],'ok');self.assertIn('TUN DNS 路径',rows(value))
        self.assertIn('https://example.com/',[c[1] for c in client.calls])

    def test_geographic_cdn_dns_difference_is_observation_not_pollution_claim(self):
        client=Client(doh(),[{'status':200}])
        with patch.object(api,'resolve',return_value=['1.1.1.1']):
            value=inspect.domain_test({'target':'example.com'},client,api=api)
        self.assertIn('不同',rows(value)['DNS 对照']);self.assertEqual(value['status'],'ok')
        self.assertIn('差异本身不证明',next(r['detail'] for r in value['rows'] if r['label']=='DNS 对照'))

    def test_mismatched_question_and_truncated_answer_cannot_be_green(self):
        client=Client([dns(host='wrong.example'),dns(TC=True),dns(28)],[{'status':200}])
        with patch.object(api,'resolve',return_value=[IP]):
            value=inspect.domain_test({'target':'example.com'},client,api=api)
        self.assertEqual(value['status'],'partial');self.assertFalse(value['metrics']['public_dns_complete'])

    def test_public_dns_no_data_is_not_green_complete_coverage(self):
        client=Client([dns(addresses=[]),dns(28,addresses=[]),dns(addresses=[]),dns(28,addresses=[])],[{'status':200}])
        with patch.object(api,'resolve',return_value=[IP]):
            value=inspect.domain_test({'target':'example.com'},client,api=api)
        self.assertEqual(value['status'],'partial');self.assertFalse(value['metrics']['public_dns_complete'])

    def test_unrelated_dns_answers_do_not_become_target_addresses(self):
        value=dns();value['Answer'][0]['name']='unrelated.example'
        parsed=inspect._dns_answer(value,'example.com',1,api)
        self.assertEqual(parsed['addresses'],[])
        value=dns();value['Answer'][0]['name']='edge.example.net'
        value['Answer'].insert(0,{'name':'example.com.','type':5,'TTL':30,'data':'edge.example.net.'})
        parsed=inspect._dns_answer(value,'example.com',1,api)
        self.assertEqual(parsed['addresses'],[IP]);self.assertEqual(parsed['aliases'],['edge.example.net'])

    def test_public_ip_literal_has_no_meaningless_external_dns_queries(self):
        client=Client(responses=[{'status':200}])
        with patch.object(api,'resolve',return_value=[IP]):
            value=inspect.domain_test({'target':IP},client,api=api)
        self.assertEqual(value['status'],'ok');self.assertEqual(len(client.calls),1)

    def test_large_dns_answer_reports_truncation_instead_of_full_coverage(self):
        value=dns(addresses=['8.8.8.'+str(n) for n in range(1,40)])
        parsed=inspect._dns_answer(value,'example.com',1,api)
        self.assertTrue(parsed['truncated']);self.assertEqual(len(parsed['addresses']),32)

    def test_public_dns_unknown_schema_or_boolean_rcode_is_rejected(self):
        for data in ({'Status':False,'Question':[]},{'Status':0,'Question':[{'name':'other.test','type':1}]}):
            with self.assertRaises(api.TestError):inspect._dns_answer(data,'example.com',1,api)


class EgressTests(unittest.TestCase):
    def trace(self,second=IP,first=IP,ipv6=IP6):
        client=Client([{'ip':first},{'ip':ipv6}],[{'status':200,'body':('ip='+second+'\nloc=US\nwarp=off\n').encode()}])
        with patch.object(api,'system_network',return_value=[]):
            return inspect.ip_trace({},client,api=api)

    def test_ipv4_and_ipv6_differences_are_not_compared_as_a_leak(self):
        value=self.trace(second=IP6)
        self.assertFalse(value['metrics']['same_family_disagreement'])
        self.assertNotIn('不同出口',value['summary']);self.assertEqual(value['metrics']['observed_ipv6'],2)

    def test_two_different_ipv4_exits_warn_about_split_route_without_leak_claim(self):
        value=self.trace(second='1.1.1.1')
        self.assertEqual(value['status'],'partial');self.assertTrue(value['metrics']['same_family_disagreement'])
        self.assertIn('不能单独证明',value['summary'])

    def test_unsupported_ipv6_is_unknown_not_a_fabricated_failure(self):
        client=Client([{'ip':IP},api.TestError('unavailable')],[{'status':200,'body':('ip='+IP+'\n').encode()}])
        with patch.object(api,'system_network',return_value=[]):
            value=inspect.ip_trace({},client,api=api)
        self.assertEqual(value['status'],'ok');self.assertEqual(rows(value)['IPv6 出口观测'],'未确认')
        self.assertFalse(value['metrics']['same_family_disagreement'])

    def test_incomplete_sources_never_claim_equal_exit(self):
        client=Client([api.TestError('unavailable'),api.TestError('unavailable')],[api.TestError('unavailable')])
        with patch.object(api,'system_network',return_value=[]):
            value=inspect.ip_trace({},client,api=api)
        self.assertEqual(value['status'],'partial');self.assertEqual(value['metrics']['observed_ipv4'],0)
        self.assertNotIn('相同出口',value['summary'])
