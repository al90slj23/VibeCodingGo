import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from vibecue import engineering_install as install
from vibecue import engineering_maintenance as update
from vibecue.engineering_compat import CompatError
from vibecue.engineering_runtime import ProcessResult


def archive_bytes(name,version,format):
    data=(name+' '+version+'\n').encode();output=io.BytesIO()
    if format=='zip':
        with zipfile.ZipFile(output,'w') as stream:stream.writestr('package/bin/'+name,data)
    else:
        with tarfile.open(fileobj=output,mode='w:gz') as stream:
            member=tarfile.TarInfo('package/bin/'+name);member.size=len(data);member.mode=0o700
            stream.addfile(member,io.BytesIO(data))
    return output.getvalue()


class Runner:
    cancelled=False
    deadline=100000000
    def __init__(self):self.calls=[]
    def clock(self):return 1
    def run(self,argv,**kw):
        self.calls.append((argv,kw));version=Path(argv[0]).read_text().strip()
        return ProcessResult(0,stdout=(version+'\n').encode())


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.home=Path(self.temp.name).resolve();self.root=self.home/'engineering-tools'
        self.specs=copy.deepcopy(install.TOOLS);self.archives={};self.next_specs={}
        for name,spec in self.specs.items():
            olddata=archive_bytes(name,spec['version'],spec['format']);spec['size']=len(olddata);spec['sha256']=hashlib.sha256(olddata).hexdigest();self.archives[spec['asset']]=olddata
            pieces=spec['version'].split('.');pieces[-1]=str(int(pieces[-1])+1);version='.'.join(pieces)
            candidate=install.official_spec(name,version,'0'*64,1)
            data=archive_bytes(name,version,spec['format']);candidate.update(size=len(data),sha256=hashlib.sha256(data).hexdigest());self.next_specs[name]=candidate;self.archives[candidate['asset']]=data
        self.patch=patch.object(install,'TOOLS',self.specs);self.patch.start();self.addCleanup(self.patch.stop)
        self.fetches=[];self.downloads=[];self.probes=[]

    def bootstrap(self):
        return install.install(self.home,downloader=lambda spec,path:path.write_bytes(self.archives[spec['asset']]),version_check=lambda p,n,s:n+' '+s['version'])

    def fetch(self,name,runner):self.fetches.append(name);return self.next_specs[name]
    def download(self,spec,path,runner):self.downloads.append(spec['asset']);path.write_bytes(self.archives[spec['asset']])
    def compatibility(self,name,path,runner):self.probes.append(name);return {'compatible':True,'summary':'fixture contract'}
    def maintain(self,**kw):
        return update.maintain(self.home,runner=Runner(),release_fetcher=self.fetch,downloader=self.download,compatibility=self.compatibility,architecture=lambda *_:None,now=1800000000,**kw)

    def test_status_is_metadata_only_and_does_not_initialize_an_empty_home(self):
        with patch.object(install,'digest',side_effect=AssertionError('hash')),patch('subprocess.Popen',side_effect=AssertionError('process')):
            value=update.status(self.home)
        self.assertFalse(self.root.exists());self.assertEqual(value['config'],update.DEFAULT_CONFIG)
        self.assertTrue(all(row['status']=='missing' for row in value['tools']))
        self.bootstrap()
        with patch.object(install,'digest',side_effect=AssertionError('hash')),patch('subprocess.Popen',side_effect=AssertionError('process')):
            value=update.status(self.home)
        self.assertTrue(all(row['installed_version'] for row in value['tools']))
        self.assertEqual(value['next_check_at'],0)

    def test_config_persists_revision_and_rejects_invalid_values_without_replacing(self):
        a=update.configure(self.home,{'automatic_updates':False,'interval_hours':48});b=update.configure(self.home,{'interval_hours':12})
        self.assertEqual((a['config_revision'],b['config_revision']),(1,2));self.assertFalse(b['config']['automatic_updates'])
        before=(self.root/'settings.json').read_bytes()
        for payload in ({'automatic_updates':'yes'},{'interval_hours':True},{'interval_hours':5},{'interval_hours':169},{'source':'https://untrusted/'},{'revision':0}):
            with self.assertRaises(install.InstallError):update.configure(self.home,payload)
            self.assertEqual((self.root/'settings.json').read_bytes(),before)

    def test_disabled_and_not_due_maintenance_do_no_network_processes_or_hashes(self):
        self.bootstrap();update.configure(self.home,{'automatic_updates':False})
        with patch.object(install,'digest',side_effect=AssertionError('hash')):
            result=self.maintain()
        self.assertFalse(self.fetches);self.assertFalse(self.downloads);self.assertFalse(result['config']['automatic_updates'])
        update.configure(self.home,{'automatic_updates':True});install.atomic_json(self.root/'maintenance.json',{'tools':{},'checked_at':1800000000})
        with patch.object(install,'digest',side_effect=AssertionError('hash')):self.maintain()
        self.assertFalse(self.fetches)

    def test_competing_commit_before_maintenance_lease_reloads_due_policy_and_revision(self):
        self.bootstrap();real_flock=update.fcntl.flock
        for automatic in (True,False):
            for name in ('maintenance.json','settings.json'):(self.root/name).unlink(missing_ok=True)
            injected=[]
            def acquire(descriptor,mode):
                result=real_flock(descriptor,mode)
                if not injected and mode==update.fcntl.LOCK_EX|update.fcntl.LOCK_NB:
                    injected.append(True)
                    install.atomic_json(self.root/'maintenance.json',{'tools':{},'checked_at':1800000000,'revision':100})
                    install.atomic_json(self.root/'settings.json',{'automatic_updates':automatic,'interval_hours':24,'revision':20})
                return result
            with patch.object(update.fcntl,'flock',side_effect=acquire):value=self.maintain()
            self.assertFalse(self.fetches);self.assertFalse(self.downloads)
            self.assertEqual(value['state_revision'],100);self.assertEqual(value['config_revision'],20)
            self.assertEqual(value['config']['automatic_updates'],automatic)
            self.assertEqual(update._state(self.home)['revision'],100)

    def test_forced_check_uses_state_revision_committed_before_lease_instead_of_old_baseline(self):
        self.bootstrap();real_flock=update.fcntl.flock;injected=[]
        def acquire(descriptor,mode):
            result=real_flock(descriptor,mode)
            if not injected and mode==update.fcntl.LOCK_EX|update.fcntl.LOCK_NB:
                injected.append(True)
                install.atomic_json(self.root/'maintenance.json',{'tools':{},'checked_at':1800000000,'revision':100})
            return result
        with patch.object(update.fcntl,'flock',side_effect=acquire):value=self.maintain(force=True)
        self.assertEqual(len(self.fetches),3);self.assertGreater(value['state_revision'],100)
        self.assertEqual(update._state(self.home)['revision'],value['state_revision'])

    def test_state_revision_advances_for_progress_and_completion_but_not_cheap_reads(self):
        self.bootstrap();self.assertEqual(update.status(self.home)['state_revision'],0)
        revisions=[];original=install.atomic_json
        def record(path,value):
            if path.name=='maintenance.json':revisions.append(value['revision'])
            original(path,value)
        with patch.object(install,'atomic_json',side_effect=record):value=self.maintain(force=True)
        self.assertEqual(revisions,list(range(1,len(revisions)+1)))
        self.assertGreater(len(revisions),3);self.assertEqual(value['state_revision'],revisions[-1])
        before=(self.root/'maintenance.json').read_bytes()
        self.assertEqual(update.status(self.home)['state_revision'],revisions[-1])
        self.assertEqual((self.root/'maintenance.json').read_bytes(),before)

    def test_unknown_or_false_compatibility_results_never_publish_current_or_new_candidate(self):
        self.bootstrap();before=install.load_manifest(self.root)
        for specs in (self.specs,self.next_specs):
            result=update.maintain(self.home,force=True,runner=Runner(),release_fetcher=lambda name,_:specs[name],downloader=self.download,compatibility=lambda *_:{'compatible':False},architecture=lambda *_:None,now=1800000000)
            self.assertEqual(install.load_manifest(self.root),before)
            self.assertTrue(all(row['status']=='incompatible' for row in result['tools']))

    def test_new_versions_are_verified_then_published_per_tool_with_previous_preserved(self):
        original=self.bootstrap();value=self.maintain(force=True);manifest=install.load_manifest(self.root)
        self.assertEqual(self.fetches,list(self.specs));self.assertEqual(len(self.downloads),3);self.assertEqual(self.probes,list(self.specs))
        for name in self.specs:
            entry=manifest['tools'][name]
            self.assertEqual(entry['version'],self.next_specs[name]['version']);self.assertEqual(entry['compatibility_schema'],1)
            self.assertEqual(entry['previous']['binary'],original['tools'][name]['binary']);self.assertTrue((self.root/entry['previous']['binary']).exists())
            self.assertTrue(install.verified_entry(self.root,name,self.next_specs[name],entry,linked=True))
        self.assertEqual({row['status'] for row in value['tools']},{'updated'});self.assertGreater(value['next_check_at'],1800000000)

    def test_unchanged_unvalidated_version_is_probed_once_then_reused(self):
        self.bootstrap();self.next_specs=copy.deepcopy(self.specs)
        self.maintain(force=True);self.assertEqual(len(self.probes),3);self.assertFalse(self.downloads)
        self.probes.clear();self.maintain(force=True)
        self.assertFalse(self.probes);self.assertFalse(self.downloads)

    def test_incompatible_candidate_does_not_replace_current_and_other_tools_can_update(self):
        before=self.bootstrap()
        def compatibility(name,*_):
            if name=='hurl':raise CompatError('fixed incompatible')
            return {'compatible':True}
        value=update.maintain(self.home,force=True,runner=Runner(),release_fetcher=self.fetch,downloader=self.download,compatibility=compatibility,architecture=lambda *_:None,now=1800000000)
        current=install.load_manifest(self.root)['tools']
        self.assertEqual(current['hurl'],before['tools']['hurl']);self.assertEqual({row['name']:row['status'] for row in value['tools']}['hurl'],'incompatible')
        self.assertEqual(current['mise']['version'],self.next_specs['mise']['version']);self.assertEqual(value['next_check_at'],1800000300)

    def test_bad_hash_never_reaches_version_architecture_or_probe(self):
        before=self.bootstrap();prior=(self.root/'manifest.json').read_bytes()
        for spec in self.next_specs.values():spec['sha256']='a'*64
        value=self.maintain(force=True)
        self.assertEqual((self.root/'manifest.json').read_bytes(),prior);self.assertFalse(self.probes)
        self.assertTrue(all(row['status']=='error' for row in value['tools']))

    def test_release_parser_rejects_unverified_prerelease_ambiguous_or_external_assets(self):
        spec=self.next_specs['hurl'];base={'draft':False,'prerelease':False,'tag_name':spec['tag'],'assets':[{'name':spec['asset'],'state':'uploaded','size':spec['size'],'digest':'sha256:'+spec['sha256'],'browser_download_url':install.source_url(spec)}]}
        self.assertEqual(update.release_spec('hurl',base),spec)
        wrong=[]
        for field,value in [('prerelease',True),('draft',True),('tag_name','8.1.1-rc1')]:x=copy.deepcopy(base);x[field]=value;wrong.append(x)
        for field,value in [('digest',None),('digest','sha256:unknown'),('size',True),('size',129*1024*1024),('browser_download_url','https://other.example/tool')]:x=copy.deepcopy(base);x['assets'][0][field]=value;wrong.append(x)
        x=copy.deepcopy(base);x['assets']*=2;wrong.append(x)
        for value in wrong:
            with self.assertRaises(install.InstallError):update.release_spec('hurl',value)

    def test_releases_latest_never_downgrades_a_newer_local_version(self):
        self.bootstrap();self.maintain(force=True);self.downloads.clear();self.probes.clear();self.next_specs=self.specs
        value=self.maintain(force=True)
        self.assertFalse(self.downloads);self.assertFalse(self.probes);self.assertTrue(all(row['status']=='current' for row in value['tools']))

    def test_later_verified_versions_survive_pinned_bootstrap_and_verify_only(self):
        self.bootstrap();self.maintain(force=True);prior=install.load_manifest(self.root)
        result=install.install(self.home,downloader=lambda *_:self.fail('downloaded baseline'),version_check=lambda *_:self.fail('ran baseline'))
        self.assertEqual(result['tools'],prior['tools']);self.assertEqual(install.install(self.home,verify_only=True)['status'],'verified')

    def test_invalid_newer_manifest_does_not_get_silently_downgraded(self):
        self.bootstrap();self.maintain(force=True);value=install.load_manifest(self.root);value['tools']['hurl']['repo']='untrusted/hurl';install.atomic_json(self.root/'manifest.json',value)
        with self.assertRaises(install.InstallError):install.install(self.home,downloader=lambda *_:self.fail('downloaded'))
        self.assertEqual(install.load_manifest(self.root),value)

    def test_lease_defers_activation_then_reuses_verified_candidate_without_download(self):
        self.bootstrap();before=install.load_manifest(self.root)
        with install.tool_use_lock(self.home,nonblocking=True) as acquired:
            self.assertTrue(acquired);value=self.maintain(force=True)
        self.assertEqual(install.load_manifest(self.root),before);self.assertTrue(all(row['status']=='deferred' for row in value['tools']))
        self.downloads.clear();self.probes.clear();value=self.maintain(force=True)
        self.assertFalse(self.downloads);self.assertFalse(self.probes);self.assertTrue(all(row['status']=='updated' for row in value['tools']))

    def test_user_disables_during_checks_prevents_automatic_publication_and_latest_config_wins(self):
        self.bootstrap();prior=install.load_manifest(self.root)
        def compatibility(name,*_):
            update.configure(self.home,{'automatic_updates':False})
            return {'compatible':True}
        result=update.maintain(self.home,runner=Runner(),release_fetcher=self.fetch,downloader=self.download,compatibility=compatibility,architecture=lambda *_:None,now=1800000000)
        self.assertEqual(install.load_manifest(self.root),prior);self.assertFalse(result['config']['automatic_updates']);self.assertEqual(result['config_revision'],3)
        self.assertTrue(all(row['status']=='deferred' for row in result['tools']))

    def test_explicit_check_can_run_when_automatic_is_disabled(self):
        self.bootstrap();update.configure(self.home,{'automatic_updates':False});value=self.maintain(force=True)
        self.assertFalse(value['config']['automatic_updates']);self.assertTrue(all(row['status']=='updated' for row in value['tools']))

    def test_manifest_write_failure_restores_original_links(self):
        self.bootstrap();prior=install.load_manifest(self.root);real=install.atomic_json
        def write(path,value):
            if path.name=='manifest.json':raise OSError('fixture disk error')
            return real(path,value)
        with patch.object(install,'atomic_json',side_effect=write):self.maintain(force=True)
        self.assertEqual(install.load_manifest(self.root),prior)
        for name,entry in prior['tools'].items():self.assertEqual(os.readlink(self.root/'bin'/name),'../'+entry['binary'])

    def test_failure_retry_is_persisted_bounded_and_not_repeated_before_due(self):
        self.bootstrap()
        def fail(*_):raise install.InstallError('官方目录暂不可读。')
        a=update.maintain(self.home,force=True,runner=Runner(),release_fetcher=fail,now=1800000000)
        self.assertEqual(a['next_check_at'],1800000300)
        with patch.object(update,'fetch_release',side_effect=AssertionError('fetch')):self.maintain()
        self.assertFalse(self.fetches)
        b=update.maintain(self.home,force=True,runner=Runner(),release_fetcher=fail,now=1800000010)
        self.assertEqual(b['next_check_at'],1800000610)

    def test_cancelled_runner_does_not_download_or_publish(self):
        self.bootstrap();before=install.load_manifest(self.root);runner=Runner();runner.cancelled=True
        value=update.maintain(self.home,force=True,runner=runner,release_fetcher=self.fetch,downloader=self.download,compatibility=self.compatibility,architecture=lambda *_:None,now=1800000000)
        self.assertEqual(install.load_manifest(self.root),before);self.assertFalse(self.fetches);self.assertFalse(self.downloads);self.assertTrue(all(row['status']=='cancelled' for row in value['tools']))

    def test_state_and_config_symlinks_fail_closed(self):
        self.root.mkdir();(self.root/'settings.json').symlink_to(self.home/'elsewhere')
        with self.assertRaises(install.InstallError):update.status(self.home)

    def test_update_runner_bounds_output_timeout_and_does_not_echo_error_text(self):
        runner=update.UpdateRunner(timeout=2)
        output=runner.run(['/usr/bin/yes'],timeout=1,max_output=1024)
        self.assertTrue(output.truncated);self.assertLessEqual(len(output.stdout)+len(output.stderr),1024)
        runner=update.UpdateRunner(timeout=2);output=runner.run(['/bin/sleep','5'],timeout=.05)
        self.assertTrue(output.timed_out);self.assertLess(output.duration_ms,1000)

    def test_public_fetch_excludes_auth_and_bounds_requests(self):
        runner=Runner()
        with patch.object(runner,'run',return_value=ProcessResult(1)) as run,patch.dict(os.environ,{'GH_TOKEN':'fixture-secret','OPENAI_API_KEY':'fixture-secret'}):
            with self.assertRaises(install.InstallError):update.fetch_release('mise',runner)
        args,kw=run.call_args
        self.assertIn('https://api.github.com/repos/jdx/mise/releases/latest',args[0]);self.assertNotIn('GH_TOKEN',kw['env']);self.assertNotIn('OPENAI_API_KEY',kw['env']);self.assertNotIn('fixture-secret',str(args));self.assertEqual(kw['timeout'],14)

if __name__=='__main__':unittest.main()
