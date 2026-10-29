"""Local presentation metadata for verified, saved Codex thread settings.

The model catalogue describes capabilities, not a composer's unsubmitted state.
Only the catalogue's model names and service-tier identifiers/names survive the
read. No model selection, protocol projection, or raw setting is changed here.
"""
import hashlib
import json
import math
import stat
import time

from .events import codex_directory


MAX_CATALOG_BYTES = 8 * 1024 * 1024
_PRESENTATION_KEYS = frozenset(('model_display_name', 'speed_kind', 'speed_label',
                               'speed_source', 'speed_matches_model', 'speed_message'))
_BOUNDARY = 'Codex 输入框中尚未提交的模型、强度或速度选择可能尚未写入已保存设置。'


def _label(value, limit=180):
    if (not isinstance(value, str) or not value.strip() or len(value.encode('utf-8')) > limit
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        return None
    return value.strip()


def _stamp(value):
    try:
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    except OverflowError:
        return None


def _fresh(settings, now):
    verified, expires = _stamp(settings.get('verified_at')), _stamp(settings.get('expires_at'))
    return (settings.get('status') in ('current', 'partial')
            and settings.get('source') == 'codex_desktop'
            and verified is not None and expires is not None
            and verified <= expires and verified - 5 <= now <= expires <= verified + 60)


def _tier_kind(identifier, name):
    """Match Codex's tier icon normalization, independently of model effort."""
    normalized = name.strip().lower()
    if identifier in ('priority', 'fast') or normalized == 'fast':
        return 'fast'
    if identifier == 'ultrafast' or normalized == 'ultrafast':
        return 'ultrafast'
    return None


def _catalogue(value):
    rows = value.get('models') if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > 4096:
        return {}
    result, duplicates = {}, set()
    for row in rows:
        if not isinstance(row, dict) or not (slug := _label(row.get('slug'))):
            continue
        if slug in result:
            duplicates.add(slug)
            continue
        tiers = row.get('service_tiers')
        # Missing/malformed capability data differs from a confirmed empty list.
        clean_tiers = None
        if isinstance(tiers, list) and len(tiers) <= 32:
            clean_tiers = {}
            for tier in tiers:
                identifier = _label(tier.get('id'), 40) if isinstance(tier, dict) else None
                name = _label(tier.get('name'), 80) if isinstance(tier, dict) else None
                if identifier is None or name is None or identifier in clean_tiers:
                    clean_tiers = None
                    break
                clean_tiers[identifier] = name
        result[slug] = {'display_name': _label(row.get('display_name')),
                        'service_tiers': clean_tiers,
                        'default_service_tier': _label(row.get('default_service_tier'), 40)}
    for slug in duplicates:
        result.pop(slug, None)
    return result


class ModelPresentation:
    """Cache a bounded whitelist by file identity; forget it on read failure."""
    def __init__(self, codex_home=None, *, clock=time.time):
        self.path = codex_directory(codex_home) / 'models_cache.json'
        self.clock = clock
        self._signature = None
        self._digest = None
        self._models = {}

    def _forget(self):
        self._signature = self._digest = None
        self._models = {}

    @staticmethod
    def _identity(stat):
        return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)

    def _read(self):
        try:
            info = self.path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CATALOG_BYTES:
                self._forget()
                return {}
            signature = self._identity(info)
            if signature == self._signature:
                return self._models
            with self.path.open('rb') as stream:
                raw = stream.read(MAX_CATALOG_BYTES + 1)
            if len(raw) > MAX_CATALOG_BYTES or self._identity(self.path.stat()) != signature:
                self._forget()
                return {}
            digest = hashlib.sha256(raw).digest()
            if digest != self._digest:
                models = _catalogue(json.loads(raw))
                self._models, self._digest = models, digest
            self._signature = signature
            return self._models
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            self._forget()
            return {}

    def decorate(self, settings):
        if not isinstance(settings, dict):
            return settings
        result = {key: value for key, value in settings.items() if key not in _PRESENTATION_KEYS}
        if not _fresh(result, self.clock()):
            return result
        model = self._read().get(result.get('model')) if isinstance(result.get('model'), str) else None
        if model and model['display_name']:
            result['model_display_name'] = model['display_name']
        tier = _label(result.get('service_tier'), 40)
        kind, label, matches = 'unknown', '速度待核实', None
        detail = '本机模型目录尚未确认此已保存速度档位。'
        if result.get('speed_status') == 'default' and result.get('service_tier') is None:
            kind, label = 'default', '速度跟随默认'
            detail = '此对话没有显式速度覆盖，实际默认速度保持待核实。'
        elif result.get('speed_status') == 'explicit' and tier in ('default', 'standard'):
            kind, label, matches = 'standard', '标准速度', True
            detail = '此对话已显式保存标准速度。'
        elif result.get('speed_status') == 'explicit' and tier:
            tiers = model['service_tiers'] if model else None
            if tiers is not None:
                matched = (next(((identifier, name) for identifier, name in tiers.items()
                                 if _tier_kind(identifier, name) == 'fast' or name.strip().lower() == 'priority'), None)
                           if tier == 'fast' else (tier, tiers[tier]) if tier in tiers else None)
                matches = matched is not None
                if not matches:
                    kind, label = 'unsupported', '速度档位待核实'
                    detail = '本机模型目录未列出此模型的已保存速度档位。'
                else:
                    normalized_kind = _tier_kind(*matched)
                    if normalized_kind is not None:
                        kind = normalized_kind
                        label = 'Fast' if kind == 'fast' else 'Ultra Fast'
                        detail = '已按本机模型目录核对已保存速度档位。'
                    else:
                        detail = '本机模型目录列出了此档位，其速度名称保持待核实。'
        raw = 'null' if result.get('service_tier') is None else tier or '未提供'
        result.update(speed_kind=kind, speed_label=label,
                      speed_source='codex_saved_thread_settings', speed_matches_model=matches,
                      speed_message=f'Codex 已保存的对话设置：serviceTier={raw}。{detail}{_BOUNDARY}')
        return result
