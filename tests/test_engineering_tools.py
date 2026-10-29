import copy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import engineering_tools as tools
from vibecue.engineering_runtime import EngineeringError, ProcessResult, Runtime


class FakeRuntime:
    cancelled=False
    def __init__(self, mise=False, lnav=False):self.mise=mise;self.lnav=lnav;self.calls=[]
    def find_tool(self,name):return '/fixture/'+name if (name=='mise' and self.mise) or (name=='lnav' and self.lnav) else None
    def capability(self,name):
        version={'node':'v20.10.0','npm':'10.0.0','pnpm':'9.1.0','python3':'Python 3.11.2','go':'go version go1.22.0 darwin/arm64','git':'git version 2.42.0'}.get(name)
        if (name=='mise' and self.mise) or (name=='lnav' and self.lnav):version='fixture 1.0'
        return {'available':bool(version),'path':'/fixture/'+name if version else None,'version':version}
    def run(self,args,**kwargs):
        self.calls.append((args,kwargs))
        if args[0].endswith('mise'):return ProcessResult(0,json.dumps([{'version':'22.0.0','installed':True}] if args[-1]=='node' else []).encode())
        filename=args[-1]
        return ProcessResult(0,json.dumps([{'log_path':filename,'log_time':'2026-10-30 12:00:00.000','log_level':'error','log_text':'2026-10-30T12:00:00Z ERROR failed token=[已脱敏]'}],ensure_ascii=False).encode())


class EngineeringToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def log(self,name='a.log',text='2026-10-30T12:00:00Z INFO started\n2026-10-30T12:00:01Z ERROR failure\n'):
        path=self.root/name;path.write_text(text);return str(path)

    def test_partial_strict_comparators_are_unknown_instead_of_false_pass_or_fail(self):
        for expected in ('>20', '<=20', '>20.1', '<=20.1'):
            self.assertIsNone(tools.version_match(expected, 'v20.1.1'), expected)
        self.assertIs(tools.version_match('>20.1.0', 'v20.1.1'), True)
        self.assertIs(tools.version_match('<=20.1.0', 'v20.1.1'), False)
        self.assertIs(tools.version_match('>=20', 'v20.1.1'), True)

    def test_found_but_unverified_capability_never_says_installed_or_passed(self):
        runtime = FakeRuntime()
        runtime.capability = lambda name: {'available': True, 'path': '/fixture/' + name,
                                           'version': None, 'version_error': '版本探测超时'}
        result = tools.capabilities(runtime=runtime)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['items'])
        for item in result['items']:
            self.assertEqual(item['status'], 'unknown')
            self.assertEqual(item['value'], '已找到，版本未核实')

    def test_environment_shows_optional_engine_identity_without_duplicate_probes(self):
        runtime = FakeRuntime(mise=True, lnav=True)
        original = runtime.capability
        probed = []
        def capability(name):
            probed.append(name)
            return original(name)
        runtime.capability = capability
        result = tools.environment({'project_path': str(self.root)}, runtime)
        self.assertEqual(probed, list(tools.CORE_TOOLS + tools.OPTIONAL_TOOLS))
        inventory = {item['label']: item for item in result['items'] if item['label'].startswith('工程引擎 · ')}
        self.assertEqual(set(inventory), {'工程引擎 · '+name for name in tools.OPTIONAL_TOOLS})
        for name in ('mise', 'lnav'):
            item = inventory['工程引擎 · '+name]
            self.assertEqual(item['value'], 'fixture 1.0')
            self.assertEqual(item['status'], 'success')
            self.assertEqual(item['detail'], '/fixture/'+name)
        self.assertEqual(inventory['工程引擎 · hurl']['value'], '未安装')
        self.assertEqual(inventory['工程引擎 · hurl']['status'], 'unknown')
        # Only the pre-existing, explicit mise inventory commands are executed.
        self.assertEqual(len(runtime.calls), 3)
        self.assertEqual(result['engine'], 'builtin+mise')

    def test_optional_engine_version_failure_keeps_path_and_never_claims_pass(self):
        (self.root/'package.json').write_text('{"engines":{"node":"20"}}')
        runtime = FakeRuntime(mise=True)
        original = runtime.capability
        def capability(name):
            if name == 'hurl':
                return {'available': True, 'version': None, 'path': '/fixture/hurl', 'version_error': '版本探测超时'}
            return original(name)
        runtime.capability = capability
        result = tools.environment({'project_path': str(self.root)}, runtime)
        item = next(item for item in result['items'] if item['label'] == '工程引擎 · hurl')
        self.assertEqual(item['value'], '已找到，版本未核实')
        self.assertEqual(item['status'], 'unknown')
        self.assertIn('/fixture/hurl', item['detail'])
        self.assertIn('版本探测超时', item['detail'])
        # Unavailable optional engines do not turn successful static checks into
        # failures; their actual state is reported on the individual engine row.
        self.assertEqual(result['status'], 'success')

    def test_static_project_requirements_and_versions(self):
        (self.root/'package.json').write_text(json.dumps({'engines':{'node':'>=22','npm':'>=9'},'packageManager':'pnpm@9.1.0','scripts':{'postinstall':'touch should-not-run'}}))
        (self.root/'.python-version').write_text('3.11\n');(self.root/'go.mod').write_text('module example\ngo 1.21\n')
        r=tools.environment({'project_path':str(self.root)},FakeRuntime())
        self.assertEqual(r['kind'],'environment');self.assertEqual(r['engine'],'builtin')
        checks={x['tool']:x for x in r['requirements']};self.assertEqual(checks['node']['status'],'error');self.assertEqual(checks['npm']['status'],'success');self.assertEqual(checks['python3']['status'],'success');self.assertEqual(checks['go']['status'],'success')
        self.assertFalse((self.root/'should-not-run').exists())

    def test_mise_inventory_is_real_no_config_not_project_execution(self):
        (self.root/'mise.toml').write_text('[tools]\nnode = "22"\n[env]\nSECRET = "{{exec(command=\"dangerous\")}}"\n')
        runtime=FakeRuntime(mise=True);r=tools.environment({'project_path':str(self.root)},runtime)
        self.assertEqual(r['engine'],'builtin+mise');self.assertEqual(r['mise_installed_versions'],1)
        args,kwargs=runtime.calls[0];self.assertIn('--no-config',args);self.assertIn('--installed',args);self.assertEqual(kwargs['env']['MISE_NO_CONFIG'],'1');self.assertNotEqual(kwargs['cwd'],str(self.root))
        self.assertNotIn('dangerous',json.dumps(r));self.assertNotIn('SECRET',json.dumps(r))

    def test_semver_supported_subset_and_unknown_syntax(self):
        for expected,actual,result in [('20','v20.1.2',True),('>=20 <22','v22.0.0',False),('^0.2.1','0.3.0',False),('^0.0.2','0.0.3',False),('~20.1','20.2.0',False),('18 || >=20','20.0.0',True),('lts/*','20.0.0',None),('>=2','go version go1.22.0 darwin/arm64',False),('>=20','v20.0.0-rc1',None)]:
            self.assertIs(tools.version_match(expected,actual),result,(expected,actual))

    def test_symlinked_project_file_is_not_read(self):
        outside=self.root/'outside';outside.mkdir();secret=outside/'config';secret.write_text('{"engines":{"node":"99"}}')
        project=self.root/'project';project.mkdir();(project/'package.json').symlink_to(secret)
        r=tools.environment({'project_path':str(project)},FakeRuntime())
        self.assertEqual(r['requirements'],[]);self.assertTrue(any('符号链接' in x for x in r['warnings']))

    def test_duplicate_static_requirements_remain_bounded(self):
        (self.root/'mise.toml').write_text('[tools]\n'+'node = "20"\n'*1900)
        r=tools.environment({'project_path':str(self.root)},FakeRuntime());self.assertEqual(len(r['requirements']),1)

    def test_logs_literal_filter_level_and_dedup(self):
        path=self.log();r=tools.logs({'paths':[path,path],'query':'fail','level':'error','maximum_lines':200},FakeRuntime())
        self.assertEqual(len(r['rows']),1);self.assertEqual(len(r['sources']),1);row=r['rows'][0]
        self.assertEqual(row['line'],2);self.assertEqual(row['line_scope'],'file');self.assertEqual(row['level'],'error')
        self.assertEqual(tools.logs({'paths':[path],'query':'.*'},FakeRuntime())['rows'],[])

    def test_large_file_is_tail_bounded_and_line_scope_explicit(self):
        path=self.root/'large.log';path.write_bytes(('2026-10-30T12:00:00Z INFO '+('中文'*40)+'\n').encode()*8000)
        r=tools.logs({'paths':[str(path)],'maximum_lines':1000},FakeRuntime())
        self.assertLessEqual(r['metrics']['bytes_read'],tools.MAX_LOG_FILE);self.assertTrue(r['metrics']['truncated']);self.assertEqual(r['sources'][0]['line_scope'],'tail')
        self.assertTrue(all(row['line_scope']=='tail' for row in r['rows']));self.assertLess(len(json.dumps(r,ensure_ascii=False).encode()),256*1024)

    def test_unknown_timestamp_and_level_are_not_fabricated(self):
        path=self.log(text='a plain line without recognized fields\n')
        row=tools.logs({'paths':[path]},FakeRuntime())['rows'][0]
        self.assertIsNone(row['timestamp']);self.assertEqual(row['level'],'unknown')

    def test_lnav_receives_bounded_redacted_snapshots_and_enriches_true_fields(self):
        path=self.log(text='2026-10-30T12:00:00Z ERROR failed token=private\n');runtime=FakeRuntime(lnav=True)
        r=tools.logs({'paths':[path]},runtime)
        self.assertEqual(r['engine'],'builtin+lnav');self.assertEqual(r['rows'][0]['parser'],'lnav');self.assertEqual(r['metrics']['lnav_enriched_rows'],1)
        self.assertNotIn('token=private',json.dumps(r));args,kwargs=runtime.calls[0]
        self.assertIn('-N',args);self.assertIn(':write-json-to -',args);self.assertNotEqual(args[-1],path);self.assertFalse(Path(args[-1]).exists());self.assertNotEqual(kwargs['env']['HOME'],str(Path.home()))

    def test_multiline_private_key_is_redacted_before_line_split(self):
        path=self.log(text='-----BEGIN PRIVATE KEY-----\nfixture-base64-secret\n-----END PRIVATE KEY-----\n')
        r=tools.logs({'paths':[path]},FakeRuntime())
        self.assertNotIn('fixture-base64-secret',json.dumps(r));self.assertEqual(len(r['rows']),3)

    def test_timestamp_merge_normalizes_explicit_timezones(self):
        first=self.log('first.log','2026-10-30T13:00:00+09:00 INFO early\n')
        second=self.log('second.log','2026-10-30T12:00:00+00:00 INFO later\n')
        r=tools.logs({'paths':[first,second]},FakeRuntime())
        self.assertIn('later',r['rows'][-1]['text'])

    def test_long_line_is_explicitly_truncated(self):
        path=self.log(text='INFO '+('x'*10000)+'\n')
        r=tools.logs({'paths':[path]},FakeRuntime())
        self.assertTrue(r['metrics']['truncated']);self.assertLessEqual(len(r['rows'][0]['text']),tools.MAX_ROW_TEXT)

    def test_lnav_bad_protocol_retains_native_rows(self):
        runtime=FakeRuntime(lnav=True);runtime.run=lambda *a,**k:ProcessResult(0,b'not json')
        r=tools.logs({'paths':[self.log()]},runtime);self.assertEqual(len(r['rows']),2);self.assertEqual(r['engine'],'builtin');self.assertTrue(r['warnings'])

    def test_rotation_changes_are_marked_without_claiming_live_follow(self):
        path=self.root/'a.log';path.write_text('plain\n');original=os.read;swapped=False
        def rotate(fd,size):
            nonlocal swapped
            data=original(fd,size)
            if data and not swapped:
                swapped=True;path.rename(self.root/'a.log.old');path.write_text('new file\n')
            return data
        with patch.object(tools.os,'read',side_effect=rotate):r=tools.logs({'paths':[str(path)]},FakeRuntime())
        self.assertTrue(r['sources'][0]['rotated_during_read']);self.assertEqual(r['rows'][0]['text'],'plain');self.assertEqual(r['status'],'partial')

    def test_bad_paths_limits_and_fifo_are_controlled(self):
        for payload in [{'paths':[]},{'paths':['relative']},{'paths':[str(self.root)]},{'paths':[self.log()],'maximum_lines':1001},{'paths':[self.log()],'level':'sql'}]:
            with self.assertRaises(EngineeringError):tools.logs(payload,FakeRuntime())
        fifo=self.root/'fifo';os.mkfifo(fifo)
        with self.assertRaises(EngineeringError):tools.absolute(str(fifo))

    def test_cancelled_operation_never_reads_log_or_runs_optional_tool(self):
        runtime=FakeRuntime(lnav=True);runtime.cancelled=True
        r=tools.logs({'paths':[self.log()]},runtime)
        self.assertEqual(r['status'],'cancelled');self.assertEqual(r['rows'],[]);self.assertEqual(runtime.calls,[])

    def test_cli_malformed_input_is_controlled_structured_error(self):
        fake=io.TextIOWrapper(io.BytesIO(b'{not-json}'),encoding='utf-8')
        with patch('sys.stdin',fake),patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(tools.main(['environment']),0);r=json.loads(output.getvalue());self.assertEqual(r['status'],'error');self.assertEqual(r['kind'],'environment')


if __name__=='__main__':unittest.main()
