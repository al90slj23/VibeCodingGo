import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue.account_quota import (CodexQuotaProcess, FRESH_SECONDS, QuotaError,
                                   QuotaStore, account_identity, build_snapshot,
                                   quota_buckets, quota_window, read_current,
                                   merge_shared_accounts)


def account(email='one@example.com', identifier=None):
    value={'account':{'type':'chatgpt','email':email,'planType':'pro'}}
    if identifier is not None:value['account']['accountId']=identifier
    return value


def limits(identifier='account-one', used=25, reset=2000):
    return {'accountId':identifier,'rateLimits':{'limitId':'codex','primary':{
        'usedPercent':used,'windowDurationMins':10080,'resetsAt':reset},'secondary':None}}


def snapshot(identifier='account-one', email='one@example.com', now=1000):
    return build_snapshot(account(email),limits(identifier),account(email),now)


class NormalizationTests(unittest.TestCase):
    def test_weekly_primary_is_not_mislabeled_as_five_hour(self):
        row=snapshot()
        window=row['buckets'][0]['windows'][0]
        self.assertEqual((window['kind'],window['remaining_percent'],window['window_minutes']),('weekly',75,10080))
        self.assertEqual(row['account_id'],'account-one')
        self.assertEqual(row['identity_kind'],'account_id')

    def test_multibucket_response_preferred_without_legacy_double_count(self):
        value=limits()
        value['rateLimitsByLimitId']={'codex':value['rateLimits'],'fast':{
            'primary':{'usedPercent':10,'windowDurationMins':300,'resetsAt':3000}}}
        rows=quota_buckets(value,1000)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['limit_id'],'codex')
        self.assertEqual(rows[1]['windows'][0]['kind'],'short')

    def test_null_windows_and_missing_percent_are_unknown_not_zero(self):
        for raw in (None,{}, {'usedPercent':None},{'usedPercent':True},{'usedPercent':float('nan')},{'usedPercent':-1}):
            with self.subTest(raw=raw):self.assertIsNone(quota_window(raw,1000))
        with self.assertRaises(QuotaError):quota_buckets({'rateLimits':{'primary':None}},1000)

    def test_zero_usage_has_no_moving_reset_placeholder(self):
        value=quota_window({'usedPercent':0,'windowDurationMins':10080,'resetsAt':9999},1000)
        self.assertEqual(value['remaining_percent'],100)
        self.assertEqual(value['reset_status'],'not_started')
        self.assertIsNone(value['resets_at'])

    def test_overused_percentage_does_not_create_negative_remaining(self):
        self.assertEqual(quota_window({'usedPercent':120},1000)['remaining_percent'],0)

    def test_account_switch_during_read_does_not_attach_quota_to_wrong_email(self):
        with self.assertRaises(QuotaError):build_snapshot(account(),limits(),account('other@example.com'),1000)

    def test_server_account_id_mismatch_rejected(self):
        with self.assertRaises(QuotaError):build_snapshot(account(identifier='wrong'),limits(),account(identifier='wrong'),1000)

    def test_email_fallback_does_not_fabricate_backend_account_id(self):
        row=build_snapshot(account(),limits(None),account(),1000)
        self.assertIsNone(row['account_id'])
        self.assertEqual(row['identity_kind'],'email')
        self.assertTrue(row['account_key'].startswith('email:'))
        with self.assertRaises(QuotaError):build_snapshot(account(None),limits(None),account(None),1000)

    def test_non_chatgpt_login_never_guesses_subscription_quota(self):
        for value in ({'account':None},{'account':{'type':'apiKey'}},{'account':{'type':'amazonBedrock'}}):
            with self.subTest(value=value),self.assertRaises(QuotaError):account_identity(value)

    def test_only_identity_and_numeric_quota_fields_survive(self):
        before=account();before['account'].update(accessToken='PRIVATE TOKEN',refresh_token='PRIVATE TOKEN')
        quota=limits();quota['secret']='PRIVATE TOKEN';quota['rateLimits']['secret']='PRIVATE TOKEN'
        row=build_snapshot(before,quota,before,1000)
        self.assertNotIn('PRIVATE',json.dumps(row))
        self.assertNotIn('token',json.dumps(row).lower())


class SharedAccountTests(unittest.TestCase):
    def current(self):
        row=snapshot()
        row.update(current_account=True,status='current')
        return {'accounts':[row],'current_account_key':row['account_key']}

    def shared(self, identifier='account-two', observed=1000):
        row=snapshot(identifier,'two@example.com',observed)
        row['source']='ccswitch_quota_bridge'
        return {'accounts':[row],'errors':[],'configured_count':2,'bridge_status':'ready'}

    def test_two_accounts_are_live_without_changing_current_account(self):
        result=merge_shared_accounts(self.current(),self.shared(),now=1001)
        self.assertTrue(result['multi_account_live'])
        self.assertEqual(result['live_account_count'],2)
        self.assertEqual(result['current_account_key'],'account:account-one')
        self.assertFalse(result['accounts'][1]['current_account'])
        self.assertTrue(result['accounts'][1]['is_live'])

    def test_same_service_account_is_deduplicated_across_sources(self):
        shared=self.shared('account-one',1001)
        shared['accounts'][0].update(account_key='ccswitch:local-one',ccswitch_account_id='local-one',email='one@example.com')
        result=merge_shared_accounts(self.current(),shared,now=1002)
        self.assertEqual(result['account_count'],1)
        self.assertTrue(result['accounts'][0]['current_account'])
        self.assertEqual(result['accounts'][0]['source'],'ccswitch_quota_bridge')

    def test_same_workspace_different_people_are_kept_separate(self):
        shared=self.shared('account-one',1001)
        shared['accounts'][0].update(account_key='ccswitch:local-two',ccswitch_account_id='local-two')
        result=merge_shared_accounts(self.current(),shared,now=1002)
        self.assertEqual(result['live_account_count'],2)
        self.assertEqual(result['account_count'],2)
        second=next(row for row in result['accounts'] if row['account_key']=='ccswitch:local-two')
        self.assertEqual(second['email'],'two@example.com')
        self.assertFalse(second['current_account'])

    def test_shared_failure_aliases_only_the_matching_workspace_and_user(self):
        shared={'accounts':[],'identities':[{'account_key':'ccswitch:local-one','account_id':'account-one','email':'one@example.com'}],
                'errors':[{'account_key':'ccswitch:local-one','message':'Read failed'}]}
        result=merge_shared_accounts(self.current(),shared,now=1001)
        self.assertEqual(result['account_count'],1)
        self.assertTrue(result['accounts'][0]['is_live'])

    def test_ccswitch_failure_does_not_hide_other_accounts_or_use_old_quota_as_live(self):
        shared=self.shared()
        shared['errors']=[{'account_key':'account:account-two','label':'two@example.com','message':'Read failed'}]
        result=merge_shared_accounts(self.current(),shared,now=1001)
        self.assertEqual(result['live_account_count'],1)
        second=next(row for row in result['accounts'] if row['account_key']=='account:account-two')
        self.assertFalse(second['is_live'])
        self.assertIn('read_error',second)

    def test_account_without_first_success_still_appears_with_reason(self):
        shared={'accounts':[],'errors':[{'account_key':'account:account-two','label':'two@example.com','message':'Read failed'}],
                'configured_count':2,'bridge_status':'ready'}
        result=merge_shared_accounts(self.current(),shared,now=1001)
        self.assertEqual(result['account_count'],2)
        second=result['accounts'][1]
        self.assertEqual((second['status'],second['buckets']),('unavailable',[]))

    def test_direct_current_read_can_remain_live_when_its_shared_read_fails(self):
        shared={'accounts':[],'errors':[{'account_key':'account:account-one','message':'Read failed'}]}
        result=merge_shared_accounts(self.current(),shared,now=1001)
        self.assertEqual(result['live_account_count'],1)

    def test_expired_source_and_elapsed_reset_never_show_live(self):
        for kind in ('expired','reset'):
            shared=self.shared()
            if kind=='expired':shared['accounts'][0]['expires_at']=900
            else:shared['accounts'][0]['buckets'][0]['windows'][0]['resets_at']=999
            result=merge_shared_accounts(self.current(),shared,now=1001)
            self.assertEqual(result['live_account_count'],1)
            self.assertFalse(result['multi_account_live'])

    def test_expired_export_retains_a_numeric_history_without_claiming_live(self):
        shared=self.shared()
        shared['history']=shared.pop('accounts')
        shared['errors']=[{'account_key':'account:account-two','message':'Snapshot expired'}]
        result=merge_shared_accounts(self.current(),shared,now=1201)
        row=next(row for row in result['accounts'] if row['account_key']=='account:account-two')
        self.assertFalse(row['is_live'])
        self.assertEqual(row['buckets'][0]['windows'][0]['remaining_percent'],75)

    def test_cli_status_reads_two_shared_accounts_without_running_a_login_helper(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            exported=root/'shared.json'
            now=int(time.time())
            rows=[{'ccswitch_account_id':'local-'+str(index),'account_id':'workspace-'+str(index),
                   'email':str(index)+'@example.com','success':True,'observed_at':now,
                   'tiers':[{'name':'seven_day','utilization':index*10,'resets_at':None}]}
                  for index in (1,2)]
            exported.write_text(json.dumps({'schema_version':1,'source':'ccswitch_quota_bridge',
                                            'generated_at':now,'accounts':rows}))
            exported.chmod(0o600)
            env=os.environ.copy()
            env.update(VIBECUE_HOME=str(root/'home'),VIBECUE_CC_SWITCH_QUOTA_FILE=str(exported))
            command=subprocess.run([sys.executable,'-m','vibecue.account_quota','status'],
                                   env=env,capture_output=True,text=True,timeout=10,check=True)
            result=json.loads(command.stdout)
            self.assertEqual(result['live_account_count'],2)
            self.assertTrue(result['multi_account_live'])
            self.assertIsNone(result['current_account_key'])
            self.assertTrue(all(row['source']=='ccswitch_quota_bridge' for row in result['accounts']))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.now=1000
        self.store=QuotaStore(Path(self.directory.name),clock=lambda:self.now)

    def test_two_account_snapshots_keep_noncurrent_explicitly_historical(self):
        self.store.refresh(reader=lambda:snapshot(now=self.now))
        self.now+=61
        data=self.store.refresh(reader=lambda:snapshot('account-two','two@example.com',self.now))
        self.assertEqual(data['account_count'],2)
        first,second=data['accounts']
        self.assertEqual((first['account_id'],first['status'],first['current_account']),('account-two','current',True))
        self.assertEqual((second['account_id'],second['status'],second['current_account']),('account-one','last_read',False))
        self.assertFalse(data['multi_account_live'])

    def test_refresh_failure_does_not_reuse_old_account_as_current(self):
        self.store.refresh(reader=lambda:snapshot(now=self.now))
        self.now+=61
        def fail():raise QuotaError('controlled failure')
        data=self.store.refresh(reader=fail)
        self.assertFalse(data['available']);self.assertIsNone(data['current_account_key'])
        self.assertEqual(data['accounts'][0]['status'],'last_read')

    def test_same_account_refresh_updates_in_place(self):
        self.store.refresh(reader=lambda:snapshot(now=self.now));self.now+=61
        data=self.store.refresh(reader=lambda:snapshot(now=self.now))
        self.assertEqual(data['account_count'],1)
        self.assertEqual(data['accounts'][0]['observed_at'],self.now)

    def test_refresh_is_throttled_and_manual_force_works(self):
        called=[]
        def reader():called.append(True);return snapshot(now=self.now)
        self.store.refresh(reader=reader);self.now+=10;self.store.refresh(reader=reader)
        self.assertEqual(len(called),1)
        self.store.refresh(reader=reader,force=True)
        self.assertEqual(len(called),2)

    def test_aged_and_reset_elapsed_snapshots_are_not_live(self):
        self.store.refresh(reader=lambda:snapshot(now=self.now))
        self.now+=FRESH_SECONDS+1
        self.assertEqual(self.store.status()['accounts'][0]['status'],'stale')
        self.now=1100
        self.store.refresh(force=True,reader=lambda:build_snapshot(account(),limits(reset=1101),account(),self.now))
        self.now=1102
        data=self.store.status()
        self.assertFalse(data['available'])
        self.assertEqual(data['accounts'][0]['buckets'][0]['windows'][0]['remaining_percent'],75)

    def test_cache_permissions_and_whitelist_are_private(self):
        self.store.refresh(reader=lambda:snapshot(now=self.now))
        self.assertEqual(stat.S_IMODE(self.store.file.stat().st_mode),0o600)
        data=json.loads(self.store.file.read_text());data['accounts'][0]['token']='PRIVATE'
        self.store.file.write_text(json.dumps(data))
        self.assertNotIn('PRIVATE',json.dumps(self.store.status()))

    def test_corrupt_cache_remains_unavailable_and_unmodified_on_status(self):
        self.store.file.write_text('broken')
        self.assertFalse(self.store.status()['available'])
        self.assertEqual(self.store.file.read_text(),'broken')

    def test_save_failure_does_not_keep_old_current_identity(self):
        self.store.refresh(reader=lambda:snapshot(now=self.now));self.now+=61
        with patch.object(self.store,'save',side_effect=OSError('private diagnostic')):
            data=self.store.refresh(reader=lambda:snapshot('account-two','two@example.com',self.now))
        self.assertFalse(data['available']);self.assertIsNone(data['current_account_key'])
        self.assertTrue(all(not row['current_account'] for row in data['accounts']))
        self.assertNotIn('private diagnostic',data['message'])


class ProtocolTests(unittest.TestCase):
    def test_read_current_uses_only_bracketed_account_reads_and_quota(self):
        calls=[]
        class Client:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,method,params):
                calls.append((method,params))
                return limits() if method=='account/rateLimits/read' else account()
        row=read_current(client_factory=Client,now=1000)
        self.assertEqual(row['account_id'],'account-one')
        self.assertEqual(calls,[('account/read',{'refreshToken':False}),('account/rateLimits/read',{}),('account/read',{'refreshToken':False})])

    def test_api_key_only_login_does_not_request_subscription_quota(self):
        calls=[]
        class Client:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def call(self,method,params):calls.append(method);return {'account':{'type':'apiKey'}}
        with self.assertRaises(QuotaError):read_current(client_factory=Client)
        self.assertEqual(calls,['account/read'])

    def test_unsafe_rpc_and_forced_refresh_are_rejected_before_writing(self):
        client=CodexQuotaProcess(executable='/fixture')
        for method,params in [('account/login/start',{}),('account/logout',{}),('thread/resume',{}),('turn/start',{}),('account/read',{'refreshToken':True}),('account/rateLimitResetCredit/consume',{})]:
            with self.subTest(method=method),self.assertRaises(QuotaError):client.call(method,params)

    def factory(self, code, processes):
        def start(arguments,**kwargs):
            self.assertEqual(arguments,['/fixture','app-server','--stdio'])
            self.assertEqual(kwargs['stderr'],subprocess.DEVNULL)
            self.assertTrue(kwargs['start_new_session'])
            self.assertNotIn('OPENAI_API_KEY',kwargs['env'])
            process=subprocess.Popen([sys.executable,'-u','-c',code],**kwargs);processes.append(process)
            return process
        return start

    def test_official_stdio_framing_and_cleanup(self):
        code='''import sys,json
for line in sys.stdin:
 value=json.loads(line)
 if 'id' in value:
  result={} if value['method']=='initialize' else {'account':{'type':'chatgpt','email':'one@example.com','planType':'pro'}}
  print(json.dumps({'id':value['id'],'result':result}),flush=True)
'''
        processes=[]
        with CodexQuotaProcess(executable='/fixture',process_factory=self.factory(code,processes)) as client:
            result=client.call('account/read',{'refreshToken':False})
            self.assertEqual(result['account']['type'],'chatgpt')
        self.assertIsNotNone(processes[0].poll())

    def test_timeout_closes_owned_helper_without_returning_raw_stderr(self):
        processes=[];start=time.monotonic()
        with self.assertRaisesRegex(QuotaError,'超时'):
            with CodexQuotaProcess(timeout=1,executable='/fixture',process_factory=self.factory('import time;time.sleep(20)',processes)):pass
        self.assertLess(time.monotonic()-start,3)
        self.assertIsNotNone(processes[0].poll())

    def test_unexpected_auth_request_is_not_answered_with_credentials(self):
        code='''import sys,json
for line in sys.stdin:
 value=json.loads(line)
 if value.get('method')=='initialize':print(json.dumps({'id':value['id'],'result':{}}),flush=True)
 elif value.get('method')=='account/read':print(json.dumps({'id':99,'method':'account/chatgptAuthTokens/refresh','params':{'secret':'PRIVATE'}}),flush=True)
'''
        processes=[]
        with self.assertRaisesRegex(QuotaError,'需要交互'):
            with CodexQuotaProcess(executable='/fixture',process_factory=self.factory(code,processes)) as client:
                client.call('account/read',{'refreshToken':False})
        self.assertIsNotNone(processes[0].poll())


if __name__=='__main__':unittest.main()
