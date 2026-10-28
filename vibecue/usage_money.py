"""API-price estimates and anonymous USD/CNY reference rates.

Only CC Switch's six public model-pricing columns are read. Its settings,
accounts, credentials and OAuth state are never opened. Token accounting stays
in usage.sqlite3; price changes only affect the displayed estimate.
"""
from __future__ import annotations

import argparse
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

RATE_URL = 'https://api.frankfurter.dev/v2/rate/usd/cny'
RATE_SOURCE = 'Frankfurter · USD/CNY 每日参考汇率'
RATE_TTL = 6 * 60 * 60
FAILURE_TTL = 15 * 60
MAX_JSON = 64 * 1024
MAX_SETTINGS = 1024 * 1024
RATE_KEYS = ('input', 'output', 'cache_read', 'cache_creation')
TOKEN_KEYS = ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens')
CC_COLUMNS = ('input_cost_per_million', 'output_cost_per_million',
              'cache_read_cost_per_million', 'cache_creation_cost_per_million')
DEFAULTS = {'currency': 'CNY', 'fx_mode': 'auto', 'manual_rate': None, 'model_overrides': {}}


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
    result = dict(DEFAULTS, model_overrides={})
    warning = None
    try:
        result.update(validate_patch(read_json(home / 'usage-display.json', MAX_SETTINGS)))
        if result['fx_mode'] == 'manual' and result['manual_rate'] is None:
            raise MoneyError('手动汇率尚未设置')
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError):
        result = dict(DEFAULTS, model_overrides={})
        warning = '费用显示设置不可读，当前使用默认显示；未修改原文件'
    return result, warning


def save_preferences(home, patch):
    home = vibecue_directory(home)
    checked = validate_patch(patch)
    current, _ = preferences(home)
    current.update(checked)
    if current['fx_mode'] == 'manual' and current['manual_rate'] is None:
        raise MoneyError('请填写大于零的手动汇率')
    private_write(home / 'usage-display.json', current)
    return settings(home)


def catalog(home=None, cc_path=None, prefs=None):
    """Exact model IDs only; never infer a price from a similar model name."""
    home = vibecue_directory(home)
    prefs = preferences(home)[0] if prefs is None else prefs
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
    for identifier, rates in prefs['model_overrides'].items():
        prices[identifier] = dict(model=identifier, name=identifier, source='custom', **rates)
    state = 'partial' if invalid or (prices and not available) else 'available' if available else 'unavailable'
    note = ('按精确模型标识匹配 CC Switch 单价；自定义单价优先。未知模型保持未定价。' if available else
            'CC Switch 模型单价当前不可读；仅使用已有自定义单价，未知模型保持未定价。')
    if invalid:
        note += ' 部分无效单价已跳过。'
    return prices, {'pricing_status': state, 'pricing_source': 'CC Switch model_pricing / 自定义',
                    'pricing_note': note, 'invalid_prices': invalid}


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
    request = urllib.request.Request(RATE_URL, headers={'Accept': 'application/json', 'User-Agent': 'VibeCue/1.0 exchange-rate'})
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
    prices, info = catalog(home, cc_path, prefs)
    return {'schema_version': 1, 'prefs': prefs, 'fx': exchange(home, prefs, now),
            'pricing': [prices[key] for key in sorted(prices)], 'settings_warning': warning,
            'estimated': True, **info}


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
            cost += Decimal(int(raw)) * Decimal(str(selected[key]))
        return cost / Decimal(1_000_000)


def folded_cost(rows, prices, rate):
    total, priced, unpriced = Decimal(0), 0, 0
    price_sources = set()
    with localcontext() as ctx:
        ctx.prec = 96
        for row in rows:
            requests = row.get('requests', 1)
            if isinstance(requests, bool) or not isinstance(requests, int) or requests < 0:
                requests = 1
            cost = price_decimal(row, prices)
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


def decorate(home, result, aggregate_groups, cc_path=None, now=None):
    """Decorate a filtered usage query; never performs network I/O.

    aggregate_groups: full filtered GROUP BY app,source,model, with requests and
    SUM of the four disjoint token columns. Visible tabs may contain only a page.
    Unknown models remain nullable, even when every known price is zero.
    """
    snapshot = settings(home, cc_path, now)
    prices = {row['model']: row for row in snapshot.pop('pricing')}
    rate = snapshot['fx']['rate']
    result['money'] = snapshot
    result['summary'].update(folded_cost(aggregate_groups, prices, rate))
    for row in result.get('logs', []):
        row.update(folded_cost([dict(row, requests=1)], prices, rate))
    for row in result.get('models', []):
        groups = [group for group in aggregate_groups if group.get('model') == row.get('model')]
        row.update(folded_cost(groups, prices, rate))
    for row in result.get('sources', []):
        groups = [group for group in aggregate_groups if group.get('source') == row.get('source') and group.get('app') == row.get('app')]
        row.update(folded_cost(groups, prices, rate))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='VibeCue API 估算费用与参考汇率')
    parser.add_argument('command', choices=('settings', 'save', 'refresh'))
    parser.add_argument('--home')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        if args.command == 'settings':
            result = settings(args.home)
        elif args.command == 'refresh':
            result = refresh(args.home, args.force)
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
