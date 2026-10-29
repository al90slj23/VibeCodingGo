"""Explicit API runs use only local fixture servers and fake Hurl processes."""
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from vibecue import engineering_api as api
from vibecue.engineering_runtime import ProcessResult, Runtime


class FakeRuntime:
    def __init__(self, result=None, missing=()):
        self.result = result or ProcessResult(0, stdout=b'[]')
        self.missing = set(missing); self.calls = []
    def find_tool(self, name): return None if name in self.missing else '/usr/bin/'+name
    def run(self, argv, **options):
        self.calls.append((argv,options))
        if callable(self.result): return self.result(argv,options)
        return self.result


def curl_response(code=200, body=b'{"ok":true}', headers=b'Content-Type: application/json\r\n'):
    def respond(argv, options):
        marker = re.search(rb'VCGO_HTTP_[0-9a-f]+', options['input']).group()
        return ProcessResult(0, stdout=b'HTTP/1.1 '+str(code).encode()+b' OK\r\n'+headers+b'\r\n'+body+b'\n'+marker+b':'+str(code).encode()+b':0.125:'+str(len(body)).encode(),duration_ms=126)
    return respond


class APITests(unittest.TestCase):
    def setUp(self):
        self.route = patch.object(api,'_route',return_value={'proxy':'','source':'direct'})
        self.route.start();self.addCleanup(self.route.stop)
    def request(self, runtime=None, **options):
        return api.run(dict(mode='request',url='http://localhost:1234/health',**options),runtime or FakeRuntime(curl_response()))
    def test_simple_curl_request_has_current_status_body_timing_and_no_shell(self):
        runner=FakeRuntime(curl_response());r=self.request(runner)
        self.assertEqual((r['status'],r['response']['status_code'],r['response']['timing_ms']),('success',200,125))
        self.assertTrue(json.loads(r['response']['body'])['ok'])
        self.assertEqual(runner.calls[0][0],['/usr/bin/curl','--disable','--config','-'])
        self.assertIn(b'retry = 0',runner.calls[0][1]['input']);self.assertNotIn(b'location\n',runner.calls[0][1]['input'])
    def test_request_secrets_never_enter_argv_or_structured_report(self):
        secret='a-secret-from-fixture-only';echo=json.dumps({'headers':{'Authorization':'Bearer '+secret},'other':secret,'password':'hidden-value'}).encode()
        runner=FakeRuntime(curl_response(body=echo,headers=('Authorization: Bearer '+secret+'\r\nSet-Cookie: session='+secret+'\r\n').encode()))
        r=self.request(runner,headers='Authorization: Bearer '+secret+'\nX-Auth-Token: '+secret)
        self.assertNotIn(secret,str(runner.calls[0][0]));self.assertNotIn(secret,json.dumps(r))
        self.assertNotIn('hidden-value',json.dumps(r));self.assertIn(secret,runner.calls[0][1]['input'].decode())
        self.assertTrue(all(h['value']=='[已脱敏]' for h in r['response']['headers']))
    def test_query_secret_is_only_in_stdin_and_display_is_redacted(self):
        runner=FakeRuntime(curl_response(body=b'{"url":"http://localhost/?token=fixture-credential"}'))
        r=api.run({'url':'http://localhost/?token=fixture-credential'},runner)
        self.assertNotIn('fixture-credential',json.dumps(r));self.assertIn(b'fixture-credential',runner.calls[0][1]['input'])
    def test_json_redaction_keeps_json_syntax_and_message_boundaries(self):
        body=json.dumps({'note':'Authorization: Bearer fixture-private-token','ok':True}).encode()
        r=self.request(FakeRuntime(curl_response(body=body)),headers='Authorization: Bearer fixture-private-token')
        self.assertTrue(json.loads(r['response']['body'])['ok'])
        self.assertNotIn('fixture-private-token',r['response']['body'])
    def test_json_decimal_literals_and_large_integer_are_not_rounded(self):
        raw=b'{"amount":0.12345678901234567890,"n":9007199254740993,"exponent":1.234567890123456789e-10}'
        result=self.request(FakeRuntime(curl_response(body=raw)))
        self.assertIn('0.12345678901234567890',result['response']['body'])
        self.assertIn('9007199254740993',result['response']['body'])
        self.assertIn('1.234567890123456789e-10',result['response']['body'])
        self.assertEqual(json.loads(result['response']['body'])['n'],9007199254740993)
    def test_short_json_arrays_are_not_silently_cut_at_arbitrary_row_counts(self):
        raw=json.dumps(list(range(1005))).encode()
        result=self.request(FakeRuntime(curl_response(body=raw)))
        self.assertEqual(json.loads(result['response']['body']),list(range(1005)))
        self.assertFalse(result['response']['truncated'])
    def test_http_failures_and_redirect_are_actual_results(self):
        failed=self.request(FakeRuntime(curl_response(503)))
        self.assertEqual(failed['status'],'error');self.assertEqual(failed['response']['status_code'],503)
        redirect=self.request(FakeRuntime(curl_response(302,headers=b'Location: http://other.test/\r\n')))
        self.assertEqual(redirect['response']['status_code'],302);self.assertTrue(redirect['warnings'])
    def test_body_preview_is_bounded_and_marks_incomplete(self):
        r=self.request(FakeRuntime(curl_response(body=b'a'*(api.MAX_PREVIEW+100))))
        self.assertEqual(r['status'],'partial');self.assertTrue(r['response']['truncated'])
        self.assertLessEqual(len(r['response']['body']),api.MAX_PREVIEW)
    def test_timeout_cancel_and_transport_error_do_not_retry(self):
        for p,state in [(ProcessResult(-1,timed_out=True),'error'),(ProcessResult(-1,cancelled=True),'cancelled'),(ProcessResult(60,stderr=b'http://private/?token=do-not-show'),'error')]:
            runner=FakeRuntime(p);r=self.request(runner)
            self.assertEqual(r['status'],state);self.assertEqual(len(runner.calls),1);self.assertNotIn('do-not-show',json.dumps(r))
    def test_rejects_invalid_or_credential_urls_control_headers_and_unbounded_input(self):
        for options in [{'url':'file:///etc/passwd'},{'url':'http://user:password@localhost/'},{'url':'http://localhost/#fragment'},{'url':'http://localhost/\nheader'}, {'method':'CONNECT'}, {'headers':'X-Test: ok\rInjected: yes'}, {'body':'a'*(api.MAX_BODY+1)}, {'timeout_seconds':float('nan')},{'timeout_seconds':True},{'mode':'other'}]:
            runner=FakeRuntime();r=api.run(dict(url='http://localhost/',**{})|options,runner)
            self.assertEqual(r['status'],'error',options);self.assertFalse(runner.calls)
    def test_head_and_post_body_are_explicit(self):
        runner=FakeRuntime(curl_response());self.request(runner,method='HEAD');self.assertIn(b'\nhead\n',runner.calls[0][1]['input'])
        runner=FakeRuntime(curl_response());self.request(runner,method='POST',body='{"input":"hello"}')
        self.assertIn(b'request = "POST"',runner.calls[0][1]['input']);self.assertIn(b'data-binary = ',runner.calls[0][1]['input'])
    def test_interim_response_headers_do_not_become_body_or_final_status(self):
        body,headers,status=api._split_response(b'HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 201 Created\r\nX-Result: yes\r\n\r\nbody')
        self.assertEqual((body,headers,status),(b'body',[('X-Result','yes')],201))
    def collection(self,content,runtime=None):
        with tempfile.TemporaryDirectory(prefix='vcgo-hurl-test-') as temp:
            path=Path(temp)/'checks.hurl';path.write_text(content)
            return api.run({'mode':'hurl','collection_path':str(path)},runtime or FakeRuntime())
    def test_hurl_executes_collection_once_from_stdin_with_private_config_home(self):
        content='GET http://localhost:1234/health\nHTTP 200\n[Asserts]\njsonpath "$.ok" == true\n'
        report=[{'entries':[{'calls':[{'response':{'status':200,'headers':[{'name':'Set-Cookie','value':'secret-cookie'}]}}]}]}]
        runner=FakeRuntime(ProcessResult(0,stdout=json.dumps(report).encode(),duration_ms=3));r=self.collection(content,runner)
        self.assertEqual(r['status'],'success');self.assertEqual(r['rows'][0]['text'],'HTTP 200')
        argv,options=runner.calls[0];self.assertIn('--json',argv);self.assertIn('--test',argv);self.assertNotIn('--report-json',argv)
        self.assertEqual(options['input'].decode(),content);self.assertFalse(Path(options['env']['HOME']).exists())
        self.assertNotIn('secret-cookie',json.dumps(r));self.assertEqual(argv[argv.index('--jobs')+1],'1')
        self.assertIsInstance(json.loads(r['response']['body']),list)
        self.assertEqual(argv[argv.index('--max-time')+1],'15000ms')
    def test_hurl_missing_is_actionable_but_simple_requests_still_work(self):
        r=self.collection('GET http://localhost/\nHTTP 200\n',FakeRuntime(missing={'hurl'}))
        self.assertEqual(r['status'],'error');self.assertIn('未安装',r['summary']);self.assertTrue(r['items'])
        self.assertEqual(self.request(FakeRuntime(curl_response(),missing={'hurl'}))['status'],'success')
    def test_collection_rejects_disk_options_repeat_dynamic_host_and_external_body(self):
        for text in ['GET http://localhost/\n[Options]\noutput: /tmp/result\n','GET http://localhost/\n[Options]\nrepeat: 1000\n','GET http://localhost/\n[Options]\ninsecure: true\n','GET {{dynamic_host}}/\n','GET http://localhost/\n\nfile,/etc/passwd;\n','GET http://localhost/\nPOST {{target}}/\n','TRACE http://localhost/\n']:
            runner=FakeRuntime();r=self.collection(text,runner)
            self.assertEqual(r['status'],'error',text);self.assertFalse(runner.calls)
    def test_collection_status_failure_cancellation_and_schema_change_are_controlled(self):
        for result,status in [(ProcessResult(4,stdout=b'[]',stderr=b'fixture-secret'),'error'),(ProcessResult(-1,cancelled=True),'cancelled'),(ProcessResult(-1,timed_out=True),'error'),(ProcessResult(0,stdout=b'not-json'),'partial')]:
            r=self.collection('GET http://localhost/\nHTTP 200\n',FakeRuntime(result));self.assertEqual(r['status'],status)
            self.assertNotIn('fixture-secret',json.dumps(r))
    def test_multi_target_pac_differences_are_not_silently_bypassed(self):
        runner=FakeRuntime()
        with patch.object(api,'_route',side_effect=[{'proxy':'http://one:8','source':'pac'},{'proxy':'','source':'pac'}]):
            r=self.collection('GET http://one.test/\nGET http://two.test/\n',runner)
        self.assertEqual(r['status'],'error');self.assertIn('代理',r['summary']);self.assertFalse(runner.calls)
    @unittest.skipUnless(hasattr(os,'mkfifo'),'requires POSIX')
    def test_named_pipe_is_rejected_before_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'pipe.hurl';os.mkfifo(path)
            r=api.run({'mode':'hurl','collection_path':str(path)},FakeRuntime())
            self.assertEqual(r['status'],'error');self.assertIn('普通文件',r['summary'])
    def test_http_proxy_selection_does_not_reuse_https_proxy_for_http_url(self):
        self.route.stop()
        from vibecue import network_tests
        try:
            with patch.dict(os.environ,{'http_proxy':'http://http-proxy:8080','https_proxy':'http://https-proxy:8081','no_proxy':''},clear=True):
                self.assertEqual(api._route('http://api.test/')['proxy'],'http://http-proxy:8080')
                self.assertEqual(api._route('https://api.test/')['proxy'],'http://https-proxy:8081')
            with patch.dict(os.environ,{'https_proxy':'http://https-only:8081','NO_PROXY':'localhost'},clear=True):
                self.assertEqual(api._route('http://localhost/')['source'],'bypass')
        finally:self.route.start()


class LocalServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.received=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                cls.received.append((self.command,self.path))
                if self.path=='/redirect':
                    self.send_response(302);self.send_header('Location','/target');self.end_headers();return
                body=json.dumps({'received_authorization':self.headers.get('Authorization'),'echo':self.headers.get('X-Auth-Token'),'ok':self.path!='/error'}).encode()
                self.send_response(503 if self.path=='/error' else 200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.send_header('Set-Cookie','fixture-cookie=private');self.end_headers();self.wfile.write(body)
        try:cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        except PermissionError as error:raise unittest.SkipTest(str(error))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join(2)
    def request(self,path,**options):
        with patch.object(api,'_route',return_value={'proxy':'','source':'direct'}):
            return api.run(dict(url=f'http://127.0.0.1:{self.server.server_port}'+path,**options),Runtime())
    def test_real_curl_reports_local_status_and_redacts_echoed_header_values(self):
        result=self.request('/ok',headers='Authorization: Bearer fixture-private-token\nX-Auth-Token: fixture-private-token')
        self.assertEqual(result['response']['status_code'],200);self.assertEqual(result['status'],'success')
        self.assertNotIn('fixture-private-token',json.dumps(result));self.assertNotIn('fixture-cookie=private',json.dumps(result))
        self.assertGreater(result['response']['bytes'],0)
    def test_real_curl_does_not_follow_redirect_or_treat_http_error_as_success(self):
        start=len(self.received);result=self.request('/redirect');self.assertEqual(result['response']['status_code'],302)
        self.assertEqual(self.received[start:],[('GET','/redirect')])
        result=self.request('/error');self.assertEqual(result['response']['status_code'],503);self.assertEqual(result['status'],'error')

    @unittest.skipUnless(os.environ.get('VIBECUE_TEST_HURL'), 'set VIBECUE_TEST_HURL to a verified official binary')
    def test_real_hurl_success_and_redacted_structured_json(self):
        self.real_hurl(200, 'success')
    @unittest.skipUnless(os.environ.get('VIBECUE_TEST_HURL'), 'set VIBECUE_TEST_HURL to a verified official binary')
    def test_real_hurl_failure_includes_assertion_error_rows(self):
        result=self.real_hurl(201, 'error')
        self.assertTrue(any(row['source']=='Hurl · 断言' and row['level']=='error' for row in result['rows']))
    def real_hurl(self, expected, status):
        binary=Path(os.environ['VIBECUE_TEST_HURL']).resolve()
        self.assertTrue(binary.is_absolute() and binary.is_file())
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'local.hurl'
            path.write_text(f'GET http://127.0.0.1:{self.server.server_port}/ok\nAuthorization: Bearer fixture-private-token\nHTTP {expected}\n[Asserts]\njsonpath "$.ok" == true\n')
            with patch.object(api,'_route',return_value={'proxy':'','source':'direct'}):
                result=api.run({'mode':'hurl','collection_path':str(path)},Runtime(search_path=str(binary.parent)))
        self.assertEqual(result['status'],status,result['summary'])
        self.assertIsInstance(json.loads(result['response']['body']),dict)
        self.assertNotIn('fixture-private-token',json.dumps(result))
        self.assertNotIn('fixture-cookie=private',json.dumps(result))
        return result
