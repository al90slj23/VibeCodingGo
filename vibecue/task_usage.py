"""Cached numeric task-row usage, without scanning or modifying source history.

The live turn and the durable ledger are separate evidence scopes. In
particular, live requests are never added to a historical subtotal: a request
may already be in the ledger, and the watcher cannot infer that safely.
"""
from __future__ import annotations

from contextlib import closing
import math
from pathlib import Path
import sqlite3
import time

from .events import vibecue_directory
from . import usage_money, usage_pricing, usage_schedule
from .usage import get_meta, TOKEN_COLUMNS

MAX_THREADS = 256
CACHE_TTL = 30
QUERY_BUDGET = 1.0


def _state(path, *, empty_absent=False):
    try:
        value = path.stat()
        if empty_absent and not value.st_size:
            return (str(path), None)
        return (str(path), value.st_ino, value.st_size, value.st_mtime_ns)
    except OSError:
        return (str(path), None)


def _timestamp(value, now):
    return (value if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 < value <= now + 60 else None)


def _integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class TaskUsageCache:
    """A watcher-owned cache. decorate() returns copies and never writes files.

    Database/WAL and settings/cache file changes invalidate immediately; the
    TTL also refreshes stale-price and stale-ledger flags. Returned timestamps
    always come from observed events or sync metadata, never from a poll.
    """
    def __init__(self, home=None, cc_path=None, ttl=CACHE_TTL):
        self.home = vibecue_directory(home)
        self.cc_path = Path(cc_path) if cc_path is not None else Path.home() / '.cc-switch/cc-switch.db'
        self.ttl = ttl
        self._money_key = self._ledger_key = None
        self._money_at = self._ledger_at = -math.inf
        self._money = None
        self._ledger = {'available': False, 'groups': {}, 'sessions': {}, 'sync': {}}

    def _money_snapshot(self, now):
        prefs, _ = usage_money.preferences(self.home)
        paths = [self.home / 'usage-display.json', self.home / 'usage-exchange.json',
                 self.home / 'usage-schedule.json']
        paths.extend(usage_pricing.cache_path(self.home, source) for source in prefs['pricing_sources']
                     if source['enabled'] and source['kind'] not in ('custom', 'ccswitch'))
        if any(source['enabled'] and source['kind'] == 'ccswitch' for source in prefs['pricing_sources']):
            paths.extend((self.cc_path, Path(str(self.cc_path) + '-wal')))
        key = tuple(_state(path) for path in paths)
        if self._money is None or key != self._money_key or now - self._money_at >= self.ttl or now < self._money_at:
            self._money = usage_money.settings(self.home, self.cc_path, now)
            self._prices = {row['model']: row for row in self._money['pricing']}
            self._schedule = usage_schedule.settings(self.home)['prefs']
            self._money_key, self._money_at = key, now
        return self._money

    def _read_ledger(self, identifiers, now):
        path = self.home / 'usage.sqlite3'
        key = (identifiers, _state(path), _state(Path(str(path) + '-wal'), empty_absent=True))
        if key == self._ledger_key and 0 <= now - self._ledger_at < self.ttl:
            return self._ledger
        old = self._ledger
        result = {'available': False, 'groups': {}, 'sessions': {}, 'sync': {}}
        try:
            if path.is_file() and identifiers:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.1)) as conn:
                    deadline = time.monotonic() + QUERY_BUDGET
                    conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                    conn.row_factory = sqlite3.Row
                    conn.execute('PRAGMA query_only=ON')
                    conn.execute('BEGIN')
                    sync = get_meta(conn, 'sync', {})
                    if not isinstance(sync, dict):
                        raise ValueError('invalid sync metadata')
                    synced = _timestamp(sync.get('last_synced_at'), now)
                    result['sync'] = {
                        'last_synced_at': synced, 'running': sync.get('running') is True,
                        'partial': bool(sync.get('errors') or sync.get('warnings') or sync.get('historical_gaps') or sync.get('rebuild')),
                        'codex_unavailable': any(isinstance(row, dict) and row.get('app') == 'codex'
                                                 and row.get('available') is not True for row in sync.get('coverage', [])
                                                 if isinstance(sync.get('coverage'), list)),
                    }
                    if synced is not None:
                        placeholders = ','.join('?' for _ in identifiers)
                        sums = ','.join('COALESCE(SUM(' + column + '),0) AS ' + column
                                        for column in TOKEN_COLUMNS + ('total_tokens',))
                        for row in conn.execute('SELECT thread_id,model,COUNT(*) AS requests,' + sums +
                                                " FROM usage_requests WHERE app='codex' AND thread_id IN (" +
                                                placeholders + ') GROUP BY thread_id,model', identifiers):
                            group = dict(row)
                            if any(_integer(group[column]) is None for column in ('requests',) + TOKEN_COLUMNS + ('total_tokens',)):
                                raise ValueError('invalid numeric ledger')
                            result['groups'].setdefault(group.pop('thread_id'), []).append(group)
                        for row in conn.execute('SELECT id,complete FROM usage_codex_sessions WHERE id IN (' + placeholders + ')', identifiers):
                            result['sessions'][row['id']] = row['complete'] == 1
                        result['available'] = True
        except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
            # A transient lock or an interrupted read may retain a known
            # subtotal, but must label it as stale rather than pretend fresh.
            if old.get('available') and self._ledger_key and identifiers == self._ledger_key[0]:
                result = dict(old, read_error=True)
            else:
                result = {'available': False, 'groups': {}, 'sessions': {}, 'sync': {}}
        self._ledger, self._ledger_key, self._ledger_at = result, key, now
        return result

    @staticmethod
    def _summary(groups, tokens_status, total, request_count, money, prices, provenance, updated_at,
                 *, stale=False, message=''):
        fx = {key: money['fx'].get(key) for key in ('rate', 'source', 'date', 'updated_at', 'stale')}
        result = {'total_tokens': total, 'request_count': request_count,
                  'tokens_status': tokens_status, 'provenance': provenance, 'updated_at': updated_at,
                  'stale': stale, 'currency': money['prefs']['currency'], 'estimated': True,
                  'fx': fx, 'message': message}
        for column in TOKEN_COLUMNS:
            result[column] = sum(group[column] for group in groups) if total is not None else None
        if tokens_status == 'unknown':
            result.update(cost_usd=None, cost_cny=None, priced_requests=0, unpriced_requests=0,
                          cost_partial=False, price_sources=[], cost_status='unknown', pricing_stale=False)
            return result
        result.update(usage_money.folded_cost(groups, prices, fx['rate']))
        result['pricing_stale'] = any(prices.get(group.get('model'), {}).get('source_stale') for group in groups)
        result['cost_status'] = ('unpriced' if result['unpriced_requests'] and not result['priced_requests'] else
                                 'partial' if result['unpriced_requests'] or tokens_status == 'partial' or result['pricing_stale'] else 'complete')
        # A zero subtotal with an incomplete observation is not evidence of a
        # free turn; no successfully priced request has been seen yet.
        if tokens_status == 'partial' and not request_count:
            result.update(cost_usd=None, cost_cny=None, cost_status='unknown')
        return result

    def decorate(self, rows, now=None):
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError('invalid task usage timestamp')
        rows = list(rows)
        if not rows:
            return []
        money = self._money_snapshot(now)
        identifiers = tuple(sorted({row['thread_id'] for row in rows[:MAX_THREADS]
                                    if isinstance(row.get('thread_id'), str) and row['thread_id']}))
        ledger = self._read_ledger(identifiers, now)
        synced = ledger['sync'].get('last_synced_at')
        overdue = synced is not None and now - synced > max(60, self._schedule['sync_interval_seconds'] * 2)
        result = []
        for original in rows:
            row = dict(original)
            status = row.get('tokens_status')
            status = status if status in ('complete', 'partial') and _integer(row.get('total_tokens')) is not None else 'unknown'
            groups = row.get('usage_by_model')
            if (not isinstance(groups, list) or len(groups) > 16 or
                    any(not isinstance(group, dict) or not isinstance(group.get('model'), str) or
                        any(_integer(group.get(column)) is None for column in ('requests',) + TOKEN_COLUMNS)
                        for group in groups) or
                    sum(sum(group[column] for column in TOKEN_COLUMNS) for group in groups) != (row.get('total_tokens') or 0)):
                # Older snapshots lack the request's observed model. Preserve
                # tokens but do not price them with a later selected model.
                groups = [dict(model='', requests=_integer(row.get('request_count')) or 1,
                               **{column: _integer(row.get(column)) or 0 for column in TOKEN_COLUMNS})] if status != 'unknown' and row.get('total_tokens') else []
            row['turn_usage'] = self._summary(groups, status, row.get('total_tokens') if status != 'unknown' else None,
                                               _integer(row.get('request_count')) if status != 'unknown' else None,
                                               money, self._prices, 'current_turn', row.get('last_activity'),
                                               message='本轮已观察用量；费用按各请求模型的 API 参考单价估算。')
            thread = row.get('thread_id')
            history = ledger['groups'].get(thread, [])
            known = ledger['available'] and (bool(history) or ledger['sessions'].get(thread) is True)
            behind = synced is not None and (row.get('last_activity') or 0) > synced
            stale = bool(overdue or behind or ledger.get('read_error') or ledger['sync'].get('running'))
            partial = stale or ledger['sync'].get('partial') or ledger['sync'].get('codex_unavailable') or ledger['sessions'].get(thread) is False
            history_status = 'partial' if known and partial else 'complete' if known else 'unknown'
            row['history_usage'] = self._summary(history, history_status,
                                                  sum(group['total_tokens'] for group in history) if known else None,
                                                  sum(group['requests'] for group in history) if known else None,
                                                  money, self._prices, 'synced_history', synced, stale=stale if known else False,
                                                  message='整段对话已入账历史小计；本轮尚未同步的用量会在下次同步后计入。' if known else
                                                          '这段对话尚无可确认的已同步历史用量。')
            row.pop('usage_by_model', None)
            result.append(row)
        return result
