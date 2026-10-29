import json
import os
from pathlib import Path
import re
import struct
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

from vibecue import engineering_compat as compat
from vibecue.engineering_runtime import ProcessResult, Runtime


def macho(cpu=compat.ARM64, filetype=2):
    return struct.pack('<IIIIIIII', 0xFEEDFACF, cpu, 0, filetype, 0, 0, 0, 0)


class ArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.path=self.root/'binary'
    def write(self,value):self.path.write_bytes(value);return self.path
    def test_thin_arm64_requires_a_mac_executable(self):
        result=compat.verify_arm64(self.write(macho()))
        self.assertEqual(result,{'architecture':'arm64','universal':False})
        for bad in (b'#!/bin/sh\n',macho(0x01000007),macho(filetype=6)):
            with self.assertRaises(compat.CompatError):compat.verify_arm64(self.write(bad))
    def test_symlink_and_directory_are_not_accepted_as_binary(self):
        original=self.root/'real';original.write_bytes(macho());self.path.symlink_to(original)
        with self.assertRaises(compat.CompatError):compat.verify_arm64(self.path)
        with self.assertRaises(compat.CompatError):compat.verify_arm64(self.root)
    def fat(self,wide=False,cpu=compat.ARM64,offset=128,size=32,slice_cpu=None):
        magic=0xCAFEBABF if wide else 0xCAFEBABE
        head=struct.pack('>II',magic,1)
        arch=struct.pack('>IIQQII',cpu,0,offset,size,0,0) if wide else struct.pack('>IIIII',cpu,0,offset,size,0)
        return (head+arch).ljust(128,b'\0')+macho(cpu if slice_cpu is None else slice_cpu)
    def test_fat32_and_fat64_validate_actual_arm64_slice(self):
        for wide in (False,True):
            self.assertEqual(compat.verify_arm64(self.write(self.fat(wide))),{'architecture':'arm64','universal':True})
            with self.assertRaises(compat.CompatError):compat.verify_arm64(self.write(self.fat(wide,slice_cpu=0x01000007)))
            with self.assertRaises(compat.CompatError):compat.verify_arm64(self.write(self.fat(wide,cpu=0x01000007)))
    def test_bad_fat_count_table_or_slice_never_scans_whole_binary(self):
        for bad in (struct.pack('>II',0xCAFEBABE,1000).ljust(32,b'\0'),
                    struct.pack('>II',0xCAFEBABE,3).ljust(32,b'\0'),
                    self.fat(offset=0),self.fat(offset=0xFFFFFFF0),self.fat(size=0)):
            with self.assertRaises(compat.CompatError):compat.verify_arm64(self.write(bad))
        with patch.object(compat.os,'pread',wraps=compat.os.pread) as read:
            compat.verify_arm64(self.write(self.fat()))
        self.assertLessEqual(sum(call.args[1] for call in read.call_args_list),1100)


class FakeRunner:
    cancelled=False
    def __init__(self,mode='good'):
        self.mode=mode;self.calls=[]
    def run(self,args,**options):
        self.calls.append((args,options))
        if self.mode=='cancelled':return ProcessResult(-1,cancelled=True)
        if self.mode=='truncated':return ProcessResult(0,stdout=b'[]',truncated=True)
        if self.mode=='timed_out':return ProcessResult(-1,timed_out=True)
        if args[0].endswith('mise'):
            data=[] if self.mode=='good' else {'node':[]}
            return ProcessResult(0,stdout=json.dumps(data).encode())
        if args[0].endswith('lnav'):
            path=Path(args[-1]);text=path.read_text().strip()
            data=[{'log_path':str(path),'log_time':'2026-10-30 00:00:00.000','log_level':'error','log_text':text}]
            if self.mode=='unknown':data[0]['log_level']='unknown'
            if self.mode=='unmatched':data[0]['log_path']='/not-the-probe.log'
            if self.mode=='schema':data={}
            return ProcessResult(0,stdout=json.dumps(data).encode())
        raw=options['input'].decode();url=re.search(r'^GET (http://127\.0\.0\.1:\d+/[^\n]+)',raw).group(1)
        if self.mode!='no_request':
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url,timeout=1) as response:self.asserted=json.load(response)
        failing='"$.ok" == false' in raw
        report={'entries':[{'calls':[{'response':{'status':200}}],
                            'asserts':[{'success':True},{'success':not failing}]}]}
        code=4 if failing else 0
        if self.mode=='ignored_assertion':code=0
        if self.mode=='schema':report={'entries':None}
        if self.mode=='no_assertion':report['entries'][0]['asserts']=[]
        return ProcessResult(code,stdout=json.dumps(report).encode())


class ProbeTests(unittest.TestCase):
    def probe(self,name,runner=None):return compat.verify_compatibility(name,'/fixture/'+name,runner or FakeRunner())
    def test_mise_uses_only_offline_isolated_inventory_commands(self):
        runner=FakeRunner();report=self.probe('mise',runner)
        self.assertTrue(report['compatible']);self.assertEqual(len(runner.calls),3)
        self.assertEqual([args[-1] for args,_ in runner.calls],['node','python','go'])
        for args,options in runner.calls:
            self.assertIn('--no-config',args);self.assertIn('--installed',args)
            env=options['env'];self.assertEqual(env['MISE_OFFLINE'],'1');self.assertEqual(env['MISE_AUTO_INSTALL'],'false')
            self.assertNotEqual(env['HOME'],str(Path.home()));self.assertFalse(Path(env['HOME']).exists())
            self.assertEqual(options['max_output'],compat.MAX_REPORT)
            self.assertLessEqual(options['timeout'],5)
            self.assertNotIn('OPENAI_API_KEY',env)
    def test_mise_grouped_json_is_rejected_for_current_adapter(self):
        with self.assertRaises(compat.CompatError):self.probe('mise',FakeRunner('schema'))
    def test_cancel_timeout_and_truncation_stop_probes_immediately(self):
        for mode in ('cancelled','timed_out','truncated'):
            runner=FakeRunner(mode)
            with self.assertRaises(compat.CompatError):self.probe('mise',runner)
            self.assertEqual(len(runner.calls),1)
    def test_hurl_verifies_http_success_real_assertion_failure_and_no_proxy(self):
        runner=FakeRunner();report=self.probe('hurl',runner)
        self.assertTrue(report['compatible']);self.assertEqual(len(runner.calls),2)
        self.assertTrue(runner.asserted['ok'])
        for args,options in runner.calls:
            self.assertEqual(args[args.index('--retry')+1],'0')
            self.assertEqual(args[args.index('--jobs')+1],'1')
            self.assertEqual(args[args.index('--max-redirs')+1],'0')
            self.assertEqual(options['env']['NO_PROXY'],'*');self.assertEqual(options['env']['HTTPS_PROXY'],'')
            self.assertRegex(options['input'].decode(),r'^GET http://127\.0\.0\.1:\d+/vcgo-compat-')
    def test_hurl_cannot_pass_by_printing_a_report_without_requests(self):
        with self.assertRaises(compat.CompatError):self.probe('hurl',FakeRunner('no_request'))
    def test_hurl_ignoring_assertions_or_returning_unknown_schema_is_rejected(self):
        for mode in ('ignored_assertion','schema','no_assertion'):
            with self.assertRaises(compat.CompatError):self.probe('hurl',FakeRunner(mode))
    def test_lnav_verifies_fixed_sql_and_recognized_time_level_source(self):
        runner=FakeRunner();report=self.probe('lnav',runner)
        self.assertTrue(report['compatible']);self.assertEqual(len(runner.calls),1)
        args,options=runner.calls[0]
        self.assertIn('-n',args);self.assertIn('-N',args);self.assertIn(':write-json-to -',args)
        self.assertTrue(any('FROM all_logs' in arg for arg in args))
        self.assertFalse(Path(args[-1]).exists());self.assertEqual(options['env']['TZ'],'UTC')
    def test_lnav_unknown_level_unmatched_source_or_schema_does_not_pass(self):
        for mode in ('unknown','unmatched','schema'):
            with self.assertRaises(compat.CompatError):self.probe('lnav',FakeRunner(mode))
    def test_unknown_names_relative_paths_and_tool_error_text_are_not_exposed(self):
        for name,path in [('other','/fixture/other'),('mise','relative')]:
            with self.assertRaises(compat.CompatError):compat.verify_compatibility(name,path,FakeRunner())
        runner=FakeRunner();runner.run=lambda *a,**k:ProcessResult(1,stderr=b'credential=private-value')
        with self.assertRaises(compat.CompatError) as error:self.probe('mise',runner)
        self.assertNotIn('private-value',str(error.exception))


class OfficialProbeTests(unittest.TestCase):
    def check(self,name):
        binary=Path(os.environ['VIBECUE_TEST_'+name.upper()]).resolve()
        compat.verify_arm64(binary)
        runner=Runtime(timeout=30)
        with runner.signals():result=compat.verify_compatibility(name,binary,runner)
        self.assertTrue(result['compatible'])
    @unittest.skipUnless(os.environ.get('VIBECUE_TEST_MISE'),'set VIBECUE_TEST_MISE to verified official binary')
    def test_real_mise_offline_inventory_contract(self):self.check('mise')
    @unittest.skipUnless(os.environ.get('VIBECUE_TEST_HURL'),'set VIBECUE_TEST_HURL to verified official binary')
    def test_real_hurl_request_and_assertion_contract(self):self.check('hurl')
    @unittest.skipUnless(os.environ.get('VIBECUE_TEST_LNAV'),'set VIBECUE_TEST_LNAV to verified official binary')
    def test_real_lnav_headless_sql_contract(self):self.check('lnav')
