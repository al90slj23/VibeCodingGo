"""Finite networking processes, routing and precise transfer metadata."""
import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vibecue import network_tests as network
from vibecue.network_runtime import NetworkRuntime, NetworkCancelled


def captured(body=b'data', *, status=200, headers=b'', timing=b'200:1.0:0.1:0.2:0.3:0.6:4'):
    return subprocess.CompletedProcess([],0,stdout=b'HTTP/2 '+str(status).encode()+b'\r\n'+headers+b'\r\n'+body+b'\nVCGO_META:'+timing)


class NetworkProcessTests(unittest.TestCase):
    def test_construction_and_expired_deadline_never_start_process(self):
        now=[0.0]
        with patch('subprocess.Popen',side_effect=AssertionError('eager process')):
            operation=NetworkRuntime(timeout=1,clock=lambda:now[0]);now[0]=2
            with self.assertRaises(subprocess.TimeoutExpired):
                operation.capture([sys.executable,'-c','pass'])

    def test_large_stdin_is_streamed_while_both_output_pipes_are_drained(self):
        script='import os,sys;os.write(1,b"x"*120000);os.write(2,b"e"*12000);v=sys.stdin.buffer.read();print(len(v))'
        operation=NetworkRuntime(timeout=3)
        value=operation.capture([sys.executable,'-c',script],input=b'i'*200000,timeout=2,limit=200000)
        self.assertEqual(value.returncode,0);self.assertEqual(len(value.stderr),12000)
        self.assertTrue(value.stdout.endswith(b'200000\n'));self.assertEqual(len(value.stdout),120007)

    def test_output_caps_stop_oversized_stdout_and_stderr(self):
        for number,limit in ((1,2048),(2,100000)):
            with self.subTest(pipe=number):
                operation=NetworkRuntime(timeout=2)
                value=operation.capture([sys.executable,'-c',f'import os;os.write({number},b"x"*1000000)'],timeout=1,limit=limit)
                self.assertEqual(value.returncode,-1000);self.assertLessEqual(len(value.stdout),limit)
                self.assertEqual(value.stderr,b'')

    def test_deadline_ends_a_disposable_child_without_waiting_for_its_sleep(self):
        operation=NetworkRuntime(timeout=2);began=time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            operation.capture([sys.executable,'-c','import time;time.sleep(10)'],timeout=.15)
        self.assertLess(time.monotonic()-began,1.5)

    def test_cancellation_ends_the_child_and_its_descendant_group(self):
        with tempfile.TemporaryDirectory(prefix='vcgo-network-cancel-') as temp:
            marker=Path(temp)/'pid'
            script='import subprocess,sys,time;from pathlib import Path;p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);Path(sys.argv[1]).write_text(str(p.pid));time.sleep(30)'
            operation=NetworkRuntime(timeout=10);outcome=[]
            def execute():
                try: operation.capture([sys.executable,'-c',script,str(marker)],timeout=8)
                except NetworkCancelled: outcome.append('cancelled')
            thread=threading.Thread(target=execute);thread.start()
            end=time.monotonic()+3
            while not marker.exists() and time.monotonic()<end: time.sleep(.01)
            self.assertTrue(marker.exists());pid=int(marker.read_text())
            operation.cancelled=True;thread.join(timeout=2)
            self.assertFalse(thread.is_alive());self.assertEqual(outcome,['cancelled'])
            # An exited orphan may briefly remain a zombie until init reaps it.
            result=subprocess.run(['/bin/ps','-p',str(pid),'-o','stat='],capture_output=True,text=True)
            self.assertTrue(result.returncode != 0 or result.stdout.lstrip().startswith('Z'),result.stdout)

    def test_signal_scope_is_restored_and_sleep_is_cancelable(self):
        previous=signal.getsignal(signal.SIGTERM);operation=NetworkRuntime(timeout=2)
        with operation.signals():
            self.assertIs(NetworkRuntime.current(),operation)
            operation.cancelled=True
            with self.assertRaises(NetworkCancelled): operation.sleep(1)
        self.assertIsNone(NetworkRuntime.current());self.assertEqual(signal.getsignal(signal.SIGTERM),previous)

    def test_argv_and_stdin_are_bounded_without_shell_execution(self):
        operation=NetworkRuntime()
        for argv in ('echo secret',['relative'],[sys.executable,'bad\0value']):
            with self.assertRaises(ValueError):operation.capture(argv)
        with self.assertRaises(ValueError):operation.capture([sys.executable],input=b'x'*(256*1024+1))


class NetworkTransportTests(unittest.TestCase):
    def request(self, value=None, **options):
        value=value or captured()
        spec=dict(url='https://example.com/',method='GET',max_bytes=1000,timeout=2,**options)
        return network._curl_request(spec,{'source':'direct','proxy':''},runner=lambda *a,**k:value)

    def test_real_default_client_does_not_create_a_fetch_python_worker(self):
        client=network.HTTPClient(proxy_check=lambda _: {'source':'direct','proxy':''})
        value={'status':200,'body':base64.b64encode(b'{}').decode(),'elapsed':.1}
        with patch.object(network,'_curl_request',return_value=value) as curl, patch('subprocess.Popen',side_effect=AssertionError('Python worker started')):
            self.assertEqual(client.json('https://example.com/',deadline=time.monotonic()+2),{})
        self.assertEqual(curl.call_count,1);self.assertIs(curl.call_args.kwargs['runtime'],client.runtime)

    def test_timings_keep_total_and_body_transfer_distinct(self):
        value=self.request()
        self.assertEqual(value['timings']['dns'],.1)
        self.assertAlmostEqual(value['timings']['tcp'],.1);self.assertAlmostEqual(value['timings']['tls'],.1)
        self.assertEqual(value['timings']['first_byte'],.6);self.assertEqual(value['timings']['total'],1)
        self.assertAlmostEqual(value['timings']['transfer'],.4);self.assertEqual(value['elapsed'],1)

    def test_nonfinite_or_impossible_timing_and_wrong_status_are_not_trusted(self):
        for timing in (b'200:nan',b'200:1:0.1:0.2:0.3:2:4',b'403:1:0.1:0.2:0.3:0.6:4'):
            with self.subTest(timing=timing):self.assertEqual(self.request(captured(timing=timing)),{'error':'invalid'})

    def test_http_looking_body_is_not_consumed_as_another_response_header(self):
        body=b'HTTP/2 403\r\nX-Body: yes\r\n\r\ncontent'
        value=self.request(captured(body=body,timing=b'200:1'))
        self.assertEqual(base64.b64decode(value['body']),body)

    def test_interim_headers_are_skipped_but_final_status_must_match(self):
        value=captured();value.stdout=b'HTTP/1.1 100 Continue\r\n\r\n'+value.stdout
        self.assertEqual(self.request(value)['status'],200)

    def test_retry_quota_and_redirect_are_safe_and_bounded(self):
        headers=b'Retry-After: 120\r\nX-RateLimit-Limit: 250\r\nX-RateLimit-Remaining: 0\r\nContent-Type: application/json\r\nContent-Length: 4\r\nLocation: https://other.example/private/SECRET?token=SECRET\r\nSet-Cookie: SECRET\r\n'
        value=self.request(captured(headers=headers))
        self.assertEqual(value['retry_after_seconds'],120);self.assertEqual(value['quota'],{'limit':250,'remaining':0})
        self.assertEqual(value['redirect'],'https://other.example');self.assertNotIn('SECRET',json.dumps(value))
        self.assertEqual(value['content_type'],'application/json');self.assertEqual(value['content_length'],4)

    def test_rate_limit_typed_error_retains_metadata_without_body_or_credentials(self):
        data={'status':429,'body':'e30=','retry_after_seconds':30,'quota':{'remaining':0},'elapsed':.1}
        client=network.HTTPClient(proxy_check=lambda _:{'source':'direct','proxy':''})
        with patch.object(network,'_curl_request',return_value=data),self.assertRaises(network.RateLimited) as error:
            client.request('https://example.com/',deadline=time.monotonic()+2)
        self.assertEqual(error.exception.status_code,429);self.assertEqual(error.exception.retry_after_seconds,30)
        self.assertEqual(error.exception.code,'rate_limited');self.assertEqual(client.last_response_meta['quota'],{'remaining':0})
        self.assertNotIn('body',client.last_response_meta)

    def test_dns_json_accept_is_explicit_but_arbitrary_accepts_are_rejected(self):
        client=network.HTTPClient(proxy_check=lambda _:{'source':'direct','proxy':''})
        with patch.object(network,'_curl_request',return_value={'status':200,'body':'e30='}) as curl:
            self.assertEqual(client.json('https://example.com/',deadline=time.monotonic()+2,accept='application/dns-json'),{})
            self.assertEqual(curl.call_args.args[0]['accept'],'application/dns-json')
        with self.assertRaises(network.TestError):client.request('https://example.com/',deadline=time.monotonic()+2,accept='text/html\r\nInjected: yes')

    def test_default_http_and_https_environment_routes_do_not_cross(self):
        proxies={'http_proxy':'http://127.0.0.1:8081','https_proxy':'http://127.0.0.1:8082'}
        with patch.dict(os.environ,proxies,clear=True):
            self.assertEqual(network.proxy_guard('http://example.com/')['proxy'],'http://127.0.0.1:8081')
            self.assertEqual(network.proxy_guard('https://example.com/')['proxy'],'http://127.0.0.1:8082')

    def test_port_scoped_no_proxy_matches_default_https_port(self):
        value=network.proxy_guard('https://example.com/',proxies={'https':'http://127.0.0.1:8082','no':'example.com:443'})
        self.assertEqual(value,{'proxy':'','source':'bypass'})

    def test_dns_process_is_isolated_and_inherits_no_api_credentials(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'SECRET','HTTPS_PROXY':'http://SECRET'}):
            with patch.object(NetworkRuntime,'capture',return_value=subprocess.CompletedProcess([],0,stdout=b'["8.8.8.8"]')) as run:
                self.assertEqual(network.resolve('example.com'),['8.8.8.8'])
        self.assertIn('-I',run.call_args.args[0]);env=run.call_args.kwargs['env']
        self.assertNotIn('OPENAI_API_KEY',env);self.assertNotIn('HTTPS_PROXY',env)

    def test_failed_system_dns_command_does_not_publish_stale_stdout(self):
        def runner(args,**kwargs):return subprocess.CompletedProcess(args,1,stdout=b'nameserver[0] : 1.1.1.1\n')
        rows=network.system_network(runner)
        self.assertEqual(next(x['value'] for x in rows if x['label']=='系统 DNS 配置'),'未确认')

    def test_local_dns_and_system_reads_honor_the_feature_deadline(self):
        seen=[]
        def runner(args,**kwargs):
            seen.append(kwargs['timeout'])
            return subprocess.CompletedProcess(args,0,stdout=b'["8.8.8.8"]' if '-c' in args else b'')
        self.assertEqual(network.resolve('example.com',runner,deadline=time.monotonic()+.2),['8.8.8.8'])
        self.assertLessEqual(seen[0],.2)
        with self.assertRaises(network.TestError):
            network.resolve('example.com',lambda *a,**k:self.fail('expired DNS started'),deadline=time.monotonic()-1)
        value=network.system_network(lambda *a,**k:self.fail('expired system read started'),deadline=time.monotonic()-1)
        self.assertEqual(next(x['value'] for x in value if x['label']=='系统 DNS 配置'),'未确认')

    def test_cancelled_client_stops_before_route_or_sleep(self):
        client=network.HTTPClient(proxy_check=lambda _:self.fail('route was read'))
        client.runtime.cancelled=True
        with self.assertRaises(network.Cancelled):client.request('https://example.com/',deadline=time.monotonic()+1)
        with self.assertRaises(network.Cancelled):client.sleep(1)
