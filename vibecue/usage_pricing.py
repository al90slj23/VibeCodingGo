"""Public, anonymous price catalogs and an ordered exact-model fallback queue.

Only normalized scalar USD / million-token prices are retained. Remote text is
never executed; settings/usage reads consume local snapshots without networking.
"""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.parse
import urllib.request

MAX_RESPONSE = 16 * 1024 * 1024
MAX_CACHE = 12 * 1024 * 1024
MAX_MODELS = 20000
REFRESH_SECONDS = 21600
FAILURE_SECONDS = 900
KINDS = frozenset(('custom', 'ccswitch', 'basellm', 'models_dev', 'litellm', 'openrouter', 'vercel', 'json'))
RATE_KEYS = ('input', 'output', 'cache_read', 'cache_creation')
DEFAULT_SOURCES = [
    {'id': 'custom', 'name': '手动模型单价', 'kind': 'custom', 'enabled': True},
    {'id': 'ccswitch', 'name': 'CC Switch 本机价格', 'kind': 'ccswitch', 'enabled': True},
    {'id': 'basellm', 'name': 'BaseLLM', 'kind': 'basellm', 'enabled': True, 'url': 'https://basellm.github.io/llm-metadata/api/newapi/ratio_config-v1-base.json'},
    {'id': 'models-dev', 'name': 'models.dev', 'kind': 'models_dev', 'enabled': True, 'url': 'https://models.dev/api.json'},
    {'id': 'litellm', 'name': 'LiteLLM', 'kind': 'litellm', 'enabled': True, 'url': 'https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json'},
    {'id': 'openrouter', 'name': 'OpenRouter', 'kind': 'openrouter', 'enabled': True, 'url': 'https://openrouter.ai/api/v1/models'},
    {'id': 'vercel', 'name': 'Vercel AI Gateway', 'kind': 'vercel', 'enabled': True, 'url': 'https://ai-gateway.vercel.sh/v1/models'},
]


def defaults():
    return [dict(row) for row in DEFAULT_SOURCES]


def validate_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(ch) < 33 for ch in value):
        raise ValueError('请填写有效的公开 HTTPS JSON 地址')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('价格来源地址无效') from None
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or port not in (None, 443):
        raise ValueError('价格来源需要不含授权信息的公开 HTTPS 地址')
    host = parsed.hostname.lower().rstrip('.')
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        raise ValueError('价格来源需要公开 HTTPS 地址')
    try: address = ipaddress.ip_address(host)
    except ValueError: address = None
    if address is not None and not address.is_global:
        raise ValueError('价格来源需要公开 HTTPS 地址')
    for key, _ in urllib.parse.parse_qsl(parsed.query):
        if re.search(r'(?:^|[_-])(token|key|password|secret|authorization|auth)(?:$|[_-])', key.lower()) or key.lower() in ('apikey', 'accesstoken'):
            raise ValueError('价格来源仅支持无授权的公开目录')
    return value


def validate_sources(value):
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError('价格来源最多支持 20 个')
    result, identities, local = [], set(), set()
    for raw in value:
        if not isinstance(raw, dict) or set(raw) - {'id', 'name', 'kind', 'url', 'enabled'}:
            raise ValueError('价格来源包含无效字段')
        identity, name, kind = raw.get('id'), raw.get('name'), raw.get('kind')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identity) or identity in identities:
            raise ValueError('价格来源标识无效或重复')
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(ch) < 32 for ch in name):
            raise ValueError('请填写价格来源名称（最多 80 字）')
        if kind not in KINDS or type(raw.get('enabled')) is not bool:
            raise ValueError('价格来源格式或启用状态无效')
        row = dict(id=identity, name=name.strip(), kind=kind, enabled=raw['enabled'])
        if kind in ('custom', 'ccswitch'):
            if kind in local or raw.get('url'):
                raise ValueError('本机价格来源不可重复，也不需要 URL')
            local.add(kind)
        else:
            row['url'] = validate_url(raw.get('url'))
        identities.add(identity); result.append(row)
    return result


def identifier(value):
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 180 or any(ord(ch) < 32 for ch in value):
        raise ValueError('无效模型标识')
    return value


def rate(value, factor=1):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 80:
        raise ValueError('无效单价')
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > Decimal('1e15') or (number and number.adjusted() < -30):
            raise ValueError('无效单价')
        number *= factor
        if number > Decimal('1e15'):
            raise ValueError('无效单价')
        return float(number)
    except InvalidOperation:
        raise ValueError('无效单价') from None


def linear_expression(value):
    """Recognize the documented constant-linear BaseLLM/NewAPI token DSL.

    Coefficients already mean USD / 1M. Its p includes cache tokens unless cr
    or cc is explicitly split out; mirror that rule in our disjoint columns.
    Conditions, other functions, TTL-specific cc1h, and code are unsupported.
    This is a grammar recognizer, never an expression evaluator.
    """
    if not isinstance(value, str) or len(value) > 1024: return None
    wrapper = re.fullmatch(r'\s*tier\(\s*"(?:standard|base|default)"\s*,\s*(.*?)\s*\)\s*', value)
    if wrapper is None: return None
    number = r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
    variable = r'(?:cr|cc|p|c)'
    term = r'(?:' + variable + r'\s*\*\s*' + number + r'|' + number + r'\s*\*\s*' + variable + r')'
    body = wrapper.group(1)
    if re.fullmatch(r'\s*' + term + r'(?:\s*\+\s*' + term + r')*\s*', body) is None: return None
    values = {}
    token = re.compile(r'(' + variable + r')\s*\*\s*(' + number + r')|(' + number + r')\s*\*\s*(' + variable + r')')
    try:
        for found in token.finditer(body):
            name, amount = (found.group(1), found.group(2)) if found.group(1) else (found.group(4), found.group(3))
            if name in values: return None
            values[name] = rate(amount)
        if 'p' not in values or 'c' not in values: return None
        return dict(input=values['p'], output=values['c'], cache_read=values.get('cr', values['p']), cache_creation=values.get('cc', values['p']))
    except ValueError: return None


def normalize(kind, payload):
    """No fuzzy names, guessed cache discounts, non-token prices, or eval."""
    prices, invalid = {}, 0
    def add(model, raw, name=None, factor=1, **extra):
        nonlocal invalid
        try:
            model = identifier(model)
            values = {key: rate(raw.get(key), factor) for key in RATE_KEYS}
            if values['input'] is None or values['output'] is None:
                raise ValueError('缺少基础输入或输出单价')
            if len(prices) >= MAX_MODELS and model not in prices:
                raise ValueError('模型目录过长')
            prices.setdefault(model, dict(model=model, name=' '.join(str(name or model).split())[:180], **values, **extra))
        except (ValueError, TypeError, AttributeError):
            invalid += 1
    if not isinstance(payload, dict):
        raise ValueError('价格目录必须是 JSON 对象')
    if kind == 'json':
        models = payload.get('models')
        if not isinstance(models, dict):
            raise ValueError('标准 JSON 缺少 models 对象')
        for model, row in models.items():
            if isinstance(row, dict): add(model, row, row.get('name'))
            else: invalid += 1
    elif kind == 'models_dev':
        # Bare IDs come only from first-party providers. Other providers keep
        # their namespace, so discounted reseller names cannot replace origin.
        native = {'openai', 'anthropic', 'google', 'deepseek', 'xai', 'mistral', 'cohere', 'moonshotai', 'minimax', 'zhipuai', 'qwen', 'perplexity', 'nvidia'}
        ordered = sorted(payload.items(), key=lambda item: (item[0] not in native, item[0]))
        for provider, data in ordered:
            if not isinstance(data, dict) or not isinstance(data.get('models'), dict): continue
            for model, row in data['models'].items():
                if not isinstance(row, dict) or not isinstance(row.get('cost'), dict): continue
                cost = row['cost']; model = row.get('id') or model
                values = dict(input=cost.get('input'), output=cost.get('output'), cache_read=cost.get('cache_read'), cache_creation=cost.get('cache_write'))
                if provider in native and isinstance(model, str) and ('/' not in model or model.startswith(provider + '/')):
                    add(model, values, row.get('name'), provider=provider)
                scoped = model if isinstance(model, str) and model.startswith(provider + '/') else provider + '/' + str(model)
                add(scoped, values, row.get('name'), provider=provider)
    elif kind == 'litellm':
        for model, row in payload.items():
            if not isinstance(row, dict) or row.get('mode') not in (None, 'chat', 'completion', 'responses'): continue
            add(model, dict(input=row.get('input_cost_per_token'), output=row.get('output_cost_per_token'), cache_read=row.get('cache_read_input_token_cost'), cache_creation=row.get('cache_creation_input_token_cost')), model, 1_000_000)
    elif kind in ('openrouter', 'vercel'):
        if not isinstance(payload.get('data'), list): raise ValueError('目录缺少 data 数组')
        for row in payload['data']:
            if not isinstance(row, dict) or not isinstance(row.get('pricing'), dict): continue
            if kind == 'vercel' and row.get('type') not in (None, 'language'): continue
            pricing = row['pricing']
            add(row.get('id'), dict(input=pricing.get('prompt' if kind == 'openrouter' else 'input'), output=pricing.get('completion' if kind == 'openrouter' else 'output'), cache_read=pricing.get('input_cache_read'), cache_creation=pricing.get('input_cache_write')), row.get('name'), 1_000_000)
    elif kind == 'basellm':
        data = payload.get('data', payload)
        if not isinstance(data, dict) or not (isinstance(data.get('model_ratio'), dict) or isinstance(data.get('billing_expr'), dict)):
            raise ValueError('目录缺少模型基础价格')
        def mapping(model, key):
            value = data.get(key, {}); return value.get(model) if isinstance(value, dict) else None
        ratios = data.get('model_ratio') if isinstance(data.get('model_ratio'), dict) else {}
        expressions = data.get('billing_expr') if isinstance(data.get('billing_expr'), dict) else {}
        for model, ratio in ratios.items():
            if mapping(model, 'billing_expr') or mapping(model, 'model_price') is not None or mapping(model, 'billing_mode') not in (None, '', 'ratio') or mapping(model, 'billing_unit') not in (None, '', 'token', 'tokens', 'million_tokens'):
                continue
            try:
                base = rate(ratio, 2)
                def scaled(key):
                    relative = rate(mapping(model, key)); return None if relative is None or base is None else rate(Decimal(str(base)) * Decimal(str(relative)))
                add(model, dict(input=base, output=scaled('completion_ratio'), cache_read=scaled('cache_ratio'), cache_creation=scaled('create_cache_ratio')))
            except ValueError: invalid += 1
        for model, expression in expressions.items():
            if mapping(model, 'model_price') is not None or mapping(model, 'billing_mode') not in (None, '', 'tiered_expr') or mapping(model, 'billing_unit') not in (None, '', 'token', 'tokens', 'million_tokens'): continue
            values = linear_expression(expression)
            if values is not None: add(model, values)
    else:
        raise ValueError('未知价格目录格式')
    if not prices:
        raise ValueError('目录没有可用的 Token 基础单价')
    return prices, invalid


def fingerprint(source):
    return hashlib.sha256((source['kind'] + '\n' + source.get('url', '')).encode()).hexdigest()


def cache_path(home, source):
    return Path(home) / 'usage-pricing' / (source['id'] + '-' + fingerprint(source)[:20] + '.json')


def read_cache(home, source, now=None):
    now = time.time() if now is None else now
    try:
        path = cache_path(home, source)
        if path.stat().st_size > MAX_CACHE: return {}
        raw = json.loads(path.read_bytes())
        if not isinstance(raw, dict) or raw.get('fingerprint') != fingerprint(source): return {}
        for key in ('updated_at', 'attempted_at'):
            value = raw.get(key)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > now + 60): return {}
        models = raw.get('models', {})
        if not isinstance(models, dict) or len(models) > MAX_MODELS: return {}
        clean = {}
        for model, row in models.items():
            model = identifier(model)
            if not isinstance(row, dict): return {}
            values = {key: rate(row.get(key)) for key in RATE_KEYS}
            if values['input'] is None or values['output'] is None: return {}
            clean[model] = dict(model=model, name=' '.join(str(row.get('name') or model).split())[:180], **values)
        return dict(fingerprint=raw['fingerprint'], models=clean, updated_at=raw.get('updated_at'), attempted_at=raw.get('attempted_at'), error=bool(raw.get('error')), invalid_prices=max(0, int(raw.get('invalid_prices', 0))), content_sha256=raw.get('content_sha256'))
    except (OSError, ValueError, TypeError, OverflowError): return {}


def write_cache(home, source, value):
    path = cache_path(home, source); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()
    if len(data) > MAX_CACHE: raise ValueError('标准化价格目录过大')
    fd, temp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as stream: stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp): os.unlink(temp)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Each configured catalog has an explicit endpoint. A changed location
        # must be edited rather than forwarding a request to a hidden target.
        raise ValueError('价格来源发生重定向，请更新地址')


def fetch_source(source):
    url = validate_url(source['url'])
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'VibeCue/0.22 public-pricing'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE: raise ValueError('价格目录响应过大')
    return normalize(source['kind'], json.loads(data))


def refresh(home, prefs, force=False, now=None):
    now = time.time() if now is None else now
    root = Path(home); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / 'usage-pricing-refresh.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        due = []
        for source in prefs['pricing_sources']:
            if not source['enabled'] or source['kind'] in ('custom', 'ccswitch'): continue
            old = read_cache(home, source, now)
            if not force:
                if old.get('error') and now - (old.get('attempted_at') or 0) < FAILURE_SECONDS: continue
                if not old.get('error') and old.get('updated_at') is not None and now - old['updated_at'] < prefs['pricing_refresh_seconds']: continue
            due.append((source, old))
        def update(pair):
            source, old = pair
            try:
                models, invalid = fetch_source(source)
                digest = hashlib.sha256(json.dumps(models, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                new = dict(fingerprint=fingerprint(source), models=models, updated_at=now, attempted_at=now, error=False, invalid_prices=invalid, content_sha256=digest)
            except Exception:
                new = dict(old, fingerprint=fingerprint(source), attempted_at=now, error=True)
            try: write_cache(home, source, new)
            except (OSError, ValueError): pass
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(update, due))


def combine(home, prefs, cc_prices, cc_available, cc_invalid, now=None):
    now = time.time() if now is None else now
    selected, statuses, invalid, available, stale_used = {}, [], 0, False, False
    next_refresh = []
    for source in prefs['pricing_sources']:
        state = dict(source, model_count=0, updated_at=None, stale=False, status='disabled', message='已停用')
        models, stale, updated, bad = {}, False, None, 0
        if source['enabled']:
            if source['kind'] == 'custom':
                models = {model: dict(model=model, name=model, **rates) for model, rates in prefs['model_overrides'].items()}
                state.update(status='available', message='本机手动设置；按队列优先级采用')
                available = available or bool(models)
            elif source['kind'] == 'ccswitch':
                models, bad = cc_prices, cc_invalid
                state.update(status='available' if cc_available else 'unavailable', message='只读 model_pricing 单价表' if cc_available else '本机价格表暂不可读，将继续查找后续来源')
                available = available or cc_available
            else:
                old = read_cache(home, source, now); models = old.get('models', {}); updated = old.get('updated_at'); bad = old.get('invalid_prices', 0)
                next_refresh.append((old.get('attempted_at') or 0) + FAILURE_SECONDS if old.get('error') else updated + prefs['pricing_refresh_seconds'] if updated is not None else now)
                state.update(attempted_at=old.get('attempted_at'), content_sha256=old.get('content_sha256'))
                stale = bool(models and (old.get('error') or updated is None or now - updated >= prefs['pricing_refresh_seconds']))
                state.update(status='stale' if stale else 'available' if models else 'error' if old.get('error') else 'pending', message='保留上次成功价格；更新暂未完成' if stale else '公开基础价已缓存' if models else '尚未取得价格；将继续查找后续来源')
                available = available or bool(models)
            state.update(model_count=len(models), updated_at=updated, stale=stale)
            invalid += bad
            for model, row in models.items():
                if model in selected or len(selected) >= MAX_MODELS: continue
                selected[model] = dict(row, source=source['id'], source_name=source['name'], source_updated_at=updated, source_stale=stale)
                stale_used = stale_used or stale
        statuses.append(state)
    state = 'partial' if invalid or stale_used else 'available' if available else 'unavailable'
    note = '按队列从上到下精确匹配模型；首个提供该模型基础输入和输出价格的来源生效。缺失缓存单价不按免费处理。公开基础价不包含长上下文阶梯、平台优惠或税费。'
    next_at = min(next_refresh) if next_refresh else None
    return selected, dict(pricing_status=state, pricing_source='自定义优先级来源队列', pricing_note=note, invalid_prices=invalid, source_status=statuses,
                          pricing_refresh_due=next_at is not None and next_at <= now, pricing_next_refresh_at=next_at)
