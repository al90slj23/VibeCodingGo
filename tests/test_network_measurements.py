import json
import types
import unittest

from vibecue import network_measurements as measurements
from vibecue import network_tests as network

MIB=1024*1024
ID='public-measurement-1'


def api(seconds=35):
    names=('GLOBALPING','REGIONS','PROBE_LIMIT','result','row','finite','text','TestError','RateLimited','target_host','public_ip')
    value=types.SimpleNamespace(**{name:getattr(network,name) for name in names})
    value.GLOBAL_SECONDS=seconds
    return value


def node(country='US', *, http=False, status='finished', loss=0, avg=12):
    result={'status':status,'rawOutput':'PRIVATE_RAW','resolvedAddress':'1.1.1.1'}
    if http:
        result.update(statusCode=403,headers={'Set-Cookie':'PRIVATE_COOKIE'},rawHeaders='PRIVATE_RAW_HEADERS',rawBody='PRIVATE_BODY',
                      timings={'dns':2,'tcp':3,'tls':5,'firstByte':7,'download':1,'total':18},
                      tls={'protocol':'TLSv1.3','authorized':True,'subject':{'CN':'PRIVATE_CN'}})
    else:
        result['stats']={'avg':avg,'min':8,'max':19,'loss':loss,'total':3,'rcv':0 if loss==100 else 3,'drop':3 if loss==100 else 0}
    return {'probe':{'country':country,'city':'Public probe city','asn':123,'network':'Public provider','latitude':1.2,'longitude':3.4,'resolvers':['PRIVATE_RESOLVER']},'result':result}


def reply(nodes=None, *, status='finished', http=False, count=None, **values):
    nodes=nodes if nodes is not None else [node(country,http=http) for country in ('US','DE','SG')]
    body=dict(id=ID,target='example.com',type='http' if http else 'ping',status=status,results=nodes,probesCount=len(nodes) if count is None else count,createdAt='2026-10-30T00:00:00Z')
    body.update(values)
    return {'status':200,'body':json.dumps(body).encode(),'etag':'"test-etag"'}


class Client:
    def __init__(self,responses=(),created=None):
        self.responses=list(responses);self.created=created if created is not None else {'id':ID,'probesCount':3}
        self.calls=[];self.sleeps=[];self.now=100.;self.cancelled=False;self.last_response_meta={}
    def clock(self):return self.now
    def sleep(self,seconds):self.sleeps.append(seconds);self.now+=seconds
    def json(self,url,**kwargs):
        self.calls.append(('POST',url,kwargs))
        if isinstance(self.created,Exception):raise self.created
        return self.created
    def request(self,url,**kwargs):
        self.calls.append(('GET',url,kwargs));self.now+=.1
        value=self.responses.pop(0) if self.responses else {'status':304,'body':b''}
        if callable(value):return value(self)
        if isinstance(value,Exception):raise value
        return value


class GlobalMeasurementTests(unittest.TestCase):
    def run_global(self,client,params=None,kind='global_ping',seconds=35):
        return measurements.global_test(dict({'target':'example.com','probe_limit':3},**(params or {})),client,kind,api=api(seconds))

    def test_flat_official_probe_schema_and_one_probe_per_country(self):
        client=Client([reply()]);result=self.run_global(client)
        request=client.calls[0][2]['json_body']
        self.assertEqual(request['locations'],[{'country':country,'limit':1} for country in ('US','DE','SG')])
        self.assertNotIn('limit',request);self.assertEqual(request['timeout'],15)
        self.assertEqual(client.calls[0][2]['deadline'],110)
        self.assertEqual(client.sleeps,[])
        self.assertEqual(result['status'],'ok')
        self.assertTrue(result['measurement']['complete']);self.assertFalse(result['measurement']['resume_allowed'])
        self.assertEqual(result['measurement']['covered_regions'],['US','DE','SG'])
        self.assertIn('美国',result['rows'][4]['label']);self.assertIn('AS123',result['rows'][4]['detail'])
        self.assertNotIn('PRIVATE_',json.dumps(result))

    def test_duplicate_country_cannot_impersonate_complete_coverage(self):
        result=self.run_global(Client([reply([node('US')]*3)]))
        self.assertEqual(result['status'],'partial');self.assertEqual(result['measurement']['covered_regions'],['US'])
        self.assertEqual(result['measurement']['missing_regions'],['DE','SG'])
        self.assertTrue(result['measurement']['complete']);self.assertFalse(result['measurement']['resume_allowed'])
        self.assertIn('已结束',result['summary'])

    def test_count_mismatch_or_unknown_count_remains_partial(self):
        for created,count in [({'id':ID,'probesCount':2},3),({'id':ID},True)]:
            result=self.run_global(Client([reply(count=count)],created=created))
            self.assertEqual(result['status'],'partial')
            self.assertEqual(result['measurement']['observed_probes'],3)

    def test_terminal_all_loss_is_reported_as_finished_not_still_polling(self):
        result=self.run_global(Client([reply([node(c,avg=None,loss=100) for c in ('US','DE','SG')])]))
        self.assertTrue(result['measurement']['complete']);self.assertFalse(result['measurement']['resume_allowed'])
        self.assertTrue(all(p['reachable'] is False for p in result['measurement']['probes']))
        self.assertTrue(all('100%' in row['value'] for row in result['rows'][4:]))
        self.assertIn('已结束',result['summary'])

    def test_http403_is_a_response_not_a_global_ban_and_stages_are_distinct(self):
        result=self.run_global(Client([reply(http=True)]),kind='global_http')
        self.assertEqual(result['status'],'ok')
        probe=result['measurement']['probes'][0]
        self.assertTrue(probe['reachable']);self.assertEqual(probe['http_status'],403)
        self.assertEqual(probe['timings_ms']['dns'],2);self.assertTrue(probe['tls_authorized'])
        self.assertIn('不等于全网封禁',result['rows'][4]['detail']);self.assertNotIn('PRIVATE_',json.dumps(result))
        self.assertEqual(result['rows'][4]['status'],'warning')

    def test_failed_offline_or_unknown_nodes_do_not_become_green_from_stale_metrics(self):
        for status in ('failed','offline','in-progress','unexpected-new-state'):
            result=self.run_global(Client([reply([node('US',status=status),node('DE'),node('SG')])]))
            self.assertEqual(result['status'],'partial');self.assertFalse(result['measurement']['probes'][0]['usable'])

    def test_missing_loss_or_failed_tls_cannot_turn_into_green_complete_result(self):
        values=[node(country) for country in ('US','DE','SG')]
        values[0]['result']['stats'].pop('loss')
        answer=self.run_global(Client([reply(values)]))
        self.assertEqual(answer['status'],'partial')
        self.assertFalse(answer['measurement']['probes'][0]['usable'])
        values=[node(country,http=True) for country in ('US','DE','SG')]
        values[0]['result']['tls']['authorized']=False
        answer=self.run_global(Client([reply(values,http=True)]),kind='global_http')
        self.assertEqual(answer['status'],'partial')
        self.assertFalse(answer['measurement']['probes'][0]['usable'])

    def test_failed_node_has_safe_diagnostic_category_without_raw_error_text(self):
        values=[node(country) for country in ('US','DE','SG')]
        values[0]['result'].update(status='failed',error={'type':'ETIMEDOUT','message':'PRIVATE_SECRET'})
        answer=self.run_global(Client([reply(values)]))
        self.assertEqual(answer['status'],'partial')
        self.assertIn('节点超时',answer['rows'][4]['detail'])
        self.assertNotIn('PRIVATE_SECRET',json.dumps(answer))

    def test_polling_is_after_response_bounded_and_reuses_etag(self):
        client=Client([reply([node('US')],status='in-progress',count=3),{'status':304,'body':b''},reply()])
        result=self.run_global(client)
        self.assertEqual(result['status'],'ok');self.assertEqual(client.sleeps,[.6,.6])
        self.assertEqual(client.calls[2][2]['etag'],'"test-etag"')
        self.assertEqual(len([c for c in client.calls if c[0]=='POST']),1)

    def test_poll_error_preserves_partial_nodes_and_known_id_without_replay(self):
        client=Client([reply([node('US')],status='in-progress',count=3),network.TestError('暂时不可读')])
        result=self.run_global(client)
        self.assertEqual(result['status'],'partial');self.assertEqual(result['measurement']['id'],ID)
        self.assertEqual(result['measurement']['observed_probes'],1);self.assertTrue(result['measurement']['resume_allowed'])
        self.assertEqual(len([c for c in client.calls if c[0]=='POST']),1)

    def test_rate_limit_retains_id_and_retry_delay(self):
        error=network.RateLimited('限流');error.retry_after_seconds=7
        result=self.run_global(Client([error]))
        self.assertEqual(result['measurement']['retry_after_seconds'],7)
        self.assertTrue(result['measurement']['resume_allowed']);self.assertEqual(result['status'],'partial')

    def test_creation_loss_is_not_automatically_replayed(self):
        client=Client(created=network.TestError('lost'))
        with self.assertRaises(network.TestError):self.run_global(client)
        self.assertEqual(len(client.calls),1)

    def test_resume_only_gets_the_existing_id(self):
        client=Client([reply()]);result=self.run_global(client,{'measurement_id':ID})
        self.assertTrue(result['measurement']['resumed']);self.assertTrue(all(c[0]=='GET' for c in client.calls))
        self.assertEqual(client.calls[0][1],network.GLOBALPING+'/'+ID)

    def test_resume_rejects_url_path_and_wrong_id_target_or_type(self):
        for bad in ('../escape','https://bad.test/id','a?b=1',True,'a'*101):
            client=Client()
            with self.assertRaises(network.TestError):self.run_global(client,{'measurement_id':bad})
            self.assertFalse(client.calls)
        for values in ({'id':'other'},{'target':'other.example'},{'type':'http'}):
            result=self.run_global(Client([reply(**values)]),{'measurement_id':ID})
            self.assertEqual(result['status'],'error');self.assertEqual(result['measurement']['id'],ID)
            self.assertEqual(result['measurement']['observed_probes'],0)

    def test_cancelled_read_retains_handle_and_any_partial_results(self):
        def cancel(client):
            client.cancelled=True;raise network.TestError('cancelled')
        result=self.run_global(Client([reply([node('US')],status='in-progress',count=3),cancel]))
        self.assertEqual(result['status'],'cancelled');self.assertEqual(result['measurement']['observed_probes'],1)
        self.assertTrue(result['measurement']['resume_allowed'])

    def test_total_wait_window_retains_handle_and_stops_polling(self):
        client=Client();result=self.run_global(client,seconds=2)
        self.assertEqual(result['status'],'partial');self.assertTrue(result['measurement']['resume_allowed'])
        self.assertLessEqual(client.now,102.2);self.assertLessEqual(result['measurement']['poll_count'],4)

    def test_all_non_progress_measurement_states_are_terminal(self):
        result=self.run_global(Client([reply(status='failed')]))
        self.assertTrue(result['measurement']['complete']);self.assertFalse(result['measurement']['resume_allowed'])
        self.assertEqual(result['status'],'partial')

    def test_legacy_nested_probe_is_readable_but_raw_body_and_resolvers_omitted(self):
        nodes=[node(c) for c in ('US','DE','SG')]
        for value in nodes:value['probe']={'location':value['probe']}
        result=self.run_global(Client([reply(nodes)]))
        self.assertEqual(result['status'],'ok');self.assertNotIn('PRIVATE_',json.dumps(result))

    def test_invalid_json_and_overlong_node_lists_are_bounded(self):
        result=self.run_global(Client([{'status':200,'body':b'['*2000}]))
        self.assertEqual(result['status'],'error');self.assertEqual(result['measurement']['id'],ID)
        result=self.run_global(Client([reply([node('US')]*30,count=30)]))
        self.assertLessEqual(len(result['measurement']['probes']),6);self.assertEqual(result['status'],'partial')

    def test_idna_input_binding_retains_original_without_changing_canonical_target(self):
        target='xn--fsqu00a.xn--fiqs8s'
        result=self.run_global(Client([reply(target=target)]),{'target':'例子.中国'})
        self.assertEqual(result['measurement']['target'],target);self.assertEqual(result['measurement']['input_target'],'例子.中国')


class SpeedMeasurementTests(unittest.TestCase):
    def run_speed(self,value,size=MIB):
        return measurements.speed_test({'download_bytes':size},Client([value]),api=api())

    def test_total_effective_rate_and_body_rate_use_different_finite_timings(self):
        result=self.run_speed({'status':200,'bytes_received':3*MIB,'elapsed':2.,'timings':{'dns':.1,'tcp':.1,'tls':.2,'first_byte':1.5,'transfer':.5}},3*MIB)
        metrics=result['metrics'];self.assertAlmostEqual(metrics['effective_mbps'],3*MIB*8/2/1e6)
        self.assertAlmostEqual(metrics['transfer_mbps'],3*MIB*8/.5/1e6)
        self.assertEqual(metrics['sample_quality'],'limited');self.assertEqual(metrics['bytes_received'],3*MIB)

    def test_small_or_brief_sample_is_not_presented_as_wan_peak(self):
        result=self.run_speed({'status':200,'bytes_received':MIB,'elapsed':.25,'timings':{'first_byte':.24,'transfer':.01}})
        self.assertEqual(result['metrics']['sample_quality'],'short');self.assertIn('短样本',result['summary'])
        self.assertIn('不是线路峰值',result['rows'][0]['detail'])

    def test_missing_or_inconsistent_transfer_timing_keeps_only_effective_rate(self):
        for timings in ({},{'transfer':3},{'first_byte':1,'transfer':1.5},{'transfer':float('nan')}):
            result=self.run_speed({'status':200,'bytes_received':MIB,'elapsed':2,'timings':timings})
            self.assertIsNone(result['metrics']['transfer_mbps']);self.assertGreater(result['metrics']['effective_mbps'],0)

    def test_partial_wrong_status_and_invalid_timing_do_not_generate_speed(self):
        for values in ({'status':206,'bytes_received':MIB,'elapsed':1},{'status':200,'bytes_received':MIB-1,'elapsed':1},
                       {'status':200,'bytes_received':MIB,'elapsed':0},{'status':200,'bytes_received':MIB,'elapsed':float('nan')}):
            with self.assertRaises(network.TestError):self.run_speed(values)
