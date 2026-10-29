"""API-price estimates and anonymous USD/CNY reference rates.

Only CC Switch's six public model-pricing columns are read. Its settings,
accounts, credentials and OAuth state are never opened. Token accounting stays
in usage.sqlite3; price changes only affect the displayed estimate.
"""
from __future__ import annotations

import argparse
import fcntl
from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import urllib.request

from .events import vibecue_directory
from . import usage_pricing

RATE_URL = 'https://api.frankfurter.dev/v2/rate/usd/cny'
RATE_SOURCE = 'Frankfurter · USD/CNY 每日参考汇率'
RATE_TTL = 6 * 60 * 60
FAILURE_TTL = 15 * 60
MAX_DECORATION_COST_ROWS = 8192
_MISSING_ROW_COST = object()
MAX_JSON = 64 * 1024
MAX_SETTINGS = 1024 * 1024
RATE_KEYS = ('input', 'output', 'cache_read', 'cache_creation')
TOKEN_KEYS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')
CC_COLUMNS = ('input_cost_per_million', 'output_cost_per_million',
              'cache_read_cost_per_million', 'cache_creation_cost_per_million')
DEFAULTS = {'currency': 'CNY', 'fx_mode': 'auto', 'manual_rate': None, 'model_overrides': {},
            'pricing_sources': usage_pricing.defaults(), 'pricing_refresh_seconds': usage_pricing.REFRESH_SECONDS}


class MoneyError(ValueError):
    pass


def numeric(value, *, positive=False):
    """Bound decimal complexity before arithmetic; reject booleans and NaNs."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise MoneyError('金额或汇率必须是有效数字')
    text = str(value).strip()
    if not text or len(text) > 80:
        raise MoneyError('金额或汇率必须是有效数字')
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise MoneyError('金额或汇率必须是有效数字') from None
    if not result.is_finite() or result < 0 or (positive and result == 0) or result > Decimal('1e15'):
        raise MoneyError('金额或汇率超出有效范围')
    if result and result.adjusted() < -30:
        raise MoneyError('金额或汇率精度过高')
    return result


def model_id(value):
    if (not isinstance(value, str) or not value or value != value.strip() or len(value) > 180 or
            any(ord(ch) < 32 for ch in value)):
        raise MoneyError('模型标识无效')
    return value


def validate_rates(value):
    if not isinstance(value, dict) or set(value) != set(RATE_KEYS):
        raise MoneyError('模型单价必须包含输入、输出、缓存读取和缓存写入四项')
    return {key: float(numeric(value[key])) for key in RATE_KEYS}


def validate_patch(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise MoneyError('费用显示设置包含未知字段')
    result = {}
    for key, raw in value.items():
        if key == 'currency':
            if raw not in ('CNY', 'USD', 'both'):
                raise MoneyError('请选择人民币、美元或双币显示')
            result[key] = raw
        elif key == 'fx_mode':
            if raw not in ('auto', 'manual'):
                raise MoneyError('汇率模式无效')
            result[key] = raw
        elif key == 'manual_rate':
            result[key] = None if raw is None else float(numeric(raw, positive=True))
        elif key == 'model_overrides':
            if not isinstance(raw, dict) or len(raw) > 2000:
                raise MoneyError('自定义模型单价列表无效或过长')
            result[key] = {model_id(model): validate_rates(rates) for model, rates in raw.items()}
        elif key == 'pricing_sources':
            try:
                result[key] = usage_pricing.validate_sources(raw)
            except ValueError as error:
                raise MoneyError(str(error)) from None
        elif key == 'pricing_refresh_seconds':
            if type(raw) is not int or not 1800 <= raw <= 604800:
                raise MoneyError('公开价格更新间隔应在 30 分钟至 7 天之间')
            result[key] = raw
    return result


def read_json(path, maximum):
    if path.stat().st_size > maximum:
        raise MoneyError('本机费用设置文件过大')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise MoneyError('本机费用设置文件过大')
    return json.loads(raw)


def private_write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    if len(raw) > MAX_SETTINGS:
        raise MoneyError('自定义模型单价列表过大')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def preferences(home=None):
    home = vibecue_directory(home)
    result = dict(DEFAULTS, model_overrides={}, pricing_sources=usage_pricing.defaults())
    warning = None
    try:
        result.update(validate_patch(read_json(home / 'usage-display.json', MAX_SETTINGS)))
        if result['fx_mode'] == 'manual' and result['manual_rate'] is None:
            raise MoneyError('手动汇率尚未设置')
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        result = dict(DEFAULTS, model_overrides={}, pricing_sources=usage_pricing.defaults())
        warning = '费用显示设置不可读，当前使用默认显示；未修改原文件'
    return result, warning


def save_preferences(home, patch):
    home = vibecue_directory(home)
    checked = validate_patch(patch)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (home / 'usage-display.lock').open('a') as lock:
        os.chmod(lock.name, 0o600); fcntl.flock(lock, fcntl.LOCK_EX)
        current, _ = preferences(home)
        current.update(checked)
        if current['fx_mode'] == 'manual' and current['manual_rate'] is None:
            raise MoneyError('请填写大于零的手动汇率')
        private_write(home / 'usage-display.json', current)
    return settings(home)


def catalog(home=None, cc_path=None, prefs=None, now=None):
    """Exact model IDs only; never infer a price from a similar model name."""
    home = vibecue_directory(home)
    prefs = preferences(home)[0] if prefs is None else prefs
    # No account/settings tables are inspected, and a disabled CC source does
    # not open its database at all.
    if not any(row['enabled'] and row['kind'] == 'ccswitch' for row in prefs['pricing_sources']):
        return usage_pricing.combine(home, prefs, {}, False, 0, now)
    path = Path(cc_path) if cc_path is not None else Path.home() / '.cc-switch/cc-switch.db'
    prices = {}
    available, invalid = False, 0
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.25)) as source:
            source.execute('PRAGMA query_only=ON')
            rows = source.execute('SELECT model_id,display_name,' + ','.join(CC_COLUMNS) +
                                  ' FROM model_pricing LIMIT 10001').fetchall()
            if len(rows) > 10000:
                raise MoneyError('CC Switch 模型单价列表过长')
            available = True
            for identifier, name, *raw in rows:
                try:
                    identifier = model_id(identifier)
                    rates = validate_rates(dict(zip(RATE_KEYS, raw)))
                    prices[identifier] = dict(model=identifier, name=' '.join(str(name or identifier).split())[:180],
                                              source='ccswitch', **rates)
                except (ValueError, TypeError):
                    invalid += 1
    except (OSError, sqlite3.Error, ValueError):
        available = False
        prices = {}
    return usage_pricing.combine(home, prefs, prices, available, invalid, now)


def validate_rate_payload(value, now=None):
    now = time.time() if now is None else float(now)
    if (not isinstance(value, dict) or not isinstance(value.get('base'), str) or not isinstance(value.get('quote'), str) or
            value['base'].upper() != 'USD' or value['quote'].upper() != 'CNY'):
        raise MoneyError('汇率接口返回的币种不匹配')
    raw_date = value.get('date')
    if not isinstance(raw_date, str) or len(raw_date) != 10:
        raise MoneyError('汇率接口返回的日期无效')
    try:
        reference_date = date.fromisoformat(raw_date)
        today = datetime.fromtimestamp(now, timezone.utc).date()
    except (ValueError, TypeError, OverflowError):
        raise MoneyError('汇率接口返回的日期无效') from None
    if reference_date.isoformat() != raw_date or (reference_date - today).days > 0 or reference_date.year < 1999:
        raise MoneyError('汇率接口返回的日期无效')
    return {'base': 'USD', 'quote': 'CNY', 'rate': float(numeric(value.get('rate'), positive=True)), 'date': raw_date}


def cache(home, now=None):
    now = time.time() if now is None else float(now)
    try:
        raw = read_json(vibecue_directory(home) / 'usage-exchange.json', MAX_JSON)
        if not isinstance(raw, dict):
            return {}
        result = {}
        if raw.get('rate') is not None:
            result.update(validate_rate_payload(raw, now))
            updated = raw.get('updated_at')
            if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated) or updated < 0 or updated > now + 60:
                return {}
            result['updated_at'] = updated
        attempted = raw.get('attempted_at')
        if isinstance(attempted, (int, float)) and not isinstance(attempted, bool) and math.isfinite(attempted) and 0 <= attempted <= now + 60:
            result['attempted_at'] = attempted
        if raw.get('error'):
            result['error'] = '自动汇率更新失败，保留上次参考汇率；稍后可重试' if result.get('rate') is not None else '自动汇率暂不可用；可重试或设置手动汇率'
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def exchange(home=None, prefs=None, now=None):
    now = time.time() if now is None else float(now)
    prefs = preferences(home)[0] if prefs is None else prefs
    if prefs['fx_mode'] == 'manual':
        return {'rate': prefs['manual_rate'], 'source': '自定义汇率', 'date': None,
                'updated_at': None, 'stale': False, 'error': None, 'mode': 'manual', 'refresh_due': False}
    raw = cache(home, now)
    updated = raw.get('updated_at')
    stale = updated is None or now - updated >= RATE_TTL
    if raw.get('date'):
        stale = stale or (datetime.fromtimestamp(now, timezone.utc).date() - date.fromisoformat(raw['date'])).days > 7
    stale = bool(stale or raw.get('error'))
    failed_recently = bool(raw.get('error')) and now - raw.get('attempted_at', 0) < FAILURE_TTL
    return {'rate': raw.get('rate'), 'source': RATE_SOURCE, 'date': raw.get('date'),
            'updated_at': updated, 'stale': bool(stale or raw.get('error')), 'error': raw.get('error'),
            'mode': 'auto', 'refresh_due': bool(stale and not failed_recently)}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MoneyError('汇率接口重定向未被允许')


def fetch_rate(now=None):
    request = urllib.request.Request(RATE_URL, headers={'Accept': 'application/json', 'User-Agent': 'VCGo/0.26.0 exchange-rate'})
    # A fixed anonymous pair, no account identifiers, usage, API keys or bodies.
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=8) as response:
        data = response.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise MoneyError('汇率接口响应过大')
    return validate_rate_payload(json.loads(data), now)


def refresh(home=None, force=False, now=None):
    home = vibecue_directory(home)
    now = time.time() if now is None else float(now)
    prefs, _ = preferences(home)
    current = exchange(home, prefs, now)
    if prefs['fx_mode'] == 'manual' or (not force and not current['refresh_due']):
        return settings(home, now=now)
    old = cache(home, now)
    try:
        new = fetch_rate(now)
        new.update(updated_at=now, attempted_at=now)
    except Exception:
        new = old
        new['attempted_at'] = now
        new['error'] = '更新失败'
    private_write(home / 'usage-exchange.json', new)
    return settings(home, now=now)


def settings(home=None, cc_path=None, now=None):
    prefs, warning = preferences(home)
    prices, info = catalog(home, cc_path, prefs, now)
    return {'schema_version': 1, 'prefs': prefs, 'fx': exchange(home, prefs, now),
            'pricing': [prices[key] for key in sorted(prices)], 'settings_warning': warning,
            'default_pricing_sources': usage_pricing.defaults(),
            'estimated': True, **info}


def refresh_pricing(home=None, force=False, now=None):
    home = vibecue_directory(home)
    usage_pricing.refresh(home, preferences(home)[0], force=force, now=now)
    return settings(home, now=now)


def price_decimal(row, prices):
    selected = prices.get(row.get('model'))
    if selected is None:
        return None
    with localcontext() as ctx:
        ctx.prec = 96
        cost = Decimal(0)
        for token, key in zip(TOKEN_KEYS, RATE_KEYS):
            raw = row.get(token, 0)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw) or raw < 0 or int(raw) != raw:
                return None
            if raw == 0:
                continue
            if selected.get(key) is None:
                return None
            cost += Decimal(int(raw)) * Decimal(str(selected[key]))
        return cost / Decimal(1_000_000)


class _DecimalPriceMemo:
    """One decoration's bounded exact row costs, never a global value cache.

    Prices are the decoration's fixed snapshot. Source labels, request counts,
    FX multiplication and ordered subtotal addition remain outside this memo.
    A full memo keeps its existing rows instead of thrashing across repeated
    chronological scans; excess rows use the original exact function.
    """
    def __init__(self, prices):
        self.prices = prices
        self.rows = {}

    def __call__(self, row, prices):
        if prices is not self.prices:
            return price_decimal(row, prices)
        model = row.get('model')
        incoming, output = row.get('input_tokens', 0), row.get('output_tokens', 0)
        read, creation = row.get('cache_read_tokens', 0), row.get('cache_creation_tokens', 0)
        if type(model) is str and type(incoming) is int and type(output) is int and type(read) is int and type(creation) is int:
            # SQL's exact str/int projection is the common path. Its shape
            # explicitly identifies the types without allocating five pairs.
            key = (str, model, incoming, output, read, creation)
        else:
            values = model, incoming, output, read, creation
            if any(type(value) not in (str, int, float, bool, type(None)) for value in values):
                return price_decimal(row, prices)
            # bool == 1 and 1.0 == 1 cannot share a validation result.
            key = tuple((type(value), value) for value in values)
        cached = self.rows.get(key, _MISSING_ROW_COST)
        if cached is not _MISSING_ROW_COST:
            return cached
        cost = price_decimal(row, prices)
        if len(self.rows) < MAX_DECORATION_COST_ROWS:
            self.rows[key] = cost
        return cost


def folded_cost(rows, prices, rate):
    return _folded_cost(rows, prices, rate, price_decimal)


def _folded_cost(rows, prices, rate, row_cost):
    total, priced, unpriced = Decimal(0), 0, 0
    price_sources = set()
    with localcontext() as ctx:
        ctx.prec = 96
        for row in rows:
            requests = row.get('requests', 1)
            if isinstance(requests, bool) or not isinstance(requests, int) or requests < 0:
                requests = 1
            cost = row_cost(row, prices)
            if cost is None:
                unpriced += requests
            else:
                total += cost
                priced += requests
                price_sources.add(prices[row['model']]['source'])
        usd = float(total) if priced or not unpriced else None
        cny = float(total * Decimal(str(rate))) if (priced or not unpriced) and rate is not None else None
        return {'cost_usd': usd, 'cost_cny': cny, 'priced_requests': priced,
                'unpriced_requests': unpriced, 'cost_partial': bool(unpriced),
                'price_sources': sorted(price_sources)}


def decorate(home, result, aggregate_groups, cc_path=None, now=None, *, provider_groups=(),
             series_groups=(), heatmap_groups=None):
    """Decorate a filtered usage query; never performs network I/O.

    aggregate_groups: full filtered GROUP BY app,source,model, with requests and
    SUM of the four disjoint token columns. Visible tabs may contain only a page.
    Unknown models remain nullable, even when every known price is zero.
    """
    snapshot = settings(home, cc_path, now)
    prices = {row['model']: row for row in snapshot.pop('pricing')}
    rate = snapshot['fx']['rate']
    row_cost = _DecimalPriceMemo(prices)
    def fold(rows):
        return _folded_cost(rows, prices, rate, row_cost)
    result['money'] = snapshot
    result['summary'].update(fold(aggregate_groups))
    for row in result.get('logs', []):
        row.update(fold([dict(row, requests=1)]))
        selected = prices.get(row.get('model'))
        row['price_source_name'] = selected.get('source_name', selected.get('source')) if selected else None
        for token, component in zip(TOKEN_KEYS, RATE_KEYS):
            amount, unit = row.get(token, 0), selected.get(component) if selected else None
            with localcontext() as ctx:
                ctx.prec = 96
                usd = Decimal(0) if amount == 0 else (Decimal(amount) * Decimal(str(unit)) / Decimal(1_000_000) if unit is not None else None)
                row[component + '_cost_usd'] = float(usd) if usd is not None else None
                row[component + '_cost_cny'] = float(usd * Decimal(str(rate))) if usd is not None and rate is not None else (0.0 if usd == 0 else None)
    for row in result.get('models', []):
        groups = [group for group in aggregate_groups if group.get('model') == row.get('model')]
        row.update(fold(groups))
    for row in result.get('sources', []):
        groups = [group for group in aggregate_groups if group.get('source') == row.get('source') and group.get('app') == row.get('app')]
        row.update(fold(groups))
    for row in result.get('providers', []):
        groups = [group for group in provider_groups if group.get('provider_key') == row.get('provider_key')]
        row.update(fold(groups))
    for row, groups in series_groups:
        row.update(fold(groups))
    if heatmap_groups is not None:
        result['heatmap_summary'].update(fold(heatmap_groups))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='VibeCoding Go API 估算费用与参考汇率')
    parser.add_argument('command', choices=('settings', 'save', 'refresh', 'refresh-pricing'))
    parser.add_argument('--home')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == 'settings':
            result = settings(args.home)
        elif args.command == 'refresh':
            result = refresh(args.home, args.force)
        elif args.command == 'refresh-pricing':
            result = refresh_pricing(args.home, args.force)
        else:
            raw = sys.stdin.buffer.read(MAX_SETTINGS + 1)
            if len(raw) > MAX_SETTINGS:
                raise MoneyError('费用显示设置过大')
            result = save_preferences(args.home, json.loads(raw))
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error):
        print(json.dumps({'error': '费用显示设置或汇率暂不可用，请检查输入后重试'}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
