import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue.providers.task_recovery import (TaskRecovery, codex_process_snapshot,
                                              dismiss, remember, safe_row)
from vibecue.providers.codex import ThreadMetadata, Watcher


def identity(name):
    return hashlib.sha256(name.encode()).hexdigest()


def runtime(name='A', started=1, available=True):
    return {'available': available, 'desktop': [{'id': identity(name), 'started_at': started}] if name else [], 'cli': []}


def task(work=100, activity=None, turn='turn-one'):
    return {'thread_id': 'thread-one', 'title': '需要继续的任务', 'provider': 'codex', 'turn_id': turn,
            'started_at': 90, 'last_activity': activity or work, 'last_work_at': work,
            'elapsed_seconds': 10, 'total_tokens': 12345, 'tokens_status': 'complete'}


def observation(row=None, state='running', **extra):
    value = {'thread_id': 'thread-one', 'task': row, 'state': state,
             'source_readable': True, 'watermark': (row or {}).get('last_activity', 0),
             'source_kind': 'desktop', 'transcript_path': '/private/tmp/recovery-fixture.jsonl'}
    value.update(extra)
    return value


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ledger = TaskRecovery(self.home, observer_started=100)

    def active(self):
        return self.ledger.reconcile([observation(task())], runtime(), now=101)

    def interrupt(self):
        self.active()
        return self.ledger.reconcile([observation(task())], runtime('B', started=110), now=120)

    def test_private_persistence_only_keeps_metadata_and_numeric_telemetry(self):
        raw = task(); raw.update(messages=[{'text': 'private body'}], last_message='private body', arbitrary_secret='never')
        active, interrupted = self.ledger.reconcile([observation(raw)], runtime(), now=101)
        self.assertEqual(len(active), 1); self.assertFalse(interrupted)
        text = (self.home / 'task-recovery.json').read_text()
        self.assertNotIn('private body', text); self.assertNotIn('arbitrary_secret', text)
        self.assertEqual((self.home / 'task-recovery.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('owner_ids', active[0]); self.assertNotIn('transcript_path', active[0])
        self.assertEqual(active[0]['total_tokens'], 12345)

    def test_new_process_does_not_revive_stranded_tail_and_duration_is_frozen(self):
        active, interrupted = self.interrupt()
        self.assertFalse(active); self.assertEqual(interrupted[0]['status'], 'interrupted')
        stamp, elapsed = interrupted[0]['interrupted_at'], interrupted[0]['elapsed_seconds']
        second = TaskRecovery(self.home, observer_started=130)
        active, interrupted = second.reconcile([observation(task())], runtime('B', started=110), now=150)
        self.assertFalse(active)
        self.assertEqual(interrupted[0]['interrupted_at'], stamp)
        self.assertEqual(interrupted[0]['elapsed_seconds'], elapsed)
        self.assertEqual(len(second.retained_metadata()), 1)

    def test_token_only_refresh_cannot_revive_interrupted_work(self):
        self.interrupt()
        active, interrupted = self.ledger.reconcile([observation(task(activity=140))], runtime('B', started=110), now=141)
        self.assertFalse(active); self.assertEqual(len(interrupted), 1)
        active, interrupted = self.ledger.reconcile([observation(task(work=145, activity=146))], runtime('B', started=110), now=147)
        self.assertEqual(len(active), 1); self.assertFalse(interrupted)

    def test_watcher_only_restart_preserves_same_live_owner(self):
        self.active()
        second = TaskRecovery(self.home, observer_started=130)
        active, interrupted = second.reconcile([observation(task())], runtime(), now=131)
        self.assertEqual(len(active), 1); self.assertFalse(interrupted)

    def test_old_watcher_snapshot_migrates_without_inventing_owner_continuity(self):
        (self.home / 'watcher-state.json').write_text(json.dumps({'running_tasks': [task()], 'cursors': {'thread-one': {'path': '/private/tmp/recovery-fixture.jsonl'}}}))
        migrated = TaskRecovery(self.home, observer_started=130)
        active, interrupted = migrated.reconcile([observation(task())], runtime(), now=131)
        self.assertFalse(active); self.assertEqual(interrupted[0]['status'], 'unconfirmed')
        self.assertEqual(interrupted[0]['reason'], 'previous_observer_lost')
        self.assertEqual(migrated.retained_metadata()[0]['transcript_path'], '/private/tmp/recovery-fixture.jsonl')

    def test_completion_clears_but_explicit_abort_remains_available_to_resume(self):
        self.active()
        active, interrupted = self.ledger.reconcile([observation(state='idle', terminal_at=110, terminal_kind='turn_aborted')], runtime(), now=111)
        self.assertFalse(active); self.assertEqual(interrupted[0]['reason'], 'previous_turn_aborted')
        self.assertEqual(interrupted[0]['interrupted_at'], 110)
        active, interrupted = self.ledger.reconcile([observation(state='idle', terminal_at=120, terminal_kind='task_complete')], runtime(), now=121)
        self.assertFalse(active); self.assertFalse(interrupted)

    def test_older_terminal_does_not_hide_a_new_running_turn(self):
        self.active()
        active, interrupted = self.ledger.reconcile([observation(task(work=120, turn='new-turn'), terminal_at=110, terminal_kind='turn_aborted')], runtime(), now=121)
        self.assertEqual(active[0]['turn_id'], 'new-turn'); self.assertFalse(interrupted)

    def test_known_waiting_is_not_mislabeled_interrupted(self):
        self.active()
        self.assertEqual(self.ledger.reconcile([observation(state='waiting', watermark=110)], runtime(), now=111), ([], []))
        self.active()
        self.assertEqual(self.ledger.reconcile([observation(task())], runtime(), now=112, waiting=['thread-one']), ([], []))

    def test_ttl_and_missing_source_keep_navigation_record(self):
        self.active()
        active, interrupted = self.ledger.reconcile([observation(state='running')], runtime(), now=200000)
        self.assertFalse(active); self.assertEqual(interrupted[0]['reason'], 'activity_expired')
        self.assertEqual(len(TaskRecovery(self.home).records), 1)

    def test_read_error_recovers_same_epoch_without_requiring_new_work(self):
        self.active()
        active, interrupted = self.ledger.reconcile([observation(source_readable=False)], runtime(), now=102)
        self.assertFalse(active); self.assertEqual(interrupted[0]['reason'], 'source_unavailable')
        active, interrupted = self.ledger.reconcile([observation(task())], runtime(), now=103)
        self.assertEqual(len(active), 1); self.assertFalse(interrupted)

    def test_dismissal_compare_and_tombstone_prevent_reappearing_old_tail(self):
        _, rows = self.interrupt(); row = rows[0]
        self.assertFalse(dismiss(self.home, row['thread_id'], 'wrong-turn', row['interrupted_at']))
        self.assertTrue(dismiss(self.home, row['thread_id'], row['turn_id'], row['interrupted_at']))
        self.assertEqual(self.ledger.reconcile([observation(task())], runtime('B', started=110), now=130), ([], []))
        restarted = TaskRecovery(self.home, observer_started=135)
        self.assertEqual(restarted.reconcile([observation(task())], runtime('B', started=110), now=136), ([], []))
        active, _ = restarted.reconcile([observation(task(work=140, turn='new-turn'))], runtime('B', started=110), now=141)
        self.assertEqual(active[0]['turn_id'], 'new-turn')

    def test_stale_dismissal_cannot_remove_a_resumed_task(self):
        _, rows = self.interrupt(); row = rows[0]
        self.assertTrue(dismiss(self.home, row['thread_id'], row['turn_id'], row['interrupted_at']))
        # Simulate a new status already committed by another watcher cycle.
        new = TaskRecovery(self.home, observer_started=120)
        new._apply_dismissals()
        new.reconcile([observation(task(work=140, turn='new-turn'))], runtime('B', started=110), now=141)
        self.assertFalse(dismiss(self.home, row['thread_id'], row['turn_id'], row['interrupted_at']))

    def test_inaccessible_receipt_directories_do_not_drop_retained_work(self):
        self.interrupt()
        with patch.object(Path, 'glob', side_effect=PermissionError(13, 'Permission denied')):
            self.ledger.apply_remembered()
            active, rows = self.ledger.reconcile([], runtime('B', started=110), now=130)
        self.assertFalse(active)
        self.assertEqual([row['thread_id'] for row in rows], ['thread-one'])
        self.assertIn('操作暂不可读', self.ledger.error)

    def test_token_only_refresh_does_not_undo_explicit_dismissal(self):
        _, rows = self.interrupt(); row = rows[0]
        self.assertTrue(dismiss(self.home, row['thread_id'], row['turn_id'], row['interrupted_at']))
        active, rows = self.ledger.reconcile([observation(task(activity=140))], runtime('B', started=110), now=141)
        self.assertFalse(active); self.assertFalse(rows)

    def test_explicit_historical_import_is_checked_against_new_tail(self):
        self.assertEqual(remember(self.home, [task()]), 1)
        self.ledger.apply_remembered()
        active, rows = self.ledger.reconcile([observation(state='idle', terminal_at=105, terminal_kind='turn_aborted')], runtime('B', started=110), now=120)
        self.assertFalse(active); self.assertEqual(rows[0]['reason'], 'previous_turn_aborted')
        self.assertEqual(remember(self.home, [task()]), 1)
        self.ledger.apply_remembered()
        active, rows = self.ledger.reconcile([observation(state='idle', terminal_at=130, terminal_kind='task_complete')], runtime('B', started=110), now=140)
        self.assertFalse(active); self.assertFalse(rows)

    def test_corrupt_ledger_is_preserved_and_old_snapshot_can_be_recovered(self):
        (self.home / 'task-recovery.json').write_text('broken{')
        (self.home / 'watcher-state.json').write_text(json.dumps({'running_tasks': [task()]}))
        recovered = TaskRecovery(self.home, observer_started=130)
        recovered.reconcile([], runtime(), now=131)
        backups = list(self.home.glob('task-recovery-corrupt-*.json'))
        self.assertEqual(len(backups), 1); self.assertEqual(backups[0].read_text(), 'broken{')
        self.assertEqual(len(recovered.records), 1)

    def test_invalid_ids_and_non_numeric_counts_are_not_durable(self):
        bad = task(); bad['thread_id'] = '../escape'
        self.assertIsNone(safe_row(bad))
        raw = task(); raw['total_tokens'] = True
        self.assertNotIn('total_tokens', safe_row(raw))

    def test_process_probe_never_reads_argv_and_distinguishes_unknown(self):
        output = '123 Tue Oct 27 01:02:03 2026 /Applications/ChatGPT.app/Contents/Resources/codex\n456 Tue Oct 27 01:02:03 2026 /opt/homebrew/bin/codex\n789 Tue Oct 27 01:02:03 2026 /usr/bin/python3\n'
        with patch('vibecue.providers.task_recovery.subprocess.run', return_value=subprocess.CompletedProcess([], 0, output, '')) as run:
            snapshot = codex_process_snapshot(200)
        self.assertTrue(snapshot['available']); self.assertEqual(len(snapshot['desktop']), 1); self.assertEqual(len(snapshot['cli']), 1)
        self.assertEqual(run.call_args.args[0], ['/bin/ps', '-axo', 'pid=,lstart=,comm='])
        with patch('vibecue.providers.task_recovery.subprocess.run', side_effect=PermissionError()):
            self.assertFalse(codex_process_snapshot()['available'])

    def test_code_mode_helper_does_not_change_task_owner_snapshot(self):
        owner = '123 Tue Oct 27 01:02:03 2026 /Applications/Codex.app/Contents/Resources/codex\n'
        helper = '456 Tue Oct 27 01:02:03 2026 /Applications/Codex.app/Contents/Resources/codex-code-mode-host\n'
        with patch('vibecue.providers.task_recovery.subprocess.run', return_value=subprocess.CompletedProcess([], 0, owner, '')):
            before = codex_process_snapshot(200)
        with patch('vibecue.providers.task_recovery.subprocess.run', return_value=subprocess.CompletedProcess([], 0, owner + helper, '')):
            after = codex_process_snapshot(200)
        self.assertEqual(before, after)
        self.assertEqual(len(after['desktop']), 1)
        self.assertEqual(after['cli'], [])

    def test_watcher_reopens_retained_path_outside_recent_metadata(self):
        path = self.home / 'retained.jsonl'; now = time.time()
        def event(kind, timestamp):
            return json.dumps({'type': 'event_msg', 'timestamp': timestamp, 'payload': {'type': kind, 'turn_id': 'turn-one'}}).encode() + b'\n'
        path.write_bytes(event('task_started', now - 5))
        data = task(work=now - 5); data.update(started_at=now - 5, transcript_path=str(path))
        remember(self.home, [data])
        watcher = Watcher(self.home / 'codex', self.home, emit=lambda **event: self.fail('historical alerts must not replay'))
        watcher.metadata = lambda: []
        watcher.running = True; watcher.discover(initial=True)
        self.assertIn('thread-one', watcher.watched)
        with patch('vibecue.providers.codex.codex_process_snapshot', return_value=runtime('B', started=now)):
            watcher.write_status()
        state = json.loads((self.home / 'watcher-state.json').read_text())
        self.assertEqual(len(state['interrupted_tasks']), 1)
        with path.open('ab') as stream:
            stream.write(event('task_complete', time.time() + .01))
        # Read directly to bypass the pre-launch event filter without making a
        # real watcher emit an intentional new completion notification.
        interpreter = watcher.watched['thread-one'][1]
        interpreter._observe_activity({'type': 'event_msg', 'timestamp': now + 1, 'payload': {'type': 'task_complete', 'turn_id': 'turn-one'}})
        with patch('vibecue.providers.codex.codex_process_snapshot', return_value=runtime('B', started=now)):
            watcher.write_status()
        self.assertEqual(json.loads((self.home / 'watcher-state.json').read_text())['interrupted_tasks'], [])


if __name__ == '__main__':
    unittest.main()
