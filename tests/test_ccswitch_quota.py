import json
import os
from pathlib import Path
import tempfile
import unittest

from vibecue.ccswitch_quota import SOURCE, read_all_accounts


def record(identifier='workspace-one', email='one@example.com', **values):
    return dict({'ccswitch_account_id':'local-'+identifier,'account_id':identifier,
                 'email':email,'observed_at':1000,'success':True,
                 'tiers':[{'name':'seven_day','utilization':22,'resets_at':'1970-01-02T00:00:00Z'}]},**values)


class CCSwitchQuotaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.file=Path(self.tmp.name)/'vibecue-codex-quota.json'

    def write(self, rows=None, **values):
        raw=dict({'schema_version':1,'source':SOURCE,'generated_at':1000,
                  'accounts':[record(),record('workspace-two','two@example.com')] if rows is None else rows},**values)
        self.file.write_text(json.dumps(raw));self.file.chmod(0o600)

    def read(self,now=1001):return read_all_accounts(self.file,now=now)

    def test_two_accounts_read_without_any_authorization_file_or_network(self):
        self.write();value=self.read()
        self.assertEqual(value['configured_count'],2)
        self.assertEqual(len(value['accounts']),2)
        self.assertEqual(value['errors'],[])
        self.assertEqual(value['bridge_status'],'ready')
        self.assertEqual(value['accounts'][0]['buckets'][0]['windows'][0]['remaining_percent'],78)

    def test_missing_bridge_is_distinct_from_no_configured_accounts(self):
        self.assertEqual(self.read()['bridge_status'],'unavailable')
        self.write([])
        self.assertEqual(self.read()['bridge_status'],'ready')
        self.assertEqual(self.read()['configured_count'],0)

    def test_one_failed_account_does_not_discard_other_current_account(self):
        self.write([record(),record('two',success=False,error_code='authorization_unavailable')])
        value=self.read();self.assertEqual(len(value['accounts']),1)
        self.assertEqual(len(value['errors']),1)
        self.assertEqual(len(value['identities']),2)

    def test_stale_whole_snapshot_is_never_live(self):
        self.write();value=self.read(now=1121)
        self.assertEqual(value['bridge_status'],'stale')
        self.assertEqual(value['accounts'],[])
        self.assertEqual(len(value['errors']),2)
        self.assertEqual(len(value['history']),2)
        self.assertTrue(all(row['is_live'] is False and row['status']=='last_read' for row in value['history']))
        self.assertEqual(value['history'][0]['buckets'][0]['windows'][0]['remaining_percent'],78)

    def test_old_individual_record_is_not_fresh_under_new_export_time(self):
        self.write([record(observed_at=800)])
        self.assertEqual(self.read()['accounts'],[])
        self.assertEqual(self.read()['history'][0]['observed_at'],800)

    def test_failed_source_row_never_becomes_historical_success(self):
        self.write([record(success=False)])
        self.assertEqual(self.read(now=1121)['history'],[])

    def test_future_individual_row_has_no_historical_success(self):
        self.write([record(observed_at=2000)])
        self.assertEqual(self.read()['history'],[])

    def test_record_after_timeout_has_only_safe_error(self):
        self.write([record(success=False,error_code='query_timeout',error='Bearer secret-token')])
        value=self.read();self.assertIn('超时',value['errors'][0]['message'])
        self.assertNotIn('secret-token',json.dumps(value))

    def test_unknown_fields_and_tokens_never_propagate(self):
        self.write([record(access_token='access-secret',refresh_token='refresh-secret',id_token='identity-secret')],token='top-secret')
        value=self.read()
        self.assertFalse(any(s in json.dumps(value) for s in ['access-secret','refresh-secret','identity-secret','top-secret']))

    def test_missing_used_percent_is_unknown_not_zero(self):
        self.write([record(tiers=[{'name':'seven_day'}])])
        self.assertEqual(self.read()['accounts'],[])

    def test_zero_usage_hides_placeholder_reset(self):
        self.write([record(tiers=[{'name':'seven_day','utilization':0,'resets_at':'1970-01-02T00:00:00Z'}])])
        window=self.read()['accounts'][0]['buckets'][0]['windows'][0]
        self.assertEqual(window['remaining_percent'],100)
        self.assertIsNone(window['resets_at'])
        self.assertEqual(window['reset_status'],'not_started')

    def test_unknown_duration_does_not_become_weekly(self):
        self.write([record(tiers=[{'name':'unknown','utilization':10}])])
        window=self.read()['accounts'][0]['buckets'][0]['windows'][0]
        self.assertEqual(window['kind'],'other');self.assertIsNone(window['window_minutes'])

    def test_unowned_world_readable_export_and_symlinks_are_rejected(self):
        self.write();self.file.chmod(0o644)
        self.assertEqual(self.read()['bridge_status'],'invalid')
        self.file.chmod(0o600);link=self.file.with_name('link.json');link.symlink_to(self.file)
        self.assertEqual(read_all_accounts(link,now=1001)['bridge_status'],'invalid')

    def test_invalid_top_level_and_future_timestamps_rejected(self):
        for raw in ['[]','null','"bad"','{']:
            self.file.write_text(raw);self.file.chmod(0o600)
            self.assertEqual(self.read()['bridge_status'],'invalid')
        self.write(generated_at=2000)
        self.assertEqual(self.read()['bridge_status'],'invalid')

    def test_same_local_authorization_id_is_deduplicated(self):
        self.write([record(),record()])
        self.assertEqual(len(self.read()['accounts']),1)

    def test_two_users_in_same_workspace_remain_separate_accounts(self):
        self.write([record('workspace-one','one@example.com',ccswitch_account_id='local-user-one'),
                    record('workspace-one','two@example.com',ccswitch_account_id='local-user-two')])
        value=self.read()
        self.assertEqual(value['configured_count'],2)
        self.assertEqual(len(value['accounts']),2)
        self.assertEqual({row['account_key'] for row in value['accounts']},
                         {'ccswitch:local-user-one','ccswitch:local-user-two'})
        self.assertTrue(all(row['account_id']=='workspace-one' for row in value['accounts']))
        self.assertTrue(all(row['identity_kind']=='ccswitch_account_id' for row in value['accounts']))

    def test_absent_local_id_falls_back_to_workspace_identity(self):
        self.write([record(ccswitch_account_id='')])
        value=self.read()['accounts'][0]
        self.assertEqual(value['account_key'],'account:workspace-one')
        self.assertEqual(value['identity_kind'],'account_id')

    def test_missing_backend_id_retains_local_identity_without_fake_quota(self):
        self.write([record(account_id='')]);value=self.read()
        self.assertEqual(value['configured_count'],1);self.assertEqual(value['accounts'],[])
        self.assertTrue(value['identities'][0]['account_key'].startswith('ccswitch:'))

    def test_invalid_numeric_values_not_accepted(self):
        for value in [None,True,-1,float('nan')]:
            self.write([record(tiers=[{'name':'seven_day','utilization':value}])])
            self.assertEqual(self.read()['accounts'],[])


if __name__=='__main__':unittest.main()
