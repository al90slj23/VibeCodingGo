"""Real local cancellation and proxy selection, without public requests."""
import json
import base64
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vibecue import engineering_api,network_tests


class NetworkIntegrationTests(unittest.TestCase):
    def test_http_and_https_tools_share_exact_proxy_selection(self):
        env={'HTTP_PROXY':'http://127.0.0.1:10001','HTTPS_PROXY':'http://127.0.0.1:10002'}
        with patch.dict(os.environ,env,clear=True),patch.object(network_tests,'_system_proxy',side_effect=AssertionError('unexpected direct/system fallback')):
            self.assertEqual(engineering_api._route('http://example.com/')['proxy'],env['HTTP_PROXY'])
            self.assertEqual(engineering_api._route('https://example.com/')['proxy'],env['HTTPS_PROXY'])
            self.assertEqual(network_tests.proxy_guard('http://example.com/')['proxy'],env['HTTP_PROXY'])
            self.assertEqual(network_tests.proxy_guard('https://example.com/')['proxy'],env['HTTPS_PROXY'])
        self.assertEqual(network_tests.proxy_guard('http://example.com/',proxies={'https':env['HTTPS_PROXY']})['source'],'direct')

    def test_no_proxy_port_rule_does_not_disable_proxy_for_other_ports(self):
        env={'HTTPS_PROXY':'http://127.0.0.1:10002','NO_PROXY':'example.com:8443'}
        with patch.dict(os.environ,env,clear=True):
            self.assertEqual(engineering_api._route('https://example.com:8443/')['source'],'bypass')
            self.assertEqual(engineering_api._route('https://example.com/')['proxy'],env['HTTPS_PROXY'])

    def test_zero_measured_time_is_not_clamped_into_invented_throughput(self):
        def run(args,**kwargs):
            return subprocess.CompletedProcess(args,0,stdout=b'HTTP/2 200\r\n\r\nx\nVCGO_META:200:0:0:0:0:0:1')
        data=network_tests._curl_request({'url':'https://example.com/','method':'GET','max_bytes':1000,'timeout':1,'count_only':True},{'source':'direct','proxy':''},runner=run)
        self.assertEqual(data['elapsed'],0)
        self.assertEqual(data['timings']['total'],0)

    def test_untrusted_nested_json_is_controlled_at_cli_and_provider_boundaries(self):
        root=Path(__file__).resolve().parents[1]
        payload='{"kind":"domain","nested":'+('['*2000)+(']'*2000)+'}'
        env=dict(os.environ,PYTHONPATH=str(root),PYTHONDONTWRITEBYTECODE='1')
        run=subprocess.run([sys.executable,'-m','vibecue.network_tests','run'],input=payload.encode(),capture_output=True,env=env,timeout=4)
        self.assertEqual(run.returncode,0);self.assertEqual(json.loads(run.stdout)['status'],'error')
        self.assertNotIn(b'Traceback',run.stderr)
        body=('['*2000+']'*2000).encode()
        def provider(args,**kwargs):
            value={'status':200,'body':base64.b64encode(body).decode(),'elapsed':.1,'bytes_received':len(body)}
            return subprocess.CompletedProcess(args,0,stdout=json.dumps(value).encode())
        client=network_tests.HTTPClient(runner=provider,proxy_check=lambda url:{'source':'direct','proxy':''})
        with self.assertRaises(network_tests.TestError):client.json('https://example.com/',deadline=client.clock()+2)
        self.assertIsNone(network_tests.finite(10**1000))

    def test_not_modified_response_does_not_decode_a_nonexistent_gzip_body(self):
        def run(args,**kwargs):
            return subprocess.CompletedProcess(args,0,stdout=b'HTTP/2 304\r\nContent-Encoding: gzip\r\nETag: "cached"\r\n\r\n\nVCGO_META:304:0.1')
        data=network_tests._curl_request({'url':'https://example.com/','method':'GET','max_bytes':1000,'timeout':1},{'source':'direct','proxy':''},runner=run)
        self.assertEqual(data['status'],304);self.assertEqual(data['bytes_received'],0)
        self.assertEqual(data['etag'],'"cached"')

    def test_cancelling_measurement_creation_is_cancelled_without_replay(self):
        class Client:
            cancelled=False
            calls=0
            clock=lambda self:100.
            def json(self,*args,**kwargs):
                self.calls+=1
                raise network_tests.Cancelled()
        client=Client();value=network_tests.run({'kind':'global_ping','target':'example.com'},client)
        self.assertEqual(value['status'],'cancelled');self.assertEqual(value['error_code'],'cancelled')
        self.assertEqual(client.calls,1)

    def test_cli_sigterm_returns_cancelled_and_closes_its_proxy_connection(self):
        with tempfile.TemporaryDirectory(prefix='vcgo-network-cli-') as temporary, socket.socket() as server:
            try:
                server.bind(('127.0.0.1',0));server.listen(1)
            except PermissionError as error:self.skipTest(str(error))
            server.settimeout(3);events=[];errors=[]
            env={'HOME':temporary,'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8',
                 'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'PYTHONDONTWRITEBYTECODE':'1',
                 'HTTPS_PROXY':'http://127.0.0.1:'+str(server.getsockname()[1]),'NO_PROXY':''}
            process=subprocess.Popen([sys.executable,'-m','vibecue.network_tests','run'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
            def proxy():
                try:
                    conn,_=server.accept()
                    with conn:
                        conn.settimeout(2);data=b''
                        while b'\r\n\r\n' not in data and len(data)<8192:
                            block=conn.recv(4096)
                            if not block:break
                            data+=block
                        events.append(data.startswith(b'CONNECT api.ipapi.is:443'))
                        began=time.monotonic();os.kill(process.pid,signal.SIGTERM)
                        events.append(conn.recv(1)==b'')
                        events.append(time.monotonic()-began<2)
                except Exception as error:errors.append(type(error).__name__)
            thread=threading.Thread(target=proxy,daemon=True);thread.start()
            try:
                stdout,stderr=process.communicate(json.dumps({'kind':'ip_reputation','target':'8.8.8.8'}).encode(),timeout=6)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:process.wait(timeout=2)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=2)
                thread.join(timeout=3)
            self.assertEqual(process.returncode,0)
            self.assertFalse(errors,errors);self.assertEqual(events,[True,True,True])
            self.assertEqual(json.loads(stdout)['status'],'cancelled')
            self.assertNotIn(b'Traceback',stderr)
