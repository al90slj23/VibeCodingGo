"""Idle work reductions preserve real socket delivery and published truth."""
import json
from pathlib import Path
import select
import socket
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from vibecue.desktop_attention import DesktopAttentionService
from vibecue.side_chats import SideChatService
from vibecue.providers import codex


class ResidentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.attention = DesktopAttentionService(self.home, self.home / 'codex')
        self.attention.monitor.clock = lambda: 100.0
        self.sides = SideChatService(self.home / 'codex', home=self.home,
                                    channel_factory=None, clock=lambda: 100.0)
        self.sides.next_scan = 130.0

    def attention_delay(self):
        with patch('vibecue.desktop_attention.time.monotonic', return_value=100.0):
            return self.attention._poll_timeout()

    def test_healthy_subscribed_stream_waits_without_ten_idle_wakeups(self):
        self.attention._threads = ['known']
        self.attention.projections['known'] = SimpleNamespace(revision=1)
        self.assertEqual(self.attention_delay(), 1.0)
        self.assertEqual(self.sides._poll_timeout(), 1.0)

    def test_unsettled_human_request_keeps_exact_settlement_deadline(self):
        request = SimpleNamespace(observed=99.8, emitted=False)
        self.attention.monitor.pending['request'] = request
        self.assertAlmostEqual(self.attention_delay(), .15)
        request.observed = 99.6
        self.assertEqual(self.attention_delay(), 0)
        request.emitted = True
        self.assertEqual(self.attention_delay(), 1.0)

    def test_stdio_owned_attention_does_not_spin_on_unemitted_duplicate(self):
        self.attention.monitor.pending['request'] = SimpleNamespace(observed=90.0, emitted=False)
        self.attention.monitor.emit_enabled = False
        self.assertEqual(self.attention_delay(), 1.0)

    def test_discovery_keeps_short_cadence_and_future_retry_deadline(self):
        self.attention._threads = ['new']
        self.assertEqual(self.attention_delay(), .1)
        self.attention.next_discovery['new'] = 100.4
        self.assertAlmostEqual(self.attention_delay(), .4)
        self.attention.quarantine_until['new'] = 400
        self.assertEqual(self.attention_delay(), 1.0)

    def test_initial_snapshot_deadline_is_not_extended_by_idle_wait(self):
        self.attention._threads = ['new', 'later']
        self.attention.projections['new'] = SimpleNamespace(revision=None)
        self.attention.subscribed_at['new'] = 95.2
        self.assertAlmostEqual(self.attention_delay(), .2)
        self.sides.projections['new'] = SimpleNamespace(revision=None, relation=None)
        self.sides.subscribed_at['new'] = 95.2
        self.assertAlmostEqual(self.sides._poll_timeout(), .2)

    def test_ready_socket_data_never_waits_for_idle_timeout(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close); self.addCleanup(server.close)
        server.send(b'protocol-event')
        ready, _, _ = select.select([client], [], [], self.attention_delay())
        self.assertEqual(ready, [client])
        self.assertEqual(client.recv(32), b'protocol-event')
        server.close()
        ready, _, _ = select.select([client], [], [], self.attention_delay())
        self.assertEqual(ready, [client])
        self.assertEqual(client.recv(1), b'')

    def test_sidechat_due_work_and_scan_deadlines_remain_bounded(self):
        self.sides.candidates['new'] = 100
        self.assertEqual(self.sides._poll_timeout(), .1)
        self.sides.next_attempt['new'] = 100.6
        self.assertAlmostEqual(self.sides._poll_timeout(), .6)
        self.sides.next_scan = 100.25
        self.assertAlmostEqual(self.sides._poll_timeout(), .25)

    def test_sidechat_cache_write_and_owner_check_deadlines_remain_live(self):
        self.sides.cache_dirty = True
        self.sides.next_cache_write = 100.3
        self.assertAlmostEqual(self.sides._poll_timeout(), .3)
        self.sides.cache_dirty = False
        self.sides.projections['known'] = SimpleNamespace(revision=1, relation={})
        self.sides.next_owner_check['known'] = 100.2
        self.assertAlmostEqual(self.sides._poll_timeout(), .2)

    def test_watcher_compact_utf8_preserves_models_numbers_and_invalid_source_surrogate(self):
        live = {'status': 'current', 'model': '\u4e2d\u6587\U0001f600\ud800', 'effort': 'ultra',
                'precise_tokens': 9000000000000001, 'service_tier': None}
        with patch.object(codex, 'DesktopAttentionService') as service, \
                patch.object(codex, 'codex_process_snapshot', return_value={'available': False, 'desktop': [], 'cli': []}):
            desktop = service.return_value
            desktop.snapshot.return_value = {'connected': False, 'capabilities': {'attention': False}}
            desktop.current_runtime.return_value = {}
            desktop.current_settings.return_value = {'fixture-thread': live}
            desktop.side_chats.return_value = {}
            watcher = codex.Watcher(self.home / 'codex', self.home / 'cue')
            watcher.write_status()
            path = watcher.vibecue_home / 'watcher-state.json'
            content = path.read_bytes()
            value = json.loads(content)
            self.assertEqual(value['current_model_settings']['fixture-thread'], live)
            self.assertIn('\u4e2d\u6587\U0001f600'.encode(), content)
            self.assertIn(b'\\ud800', content)
            self.assertLess(len(content), len(json.dumps(value).encode()))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            before = value['heartbeat']
            watcher.write_status()
            self.assertGreaterEqual(json.loads(path.read_bytes())['heartbeat'], before)


if __name__ == '__main__':
    unittest.main()
