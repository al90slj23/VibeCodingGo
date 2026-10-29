from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibecue.codex_control import ControlError
from vibecue.codex_desktop_ipc import DesktopOwnerUnavailable
from vibecue.side_chats import (NotSideChat, SideChatCandidates, SideChatProjection,
                               SideChatService, own_input_title, relationship_metadata)

CHILD = '01a185d1-3cb4-7440-9be2-2796b40984b1'
PARENT = '01a17d0b-9a04-72e2-a83e-85e4c73d9c72'
OTHER = '01a185c0-cfa5-7fd3-8dcd-d2a9c6c51fdd'
OWNER = '22222222-2222-4222-8222-222222222222'
CLIENT = '11111111-1111-4111-8111-111111111111'


def state(status='inProgress', kind='active', flags=None):
    return {'id': CHILD, 'hostId': 'local', 'ephemeral': True, 'sideConversation': True,
            'forkedFromId': PARENT, 'sideConversationParentNavigationPath': '/local/' + PARENT,
            'parentThreadId': None, 'threadSource': 'user', 'title': 'PRIVATE INHERITED PARENT TITLE',
            'resumeState': 'resumed', 'threadRuntimeStatus': {'type': kind, 'activeFlags': flags or []},
            'requests': [], 'turns': [{'turnId': 'own-turn', 'status': status, 'items': [{'text': 'PRIVATE BODY'}]}]}


def change(value=None, revision=1):
    return {'type': 'snapshot', 'revision': revision, 'conversationState': value or state()}


def titled_state(text='First own question'):
    value = state()
    value['turns'] = []
    value['turnHistory'] = {'kind': 'canonical', 'history': {'isComplete': True,
        'islands': [{'entries': [{'value': 'turn:own'}]}],
        'entitiesByKey': {'turn:own': {'turnId': 'own-turn', 'status': 'inProgress',
            'params': {'threadId': CHILD, 'input': [{'type': 'text', 'text': text}]},
            'items': [{'text': 'PRIVATE BODY'}]}}}}
    return value


class RelationshipTests(unittest.TestCase):
    def test_owner_flags_parent_and_path_are_required_not_nearby_log_events(self):
        relation = relationship_metadata(state(), CHILD)
        self.assertEqual(relation['parent_thread_id'], PARENT)
        self.assertEqual(relation['title'], '侧边聊天 · ' + CHILD[:8])
        for key, value in [('ephemeral', False), ('sideConversation', False), ('id', OTHER),
                           ('hostId', 'remote'), ('threadSource', 'prediction'),
                           ('parentThreadId', OTHER), ('source', {'subAgent': {'parentThreadId': OTHER}}),
                           ('source', {'subagent': {'parent_thread_id': OTHER}}),
                           ('forkedFromId', 'invalid-parent'),
                           ('sideConversationParentNavigationPath', '/local/' + OTHER),
                           ('sideConversationParentNavigationPath', 'https://example.com/local/' + PARENT),
                           ('sideConversationParentNavigationPath', '/local/' + PARENT + '#fragment')]:
            sample = state(); sample[key] = value
            self.assertIsNone(relationship_metadata(sample, CHILD), key)

    def test_safe_path_can_identify_parent_when_backend_omits_forked_id(self):
        sample = state(); sample.pop('forkedFromId')
        self.assertEqual(relationship_metadata(sample, CHILD)['parent_thread_id'], PARENT)
        sample.pop('sideConversationParentNavigationPath')
        self.assertIsNone(relationship_metadata(sample, CHILD))

    def test_title_only_from_complete_first_own_turn_and_redacts_secrets(self):
        value = titled_state('<environment_context>PRIVATE CONTEXT</environment_context>\n## My request:\nTitle api_key=sk-testcredentialvalue123456789 ' + '字' * 100)
        title = own_input_title(value, CHILD)
        self.assertNotIn('PRIVATE', title)
        self.assertNotIn('sk-test', title)
        self.assertLessEqual(len(title), 80)
        self.assertIn('[已隐藏凭据]', title)
        for edit in ('incomplete', 'multi', 'wrong-thread', 'missing-oldest', 'mixed-input', 'context'):
            sample = titled_state()
            history = sample['turnHistory']['history']; turn = history['entitiesByKey']['turn:own']
            if edit == 'incomplete': history['isComplete'] = False
            elif edit == 'multi': history['islands'].append({'entries': []})
            elif edit == 'wrong-thread': turn['params']['threadId'] = PARENT
            elif edit == 'missing-oldest': turn['itemsPagination'] = {'hasLoadedOldest': False}
            elif edit == 'mixed-input': turn['params']['input'].append({'type': 'image'})
            else: turn['params']['input'][0]['text'] = '<environment_context>PRIVATE</environment_context>'
            self.assertIsNone(own_input_title(sample, CHILD), edit)


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.projection = SideChatProjection(CHILD, OWNER, clock=lambda: self.now)

    def test_does_not_retain_body_or_parent_title_and_exports_live_metadata(self):
        self.projection.apply(change(titled_state('Own question')))
        self.assertEqual(self.projection.snapshot()['title'], 'Own question')
        retained = {'relation': self.projection.relation, 'requests': self.projection.attention.requests,
                    'runtime': self.projection.attention.runtime.data}
        self.assertNotIn('PRIVATE', json.dumps(retained))
        self.assertNotIn('Own question', json.dumps(self.projection.attention.runtime.data))
        self.assertTrue(self.projection.snapshot()['can_interact'])
        self.assertEqual(self.projection.snapshot()['status'], 'running')

    def test_rejects_other_kinds_before_projecting_or_reading_input(self):
        value = titled_state(); value['sideConversation'] = False
        with patch('vibecue.side_chats.own_input_title') as extract:
            with self.assertRaises(NotSideChat): self.projection.apply(change(value))
            extract.assert_not_called()
        self.assertIsNone(self.projection.relation)
        self.assertEqual(self.projection.attention.runtime.data, {})

    def test_wait_terminal_disconnect_and_expiration_remain_distinct(self):
        self.projection.apply(change(state(flags=['waitingOnUserInput'])))
        self.assertEqual(self.projection.snapshot()['status'], 'waiting')
        self.projection.apply(change(state('completed', 'idle'), 2))
        self.assertEqual(self.projection.snapshot()['status'], 'completed')
        self.assertEqual(self.projection.snapshot(False)['status'], 'disconnected')
        self.assertFalse(self.projection.snapshot(False)['can_open'])
        self.now += 31
        self.assertEqual(self.projection.snapshot()['status'], 'disconnected')
        self.assertEqual(self.projection.snapshot()['parent_thread_id'], PARENT)

    def test_identity_patch_invalidates_instead_of_reparenting(self):
        self.projection.apply(change())
        with self.assertRaises(ControlError):
            self.projection.apply({'type': 'patches', 'baseRevision': 1, 'revision': 2,
                                   'patches': [{'op': 'replace', 'path': ['forkedFromId'], 'value': OTHER}]})
        self.assertEqual(self.projection.snapshot()['parent_thread_id'], PARENT)
        self.assertFalse(self.projection.snapshot()['can_interact'])

    def test_cached_relation_requires_a_new_snapshot_and_cannot_be_reparented(self):
        self.projection.apply(change(titled_state('Own question')))
        restored = SideChatProjection(CHILD, OWNER, clock=lambda: self.now,
                                      previous=self.projection.snapshot())
        restored.mark_live()
        self.assertEqual(restored.snapshot()['status'], 'disconnected')
        self.assertFalse(restored.snapshot()['can_open'])
        changed = state()
        changed['forkedFromId'] = OTHER
        changed['sideConversationParentNavigationPath'] = '/local/' + OTHER
        with self.assertRaises(NotSideChat):
            restored.apply(change(changed))
        self.assertEqual(restored.snapshot()['parent_thread_id'], PARENT)
        restored.apply(change())
        self.assertEqual(restored.snapshot()['title'], 'Own question')
        self.assertTrue(restored.snapshot()['can_open'])


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = datetime(2026, 10, 29).timestamp()
        self.day = self.root / '2026/10/29'; self.day.mkdir(parents=True)
        self.path = self.day / 'codex-desktop-fixture-t0.log'
        self.scanner = SideChatCandidates(self.root, clock=lambda: self.now)

    def event(self, thread=CHILD, role='owner'):
        return f'2026-10-29T04:00:45.864Z info [electron-message-handler] thread_stream_role_changed conversationId={thread} role={role}\n'

    def test_only_fixed_identity_events_are_candidates_and_scan_is_incremental(self):
        self.path.write_text('PRIVATE BODY conversationId=' + OTHER + '\n' + self.event() + self.event(PARENT, 'follower'))
        self.assertEqual(self.scanner.scan(), [CHILD])
        self.assertEqual(self.scanner.scan(), [])
        with self.path.open('a') as stream: stream.write(self.event(OTHER))
        self.assertEqual(self.scanner.scan(), [OTHER])
        self.assertNotIn('PRIVATE', repr(vars(self.scanner)))

    def test_partial_lines_overlong_lines_and_bounded_tail_do_not_supply_false_ids(self):
        self.path.write_text('x' * 9000 + self.event(OTHER) + self.event())
        self.assertEqual(self.scanner.scan(), [CHILD])
        with self.path.open('a') as stream: stream.write(self.event(OTHER).rstrip('\n'))
        self.assertEqual(self.scanner.scan(), [])
        with self.path.open('a') as stream: stream.write('\n')
        self.assertEqual(self.scanner.scan(), [OTHER])
        self.scanner.positions.clear()
        with patch('vibecue.side_chats.MAX_LOG_TAIL', 300):
            self.assertLessEqual(len(self.scanner.scan()), 2)

    def test_custom_homes_never_scan_actual_library_logs(self):
        with patch('vibecue.side_chats.Path.home', return_value=self.root):
            service = SideChatService(self.root / '.codex', home=self.root / 'fixture', channel_factory=None)
            self.assertEqual(service.scanner.root, self.root / 'fixture/desktop-logs')
            service = SideChatService(self.root / 'fixture-codex', home=self.root / 'Library/Application Support/VibeCue', channel_factory=None)
            self.assertEqual(service.scanner.root, self.root / 'Library/Application Support/VibeCue/desktop-logs')

    def test_similar_body_text_and_symlink_are_not_log_identity_sources(self):
        self.path.write_text('PRIVATE text: ' + self.event(OTHER) + self.event())
        link = self.day / 'codex-desktop-symlink.log'; link.symlink_to(self.path)
        self.assertEqual(self.scanner.scan(), [CHILD])

    def test_previous_day_is_considered_under_the_same_file_budget(self):
        yesterday = self.root / '2026/10/28'; yesterday.mkdir(parents=True)
        old = yesterday / 'codex-desktop-yesterday.log'; old.write_text(self.event())
        self.path.write_text(self.event(OTHER))
        self.assertEqual(self.scanner.scan(), [CHILD, OTHER])  # Newest file's candidates have discovery priority.
        self.scanner.positions.clear()
        with patch('vibecue.side_chats.MAX_LOG_FILES', 1):
            self.assertEqual(len(self.scanner.scan()), 1)


class FakeChannel:
    client_id = CLIENT
    owner = OWNER
    socket = None
    unavailable = False
    def __init__(self): self.sent = []; self.calls = []
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def _send(self, value): self.sent.append(value)
    def discover(self):
        self.calls.append(self.thread_id)
        if self.unavailable: raise DesktopOwnerUnavailable('no current owner')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        scanner = type('Scanner', (), {'scan': lambda _: [CHILD]})()
        self.service = SideChatService('/fixture', home='/fixture', channel_factory=None, scanner=scanner, clock=lambda: self.now)
        self.service.channel = FakeChannel(); self.service.connected = True

    def message(self, value=None, revision=1, **replace):
        result = {'type': 'broadcast', 'method': 'thread-stream-state-changed', 'version': 11,
                  'sourceClientId': OWNER, 'targetClientIds': [CLIENT],
                  'params': {'hostId': 'local', 'conversationId': CHILD, 'change': change(value, revision)}}
        result.update(replace)
        return result

    def test_candidate_not_exposed_until_verified_and_cross_owner_ignored(self):
        self.service._discover()
        self.assertEqual(self.service.snapshot()['by_parent'], {})
        self.service._on_message(self.message(sourceClientId='wrong-owner'))
        self.assertEqual(self.service.snapshot()['by_parent'], {})
        self.service._on_message(self.message(targetClientIds=['wrong-client']))
        self.assertEqual(self.service.snapshot()['by_parent'], {})
        self.service._on_message(self.message())
        row = self.service.snapshot()['by_parent'][PARENT][0]
        self.assertEqual(row['thread_id'], CHILD)
        self.assertTrue(row['can_interact'])
        self.assertEqual(len(self.service.channel.calls), 1)

    def test_owner_unavailable_retains_relation_without_inventing_completion_or_archive(self):
        self.service._discover(); self.service._on_message(self.message())
        self.now += 16; self.service.channel.unavailable = True
        self.service._discover()
        row = self.service.snapshot()['by_parent'][PARENT][0]
        self.assertEqual(row['status'], 'disconnected')
        self.assertFalse(row['can_open'])
        self.assertNotIn(CHILD, self.service.projections)
        self.assertEqual(len(self.service.known), 1)

    def test_lower_revision_requires_fresh_follow_then_recovers_without_candidate_cooldown(self):
        self.service._discover()
        self.service._on_message(self.message(titled_state('Verified own title'), revision=90))
        original = self.service.projections[CHILD]
        self.service._on_message(self.message(state('completed', 'idle'), revision=1))
        self.service._on_message(self.message(state('completed', 'idle'), revision=91))  # Queued old stream.
        self.assertEqual(original.revision, 90)
        row = self.service.snapshot()['by_parent'][PARENT][0]
        self.assertEqual(row['status'], 'disconnected')
        self.assertFalse(row['can_open'])
        self.assertEqual(row['title'], 'Verified own title')
        self.service._discover()
        self.assertNotIn(CHILD, self.service.projections)
        self.assertFalse(self.service.channel.sent[-1]['params']['following'])
        self.now += 2
        self.service._discover()
        self.assertIsNot(self.service.projections[CHILD], original)
        self.assertFalse(self.service.snapshot()['by_parent'][PARENT][0]['can_interact'])
        self.service._on_message(self.message(revision=1))
        row = self.service.snapshot()['by_parent'][PARENT][0]
        self.assertEqual(row['status'], 'running')
        self.assertEqual(row['title'], 'Verified own title')
        self.assertTrue(row['can_interact'])
        self.assertEqual(len(self.service.channel.calls), 2)

    def test_patch_gap_loses_permissions_and_known_owner_replacement_revalidates(self):
        self.service._discover(); self.service._on_message(self.message(revision=90))
        message = self.message()
        message['params']['change'] = {'type': 'patches', 'baseRevision': 89, 'revision': 91, 'patches': []}
        self.service._on_message(message)
        self.assertFalse(self.service.snapshot()['by_parent'][PARENT][0]['can_open'])
        self.service._discover()
        self.now += 2
        new_owner = '33333333-3333-4333-8333-333333333333'
        self.service.channel.owner = new_owner
        self.service._discover()
        for malformed in (None, [], 'invalid'):
            self.service._on_message(malformed)
        self.service._on_message(self.message(state('completed', 'idle')))
        self.service._on_message(self.message(sourceClientId=new_owner, targetClientIds=['old-client']))
        self.assertFalse(self.service.snapshot()['by_parent'][PARENT][0]['can_open'])
        self.service._on_message(self.message(sourceClientId=new_owner))
        self.assertEqual(self.service.snapshot()['by_parent'][PARENT][0]['status'], 'running')

    def test_missing_owner_retries_known_relation_soon_and_never_reuses_terminal_status(self):
        self.service._discover(); self.service._on_message(self.message(state('completed', 'idle')))
        self.now += 16
        self.service.channel.unavailable = True
        self.service._discover()
        self.assertEqual(self.service.snapshot()['by_parent'][PARENT][0]['status'], 'disconnected')
        self.now += 2
        self.service._discover()
        self.assertEqual(len(self.service.channel.calls), 3)
        self.service.channel.unavailable = False
        self.now += 2
        self.service._discover()
        self.assertFalse(self.service.snapshot()['by_parent'][PARENT][0]['can_open'])
        self.service._on_message(self.message())
        self.assertEqual(self.service.snapshot()['by_parent'][PARENT][0]['status'], 'running')

    def test_two_missing_known_owners_cannot_starve_older_known_or_new_candidates(self):
        class DiscoveryChannel(FakeChannel):
            def discover(channel):
                channel.calls.append(channel.thread_id)
                if channel.thread_id != PARENT:
                    raise DesktopOwnerUnavailable('fixture owner unavailable')

        for reachable_is_known in (True, False):
            with self.subTest(reachable_is_known=reachable_is_known):
                self.now = 1000.0
                service = SideChatService('/fixture', home='/fixture', channel_factory=None,
                                          scanner=self.service.scanner, clock=lambda: self.now)
                service.channel = DiscoveryChannel(); service.connected = True
                threads = (PARENT, CHILD, OTHER) if reachable_is_known else (CHILD, OTHER)
                for thread in threads:
                    projection = SideChatProjection(CHILD, OWNER, clock=lambda: self.now)
                    projection.apply(change())
                    service.known[thread] = dict(projection.snapshot(False), thread_id=thread,
                                                 parent_thread_id='44444444-4444-4444-8444-444444444444')
                service.candidates[PARENT] = self.now
                for tick in range(3):
                    self.now = 1000.0 + tick
                    service._discover()
                # Known chats win the initial priority, then both unavailable
                # owners yield to the next chat instead of alternating forever.
                self.assertEqual(service.channel.calls, [OTHER, CHILD, PARENT])
                self.assertIn(PARENT, service.projections)

    def test_connection_restart_clears_old_subscription_cooldown_and_rechecks_relation(self):
        self.service._discover(); self.service._on_message(self.message(revision=90))
        self.service.next_attempt[CHILD] = self.now + 180
        self.service.channel_factory = lambda *args, **kwargs: FakeChannel()
        with patch('vibecue.side_chats.select.select', side_effect=ControlError('fixture restart')), \
                patch.object(self.service.stop_event, 'wait', return_value=True):
            self.service._run()
        self.assertNotIn(CHILD, self.service.next_attempt)
        self.assertFalse(self.service.snapshot()['by_parent'][PARENT][0]['can_open'])
        self.service.channel = FakeChannel(); self.service.connected = True
        self.service._discover()
        self.assertIn(CHILD, self.service.projections)
        self.assertFalse(self.service.snapshot()['by_parent'][PARENT][0]['can_open'])
        self.service._on_message(self.message(revision=1))
        self.assertTrue(self.service.snapshot()['by_parent'][PARENT][0]['can_open'])

    def test_non_side_chat_is_unfollowed_and_never_exported(self):
        self.service._discover()
        value = state(); value['sideConversation'] = False
        self.service._on_message(self.message(value))
        self.service._on_message(self.message(value))  # Queued body frames remain ignored.
        self.service._discover()
        self.assertNotIn(CHILD, self.service.projections)
        self.assertEqual(self.service.snapshot()['by_parent'], {})
        self.assertFalse(self.service.channel.sent[-1]['params']['following'])

    def test_cache_survives_restart_without_bodies_or_live_permissions(self):
        with tempfile.TemporaryDirectory() as home:
            service = SideChatService('/fixture', home=home, channel_factory=None,
                                      scanner=self.service.scanner, clock=lambda: self.now)
            service.channel = FakeChannel(); service.connected = True
            service._discover(); service._on_message(self.message(titled_state('Own safe title')))
            service._save_cache(force=True)
            raw = service.cache_path.read_text()
            self.assertNotIn('PRIVATE', raw)
            self.assertNotIn('can_open', raw)
            self.assertNotIn('turnHistory', raw)
            self.assertEqual(service.cache_path.stat().st_mode & 0o777, 0o600)
            restored = SideChatService('/fixture', home=home, channel_factory=None,
                                       scanner=self.service.scanner, clock=lambda: self.now)
            row = restored.snapshot()['by_parent'][PARENT][0]
            self.assertEqual(row['title'], 'Own safe title')
            self.assertEqual(row['status'], 'disconnected')
            self.assertFalse(row['can_open'])
            self.assertFalse(row['can_interact'])
            restored.channel = FakeChannel(); restored.connected = True
            self.assertFalse(restored.snapshot()['by_parent'][PARENT][0]['can_open'])
            restored._discover(); restored._on_message(self.message())
            self.assertTrue(restored.snapshot()['by_parent'][PARENT][0]['can_open'])

    def test_cache_with_public_permissions_or_unknown_parent_is_ignored(self):
        with tempfile.TemporaryDirectory() as home:
            service = SideChatService('/fixture', home=home, channel_factory=None, clock=lambda: self.now)
            service.known[CHILD] = {'thread_id': CHILD, 'parent_thread_id': PARENT, 'title': 'Safe title',
                'source': 'codex-desktop-owner-stream', 'ephemeral': True, 'status': 'completed',
                'observed_at': 1000, 'verified_at': 1000, 'expires_at': 1030}
            service.cache_dirty = True; service._save_cache(force=True)
            service.cache_path.chmod(0o644)
            restored = SideChatService('/fixture', home=home, channel_factory=None, clock=lambda: self.now)
            self.assertEqual(restored.snapshot()['by_parent'], {})
            payload = json.loads(service.cache_path.read_text()); payload['relations'][0]['parent_thread_id'] = 'invalid'
            service.cache_path.write_text(json.dumps(payload)); service.cache_path.chmod(0o600)
            restored = SideChatService('/fixture', home=home, channel_factory=None, clock=lambda: self.now)
            self.assertEqual(restored.snapshot()['by_parent'], {})


if __name__ == '__main__':
    unittest.main()
