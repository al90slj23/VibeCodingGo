"""Private recovery ledger for work that outlives an observer or Codex process.

Only navigation metadata and numeric telemetry are durable. Process observation
uses executable names/start times, never argv, credentials or Codex mutations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from ..events import clean_text, vibecue_directory

MAX_LEDGER_BYTES = 8 * 1024 * 1024
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,200}\Z')
TOKEN_KEYS = ('total_tokens', 'input_tokens', 'output_tokens', 'cache_read_tokens',
              'cache_creation_tokens', 'request_count')
TEXT_KEYS = ('model', 'reasoning_effort', 'service_tier', 'elapsed_status', 'tokens_status')
REASONS = {
    'codex_process_lost': '此前运行的 Codex 进程已退出，未见本轮完成记录',
    'codex_process_changed': 'Codex 运行实例已变化，原任务是否继续待确认',
    'previous_observer_lost': '上次运行记录已保留，等待新的执行证据',
    'process_unavailable': '暂时无法核实 Codex 进程，保留上次运行记录',
    'source_unavailable': '运行记录暂时不可读，保留上次运行记录',
    'activity_expired': '长时间没有新的执行证据，保留上次运行记录',
    'historical_unfinished_turn': '历史本轮未见完成记录，是否因退出中断待确认',
    'previous_turn_aborted': '本轮有中断记录，尚未见后续继续或完成',
}


def finite(value, default=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value) if math.isfinite(value) and value >= 0 else default


def safe_row(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get('thread_id'), str) or not IDENTIFIER.fullmatch(raw['thread_id']):
        return None
    if raw.get('provider', 'codex') != 'codex':
        return None
    started, last = finite(raw.get('started_at')), finite(raw.get('last_activity'))
    if not started or not last:
        return None
    row = {'thread_id': raw['thread_id'], 'title': clean_text(raw.get('title', ''), 160),
           'provider': 'codex', 'turn_id': clean_text(raw.get('turn_id', ''), 200),
           'started_at': started, 'last_activity': last}
    for key in TOKEN_KEYS:
        value = raw.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 9_000_000_000_000_000:
            row[key] = value
    for key in TEXT_KEYS:
        if isinstance(raw.get(key), str):
            row[key] = clean_text(raw[key], 180)
    if 'elapsed_seconds' in raw:
        row['elapsed_seconds'] = finite(raw['elapsed_seconds'])
    row['status'] = raw.get('status') if raw.get('status') in ('running', 'interrupted', 'unconfirmed') else 'running'
    row['last_seen_at'] = finite(raw.get('last_seen_at'), last)
    row['last_work_at'] = finite(raw.get('last_work_at'), last)
    row['source_kind'] = 'cli' if raw.get('source_kind') == 'cli' else 'desktop'
    row['owner_ids'] = [value for value in raw.get('owner_ids', [])[:256]
                        if isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value)] if isinstance(raw.get('owner_ids'), list) else []
    path = raw.get('transcript_path')
    if isinstance(path, str) and len(path) <= 4096 and '\x00' not in path and Path(path).is_absolute():
        row['transcript_path'] = path
    if row['status'] != 'running':
        row['interrupted_at'] = finite(raw.get('interrupted_at'), row['last_seen_at'])
        reason = raw.get('reason')
        row['reason'] = reason if reason in REASONS else 'previous_observer_lost'
        row['reason_text'] = REASONS[row['reason']]
        row['finished_at'] = finite(raw.get('finished_at'), row['last_seen_at'])
    return row


def private_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    content = (json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if len(content) > MAX_LEDGER_BYTES:
        raise ValueError('task recovery metadata exceeds limit')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, maximum=MAX_LEDGER_BYTES):
    with Path(path).open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('task recovery metadata exceeds limit')
    return json.loads(data)


def codex_process_snapshot(now=None):
    """Inspect pid/start/executable only. Failure is unknown, never 'no Codex'."""
    now = time.time() if now is None else now
    try:
        result = subprocess.run(['/bin/ps', '-axo', 'pid=,lstart=,comm='], capture_output=True,
                                text=True, timeout=2, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        if result.returncode != 0 or len(result.stdout) > 8 * 1024 * 1024:
            return {'available': False, 'observed_at': now, 'desktop': [], 'cli': []}
        groups = {'desktop': [], 'cli': []}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 6)
            if len(parts) != 7 or not parts[0].isdigit():
                continue
            executable = parts[6]
            name = Path(executable).name
            if not re.fullmatch(r'codex(?:-[A-Za-z0-9_-]+)?', name, re.I):
                continue
            try:
                started = time.mktime(time.strptime(' '.join(parts[1:6]), '%a %b %d %H:%M:%S %Y'))
            except (ValueError, OverflowError):
                continue
            kind = 'desktop' if re.search(r'/(?:ChatGPT|Codex)\.app/Contents/', executable) else 'cli'
            identity = hashlib.sha256((parts[0] + ':' + str(started) + ':' + executable).encode()).hexdigest()
            groups[kind].append({'id': identity, 'started_at': started})
        return {'available': True, 'observed_at': now, **groups}
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return {'available': False, 'observed_at': now, 'desktop': [], 'cli': []}


class TaskRecovery:
    def __init__(self, home, observer_started=None):
        self.home = Path(home); self.path = self.home / 'task-recovery.json'
        self.observer_started = time.time() if observer_started is None else observer_started
        self.records, self.dismissed = {}, {}
        self.error = ''
        self._last_payload = None
        self._corrupt = False
        try:
            value = read_json(self.path)
            if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('records'), dict):
                raise ValueError('invalid recovery ledger')
            for raw in value['records'].values():
                row = safe_row(raw)
                if row:
                    self.records[row['thread_id']] = row
            raw_dismissed = value.get('dismissed', {})
            if isinstance(raw_dismissed, dict):
                for tid, raw in raw_dismissed.items():
                    if isinstance(tid, str) and IDENTIFIER.fullmatch(tid) and isinstance(raw, dict):
                        self.dismissed[tid] = {'turn_id': clean_text(raw.get('turn_id', ''), 200),
                                               'last_activity': finite(raw.get('last_activity'))}
        except FileNotFoundError:
            self._migrate()
        except (OSError, ValueError, TypeError):
            self.error = '保留任务记录暂不可读，原文件已保留'
            self._corrupt = True
            self._migrate()

    def _migrate(self):
        try:
            old = read_json(self.home / 'watcher-state.json')
            cursors = old.get('cursors', {})
            for raw in old.get('running_tasks', []) + old.get('interrupted_tasks', []):
                row = safe_row(raw)
                if not row:
                    continue
                path = cursors.get(row['thread_id'], {}).get('path') if isinstance(cursors, dict) else None
                if isinstance(path, str) and Path(path).is_absolute():
                    row['transcript_path'] = path
                if row['status'] == 'running':
                    self._interrupt(row, 'unconfirmed', 'previous_observer_lost', self.observer_started)
                self.records[row['thread_id']] = row
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def retained_metadata(self):
        return [dict(row) for row in self.records.values() if row.get('transcript_path')]

    def _receipt_paths(self, folder, limit):
        try:
            return list((self.home / folder).glob('*.json'))[:limit]
        except OSError:
            # An inaccessible optional inbox must not stop transcript polling
            # or make already retained navigation targets disappear.
            self.error = '保留任务操作暂不可读，已有任务记录仍保留'
            return []

    def apply_remembered(self):
        """Apply explicit retrospective receipts before transcript discovery.

        Their current tails are then reconciled before they are published; a
        newer start/completion supersedes the imported unfinished old turn.
        """
        for path in self._receipt_paths('recovery-imports', 64):
            changed = False
            try:
                data = read_json(path, 1024 * 1024)
                for raw in data.get('tasks', [])[:512]:
                    row = safe_row(raw)
                    if row and row['thread_id'] not in self.records and row['thread_id'] not in self.dismissed:
                        self._interrupt(row, 'unconfirmed', 'historical_unfinished_turn', self.observer_started)
                        self.records[row['thread_id']] = row
                        changed = True
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            if not changed or self._persist(time.time()):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _interrupt(self, row, status, reason, now):
        if row['status'] == 'running' or row.get('reason') != reason:
            row['interrupted_at'] = now
        row.update(status=status, reason=reason, reason_text=REASONS[reason])
        row['finished_at'] = row.get('last_seen_at', row['last_activity'])
        row.setdefault('elapsed_seconds', max(0, row['finished_at'] - row['started_at']))

    def _owners(self, observation, runtime):
        kind = observation.get('source_kind', 'desktop')
        return runtime.get(kind, []) if kind == 'desktop' else runtime.get('cli', [])

    def _apply_dismissals(self):
        for path in self._receipt_paths('recovery-dismissals', 128):
            changed = False
            try:
                value = read_json(path, 4096)
                row = self.records.get(value.get('thread_id'))
                if (row and row['status'] != 'running' and value.get('turn_id') == row['turn_id'] and
                        value.get('interrupted_at') == row.get('interrupted_at')):
                    self.dismissed[row['thread_id']] = {'turn_id': row['turn_id'], 'last_activity': row['last_activity']}
                    del self.records[row['thread_id']]
                    changed = True
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            if not changed or self._persist(time.time()):
                try:
                    path.unlink()
                except OSError:
                    pass

    def reconcile(self, observations, runtime, now=None, waiting=()):
        now = time.time() if now is None else now
        self._apply_dismissals()
        observed = {item['thread_id']: item for item in observations}
        waiting = set(waiting)
        for tid in list(set(observed) | set(self.records)):
            observation = observed.get(tid, {})
            row = self.records.get(tid)
            candidate = safe_row(observation.get('task'))
            watermark = finite(observation.get('watermark'))
            terminal = finite(observation.get('terminal_at'))
            terminal_kind = observation.get('terminal_kind')
            terminal_turn = observation.get('terminal_turn_id')
            matching_terminal = not terminal_turn or not row or terminal_turn == row['turn_id']
            if row and ((observation.get('state') == 'idle' and matching_terminal and terminal >= row['last_activity'] and terminal and terminal_kind not in ('turn_aborted', 'task_aborted')) or
                        (observation.get('state') == 'waiting' and watermark >= row['last_activity']) or tid in waiting):
                self.records.pop(tid, None)
                continue
            owners = self._owners(observation or row or {}, runtime)
            owner_ids = {item['id'] for item in owners}
            old_ids = set(row.get('owner_ids', [])) if row else set()
            runtime_known = runtime.get('available') is True
            lost = bool(old_ids and runtime_known and old_ids.isdisjoint(owner_ids))
            changed = bool(old_ids and runtime_known and old_ids != owner_ids)
            if row and observation.get('state') == 'idle' and matching_terminal and terminal and terminal >= row['last_activity'] and terminal_kind in ('turn_aborted', 'task_aborted'):
                row['last_seen_at'] = min(row['last_seen_at'], terminal)
                self._interrupt(row, 'interrupted', 'previous_turn_aborted', terminal)
                continue
            floor = self.dismissed.get(tid)
            if floor and candidate:
                if candidate['last_work_at'] <= floor['last_activity']:
                    candidate = None
                else:
                    self.dismissed.pop(tid, None)
            resumed = bool(candidate and row and candidate['last_work_at'] > row['last_work_at'])
            if candidate and (not row or row['status'] == 'running' or resumed or
                              (row.get('reason') == 'source_unavailable' and not lost)):
                # Prime-only evidence before every current process started is a
                # stranded old turn, not work owned by the freshly opened app.
                predates_all = bool(runtime_known and owners and
                                    candidate['last_work_at'] + 2 < min(item['started_at'] for item in owners))
                fresh = bool((candidate['last_work_at'] >= self.observer_started or resumed) and
                             (not runtime_known or (owners and candidate['last_work_at'] + 2 >= min(item['started_at'] for item in owners))))
                if lost and not fresh:
                    self._interrupt(row, 'interrupted', 'codex_process_lost', now)
                elif predates_all and not fresh:
                    row = row or candidate
                    self._interrupt(row, 'interrupted', 'codex_process_lost', now)
                    self.records[tid] = row
                elif runtime_known and not owners:
                    row = row or candidate
                    self._interrupt(row, 'interrupted', 'codex_process_lost', now)
                    self.records[tid] = row
                elif changed and not fresh:
                    self._interrupt(row, 'unconfirmed', 'codex_process_changed', now)
                else:
                    candidate.update(status='running', last_seen_at=now,
                                     owner_ids=sorted(owner_ids) if runtime_known else list(old_ids),
                                     source_kind=observation.get('source_kind', 'desktop'))
                    path = observation.get('transcript_path')
                    if isinstance(path, str) and Path(path).is_absolute():
                        candidate['transcript_path'] = path
                    self.records[tid] = candidate
                continue
            if not row:
                continue
            if lost:
                self._interrupt(row, 'interrupted', 'codex_process_lost', now)
            elif row['status'] == 'running':
                reason = ('source_unavailable' if observation.get('source_readable') is not True else
                          'activity_expired' if observation.get('state') == 'running' else
                          'process_unavailable' if not runtime_known else 'previous_observer_lost')
                self._interrupt(row, 'unconfirmed', reason, now)
        self._persist(now)
        rows = [self.public_row(row) for row in self.records.values()]
        active = sorted((row for row in rows if row['status'] == 'running'), key=lambda row: (-row['last_activity'], row['thread_id']))
        interrupted = sorted((row for row in rows if row['status'] != 'running'), key=lambda row: (-row['interrupted_at'], row['thread_id']))
        return active, interrupted

    @staticmethod
    def public_row(row):
        return {key: value for key, value in row.items() if key not in ('owner_ids', 'source_kind', 'transcript_path')}

    def _persist(self, now):
        payload = {'version': 1, 'records': self.records, 'dismissed': self.dismissed}
        serial = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if serial == self._last_payload:
            return True
        try:
            if self._corrupt and self.path.exists():
                backup = self.path.with_name('task-recovery-corrupt-' + uuid.uuid4().hex + '.json')
                self.path.rename(backup)
                backup.chmod(0o600)
                self._corrupt = False
            private_json(self.path, dict(payload, updated_at=now))
            self._last_payload = serial
            return True
        except (OSError, ValueError):
            self.error = '保留任务记录暂时无法写入，本次记录仍保存在运行状态中'
            return False


def dismiss(home, thread_id, turn_id, interrupted_at):
    if not IDENTIFIER.fullmatch(thread_id) or len(turn_id) > 200 or not finite(interrupted_at):
        raise ValueError('invalid recovery target')
    ledger = TaskRecovery(home)
    row = ledger.records.get(thread_id)
    if not row or row['status'] == 'running' or row['turn_id'] != turn_id or row.get('interrupted_at') != interrupted_at:
        return False
    private_json(Path(home) / 'recovery-dismissals' / (uuid.uuid4().hex + '.json'),
                 {'thread_id': thread_id, 'turn_id': turn_id, 'interrupted_at': interrupted_at})
    return True


def remember(home, rows):
    if not isinstance(rows, list) or len(rows) > 512:
        raise ValueError('invalid recovery import')
    checked = []
    for raw in rows:
        row = safe_row(raw)
        if row is None:
            raise ValueError('invalid recovery import row')
        row.update(status='unconfirmed', reason='historical_unfinished_turn')
        checked.append(row)
    if checked:
        private_json(Path(home) / 'recovery-imports' / (uuid.uuid4().hex + '.json'), {'tasks': checked})
    return len(checked)


def main(argv=None):
    parser = argparse.ArgumentParser(description='VibeCoding Go 保留任务记录操作，不控制 Codex 执行')
    parser.add_argument('command', choices=('dismiss', 'remember'))
    parser.add_argument('--home')
    parser.add_argument('--thread-id', default='')
    parser.add_argument('--turn-id', default='')
    parser.add_argument('--interrupted-at', type=float)
    args = parser.parse_args(argv)
    try:
        home = vibecue_directory(args.home)
        if args.command == 'remember':
            raw = sys.stdin.buffer.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('recovery import too large')
            result = {'queued': remember(home, json.loads(raw))}
        else:
            result = {'dismissed': dismiss(home, args.thread_id, args.turn_id, args.interrupted_at)}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError):
        print(json.dumps({'error': '未能更新保留任务记录，请稍后重试'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
