import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibecue.ccswitch_quota import HISTORY_NAME, SOURCE, read_all_accounts


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
        for value in [None,True,-1,float('nan'),float('inf'),10**1000]:
            self.write([record(tiers=[{'name':'seven_day','utilization':value}])])
            self.assertEqual(self.read()['accounts'],[])


class QuotaHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.file=Path(self.tmp.name)/'export.json'
        self.home=Path(self.tmp.name)/'private-vibecue'

    write=CCSwitchQuotaTests.write

    def read(self,now=1001):return read_all_accounts(self.file,now=now,home=self.home)

    def cached(self):return json.loads((self.home/HISTORY_NAME).read_text())

    def test_omitting_home_remains_read_only(self):
        self.write();read_all_accounts(self.file,now=1001)
        self.assertEqual(list(Path(self.tmp.name).iterdir()),[self.file])

    def test_missing_export_keeps_both_last_successes_private_and_nonlive(self):
        self.write();self.read();self.file.unlink()
        value=self.read(now=1300)
        self.assertEqual(value['bridge_status'],'unavailable')
        self.assertEqual(value['accounts'],[])
        self.assertEqual(value['configured_count'],2)
        self.assertEqual(len(value['errors']),2)
        self.assertEqual(len(value['history']),2)
        self.assertTrue(all(row['is_live'] is False and row['status']=='last_read' for row in value['history']))
        self.assertEqual(value['history'][0]['observed_at'],1000)
        self.assertEqual(value['history'][0]['buckets'][0]['windows'][0]['remaining_percent'],78)
        cache=self.home/HISTORY_NAME
        self.assertEqual(cache.stat().st_mode & 0o777,0o600)
        self.assertEqual({path.name for path in self.home.iterdir()},{HISTORY_NAME,'ccswitch-quota-history.lock'})

    def test_failed_new_export_does_not_destroy_previous_numeric_success(self):
        self.write();self.read()
        self.write([record(observed_at=1060),record('workspace-two','two@example.com',success=False,tiers=[],error_code='query_timeout')],generated_at=1060)
        value=self.read(now=1061)
        self.assertEqual(len(value['accounts']),1)
        self.assertEqual(len(value['history']),1)
        self.assertEqual(value['history'][0]['email'],'two@example.com')
        self.assertEqual(value['history'][0]['observed_at'],1000)
        self.assertIn('超时',value['errors'][0]['message'])
        self.assertEqual(len(self.cached()['accounts']),2)

    def test_stale_success_can_be_retained_then_survive_export_removal(self):
        self.write();first=self.read(now=2000);self.file.unlink()
        second=self.read(now=2001)
        self.assertEqual(len(first['history']),2)
        self.assertEqual([row['observed_at'] for row in second['history']],[1000,1000])
        self.assertEqual(second['accounts'],[])

    def test_fresh_complete_configuration_deletion_is_persisted(self):
        self.write();self.read()
        self.write([record(observed_at=1060)],generated_at=1060)
        self.assertEqual(self.read(now=1061)['configured_count'],1)
        self.file.unlink()
        value=self.read(now=1300)
        self.assertEqual(value['configured_count'],1)
        self.assertEqual([row['email'] for row in value['history']],['one@example.com'])
        self.assertEqual(len(self.cached()['accounts']),1)

    def test_fresh_empty_configuration_removes_all_and_never_revives_them(self):
        self.write();self.read()
        self.write([],generated_at=1060);value=self.read(now=1061)
        self.assertTrue(value['configuration_complete'])
        self.assertEqual(self.cached()['accounts'],[])
        self.file.unlink();value=self.read(now=1300)
        self.assertEqual((value['configured_count'],value['history'],value['errors']),(0,[],[]))

    def test_partial_invalid_account_list_is_not_a_deletion_instruction(self):
        self.write();self.read()
        self.write([record(observed_at=1060),None],generated_at=1060)
        value=self.read(now=1061)
        self.assertFalse(value['configuration_complete'])
        self.assertEqual(value['configured_count'],2)
        self.assertEqual([row['email'] for row in value['history']],['two@example.com'])

    def test_unknown_export_schema_keeps_history_and_does_not_replace_cache(self):
        self.write();self.read()
        previous=(self.home/HISTORY_NAME).read_bytes()
        self.write([],schema_version=2,generated_at=1060)
        value=self.read(now=1061)
        self.assertEqual(value['bridge_status'],'invalid')
        self.assertEqual(len(value['history']),2)
        self.assertEqual((self.home/HISTORY_NAME).read_bytes(),previous)

    def test_oversized_numeric_field_is_invalid_but_keeps_saved_history(self):
        self.write();self.read()
        self.write([],generated_at=10**1000)
        value=self.read(now=1061)
        self.assertEqual(value['bridge_status'],'invalid')
        self.assertEqual(len(value['history']),2)

    def test_older_complete_configuration_cannot_resurrect_removed_account(self):
        self.write();self.read()
        self.write([record(observed_at=1060)],generated_at=1060);self.read(now=1061)
        self.write(generated_at=1000)
        value=self.read(now=1062)
        self.assertEqual(value['bridge_status'],'stale')
        self.assertEqual(value['accounts'],[])
        self.assertEqual(value['configured_count'],1)
        self.assertEqual([row['email'] for row in value['history']],['one@example.com'])
        self.assertEqual(self.cached()['configuration_observed_at'],1060)

    def test_older_individual_read_does_not_replace_newer_success(self):
        self.write([record(observed_at=1050)],generated_at=1050);self.read(now=1051)
        self.write([record(observed_at=1000,tiers=[{'name':'seven_day','utilization':99}])],generated_at=1060)
        value=self.read(now=1061)
        self.assertEqual(value['accounts'],[])
        self.assertEqual(value['history'][0]['observed_at'],1050)
        self.assertEqual(value['history'][0]['buckets'][0]['windows'][0]['remaining_percent'],78)
        self.assertEqual(self.cached()['accounts'][0]['observed_at'],1050)

    def test_rebound_local_identity_never_receives_the_previous_users_quota(self):
        self.write([record(ccswitch_account_id='same-local')]);self.read()
        self.write([record('another-workspace','other@example.com',ccswitch_account_id='same-local',
                           success=False,tiers=[],error_code='query_failed')],generated_at=1060)
        value=self.read(now=1061)
        self.assertEqual(value['history'],[])
        self.assertEqual(value['identities'][0]['email'],'other@example.com')
        self.assertEqual(self.cached()['accounts'],[])

    def test_same_workspace_users_remain_separate_in_durable_history(self):
        self.write([record('same-workspace','one@example.com',ccswitch_account_id='user-one'),
                    record('same-workspace','two@example.com',ccswitch_account_id='user-two')])
        self.read();self.file.unlink();value=self.read(now=1300)
        self.assertEqual({row['account_key'] for row in value['history']},{'ccswitch:user-one','ccswitch:user-two'})
        self.assertEqual(len(value['history']),2)

    def test_only_identity_and_numeric_whitelist_reaches_history_or_output(self):
        self.write([record(access_token='private-access',refresh_token='private-refresh',
                           tiers=[{'name':'seven_day','utilization':22,'token':'private-nested'}])],token='private-top')
        self.read();cache=self.home/HISTORY_NAME
        self.assertNotIn('private-',cache.read_text())
        data=json.loads(cache.read_text())
        data['secret']='private-top'
        data['accounts'][0]['access_token']='private-access'
        data['accounts'][0]['buckets'][0]['windows'][0]['remaining_percent']=999
        cache.write_text(json.dumps(data));cache.chmod(0o600);self.file.unlink()
        value=self.read(now=1300)
        self.assertNotIn('private-',json.dumps(value))
        self.assertEqual(value['history'][0]['buckets'][0]['windows'][0]['remaining_percent'],78)

    def test_symlink_history_is_not_followed_or_written_through(self):
        self.write();self.read()
        cache=self.home/HISTORY_NAME
        target=self.home/'unrelated.json';target.write_text('unrelated original')
        cache.unlink();cache.symlink_to(target)
        value=self.read()
        self.assertEqual(len(value['accounts']),2)
        self.assertEqual(target.read_text(),'unrelated original')
        self.assertFalse(cache.is_symlink())
        self.assertEqual(cache.stat().st_mode & 0o777,0o600)

    def test_failed_save_retains_current_and_historical_results_without_raw_error(self):
        self.write();self.read()
        self.write([record(observed_at=1060),record('workspace-two','two@example.com',success=False,tiers=[])],generated_at=1060)
        with patch('vibecue.ccswitch_quota._save_history',side_effect=OSError('secret raw path')):
            value=self.read(now=1061)
        self.assertEqual(len(value['accounts']),1)
        self.assertEqual(len(value['history']),1)
        self.assertIn('history_cache_error',value)
        self.assertNotIn('secret raw path',json.dumps(value))


if __name__=='__main__':unittest.main()
