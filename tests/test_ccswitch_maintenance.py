import fcntl
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibecue import ccswitch_maintenance as maintenance


def app_bundle(path, content=b'official executable', version='4.0.21', build='4.0.21'):
    path=Path(path)
    (path/'Contents/MacOS').mkdir(parents=True,exist_ok=True)
    (path/'Contents/Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier':maintenance.BUNDLE_ID,'CFBundleExecutable':'cc-switch',
        'CFBundleShortVersionString':version,'CFBundleVersion':build}))
    binary=path/'Contents/MacOS/cc-switch'
    binary.write_bytes(content);binary.chmod(0o700)
    return path


class NativeFixture:
    def __init__(self):
        self.running=False;self.frontmost=False;self.pid=12345
        self.quit_status='stopped';self.launch_statuses=[];self.calls=[]

    def __call__(self, action, payload=None):
        self.calls.append((action,payload))
        if action=='probe':
            return {'status':'running' if self.running else 'not_running',
                    'is_running':self.running,'is_frontmost':self.frontmost,
                    'pid':self.pid if self.running else None}
        if action=='quit':
            if self.quit_status=='stopped':self.running=False
            return {'status':self.quit_status,'is_running':self.running}
        if action=='relaunch':
            status=self.launch_statuses.pop(0) if self.launch_statuses else 'started'
            self.running=status in ('running','started')
            return {'status':status,'is_running':self.running}
        raise AssertionError('Unexpected native action')


class FixtureMaintenance(maintenance.Maintenance):
    def __init__(self, home, app, work, native_call, clock):
        super().__init__(home,app=app,native_call=native_call,clock=clock)
        self.work=work;self.work.mkdir(parents=True,exist_ok=True)
        self.trusted=True;self.idle=True;self.bad_signatures=set()
        self.build_calls=0;self.build_callback=None;self.built_cache=None
        self.export_ready=True;self.confirmations=[]

    def paths(self):return self.work
    def recognized(self, identity):return self.trusted
    def proxy_idle(self):return self.idle
    def verify_signature(self, app):
        if str(app) in self.bad_signatures:
            raise maintenance.MaintenanceError('bundle_signature_invalid')
    def confirm_export(self, after, binary_hash):
        self.confirmations.append((after,binary_hash));return self.export_ready
    def build(self, identity):
        self.build_calls+=1
        if self.build_callback:self.build_callback()
        return self.built_cache


class MaintenanceTests(unittest.TestCase):
    def test_native_helper_uses_the_renamed_application_without_shell_parsing(self):
        process = Mock(returncode=0, stdout=b'{"status":"not_running"}')
        with patch.object(maintenance.subprocess, 'run', return_value=process) as run:
            result = maintenance.native('probe', {'source': 'settings'})
        self.assertEqual(result, {'status': 'not_running'})
        self.assertEqual(run.call_args.args[0], [
            '/Applications/VibeCoding Go.app/Contents/MacOS/VCGo',
            '--ccswitch-system-action', 'probe'])
        self.assertEqual(json.loads(run.call_args.kwargs['input']), {'source': 'settings'})
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertEqual(maintenance.TEMPLATE, Path(
            '/Applications/VibeCoding Go.app/Contents/Resources/CCSwitchIntegration/vibecue_quota.rs'))

    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.user=self.root/'user';self.user.mkdir()
        home_patch=patch.object(maintenance.Path,'home',return_value=self.user)
        home_patch.start();self.addCleanup(home_patch.stop)
        marker=self.user/'.cc-switch/vibecue-quota-sharing.enabled'
        marker.parent.mkdir();marker.write_text('enabled');marker.chmod(0o600)
        self.app=app_bundle(self.root/'Applications/CC Switch.app')
        self.home=self.root/'VibeCue';self.native=NativeFixture();self.now=1000
        self.manager=FixtureMaintenance(self.home,self.app,self.root/'work',self.native,lambda:self.now)
        self.template=self.root/'bridge-template.rs'
        self.template.write_bytes(b'pub fn start() { original_export(); }\n#[cfg(test)]\nmod tests {}\n')
        maintenance.write_json(self.manager.config_file,{'enabled':True,'work_root':str(self.root/'work'),
                                                        'template_file':str(self.template)})
        self.identity=maintenance.inspect_app(self.app)
        self.cache=self.prepare_cache()
        self.manager.built_cache=self.cache

    def prepare_cache(self):
        directory=self.manager.work/'cache/version-one'
        app=app_bundle(directory/'CC Switch.app',b'patched executable')
        value=dict(maintenance.inspect_app(app),bridge_protocol=1,
                   upstream_binary_sha256=self.identity['binary_sha256'],
                   production_sha256=maintenance.production_hash(self.template),
                   source_commit='1'*40,adapter_manifest={'adapter_version':1,'module_sha256':'2'*64})
        maintenance.write_json(directory/'manifest.json',value)
        return dict(value,bundle_path=str(app))

    def test_healthy_ensure_never_spawns_or_operates_on_application(self):
        self.manager.update('healthy',healthy_binary_sha256=self.identity['binary_sha256'],
                            healthy_production_sha256=maintenance.production_hash(self.template))
        spawn=Mock()
        result=self.manager.ensure(spawn=spawn)
        self.assertEqual(result['phase'],'healthy')
        spawn.assert_not_called();self.assertEqual(self.native.calls,[])

    def test_detected_update_spawns_once_even_before_worker_takes_its_lock(self):
        spawn=Mock()
        self.manager.ensure(spawn=spawn)
        self.manager.ensure(spawn=spawn)
        self.assertEqual(spawn.call_count,1)

    def test_worker_lock_prevents_duplicate_spawn(self):
        self.home.mkdir(parents=True,exist_ok=True)
        with (self.home/'ccswitch-maintenance.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            spawn=Mock();self.manager.ensure(spawn=spawn);spawn.assert_not_called()

    def test_retry_backoff_expires_without_user_intervention(self):
        self.manager.update('waiting_retry',target_sha256=self.identity['binary_sha256'],next_retry_at=self.now+120)
        spawn=Mock();self.manager.ensure(spawn=spawn);spawn.assert_not_called()
        self.now+=121;self.manager.ensure(spawn=spawn);spawn.assert_called_once()

    def test_cache_requires_exact_version_build_architecture_and_source_fingerprint(self):
        manifest=Path(self.cache['bundle_path']).parent/'manifest.json'
        original=maintenance.read_json(manifest)
        self.assertIsNotNone(self.manager.cache_manifest(self.identity))
        variants={'version':'4.0.22','build':'different-build','architecture':'different-architecture',
                  'upstream_binary_sha256':'0'*64,'binary_sha256':'3'*64,'bridge_protocol':2}
        for key,value in variants.items():
            with self.subTest(key=key):
                changed=dict(original);changed[key]=value;maintenance.write_json(manifest,changed)
                self.assertIsNone(self.manager.cache_manifest(self.identity))
        maintenance.write_json(manifest,original)

    def test_older_cached_app_cannot_replace_newer_installed_version(self):
        old=app_bundle(self.root/'old/CC Switch.app',b'old patched version','4.0.20','4.0.20')
        candidate=dict(self.cache,bundle_path=str(old),binary_sha256=maintenance.inspect_app(old)['binary_sha256'])
        with self.assertRaises(maintenance.MaintenanceError):self.manager.install(self.identity,candidate)
        self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))
        self.assertEqual(self.native.calls,[])

    def test_foreground_or_busy_proxy_waits_without_asking_application_to_quit(self):
        for foreground,idle in ((True,True),(False,False)):
            with self.subTest(foreground=foreground,idle=idle):
                self.native.calls=[];self.native.running=True;self.native.frontmost=foreground;self.manager.idle=idle
                self.assertFalse(self.manager.install(self.identity,self.cache))
                self.assertEqual(self.manager.state()['phase'],'waiting_idle')
                self.assertEqual([action for action,_ in self.native.calls],['probe'])
                self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))

    def test_changed_app_during_build_is_not_replaced_or_downgraded(self):
        self.manager.build_callback=lambda:app_bundle(self.app,b'new official update','4.0.22','4.0.22')
        with patch.object(self.manager,'cache_manifest',return_value=None):result=self.manager.repair()
        self.assertEqual(self.manager.build_calls,1)
        self.assertEqual(result['phase'],'detected')
        self.assertEqual(maintenance.inspect_app(self.app)['version'],'4.0.22')
        self.assertEqual(self.native.calls,[])

    def test_update_at_the_final_rename_is_restored_instead_of_overwritten(self):
        original_rename=Path.rename
        changed=[]
        def update_before_move(path, destination):
            if path==self.app and Path(destination).name.startswith('.CCSwitch-vibecue-previous-'):
                app_bundle(self.app,b'newer official executable','4.0.22','4.0.22')
                changed.append(True)
            return original_rename(path,destination)
        with patch.object(Path,'rename',update_before_move):
            with self.assertRaises(maintenance.MaintenanceError) as caught:
                self.manager.install(self.identity,self.cache)
        self.assertEqual(changed,[True])
        self.assertEqual(caught.exception.code,'app_changed_at_swap')
        self.assertEqual(maintenance.inspect_app(self.app)['version'],'4.0.22')
        self.assertEqual((self.app/'Contents/MacOS/cc-switch').read_bytes(),b'newer official executable')
        self.assertFalse(list(self.app.parent.glob('.CCSwitch-vibecue-stage-*.app')))
        self.assertNotIn('quit',[action for action,_ in self.native.calls])

    def test_a_changed_production_template_invalidates_an_old_cached_build(self):
        self.assertIsNotNone(self.manager.cache_manifest(self.identity))
        self.template.write_bytes(self.template.read_bytes().replace(b'original_export()',b'new_export()'))
        self.assertIsNone(self.manager.cache_manifest(self.identity))

    def test_test_only_template_changes_do_not_trigger_a_production_rebuild(self):
        before=maintenance.production_hash(self.template)
        self.template.write_bytes(self.template.read_bytes().replace(b'mod tests {}',b'mod tests { fn new_test() {} }'))
        self.assertEqual(maintenance.production_hash(self.template),before)
        self.assertIsNotNone(self.manager.cache_manifest(self.identity))

    def test_healthy_old_template_is_detected_and_rebuilt_automatically(self):
        self.manager.update('healthy',healthy_binary_sha256=self.identity['binary_sha256'],
                            healthy_production_sha256=maintenance.production_hash(self.template))
        self.template.write_bytes(self.template.read_bytes().replace(b'original_export()',b'new_export()'))
        self.assertEqual(self.manager.status()['phase'],'detected')
        spawn=Mock();result=self.manager.ensure(spawn=spawn)
        spawn.assert_called_once();self.assertEqual(result['phase'],'detected')
        self.assertEqual(self.native.calls,[])

    def test_revoking_the_sharing_marker_stops_worker_and_install_actions(self):
        marker=self.user/'.cc-switch/vibecue-quota-sharing.enabled';marker.unlink()
        self.native.running=True
        self.assertEqual(self.manager.repair()['phase'],'disabled')
        self.assertFalse(self.manager.install(self.identity,self.cache))
        self.assertEqual(self.native.calls,[])
        self.assertEqual(self.manager.build_calls,0)
        self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))

    def test_revoking_sharing_while_staging_never_requests_exit_or_replaces_app(self):
        self.native.running=True
        original_copytree=maintenance.shutil.copytree
        marker=self.user/'.cc-switch/vibecue-quota-sharing.enabled'
        def stage_then_revoke(source,destination,*args,**kwargs):
            result=original_copytree(source,destination,*args,**kwargs)
            if Path(destination).name.startswith('.CCSwitch-vibecue-stage-'):
                marker.unlink()
            return result
        with patch.object(maintenance.shutil,'copytree',side_effect=stage_then_revoke):
            self.assertFalse(self.manager.install(self.identity,self.cache))
        self.assertNotIn('quit',[action for action,_ in self.native.calls])
        self.assertNotIn('relaunch',[action for action,_ in self.native.calls])
        self.assertEqual(self.manager.state()['phase'],'disabled')
        self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))
        self.assertFalse(list(self.app.parent.glob('.CCSwitch-vibecue-stage-*.app')))

    def test_declined_normal_exit_never_swaps_or_force_kills(self):
        self.native.running=True;self.native.quit_status='waiting_exit'
        self.assertFalse(self.manager.install(self.identity,self.cache))
        self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))
        self.assertEqual([action for action,_ in self.native.calls],['probe','quit'])
        self.assertEqual(self.native.calls[1][1],{'expected_pid':self.native.pid})
        self.assertFalse(list(self.app.parent.glob('.CCSwitch-vibecue-stage-*.app')))

    def test_repair_uses_new_inode_keeps_original_backup_and_confirms_export(self):
        self.native.running=True
        before=(self.app/'Contents/MacOS/cc-switch').stat().st_ino
        self.assertTrue(self.manager.install(self.identity,self.cache))
        self.assertNotEqual((self.app/'Contents/MacOS/cc-switch').stat().st_ino,before)
        backups=list((self.home/'backups').glob('ccswitch-before-auto-*.app'))
        self.assertEqual(len(backups),1)
        self.assertEqual(maintenance.inspect_app(backups[0])['binary_sha256'],self.identity['binary_sha256'])
        self.assertEqual(maintenance.inspect_app(self.app)['binary_sha256'],self.cache['binary_sha256'])
        self.assertEqual(self.manager.state()['phase'],'healthy')
        self.assertEqual(self.confirmations(),[(self.now,self.cache['binary_sha256'])])
        self.assertEqual([action for action,_ in self.native.calls],['probe','quit','probe','relaunch'])

    def confirmations(self):return self.manager.confirmations

    def test_launch_failure_restores_verified_original_and_relaunches_it(self):
        self.native.running=True;self.native.launch_statuses=['unavailable','started']
        with self.assertRaises(maintenance.MaintenanceError) as caught:self.manager.install(self.identity,self.cache)
        self.assertEqual(caught.exception.code,'restart_failed_rolled_back')
        self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))
        self.assertEqual([action for action,_ in self.native.calls].count('relaunch'),2)
        self.assertTrue(self.native.running)

    def test_failed_export_does_not_rollback_under_a_still_running_replacement(self):
        self.native.running=True;self.manager.export_ready=False
        with self.assertRaises(maintenance.MaintenanceError):self.manager.install(self.identity,self.cache)
        calls=[action for action,_ in self.native.calls]
        self.assertGreaterEqual(calls.count('quit'),2)
        self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))

    def test_bad_cache_signature_is_rejected_before_any_application_action(self):
        self.manager.bad_signatures.add(self.cache['bundle_path'])
        with self.assertRaises(maintenance.MaintenanceError):self.manager.install(self.identity,self.cache)
        self.assertEqual(self.native.calls,[])
        self.assertTrue(maintenance.same_app(self.identity,maintenance.inspect_app(self.app)))

    def test_unrecognized_upstream_app_does_not_build_or_replace(self):
        self.manager.trusted=False
        result=self.manager.repair()
        self.assertEqual(result['phase'],'incompatible')
        self.assertEqual(result['error_code'],'installed_app_not_recognized')
        self.assertEqual(self.manager.build_calls,0)
        self.assertEqual(self.native.calls,[])

    def test_official_recognition_checks_known_team_and_strict_signature(self):
        response=Mock(returncode=0,stderr='TeamIdentifier=UNKNOWN\n')
        with patch.object(self.manager,'cache_manifest',return_value=None),patch.object(maintenance.subprocess,'run',return_value=response):
            self.assertFalse(maintenance.Maintenance.recognized(self.manager,self.identity))
            response.stderr='TeamIdentifier='+maintenance.OFFICIAL_TEAM+'\n'
            self.assertTrue(maintenance.Maintenance.recognized(self.manager,self.identity))
            self.manager.bad_signatures.add(str(self.app))
            self.assertFalse(maintenance.Maintenance.recognized(self.manager,self.identity))

    def test_state_and_config_are_private_and_public_files_not_trusted(self):
        self.manager.update('waiting_retry',error_code='temporary_failure')
        for path in (self.manager.state_file,self.manager.config_file):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode),0o600)
        self.manager.state_file.chmod(0o644)
        self.assertEqual(self.manager.state(),{})

    def test_build_environment_does_not_inherit_credentials(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'private-value','GH_TOKEN':'private-value','AWS_SECRET_ACCESS_KEY':'private-value'}):
            environment=self.manager.environment(self.manager.work)
        self.assertFalse(any('private-value'==value for value in environment.values()))
        self.assertEqual(environment['TMPDIR'],str(self.manager.work/'tmp'))
        self.assertEqual(environment['CARGO_TARGET_DIR'],str(self.manager.work/'target'))


class ArchiveTests(unittest.TestCase):
    def archive(self,name,kind=tarfile.REGTYPE):
        output=io.BytesIO()
        with tarfile.open(fileobj=output,mode='w:gz') as archive:
            item=tarfile.TarInfo(name);item.type=kind
            if kind==tarfile.SYMTYPE:item.linkname='../../outside'
            if kind==tarfile.REGTYPE:
                body=b'public source';item.size=len(body);archive.addfile(item,io.BytesIO(body))
            else:archive.addfile(item)
        return output.getvalue()

    def test_traversal_absolute_and_symlink_archives_are_rejected(self):
        for name,kind in [('repo/../../outside',tarfile.REGTYPE),('/absolute/outside',tarfile.REGTYPE),
                          ('repo/link',tarfile.SYMTYPE)]:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as temporary:
                fetcher=maintenance.OfficialFetch()
                with patch.object(fetcher,'request',return_value=self.archive(name,kind)):
                    with self.assertRaises(maintenance.MaintenanceError) as caught:
                        fetcher.source('1'*40,Path(temporary)/'source')
                self.assertEqual(caught.exception.code,'source_archive_invalid')
                self.assertFalse((Path(temporary)/'outside').exists())

    def test_exact_official_commit_extracts_regular_source_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination=Path(temporary)/'source';fetcher=maintenance.OfficialFetch()
            with patch.object(fetcher,'request',return_value=self.archive('repo/src/file.rs')) as request:
                fetcher.source('1'*40,destination)
            self.assertEqual((destination/'src/file.rs').read_bytes(),b'public source')
            self.assertIn('/farion1231/cc-switch/tar.gz/'+'1'*40,request.call_args[0][0])
            self.assertFalse(list(Path(temporary).glob('*.tar.gz')))

    def test_unrecognized_source_host_never_makes_a_network_request(self):
        with patch.object(maintenance.urllib.request,'urlopen') as request:
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.OfficialFetch().request('https://untrusted.invalid/source.tar.gz',100)
        request.assert_not_called()


if __name__=='__main__':unittest.main()
