import fcntl
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue import model_queue as queue
from vibecue.codex_control import ControlError


class DeferredModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def rows(self):
        return json.loads((self.home / 'codex-model-queue.json').read_text())['records']

    def enqueue(self, identifier='one', **params):
        return queue.enqueue(identifier, params or {'effort': 'max'}, self.home)

    def test_pending_intent_survives_restart_and_is_not_claimed_as_applied(self):
        result = self.enqueue()
        self.assertEqual(result['status'], 'queued'); self.assertFalse(result['verified'])
        self.assertEqual(self.rows()[0]['params'], {'effort': 'max'})
        self.assertEqual((self.home / 'codex-model-queue.json').stat().st_mode & 0o777, 0o600)

    def test_only_selected_pending_intent_is_admitted_and_submission_is_recorded_first(self):
        self.enqueue(); self.enqueue('two')
        with patch('vibecue.desktop_control.run') as apply:
            self.assertEqual(queue.drain(['other'], self.home)['results'], [])
            apply.assert_not_called()
        def perform(operation, params, **kwargs):
            self.assertEqual(self.rows()[1]['status'], 'submitting')
            self.assertEqual(operation, 'apply'); self.assertEqual(params, {'thread_ids': ['two'], 'effort': 'max'})
            self.assertTrue(kwargs['queued_execution']); self.assertNotIn('defer_unloaded', params)
            return {'results': [{'thread_id': 'two', 'status': 'verified', 'verified': True}]}
        with patch('vibecue.desktop_control.run', side_effect=perform) as apply:
            result = queue.drain(['two'], self.home)
        self.assertEqual(result['results'][0]['status'], 'verified')
        self.assertEqual([row['status'] for row in self.rows()], ['queued', 'verified'])
        self.assertEqual(apply.call_count, 1)

    def test_uncertain_result_is_never_replayed(self):
        self.enqueue()
        result = {'results': [{'thread_id': 'one', 'status': 'unverified', 'verified': False}]}
        with patch('vibecue.desktop_control.run', return_value=result) as apply:
            queue.drain(['one'], self.home); queue.drain(['one'], self.home)
        self.assertEqual(apply.call_count, 1); self.assertEqual(self.rows()[0]['status'], 'unverified')

    def test_killed_worker_leaves_uncertain_receipt_and_never_replays(self):
        self.enqueue()
        with patch('vibecue.desktop_control.run', side_effect=SystemExit('worker stopped')):
            with self.assertRaises(SystemExit): queue.drain(['one'], self.home)
        self.assertEqual(self.rows()[0]['status'], 'submitting')
        self.assertEqual(self.rows()[0]['result']['status'], 'unverified')
        with patch('vibecue.desktop_control.run') as apply:
            queue.drain(['one'], self.home)
        apply.assert_not_called()

    def test_unavailable_owner_waits_without_navigation_and_backs_off(self):
        self.enqueue()
        result = {'results': [{'thread_id': 'one', 'status': 'failed', 'error_kind': 'not_loaded'}]}
        with patch('vibecue.desktop_control.run', return_value=result) as apply:
            queue.drain(['one'], self.home); queue.drain(['one'], self.home)
        self.assertEqual(apply.call_count, 1)
        self.assertEqual(self.rows()[0]['status'], 'queued')
        self.assertGreater(self.rows()[0]['retry_at'], time.time())

    def test_repeated_prewrite_unavailability_uses_bounded_exponential_backoff(self):
        self.enqueue()
        result = {'results': [{'thread_id': 'one', 'status': 'failed', 'error_kind': 'not_loaded'}]}
        with patch('vibecue.desktop_control.run', return_value=result):
            for attempt in range(1, 7):
                with queue._locked(self.home) as rows: rows['one']['retry_at'] = 0
                queue.drain(['one'], self.home)
                row = self.rows()[0]
                self.assertEqual(row['attempts'], attempt)
                self.assertGreater(row['retry_at'] - time.time(), min(600, 30 * 2 ** (attempt-1)) - 2)
                self.assertLessEqual(row['retry_at'] - time.time(), 600)

    def test_a_later_explicit_request_supersedes_the_old_queue(self):
        old = self.enqueue(); new = self.enqueue(model='model-one', effort='high')
        self.assertNotEqual(old['queue_id'], new['queue_id'])
        queue.cancel([{'thread_id': 'one', 'queue_id': old['queue_id']}], self.home)
        self.assertEqual(self.rows()[0]['status'], 'queued')
        queue.supersede('one', self.home)
        self.assertEqual(self.rows(), [])

    def test_cancel_stops_pending_but_does_not_claim_to_undo_an_inflight_update(self):
        pending = self.enqueue()
        queue.cancel([{'thread_id': 'one', 'queue_id': pending['queue_id']}], self.home)
        self.assertEqual(self.rows()[0]['status'], 'failed')
        with patch('vibecue.desktop_control.run') as apply: queue.drain(['one'], self.home)
        apply.assert_not_called()

    def test_foreground_batch_lock_prevents_concurrent_background_updates(self):
        self.enqueue()
        with (self.home / 'codex-batch.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('vibecue.desktop_control.run') as apply:
                self.assertTrue(queue.drain(['one'], self.home)['busy'])
            apply.assert_not_called()
        self.assertEqual(self.rows()[0]['status'], 'queued')

    def test_corrupt_or_expanded_queue_never_reaches_the_native_writer(self):
        self.enqueue()
        path = self.home / 'codex-model-queue.json'
        value = json.loads(path.read_text()); value['records'][0]['params']['thread_ids'] = ['another']
        path.write_text(json.dumps(value))
        with patch('vibecue.desktop_control.run') as apply:
            with self.assertRaises(ControlError): queue.drain(['one'], self.home)
        apply.assert_not_called()
        with self.assertRaises(ControlError): self.enqueue(input='PRIVATE PROMPT')
