import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from vibecue.engineering_runtime import EngineeringError, Runtime, redact


class RuntimeTests(unittest.TestCase):
    def test_import_does_not_start_processes(self):
        import vibecue.engineering_runtime as module
        with patch('subprocess.Popen', side_effect=AssertionError('eager child')):
            module.Runtime()

    def test_stdout_stderr_are_drained_without_deadlock(self):
        r=Runtime().run([sys.executable,'-c',"import os;os.write(2,b'e'*70000);os.write(1,b'o'*70000)"],max_output=200000,timeout=3)
        self.assertEqual(r.returncode,0);self.assertEqual(len(r.stdout),70000);self.assertEqual(len(r.stderr),70000)

    def test_input_and_output_both_stream_without_blocking(self):
        data=b'x'*200000
        r=Runtime().run([sys.executable,'-c',"import sys;d=sys.stdin.buffer.read();sys.stdout.buffer.write(d)"],input=data,max_output=250000,timeout=3)
        self.assertEqual(r.returncode,0);self.assertEqual(r.stdout,data)

    def test_combined_output_budget_is_enforced_and_marked(self):
        r=Runtime().run([sys.executable,'-c',"import os;os.write(1,b'x'*100000);os.write(2,b'y'*100000)"],max_output=4096,timeout=2)
        self.assertTrue(r.truncated);self.assertLessEqual(len(r.stdout)+len(r.stderr),4096)

    def test_deadline_cleans_child_that_holds_pipe_open(self):
        began=time.monotonic()
        r=Runtime().run([sys.executable,'-c',"import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)'])"],timeout=.15)
        self.assertTrue(r.timed_out);self.assertLess(time.monotonic()-began,2)

    def test_cancellation_is_observed_without_waiting_for_deadline(self):
        runtime=Runtime()
        timer=threading.Timer(.1,lambda:setattr(runtime,'cancelled',True));timer.start()
        try:r=runtime.run([sys.executable,'-c','import time;time.sleep(10)'],timeout=5)
        finally:timer.cancel()
        self.assertTrue(r.cancelled);self.assertLess(r.duration_ms,1500)

    def test_signal_scope_restores_previous_handler(self):
        runtime=Runtime();before=signal.getsignal(signal.SIGTERM)
        with runtime.signals():
            handler=signal.getsignal(signal.SIGTERM);handler(signal.SIGTERM,None);self.assertTrue(runtime.cancelled)
        self.assertEqual(signal.getsignal(signal.SIGTERM),before)

    def test_no_shell_or_unbounded_input(self):
        with self.assertRaises(EngineeringError):Runtime().run('echo hello')
        with self.assertRaises(EngineeringError):Runtime().run(['echo','hello'])
        with self.assertRaises(EngineeringError):Runtime().run([sys.executable],input=b'x'*300000)
        r=Runtime().run(['/bin/echo','$(touch /tmp/vcgo-must-not-exist)'],timeout=1)
        self.assertEqual(r.stdout.strip(),b'$(touch /tmp/vcgo-must-not-exist)')

    def test_environment_excludes_secrets_and_corepack_downloads(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'fixture-secret','AUTHORIZATION':'secret','HTTP_PROXY':'http://proxy','COREPACK_ENABLE_NETWORK':'1'}):
            env=Runtime.child_environment()
            self.assertNotIn('OPENAI_API_KEY',env);self.assertNotIn('AUTHORIZATION',env);self.assertNotIn('HTTP_PROXY',env)
            self.assertEqual(env['COREPACK_ENABLE_NETWORK'],'0')
        self.assertEqual(Runtime.child_environment({'HTTPS_PROXY':'http://proxy'})['HTTPS_PROXY'],'http://proxy')

    def test_relative_search_paths_and_unknown_tools_are_ignored(self):
        runtime=Runtime(search_path='.:relative')
        self.assertIsNone(runtime.find_tool('not-a-supported-command'))
        with tempfile.TemporaryDirectory() as directory:
            executable=Path(directory)/'hurl';executable.write_text('#!/bin/sh\nprintf "hurl fixture\\n"\n');executable.chmod(0o700)
            found=Runtime(search_path=directory).capability('hurl')
            self.assertTrue(found['available']);self.assertEqual(found['version'],'hurl fixture')

    def test_reports_redact_tokens_headers_and_connection_passwords(self):
        text='Authorization: Bearer abc123\npassword=secret api_key=private\nhttps://u:p@example.com?token=hide\nCookie: session=cookie-secret\nSet-Cookie: session=set-cookie-secret\nsk-abcdefghijklmnop ghp_abcdefghijklmno'
        safe=redact(text)
        for secret in ('abc123','secret','private','hide','abcdefghijklmnop','abcdefghijklmno','u:p@','cookie-secret','set-cookie-secret'):self.assertNotIn(secret,safe)
        self.assertLessEqual(len(redact('a'*10000)),4000)


if __name__=='__main__':unittest.main()
