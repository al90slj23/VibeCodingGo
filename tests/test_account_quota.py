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
                                   quota_buckets, quota_window, read_current)


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
