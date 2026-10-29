import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from vibecue.providers.codex import Interpreter, ThreadMetadata, Watcher, MAX_PRIME
from vibecue.providers.task_details import (TaskDetails, bounded_details, bound_status_previews, MAX_THREAD_TEXT,
                                           MAX_DETAIL_BYTES, MAX_GLOBAL_TEXT)


def counts(inputs, outputs=0, cached=0, created=0, reasoning=0):
    return dict(input_tokens=inputs, output_tokens=outputs, cached_input_tokens=cached,
                cache_write_input_tokens=created, reasoning_output_tokens=reasoning)


def item(category, payload, at):
    return {'type': category, 'timestamp': at, 'payload': payload}


def message(role, text, phase='commentary'):
    return {'type': 'message', 'role': role, 'phase': phase,
            'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]}


class TaskDetailsTests(unittest.TestCase):
    def setUp(self):
        self.details = TaskDetails()
        self.send('event_msg', {'type': 'task_started', 'turn_id': 'turn-one'}, 1000)

    def send(self, category, payload, at):
        self.details.observe(item(category, payload, at), at)

    def token(self, at, total=None, last=None, bucket='one'):
        info = {}
        if total is not None:
            info['total_token_usage'] = total
        if last is not None:
            info['last_token_usage'] = last
        self.send('event_msg', {'type': 'token_count', 'info': info,
                                'rate_limits': {'limit_id': bucket}}, at)

    def snapshot(self, state='running', now=1100):
        return self.details.snapshot('thread', '测试任务', state, now)

    def test_single_usage_cache_reasoning_and_cross_bucket_refresh(self):
        total, last = counts(1000, 200, 800), counts(100, 20, 80, 5, 7)
        self.token(1001, total, last)
        self.token(1002, total, last, bucket='two')
        self.token(1003, counts(1100, 230, 850), counts(50, 10, 20))
        row = self.snapshot()
        self.assertEqual(row['total_tokens'], 180)
        self.assertEqual([row[k] for k in ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')], [45, 30, 100, 5])
        self.assertEqual(row['tokens_status'], 'complete')
        self.assertEqual(row['request_count'], 2)

    def test_new_turn_uses_prior_baseline_and_never_session_total(self):
        self.token(1001, counts(10000, 1000, 8000), counts(100, 10, 80))
        self.send('event_msg', {'type': 'task_complete', 'turn_id': 'turn-one'}, 1002)
        self.send('event_msg', {'type': 'task_started', 'turn_id': 'turn-two'}, 1003)
        self.token(1004, counts(10050, 1005, 8030))
        row = self.snapshot()
        self.assertEqual(row['turn_id'], 'turn-two')
        self.assertEqual(row['total_tokens'], 55)
        self.assertEqual(row['input_tokens'], 20)
        self.assertEqual(row['cache_read_tokens'], 30)

    def test_missing_baseline_is_partial_then_uses_numeric_delta(self):
        self.token(1001, counts(9000000, 300000, 6000000))
        self.assertEqual(self.snapshot()['total_tokens'], 0)
        self.assertEqual(self.snapshot()['tokens_status'], 'partial')
        self.token(1002, counts(9000020, 300003, 6000010))
        self.assertEqual(self.snapshot()['total_tokens'], 23)
        self.assertEqual(self.snapshot()['input_tokens'], 10)

    def test_optional_null_refresh_does_not_degrade_valid_turn(self):
        self.token(1001, last=counts(20, 2))
        self.send('event_msg', {'type': 'token_count', 'info': {'total_token_usage': None, 'last_token_usage': None}}, 1002)
        self.assertEqual(self.snapshot()['tokens_status'], 'complete')
        self.assertEqual(self.snapshot()['total_tokens'], 22)

    def test_explicit_zero_is_authoritative_and_advances_baseline(self):
        self.token(1001, counts(1000, 100, 800), counts(20, 2, 10))
        self.token(1002, counts(1000000, 100000, 800000), counts(0))
        self.token(1003, counts(1000100, 100010, 800040))
        self.assertEqual(self.snapshot()['total_tokens'], 132)

    def test_last_only_distinct_requests_are_not_value_deduplicated(self):
        self.token(1001, last=counts(30, 5))
        self.token(1002, last=counts(30, 5))
        self.token(1002, last=counts(30, 5))
        self.assertEqual(self.snapshot()['total_tokens'], 70)

    def test_explicit_records_merge_only_the_covered_snapshot(self):
        self.send('token_usage_record', {'usage': counts(100, 10), 'response_id': 'first'}, 1001)
        self.send('token_usage_record', {'usage': counts(200, 20), 'response_id': 'second'}, 1002)
        self.token(1003, counts(300, 30), counts(200, 20))
        self.assertEqual(self.snapshot()['total_tokens'], 330)

    def test_record_followed_by_zero_and_missing_last_is_not_double_counted(self):
        self.send('token_usage_record', {'usage': counts(100, 10), 'response_id': 'one'}, 1001)
        self.token(1002, counts(100, 10), counts(0))
        self.token(1003, counts(150, 20))
        self.assertEqual(self.snapshot()['total_tokens'], 170)
        self.send('token_usage_record', {'usage': counts(25, 5), 'response_id': 'two'}, 1004)
        self.token(1005, counts(175, 25))
        self.assertEqual(self.snapshot()['total_tokens'], 200)
        self.assertEqual(self.snapshot()['tokens_status'], 'partial')

    def test_finished_duration_stops_and_current_round_resets(self):
        self.send('response_item', message('assistant', '已经完成', 'final_answer'), 1010)
        self.send('event_msg', {'type': 'task_complete', 'turn_id': 'turn-one'}, 1011)
        row = self.snapshot('idle', now=9999)
        self.assertEqual(row['elapsed_seconds'], 11)
        self.assertEqual(row['finished_at'], 1011)
        self.assertEqual(row['status'], 'complete')
        self.send('event_msg', {'type': 'task_started', 'turn_id': 'turn-two'}, 1020)
        row = self.snapshot(now=1025)
        self.assertEqual(row['elapsed_seconds'], 5)
        self.assertIsNone(row['total_tokens'])
        self.assertEqual(row['last_message'], '已经完成')

    def test_identical_visible_reply_in_new_turn_is_a_new_message(self):
        self.send('response_item', message('assistant', '完成', 'final_answer'), 1001)
        self.send('event_msg', {'type': 'task_complete', 'turn_id': 'turn-one', 'last_agent_message': '完成'}, 1002)
        self.assertEqual(len(self.snapshot('idle')['messages']), 1)
        self.send('event_msg', {'type': 'task_started', 'turn_id': 'turn-two'}, 1003)
        self.send('response_item', message('assistant', '完成', 'final_answer'), 1004)
        row = self.snapshot('idle')
        self.assertEqual(len(row['messages']), 2)
        self.assertEqual(row['last_message_at'], 1004)

    def test_settings_come_from_observed_context(self):
        self.send('turn_context', {'turn_id': 'turn-one', 'model': 'model-a', 'effort': 'high', 'service_tier': 'fast'}, 1001)
        self.assertEqual(self.snapshot()['model'], 'model-a')
        self.assertEqual(self.snapshot()['reasoning_effort'], 'high')
        self.assertEqual(self.snapshot()['service_tier'], 'fast')
        self.send('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {
            'collaboration_mode': {'settings': {'model': 'model-b', 'reasoning_effort': 'low'}}}}, 1002)
        self.assertEqual(self.snapshot()['model'], 'model-b')
        self.assertEqual(self.snapshot()['reasoning_effort'], 'low')
        self.send('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {'service_tier': None}}, 1003)
        self.assertEqual(self.snapshot()['service_tier'], 'default')

    def test_usage_retains_each_requests_model_across_setting_changes(self):
        self.send('turn_context', {'turn_id': 'turn-one', 'model': 'model-a'}, 1001)
        self.token(1002, last=counts(100, 10, 80))
        self.send('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {'model': 'model-b'}}, 1003)
        self.token(1004, last=counts(200, 20, 100))
        self.send('event_msg', {'type': 'thread_settings_applied', 'thread_settings': {'model': 'model-c'}}, 1005)
        row = self.snapshot()
        groups = {group['model']: group for group in row['usage_by_model']}
        self.assertEqual(set(groups), {'model-a', 'model-b'})
        self.assertEqual(groups['model-a']['total_tokens'], 110)
        self.assertEqual(groups['model-b']['total_tokens'], 220)
        self.assertEqual(groups['model-a']['cache_read_tokens'], 80)
        self.assertEqual(row['model'], 'model-c')

    def test_mirrored_record_rolls_back_its_original_model_bucket(self):
        self.send('turn_context', {'turn_id': 'turn-one', 'model': 'model-a'}, 1001)
        self.send('token_usage_record', {'usage': counts(100, 10), 'response_id': 'one', 'model': 'compaction-model'}, 1002)
        self.send('event_msg', {'type': 'token_count', 'info': {'last_token_usage': counts(100, 10), 'model': 'model-b'}}, 1003)
        groups = self.snapshot()['usage_by_model']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['model'], 'model-b')
        self.assertEqual(groups[0]['requests'], 1)
        self.assertEqual(groups[0]['total_tokens'], 110)

    def test_new_turn_drops_prior_model_usage(self):
        self.send('turn_context', {'turn_id': 'turn-one', 'model': 'model-a'}, 1001)
        self.token(1002, last=counts(10, 1))
        self.send('event_msg', {'type': 'task_started', 'turn_id': 'turn-two'}, 1003)
        self.assertEqual(self.snapshot()['usage_by_model'], [])
        self.assertIsNone(self.snapshot()['total_tokens'])

    def test_model_churn_is_bounded_with_explicit_unknown_bucket(self):
        from vibecue.providers.task_details import MAX_USAGE_MODELS
        for offset in range(MAX_USAGE_MODELS + 5):
            self.send('event_msg', {'type': 'token_count', 'info': {
                'last_token_usage': counts(10, 1), 'model': 'model-' + str(offset)}}, 1001 + offset)
        row = self.snapshot()
        self.assertLessEqual(len(row['usage_by_model']), MAX_USAGE_MODELS)
        self.assertIn('', {group['model'] for group in row['usage_by_model']})
        self.assertEqual(sum(group['total_tokens'] for group in row['usage_by_model']), row['total_tokens'])

    def test_old_or_other_turn_terminal_does_not_override_current_details(self):
        self.token(1005, last=counts(20, 2))
        self.send('event_msg', {'type': 'task_complete', 'turn_id': 'old'}, 1006)
        self.send('event_msg', {'type': 'task_started', 'turn_id': 'old'}, 999)
        row = self.snapshot()
        self.assertEqual(row['turn_id'], 'turn-one')
        self.assertIsNone(row['finished_at'])
        self.assertEqual(row['total_tokens'], 22)

    def test_only_visible_messages_are_kept_and_credentials_are_redacted(self):
        for role in ('system', 'developer', 'tool'):
            self.send('response_item', message(role, 'PRIVATE-INTERNAL-' + role), 1001)
        self.send('response_item', {'type': 'reasoning', 'summary': [{'text': 'PRIVATE-REASONING'}]}, 1002)
        self.send('response_item', {'type': 'function_call', 'name': 'shell', 'arguments': 'PRIVATE-TOOL-ARGS'}, 1003)
        self.send('response_item', message('user', '<environment_context>PRIVATE-CONTEXT</environment_context>'), 1004)
        self.send('response_item', message('user', '请检查这个问题'), 1005)
        self.send('response_item', message('assistant', '结果如下\nAPI_KEY=supersecret987654\nBearer abcdefghijklmnopqrstuvwxyz\nsk-privatekeyabcdefghijklmn\nOPENAI_API_KEY=sampleprivate123\nhttps://user:urlsecret123@example.invalid/path\n-----BEGIN RSA PRIVATE KEY-----\nPRIVATE-KEY-BYTES\n-----END RSA PRIVATE KEY-----'), 1006)
        row = self.snapshot()
        encoded = json.dumps(row)
        for secret in ('PRIVATE-INTERNAL', 'PRIVATE-REASONING', 'PRIVATE-TOOL-ARGS', 'PRIVATE-CONTEXT', 'supersecret987654', 'abcdefghijklmnopqrstuvwxyz', 'sk-privatekey', 'sampleprivate123', 'urlsecret123', 'PRIVATE-KEY-BYTES'):
            self.assertNotIn(secret, encoded)
        self.assertEqual([message['role'] for message in row['messages']], ['user', 'assistant'])
        self.assertIn('[已隐藏凭据]', row['last_message'])

    def test_structured_answer_is_decoded_before_preview_truncation(self):
        envelope='<send_user_message_question_reply>'+json.dumps([{
            'questionItemId':'PRIVATE-'+('x'*20_000),
            'question':'该使用哪个账号？','answer':'我切换到普通账号了'}],ensure_ascii=False)+'</send_user_message_question_reply>'
        self.send('response_item',message('user',envelope),1001)
        row=self.snapshot()
        self.assertEqual(row['last_message'],'我切换到普通账号了')
        self.assertFalse(row['content_truncated'])
        self.assertNotIn('questionItemId',json.dumps(row,ensure_ascii=False))
        self.assertNotIn('send_user_message_question_reply',json.dumps(row,ensure_ascii=False))

    def test_multiple_structured_answers_keep_question_context_and_redact_secrets(self):
        rows=[{'question':'颜色？','answer':'蓝色','questionItemId':'PRIVATE-ONE'},
              {'question':'凭据？','answer':'API_KEY=secret-value','questionItemId':'PRIVATE-TWO'}]
        value='<send_user_message_question_reply>'+json.dumps(rows,ensure_ascii=False)+'</send_user_message_question_reply>'
        self.send('response_item',message('user',value),1001)
        text=self.snapshot()['last_message']
        self.assertIn('颜色？\n蓝色',text)
        self.assertIn('凭据？\nAPI_KEY=[已隐藏凭据]',text)
        self.assertNotIn('PRIVATE',text)

    def test_structured_answer_with_quoted_credentials_is_decoded_before_redaction(self):
        answer='已确认，可以继续。\npassword="SYNTHETIC-PASSWORD"\nAPI_KEY="SYNTHETIC-API-KEY"\n保留这段说明。'
        value='<send_user_message_question_reply>'+json.dumps([
            {'questionItemId':'SYNTHETIC-ID','answer':answer}],ensure_ascii=False)+'</send_user_message_question_reply>'
        for content in (value, [{'type':'input_text','text':value}]):
            with self.subTest(content_type=type(content).__name__):
                self.send('response_item',{'type':'message','role':'user','content':content},1001)
                text=self.snapshot()['last_message']
                self.assertEqual(text,'已确认，可以继续。\npassword=[已隐藏凭据]"\nAPI_KEY=[已隐藏凭据]"\n保留这段说明。')
                self.assertNotIn('SYNTHETIC',text)

    def test_literal_json_and_fenced_transport_example_remain_visible(self):
        for value in ['{"questionItemId":"example","answer":"example answer"}',
                      '```xml\n<send_user_message_question_reply>[{"answer":"example"}]</send_user_message_question_reply>\n```',
                      '请解释 <send_user_message_question_reply> 这个标签。']:
            self.send('response_item',message('user',value),1001)
            self.assertEqual(self.snapshot()['last_message'],value)

    def test_malformed_question_envelope_does_not_leak_transport_ids(self):
        value='<send_user_message_question_reply>[{"questionItemId":"PRIVATE"}]</send_user_message_question_reply>'
        self.send('response_item',message('user',value),1001)
        self.assertEqual(self.snapshot()['last_message'],'问题回复暂无法预览。')

    def test_bounded_content_preserves_last_message_and_numbers(self):
        for offset in range(12):
            self.send('response_item', message('assistant', str(offset) + '中' * 2000), 1001 + offset)
        self.token(1020, last=counts(100, 10))
        row = self.snapshot()
        self.assertLessEqual(len(row['messages']), 8)
        self.assertLessEqual(sum(len(message['text']) for message in row['messages']), MAX_THREAD_TEXT)
        rows = [dict(row, thread_id='t-' + str(i), last_activity=2000 + i) for i in range(300)]
        result = bounded_details(rows)
        self.assertEqual(len(result), 256)
        self.assertLessEqual(sum(bool(row['messages']) for row in result.values()), 48)
        size = sum(sum(len(message['text']) for message in row['messages']) + len(row['last_message']) for row in result.values())
        self.assertLessEqual(size, MAX_GLOBAL_TEXT)
        self.assertLessEqual(len(json.dumps(result).encode()), MAX_DETAIL_BYTES)
        self.assertTrue(all(row['total_tokens'] == 110 for row in result.values()))

    def test_whole_status_budget_keeps_numeric_metrics(self):
        self.send('response_item', message('assistant', '中' * 11000), 1001)
        self.token(1002, last=counts(100, 10))
        row = self.snapshot()
        details = bounded_details([dict(row, thread_id='thread-' + str(i), last_activity=1000 + i) for i in range(256)])
        state = {'task_details': details, 'cursors': {str(i): {'path': 'x' * 1000} for i in range(380)}}
        constrained = bound_status_previews(state, maximum=750000)
        self.assertLessEqual(len(json.dumps(constrained).encode()), 750000)
        self.assertEqual(len(constrained['task_details']), 256)
        self.assertTrue(all(row['total_tokens'] == 110 for row in constrained['task_details'].values()))

    def test_truncated_start_is_explicitly_partial(self):
        self.details.reset(window_truncated=True)
        self.send('turn_context', {'turn_id': 'partial', 'model': 'model-a'}, 1000)
        self.token(1001, counts(10000, 1000), counts(100, 10))
        row = self.snapshot()
        self.assertEqual(row['elapsed_status'], 'partial')
        self.assertEqual(row['tokens_status'], 'partial')
        self.assertEqual(row['total_tokens'], 110)


class WatcherDetailsIntegrationTests(unittest.TestCase):
    def test_prime_recovers_details_and_completed_messages_without_alerts(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / 'thread.jsonl'
            now = time.time()
            events = [item('event_msg', {'type': 'task_started', 'turn_id': 'latest'}, now - 30),
                      item('turn_context', {'model': 'model-a', 'effort': 'high'}, now - 29),
                      item('event_msg', {'type': 'token_count', 'info': {'last_token_usage': counts(100, 10, 80)}}, now - 20),
                      item('response_item', message('assistant', '最新结果', 'final_answer'), now - 10),
                      item('event_msg', {'type': 'task_complete', 'turn_id': 'latest', 'last_agent_message': '最新结果'}, now - 9)]
            path.write_bytes(b''.join((json.dumps(event) + '\n').encode() for event in events))
            emitted = []
            watcher = Watcher(home, home / 'vibecue', emit=lambda **event: emitted.append(event))
            watcher.metadata = lambda: [ThreadMetadata('thread', path, '任务', 'vscode')]
            watcher.running = True
            watcher.discover(initial=True)
            with patch('vibecue.providers.codex.approval_status', return_value={'connected': False}):
                watcher.write_status()
            state = json.loads((watcher.vibecue_home / 'watcher-state.json').read_text())
            self.assertEqual(emitted, [])
            self.assertEqual(state['running_tasks'], [])
            row = state['task_details']['thread']
            self.assertEqual(row['total_tokens'], 110)
            self.assertEqual(row['last_message'], '最新结果')
            self.assertEqual(len(row['messages']), 1)
            self.assertEqual(row['status'], 'complete')
            self.assertEqual(row['elapsed_seconds'], 21)
            self.assertEqual((watcher.vibecue_home / 'watcher-state.json').stat().st_mode & 0o777, 0o600)

    def test_subagent_has_no_detail_and_running_body_is_not_duplicated(self):
        now = time.time()
        child = Interpreter(ThreadMetadata('child', Path('/unused'), source={'subagent': {'thread_spawn': {}}}), lambda **event: None)
        child.process(item('response_item', message('assistant', '不可混入主对话'), now))
        self.assertIsNone(child.detail_snapshot(now))
        parent = Interpreter(ThreadMetadata('parent', Path('/unused'), source='vscode'), lambda **event: None)
        parent.process(item('event_msg', {'type': 'task_started', 'turn_id': 'live'}, now))
        parent.process(item('response_item', message('assistant', '当前进度'), now + 1))
        task = parent.running_task(now + 2)
        self.assertNotIn('last_message', task)
        self.assertNotIn('messages', task)
        self.assertEqual(task['status'], 'running')


if __name__ == '__main__':
    unittest.main()
